"""Discord bot for Zero DTE pipeline automation."""
from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import subprocess
import sys
import time as time_lib
from urllib.parse import quote
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv
import httpx
from zoneinfo import ZoneInfo

from delivery.state_builder import build_tnt_state as build_canonical_tnt_state
from delivery.tnt_llm import call_tnt_agent_async

# NOTE: Keep tests deterministic. Golden/smoke tests should not change based on a
# developer's local .env/.env.local.
if "pytest" not in sys.modules:
    try:
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
    except Exception:  # noqa: BLE001
        pass

    try:
        env_local_path = Path.cwd() / ".env.local"
        if env_local_path.exists():
            load_dotenv(env_local_path, override=True)
    except Exception:  # noqa: BLE001
        pass

ET = ZoneInfo("America/New_York")

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "./db/tnt.db")
TF = os.getenv("TF", "1m")
MODEL_VERSION = os.getenv("MODEL_VERSION", "heuristic-v1")
HORIZON_MIN = int(os.getenv("MODEL_HORIZON_MIN", "5"))

CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or "0")

AUTOPOST_ENABLED = os.getenv("DISCORD_AUTOPOST_ENABLED", "0") == "1"
AUTOPOST_POLL_SEC = int(os.getenv("DISCORD_AUTOPOST_SECONDS", "45"))
AUTOPOST_CONF_THRESH = float(os.getenv("DISCORD_AUTOPOST_CONF_THRESHOLD", "0.02"))
AUTOPOST_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("DISCORD_AUTOPOST_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]
AUTOPOST_COMBINED = os.getenv("DISCORD_AUTOPOST_COMBINED", "0") == "1"

_DEFAULT_FUTURES_CONTEXT_PATH = (
    Path(__file__).resolve().parent.parent / "iqfeed_service" / "output" / "futures_context.json"
)
FUTURES_CONTEXT_PATH = Path(os.getenv("FUTURES_CONTEXT_PATH", "") or _DEFAULT_FUTURES_CONTEXT_PATH).expanduser()
FUTURES_CONTEXT_LABEL = os.getenv("FUTURES_CONTEXT_LABEL", "@ES#")
FUTURES_STALE_MINUTES_ACTIVE = int(os.getenv("FUTURES_STALE_MINUTES_ACTIVE", "20"))
FUTURES_STALE_MINUTES_OFFHOURS = int(os.getenv("FUTURES_STALE_MINUTES_OFFHOURS", str(12 * 60)))

DAILY_SUMMARY_ENABLED = os.getenv("DAILY_SUMMARY_ENABLED", "0") == "1"
DAILY_SUMMARY_TIME_ET = os.getenv("DAILY_SUMMARY_TIME_ET", "09:31")
DAILY_SUMMARY_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("DAILY_SUMMARY_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]
DAILY_SUMMARY_CHANNEL_ID = int(os.getenv("DAILY_SUMMARY_CHANNEL_ID", "0") or "0")

MODEL_SCHED_ENABLED = os.getenv("MODEL_SCHED_ENABLED", "0") == "1"
MODEL_SCHED_EVERY_SEC = int(os.getenv("MODEL_SCHED_EVERY_SEC", "300"))
MODEL_SCHED_RTH_ONLY = os.getenv("MODEL_SCHED_RTH_ONLY", "1") == "1"

AI_ENABLED = os.getenv("DISCORD_AI_ENABLED", "0") == "1"
AI_MODE = os.getenv("DISCORD_AI_MODE", "mention")
AI_ROLE_ID = int(os.getenv("DISCORD_AI_ROLE_ID", "0") or "0")
AI_CHANNEL_ID = int(os.getenv("DISCORD_AI_CHANNEL_ID", "0") or "0")
# TNT agent calls are model-locked in delivery.tnt_llm; keep a stable, non-drifting default here.
AI_MODEL = "gpt-5.2"
AI_MAX_CHARS = int(os.getenv("DISCORD_AI_MAX_CHARS", "1200"))

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE_URL = (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")
MASSIVE_BASE_URL = (os.getenv("MASSIVE_BASE_URL") or "").rstrip("/")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
USE_MASSIVE = os.getenv("ZERO_DTE_USE_MASSIVE", "0") == "1"
POLYGON_DISABLED = os.getenv("ZERO_DTE_DISABLE_POLYGON", "0") == "1"

VIX_ALERTS_ENABLED = os.getenv("VIX_ALERTS_ENABLED", "0") == "1"
VIX_ALERT_CHANNEL_ID = int(os.getenv("VIX_ALERT_CHANNEL_ID", "0") or "0")
VIX_ALERT_CHECK_SEC = int(os.getenv("VIX_ALERT_CHECK_SEC", "60"))
VIX_ALERT_ABS_MOVE = float(os.getenv("VIX_ALERT_ABS_MOVE", "1.00"))
VIX_ALERT_PCT_MOVE = float(os.getenv("VIX_ALERT_PCT_MOVE", "5.0"))
VIX_GATING_ENABLED = os.getenv("VIX_GATING_ENABLED", "0") == "1"
VIX_MAX_REGIME = (os.getenv("VIX_MAX_REGIME", "HIGH / STRESS") or "HIGH / STRESS").upper()
VIX_HARD_BLOCK_LEVEL = float(os.getenv("VIX_HARD_BLOCK_LEVEL", "0"))
VIX_SOFT_BLOCK_LEVEL = float(os.getenv("VIX_SOFT_BLOCK_LEVEL", "0"))

MONDAY_PLAYBOOK_ENABLED = os.getenv("MONDAY_PLAYBOOK_ENABLED", "0") == "1"
MONDAY_PLAYBOOK_TIME_ET = os.getenv("MONDAY_PLAYBOOK_TIME_ET", "09:25")
MONDAY_PLAYBOOK_CHANNEL_ID = int(os.getenv("MONDAY_PLAYBOOK_CHANNEL_ID", "0") or "0")
MONDAY_PLAYBOOK_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("MONDAY_PLAYBOOK_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
RTH_START = time(9, 31)
PM_OPEN = time(4, 0)
AH_CLOSE = time(20, 0)

intent = discord.Intents.default()
intent.message_content = True
bot = commands.Bot(command_prefix="!", intents=intent)

# NOTE: This legacy bot is retained for reference, but all LLM calls are routed
# through `delivery.tnt_llm`. We intentionally avoid direct OpenAI SDK usage here.
_openai: Optional[object] = None

_autopost_task: Optional[asyncio.Task] = None
_last_posted_ts: Dict[str, str] = {}
_last_summary_date: Optional[date] = None
_last_vix_alert_level: Optional[float] = None
_last_vix_alert_ts: Optional[str] = None
_last_vix_gating_state: Optional[str] = None
_last_monday_date: Optional[date] = None

# Rate-limit intraday daily-bar refreshes (used to keep RTH pivots current during RTH).
_LAST_DAILY_PIVOTS_REFRESH_TS: Dict[str, float] = {}

_REGIME_ORDER: Dict[str, int] = {
    "LOW VOL": 0,
    "NORMAL VOL": 1,
    "ELEVATED VOL": 2,
    "HIGH / STRESS": 3,
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def market_session_et(now_et: Optional[datetime] = None) -> str:
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return "WEEKEND"
    t = now_et.time()
    if RTH_START <= t < RTH_CLOSE:
        return "RTH"
    if PM_OPEN <= t < RTH_START:
        return "PRE"
    if RTH_CLOSE <= t < AH_CLOSE:
        return "AH"
    return "CLOSED"


def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pivots_from_hlc(high: float, low: float, close: float) -> Dict[str, float]:
    pivot = (high + low + close) / 3.0
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = high + 2 * (pivot - low)
    s3 = low - 2 * (high - pivot)
    return {"P": pivot, "R1": r1, "S1": s1, "R2": r2, "S2": s2, "R3": r3, "S3": s3}


def conviction_from_edge(edge: float) -> str:
    if edge >= 0.08:
        return "HIGH"
    if edge >= 0.04:
        return "MEDIUM"
    return "LOW"


def _format_ts(ts: Optional[str]) -> str:
    if not ts:
        return "unknown"
    try:
        return parse_iso(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts


def _fmt_price(val: Optional[object]) -> str:
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(num) or math.isinf(num):
        return "n/a"
    return f"{num:.2f}"


def _safe_float(val: Optional[object]) -> Optional[float]:
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _load_futures_context_payload(now_ts: Optional[str] = None) -> dict:
    path_value = FUTURES_CONTEXT_PATH
    path = Path(path_value) if not isinstance(path_value, Path) else path_value
    now_et = parse_iso(now_ts).astimezone(ET) if now_ts else datetime.now(ET)

    if not str(path).strip():
        return {"ok": False, "reason": "path not configured"}

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw_payload = json.load(handle)
    except FileNotFoundError:
        return {"ok": False, "reason": "missing"}
    except Exception as exc:  # noqa: BLE001
        print(f"[AUTOPOST] futures context read error: {exc}")
        return {"ok": False, "reason": "read error"}

    if not isinstance(raw_payload, dict):
        return {"ok": False, "reason": "invalid payload"}

    payload = dict(raw_payload)
    ctx_candidate = payload.get("context") if isinstance(raw_payload.get("context"), dict) else raw_payload
    ctx = dict(ctx_candidate) if isinstance(ctx_candidate, dict) else {}
    payload["context"] = ctx

    computed_ts = ctx.get("computed_ts") or payload.get("computed_ts")
    session = market_session_et(now_et)
    payload["session"] = session

    if not computed_ts:
        payload["ok"] = False
        payload["reason"] = "missing timestamp"
        return payload

    try:
        computed_dt = parse_iso(str(computed_ts)).astimezone(ET)
    except Exception:  # noqa: BLE001
        payload["ok"] = False
        payload["reason"] = "unparseable timestamp"
        return payload

    payload["computed_dt"] = computed_dt.isoformat()
    age = now_et - computed_dt
    age_minutes = age.total_seconds() / 60.0
    payload["age_minutes"] = age_minutes

    threshold_min = FUTURES_STALE_MINUTES_ACTIVE if session in {"PRE", "RTH"} else FUTURES_STALE_MINUTES_OFFHOURS
    if age_minutes > threshold_min:
        payload["ok"] = False
        payload["reason"] = f"stale ({int(age_minutes)} min old)"
        return payload

    base_ok = raw_payload.get("ok", True)
    payload["ok"] = bool(base_ok)
    return payload


def _derive_futures_posture(ctx: dict) -> Optional[str]:
    on_high = _safe_float(ctx.get("on_high"))
    on_low = _safe_float(ctx.get("on_low"))
    prior_high = _safe_float(ctx.get("prior_rth_high"))
    prior_low = _safe_float(ctx.get("prior_rth_low"))

    if on_high is None or on_low is None or prior_high is None or prior_low is None:
        return None

    buffer_val = max(0.25, (prior_high - prior_low) * 0.02)
    if on_high > prior_high + buffer_val:
        return "• Posture: Overnight pressing above prior RTH high."
    if on_low < prior_low - buffer_val:
        return "• Posture: Overnight testing below prior RTH low."
    return "• Posture: Overnight holding inside prior RTH range."


def _format_futures_context_lines(
    payload: dict,
    *,
    post_type: str = "default",
    show_reason: bool = False,
) -> list[str]:
    if not isinstance(payload, dict):
        return []

    if not payload.get("ok", True):
        reason = payload.get("reason")
        if show_reason and isinstance(reason, str) and reason:
            return [f"• Futures context unavailable: {reason}"]
        return []

    ctx = payload.get("context")
    if not isinstance(ctx, dict):
        return []

    lines: list[str] = []

    session_date = ctx.get("session_date") or payload.get("session_date")
    computed_ts = ctx.get("computed_ts") or payload.get("computed_ts")

    if post_type == "intraday":
        lines.append(
            "• ON range:"
            f" H {_fmt_price(ctx.get('on_high'))}"
            f" / L {_fmt_price(ctx.get('on_low'))}"
            f" / VWAP {_fmt_price(ctx.get('on_vwap'))}"
        )
        posture_line = _derive_futures_posture(ctx)
        if posture_line:
            lines.append(posture_line)
    else:
        lines.append(
            "• Overnight:"
            f" H {_fmt_price(ctx.get('on_high'))}"
            f" / L {_fmt_price(ctx.get('on_low'))}"
            f" / VWAP {_fmt_price(ctx.get('on_vwap'))}"
        )
        lines.append(
            "• RTH:"
            f" H {_fmt_price(ctx.get('rth_high'))}"
            f" / L {_fmt_price(ctx.get('rth_low'))}"
        )
        lines.append(
            "• Prior RTH:"
            f" H {_fmt_price(ctx.get('prior_rth_high'))}"
            f" / L {_fmt_price(ctx.get('prior_rth_low'))}"
            f" / Close {_fmt_price(ctx.get('prior_close'))}"
        )
        posture_line = _derive_futures_posture(ctx)
        if posture_line:
            lines.append(posture_line)

    timestamp_bits: list[str] = []
    if isinstance(session_date, str) and session_date:
        timestamp_bits.append(session_date)
    if isinstance(computed_ts, str) and computed_ts:
        timestamp_bits.append(_format_ts(computed_ts))
    if timestamp_bits:
        lines.append("• Updated: " + " | ".join(timestamp_bits))

    return [line for line in lines if line.strip()]


def _build_futures_section(
    *,
    post_type: str = "default",
    now_ts: Optional[str] = None,
    show_reason: bool = False,
) -> str:
    payload = _load_futures_context_payload(now_ts)
    lines = _format_futures_context_lines(payload, post_type=post_type, show_reason=show_reason)
    if not lines:
        return ""
    return "\n".join(
        [
            f"📊 Futures Context — **{FUTURES_CONTEXT_LABEL}**",
            *lines,
            "",
        ]
    )


def _rows(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(query, params)
    return cur.fetchall()


def get_latest_price(symbol: str) -> Optional[Tuple[float, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        try:
            row = _rows(
                conn,
                "SELECT close, ts FROM prices WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 1",
                (symbol.upper(), TF),
            )
        except sqlite3.OperationalError:
            row = _rows(
                conn,
                "SELECT close, ts FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                (symbol.upper(),),
            )
    if not row:
        return None
    close, ts = row[0]
    return float(close), str(ts)


def get_latest_bar(symbol: str, tf: str = "1m") -> Optional[Tuple[str, float, float, float, float]]:
    with sqlite3.connect(DB_PATH) as conn:
        row = _rows(
            conn,
            """
            SELECT ts, open, high, low, close
            FROM prices
            WHERE symbol=? AND tf=?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol.upper(), tf),
        )
    return tuple(row[0]) if row else None


def get_latest_close(symbol: str, tf: str = "1m") -> Optional[Dict[str, float]]:
    bar = get_latest_bar(symbol, tf=tf)
    if not bar:
        return None
    ts, _open, _high, _low, close = bar
    return {"ts": ts, "close": float(close)}


def get_close_at_or_before(symbol: str, tf: str, ts_iso: str) -> Optional[Dict[str, float]]:
    with sqlite3.connect(DB_PATH) as conn:
        row = _rows(
            conn,
            """
            SELECT ts, close
            FROM prices
            WHERE symbol=? AND tf=? AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol.upper(), tf, ts_iso),
        )
    if not row:
        return None
    ts, close = row[0]
    return {"ts": ts, "close": float(close)}


def get_last_n_bars(symbol: str, tf: str = "1m", n: int = 2000, *, with_volume: bool = False) -> list[tuple]:
    """Fetch the last N bars from SQLite.

    Backwards compatible:
    - with_volume=False returns 5-tuples: (ts, open, high, low, close)
    - with_volume=True returns 6-tuples when `prices.volume` exists.
    """

    sym = symbol.upper()
    with sqlite3.connect(DB_PATH) as conn:
        if with_volume:
            try:
                rows = _rows(
                    conn,
                    """
                    SELECT ts, open, high, low, close, volume
                    FROM prices
                    WHERE symbol=? AND tf=?
                    ORDER BY ts DESC
                    LIMIT ?
                    """,
                    (sym, tf, n),
                )
                return list(reversed(rows))
            except sqlite3.OperationalError:
                # Older schema: no volume column.
                pass

        rows = _rows(
            conn,
            """
            SELECT ts, open, high, low, close
            FROM prices
            WHERE symbol=? AND tf=?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (sym, tf, n),
        )
    return list(reversed(rows))


def get_latest_daily_bar(symbol: str) -> Optional[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        row = _rows(
            conn,
            """
            SELECT ts, open, high, low, close
            FROM prices
            WHERE symbol=? AND tf='1d'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        )
    return row[0] if row else None


def _daily_pivots_refresh_max_age_days() -> int:
    try:
        return max(1, int(os.getenv("DAILY_PIVOTS_REFRESH_MAX_AGE_DAYS", "3")))
    except Exception:  # noqa: BLE001
        return 3


def _should_refresh_daily_bar(ts_iso: Optional[str]) -> bool:
    if not ts_iso:
        return True
    try:
        bar_dt_et = parse_iso(str(ts_iso)).astimezone(ET)
        age_days = (datetime.now(ET).date() - bar_dt_et.date()).days
        return age_days >= _daily_pivots_refresh_max_age_days()
    except Exception:  # noqa: BLE001
        return True


def _upsert_daily_bar_row(
    symbol: str,
    *,
    ts_utc_iso: str,
    open_val: float,
    high_val: float,
    low_val: float,
    close_val: float,
    volume_val: float,
    source: str,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        price_columns = {row[1] for row in conn.execute("PRAGMA table_info(prices)")}
        columns = ["symbol", "tf", "ts", "open", "high", "low", "close"]
        values: list[object] = [symbol.upper(), "1d", ts_utc_iso, open_val, high_val, low_val, close_val]

        if "volume" in price_columns:
            columns.append("volume")
            values.append(volume_val)
        if "source" in price_columns:
            columns.append("source")
            values.append(source)
        if "is_partial" in price_columns:
            columns.append("is_partial")
            values.append(0)

        placeholders = ",".join(["?"] * len(values))
        conn.execute(
            f"INSERT OR REPLACE INTO prices({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()


def _daily_bar_providers() -> list[Tuple[str, str, str]]:
    providers: list[Tuple[str, str, str]] = []
    if USE_MASSIVE and MASSIVE_API_KEY and MASSIVE_BASE_URL:
        providers.append(("massive", MASSIVE_BASE_URL, MASSIVE_API_KEY))
    if not POLYGON_DISABLED and POLYGON_API_KEY:
        providers.append(("polygon", POLYGON_BASE_URL, POLYGON_API_KEY))
    return providers


def _refresh_daily_bar_from_polygon(symbol: str, *, lookback_days: int = 14) -> bool:
    sym = (symbol or "").strip().upper()
    if not sym:
        return False

    providers = _daily_bar_providers()
    if not providers:
        return False

    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    start_date = (now_utc - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    end_date = now_utc.strftime("%Y-%m-%d")

    for provider, base_url, api_key in providers:
        sym_enc = quote(sym, safe="")
        url = f"{base_url.rstrip('/')}/v2/aggs/ticker/{sym_enc}/range/1/day/{start_date}/{end_date}"
        try:
            resp = httpx.get(
                url,
                params={
                    "apiKey": api_key,
                    "adjusted": "true",
                    "sort": "asc",
                    "limit": 50000,
                },
                timeout=20,
            )
        except Exception:
            continue

        if resp.status_code != 200:
            continue

        try:
            data = resp.json()
        except Exception:
            continue

        results = data.get("results") or []
        if not isinstance(results, list) or not results:
            continue

        last = results[-1]
        try:
            open_val = float(last.get("o"))
            high_val = float(last.get("h"))
            low_val = float(last.get("l"))
            close_val = float(last.get("c"))
            volume_val = float(last.get("v") or 0.0)
            t_ms = int(last.get("t"))
        except Exception:
            continue

        # For completed sessions, normalize timestamp to 16:00 ET so "Source" matches RTH close.
        # During RTH, use a real refresh timestamp so callers can see pivots updating live.
        try:
            dt_utc = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
            dt_et = dt_utc.astimezone(ET)
            if dt_et.date() == now_et.date() and market_session_et(now_et) == "RTH":
                ts_utc_iso = now_utc.isoformat()
            else:
                close_et = datetime.combine(dt_et.date(), RTH_CLOSE, tzinfo=ET)
                ts_utc_iso = close_et.astimezone(timezone.utc).isoformat()
        except Exception:
            continue

        try:
            _upsert_daily_bar_row(
                sym,
                ts_utc_iso=ts_utc_iso,
                open_val=open_val,
                high_val=high_val,
                low_val=low_val,
                close_val=close_val,
                volume_val=volume_val,
                source=f"{provider}_daily",
            )
            return True
        except Exception:
            continue

    return False


def get_last_rth_session_hlc(symbol: str) -> Optional[Tuple[str, float, float, float, str]]:
    daily = get_latest_daily_bar(symbol)
    if daily:
        ts, _o, high, low, close = daily
        return ts[:10], float(high), float(low), float(close), str(ts)

    rows = get_last_n_bars(symbol, tf="1m")
    sessions: Dict[str, list[tuple[datetime, float, float, float, float]]] = {}
    for ts, _o, high, low, close in rows:
        dt_utc = parse_iso(ts)
        dt_et = dt_utc.astimezone(ET)
        if dt_et.weekday() >= 5:
            continue
        clock = dt_et.time()
        if not (RTH_OPEN <= clock < RTH_CLOSE):
            continue
        key = dt_et.date().isoformat()
        sessions.setdefault(key, []).append((dt_utc, float(high), float(low), float(close), float(close)))

    if not sessions:
        return None

    session_date = sorted(sessions.keys())[-1]
    bars = sorted(sessions[session_date], key=lambda item: item[0])
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    close = bars[-1][3]
    last_ts = bars[-1][0].isoformat()
    return session_date, max(highs), min(lows), close, last_ts


def get_latest_daily_pivots(symbol: str) -> Optional[Dict[str, object]]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    # If market is open, refresh daily bar (intraday H/L/C) on a short interval
    # so "RTH pivots" can update live during RTH.
    try:
        if os.getenv("DAILY_PIVOTS_LIVE_WHEN_OPEN", "1").strip().lower() not in {"0", "false", "off", "no"}:
            now_et = datetime.now(ET)
            if market_session_et(now_et) == "RTH":
                refresh_every = max(15, int(os.getenv("DAILY_PIVOTS_LIVE_REFRESH_SEC", "120") or "120"))
                last = _LAST_DAILY_PIVOTS_REFRESH_TS.get(sym, 0.0)
                now_ts = time_lib.time()
                if now_ts - last >= float(refresh_every):
                    if _refresh_daily_bar_from_polygon(sym):
                        _LAST_DAILY_PIVOTS_REFRESH_TS[sym] = now_ts

                # Fallback: if the stored daily bar isn't for today (e.g., missing yesterday's 1D bar
                # and Polygon refresh isn't available), compute intraday H/L/C from 1m RTH bars.
                try:
                    daily = get_latest_daily_bar(sym)
                    daily_date = str(daily[0])[:10] if daily else ""
                    today_date = now_et.date().isoformat()
                    if daily_date != today_date:
                        rows = get_last_n_bars(sym, tf="1m")
                        highs: list[float] = []
                        lows: list[float] = []
                        last_close: float | None = None
                        last_ts: str | None = None
                        for ts, _o, high, low, close in rows:
                            dt_utc = parse_iso(ts)
                            dt_et = dt_utc.astimezone(ET)
                            if dt_et.date().isoformat() != today_date:
                                continue
                            clock = dt_et.time()
                            if not (RTH_OPEN <= clock < RTH_CLOSE):
                                continue
                            try:
                                highs.append(float(high))
                                lows.append(float(low))
                                last_close = float(close)
                                last_ts = str(ts)
                            except Exception:
                                continue
                        if highs and lows and last_close is not None and last_ts:
                            piv = pivots_from_hlc(max(highs), min(lows), float(last_close))
                            return {"ts": last_ts, "session": today_date, "H": max(highs), "L": min(lows), "C": float(last_close), "piv": piv}
                except Exception:
                    pass
    except Exception:
        pass

    daily = get_latest_daily_bar(sym)
    if daily:
        ts, _o, high, low, close = daily
        if _should_refresh_daily_bar(str(ts)):
            if _refresh_daily_bar_from_polygon(sym):
                daily2 = get_latest_daily_bar(sym)
                if daily2:
                    ts, _o, high, low, close = daily2
        piv = pivots_from_hlc(float(high), float(low), float(close))
        return {"ts": ts, "H": float(high), "L": float(low), "C": float(close), "piv": piv}

    if _refresh_daily_bar_from_polygon(sym):
        daily2 = get_latest_daily_bar(sym)
        if daily2:
            ts, _o, high, low, close = daily2
            piv = pivots_from_hlc(float(high), float(low), float(close))
            return {"ts": ts, "H": float(high), "L": float(low), "C": float(close), "piv": piv}

    sess = get_last_rth_session_hlc(sym)
    if not sess:
        return None
    session_date, high, low, close, last_ts = sess
    piv = pivots_from_hlc(high, low, close)
    return {"ts": last_ts, "session": session_date, "H": high, "L": low, "C": close, "piv": piv}


def get_latest_signal_full(symbol: str) -> Optional[Tuple[str, str, Optional[int], float, float, Optional[str], Optional[str]]]:
    query = (
        "SELECT symbol, ts, horizon_min, prob_up, prob_down, model_version, meta "
        "FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1"
    )
    with sqlite3.connect(DB_PATH) as conn:
        rows = _rows(conn, query, (symbol.upper(),))
    return tuple(rows[0]) if rows else None


def get_latest_signal(symbol: str) -> Optional[Tuple[str, float, float, str, Optional[float], Dict[str, float]]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
            (symbol.upper(),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    data = dict(row)
    meta = data.get("meta") or data.get("meta_json")
    parsed: Dict[str, float] = {}
    if isinstance(meta, str):
        try:
            parsed = json.loads(meta)
        except json.JSONDecodeError:
            try:
                parsed = json.loads(meta.replace("'", '"'))
            except Exception:
                parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
    return (
        data.get("ts"),
        float(data.get("prob_up", 0.5)),
        float(data.get("prob_down", 0.5)),
        data.get("model") or data.get("model_version") or MODEL_VERSION,
        float(data.get("edge") or abs(float(data.get("prob_up", 0.5)) - 0.5)),
        {k: float(v) for k, v in parsed.items() if isinstance(v, (int, float))},
    )


def get_latest_signal_context(symbol: str) -> tuple[str, tuple] | tuple[None, None]:
    sql_candidates = [
        "SELECT ts, prob_up, prob_down, model, edge, ret_1m, trend, vol_z FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        "SELECT ts, prob_up, prob_down, model, edge FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        "SELECT ts, prob_up, prob_down, model FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        "SELECT ts, prob_up, model FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
    ]
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        for sql in sql_candidates:
            try:
                cur.execute(sql, (symbol.upper(),))
                row = cur.fetchone()
                if row:
                    return sql, row
            except sqlite3.OperationalError:
                continue
    return None, None


def vix_regime(level: float) -> str:
    if level < 15:
        return "LOW VOL"
    if level < 20:
        return "NORMAL VOL"
    if level < 25:
        return "ELEVATED VOL"
    return "HIGH / STRESS"


def get_vix_context(tf: str = "1m") -> Optional[Dict[str, object]]:
    latest = get_latest_close("VIX", tf=tf)
    if not latest:
        return None
    level = float(latest["close"])
    return {"ts": latest["ts"], "level": level, "regime": vix_regime(level)}


def vix_gating_action(vix_level: float) -> Dict[str, object]:
    if VIX_HARD_BLOCK_LEVEL and vix_level >= VIX_HARD_BLOCK_LEVEL:
        return {"mode": "HARD", "mult": 0.0, "reason": f"level {vix_level:.2f} >= {VIX_HARD_BLOCK_LEVEL:.2f}"}
    if VIX_SOFT_BLOCK_LEVEL and vix_level >= VIX_SOFT_BLOCK_LEVEL:
        return {"mode": "SOFT", "mult": 0.5, "reason": f"level {vix_level:.2f} >= {VIX_SOFT_BLOCK_LEVEL:.2f}"}
    return {"mode": "OK", "mult": 1.0, "reason": "VIX normal"}


def vix_gating_decision(vix: Optional[Dict[str, object]]) -> tuple[str, Optional[str]]:
    if not VIX_GATING_ENABLED or not vix:
        return "ok", None
    level = float(vix["level"])
    regime = str(vix["regime"]).upper()
    regime_rank = _REGIME_ORDER.get(regime, 99)
    max_rank = _REGIME_ORDER.get(VIX_MAX_REGIME, 99)
    if regime_rank > max_rank:
        return "hard", f"regime {regime} > max {VIX_MAX_REGIME}"
    if VIX_HARD_BLOCK_LEVEL and level >= VIX_HARD_BLOCK_LEVEL:
        return "hard", f"level {level:.2f} >= hard block {VIX_HARD_BLOCK_LEVEL:.2f}"
    if VIX_SOFT_BLOCK_LEVEL and level >= VIX_SOFT_BLOCK_LEVEL:
        return "soft", f"level {level:.2f} >= soft block {VIX_SOFT_BLOCK_LEVEL:.2f}"
    return "ok", None


def nearest_levels(price: float, levels: Dict[str, float], k: int = 2) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    items = [(name, float(value)) for name, value in levels.items()]
    below = sorted((item for item in items if item[1] <= price), key=lambda item: price - item[1])[:k]
    above = sorted((item for item in items if item[1] >= price), key=lambda item: item[1] - price)[:k]
    return below, above


def _extract_meta(meta: Optional[str]) -> Dict[str, float]:
    if not meta:
        return {}
    try:
        parsed = json.loads(meta)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: float(v) for k, v in parsed.items() if isinstance(v, (int, float))}


def format_signal(signal_row: Tuple[str, str, Optional[int], float, float, Optional[str], Optional[str]], price_info: Optional[Tuple[float, str]]) -> str:
    symbol, ts, horizon_min, prob_up, prob_down, model_version, meta = signal_row
    horizon = horizon_min if horizon_min is not None else HORIZON_MIN
    edge = abs(prob_up - 0.5)
    arrow = "Γåæ" if prob_up >= 0.5 else "Γåô"
    lines = [
        f"≡ƒôè **{symbol}** model `{model_version or MODEL_VERSION}`",
        f"ΓÇó Horizon {horizon}m | ProbΓåæ {prob_up:.3f} {arrow} | ProbΓåô {prob_down:.3f}",
        f"ΓÇó Edge {edge:.3f} | Signal time {_format_ts(ts)}",
    ]
    if price_info:
        price, price_ts = price_info
        lines.append(f"ΓÇó Last price {price:.2f} @ {_format_ts(price_ts)}")
    extras = _extract_meta(meta)
    if extras:
        lines.append("ΓÇó Meta: " + " | ".join(f"{k}={v:.2f}" for k, v in extras.items()))
    return "\n".join(lines)


def accuracy_by_vix_regime(symbol: str, horizon_min: int, n: int = 200, tf: str = "1m") -> Optional[Dict[str, Dict[str, float]]]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT ts, prob_up FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol.upper(), n),
            )
        except sqlite3.OperationalError:
            return None
        sigs = cur.fetchall()

        buckets: Dict[str, list[int]] = {}
        for sig_ts, prob_up in sigs:
            vix = get_close_at_or_before("VIX", tf, sig_ts)
            if not vix:
                continue
            regime = vix_regime(float(vix["close"]))
            buckets.setdefault(regime, [0, 0])

            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, sig_ts),
            )
            start = cur.fetchone()
            if not start:
                continue
            start_ts, start_close = start
            target_ts = (parse_iso(start_ts) + timedelta(minutes=horizon_min)).isoformat()
            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, target_ts),
            )
            finish = cur.fetchone()
            if not finish:
                continue
            finish_close = float(finish[1])
            start_close = float(start_close)
            hit = finish_close > start_close
            pred_up = float(prob_up) >= 0.5
            buckets[regime][0] += 1 if hit == pred_up else 0
            buckets[regime][1] += 1

    if not buckets:
        return None
    out: Dict[str, Dict[str, float]] = {}
    for regime, (hits, total) in buckets.items():
        if total:
            out[regime] = {"acc": hits / total, "hits": hits, "total": total}
    return out


def rolling_accuracy(symbol: str, horizon_min: int, n: int = 50, tf: str = "1m") -> Optional[Dict[str, object]]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT ts, prob_up FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol.upper(), n),
            )
        except sqlite3.OperationalError:
            return None
        sigs = cur.fetchall()

        hits = 0
        matured = 0
        for sig_ts, prob_up in sigs:
            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, sig_ts),
            )
            start = cur.fetchone()
            if not start:
                continue
            start_ts, start_close = start
            target_ts = (parse_iso(start_ts) + timedelta(minutes=horizon_min)).isoformat()
            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, target_ts),
            )
            finish = cur.fetchone()
            if not finish:
                continue
            finish_close = float(finish[1])
            start_close = float(start_close)
            matured += 1
            hit = finish_close > start_close
            pred_up = float(prob_up) >= 0.5
            hits += 1 if hit == pred_up else 0

    if matured == 0:
        return {"hits": 0, "matured": 0, "considered": len(sigs), "acc": None}
    return {"hits": hits, "matured": matured, "considered": len(sigs), "acc": hits / matured}


def format_daily_summary(symbols: list[str]) -> str:
    now_et = datetime.now(ET)
    lines = [
        "≡ƒôî **Daily Prep (RTH pivots + latest signal)**",
        f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}",
    ]

    futures_block = _build_futures_section(post_type="daily", now_ts=now_et.isoformat())
    if futures_block:
        lines.append("")
        lines.append(futures_block.rstrip("\n"))

    for sym in symbols:
        sym = sym.upper()
        lines.append(f"\n**{sym}**")
        piv = get_latest_daily_pivots(sym)
        if piv:
            vals = piv["piv"]
            lines.append(
                "ΓÇó Prev RTH pivots: P {P:.2f} | R1 {R1:.2f} | S1 {S1:.2f} | R2 {R2:.2f} | S2 {S2:.2f}".format(**vals)
            )
        else:
            lines.append("ΓÇó (no daily pivots yet)")
        _sql, sig = get_latest_signal_context(sym)
        if sig:
            prob_up = float(sig[1]) if len(sig) > 1 and sig[1] is not None else 0.5
            edge = float(sig[4]) if len(sig) > 4 and sig[4] is not None else abs(prob_up - 0.5)
            conv = conviction_from_edge(edge)
            model_name = sig[3] if len(sig) > 3 else "unknown"
            lines.append(
                f"ΓÇó Signal: prob_up {prob_up:.2f} | edge {edge:.2f} | conv {conv} | model {model_name}"
            )
        else:
            lines.append("ΓÇó (no signal yet)")
        acc = rolling_accuracy(sym, horizon_min=HORIZON_MIN, n=50, tf="1m")
        if acc and acc["acc"] is not None:
            lines.append(f"ΓÇó Rolling acc: {acc['acc']*100:.1f}% ({acc['hits']}/{acc['matured']})")
        elif acc:
            lines.append(f"ΓÇó Rolling acc: n/a (coverage {acc['matured']}/{acc['considered']})")
        else:
            lines.append("ΓÇó Rolling acc: n/a")
    lines.append("\n_Not financial advice._")
    return "\n".join(lines)


def format_monday_playbook(symbols: list[str]) -> str:
    session = market_session_et()
    title = "Monday Open Playbook" if datetime.now(ET).weekday() == 0 else "Next RTH Playbook"
    lines = [f"≡ƒôÿ **{title} (levels + regimes + plan)**", f"ET: {datetime.now(ET).strftime('%Y-%m-%d %H:%M')} | Session={session}"]
    vix = get_vix_context("1m")
    if vix:
        lines.append(f"VIX: **{vix['level']:.2f}** ({vix['regime']})")
        gate = vix_gating_action(float(vix["level"]))
        if gate["mode"] == "HARD":
            lines.append("Posture: **STAND DOWN** (volatility stress)")
        elif gate["mode"] == "SOFT":
            lines.append("Posture: **CAUTION** (reduce size / be selective)")
        else:
            lines.append("Posture: **NORMAL**")
    for sym in symbols:
        sym = sym.upper()
        lines.append(f"\n**{sym}**")
        piv = get_latest_daily_pivots(sym)
        if piv:
            vals = piv["piv"]
            lines.append(
                f"Prev RTH pivots: P {vals['P']:.2f} | R1 {vals['R1']:.2f} | S1 {vals['S1']:.2f} | R2 {vals['R2']:.2f} | S2 {vals['S2']:.2f}"
            )
            lines.extend(
                [
                    "Plan:",
                    f"ΓÇó Bull case: reclaim/hold above **P {vals['P']:.2f}** ΓåÆ target **R1 {vals['R1']:.2f}**",
                    f"ΓÇó Bear case: lose/hold below **P {vals['P']:.2f}** ΓåÆ test **S1 {vals['S1']:.2f}**",
                    "ΓÇó If inside S1-R1: treat as range until break + retest",
                ]
            )
        else:
            lines.append("ΓÇó (no daily pivots yet)")
    lines.append("\n_Not financial advice._")
    return "\n".join(lines)


async def daily_summary_loop() -> None:
    global _last_summary_date
    while True:
        try:
            now_et = datetime.now(ET)
            try:
                hh, mm = [int(x) for x in DAILY_SUMMARY_TIME_ET.split(":", 1)]
            except ValueError:
                hh, mm = 9, 31
            target = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= target:
                if _last_summary_date != now_et.date():
                    channel_id = DAILY_SUMMARY_CHANNEL_ID
                    if channel_id:
                        channel = bot.get_channel(channel_id)
                        if channel:
                            await channel.send(format_daily_summary(DAILY_SUMMARY_SYMBOLS))
                            _last_summary_date = now_et.date()
                await asyncio.sleep(30)
            else:
                wait_seconds = max(1.0, min(30.0, (target - now_et).total_seconds()))
                await asyncio.sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"[DAILY_SUMMARY_ERROR] {exc}")
            await asyncio.sleep(30)


async def monday_playbook_loop() -> None:
    global _last_monday_date
    while True:
        try:
            now_et = datetime.now(ET)
            if now_et.weekday() != 0:
                _last_monday_date = None
                await asyncio.sleep(300)
                continue
            try:
                hh, mm = [int(x) for x in MONDAY_PLAYBOOK_TIME_ET.split(":", 1)]
            except ValueError:
                hh, mm = 9, 25
            target = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= target:
                if _last_monday_date != now_et.date():
                    channel = bot.get_channel(MONDAY_PLAYBOOK_CHANNEL_ID)
                    if channel is None:
                        try:
                            channel = await bot.fetch_channel(MONDAY_PLAYBOOK_CHANNEL_ID)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[MONDAY][WARN] Unable to fetch channel {MONDAY_PLAYBOOK_CHANNEL_ID}: {exc}")
                            await asyncio.sleep(60)
                            continue
                    if channel is None:
                        print(f"[MONDAY][WARN] Channel {MONDAY_PLAYBOOK_CHANNEL_ID} not accessible")
                        await asyncio.sleep(60)
                        continue
                    session = market_session_et()
                    if session == "CLOSED":
                        msg = (
                            "≡ƒôÿ **Monday Playbook**\nET: "
                            + datetime.now(ET).strftime("%Y-%m-%d %H:%M")
                            + "\nΓÜá∩╕Å Market session is CLOSED. Holiday or unexpected closureΓÇöverify market hours."
                        )
                        await channel.send(msg)
                    else:
                        symbols = MONDAY_PLAYBOOK_SYMBOLS or ["SPY", "QQQ", "IWM"]
                        await channel.send(format_monday_playbook(symbols))
                    _last_monday_date = now_et.date()
                    print(f"[MONDAY] Playbook posted for {_last_monday_date}")
                await asyncio.sleep(60)
            else:
                wait_seconds = max(5.0, min(300.0, (target - now_et).total_seconds()))
                await asyncio.sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"[MONDAY][ERROR] {exc}")
            await asyncio.sleep(60)


def is_rth_now() -> bool:
    return market_session_et() == "RTH"


async def model_sched_loop() -> None:
    lockfile = Path("logs/model_sched.lock")
    while True:
        try:
            if (not MODEL_SCHED_RTH_ONLY) or is_rth_now():
                if lockfile.exists():
                    print("[SCHED] lock present, skipping run")
                else:
                    Path("logs").mkdir(exist_ok=True)
                    lockfile.write_text(now_utc_iso())
                    try:
                        print("[SCHED] running model/run_model.py")
                        subprocess.run([sys.executable, "model/run_model.py"], check=False)
                    finally:
                        try:
                            lockfile.unlink()
                        except OSError:
                            pass
            await asyncio.sleep(MODEL_SCHED_EVERY_SEC)
        except Exception as exc:  # noqa: BLE001
            print(f"[SCHED_ERROR] {exc}")
            await asyncio.sleep(30)


async def vix_alert_loop() -> None:
    global _last_vix_alert_level, _last_vix_alert_ts
    while True:
        try:
            if not VIX_ALERTS_ENABLED or not VIX_ALERT_CHANNEL_ID:
                await asyncio.sleep(30)
                continue
            vix = get_vix_context("1m")
            if not vix:
                await asyncio.sleep(VIX_ALERT_CHECK_SEC)
                continue
            level = float(vix["level"])
            ts = str(vix["ts"])
            if _last_vix_alert_level is None:
                _last_vix_alert_level = level
                _last_vix_alert_ts = ts
                await asyncio.sleep(VIX_ALERT_CHECK_SEC)
                continue
            change = level - _last_vix_alert_level
            pct = (change / _last_vix_alert_level) * 100 if _last_vix_alert_level else 0.0
            if abs(change) >= VIX_ALERT_ABS_MOVE or abs(pct) >= VIX_ALERT_PCT_MOVE:
                channel = bot.get_channel(VIX_ALERT_CHANNEL_ID)
                if channel:
                    await channel.send(
                        "ΓÜá∩╕Å **VIX move alert**: {old:.2f} ΓåÆ {new:.2f} ({chg:+.2f}, {pct:+.2f}%) | Regime: {regime} | ts={ts}".format(
                            old=_last_vix_alert_level,
                            new=level,
                            chg=change,
                            pct=pct,
                            regime=vix_regime(level),
                            ts=ts,
                        )
                    )
                _last_vix_alert_level = level
                _last_vix_alert_ts = ts
            await asyncio.sleep(VIX_ALERT_CHECK_SEC)
        except Exception as exc:  # noqa: BLE001
            print(f"[VIX_ALERT_ERROR] {exc}")
            await asyncio.sleep(30)


async def _fetch_bar_via_api(symbol: str, tf: str, base_url: str, api_key: str, provider: str) -> Optional[Tuple[str, float, float, float, float]]:
    if not base_url or not api_key:
        return None
    tf_map = {"1m": (1, "minute")}
    mapping = tf_map.get(tf)
    if mapping is None:
        return None
    multiplier, timespan = mapping
    now_utc = datetime.now(timezone.utc)
    start = now_utc - timedelta(minutes=max(5, multiplier * 3))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now_utc.timestamp() * 1000)
    sym_enc = quote(symbol.upper(), safe="")
    url = f"{base_url}/v2/aggs/ticker/{sym_enc}/range/{multiplier}/{timespan}/{start_ms}/{end_ms}"
    params = {"adjusted": "true", "sort": "desc", "limit": 1, "apiKey": api_key}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"[DATA][WARN] {provider} fetch failed for {symbol}: {exc}")
        return None
    results = data.get("results") or []
    if not results:
        return None
    latest = results[0]
    try:
        ts_raw = latest.get("t")
        o = latest.get("o")
        h = latest.get("h")
        l = latest.get("l")
        c = latest.get("c")
        if ts_raw is None or None in (o, h, l, c):
            return None
        ts = datetime.fromtimestamp(float(ts_raw) / 1000.0, timezone.utc).isoformat()
        return ts, float(o), float(h), float(l), float(c)
    except Exception as exc:  # noqa: BLE001
        print(f"[DATA][WARN] {provider} parse error for {symbol}: {exc}")
        return None


async def get_live_bar(symbol: str, tf: str = "1m") -> Optional[Tuple[str, float, float, float, float, str]]:
    providers: list[Tuple[str, str, str]] = []
    if USE_MASSIVE and MASSIVE_API_KEY and MASSIVE_BASE_URL:
        providers.append(("massive", MASSIVE_BASE_URL, MASSIVE_API_KEY))
    if not POLYGON_DISABLED and POLYGON_API_KEY:
        providers.append(("polygon", POLYGON_BASE_URL, POLYGON_API_KEY))
    for provider_name, base, key in providers:
        bar = await _fetch_bar_via_api(symbol, tf, base, key, provider_name)
        if bar:
            ts, o, h, l, c = bar
            return ts, o, h, l, c, provider_name
    return None


async def _autopost_loop(channel: discord.abc.Messageable) -> None:
    symbols = AUTOPOST_SYMBOLS or [
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "TSLA",
        "VIX",
    ]
    if AUTOPOST_COMBINED:
        symbols = list(dict.fromkeys(symbols))
    poll_seconds = max(1, AUTOPOST_POLL_SEC)
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            vix = get_vix_context("1m")
            state, reason = vix_gating_decision(vix)
            global _last_vix_gating_state
            state_key = f"{state}:{reason}" if reason else state
            if state_key != _last_vix_gating_state:
                if reason:
                    print(f"[GATE] VIX gating {state} ({reason})")
                else:
                    print(f"[GATE] VIX gating {state}")
                _last_vix_gating_state = state_key
            if state == "hard":
                await asyncio.sleep(poll_seconds)
                continue
            edge_threshold = AUTOPOST_CONF_THRESH
            gating_note = ""
            if state == "soft" and reason:
                edge_threshold = max(edge_threshold, AUTOPOST_CONF_THRESH * 1.5)
                gating_note = f"ΓÜá∩╕Å VIX gating active: {reason}"
            if AUTOPOST_COMBINED:
                latest = [sig for sym in symbols if (sig := get_latest_signal_full(sym)) is not None]
                if latest:
                    post_lines: list[str] = []
                    no_edge: list[str] = []
                    updated_any = False
                    posted_horizons: set[int] = set()
                    posted_models: set[str] = set()
                    for sig in latest:
                        sym2, ts, horizon_min, prob_up, prob_down, model_version, _meta = sig
                        prev = _last_posted_ts.get(sym2)
                        if prev == ts:
                            continue
                        updated_any = True
                        edge = abs(prob_up - 0.5)
                        if edge < edge_threshold:
                            _last_posted_ts[sym2] = ts
                            no_edge.append(sym2)
                            continue
                        arrow = "Γåæ" if prob_up >= 0.5 else "Γåô"
                        post_lines.append(f"- **{sym2}**: **{prob_up:.2f}** {arrow} (edge {edge:.2f})")
                        if horizon_min is not None:
                            posted_horizons.add(int(horizon_min))
                        if model_version:
                            posted_models.add(model_version)
                        _last_posted_ts[sym2] = ts
                    if post_lines:
                        if gating_note:
                            post_lines.insert(0, gating_note)
                        if not posted_horizons:
                            horizon_str = str(HORIZON_MIN)
                        elif len(posted_horizons) == 1:
                            horizon_str = str(next(iter(posted_horizons)))
                        else:
                            horizon_str = "mixed"
                        if not posted_models:
                            model_str = MODEL_VERSION
                        elif len(posted_models) == 1:
                            model_str = next(iter(posted_models))
                        else:
                            model_str = "mixed"
                        message = (
                            f"≡ƒôú **0DTE Signals** (horizon **{horizon_str}m**, model **{model_str}**)\n"
                            + "\n".join(post_lines)
                        )
                        if no_edge:
                            message += "\n\n_No edge:_ " + ", ".join(sorted(no_edge))
                        await channel.send(message)
                    elif updated_any and no_edge:
                        print("[SKIP] Combined signals below threshold: " + ", ".join(sorted(no_edge)))
            else:
                for sym in symbols:
                    sig = get_latest_signal_full(sym)
                    if not sig:
                        continue
                    sym2, ts, horizon_min, prob_up, prob_down, model_version, _meta = sig
                    prev = _last_posted_ts.get(sym2)
                    if prev == ts:
                        continue
                    edge = abs(prob_up - 0.5)
                    if edge < edge_threshold:
                        print(f"[SKIP] {sym2} signal edge {edge:.3f} < threshold {edge_threshold:.3f}")
                        _last_posted_ts[sym2] = ts
                        continue
                    last_price = get_latest_price(sym2)
                    message = format_signal(sig, last_price)
                    if gating_note:
                        message += "\n" + gating_note
                    await channel.send(message)
                    _last_posted_ts[sym2] = ts
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] autopost loop error: {exc}")
        await asyncio.sleep(poll_seconds)


async def _ensure_autopost_task() -> None:
    global _autopost_task
    if (os.getenv("TNT_AUTOMATION_ENABLED", "0") or "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    if not AUTOPOST_ENABLED:
        return
    if CHANNEL_ID == 0:
        print("[WARN] DISCORD_CHANNEL_ID missing; cannot enable autopost")
        return
    if _autopost_task and not _autopost_task.done():
        return
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Unable to fetch channel {CHANNEL_ID}: {exc}")
            return
    print(f"[OK] Autopost enabled. channel_id={CHANNEL_ID} poll={AUTOPOST_POLL_SEC}s combined={AUTOPOST_COMBINED}")
    _autopost_task = asyncio.create_task(_autopost_loop(channel))


def _extract_ai_text(resp: object) -> str:
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    choices = getattr(resp, "choices", None)
    if choices:
        try:
            message = choices[0].message
            content = getattr(message, "content", "")
            if isinstance(content, list):
                pieces = [item.get("text", "") for item in content if isinstance(item, dict)]
                content = "".join(pieces)
            if isinstance(content, str) and content.strip():
                return content.strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


@bot.event
async def on_ready() -> None:
    print(f"[OK] Logged in as {bot.user} (guilds={len(bot.guilds)})")
    if (os.getenv("TNT_AUTOMATION_ENABLED", "0") or "0").strip().lower() not in {"1", "true", "yes", "on"}:
        print("[OK] automation: disabled (TNT_AUTOMATION_ENABLED=0)")
        return
    if DAILY_SUMMARY_ENABLED and DAILY_SUMMARY_CHANNEL_ID:
        bot.loop.create_task(daily_summary_loop())
        print(f"[OK] Daily summary enabled: {DAILY_SUMMARY_TIME_ET} ET -> channel_id={DAILY_SUMMARY_CHANNEL_ID}")
    if MODEL_SCHED_ENABLED:
        bot.loop.create_task(model_sched_loop())
        print(f"[OK] Model scheduler enabled: every {MODEL_SCHED_EVERY_SEC}s RTH_only={MODEL_SCHED_RTH_ONLY}")
    if VIX_ALERTS_ENABLED and VIX_ALERT_CHANNEL_ID:
        bot.loop.create_task(vix_alert_loop())
        print(f"[OK] VIX alerts enabled every {VIX_ALERT_CHECK_SEC}s -> channel_id={VIX_ALERT_CHANNEL_ID}")
    if MONDAY_PLAYBOOK_ENABLED and MONDAY_PLAYBOOK_CHANNEL_ID:
        bot.loop.create_task(monday_playbook_loop())
        print(f"[OK] Monday playbook enabled: {MONDAY_PLAYBOOK_TIME_ET} ET -> channel_id={MONDAY_PLAYBOOK_CHANNEL_ID}")
    await _ensure_autopost_task()


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    try:
        print(
            "[MSG] from={author} content={content!r} mentions={mentions} role_mentions={role_mentions}".format(
                author=message.author,
                content=message.content,
                mentions=[m.id for m in message.mentions],
                role_mentions=[r.id for r in message.role_mentions],
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[MSG][ERROR] {exc}")
    await bot.process_commands(message)
    if not AI_ENABLED:
        return
    if message.guild is None:
        return
    mentioned_user = bool(bot.user and bot.user in message.mentions)
    mentioned_role = bool(AI_ROLE_ID) and any(r.id == AI_ROLE_ID for r in message.role_mentions)
    triggered = mentioned_user or mentioned_role
    if AI_MODE == "mention" and not triggered:
        return
    if AI_MODE == "channel" and message.channel.id != AI_CHANNEL_ID:
        return
    if message.content.strip().startswith("!"):
        return
    user_text = message.content
    if bot.user:
        user_text = user_text.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    if AI_ROLE_ID:
        user_text = user_text.replace(f"<@&{AI_ROLE_ID}>", "")
    user_text = user_text.strip()
    if not user_text:
        await message.reply("Ask me a question ≡ƒÖé", mention_author=False)
        return
    symbols = [
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "TSLA",
        "VIX",
    ]
    requested = None
    upper = user_text.upper()
    for sym in symbols:
        if sym in upper:
            requested = sym
            break
    market_context = ""
    if requested:
        last_close: Optional[float] = None
        session = market_session_et()
        market_context += f"Session (ET): {session}\n"
        if session != "RTH":
            market_context += "Note: Outside RTH, equity 1m bars may be sparse/stale.\n"
        bar = get_latest_bar(requested, tf="1m")
        if bar:
            ts, o, h, l, c = bar
            market_context += f"Latest {requested} 1m bar (DB): ts={ts}, O={o:.2f}, H={h:.2f}, L={l:.2f}, C={c:.2f}\n"
            last_close = float(c)
        dbar = get_latest_daily_bar(requested)
        if dbar:
            dts, _o, dh, dl, dc = dbar
            pivots = pivots_from_hlc(float(dh), float(dl), float(dc))
            below, above = nearest_levels(last_close or float(dc), pivots, k=2)
            market_context += (
                f"Daily pivots (from 1d bar @ {dts}): P={pivots['P']:.2f} R1={pivots['R1']:.2f} S1={pivots['S1']:.2f} "
                f"R2={pivots['R2']:.2f} S2={pivots['S2']:.2f}\n"
                f"Nearest supports: {', '.join([f'{n} {v:.2f}' for n, v in below]) or 'none'}\n"
                f"Nearest resistances: {', '.join([f'{n} {v:.2f}' for n, v in above]) or 'none'}\n"
            )
        _sql, sig = get_latest_signal_context(requested)
        if sig:
            prob_up = float(sig[1]) if len(sig) > 1 and sig[1] is not None else 0.5
            prob_down = float(sig[2]) if len(sig) > 2 and sig[2] is not None else (1.0 - prob_up)
            edge = float(sig[4]) if len(sig) > 4 and sig[4] is not None else abs(prob_up - 0.5)
            model_name = sig[3] if len(sig) > 3 else "unknown"
            conv = conviction_from_edge(edge)
            market_context += (
                f"Latest signal: ts={sig[0]}, model={model_name}, prob_up={prob_up:.3f}, prob_down={prob_down:.3f}, "
                f"edge={edge:.3f}, conviction={conv}\n"
            )
            v = get_vix_context("1m")
            if v and VIX_GATING_ENABLED:
                gate = vix_gating_action(float(v["level"]))
                edge_adj = edge * float(gate["mult"])
                conv = conviction_from_edge(edge_adj)
                market_context += (
                    f"VIX gate: {gate['mode']} ({gate['reason']}); edge {edge:.3f} -> {edge_adj:.3f}; conviction={conv}\n"
                )
                if gate["mode"] == "HARD":
                    market_context += "Trading posture: STAND DOWN (wait for VIX to cool).\n"
            if len(sig) >= 8 and sig[6] is not None and sig[7] is not None:
                trend = float(sig[6])
                vol_z = float(sig[7])
                market_context += (
                    f"Regime: {('VOL EXPANSION' if vol_z >= 1.5 else 'TREND' if abs(trend) >= 0.004 else 'RANGE / CHOP' if abs(trend) <= 0.0015 and vol_z <= 0.8 else 'MIXED')} "
                    f"(trend={trend:+.4f}, vol_z={vol_z:+.2f})\n"
                )
        acc = rolling_accuracy(requested, horizon_min=HORIZON_MIN, n=50, tf="1m")
        if acc and acc.get("acc") is not None:
            market_context += (
                f"Rolling accuracy: {acc['acc']*100:.1f}% over last {acc['matured']} matured of {acc['considered']} signals (@{HORIZON_MIN}m)\n"
            )
        vix = get_vix_context("1m")
        if vix:
            market_context += f"VIX: {vix['level']:.2f} ({vix['regime']}) as of {vix['ts']}\n"
    market_context = market_context.strip()
    print(f"[AI] responding to prompt={user_text!r}")
    requested_symbol = requested or "SPY"
    tnt_state = build_canonical_tnt_state([requested_symbol], mode="REALTIME")
    prompt = (
        f"User: {user_text}\n\n"
        "Answer using ONLY TNT_STATE. If stale or no_trade, stand down."
    ).strip()
    async with message.channel.typing():
        try:
            result = await call_tnt_agent_async(
                tnt_state=tnt_state,
                user_text=prompt,
                label="discord_head_ai",
                model=AI_MODEL,
                max_output_tokens=420,
                temperature=0.25,
            )
            reply_text = (result.text or "").strip() or "(No response text returned.)"
            await message.reply(reply_text[:AI_MAX_CHARS], mention_author=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[AI ERROR] {exc}")
            await message.reply(f"[AI error] {exc}", mention_author=False)


@bot.command()
async def health(ctx: commands.Context) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = _rows(conn, "SELECT symbol, max(ts) FROM prices GROUP BY symbol")
    lines = [f"≡ƒ⌐║ Health Check (UTC {now})"]
    for sym, ts in rows:
        lines.append(f"- {sym}: {_format_ts(ts)}")
    await ctx.send("\n".join(lines))


@bot.command(name="playbook")
async def playbook_cmd(ctx: commands.Context) -> None:
    symbols = MONDAY_PLAYBOOK_SYMBOLS or ["SPY", "QQQ", "IWM"]
    await ctx.send(format_monday_playbook(symbols))


@bot.command(name="daily")
async def daily_cmd(ctx: commands.Context) -> None:
    await ctx.send(format_daily_summary(DAILY_SUMMARY_SYMBOLS))


@bot.command(name="vix")
async def vix_cmd(ctx: commands.Context) -> None:
    v = get_vix_context("1m")
    if not v:
        await ctx.send("ΓÜá∩╕Å No VIX data found in DB yet. (Check ingest symbol mapping.)")
        return
    await ctx.send(f"≡ƒºá VIX: **{v['level']:.2f}** ({v['regime']}) as of {v['ts']}")


@bot.command(name="vixtrend")
async def vixtrend_cmd(ctx: commands.Context, minutes: int = 30) -> None:
    now = get_latest_close("VIX", "1m")
    if not now:
        await ctx.send("ΓÜá∩╕Å No VIX data found in DB yet.")
        return
    now_dt = parse_iso(now["ts"])
    past = get_close_at_or_before("VIX", "1m", (now_dt - timedelta(minutes=int(minutes))).isoformat())
    if not past:
        await ctx.send(f"ΓÜá∩╕Å Not enough VIX history to compute {minutes}m trend.")
        return
    chg = now["close"] - past["close"]
    pct = (chg / past["close"]) * 100 if past["close"] else 0.0
    direction = "UP" if chg > 0 else ("DOWN" if chg < 0 else "FLAT")
    await ctx.send(
        f"≡ƒôê VIX trend ({minutes}m): **{direction}** | {past['close']:.2f} ΓåÆ {now['close']:.2f} "
        f"({chg:+.2f}, {pct:+.2f}%)"
    )


def _format_signal_details(symbol: str, signal: Tuple[str, float, float, str, Optional[float], Dict[str, float]]) -> list[str]:
    ts, prob_up, prob_down, model_version, edge, meta = signal
    edge = edge if edge is not None else abs(prob_up - 0.5)
    arrow = "Γåæ" if prob_up >= 0.5 else "Γåô"
    lines = [
        f"Signal: **{symbol}** {arrow} prob_up={prob_up:.3f} prob_down={prob_down:.3f}",
        f"Model `{model_version}` | Edge {edge:.3f} | Conv {conviction_from_edge(edge)}",
        f"Signal ts: {_format_ts(ts)}",
    ]
    if meta:
        lines.append("Meta: " + " | ".join(f"{k}={v:.2f}" for k, v in meta.items()))
    vix = get_vix_context("1m")
    if vix and VIX_GATING_ENABLED:
        state, reason = vix_gating_decision(vix)
        lines.append(f"VIX {vix['level']:.2f} ({vix['regime']}) -> gate {state}")
        if reason:
            lines.append(f"Reason: {reason}")
    return lines


@bot.command(name="last")
async def last_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    symbol = symbol.upper()
    latest = get_latest_signal(symbol)
    if not latest:
        await ctx.send(f"ΓÜá∩╕Å No signal found for {symbol}.")
        return
    price_info = get_latest_price(symbol)
    lines = _format_signal_details(symbol, latest)
    if price_info:
        price, ts = price_info
        lines.append(f"Last price: {price:.2f} @ {_format_ts(ts)}")
    await ctx.send("\n".join(lines))


@bot.command(name="pivots")
async def pivots_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    symbol = symbol.upper()
    piv = get_latest_daily_pivots(symbol)
    if not piv:
        await ctx.send(f"ΓÜá∩╕Å No pivots available for {symbol}.")
        return
    lines = [f"≡ƒÄ» {symbol} pivots"]
    if "session" in piv:
        lines.append(f"Source session: {piv['session']} (ts={_format_ts(piv['ts'])})")
    else:
        lines.append(f"Source daily bar ts={_format_ts(piv['ts'])}")
    vals = piv["piv"]
    lines.append(
        "P {P:.2f} | R1 {R1:.2f} | R2 {R2:.2f} | R3 {R3:.2f} | "
        "S1 {S1:.2f} | S2 {S2:.2f} | S3 {S3:.2f}".format(**vals)
    )
    await ctx.send("\n".join(lines))


@bot.command(name="gating")
async def gating_cmd(ctx: commands.Context) -> None:
    vix = get_vix_context("1m")
    if not vix:
        await ctx.send("ΓÜá∩╕Å VIX context unavailable.")
        return
    mode, reason = vix_gating_decision(vix)
    msg = f"VIX {vix['level']:.2f} ({vix['regime']}) -> gate {mode}"
    if reason:
        msg += f" | {reason}"
    await ctx.send(msg)


def main() -> None:
    if not TOKEN:
        print("[FATAL] DISCORD_BOT_TOKEN not set")
        sys.exit(1)
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        print("[STOP] Keyboard interrupt, shutting down bot")


if __name__ == "__main__":
    main()
