from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import discord

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from embeds.macro_event_embeds import build_macro_event_embed
from services.macro_calendar import get_next_macro_events
from services.massive_econ_client import build_massive_client_from_env, normalize_massive_payload

ET = ZoneInfo("America/New_York")


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _et_now() -> datetime:
    return datetime.now(tz=ET)


def _econ_key(series: str) -> str:
    """Namespace econ keys under the TNT Redis prefix when available."""

    try:
        from tnt_redis import rkey as _rkey

        return _rkey("econ", "latest", str(series))
    except Exception:
        return f"econ:latest:{str(series)}"


ECON_KEYS = {
    "treasury_yields": _econ_key("treasury_yields"),
    "inflation": _econ_key("inflation"),
    "inflation_expectations": _econ_key("inflation_expectations"),
    "labor_market": _econ_key("labor_market"),
}

try:
    from tnt_redis import rkey as _rkey

    ECON_META_KEY = _rkey("econ", "meta", "last_refresh")
except Exception:
    ECON_META_KEY = "econ:meta:last_refresh"


def _redis_set_json(r, key: str, obj: Dict[str, Any], ttl_s: int = 0) -> None:
    s = json.dumps(obj, separators=(",", ":"))
    if ttl_s and ttl_s > 0:
        r.setex(key, int(ttl_s), s)
    else:
        r.set(key, s)


def _redis_get_json(r, key: str) -> Optional[Dict[str, Any]]:
    raw = r.get(key)
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def refresh_econ_cache(redis_client) -> Dict[str, Any]:
    """Pull latest values from Massive and store normalized blobs in Redis."""

    c = build_massive_client_from_env()
    now = _utc_now_iso()

    meta: Dict[str, Any] = {"ok": True, "updated_utc": now, "last_error": None, "series": {}}
    try:
        tre = normalize_massive_payload("treasury_yields", c.get_treasury_yields(limit=1, sort="date.desc"))
        inf = normalize_massive_payload("inflation", c.get_inflation(limit=1, sort="date.desc"))
        iex = normalize_massive_payload("inflation_expectations", c.get_inflation_expectations(limit=1, sort="date.desc"))
        lab = normalize_massive_payload("labor_market", c.get_labor_market(limit=1, sort="date.desc"))

        _redis_set_json(redis_client, ECON_KEYS["treasury_yields"], tre)
        _redis_set_json(redis_client, ECON_KEYS["inflation"], inf)
        _redis_set_json(redis_client, ECON_KEYS["inflation_expectations"], iex)
        _redis_set_json(redis_client, ECON_KEYS["labor_market"], lab)

        meta["series"] = {
            "treasury_yields": {"asof": tre.get("asof_date")},
            "inflation": {"asof": inf.get("asof_date")},
            "inflation_expectations": {"asof": iex.get("asof_date")},
            "labor_market": {"asof": lab.get("asof_date")},
        }
    except Exception as e:
        meta["ok"] = False
        meta["last_error"] = f"{type(e).__name__}:{e}"

    _redis_set_json(redis_client, ECON_META_KEY, meta)
    return meta


def _stage_times(event_dt: datetime) -> List[tuple[str, datetime]]:
    return [
        ("T-24H", event_dt - timedelta(hours=24)),
        ("T-2H", event_dt - timedelta(hours=2)),
        ("T-15M", event_dt - timedelta(minutes=15)),
        ("NOW", event_dt),
        ("END", event_dt + timedelta(minutes=30)),
    ]


def _post_key(event_id: str, stage: str) -> str:
    try:
        from tnt_redis import rkey as _rkey

        return _rkey("macro", "posted", str(event_id), str(stage))
    except Exception:
        return f"macro:posted:{event_id}:{stage}"


async def autopost_macro(bot: discord.Client, redis_client) -> None:
    """Post macro cards into the calendar channel at stage times."""

    channel_id = int(os.getenv("TNT_CALENDAR_EARNINGS_CHANNEL_ID", "0") or "0")
    if not channel_id:
        return

    now_et = _et_now()
    events = get_next_macro_events(now_et, horizon_days=14)
    if not events:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return

    econ_infl = _redis_get_json(redis_client, ECON_KEYS["inflation"])
    econ_lab = _redis_get_json(redis_client, ECON_KEYS["labor_market"])
    econ_tre = _redis_get_json(redis_client, ECON_KEYS["treasury_yields"])

    for ev in events[:10]:
        for stage, stage_dt in _stage_times(ev.dt_et):
            if abs((now_et - stage_dt).total_seconds()) > 5 * 60:
                continue

            k = _post_key(ev.id, stage)
            try:
                if redis_client.get(k):
                    continue
            except Exception:
                # if redis is down, don't spam (fail closed)
                continue

            econ_blob: Optional[Dict[str, Any]]
            t = (ev.title or "").upper()
            if "CPI" in t:
                econ_blob = econ_infl
            elif "FOMC" in t or "POWELL" in t:
                econ_blob = econ_tre
            elif "GDP" in t:
                econ_blob = econ_lab
            else:
                econ_blob = econ_tre

            embed = build_macro_event_embed(
                event_title=f"{ev.title} ({stage})" if stage != "NOW" else f"{ev.title} (Release)",
                event_id=ev.id,
                dt_et=ev.dt_et,
                importance=ev.importance,
                blackout_before_min=ev.blackout_before_min,
                blackout_after_min=ev.blackout_after_min,
                now_et=now_et,
                econ_latest=econ_blob,
            )

            try:
                await channel.send(embed=embed)
                redis_client.setex(k, 60 * 60 * 24 * 30, "1")
            except Exception:
                pass


async def run_macro_loops(bot: discord.Client, redis_client) -> None:
    """Runs forever: refresh econ cache (30m) + autopost macro cards (5m)."""

    last_econ = 0.0
    last_macro = 0.0

    while True:
        now = time.time()

        if now - last_econ >= 30 * 60:
            try:
                refresh_econ_cache(redis_client)
            except Exception:
                pass
            last_econ = now

        if now - last_macro >= 5 * 60:
            try:
                await autopost_macro(bot, redis_client)
            except Exception:
                pass
            last_macro = now

        await asyncio.sleep(5)
