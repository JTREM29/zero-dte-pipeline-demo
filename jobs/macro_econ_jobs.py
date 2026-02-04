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
from embeds.macro_regime_embeds import build_macro_regime_embed
from services.macro_calendar import get_next_macro_events, macro_schedule_path_str, validate_macro_schedule
from services.massive_econ_client import build_massive_client_from_env, normalize_massive_payload
from services.macro_regime import compute_and_cache_macro_regime, get_cached_macro_regime

ET = ZoneInfo("America/New_York")

DEFAULT_CALENDAR_CHANNEL_ID = "1462948320229200065"


def _calendar_channel_id() -> int:
    raw = (os.getenv("TNT_CALENDAR_EARNINGS_CHANNEL_ID") or os.getenv("CALENDAR_EARNINGS_CHANNEL_ID") or "").strip()
    if not raw:
        raw = DEFAULT_CALENDAR_CHANNEL_ID
    try:
        return int(raw)
    except Exception:
        return 0


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
        # Keep 2 records for simple trend/shock detection.
        tre = normalize_massive_payload("treasury_yields", c.get_treasury_yields(limit=2, sort="date.desc"), history_n=2)
        inf = normalize_massive_payload("inflation", c.get_inflation(limit=2, sort="date.desc"), history_n=2)
        iex = normalize_massive_payload(
            "inflation_expectations", c.get_inflation_expectations(limit=2, sort="date.desc"), history_n=2
        )
        lab = normalize_massive_payload("labor_market", c.get_labor_market(limit=2, sort="date.desc"), history_n=2)

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

        # Compute + cache macro regime snapshot.
        try:
            regime = compute_and_cache_macro_regime(
                redis_client,
                treasury_yields=tre,
                inflation=inf,
                inflation_expectations=iex,
                labor_market=lab,
                updated_utc=now,
            )
            meta["macro_regime"] = {"ok": bool(regime.get("ok")), "macro_risk": regime.get("macro_risk"), "posture": regime.get("posture")}
        except Exception:
            pass
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


def _pulse_key(day_et: str) -> str:
    try:
        from tnt_redis import rkey as _rkey

        return _rkey("macro", "pulse", str(day_et))
    except Exception:
        return f"macro:pulse:{day_et}"


def _shock_key(day_et: str) -> str:
    try:
        from tnt_redis import rkey as _rkey

        return _rkey("macro", "shock", str(day_et))
    except Exception:
        return f"macro:shock:{day_et}"


def _safe_float(x: object) -> Optional[float]:
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


def _yield_pair(econ_tre: Optional[Dict[str, Any]]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if not isinstance(econ_tre, dict):
        return None, None, None
    vals = econ_tre.get("values") if isinstance(econ_tre.get("values"), dict) else {}
    if not isinstance(vals, dict):
        vals = {}
    y10 = _safe_float(vals.get("yield_10_year") or vals.get("ten_year") or vals.get("yield_10yr"))
    y2 = _safe_float(vals.get("yield_2_year") or vals.get("two_year") or vals.get("yield_2yr"))
    spr = (y10 - y2) if (y10 is not None and y2 is not None) else None
    return y2, y10, spr


def _yield_pair_prev(econ_tre: Optional[Dict[str, Any]]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if not isinstance(econ_tre, dict):
        return None, None, None
    hist = econ_tre.get("history")
    if not (isinstance(hist, list) and len(hist) >= 2 and isinstance(hist[1], dict)):
        return None, None, None
    prev = hist[1]
    y10 = _safe_float(prev.get("yield_10_year") or prev.get("ten_year") or prev.get("yield_10yr"))
    y2 = _safe_float(prev.get("yield_2_year") or prev.get("two_year") or prev.get("yield_2yr"))
    spr = (y10 - y2) if (y10 is not None and y2 is not None) else None
    return y2, y10, spr


async def autopost_macro(bot: discord.Client, redis_client) -> None:
    """Post macro cards into the calendar channel at stage times."""

    channel_id = _calendar_channel_id()
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
    econ_meta = _redis_get_json(redis_client, ECON_META_KEY)

    econ_age_min: Optional[int] = None
    try:
        updated_utc = None
        if isinstance(econ_meta, dict):
            updated_utc = econ_meta.get("updated_utc")
        if updated_utc and isinstance(updated_utc, str):
            # meta uses Zulu.
            dt_u = datetime.fromisoformat(updated_utc.replace("Z", "+00:00"))
            age_s = max(0, (datetime.now(tz=ZoneInfo("UTC")) - dt_u).total_seconds())
            econ_age_min = int(age_s // 60)
    except Exception:
        econ_age_min = None

    schedule_label = os.path.basename(macro_schedule_path_str())

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
                schedule_label=schedule_label,
                source_label="Massive + schedule",
                econ_refreshed_age_min=econ_age_min,
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
    last_pulse = 0.0
    last_shock = 0.0
    last_validate = 0.0
    last_validate_ok = True
    warned_hash: str | None = None

    while True:
        now = time.time()

        # Validate schedule at startup and then daily. If errors exist, block autopost.
        if now - last_validate >= 24 * 60 * 60:
            try:
                rep = validate_macro_schedule(_et_now(), horizon_days=45)
                errs = rep.get("errors") or []
                last_validate_ok = not bool(errs)

                if not last_validate_ok:
                    msg = "[TNT][MACRO][VALIDATE] schedule invalid; autopost blocked: " + "; ".join(str(x) for x in list(errs)[:8])
                    print(msg)

                    # Optional: post a single warning into a canary channel (best effort).
                    try:
                        h = json.dumps({"e": errs}, separators=(",", ":"), sort_keys=True)
                        if warned_hash != h:
                            warned_hash = h
                            canary_id = int(os.getenv("DISCORD_CANARY_CHANNEL_ID", "0") or "0")
                            if canary_id:
                                ch = bot.get_channel(canary_id)
                                if ch is None:
                                    ch = await bot.fetch_channel(canary_id)
                                await ch.send("⚠️ Macro autopost blocked: schedule validation errors. Run /macro_validate.\n```\n" + "\n".join(str(x) for x in list(errs)[:12]) + "\n```")
                    except Exception:
                        pass
            except Exception:
                # If validator itself fails, fail closed on autopost.
                last_validate_ok = False
            last_validate = now

        if now - last_econ >= 30 * 60:
            try:
                refresh_econ_cache(redis_client)
            except Exception:
                pass
            last_econ = now

        # Daily Macro Pulse (once per ET day, after 08:00 ET). Fail-closed on validation errors.
        if now - last_pulse >= 60:
            try:
                if last_validate_ok:
                    now_et = _et_now()
                    if now_et.hour >= 8:
                        day = now_et.strftime("%Y-%m-%d")
                        k = _pulse_key(day)
                        if not redis_client.get(k):
                            channel_id = _calendar_channel_id()
                            if channel_id:
                                ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)

                                econ_tre = _redis_get_json(redis_client, ECON_KEYS["treasury_yields"])
                                econ_inf = _redis_get_json(redis_client, ECON_KEYS["inflation"])
                                econ_iex = _redis_get_json(redis_client, ECON_KEYS["inflation_expectations"])
                                econ_lab = _redis_get_json(redis_client, ECON_KEYS["labor_market"])

                                econ_meta = _redis_get_json(redis_client, ECON_META_KEY)
                                econ_age_min: Optional[int] = None
                                try:
                                    updated_utc = econ_meta.get("updated_utc") if isinstance(econ_meta, dict) else None
                                    if updated_utc and isinstance(updated_utc, str):
                                        dt_u = datetime.fromisoformat(updated_utc.replace("Z", "+00:00"))
                                        age_s = max(0, (datetime.now(tz=ZoneInfo("UTC")) - dt_u).total_seconds())
                                        econ_age_min = int(age_s // 60)
                                except Exception:
                                    econ_age_min = None

                                regime = get_cached_macro_regime(redis_client)
                                if not isinstance(regime, dict):
                                    regime = compute_and_cache_macro_regime(
                                        redis_client,
                                        treasury_yields=econ_tre,
                                        inflation=econ_inf,
                                        inflation_expectations=econ_iex,
                                        labor_market=econ_lab,
                                        updated_utc=(econ_meta.get("updated_utc") if isinstance(econ_meta, dict) else None),
                                    )

                                # Next 48h releases (lightweight).
                                evs = get_next_macro_events(now_et, horizon_days=3)
                                nxt: list[str] = []
                                for ev in evs[:6]:
                                    if (ev.dt_et - now_et).total_seconds() <= 48 * 3600:
                                        nxt.append(f"• {ev.dt_et.strftime('%a %m/%d %H:%M')} — {ev.title} ({ev.importance})")
                                embed = build_macro_regime_embed(
                                    regime=regime,
                                    now_et=now_et,
                                    next_events=nxt,
                                    blackout_state=None,
                                    schedule_label=os.path.basename(macro_schedule_path_str()),
                                    econ_refreshed_age_min=econ_age_min,
                                )

                                await ch.send(embed=embed)
                                redis_client.setex(k, 60 * 60 * 48, "1")
            except Exception:
                pass
            last_pulse = now

        # Rates shock detector (check every 5m, post at most once per day). Fail-closed on validation errors.
        if now - last_shock >= 5 * 60:
            try:
                if last_validate_ok:
                    now_et = _et_now()
                    day = now_et.strftime("%Y-%m-%d")
                    k = _shock_key(day)
                    if not redis_client.get(k):
                        econ_tre = _redis_get_json(redis_client, ECON_KEYS["treasury_yields"])
                        y2, y10, spr = _yield_pair(econ_tre)
                        py2, py10, pspr = _yield_pair_prev(econ_tre)
                        if y10 is not None and py10 is not None:
                            d10_bp = (y10 - py10) * 100.0
                        else:
                            d10_bp = None
                        if y2 is not None and py2 is not None:
                            d2_bp = (y2 - py2) * 100.0
                        else:
                            d2_bp = None
                        if spr is not None and pspr is not None:
                            dspr_bp = (spr - pspr) * 100.0
                        else:
                            dspr_bp = None

                        thresh_bp = float(os.getenv("TNT_MACRO_SHOCK_BP", "12") or "12")
                        hit = False
                        if d10_bp is not None and abs(d10_bp) >= thresh_bp:
                            hit = True
                        if d2_bp is not None and abs(d2_bp) >= thresh_bp:
                            hit = True
                        if dspr_bp is not None and abs(dspr_bp) >= max(10.0, thresh_bp):
                            hit = True

                        if hit:
                            channel_id = _calendar_channel_id()
                            if channel_id:
                                ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                                parts = []
                                if d10_bp is not None:
                                    parts.append(f"10Y {d10_bp:+.0f}bp")
                                if d2_bp is not None:
                                    parts.append(f"2Y {d2_bp:+.0f}bp")
                                if dspr_bp is not None:
                                    parts.append(f"2s10s {dspr_bp:+.0f}bp")
                                msg = "📣 **Rates shock**: " + ", ".join(parts) + " (vs prior print)"
                                try:
                                    await ch.send(msg)
                                except Exception:
                                    pass
                                redis_client.setex(k, 60 * 60 * 48, "1")
            except Exception:
                pass
            last_shock = now

        if now - last_macro >= 5 * 60:
            if last_validate_ok:
                try:
                    await autopost_macro(bot, redis_client)
                except Exception:
                    pass
            last_macro = now

        await asyncio.sleep(5)
