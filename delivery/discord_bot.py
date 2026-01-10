"""Discord bot for Zero DTE pipeline automation."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import csv
import shutil
import math
import os
import heapq
from collections import deque
import re
import sqlite3
import subprocess
import sys
import time as time_lib
import json
from collections.abc import Iterable
from dataclasses import dataclass
import io
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import pandas as pd
import aiohttp
from zoneinfo import ZoneInfo
from market_data.last_price import (
    LastPrice as PriceSnapshot,
    resolve_last_price as resolve_last_price_policy,
    format_last_price_block,
    market_state_label,
)

# Retain compatibility for scripts/tests expecting discord_bot.LastPrice
LastPrice = PriceSnapshot

SILENCE_NOTICE = (
    "🔕 TNT STATUS\n"
    "Market conditions unstable / low-confidence.\n"
    "No Trade Context issued."
)
from zero_dte_pipeline.candidates.market_participation_gate import (
    ParticipationGateResult,
    apply_participation_gate,
)
from zero_dte_pipeline.tech.contracts import (
    AUTOPOST_CONTRACT_VERSION,
    ContractConstraints,
    DEFAULT_MAX_CHARS_BY_PAYLOAD,
    DEFAULT_MAX_CHARS_DEFAULT,
    DEFAULT_MAX_EMOJI,
    DEFAULT_MAX_LINES,
    INSIGHTS_TECHNICAL_TERMS,
    contract_violations,
    validate_options_framework,
)

from delivery.options_framework import build_options_framework

from delivery.tnt_prompt import load_tnt_system_prompt
from delivery.tnt_state import build_tnt_state_from_analysis_payload, build_tnt_state_from_packet, enrich_tnt_state, format_tnt_state_block
from delivery.tnt_chart_contract import FOOTER_DISCLAIMER, validate_chart_spec
from delivery.options_chain_summary import summarize_options_chain




ET = ZoneInfo("America/New_York")
ET_TZ = ET

# --- Health stats: Polygon options HTTP throttle ---
_OPTIONS_HTTP_LOCK = asyncio.Lock()
_OPTIONS_HTTP_ACTIVE = 0
_OPTIONS_HTTP_PEAK = 0


def get_options_http_stats() -> dict[str, int]:
    """Best-effort health stats for Polygon options HTTP throttling."""
    try:
        configured_cap = int(os.getenv("TNT_MAX_POLYGON_OPTIONS_HTTP", "12"))
    except Exception:
        configured_cap = 12
    return {
        "cap": int(max(1, configured_cap)),
        "active": int(_OPTIONS_HTTP_ACTIVE),
        "peak_active": int(_OPTIONS_HTTP_PEAK),
    }

def _f(x, nd=2):
    try:
        if x is None:
            return None
        return round(float(x), nd)
    except Exception:
        return None


def _fmt(x, nd=2):
    val = _f(x, nd)
    return "n/a" if val is None else f"{val:.{nd}f}"


def _delta(a, b, nd=2):
    a_val = _f(a, nd)
    b_val = _f(b, nd)
    if a_val is None or b_val is None:
        return None
    return round(a_val - b_val, nd)


def _is_num(x):
    try:
        float(x)
        return True
    except Exception:
        return False


def detect_regime(price: float, P: float, R1: float, R2: float, S1: float, S2: float, eps: float = 1e-9) -> str:
    """
    Regime based on where price is vs pivot bands.
    """

    if price is None or P is None or R1 is None or S1 is None:
        return "UNKNOWN"
    if R2 is not None and price > R2 + eps:
        return "EXTENSION_UP"
    if S2 is not None and price < S2 - eps:
        return "EXTENSION_DOWN"
    if price >= R1 + eps:
        return "ABOVE_R1"
    if price <= S1 - eps:
        return "BELOW_S1"
    if price >= P + eps:
        return "ABOVE_P"
    if price <= P - eps:
        return "BELOW_P"
    return "RANGE"


def ladder_levels(price: float, levels: Dict[str, Optional[float]], nd: int = 2) -> Dict[str, List[Tuple[str, float]]]:
    """
    Returns only *valid forward* targets.
    - If price is below S1, do NOT show S1 as a downside target.
    - If price is above R1, do NOT show R1 as an upside target.
    """

    price_val = _f(price, nd)
    P = _f(levels.get("P"), nd)
    R1 = _f(levels.get("R1"), nd)
    R2 = _f(levels.get("R2"), nd)
    R3 = _f(levels.get("R3"), nd)
    S1 = _f(levels.get("S1"), nd)
    S2 = _f(levels.get("S2"), nd)
    S3 = _f(levels.get("S3"), nd)

    ups = [("P", P), ("R1", R1), ("R2", R2), ("R3", R3)]
    dns = [("S1", S1), ("S2", S2), ("S3", S3)]

    ups_valid = [
        (k, v)
        for (k, v) in ups
        if v is not None and price_val is not None and v > price_val
    ]
    dns_valid = [
        (k, v)
        for (k, v) in dns
        if v is not None and price_val is not None and v < price_val
    ]

    ups_valid.sort(key=lambda kv: kv[1])
    dns_valid.sort(key=lambda kv: kv[1], reverse=True)

    return {"up": ups_valid, "down": dns_valid}


@dataclass(slots=True)
class RenderedPost:
    """Container for formatted text plus deterministic agent context."""

    text: str
    agent_payload: Optional[Dict[str, Any]] = None
    files: Optional[list[tuple[str, bytes]]] = None

    def __str__(self) -> str:  # pragma: no cover - utility for legacy call sites
        return self.text


@dataclass(slots=True)
class _AnalyzeCacheEntry:
    """Small holder for cached /analyze renders."""

    render: RenderedPost
    ts: float


_ANALYZE_CACHE: dict[str, _AnalyzeCacheEntry] = {}


@dataclass(slots=True)
class _OptionsMicroCacheEntry:
    """Small holder for cached options microstructure summaries."""

    packet: dict[str, Any]
    ts: float


_OPTIONS_MICRO_CACHE: dict[str, _OptionsMicroCacheEntry] = {}


@dataclass(slots=True)
class _GroupedDailyCacheEntry:
    """Cache holder for Polygon grouped daily bars by date."""

    results: list[dict[str, Any]]
    ts: float


_GROUPED_DAILY_CACHE: dict[str, _GroupedDailyCacheEntry] = {}

def _env_float(name: str, fallback: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except Exception:  # noqa: BLE001
        return fallback


def _env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except Exception:  # noqa: BLE001
        return fallback


_ANALYZE_CACHE_TTL_SEC = _env_float("ANALYZE_CACHE_TTL_SEC", 86400.0)
_ANALYZE_CACHE_MAX = _env_int("ANALYZE_CACHE_MAX", 32)

_OPTIONS_MICRO_TTL_SEC = _env_float("OPTIONS_MICRO_TTL_SEC", 300.0)
_OPTIONS_MICRO_MAX = _env_int("OPTIONS_MICRO_MAX", 16)

_GROUPED_DAILY_TTL_SEC = _env_float("GROUPED_DAILY_TTL_SEC", 3600.0)
_GROUPED_DAILY_CACHE_MAX = _env_int("GROUPED_DAILY_CACHE_MAX", 7)

_ETF_CONSTITUENTS_TTL_SEC = _env_float("ETF_CONSTITUENTS_TTL_SEC", 21600.0)


def _get_cached_options_micro(symbol: str) -> Optional[_OptionsMicroCacheEntry]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    entry = _OPTIONS_MICRO_CACHE.get(sym)
    if not entry:
        return None
    if _OPTIONS_MICRO_TTL_SEC > 0:
        now = time_lib.time()
        if now - entry.ts > _OPTIONS_MICRO_TTL_SEC:
            _OPTIONS_MICRO_CACHE.pop(sym, None)
            return None
    return entry


def _set_cached_options_micro(symbol: str, packet: dict[str, Any], *, ts: Optional[float] = None) -> None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return
    timestamp = float(ts) if ts is not None else time_lib.time()
    _OPTIONS_MICRO_CACHE[sym] = _OptionsMicroCacheEntry(packet=dict(packet), ts=timestamp)
    if _OPTIONS_MICRO_MAX <= 0 or len(_OPTIONS_MICRO_CACHE) <= _OPTIONS_MICRO_MAX:
        return
    while len(_OPTIONS_MICRO_CACHE) > _OPTIONS_MICRO_MAX:
        oldest_key = min(_OPTIONS_MICRO_CACHE.items(), key=lambda item: item[1].ts)[0]
        _OPTIONS_MICRO_CACHE.pop(oldest_key, None)


def _polygon_key_and_base() -> tuple[Optional[str], str, str]:
    """Return (api_key, base_url, provider_label).

    If Massive is enabled (key present + ZERO_DTE_USE_MASSIVE=1), prefer that key/base.
    """

    use_massive = os.getenv("ZERO_DTE_USE_MASSIVE", "0") == "1"
    massive_key = (os.getenv("MASSIVE_API_KEY") or "").strip()
    massive_base = (os.getenv("MASSIVE_BASE_URL") or "").strip().rstrip("/")
    if use_massive and massive_key:
        return massive_key, (massive_base or "https://api.polygon.io"), "massive"

    try:
        from zero_dte_pipeline.config import config

        polygon_key = (config.polygon_api_key or "").strip()
    except Exception:  # noqa: BLE001
        polygon_key = (os.getenv("POLYGON_API_KEY") or "").strip()

    return (polygon_key or None), "https://api.polygon.io", "polygon"


@dataclass(slots=True)
class _EtfConstituentsCacheEntry:
    tickers: set[str]
    ts: float


_ETF_CONSTITUENTS_CACHE: dict[str, _EtfConstituentsCacheEntry] = {}


def _get_cached_etf_constituents(composite_ticker: str) -> Optional[set[str]]:
    key = (composite_ticker or "").strip().upper()
    if not key:
        return None
    entry = _ETF_CONSTITUENTS_CACHE.get(key)
    if not entry:
        return None
    if _ETF_CONSTITUENTS_TTL_SEC > 0:
        now = time_lib.time()
        if now - entry.ts > _ETF_CONSTITUENTS_TTL_SEC:
            _ETF_CONSTITUENTS_CACHE.pop(key, None)
            return None
    return set(entry.tickers)


def _set_cached_etf_constituents(composite_ticker: str, tickers: set[str], *, ts: Optional[float] = None) -> None:
    key = (composite_ticker or "").strip().upper()
    if not key:
        return
    timestamp = float(ts) if ts is not None else time_lib.time()
    _ETF_CONSTITUENTS_CACHE[key] = _EtfConstituentsCacheEntry(tickers=set(tickers), ts=timestamp)


def _with_api_key(url: str, api_key: str) -> str:
    """Ensure apiKey is present in the query string."""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["apiKey"] = api_key
    return urlunparse(parsed._replace(query=urlencode(query)))


async def _fetch_etf_constituents(composite_ticker: str) -> Optional[set[str]]:
    """Fetch ETF constituents tickers for a composite ticker (e.g., SPY).

    Uses Polygon ETF Global endpoint: GET /etf-global/v1/constituents?composite_ticker=SPY
    """

    sym = (composite_ticker or "").strip().upper()
    if not sym:
        return None

    cached = _get_cached_etf_constituents(sym)
    if cached is not None and cached:
        return cached

    api_key, base_url, _provider = _polygon_key_and_base()
    if not api_key:
        return None

    # This endpoint may not be enabled on all Polygon plans; handle failures gracefully.
    next_url: Optional[str] = f"{base_url}/etf-global/v1/constituents"
    params: dict[str, Any] = {
        "composite_ticker": sym,
        "limit": 1000,
        "apiKey": api_key,
    }

    out: set[str] = set()
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(10):
            if not next_url:
                break
            url = next_url
            if url.startswith("/"):
                url = f"{base_url}{url}"
            if url.startswith("http") and "apiKey=" not in url:
                url = _with_api_key(url, api_key)

            try:
                async with session.get(url, params=None if "?" in url else params) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json()
            except Exception:  # noqa: BLE001
                return None

            results = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(results, list):
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    t = str(item.get("constituent_ticker") or "").strip().upper()
                    if t:
                        out.add(t)

            nxt = payload.get("next_url") if isinstance(payload, dict) else None
            if isinstance(nxt, str) and nxt.strip():
                next_url = nxt.strip()
                params = {"apiKey": api_key}
                continue
            break

    if not out:
        return None

    _set_cached_etf_constituents(sym, out)
    return out


def _get_cached_grouped_daily(date_ymd: str) -> Optional[list[dict[str, Any]]]:
    key = (date_ymd or "").strip()[:10]
    if not key:
        return None
    entry = _GROUPED_DAILY_CACHE.get(key)
    if not entry:
        return None
    if _GROUPED_DAILY_TTL_SEC > 0:
        now = time_lib.time()
        if now - entry.ts > _GROUPED_DAILY_TTL_SEC:
            _GROUPED_DAILY_CACHE.pop(key, None)
            return None
    return entry.results


def _set_cached_grouped_daily(date_ymd: str, results: list[dict[str, Any]], *, ts: Optional[float] = None) -> None:
    key = (date_ymd or "").strip()[:10]
    if not key:
        return
    timestamp = float(ts) if ts is not None else time_lib.time()
    _GROUPED_DAILY_CACHE[key] = _GroupedDailyCacheEntry(results=list(results), ts=timestamp)
    if _GROUPED_DAILY_CACHE_MAX <= 0 or len(_GROUPED_DAILY_CACHE) <= _GROUPED_DAILY_CACHE_MAX:
        return
    while len(_GROUPED_DAILY_CACHE) > _GROUPED_DAILY_CACHE_MAX:
        oldest_key = min(_GROUPED_DAILY_CACHE.items(), key=lambda item: item[1].ts)[0]
        _GROUPED_DAILY_CACHE.pop(oldest_key, None)


async def _fetch_polygon_grouped_daily(date_ymd: str) -> Optional[list[dict[str, Any]]]:
    """Fetch Polygon grouped daily bars (US stocks) for a specific date.

    Endpoint: /v2/aggs/grouped/locale/us/market/stocks/{date}
    Returns: list of per-ticker bars (dicts) or None.
    """

    date_key = (date_ymd or "").strip()[:10]
    if not date_key:
        return None

    cached = _get_cached_grouped_daily(date_key)
    if cached is not None:
        return cached

    api_key, base_url, _provider = _polygon_key_and_base()
    if not api_key:
        return None

    url = f"{base_url}/v2/aggs/grouped/locale/us/market/stocks/{date_key}"
    params = {
        "adjusted": "true",
        "include_otc": "false",
        "apiKey": api_key,
    }

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
        except Exception:  # noqa: BLE001
            return None

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None

    filtered: list[dict[str, Any]] = []
    for item in results:
        if isinstance(item, dict):
            filtered.append(item)
    if not filtered:
        return None

    _set_cached_grouped_daily(date_key, filtered)
    return filtered


def _is_volume_leader_question(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    # tolerate common typos like "voluime"
    if "voluime" in q:
        q = q.replace("voluime", "volume")

    return bool(
        re.search(r"\b(most|highest|top)\s+(volume|vol)\b", q)
        or re.search(r"\b(volume|vol)\s+(leader|leaders|leadership)\b", q)
    )


def _wants_sp500_universe(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    # Common ways users specify the universe.
    return bool(
        re.search(r"\b(s\&p|s\s*and\s*p)\s*500\b", q)
        or re.search(r"\bsp\s*500\b", q)
        or re.search(r"\bsp500\b", q)
    )


def _is_market_session_question(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    return bool(
        re.search(r"\bmarket\s+(open|closed|status|session)\b", q)
        or re.search(r"\bis\s+the\s+market\s+open\b", q)
        or re.search(r"\b(rth|premarket|pre\s*market|after\s*hours|ah)\b", q)
    )


def _answer_market_session_question() -> str:
    now_et = _now_et()
    session = market_session_et(now_et.astimezone(timezone.utc))
    if session == "RTH":
        detail = f"RTH (09:31–16:00 ET)"
    elif session == "PRE":
        detail = f"PRE (04:00–09:31 ET)"
    elif session == "AH":
        detail = f"AH (16:00–20:00 ET)"
    elif session == "WEEKEND":
        detail = "WEEKEND"
    else:
        detail = "CLOSED"
    return f"Answer: Market session is {detail} as of {now_et.strftime('%Y-%m-%d %H:%M')} ET."


def _is_watchlist_question(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    return bool(
        re.search(r"\bwatch\s*list\b", q)
        or re.search(r"\bwhat\s+are\s+we\s+watching\b", q)
        or re.search(r"\bwhat\s+tickers\b", q)
        or re.search(r"\bwhat\s+symbols\b", q)
    )


def _answer_watchlist_question() -> str:
    try:
        symbols = _fetch_watchlist_symbols()
    except Exception:  # noqa: BLE001
        symbols = []
    if not symbols:
        return "Answer: Watchlist is empty."
    return f"Answer: Watchlist: {', '.join(symbols)}."


def _is_trend_question(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    return bool(re.search(r"\b(trend|trending|daily\s+trend|last\s+\d+\s+(days|sessions))\b", q))


def _is_key_levels_question(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    return bool(
        re.search(r"\b(levels?|pivots?|pivot|support|resistance)\b", q)
        or re.search(r"\b(s1|r1|s2|r2)\b", q)
    )


def _answer_key_levels_question(symbol: str, tnt_state: Mapping[str, Any]) -> Optional[str]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    meta = tnt_state.get("meta") if isinstance(tnt_state.get("meta"), dict) else {}
    permissions = tnt_state.get("permissions") if isinstance(tnt_state.get("permissions"), dict) else {}
    data_health = str(meta.get("data_health") or "UNKNOWN").upper()
    if data_health != "OK" or bool(permissions.get("no_trade")):
        return None

    price = tnt_state.get("price") if isinstance(tnt_state.get("price"), dict) else {}
    levels = tnt_state.get("levels") if isinstance(tnt_state.get("levels"), dict) else {}
    piv = levels.get("pivots_rth") if isinstance(levels.get("pivots_rth"), dict) else {}

    last = price.get("last")
    p = piv.get("P")
    s1 = piv.get("S1")
    r1 = piv.get("R1")

    if not isinstance(last, (int, float)):
        return None
    if not isinstance(p, (int, float)):
        return None

    last_f = float(last)
    p_f = float(p)

    s1_txt = fmt_money(float(s1)) if isinstance(s1, (int, float)) else "n/a"
    r1_txt = fmt_money(float(r1)) if isinstance(r1, (int, float)) else "n/a"

    snapshot = _build_coach_data_snapshot_line(tnt_state)

    # Keep conditions simple and purely level-based.
    bull_cond = f"Hold above Pivot {fmt_money(p_f)}"
    if isinstance(r1, (int, float)):
        bull_cond += f" and accept above R1 {r1_txt}"
    bull_cond += "."

    bear_cond = f"Lose Pivot {fmt_money(p_f)}"
    if isinstance(s1, (int, float)):
        bear_cond += f" and accept below S1 {s1_txt}"
    bear_cond += "."

    invalid = f"Any break one side of Pivot {fmt_money(p_f)} that immediately fails back through it."
    idle = "Price chops around Pivot with no acceptance."

    answer = (
        f"Key levels for {sym}: last {fmt_money(last_f)} | P {fmt_money(p_f)} | S1 {s1_txt} | R1 {r1_txt}. "
        f"({snapshot})"
    )

    return "\n".join(
        [
            f"Answer: {answer}",
            "Bullish only if:",
            f"- {bull_cond}",
            "Bearish if:",
            f"- {bear_cond}",
            "Invalidation:",
            f"- {invalid}",
            "Do nothing if:",
            f"- {idle}",
        ]
    )


async def _volume_leaders_for_date(date_ymd: str, *, top_n: int = 5) -> Optional[list[tuple[str, int]]]:
    results = await _fetch_polygon_grouped_daily(date_ymd)
    if not results:
        return None

    heap: list[tuple[int, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("T") or "").strip().upper()
        if not ticker:
            continue
        vol = item.get("v")
        try:
            vol_int = int(vol)
        except Exception:  # noqa: BLE001
            continue
        if vol_int <= 0:
            continue
        heap.append((vol_int, ticker))

    if not heap:
        return None

    top = heapq.nlargest(max(int(top_n), 1), heap, key=lambda it: it[0])
    return [(ticker, vol) for (vol, ticker) in top]


async def _volume_leaders_for_date_universe(
    date_ymd: str,
    *,
    universe: set[str],
    top_n: int = 5,
) -> Optional[list[tuple[str, int]]]:
    if not universe:
        return None
    results = await _fetch_polygon_grouped_daily(date_ymd)
    if not results:
        return None

    heap: list[tuple[int, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("T") or "").strip().upper()
        if not ticker or ticker not in universe:
            continue
        vol = item.get("v")
        try:
            vol_int = int(vol)
        except Exception:  # noqa: BLE001
            continue
        if vol_int <= 0:
            continue
        heap.append((vol_int, ticker))

    if not heap:
        return None

    top = heapq.nlargest(max(int(top_n), 1), heap, key=lambda it: it[0])
    return [(ticker, vol) for (vol, ticker) in top]


async def _volume_leaders_today(now_et: Optional[datetime] = None, *, lookback_days: int = 7) -> Optional[tuple[str, list[tuple[str, int]]]]:
    now_local = now_et or _now_et()
    base = now_local.date()
    for i in range(max(int(lookback_days), 1)):
        day = base - timedelta(days=i)
        leaders = await _volume_leaders_for_date(day.isoformat(), top_n=5)
        if leaders:
            return day.isoformat(), leaders
    return None


async def _volume_leaders_today_sp500(now_et: Optional[datetime] = None, *, lookback_days: int = 7) -> Optional[tuple[str, list[tuple[str, int]]]]:
    universe = await _fetch_etf_constituents("SPY")
    if not universe:
        return None

    now_local = now_et or _now_et()
    base = now_local.date()
    for i in range(max(int(lookback_days), 1)):
        day = base - timedelta(days=i)
        leaders = await _volume_leaders_for_date_universe(day.isoformat(), universe=universe, top_n=5)
        if leaders:
            return day.isoformat(), leaders
    return None


async def _answer_volume_leader_question(question: str) -> Optional[str]:
    now_et = _now_et()
    api_key, _base_url, provider_label = _polygon_key_and_base()
    if not api_key:
        return "Answer: Data feed is unavailable (no market-data API key configured), so I can’t determine volume leadership today."

    if _wants_sp500_universe(question):
        found = await _volume_leaders_today_sp500(now_et)
        if not found:
            return (
                "Answer: S&P 500 universe volume is unavailable right now (constituents feed not available), "
                "so I can’t determine the S&P 500 volume leader today."
            )
    else:
        found = await _volume_leaders_today(now_et)
    if not found:
        return "Answer: Volume data is unavailable right now (grouped daily bars not returned), so I can’t determine volume leadership today."

    date_ymd, leaders = found
    top_ticker, top_vol = leaders[0]
    tail = ", ".join([f"{t} {v:,}" for (t, v) in leaders])

    universe_label = "S&P 500" if _wants_sp500_universe(question) else "US stocks"
    return f"Answer: {top_ticker} — highest reported share volume on {date_ymd} ({provider_label} grouped daily; {universe_label}). Top 5: {tail}."


def _map_underlying_for_options(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    # SPX weeklies: treat underlying as SPX for Polygon's underlying_ticker.
    if sym == "SPXW":
        return "SPX"
    return sym


async def _fetch_polygon_options_chain_df(
    symbol: str,
    *,
    expiration_ymd: Optional[str] = None,
    strike_window_pct: float = 0.08,
    max_contracts: int = 250,
    concurrency: int = 8,
) -> Optional[pd.DataFrame]:
    """Fetch a *bounded* 0DTE-ish options chain for `symbol` via Polygon/Massive.

    Uses:
    - /v3/reference/options/contracts (bounded by strike window)
    - /v3/snapshot/options/{underlying}/{ticker} for greeks/OI/IV
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    api_key, base_url, _provider = _polygon_key_and_base()
    if not api_key:
        return None

    underlying = _map_underlying_for_options(sym)

    # Process-wide guard for Polygon options HTTP calls.
    # Without this, bursts of /oi or /pcr across users can exceed plan limits quickly.
    global _POLYGON_OPTIONS_HTTP_SEM
    try:
        _POLYGON_OPTIONS_HTTP_SEM  # type: ignore[name-defined]
    except Exception:
        _POLYGON_OPTIONS_HTTP_SEM = asyncio.Semaphore(max(int(os.getenv("TNT_MAX_POLYGON_OPTIONS_HTTP", "12")), 1))

    # Determine expiration date (default: today ET).
    now_et = _now_et()
    exp = (expiration_ymd or now_et.date().isoformat())[:10]

    # Get an approximate underlying price to bound strikes.
    try:
        price_snap = _get_last_price_snapshot(sym)
        underlying_px = float(price_snap.px) if price_snap and price_snap.px is not None else None
    except Exception:  # noqa: BLE001
        underlying_px = None

    strike_min = None
    strike_max = None
    if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
        window = max(float(strike_window_pct), 0.0)
        strike_min = underlying_px * (1.0 - window)
        strike_max = underlying_px * (1.0 + window)

    async def _get_json(session: aiohttp.ClientSession, path: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        url = f"{base_url}{path}"
        query = dict(params or {})
        query["apiKey"] = api_key
        await _POLYGON_OPTIONS_HTTP_SEM.acquire()
        try:
            async with _OPTIONS_HTTP_LOCK:
                global _OPTIONS_HTTP_ACTIVE, _OPTIONS_HTTP_PEAK
                _OPTIONS_HTTP_ACTIVE += 1
                if _OPTIONS_HTTP_ACTIVE > _OPTIONS_HTTP_PEAK:
                    _OPTIONS_HTTP_PEAK = _OPTIONS_HTTP_ACTIVE
            async with session.get(url, params=query) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        finally:
            try:
                async with _OPTIONS_HTTP_LOCK:
                    _OPTIONS_HTTP_ACTIVE = max(0, int(_OPTIONS_HTTP_ACTIVE) - 1)
            except Exception:
                pass
            try:
                _POLYGON_OPTIONS_HTTP_SEM.release()
            except Exception:
                pass

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        params: dict[str, Any] = {
            "underlying_ticker": underlying,
            "expiration_date": exp,
            "limit": int(max_contracts),
        }
        if strike_min is not None and strike_max is not None:
            # Polygon expects strike_price.gte / strike_price.lte
            params["strike_price.gte"] = f"{strike_min:.6f}"
            params["strike_price.lte"] = f"{strike_max:.6f}"

        contracts = await _get_json(session, "/v3/reference/options/contracts", params=params)
        if not contracts or contracts.get("status") != "OK":
            return None
        results = contracts.get("results") or []
        if not isinstance(results, list) or not results:
            return None

        tickers: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip()
            if not ticker:
                continue
            tickers.append(item)

        if not tickers:
            return None

        sem = asyncio.Semaphore(max(int(concurrency), 1))
        rows: list[dict[str, Any]] = []

        async def _one(contract: dict[str, Any]) -> None:
            ticker = str(contract.get("ticker") or "").strip()
            if not ticker:
                return
            async with sem:
                snap = await _get_json(session, f"/v3/snapshot/options/{underlying}/{ticker}")
            if not snap or snap.get("status") != "OK":
                return
            res = snap.get("results") if isinstance(snap.get("results"), dict) else {}
            greeks = res.get("greeks") if isinstance(res.get("greeks"), dict) else {}
            under = res.get("underlying_asset") if isinstance(res.get("underlying_asset"), dict) else {}
            day = res.get("day") if isinstance(res.get("day"), dict) else {}
            prev_day = res.get("prev_day")
            if not isinstance(prev_day, dict):
                prev_day = res.get("prevDay")
            prev_day = prev_day if isinstance(prev_day, dict) else {}

            rows.append(
                {
                    "symbol": ticker,
                    "strike": contract.get("strike_price"),
                    "type": str(contract.get("contract_type") or "").lower(),
                    "expiration": exp,
                    "bid": day.get("bid"),
                    "ask": day.get("ask"),
                    "last": day.get("close"),
                    "volume": day.get("volume"),
                    "prev_volume": prev_day.get("volume"),
                    "open_interest": res.get("open_interest"),
                    "iv": res.get("implied_volatility"),
                    "delta": greeks.get("delta"),
                    "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"),
                    "vega": greeks.get("vega"),
                    "underlying_price": under.get("price"),
                }
            )

        await asyncio.gather(*[_one(c) for c in tickers])

    if not rows:
        return None
    return pd.DataFrame(rows)


def _render_meta(render: RenderedPost) -> dict[str, Any]:
    if not isinstance(render, RenderedPost):  # pragma: no cover - defensive
        return {}
    payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    return meta if isinstance(meta, dict) else {}


def _render_is_limited(render: RenderedPost) -> bool:
    meta = _render_meta(render)
    missing = meta.get("missing_sections")
    if isinstance(missing, (list, tuple)) and missing:
        return True
    if bool(meta.get("stand_down")):
        return True
    return False


def _get_cached_analyze(symbol: str) -> Optional[_AnalyzeCacheEntry]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    entry = _ANALYZE_CACHE.get(sym)
    if not entry:
        return None

    if _ANALYZE_CACHE_TTL_SEC > 0:
        now = time_lib.time()
        if now - entry.ts > _ANALYZE_CACHE_TTL_SEC:
            _ANALYZE_CACHE.pop(sym, None)
            return None
    return entry


def _set_cached_analyze(symbol: str, render: RenderedPost, *, ts: Optional[float] = None) -> None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return

    existing = _ANALYZE_CACHE.get(sym)
    if existing and _render_is_limited(render) and not _render_is_limited(existing.render):
        timestamp = float(ts) if ts is not None else time_lib.time()
        existing.ts = timestamp
        return

    timestamp = float(ts) if ts is not None else time_lib.time()
    _ANALYZE_CACHE[sym] = _AnalyzeCacheEntry(render=render, ts=timestamp)

    if _ANALYZE_CACHE_MAX <= 0 or len(_ANALYZE_CACHE) <= _ANALYZE_CACHE_MAX:
        return

    # Drop the stalest entries first to bound memory.
    while len(_ANALYZE_CACHE) > _ANALYZE_CACHE_MAX:
        oldest_key = min(_ANALYZE_CACHE.items(), key=lambda item: item[1].ts)[0]
        _ANALYZE_CACHE.pop(oldest_key, None)


def _db_last_close(symbol: str, tf: str = "1m") -> tuple[Optional[float], Optional[datetime]]:
    sym = (symbol or "").upper()
    if not sym:
        return None, None

    db_path = Path(os.getenv("DB_PATH", "db/tnt.db"))
    if not db_path.exists():
        return None, None

    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT close, ts
                FROM prices
                WHERE symbol = ? AND tf = ?
                ORDER BY ts DESC
                LIMIT 1
                """,
                (sym, tf),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return None, None

    if not row:
        return None, None

    close, ts_raw = row
    try:
        price = float(close)
    except Exception:  # noqa: BLE001
        return None, None

    ts_et: Optional[datetime]
    if ts_raw is None:
        ts_et = None
    else:
        try:
            ts_et = parse_iso(str(ts_raw)).astimezone(ET)
        except Exception:  # noqa: BLE001
            ts_et = None

    return price, ts_et


def _fetch_polygon_snapshot_last_trade(sym: str) -> tuple[Optional[float], Optional[datetime]]:
    try:
        price, ts_iso, source = fetch_live_price(sym)
    except Exception:  # noqa: BLE001
        return None, None

    # Accept any Polygon-derived price that has a timestamp.
    # `fetch_live_price()` may fall back to aggs(1m).c or prevDay.c when `lastTrade` is missing
    # (common when markets are closed), and we still want a useful last price.
    if price is None or not ts_iso:
        return None, None

    try:
        ts = parse_iso(ts_iso).astimezone(ET)
    except Exception:  # noqa: BLE001
        return None, None

    try:
        return float(price), ts
    except Exception:  # noqa: BLE001
        return None, None


def _fetch_db_latest_1m_close(sym: str) -> tuple[Optional[float], Optional[datetime]]:
    return _db_last_close(sym, "1m")


def _fetch_db_latest_1d_close(sym: str) -> tuple[Optional[float], Optional[datetime]]:
    return _db_last_close(sym, "1d")


def _fetch_cached_analyze_price(sym: str) -> tuple[Optional[float], Optional[datetime]]:
    cached = _get_cached_analyze(sym)
    if not cached:
        return None, None

    last_price = _extract_last_price_from_bullets(cached.render.text)
    if last_price is None:
        return None, None

    try:
        ts = datetime.fromtimestamp(cached.ts, tz=timezone.utc).astimezone(ET)
    except Exception:  # noqa: BLE001
        ts = None

    try:
        price = float(last_price)
    except Exception:  # noqa: BLE001
        return None, None

    return price, ts


_LAST_PRICE_PROVIDER: Optional[Callable[[str], PriceSnapshot]] = None


def _set_last_price_provider(provider: Optional[Callable[[str], PriceSnapshot]]) -> None:
    global _LAST_PRICE_PROVIDER
    _LAST_PRICE_PROVIDER = provider


def _resolve_last_price(symbol: str, *, now_et: Optional[datetime] = None) -> PriceSnapshot:
    now_local = now_et or _now_et()
    return resolve_last_price_policy(
        symbol,
        now_et=now_local,
        fetch_polygon_snapshot_last_trade=_fetch_polygon_snapshot_last_trade,
        fetch_db_latest_1m_close=_fetch_db_latest_1m_close,
        fetch_db_latest_1d_close=_fetch_db_latest_1d_close,
        fetch_cached_analyze_price=_fetch_cached_analyze_price,
        allow_stale_when_closed=True,
        open_max_age_min=int(os.getenv("PRICE_MAX_AGE_OPEN_MIN", "10") or "10"),
        closed_max_age_days=int(os.getenv("PRICE_MAX_AGE_CLOSED_DAYS", "7") or "7"),
    )


def _default_last_price_provider(symbol: str) -> PriceSnapshot:
    return _resolve_last_price(symbol)


def _get_last_price_snapshot(symbol: str) -> PriceSnapshot:
    provider = _LAST_PRICE_PROVIDER or _default_last_price_provider
    try:
        result = provider(symbol)
    except Exception:  # noqa: BLE001
        return _resolve_last_price(symbol)
    if not isinstance(result, PriceSnapshot):
        return _resolve_last_price(symbol)
    return result


async def _fetch_on_demand_render(symbol: str, *, allow_cache: bool) -> tuple[RenderedPost, bool]:
    sym = (symbol or "").strip().upper()
    now = time_lib.time()
    if allow_cache and sym:
        cached = _get_cached_analyze(sym)
        if cached:
            return cached.render, True

    render = await asyncio.to_thread(build_on_demand_analyze_render, symbol)
    if sym:
        _set_cached_analyze(sym, render, ts=now)
    return render, False


def _format_last_price_section(
    symbols: Sequence[str],
    *,
    now_et: datetime,
    session: str,
    tf: str = "1m",
) -> str:
    def _session_state(session_code: str) -> str:
        mapping = {
            "RTH": "OPEN",
            "PRE": "CLOSED_PRE",
            "AH": "CLOSED_AFTER",
            "WEEKEND": "CLOSED_WEEKEND",
        }
        return mapping.get((session_code or "").upper(), "UNKNOWN")

    def _source_detail(source: str) -> str:
        return {
            "polygon_snapshot_lastTrade": "live snapshot",
            "db_1m_close": "last 1m close",
            "db_1d_close": "last daily close",
            "cached_analyze": "cached analyze",
            "none": "unavailable",
        }.get(source, source or "unknown")

    snapshots: list[tuple[str, PriceSnapshot]] = []
    for raw_sym in symbols:
        norm = str(raw_sym).upper() if raw_sym else "UNKNOWN"
        snapshots.append((norm, _get_last_price_snapshot(norm)))

    fallback_state = _session_state(session)
    primary = None
    for _, snap in snapshots:
        if snap.ok:
            primary = snap
            break
    if primary is None and snapshots:
        primary = snapshots[0][1]

    state_label = market_state_label(primary.market_state if primary else fallback_state)
    detail = _source_detail(primary.source if primary else "none")
    if primary and not primary.ok and primary.reason:
        detail = f"{detail}; {primary.reason.replace('_', ' ')}"

    lines: list[str] = [f"🧾 Data Mode: {state_label} ({detail})", "💵 **Last Price**"]

    for sym, snapshot in snapshots or [("UNKNOWN", PriceSnapshot("UNKNOWN", None, None, "none", fallback_state, None, False, "no_symbol"))]:
        if snapshot.price is None or snapshot.asof_et is None:
            reason = snapshot.reason or "price unavailable"
            lines.append(f"• {sym}: n/a ({reason.replace('_', ' ')})")
            continue

        block = format_last_price_block(snapshot)
        parts = block.splitlines()
        bullet_lines = parts[1:] if len(parts) > 1 else [parts[0]]
        for entry in bullet_lines:
            if entry.strip():
                lines.append(entry)

    return "\n".join(lines) + "\n"


# --- Intraday compact helpers (keep under 900 chars) ---


def _format_last_price_oneliner(
    symbol_list: list[str],
    *,
    last_prices: dict[str, float | None],
    stale_flags: dict[str, bool] | None = None,
) -> str:
    """Return a collapsed last-price summary for intraday updates."""

    stale_flags = stale_flags or {}
    parts: list[str] = []
    for sym in symbol_list:
        px = last_prices.get(sym)
        if px is None:
            continue
        try:
            parts.append(f"{sym} {float(px):.2f}")
        except Exception:  # noqa: BLE001
            continue

    if not parts:
        return "💵 Last Price: n/a"

    line = "💵 Last Price: " + " | ".join(parts)
    if any(stale_flags.get(sym, False) for sym in symbol_list):
        line += " (stale)"
    return line


def _format_levels_oneliner(
    *,
    spy_levels: dict[str, float | None],
    qqq_levels: dict[str, float | None] | None = None,
    max_len: int = 900,
    current_len: int = 0,
) -> str:
    """Render key levels on a single line, appending QQQ only if there is room."""

    def _fmt_triplet(prefix: str, lv: dict[str, float | None]) -> str:
        p_val = lv.get("P")
        r1_val = lv.get("R1")
        s1_val = lv.get("S1")

        def _fmt(x: float | None) -> str:
            try:
                return f"{float(x):.2f}" if x is not None else "n/a"
            except Exception:  # noqa: BLE001
                return "n/a"

        if p_val is None and r1_val is None and s1_val is None:
            return f"{prefix} n/a"
        return f"{prefix} P {_fmt(p_val)} / R1 {_fmt(r1_val)} / S1 {_fmt(s1_val)}"

    line = f"📐 Key Levels: {_fmt_triplet('SPY', spy_levels)}"
    if qqq_levels:
        candidate = f"{line} | {_fmt_triplet('QQQ', qqq_levels)}"
        if current_len + len(candidate) <= max_len:
            return candidate
    return line


def _load_smart_market_iq() -> Dict[str, Any]:
    """Load market participation snapshot without throwing."""
    try:
        if not SMART_MARKET_IQ_PATH.exists():
            return {"data_quality": "MISSING", "regime": "UNKNOWN"}
        raw = SMART_MARKET_IQ_PATH.read_text(encoding="utf-8")
        if not raw.strip():
            return {"data_quality": "MISSING", "regime": "UNKNOWN"}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"data_quality": "MISSING", "regime": "UNKNOWN"}
        return data
    except Exception:  # noqa: BLE001 - downstream logic handles missing data
        return {"data_quality": "MISSING", "regime": "UNKNOWN"}


def _resolve_market_participation(
    bias: Optional[str],
    edge: Optional[float],
) -> tuple[Dict[str, Any], ParticipationGateResult]:
    smiq = _load_smart_market_iq()
    regime = (smiq.get("regime") or "UNKNOWN") if isinstance(smiq, dict) else "UNKNOWN"
    quality = (smiq.get("data_quality") or "UNKNOWN") if isinstance(smiq, dict) else "UNKNOWN"
    pg = apply_participation_gate(
        bias=bias,
        model_edge=edge,
        participation_regime=str(regime).strip().upper(),
        participation_quality=str(quality).strip().upper(),
    )
    return smiq, pg


def _summarize_participation_caution(bias: Optional[str], pg: ParticipationGateResult) -> str:
    return "Breadth mixed; confirm bias before adds."


def _summarize_participation_conflict(pg: ParticipationGateResult) -> str:
    regime = getattr(pg, "regime", "UNKNOWN")
    if regime == "DEFENSIVE":
        return "Market participation conflict: defensive leadership vs bias."
    if regime == "RISK_ON":
        return "Market participation conflict: risk-on leadership vs bias."
    return "Market participation conflict: leadership flow vs bias."


def _gate_result_to_dict(pg: ParticipationGateResult) -> Dict[str, Any]:
    if hasattr(pg, "to_dict") and callable(getattr(pg, "to_dict")):
        try:
            return pg.to_dict()  # type: ignore[return-value]
        except Exception:  # noqa: BLE001 - fall back to attribute unpack below
            pass
    return {
        "regime": getattr(pg, "regime", "UNKNOWN"),
        "state": getattr(pg, "state", "UNKNOWN"),
        "impact": getattr(pg, "impact", "UNKNOWN"),
        "reason": getattr(pg, "reason", ""),
        "authority": getattr(pg, "authority", "market_participation_gate"),
        "data_quality": getattr(pg, "data_quality", "UNKNOWN"),
    }


def _format_participation_section(smiq: Dict[str, Any], pg: Dict[str, Any]) -> str:
    regime = (smiq.get("regime") or "UNKNOWN") if isinstance(smiq, dict) else "UNKNOWN"
    quality = (smiq.get("data_quality") or "UNKNOWN") if isinstance(smiq, dict) else "UNKNOWN"
    gainers = smiq.get("top_gainers") if isinstance(smiq, dict) else None
    losers = smiq.get("top_losers") if isinstance(smiq, dict) else None
    gainers_fmt = ", ".join(str(t).upper() for t in (gainers or [])[:5]) if gainers else "n/a"
    losers_fmt = ", ".join(str(t).upper() for t in (losers or [])[:5]) if losers else "n/a"

    impact = (pg.get("impact") if isinstance(pg, dict) else "UNKNOWN") or "UNKNOWN"
    state = (pg.get("state") if isinstance(pg, dict) else "UNKNOWN") or "UNKNOWN"

    gate_label = str(impact).strip().upper()
    if state and str(state).strip().upper() not in {"", "UNKNOWN"}:
        gate_label = f"{gate_label}"

    note = None
    if impact in ("CAUTION", "STAND_DOWN"):
        note = (pg.get("reason", "n/a") if isinstance(pg, dict) else "n/a")

    lines = [
        "📊 Market Participation (TNT)",
        f"• Regime: **{regime}** | Quality: **{quality}** | Gate: **{gate_label}**",
        f"• Leaders: {gainers_fmt}",
        f"• Laggards: {losers_fmt}",
        f"• Notes: {note or 'n/a'}",
    ]
    return "\n".join(lines) + "\n"


def vix_sqqq_confirmation_from_dirs(vix_dir: Optional[str], sqqq_dir: Optional[str]) -> str:
    """
    vix_dir / sqqq_dir expected: "up" / "down" / None
    """

    v = (vix_dir or "").lower().strip()
    q = (sqqq_dir or "").lower().strip()
    if v == "up" and q == "up":
        return "BEARISH"
    if v == "down" and q == "down":
        return "BULLISH"
    if v in ("up", "down") and q in ("up", "down"):
        return "MIXED"
    return "UNKNOWN"


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _pct_distance(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Percent distance between a and b, relative to a (in percent)."""

    if a is None or b is None:
        return None
    denom = abs(a)
    if denom <= 1e-9:
        return None
    return (abs(a - b) / denom) * 100.0


def tnt_flip_triggers(payload: Mapping[str, Any]) -> Dict[str, str]:
    """Return flip trigger lines (Bull/Bear/Neutral) based on pivots + confirmation."""

    pivot = _coerce_float(payload.get("pivot"))
    s1 = _coerce_float(payload.get("s1"))
    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper().strip()

    p_txt = fmt_money(pivot) if pivot is not None else "Pivot"
    s1_txt = fmt_money(s1) if s1 is not None else "S1"

    bull_confirm = "confirmation turns OK" if confirm != "BULLISH" else "confirmation OK"
    bear_confirm = "confirmation turns OK" if confirm != "BEARISH" else "confirmation OK"

    return {
        "bull": f"Bull: reclaim + hold above {p_txt} AND {bull_confirm}",
        "bear": f"Bear: break + hold below {s1_txt} AND {bear_confirm}",
        "neutral": "If still NEUTRAL/UNKNOWN: wait",
    }


def tnt_why_payload(payload: Mapping[str, Any], gate: Optional[Mapping[str, Any]] = None) -> Dict[str, object]:
    """Extract the 3–5 most important inputs that drove the TNT decision."""

    tnt = payload.get("tnt") if isinstance(payload.get("tnt"), dict) else {}

    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper().strip()
    vix_trend = str(payload.get("vix_trend") or "unknown")
    sqqq_dir = str(payload.get("sqqq_dir") or "unknown")

    edge = _coerce_float(payload.get("edge"))
    conviction = str(payload.get("conviction") or "UNKNOWN").upper().strip()

    pct_to_pivot = tnt.get("pct_to_pivot")
    pct_txt = "n/a"
    if isinstance(pct_to_pivot, (int, float)):
        pct_txt = f"{float(pct_to_pivot):.2f}%"

    tech_state = None
    dq = payload.get("data_quality") if isinstance(payload.get("data_quality"), dict) else None
    if isinstance(dq, dict):
        ts = dq.get("technical_state")
        if isinstance(ts, str) and ts:
            tech_state = ts.upper()
    if tech_state is None:
        tech_state = "UNKNOWN"

    gate_mode = None
    gate_reason = None
    if isinstance(gate, Mapping):
        gate_mode = str(gate.get("mode") or "").upper().strip() or None
        gate_reason = str(gate.get("reason") or "").strip() or None
    if not gate_mode:
        gate_mode = str(payload.get("gate_mode") or "").upper().strip() or "UNKNOWN"
    if not gate_reason:
        gate_reason = str(payload.get("gate_reason") or "").strip() or "n/a"

    edge_txt = "n/a" if edge is None else f"{edge:.3f}"

    return {
        "confirmation": {"state": confirm, "vix_trend": vix_trend, "sqqq_dir": sqqq_dir},
        "distance_to_pivot": pct_txt,
        "edge": edge_txt,
        "conviction": conviction,
        "vix_gate": {"mode": gate_mode, "reason": gate_reason},
        "freshness": tech_state,
        "tnt_reasons": list(tnt.get("reasons") or []),
    }


def tnt_no_trade_checklist(payload: Optional[Mapping[str, Any]] = None) -> str:
    """Short checklist used when regime blocks trades/playbooks."""

    flips = None
    if payload is not None:
        try:
            flips = tnt_flip_triggers(payload)
        except Exception:  # noqa: BLE001
            flips = None

    lines: list[str] = []
    lines.append("🛑 **No-Trade Checklist (TNT)**")
    lines.append("• Wait for confirmation to align (VIX + SQQQ)")
    lines.append("• Stop trading the first spike — require break + retest")
    lines.append("• Reduce size if near pivot / chop")
    lines.append("• Do nothing during macro event windows")
    if flips:
        lines.append("")
        lines.append("Flip Triggers:")
        lines.append(f"• {flips.get('bull')}")
        lines.append(f"• {flips.get('bear')}")
        lines.append(f"• {flips.get('neutral')}")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


_last_regime_audit_ts: Dict[str, int] = {}


def _maybe_append_regime_audit(payload: Mapping[str, Any], *, now_et: datetime) -> None:
    """Opt-in jsonl logging for future regime scoring/marketing."""

    if os.getenv("REGIME_AUDIT_ENABLED", "0").strip().lower() in {"0", "false", "off", "no"}:
        return

    sym = str(payload.get("symbol") or payload.get("display_symbol") or "").upper().strip()
    if not sym:
        sym = str(payload.get("trade_context_symbol") or "").upper().strip()
    if not sym:
        # fall back: do not log without a symbol
        return

    # Throttle per symbol (default 5m)
    throttle_sec = _env_int("REGIME_AUDIT_THROTTLE_SEC", "300")
    now_ts = int(now_et.timestamp())
    last = _last_regime_audit_ts.get(sym)
    if last is not None and (now_ts - last) < throttle_sec:
        return
    _last_regime_audit_ts[sym] = now_ts

    tnt = payload.get("tnt") if isinstance(payload.get("tnt"), dict) else {}

    row = {
        "ts_et": now_et.isoformat(),
        "symbol": sym,
        "tnt_regime": tnt.get("regime"),
        "tnt_posture": tnt.get("posture"),
        "tnt_confidence": tnt.get("confidence"),
        "pct_to_pivot": tnt.get("pct_to_pivot"),
        "bias": payload.get("bias"),
        "bias_confirm": payload.get("bias_confirm"),
        "edge": payload.get("edge"),
        "conviction": payload.get("conviction"),
        "vix_trend": payload.get("vix_trend"),
        "sqqq_dir": payload.get("sqqq_dir"),
        "vix_level": payload.get("vix_level"),
        "gate_mode": payload.get("gate_mode"),
        "price": payload.get("last_price"),
        "pivot": payload.get("pivot"),
        "s1": payload.get("s1"),
        "r1": payload.get("r1"),
        "technical_state": (payload.get("data_quality") or {}).get("technical_state") if isinstance(payload.get("data_quality"), dict) else None,
    }

    try:
        log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "regime_audit.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        return


def tnt_regime_assess(payload: Mapping[str, Any], *, now_et: Optional[datetime] = None) -> Dict[str, object]:
    """Rule-based TNT regime classifier used for automation + on-demand analysis.

    Output is stable, deterministic, and derived only from the payload.
    """

    # Inputs (best-effort)
    bias = str(payload.get("bias") or "NEUTRAL").upper().strip()
    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper().strip()
    session = str(payload.get("session") or "UNKNOWN").upper().strip()

    price = None
    try:
        if isinstance(payload.get("last_price"), (int, float)):
            price = float(payload["last_price"])
    except Exception:  # noqa: BLE001
        price = None

    pivot = None
    try:
        if isinstance(payload.get("pivot"), (int, float)):
            pivot = float(payload["pivot"])
    except Exception:  # noqa: BLE001
        pivot = None

    edge = None
    try:
        if isinstance(payload.get("edge"), (int, float)):
            edge = float(payload["edge"])
    except Exception:  # noqa: BLE001
        edge = None

    macro_risk = payload.get("macro_risk")
    macro_flag = bool(macro_risk)

    # Derived
    pct_to_pivot = _pct_distance(price, pivot)
    near_pivot = (pct_to_pivot is not None) and (pct_to_pivot <= 0.15)

    tech_state = None
    dq = payload.get("data_quality") if isinstance(payload.get("data_quality"), dict) else None
    if isinstance(dq, dict):
        ts = dq.get("technical_state")
        if isinstance(ts, str) and ts:
            tech_state = ts.upper()
    if tech_state is None:
        tech_state = "UNKNOWN"

    confirm_aligned = False
    if bias == "BULL" and confirm == "BULLISH":
        confirm_aligned = True
    elif bias == "BEAR" and confirm == "BEARISH":
        confirm_aligned = True

    edge_strong = (edge is not None) and (edge >= 0.065)
    edge_ok = (edge is not None) and (edge >= 0.055)

    reasons: list[str] = []
    if macro_flag:
        reasons.append("macro window risk")
    if confirm in {"UNKNOWN", "NEUTRAL"}:
        reasons.append(f"confirmation={confirm}")
    if near_pivot:
        reasons.append("near pivot")
    if pct_to_pivot is not None:
        reasons.append(f"pivot_dist={pct_to_pivot:.2f}%")
    if tech_state == "STALE":
        reasons.append("data stale")
    if edge is None:
        reasons.append("edge unavailable")
    elif edge_strong:
        reasons.append("edge strong")
    elif edge_ok:
        reasons.append("edge ok")
    else:
        reasons.append("edge weak")

    # Regime classification
    regime: str
    if macro_flag:
        regime = "DO_NOTHING"
    elif session in {"WEEKEND", "CLOSED"}:
        regime = "DO_NOTHING"
    elif confirm_aligned and edge_strong and (not near_pivot):
        regime = "TREND"
    elif near_pivot and confirm in {"UNKNOWN", "NEUTRAL"}:
        regime = "RANGE"
    else:
        regime = "TRANSITION"

    # Posture + permissions
    if regime == "TREND":
        posture = "STANDARD"
        permissions = {
            "trend_continuation": "ALLOW",
            "pullbacks": "ALLOW",
            "breakouts": "ALLOW",
            "mean_reversion": "AVOID",
            "countertrend": "AVOID",
            "size": "STANDARD",
        }
    elif regime == "RANGE":
        posture = "REDUCED"
        permissions = {
            "trend_continuation": "AVOID",
            "pullbacks": "AVOID",
            "breakouts": "AVOID",
            "mean_reversion": "ALLOW",
            "countertrend": "AVOID",
            "size": "SMALL",
        }
    elif regime == "TRANSITION":
        posture = "REDUCED"
        permissions = {
            "trend_continuation": "AVOID",
            "pullbacks": "ALLOW",
            "breakouts": "AVOID",
            "mean_reversion": "AVOID",
            "countertrend": "AVOID",
            "size": "SMALL",
        }
    else:  # DO_NOTHING
        posture = "STAND_DOWN"
        permissions = {
            "trend_continuation": "AVOID",
            "pullbacks": "AVOID",
            "breakouts": "AVOID",
            "mean_reversion": "AVOID",
            "countertrend": "AVOID",
            "size": "NONE",
        }

    confidence = 0.50
    if confirm_aligned:
        confidence += 0.20
    if edge_strong:
        confidence += 0.15
    elif edge_ok:
        confidence += 0.05
    if near_pivot:
        confidence -= 0.10
    if tech_state == "STALE":
        confidence -= 0.20
    if macro_flag:
        confidence -= 0.25
    confidence = _clamp01(confidence)

    if now_et is not None:
        try:
            ts_iso = now_et.isoformat()
        except Exception:  # noqa: BLE001
            ts_iso = None
    else:
        ts_iso = None

    return {
        "regime": regime,
        "posture": posture,
        "permissions": permissions,
        "reasons": reasons,
        "confidence": confidence,
        "pct_to_pivot": pct_to_pivot,
        "confirm_aligned": confirm_aligned,
        "asof_et": ts_iso,
    }


def tnt_regime_gate(payload: Mapping[str, Any], *, output_mode: str) -> tuple[bool, str]:
    """Return (ok, reason) for autopost/analyze execution."""

    mode = (output_mode or "strict").strip().lower()
    if mode not in {"strict", "insights"}:
        mode = "strict"

    tnt_ctx = payload.get("tnt") if isinstance(payload.get("tnt"), dict) else None
    if not tnt_ctx:
        # Fail-open for missing TNT fields; legacy behavior.
        return True, "tnt gate unavailable"

    regime = str(tnt_ctx.get("regime") or "UNKNOWN").upper()
    posture = str(tnt_ctx.get("posture") or "UNKNOWN").upper()

    if regime == "DO_NOTHING" or posture == "STAND_DOWN":
        return False, "TNT gate: STAND DOWN"

    if regime == "TRANSITION" and mode == "strict":
        return False, "TNT gate: TRANSITION (strict -> wait/reduce)"

    return True, "OK"


def fmt_last_price(symbol: str, last_price: Optional[float], ts_utc: Optional[str], tf: str = "1m") -> str:
    symbol = (symbol or "").upper()
    tf_label = f"{tf} close".strip() or "1m close"
    ts_clean = (ts_utc or "").strip()
    ts_display = "n/a"
    if ts_clean and ts_clean.lower() != "unknown":
        try:
            ts_dt = parse_iso(ts_clean)
            ts_display = ts_dt.astimezone(ET).strftime("%H:%M ET")
        except Exception:  # noqa: BLE001 - timestamp formatting must not fail
            ts_display = ts_clean

    if last_price is None:
        return f"💲 **Last Price:** **n/a** ({tf_label} | {ts_display})"

    try:
        price_val = float(last_price)
    except Exception:  # noqa: BLE001 - formatting must not fail
        return f"💲 **Last Price:** **n/a** ({tf_label} | {ts_display})"

    return f"💲 **Last Price:** **{price_val:.2f}** ({tf_label} | {ts_display})"


def _coerce_ai_string(value: Optional[object], *, max_len: int = 240) -> str:
    if value is None:
        return ""
    if level_ctx:
        lines.append(f"- Upside: {fmt_targets(level_ctx.get('upside_targets'))}")
        lines.append(f"- Downside: {fmt_targets(level_ctx.get('downside_targets'))}")

        if level_ctx.get("extension_mode") and last_price is not None:
            broken_levels = level_ctx.get("broken") if isinstance(level_ctx.get("broken"), list) else []
            try:
                price_val = float(last_price)
            except Exception:  # noqa: BLE001
                price_val = None

            if price_val is not None:
                pivot_regime = (payload.get("pivot_regime") or "").upper()
                if pivot_regime.startswith("ABOVE"):
                    broken_support = [item for item in broken_levels if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] < price_val]
                    if broken_support:
                        lines.append(f"- Support (broken): {fmt_targets(broken_support[:3])}")
                else:
                    broken_overhead = [item for item in broken_levels if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] > price_val]
                    if broken_overhead:
                        lines.append(f"- Overhead resistance (broken): {fmt_targets(broken_overhead[:3])}")
    else:
        lines.append("- Upside: n/a")
        lines.append("- Downside: n/a")
        lines.append("- (no level context available)")
        lines.append("")
        lines.append("🧠 How Pros Would Trade It:")
        lines.append("- Insufficient level data; defer to fresh signal or manual review")
        lines.append("")
        lines.append("🚫 Do Nothing If:")
        lines.append("- Levels unknown -> wait for updated data")
        lines.append("")
        lines.append("_Not financial advice._")
        return "\n".join(lines)

    if level_ctx:
        bull_targets = "-"  # keep placeholder if we craft plan below
    bull_targets_lines: list[str] = []
    bear_targets_lines: list[str] = []
    if level_ctx:
        bull_targets_lines.append(f"- Upside: {fmt_targets(level_ctx.get('upside_targets'))}")
        bear_targets_lines.append(f"- Downside: {fmt_targets(level_ctx.get('downside_targets'))}")

    lines.append("")
    lines.append("🧠 How Pros Would Trade It:")
    bull_plan = ["- Bull: enter on reclaim + hold above Pivot"]
    bear_plan = ["- Bear: enter on lose + hold below Pivot"]
    if level_ctx:
        bull_plan.append(f"- Targets: {fmt_targets(level_ctx.get('upside_targets'))}")
        bear_plan.append(f"- Targets: {fmt_targets(level_ctx.get('downside_targets'))}")
    else:
        bull_plan.append("- Targets: need updated pivots")
        bear_plan.append("- Targets: need updated pivots")
    bull_plan.append(f"- Invalidation: lose **{fmt_money(pivot)}**")
    bear_plan.append(f"- Invalidation: reclaim **{fmt_money(pivot)}**")
    lines.extend(bull_plan)
    lines.extend(bear_plan)
    symbol_norm = symbol.upper().strip()
    bias_norm = (bias or "NEUTRAL").upper().strip()
    conviction_norm = (conviction or "LOW").upper().strip()

    price_f = _f(last_price)
    P = _f(piv.get("P"))
    R1 = _f(piv.get("R1"))
    R2 = _f(piv.get("R2"))
    S1 = _f(piv.get("S1"))
    S2 = _f(piv.get("S2"))

    confirm = vix_sqqq_confirmation_from_dirs(vix_dir, sqqq_dir)

    regime = detect_regime(price_f, P, R1, R2, S1, S2)
    d_pivot = _delta(price_f, P) if P is not None else None
    if d_pivot is None:
        vs_pivot = "n/a"
    else:
        if d_pivot > 0:
            rel = "above"
        elif d_pivot < 0:
            rel = "below"
        else:
            rel = "at"
        vs_pivot = f"{d_pivot:+.2f} pts ({rel} P {_fmt(P)})"

    ladd = ladder_levels(price_f if price_f is not None else last_price, piv)
    up_targets = " → ".join([f"{k} {_fmt(v)}" for k, v in ladd["up"][:2]]) or "n/a"
    down_targets = " → ".join([f"{k} {_fmt(v)}" for k, v in ladd["down"][:2]]) or "n/a"

    if regime in ("BELOW_S1", "BELOW_P", "EXTENSION_DOWN"):
        primary = f"**Bear plan:** stay below **P {_fmt(P)}** → targets: {down_targets}"
        if S1 is not None and price_f is not None and price_f < S1:
            alternate = f"**Bull flip:** reclaim + hold **S1 {_fmt(S1)}** then **P {_fmt(P)}** → targets: {up_targets}"
        else:
            alternate = f"**Bull flip:** reclaim + hold **P {_fmt(P)}** → targets: {up_targets}"
    elif regime in ("ABOVE_P", "ABOVE_R1", "EXTENSION_UP"):
        primary = f"**Bull plan:** stay above **P {_fmt(P)}** → targets: {up_targets}"
        alternate = f"**Bear flip:** lose + hold below **P {_fmt(P)}** → targets: {down_targets}"
    else:
        primary = f"**Range plan:** trade edges only (S/R reactions). Targets: up {up_targets} | down {down_targets}"
        alternate = f"**Trend plan:** wait for break + retest of **P {_fmt(P)}** then follow direction."

    msg: List[str] = []
    msg.append(f"🚨 **{symbol_norm} — Trade Context**")
    msg.append(fmt_last_price(symbol_norm, last_price, last_price_ts_utc, last_price_tf))
    if P is not None:
        msg.append(f"📏 **vs Pivot:** {vs_pivot}")
    msg.append("")
    msg.append(
        f"**Bias:** **{bias_norm}** | **Confirmation:** **{confirm}** | **Regime:** **{regime}** | **Conviction:** **{conviction_norm}**"
    )
    msg.append("")
    msg.append("**Key Levels (RTH):**")
    msg.append(f"• P {_fmt(P)} | R1 {_fmt(R1)} | R2 {_fmt(R2)}")
    msg.append(f"• S1 {_fmt(S1)} | S2 {_fmt(S2)}")
    msg.append("")
    msg.append("**How Pros Would Trade It**")
    msg.append(f"• {primary}")
    msg.append(f"• {alternate}")
    msg.append("")
    msg.append("🚫 **Do Nothing If**")
    msg.append("• No break + retest confirmation (first spike only)")
    msg.append("• Choppy price around pivot (no direction)")
    msg.append("• Confirmation flips HARD against the plan")
    msg.append("")
    msg.append("_Not financial advice._")

    return "\n".join(msg)

import discord
from discord import app_commands
import httpx
import requests
from discord.ext import commands
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# Optional: deterministic "Concierge" templates (no LLM).
try:
    from tnt_concierge.engine import schedule_concierge_nudge
    from tnt_concierge import throttle as concierge_throttle
    from tnt_concierge import lock as decision_lock
    from tnt_concierge.templates import LockState as ConciergeLockState
except Exception:  # noqa: BLE001
    schedule_concierge_nudge = None  # type: ignore[assignment]
    concierge_throttle = None  # type: ignore[assignment]
    decision_lock = None  # type: ignore[assignment]
    ConciergeLockState = None  # type: ignore[assignment]

from delivery.state_builder import build_tnt_state as build_canonical_tnt_state
from delivery.tnt_llm import call_tnt_agent_async

from delivery.on_demand_data import (
    build_on_demand_analyze_render,
    fetch_live_price,
    is_fresh as data_is_fresh,
    symbol_supported_polygon,
)
from scripts import polygon_ingest

from zero_dte_pipeline.tech import build_agent_tech_package

# NOTE: Keep tests deterministic. Golden/smoke tests should not change based on a
# developer's local .env/.env.local.
if "pytest" not in sys.modules:
    for _env_path in (Path.cwd() / ".env.local", Path.cwd() / ".env"):
        try:
            if _env_path.exists():
                load_dotenv(_env_path, override=False)
        except Exception:  # noqa: BLE001
            pass

def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return int(default)


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)

def _positive_env_float(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except Exception:
        value = float(default)
    return value if value > 0 else float(default)

# Chart Concierge integration is implemented in tnt_concierge.engine/throttle.


QUALITY_GATE_ENABLED = os.getenv("QUALITY_GATE_ENABLED", "1") == "1"
QUALITY_GATE_STRICT = os.getenv("QUALITY_GATE_STRICT", "1") == "1"
QUALITY_GATE_FALLBACK_ENABLED = os.getenv("QUALITY_GATE_FALLBACK_ENABLED", "1") == "1"
QUALITY_GATE_LOG_FAILS = os.getenv("QUALITY_GATE_LOG_FAILS", "1") == "1"
QUALITY_GATE_DEBUG = os.getenv("DEBUG_GATE", "0") == "1"

STRICT_CONTRACTS = os.getenv("STRICT_CONTRACTS", "1") == "1"
DEV_ALLOW_VIOLATIONS = os.getenv("TNT_DEV_ALLOW_VIOLATIONS", "0") == "1"
AUTOPOST_AUDIT_ROOT = Path(os.getenv("AUTOPOST_AUDIT_ROOT", str(Path("logs") / "autopost_audit")))
AUTOPOST_CONTRACT_CONSTRAINTS = ContractConstraints(
    max_lines=DEFAULT_MAX_LINES,
    max_emoji=DEFAULT_MAX_EMOJI,
    max_chars_default=DEFAULT_MAX_CHARS_DEFAULT,
    max_chars_by_payload=dict(DEFAULT_MAX_CHARS_BY_PAYLOAD),
    forbidden_technical_terms=INSIGHTS_TECHNICAL_TERMS,
)

ASK_AUDIT_ROOT = AUTOPOST_AUDIT_ROOT / "ask"

AUTOPOST_AUDIT_RETENTION_DAYS = int(os.getenv("AUTOPOST_AUDIT_RETENTION_DAYS", "60") or "60")

PRICE_STALE_LIMIT_RTH = _positive_env_float("PRICE_STALE_LIMIT_RTH", "5")
PRICE_STALE_LIMIT_AH = _positive_env_float("PRICE_STALE_LIMIT_AH", "180")

_GIT_SHA_CACHE: Optional[str] = None
_LAST_AUDIT_PRUNE: Optional[date] = None


class ContractViolationError(RuntimeError):
    def __init__(self, label: str, violations: Sequence[str]):
        self.label = label
        self.violations = list(violations)
        message = f"{label} contract violations: {', '.join(self.violations[:5])}"
        super().__init__(message)


def _git_sha() -> str:
    global _GIT_SHA_CACHE
    if _GIT_SHA_CACHE is not None:
        return _GIT_SHA_CACHE
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        _GIT_SHA_CACHE = result.stdout.strip() or "unknown"
    except Exception:
        _GIT_SHA_CACHE = "unknown"
    return _GIT_SHA_CACHE


def _render_data_quality(render: RenderedPost) -> Optional[Any]:
    payload = render.agent_payload
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if isinstance(meta, dict):
        return meta.get("data_quality")
    return None


def _render_meta(render: RenderedPost) -> Dict[str, Any]:
    payload = render.agent_payload
    if isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict):
            return meta
    return {}


def _symbol_list_from_render(render: RenderedPost) -> list[str]:
    meta = _render_meta(render)
    symbols = meta.get("symbols")
    if isinstance(symbols, (list, tuple)):
        return [str(sym).upper() for sym in symbols if sym]
    return []


def _scenario_flags(render: RenderedPost) -> Dict[str, Any]:
    meta = _render_meta(render)
    flags: Dict[str, Any] = {}
    if "stand_down" in meta:
        flags["stand_down"] = bool(meta.get("stand_down"))
    futures_status = meta.get("futures_status")
    if isinstance(futures_status, str) and futures_status:
        flags["futures_status"] = futures_status
    dq = meta.get("data_quality")
    if isinstance(dq, dict):
        tech_state = dq.get("technical_state")
        if isinstance(tech_state, str) and tech_state:
            flags["tech_status"] = tech_state
        price_state = dq.get("price")
        if isinstance(price_state, str) and price_state:
            flags["price_status"] = price_state
    manual_flags = meta.get("scenario_flags")
    if isinstance(manual_flags, dict):
        for key, value in manual_flags.items():
            if isinstance(value, bool):
                if value:
                    flags[key] = True
            elif value is not None:
                flags[key] = value
    return flags


def _prune_autopost_audit(asof_et: datetime) -> None:
    global _LAST_AUDIT_PRUNE
    if AUTOPOST_AUDIT_RETENTION_DAYS <= 0:
        return

    today = asof_et.date()
    if _LAST_AUDIT_PRUNE == today:
        return

    AUTOPOST_AUDIT_ROOT.mkdir(parents=True, exist_ok=True)

    cutoff = today - timedelta(days=AUTOPOST_AUDIT_RETENTION_DAYS)
    for child in AUTOPOST_AUDIT_ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            child_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if child_date < cutoff:
            try:
                shutil.rmtree(child)
            except Exception:
                pass

    _LAST_AUDIT_PRUNE = today


def _contract_violations_for_render(render: RenderedPost, *, label: str) -> list[str]:
    violations = contract_violations(
        render.text,
        label=label,
        context=render.agent_payload,
        constraints=AUTOPOST_CONTRACT_CONSTRAINTS,
    )

    t = (render.text or "").lower()
    if label in {"focus_list", "intraday_update"}:
        if "smart market iq" in t:
            violations.append("LEGACY_SECTION:SMART_MARKET_IQ")
        if "options focus" in t:
            violations.append("LEGACY_SECTION:OPTIONS_FOCUS")
        if "iqfeed" in t:
            violations.append("LEGACY_VENDOR:IQFEED")
        if FOOTER_DISCLAIMER.lower() not in t:
            violations.append("MISSING_FOOTER_DISCLAIMER")

    if label == "intraday_update":
        if "🚦 permissions" not in t and "permissions:" not in t:
            violations.append("MISSING_PERMISSIONS_LINE")

    if label == "focus_list":
        if re.search(r"\b\d{2,6}\s*[cCpP]\b", render.text or ""):
            violations.append("FORBIDDEN_TEXT:OPTIONS_CONTRACT_PATTERN")

    return violations


PREFLIGHT_INTEGRITY_STANDDOWN_TEXT = (
    "Data integrity conflict detected (price/levels mismatch). TNT is in stand-down mode."
)


class PublishQueuedError(RuntimeError):
    def __init__(self, *, wait_seconds: float, due_ts: float, key: str):
        super().__init__(f"Queued for next window in ~{wait_seconds:.1f}s")
        self.wait_seconds = float(wait_seconds)
        self.due_ts = float(due_ts)
        self.key = str(key)


# --- Go-live cadence / rate limits (v1 defaults) ---
_RATE_LABEL_MIN_SEC = float(os.getenv("TNT_RATE_LABEL_MIN_SEC", "300") or "300")
_RATE_POSTURE_SYMBOL_MIN_SEC = float(os.getenv("TNT_RATE_POSTURE_SYMBOL_MIN_SEC", "1800") or "1800")
_RATE_CHARTS_MAX_PER_HOUR = int(os.getenv("TNT_RATE_CHARTS_MAX_PER_HOUR", "12") or "12")
_RATE_USER_CHART_MIN_SEC = float(os.getenv("TNT_RATE_USER_CHART_MIN_SEC", "600") or "600")
_RATE_USER_TEXT_MIN_SEC = float(os.getenv("TNT_RATE_USER_TEXT_MIN_SEC", "20") or "20")
_RATE_QUEUE_ENABLED = (os.getenv("TNT_RATE_QUEUE_ENABLED", "1") == "1")

_OPS_CHANNEL_ID = int(os.getenv("TNT_OPS_CHANNEL_ID", "0") or "0") or int(os.getenv("BOT_ALERT_CHANNEL_ID", "0") or "0")

_OPS_NOTICE_MIN_SEC = float(os.getenv("TNT_OPS_NOTICE_MIN_SEC", "900") or "900")
_OPS_NOTICE_QUEUE_MIN_SEC = float(os.getenv("TNT_OPS_NOTICE_QUEUE_MIN_SEC", "900") or "900")
_OPS_NOTICE_STALE_MIN_SEC = float(os.getenv("TNT_OPS_NOTICE_STALE_MIN_SEC", "900") or "900")

_OPS_NOTICE_LOCK: "asyncio.Lock" = asyncio.Lock()
_OPS_LAST_NOTICE_TS: dict[str, float] = {}

_PUBLISH_RATE_LOCK: "asyncio.Lock" = asyncio.Lock()

_LAST_BY_LABEL: dict[str, float] = {}
_LAST_POSTURE_BY_SYMBOL: dict[str, float] = {}
_CHART_SEND_TS: "deque[float]" = deque()
_LAST_USER_CHART_TS: dict[int, float] = {}
_LAST_USER_TEXT_TS: dict[int, float] = {}


@dataclass
class _QueuedPublish:
    due_ts: float
    created_ts: float
    key: str
    coro_factory: Any
    # For observability only
    label: str
    symbol: str


_QUEUE_HEAP: list[tuple[float, int, _QueuedPublish]] = []
_QUEUE_BY_KEY: dict[str, _QueuedPublish] = {}
_QUEUE_SEQ: int = 0
_QUEUE_TASK: Optional[asyncio.Task] = None


def _now_ts() -> float:
    return float(time_lib.time())


def _rate_group(label: str, request_kind: str) -> str:
    kind = (request_kind or "").strip().lower()
    label_norm = (label or "").strip().lower() or "unknown"
    if kind == "on_demand_chart":
        # Group by label so different commands/symbols don't block each other.
        return f"on_demand_chart:{label_norm}"
    if kind == "on_demand_text":
        # Group by label so different commands/symbols don't block each other.
        return f"on_demand_text:{label_norm}"
    return label_norm


def _looks_like_chart(render: "RenderedPost") -> bool:
    payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
    if isinstance(payload.get("chart_spec"), dict) or isinstance(payload.get("chart"), dict):
        return True
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return bool(meta.get("chart"))


def _is_posture_chart(label: str, render: "RenderedPost", request_kind: str) -> bool:
    kind = (request_kind or "").strip().lower()
    if kind == "on_demand_chart":
        return True
    return (label or "").strip().lower() in {"focus_list", "intraday_update"} or _looks_like_chart(render)


def _compute_rate_wait_seconds(
    *,
    label: str,
    symbol: str,
    render: "RenderedPost",
    requester_id: Optional[int],
    request_kind: str,
) -> tuple[float, list[str]]:
    now = _now_ts()
    reasons: list[str] = []
    wait = 0.0

    group = _rate_group(label, request_kind)
    label_norm = (label or "").strip().lower() or "unknown"
    kind_norm = (request_kind or "").strip().lower()

    # Let /ask be responsive: don't apply the global label_min_interval to on-demand coach answers.
    # Per-user text cooldown still applies via _RATE_USER_TEXT_MIN_SEC.
    if not (kind_norm == "on_demand_text" and label_norm == "ask"):
        last_label = _LAST_BY_LABEL.get(group)
        if last_label is not None and _RATE_LABEL_MIN_SEC > 0:
            delta = now - last_label
            if delta < _RATE_LABEL_MIN_SEC:
                wait = max(wait, _RATE_LABEL_MIN_SEC - delta)
                reasons.append(f"label_min_interval:{int(_RATE_LABEL_MIN_SEC)}s")

    is_posture = _is_posture_chart(label, render, request_kind)
    sym = (symbol or "").strip().upper()
    if is_posture and sym and _RATE_POSTURE_SYMBOL_MIN_SEC > 0:
        last_sym = _LAST_POSTURE_BY_SYMBOL.get(sym)
        if last_sym is not None:
            delta = now - last_sym
            if delta < _RATE_POSTURE_SYMBOL_MIN_SEC:
                wait = max(wait, _RATE_POSTURE_SYMBOL_MIN_SEC - delta)
                reasons.append(f"symbol_posture_min_interval:{int(_RATE_POSTURE_SYMBOL_MIN_SEC)}s")

    is_chart = _looks_like_chart(render) or (request_kind or "").strip().lower() == "on_demand_chart" or is_posture
    if is_chart and _RATE_CHARTS_MAX_PER_HOUR > 0:
        cutoff = now - 3600.0
        while _CHART_SEND_TS and _CHART_SEND_TS[0] < cutoff:
            _CHART_SEND_TS.popleft()
        if len(_CHART_SEND_TS) >= _RATE_CHARTS_MAX_PER_HOUR:
            oldest = _CHART_SEND_TS[0]
            wait = max(wait, (oldest + 3600.0) - now)
            reasons.append(f"charts_global_max_per_hour:{_RATE_CHARTS_MAX_PER_HOUR}")

    if requester_id is not None:
        uid = int(requester_id)
        kind = (request_kind or "").strip().lower()
        if kind == "on_demand_chart" and _RATE_USER_CHART_MIN_SEC > 0:
            last_u = _LAST_USER_CHART_TS.get(uid)
            if last_u is not None:
                delta = now - last_u
                if delta < _RATE_USER_CHART_MIN_SEC:
                    wait = max(wait, _RATE_USER_CHART_MIN_SEC - delta)
                    reasons.append(f"user_chart_min_interval:{int(_RATE_USER_CHART_MIN_SEC)}s")
        if kind == "on_demand_text" and _RATE_USER_TEXT_MIN_SEC > 0:
            last_t = _LAST_USER_TEXT_TS.get(uid)
            if last_t is not None:
                delta = now - last_t
                if delta < _RATE_USER_TEXT_MIN_SEC:
                    wait = max(wait, _RATE_USER_TEXT_MIN_SEC - delta)
                    reasons.append(f"user_text_min_interval:{int(_RATE_USER_TEXT_MIN_SEC)}s")

    return max(0.0, float(wait)), reasons


def _mark_rate_usage(*, label: str, symbol: str, render: "RenderedPost", requester_id: Optional[int], request_kind: str) -> None:
    now = _now_ts()
    group = _rate_group(label, request_kind)
    _LAST_BY_LABEL[group] = now

    if _is_posture_chart(label, render, request_kind):
        sym = (symbol or "").strip().upper()
        if sym:
            _LAST_POSTURE_BY_SYMBOL[sym] = now

    is_chart = _looks_like_chart(render) or (request_kind or "").strip().lower() == "on_demand_chart" or _is_posture_chart(label, render, request_kind)
    if is_chart:
        _CHART_SEND_TS.append(now)

    if requester_id is not None:
        uid = int(requester_id)
        kind = (request_kind or "").strip().lower()
        if kind == "on_demand_chart":
            _LAST_USER_CHART_TS[uid] = now
        elif kind == "on_demand_text":
            _LAST_USER_TEXT_TS[uid] = now


async def _notify_ops_quiet(*, client: Optional[object], message: str) -> None:
    if not message:
        return
    channel_id = int(_OPS_CHANNEL_ID or 0)
    if not channel_id or client is None:
        return
    try:
        ch = client.get_channel(channel_id)
        if ch is None:
            return
        await ch.send(message[:1800])
    except Exception:
        return


async def _notify_ops_throttled(*, client: Optional[object], key: str, message: str, min_sec: float) -> None:
    if not message:
        return
    if min_sec <= 0:
        await _notify_ops_quiet(client=client, message=message)
        return
    k = (key or "").strip().lower()
    if not k:
        await _notify_ops_quiet(client=client, message=message)
        return

    now = _now_ts()
    async with _OPS_NOTICE_LOCK:
        last = _OPS_LAST_NOTICE_TS.get(k)
        if last is not None and (now - last) < float(min_sec):
            return
        _OPS_LAST_NOTICE_TS[k] = now

    await _notify_ops_quiet(client=client, message=message)


def _ops_contract_versions(contracts: object) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(contracts, dict):
        return None, None
    agent_v = contracts.get("agent")
    chart_v = contracts.get("chart")
    agent_s = str(agent_v) if agent_v is not None else None
    chart_s = str(chart_v) if chart_v is not None else None
    return agent_s, chart_s


def _ops_short_hash(value: Optional[str], *, max_len: int = 12) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    return v[:max_len]


def _ops_relpath(path: Optional[Path]) -> str:
    if path is None:
        return "n/a"
    try:
        root = Path(__file__).resolve().parents[1]
        return path.resolve().relative_to(root).as_posix()
    except Exception:
        try:
            return path.as_posix()
        except Exception:
            return str(path)


def _extract_tnt_hashes_from_payload(agent_payload: object) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(agent_payload, dict):
        return None, None
    pkt = agent_payload.get("analysis_packet") or agent_payload.get("trade_context_packet")
    if not isinstance(pkt, dict):
        return None, None
    prompt_sha = pkt.get("tnt_prompt_sha256")
    state_sha = pkt.get("tnt_state_sha256")
    return (
        _ops_short_hash(prompt_sha),
        _ops_short_hash(state_sha),
    )


def _format_ops_event(
    *,
    tag: str,
    label: str,
    symbols: Sequence[str],
    status: str,
    contracts: Optional[dict] = None,
    prompt_sha256: Optional[str] = None,
    tnt_state_sha256: Optional[str] = None,
    violations: Optional[Sequence[str]] = None,
    audit_path: Optional[Path] = None,
) -> str:
    sym_csv = ",".join([s for s in (symbols or []) if isinstance(s, str) and s.strip()][:8]) or "n/a"
    agent_v, chart_v = _ops_contract_versions(contracts)
    parts: list[str] = []
    parts.append(f"[{tag}] label={label or 'n/a'} symbols={sym_csv} status={status or 'n/a'}")
    if agent_v:
        parts.append(f"v(agent)={agent_v}")
    if chart_v:
        parts.append(f"v(chart)={chart_v}")
    p_sha = _ops_short_hash(prompt_sha256)
    s_sha = _ops_short_hash(tnt_state_sha256)
    if p_sha:
        parts.append(f"prompt={p_sha}")
    if s_sha:
        parts.append(f"state={s_sha}")
    if violations:
        v = [str(x) for x in violations if x][:3]
        if v:
            parts.append("violations=" + ", ".join(v))
    parts.append(f"audit={_ops_relpath(audit_path)}")
    return " ".join(parts)[:1800]


def _queue_key(*, label: str, symbol: str, request_kind: str) -> str:
    group = _rate_group(label, request_kind)
    sym = (symbol or "").strip().upper() or ""
    kind = (request_kind or "").strip().lower()
    return f"{kind}:{group}:{sym}"


def start_burst_queue_loop() -> None:
    global _QUEUE_TASK
    if _QUEUE_TASK is not None and not _QUEUE_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _QUEUE_TASK = loop.create_task(_burst_queue_pump())


async def _burst_queue_pump() -> None:
    # Periodically attempt to flush queued publishes.
    while True:
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

        now = _now_ts()
        item: Optional[_QueuedPublish] = None
        seq = None

        async with _PUBLISH_RATE_LOCK:
            while _QUEUE_HEAP:
                due_ts, seq_val, candidate = _QUEUE_HEAP[0]
                if due_ts > now:
                    candidate = None
                    break
                heapq.heappop(_QUEUE_HEAP)
                # Skip stale/replaced entries
                current = _QUEUE_BY_KEY.get(candidate.key)
                if current is None or current is not candidate:
                    continue
                item = candidate
                seq = seq_val
                del _QUEUE_BY_KEY[candidate.key]
                break

        if item is None:
            continue

        try:
            await item.coro_factory()
        except Exception:
            # If it still fails, drop it silently; audit/ops will capture failures.
            continue


def _preflight_text_violations(text: str) -> list[str]:
    if not text:
        return []

    raw = text
    t = raw.lower()
    v: list[str] = []

    # Substring checks for multi-word / headings / vendor.
    forbidden_substrings: tuple[str, ...] = (
        "iqfeed",
        "smart market iq",
        "options focus",
        "take profit",
        "best strike",
        "best expiration",
        "best expiry",
    )
    for s in forbidden_substrings:
        if s and s in t:
            v.append(f"FORBIDDEN_TEXT:{s}")

    # Word-boundary checks to avoid false positives like "buyers" containing "buy".
    forbidden_words: tuple[str, ...] = (
        # execution
        "buy",
        "sell",
        "entry",
        "enter",
        "exit",
        "target",
        "stop",
        "tp",
        "sl",
        "long",
        "short",
        # options contract language
        "call",
        "put",
        "strike",
        "expiry",
        "expiration",
        "0dte",
        "dte",
    )
    for w in forbidden_words:
        if not w:
            continue
        if re.search(rf"\\b{re.escape(w)}\\b", raw, flags=re.IGNORECASE):
            v.append(f"FORBIDDEN_TEXT:{w}")

    # catches "4800c", "420p", "190 C", "190c"
    forbidden_contract_re = re.compile(r"\\b(\\d{2,6})\\s*([cCpP])\\b")
    if forbidden_contract_re.search(raw):
        v.append("FORBIDDEN_TEXT:OPTIONS_CONTRACT_PATTERN")

    return v


def _extract_chart_spec_from_context(context: object) -> Optional[Mapping[str, Any]]:
    if not isinstance(context, Mapping):
        return None
    direct = context.get("chart_spec")
    if isinstance(direct, Mapping):
        return direct
    sections = context.get("sections")
    if isinstance(sections, Mapping):
        candidate = sections.get("chart_spec")
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _preflight_chart_spec_violations(context: object) -> list[str]:
    if not isinstance(context, Mapping):
        return []

    chart = context.get("chart_spec")
    if not isinstance(chart, Mapping):
        chart = context.get("chart")
    if not isinstance(chart, Mapping):
        # ok: not every post has a chart
        return []

    # Keep the base contract check (structure) and add strict v1 whitelist validation.
    base_ok, base_msg = validate_chart_spec(chart)
    violations: list[str] = []
    if not base_ok:
        violations.append(f"CHART_SPEC:BASE_CONTRACT:{base_msg}")

    allowed_labels_by_template: dict[str, set[str]] = {
        "DECISION_ZONE": {
            "Support zone",
            "Resistance zone",
            "Pivot",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
            "Neutral posture inside range",
        },
        "NO_TRADE_COMPRESSION": {
            "No-trade zone",
            "Stand down (low expectancy)",
            "Momentum required",
        },
        "MOMENTUM_ACCEPTANCE": {
            "Acceptance band",
            "Wait for acceptance",
            "Aggression prohibited",
            "Momentum required",
            "Support zone",
            "Resistance zone",
        },
        "EVENT_RISK_OVERLAY": {
            "Event risk: structure unreliable",
            "Reduced participation",
            "Stand down (low expectancy)",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        },
        "POST_EVENT_REENGAGEMENT": {
            "Wait for acceptance",
            "Reduced participation",
            "Support zone",
            "Resistance zone",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        },
    }

    template_required_fields: dict[str, list[str]] = {
        "DECISION_ZONE": ["timeframe", "zones", "gates"],
        "NO_TRADE_COMPRESSION": ["timeframe", "no_trade_box", "breakout_triggers"],
        "MOMENTUM_ACCEPTANCE": ["timeframe", "acceptance_band"],
        "EVENT_RISK_OVERLAY": ["timeframe", "event", "gates", "behavior_banner"],
        "POST_EVENT_REENGAGEMENT": ["timeframe", "event_marker", "post_event_structure", "gates"],
    }

    hard_caps = {
        "max_lines": 5,
        "max_zones": 3,
        "max_labels": 8,
        "max_gates": 2,
    }

    forbidden_label_tokens: tuple[str, ...] = (
        "buy",
        "sell",
        "long",
        "short",
        "entry",
        "enter",
        "exit",
        "target",
        "stop",
        "take profit",
        "tp",
        "sl",
        "call",
        "put",
        "strike",
        "expiry",
        "expiration",
        "0dte",
        "dte",
        "p&l",
        "profit",
        "gain",
        "win",
        "loss",
        "guarantee",
        "rsi",
        "macd",
        "ema",
        "sma",
        "vwap",
        "bollinger",
        "stochastic",
    )

    contract_re = re.compile(r"\\b(\\d{2,6})\\s*([cCpP])\\b")

    template = chart.get("template")
    if template not in allowed_labels_by_template:
        violations.append("CHART_SPEC:UNKNOWN_TEMPLATE")
        return violations

    for f in template_required_fields.get(str(template), []):
        if f not in chart or chart.get(f) in (None, "", []):
            violations.append(f"CHART_SPEC:MISSING_FIELD:{f}")

    labels: list[str] = []

    def _collect_label(x: object) -> None:
        if isinstance(x, Mapping):
            lab = x.get("label")
            if isinstance(lab, str) and lab.strip():
                labels.append(lab.strip())

    for z in (chart.get("zones") or []) if isinstance(chart.get("zones"), list) else []:
        _collect_label(z)

    gates = chart.get("gates") or {}
    if isinstance(gates, Mapping):
        for g in gates.values():
            _collect_label(g)

    _collect_label(chart.get("no_trade_box") or {})
    for trg in (chart.get("breakout_triggers") or []) if isinstance(chart.get("breakout_triggers"), list) else []:
        _collect_label(trg)
    _collect_label(chart.get("acceptance_band") or {})
    _collect_label(chart.get("event") or {})
    _collect_label(chart.get("event_marker") or {})
    for s in (chart.get("post_event_structure") or []) if isinstance(chart.get("post_event_structure"), list) else []:
        _collect_label(s)
    for s in (chart.get("structure") or []) if isinstance(chart.get("structure"), list) else []:
        _collect_label(s)
    for p in (chart.get("prohibitions") or []) if isinstance(chart.get("prohibitions"), list) else []:
        _collect_label(p)

    if len(labels) > hard_caps["max_labels"]:
        violations.append("CHART_SPEC:TOO_MANY_LABELS")

    allowed = allowed_labels_by_template[str(template)]
    for lab in labels:
        low = lab.lower()

        if contract_re.search(lab):
            violations.append("CHART_SPEC:FORBIDDEN_OPTIONS_CONTRACT_PATTERN")

        for tok in forbidden_label_tokens:
            if tok and tok in low:
                violations.append(f"CHART_SPEC:FORBIDDEN_LABEL_TOKEN:{tok}")

        if lab not in allowed:
            violations.append(f"CHART_SPEC:INVALID_LABEL:{lab}")

    if isinstance(gates, Mapping) and len(gates) > hard_caps["max_gates"]:
        violations.append("CHART_SPEC:TOO_MANY_GATES")

    zone_count = 0
    if isinstance(chart.get("zones"), list):
        zone_count += len(chart.get("zones") or [])
    if chart.get("no_trade_box"):
        zone_count += 1
    if chart.get("acceptance_band"):
        zone_count += 1
    if isinstance(chart.get("post_event_structure"), list):
        zone_count += len(chart.get("post_event_structure") or [])
    if zone_count > hard_caps["max_zones"]:
        violations.append("CHART_SPEC:TOO_MANY_ZONES")

    line_count = 0
    if isinstance(gates, Mapping):
        line_count += len(gates)
    if isinstance(chart.get("optional_lines"), list):
        line_count += len(chart.get("optional_lines") or [])
    if isinstance(chart.get("breakout_triggers"), list):
        line_count += len(chart.get("breakout_triggers") or [])
    if line_count > hard_caps["max_lines"]:
        violations.append("CHART_SPEC:TOO_MANY_LINES")

    if template == "MOMENTUM_ACCEPTANCE":
        if ("Wait for acceptance" not in labels) and ("Aggression prohibited" not in labels):
            violations.append("CHART_SPEC:MOMENTUM_ACCEPTANCE_MISSING_DIRECTIVE")

    return violations


def _preflight_price_levels_conflict(symbols: Sequence[str]) -> tuple[bool, str]:
    """Detect symbol/scale mismatch between last price and pivot levels."""

    for sym in [str(s).upper() for s in (symbols or []) if s]:
        snap = _get_last_price_snapshot(sym)
        last_px = getattr(snap, "px", None)
        dp = get_latest_daily_pivots(sym)
        pivot = None
        if dp and isinstance(dp.get("piv"), dict):
            pivot = dp["piv"].get("P")

        try:
            lp = float(last_px) if last_px is not None else None
            pv = float(pivot) if pivot is not None else None
        except Exception:  # noqa: BLE001
            lp, pv = None, None

        if lp is None or pv is None or lp <= 0 or pv <= 0:
            continue

        rel = abs(lp - pv) / max(lp, pv)
        if rel > 0.20 and abs(lp - pv) > 10.0:
            detail = f"{sym} last={lp:.2f} pivot={pv:.2f}"
            return True, detail

    return False, "ok"


def _build_contract_violation_fallback(label: str, violations: Sequence[str]) -> str:
    lines = [
        "⚠️ **AutoPost Stand-Down — Data Incomplete**",
        f"Contract: {AUTOPOST_CONTRACT_VERSION} | Builder: {label}",
        "",
        "Blocked because:",
    ]
    for reason in violations[:3]:
        lines.append(f"• {reason}")
    if len(violations) > 3:
        lines.append(f"• (+{len(violations) - 3} more)")
    lines.extend(
        [
            "",
            "No automated briefing will publish until inputs refresh.",
        ]
    )
    return "\n".join(lines)


def _write_autopost_audit(
    *,
    render: RenderedPost,
    builder: str,
    label: str,
    channel,
    asof_et: datetime,
    status: str,
    symbol: str,
    context_mode: str,
    context_output_mode: str,
    violations: Sequence[str],
    message_id: Optional[int] = None,
    latency_ms: Optional[float] = None,
    fallback_text: Optional[str] = None,
) -> Path:
    _prune_autopost_audit(asof_et)
    channel_id = getattr(channel, "id", "unknown")
    try:
        channel_str = str(int(channel_id))
    except Exception:
        channel_str = str(channel_id)
    asof_token = asof_et.strftime("%H%M%S")
    text_hash = hashlib.sha1(render.text.encode("utf-8", "ignore")).hexdigest()[:10]
    date_dir = AUTOPOST_AUDIT_ROOT / asof_et.strftime("%Y-%m-%d")
    filename = f"{label}_{channel_str}_{asof_token}_{text_hash}.json"
    date_dir.mkdir(parents=True, exist_ok=True)
    symbol_list = _symbol_list_from_render(render)
    scenario_flags = _scenario_flags(render)
    latency_value = round(latency_ms, 2) if latency_ms is not None else None
    data_quality = _render_data_quality(render)
    metadata: Dict[str, Any] = {
        "symbol_list": symbol_list,
        "scenario_flags": scenario_flags,
        "data_quality": data_quality,
    }

    contracts = {
        "agent": AUTOPOST_CONTRACT_VERSION,
        "chart": "1.0",
    }
    metadata["contracts"] = contracts
    msg_id_serialized: Optional[int]
    try:
        msg_id_serialized = int(message_id) if message_id is not None else None
    except Exception:
        msg_id_serialized = None
    agent_payload = render.agent_payload
    if isinstance(agent_payload, dict):
        agent_payload = dict(agent_payload)
        meta_dict = agent_payload.get("meta")
        if not isinstance(meta_dict, dict):
            meta_dict = {}
        meta_dict = dict(meta_dict)
        meta_dict.setdefault("contracts", contracts)
        agent_payload["meta"] = meta_dict

        # Optional: propagate deterministic TNT_STATE hash into audit metadata.
        pkt = agent_payload.get("analysis_packet") or agent_payload.get("trade_context_packet")
        if isinstance(pkt, dict):
            tnt_sha = pkt.get("tnt_state_sha256")
            if isinstance(tnt_sha, str) and tnt_sha.strip():
                metadata["tnt_state_sha256"] = tnt_sha.strip()
            prompt_sha = pkt.get("tnt_prompt_sha256")
            if isinstance(prompt_sha, str) and prompt_sha.strip():
                metadata["tnt_prompt_sha256"] = prompt_sha.strip()

    payload: Dict[str, Any] = {
        "builder": builder,
        "post_type": label,
        "channel": {
            "id": channel_id,
            "name": getattr(channel, "name", None),
        },
        "channel_id": channel_str,
        "message_id": msg_id_serialized,
        "symbol": symbol,
        "symbol_list": symbol_list,
        "trade_context_mode": context_mode,
        "trade_context_output_mode": context_output_mode,
        "asof_et": asof_et.isoformat(),
        "git_sha": _git_sha(),
        "contract_version": AUTOPOST_CONTRACT_VERSION,
        "strict_contracts": STRICT_CONTRACTS,
        "status": status,
        "violations": list(violations),
        "data_quality": data_quality,
        "scenario_flags": scenario_flags,
        "latency_ms": latency_value,
        "short_hash": text_hash,
        "text": render.text,
        "agent_payload": agent_payload,
        "metadata": metadata,
    }
    # Legacy fields retained temporarily for downstream compatibility.
    payload["analysis_mode"] = context_mode
    payload["output_mode"] = context_output_mode
    if fallback_text is not None:
        payload["fallback_text"] = fallback_text
    audit_path = date_dir / filename
    with audit_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return audit_path


def _write_ask_audit(
    *,
    question: str,
    symbol: str,
    status: str,
    cache_hit: bool,
    coach_text: str,
    render: RenderedPost,
    channel,
    latency_ms: float,
    message_id: Optional[int],
) -> Path:
    asof_et = _now_et()
    ASK_AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    date_dir = ASK_AUDIT_ROOT / asof_et.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    try:
        channel_id_raw = getattr(channel, "id", None)
    except Exception:
        channel_id_raw = None
    try:
        channel_id_int = int(channel_id_raw) if channel_id_raw is not None else None
    except Exception:
        channel_id_int = None

    coach_hash = hashlib.sha1(coach_text.encode("utf-8", "ignore")).hexdigest()[:10]
    analyze_hash = hashlib.sha1(render.text.encode("utf-8", "ignore")).hexdigest()[:10]
    asof_token = asof_et.strftime("%H%M%S")
    filename = f"ask_{symbol.upper()}_{asof_token}_{coach_hash}.json"
    try:
        message_id_value = int(message_id) if message_id is not None else None
    except Exception:
        message_id_value = message_id

    payload: Dict[str, Any] = {
        "command": "ask",
        "question": question,
        "symbol": symbol.upper(),
        "status": status,
        "cache_hit": cache_hit,
        "latency_ms": round(float(latency_ms), 2),
        "asof_et": asof_et.isoformat(),
        "channel_id": channel_id_int,
        "trade_context_short_hash": analyze_hash,
        "trade_context_text": render.text,
        "trade_context_payload": render.agent_payload,
        "coach_short_hash": coach_hash,
        "coach_text": coach_text,
        "message_id": message_id_value,
        "git_sha": _git_sha(),
    }
    payload["analysis_short_hash"] = analyze_hash
    payload["analysis_text"] = render.text
    payload["analysis_payload"] = render.agent_payload

    # Optional: stable TNT_STATE fingerprint for replay.
    try:
        tnt_state = build_tnt_state_from_analysis_payload(payload.get("analysis_payload") or {}).state
        payload["tnt_state_sha256"] = hashlib.sha256(
            json.dumps(tnt_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    except Exception:  # noqa: BLE001
        payload["tnt_state_sha256"] = None

    audit_path = date_dir / filename
    with audit_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    return audit_path


async def _publish_autopost_render(
    channel,
    render: RenderedPost,
    *,
    builder: str,
    label: str,
    symbol: str,
    requester_id: Optional[int] = None,
    request_kind: str = "autopost",
    client: Optional[object] = None,
    queue_mode: str = "silent",
    _from_queue: bool = False,
    context_mode: Optional[str] = None,
    context_output_mode: Optional[str] = None,
    strict_contracts: Optional[bool] = None,
    allow_contract_violations: bool = False,
    analysis_mode: Optional[str] = None,
    output_mode: Optional[str] = None,
) -> None:
    start_ts = time_lib.perf_counter()
    asof_et = _now_et()
    if strict_contracts is None:
        strict_contracts = STRICT_CONTRACTS

    # Production-safe defaults: only allow relaxing contracts in explicit dev runs.
    if strict_contracts is False and not DEV_ALLOW_VIOLATIONS:
        strict_contracts = True
    if allow_contract_violations and not DEV_ALLOW_VIOLATIONS:
        allow_contract_violations = False

    if context_mode is None:
        context_mode = (analysis_mode or "db").strip().lower() if isinstance(analysis_mode, str) else "db"
    if context_mode not in {"db", "on_demand"}:
        context_mode = "db"

    if context_output_mode is None:
        context_output_mode = (output_mode or "strict").strip().lower() if isinstance(output_mode, str) else "strict"
    if context_output_mode not in VALID_MODES:
        context_output_mode = "strict"

    # If channel is None (unit tests / dry-runs), do not apply cadence/queue.
    if channel is not None:
        # Kick off the queue pump when the publish choke-point is first used.
        start_burst_queue_loop()

    if not _from_queue and channel is not None:
        async with _PUBLISH_RATE_LOCK:
            wait_s, reasons = _compute_rate_wait_seconds(
                label=label,
                symbol=symbol,
                render=render,
                requester_id=requester_id,
                request_kind=request_kind,
            )
            if wait_s > 0.001:
                key = _queue_key(label=label, symbol=symbol, request_kind=request_kind)
                due_ts = _now_ts() + wait_s
                if _RATE_QUEUE_ENABLED:
                    global _QUEUE_SEQ
                    qp = _QueuedPublish(
                        due_ts=due_ts,
                        created_ts=_now_ts(),
                        key=key,
                        coro_factory=lambda: _publish_autopost_render(
                            channel,
                            render,
                            builder=builder,
                            label=label,
                            symbol=symbol,
                            requester_id=requester_id,
                            request_kind=request_kind,
                            client=client,
                            queue_mode=queue_mode,
                            _from_queue=True,
                            context_mode=context_mode,
                            context_output_mode=context_output_mode,
                            strict_contracts=strict_contracts,
                            allow_contract_violations=allow_contract_violations,
                            analysis_mode=analysis_mode,
                            output_mode=output_mode,
                        ),
                        label=label,
                        symbol=symbol,
                    )
                    _QUEUE_SEQ += 1
                    _QUEUE_BY_KEY[key] = qp
                    heapq.heappush(_QUEUE_HEAP, (qp.due_ts, _QUEUE_SEQ, qp))

                    audit_path: Optional[Path] = None

                    try:
                        elapsed_ms = (time_lib.perf_counter() - start_ts) * 1000.0
                        audit_path = _write_autopost_audit(
                            render=render,
                            builder=builder,
                            label=label,
                            channel=channel,
                            asof_et=asof_et,
                            status="queued_rate_limit",
                            symbol=symbol,
                            context_mode=context_mode,
                            context_output_mode=context_output_mode,
                            violations=[f"RATE_LIMIT:{r}" for r in reasons] or ["RATE_LIMIT"],
                            message_id=None,
                            latency_ms=elapsed_ms,
                            fallback_text=None,
                        )
                    except Exception:
                        pass

                    try:
                        payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
                        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                        contracts = meta.get("contracts") if isinstance(meta, dict) else None
                        prompt_sha, state_sha = _extract_tnt_hashes_from_payload(payload)
                        msg = _format_ops_event(
                            tag="queued",
                            label=label,
                            symbols=_symbol_list_from_render(render) or ([symbol.upper()] if symbol else []),
                            status="queued_rate_limit",
                            contracts=contracts if isinstance(contracts, dict) else None,
                            prompt_sha256=prompt_sha,
                            tnt_state_sha256=state_sha,
                            violations=[f"RATE_LIMIT:{r}" for r in reasons] or ["RATE_LIMIT"],
                            audit_path=audit_path,
                        )
                        await _notify_ops_throttled(
                            client=(client or bot),
                            key=f"queued_rate_limit:{(label or 'n/a').strip().lower()}:{(symbol or 'n/a').strip().upper()}",
                            message=msg,
                            min_sec=_OPS_NOTICE_QUEUE_MIN_SEC,
                        )
                    except Exception:
                        pass

                    if (queue_mode or "").lower() == "raise":
                        raise PublishQueuedError(wait_seconds=wait_s, due_ts=due_ts, key=key)
                    return

                if (queue_mode or "").lower() == "raise":
                    raise PublishQueuedError(wait_seconds=wait_s, due_ts=due_ts, key=key)
                return

    symbols_for_preflight = _symbol_list_from_render(render) or ([symbol.upper()] if symbol else [])

    integrity_conflict, integrity_detail = _preflight_price_levels_conflict(symbols_for_preflight)
    if integrity_conflict:
        # Non-negotiable: post only the mandated stand-down sentence.
        stand_text = PREFLIGHT_INTEGRITY_STANDDOWN_TEXT
        payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        meta = dict(meta)
        meta.update(
            {
                "stand_down": True,
                "stand_reason": stand_text,
                "data_health": "DEGRADED",
                "data_integrity_conflict": True,
                "data_integrity_detail": integrity_detail,
            }
        )
        new_payload = dict(payload)
        new_payload["meta"] = meta
        stand_render = RenderedPost(text=stand_text, agent_payload=new_payload)

        status = "stand_down_integrity_posted"
        message = None
        audit_path = None
        try:
            message = await safe_send(
                channel,
                stand_render.text,
                kind="status",
                symbol=symbol,
                pivots=None,
                analysis_mode=context_mode,
                output_mode=context_output_mode,
                label=label,
                files=stand_render.files,
            )
            _mark_rate_usage(
                label=label,
                symbol=symbol,
                render=stand_render,
                requester_id=requester_id,
                request_kind=request_kind,
            )
        except Exception:
            status = "stand_down_integrity_send_failed"
            raise
        finally:
            elapsed_ms = (time_lib.perf_counter() - start_ts) * 1000.0
            audit_path = _write_autopost_audit(
                render=stand_render,
                builder=builder,
                label=label,
                channel=channel,
                asof_et=asof_et,
                status=status,
                symbol=symbol,
                context_mode=context_mode,
                context_output_mode=context_output_mode,
                violations=["DATA_INTEGRITY_CONFLICT"],
                message_id=getattr(message, "id", None),
                latency_ms=elapsed_ms,
                fallback_text=None,
            )
            contracts = (new_payload.get("meta") or {}).get("contracts") if isinstance(new_payload.get("meta"), dict) else None
            contract_str = None
            if isinstance(contracts, dict):
                contract_str = ", ".join(f"{k}:{v}" for k, v in contracts.items())
            ops_msg = (
                _format_ops_event(
                    tag="stand_down",
                    label=label,
                    symbols=symbols_for_preflight or ([symbol.upper()] if symbol else []),
                    status="DATA_INTEGRITY_CONFLICT",
                    contracts=contracts if isinstance(contracts, dict) else None,
                    prompt_sha256=None,
                    tnt_state_sha256=None,
                    violations=["DATA_INTEGRITY_CONFLICT"],
                    audit_path=audit_path,
                )
            )
            await _notify_ops_quiet(client=(client or bot), message=ops_msg)
        return

    violations = []
    violations.extend(_contract_violations_for_render(render, label=label))
    violations.extend(_preflight_text_violations(render.text))
    violations.extend(_preflight_chart_spec_violations(render.agent_payload))
    # De-dup while preserving order
    deduped: list[str] = []
    seen: set[str] = set()
    for v in violations:
        if not v or v in seen:
            continue
        seen.add(v)
        deduped.append(v)
    violations = deduped
    if violations:
        if strict_contracts:
            elapsed_ms = (time_lib.perf_counter() - start_ts) * 1000.0
            audit_path = _write_autopost_audit(
                render=render,
                builder=builder,
                label=label,
                channel=channel,
                asof_et=asof_et,
                status="blocked",
                symbol=symbol,
                context_mode=context_mode,
                context_output_mode=context_output_mode,
                violations=violations,
                message_id=None,
                latency_ms=elapsed_ms,
                fallback_text=None,
            )
            contracts = None
            payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            if isinstance(meta, dict):
                contracts = meta.get("contracts")
            contract_str = None
            if isinstance(contracts, dict):
                contract_str = ", ".join(f"{k}:{v}" for k, v in contracts.items())
            ops_msg = (
                _format_ops_event(
                    tag="blocked",
                    label=label,
                    symbols=_symbol_list_from_render(render) or ([symbol.upper()] if symbol else []),
                    status="blocked",
                    contracts=contracts if isinstance(contracts, dict) else None,
                    prompt_sha256=None,
                    tnt_state_sha256=None,
                    violations=violations,
                    audit_path=audit_path,
                )
            )
            await _notify_ops_quiet(client=(client or bot), message=ops_msg)
            raise ContractViolationError(label, violations)

        if allow_contract_violations:
            status = "posted_with_violations"
            message = None
            try:
                message = await safe_send(
                    channel,
                    render.text,
                    kind="analysis",
                    symbol=symbol,
                    pivots=None,
                    analysis_mode=context_mode,
                    output_mode=context_output_mode,
                    label=label,
                    files=render.files,
                )
            except Exception:
                status = "send_failed"
                raise
            finally:
                elapsed_ms = (time_lib.perf_counter() - start_ts) * 1000.0
                _write_autopost_audit(
                    render=render,
                    builder=builder,
                    label=label,
                    channel=channel,
                    asof_et=asof_et,
                    status=status,
                    symbol=symbol,
                    context_mode=context_mode,
                    context_output_mode=context_output_mode,
                    violations=violations,
                    message_id=getattr(message, "id", None),
                    latency_ms=elapsed_ms,
                    fallback_text=None,
                )
            return

        fallback_text = _build_contract_violation_fallback(label, violations)
        status = "fallback_posted"
        message = None
        try:
            message = await safe_send(
                channel,
                fallback_text,
                kind="status",
                symbol=symbol,
                pivots=None,
                analysis_mode=context_mode,
                output_mode=context_output_mode,
                label=label,
                files=render.files,
            )
        except Exception:
            status = "fallback_send_failed"
            raise
        finally:
            elapsed_ms = (time_lib.perf_counter() - start_ts) * 1000.0
            _write_autopost_audit(
                render=render,
                builder=builder,
                label=label,
                channel=channel,
                asof_et=asof_et,
                status=status,
                symbol=symbol,
                context_mode=context_mode,
                context_output_mode=context_output_mode,
                violations=violations,
                message_id=getattr(message, "id", None),
                latency_ms=elapsed_ms,
                fallback_text=fallback_text,
            )
        return

    status = "posted"
    message = None
    try:
        message = await safe_send(
            channel,
            render.text,
            kind="analysis",
            symbol=symbol,
            pivots=None,
            analysis_mode=context_mode,
            output_mode=context_output_mode,
            label=label,
            files=render.files,
        )
        _mark_rate_usage(
            label=label,
            symbol=symbol,
            render=render,
            requester_id=requester_id,
            request_kind=request_kind,
        )
    except Exception:
        status = "send_failed"
        raise
    finally:
        elapsed_ms = (time_lib.perf_counter() - start_ts) * 1000.0
        _write_autopost_audit(
            render=render,
            builder=builder,
            label=label,
            channel=channel,
            asof_et=asof_et,
            status=status,
            symbol=symbol,
            context_mode=context_mode,
            context_output_mode=context_output_mode,
            violations=[],
            message_id=getattr(message, "id", None),
            latency_ms=elapsed_ms,
            fallback_text=None,
        )


def _trade_context_meta_from_render(
    render: RenderedPost,
    default_symbol: str,
) -> tuple[str, str, str, Dict[str, Any]]:
    payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

    symbol_val = payload.get("symbol") if isinstance(payload.get("symbol"), str) else None
    if not symbol_val:
        symbols_meta = meta.get("symbols") if isinstance(meta, dict) else None
        if isinstance(symbols_meta, (list, tuple)) and symbols_meta:
            symbol_val = str(symbols_meta[0]).upper()
    if not symbol_val:
        symbol_val = (default_symbol or "").strip().upper() or "N/A"

    context_mode = "api"
    if isinstance(meta, dict):
        raw_mode = meta.get("trade_context_mode") or meta.get("analysis_mode") or "api"
        context_mode = str(raw_mode)
    context_output_mode = "strict"
    if isinstance(meta, dict):
        raw_output = meta.get("trade_context_output_mode") or meta.get("analysis_output_mode") or "strict"
        context_output_mode = str(raw_output)

    return symbol_val.upper() or "N/A", context_mode, context_output_mode, meta if isinstance(meta, dict) else {}


_analysis_meta_from_render = _trade_context_meta_from_render


REQUIRED_SECTIONS = [
    "— Trade Context",
    "Key Levels",
    "How Pros Would Trade It",
    "Market Confirmation",
    "Do Nothing If",
]

INSIGHTS_REQUIRED = [
    "— Trade Context",
    "Last Price",
    "Key Levels",
    "How Pros Would Trade It",
    "Do Nothing If",
    "_Not financial advice._",
]

BANNED_SUBSTRINGS = [
    "[[",
    "]]",
    "{{",
    "}}",
    "todo",
    "tbd",
    "???",
    "fill me",
    "insert",
    "lorem ipsum",
]

WATCHLIST_EXTRA_COLUMNS = {
    "last_ready_ts": "TEXT",
    "last_attempt_ts": "TEXT",
    "last_error": "TEXT",
}

BOOTSTRAP_BACKFILL_DAYS = int(os.getenv("BOOTSTRAP_BACKFILL_DAYS", "3") or "3")

_STARTUP_DEFAULT = "SPY,QQQ,IWM"
STARTUP_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("STARTUP_SYMBOLS", _STARTUP_DEFAULT).split(",")
    if sym.strip()
]
if not STARTUP_SYMBOLS:
    STARTUP_SYMBOLS = [sym.strip().upper() for sym in _STARTUP_DEFAULT.split(",") if sym.strip()]
_STARTUP_SYMBOL_SET = set(STARTUP_SYMBOLS)

DEFAULT_DAILY_PREP_SYMBOLS = [sym for sym in STARTUP_SYMBOLS if sym in {"SPY", "QQQ"}]
if not DEFAULT_DAILY_PREP_SYMBOLS:
    DEFAULT_DAILY_PREP_SYMBOLS = ["SPY", "QQQ"]


def _filter_startup_symbols(symbols: Iterable[str]) -> list[str]:
    if not _STARTUP_SYMBOL_SET:
        return [str(sym).upper() for sym in symbols if sym]
    filtered: list[str] = []
    for sym in symbols:
        if not sym:
            continue
        upper = str(sym).upper()
        if upper in _STARTUP_SYMBOL_SET:
            filtered.append(upper)
    return filtered


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def _extract_first_float(pattern: str, text: str) -> Optional[float]:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _normalize_section_token(value: str) -> str:
    cleaned = (value or "").lower()
    cleaned = re.sub(r"\*\*", "", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _extract_last_price_from_bullets(text: str) -> Optional[float]:
    lines = text.splitlines()
    header_idx: Optional[int] = None
    for idx, raw_line in enumerate(lines):
        if "last price" in raw_line.lower():
            header_idx = idx
            break

    if header_idx is None:
        return None

    for raw_line in lines[header_idx + 1 :]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("•"):
            # Accept both:
            #   • SPY: 123.45 (...)
            #   • SPY 123.45 (...)
            match = re.search(r"(?::\s*|\s+)\*{0,2}([0-9]+(?:\.[0-9]+)?)", stripped)
            if match:
                try:
                    return float(match.group(1))
                except Exception:  # noqa: BLE001
                    return None
            continue
        break

    return None


def validate_analysis_message(text: str, label: Optional[str] = None) -> Tuple[bool, str]:
    """Validate outbound Discord content for Trade Context or legacy formats."""

    stripped = (text or "").strip()
    if not stripped:
        return False, "empty"

    canonical_silence = SILENCE_NOTICE.strip()
    if stripped.replace("\r", "").startswith(canonical_silence.replace("\r", "")):
        return True, "silence"

    # On-demand stand-down messages are deterministic and are allowed to post even if
    # data sections (pivots/levels) are unavailable.
    if stripped.lstrip().upper().startswith("⚠️ STAND DOWN"):
        return True, "stand_down"

    is_trade_context = "TNT TRADE CONTEXT" in stripped
    if is_trade_context:
        required_tokens = [
            "🧠 TNT TRADE CONTEXT",
            "Market State:",
            "Risk Bias:",
            "Psychological Risk:",
            "TRADE ENVELOPE",
            "✅ Allowed:",
            "⚠️ Caution:",
            "⛔ Avoid:",
            "DEFAULT ACTION:",
        ]
        for token in required_tokens:
            if token not in text:
                return False, f"missing token: {token}"
        return True, "OK"

    if len(stripped) < 60:
        return False, "too short"

    label_lower = (label or "").lower()
    on_demand = label_lower.startswith("analyze")

    if on_demand:
        # On-demand renders are deterministic and may use different headings.
        # Do NOT require an exact "Last Price" section header; instead require that
        # we can parse a last price and a pivot.
        required_tokens: list[str] = []
    else:
        required_tokens = [
            "Last Price",
            "Key Levels",
            "How Pros Would Trade It",
            "Do Nothing If",
        ]

    lowered = text.lower()
    normalized_text = _normalize_section_token(text)

    def _has_token(token: str) -> bool:
        token_norm = _normalize_section_token(token)
        return bool(token_norm and token_norm in normalized_text)

    banned_words = ["guaranteed", "sure thing", "100% win", "insider", "front-run"]
    for token in banned_words:
        if token in lowered:
            return False, f"banned:{token}"
    for token in required_tokens:
        if not _has_token(token):
            return False, f"Missing section: {token}"

    if on_demand:
        # Accept: "Pivots", "Key Levels", or deterministic "Reference Levels" block.
        if not (_has_token("Pivots") or _has_token("Key Levels") or _has_token("Reference Levels")):
            return False, "Missing section: Levels"

        # Accept: legacy trade-context blocks OR on-demand "Coach Reminder".
        if not (_has_token("Trade Context") or _has_token("TNT STATUS") or _has_token("Coach Reminder")):
            return False, "Missing section: Context"

    last_px = _extract_first_float(r"(?:Last Price|Last price):\s*\*{0,2}\s*([0-9]+(?:\.[0-9]+)?)", text)
    if last_px is None:
        last_px = _extract_last_price_from_bullets(text)
    pivot = _extract_first_float(r"\bP(?:ivot)?:\s*\*{0,2}\s*([0-9]+(?:\.[0-9]+)?)", text) or _extract_first_float(
        r"\bP\s*([0-9]+(?:\.[0-9]+)?)",
        text,
    )

    if last_px is None:
        return False, "Missing last price"
    if pivot is None:
        return False, "Missing pivot"

    s1 = _extract_first_float(r"\bS1\s*([0-9]+(?:\.[0-9]+)?)", text)
    s2 = _extract_first_float(r"\bS2\s*([0-9]+(?:\.[0-9]+)?)", text)
    r1 = _extract_first_float(r"\bR1\s*([0-9]+(?:\.[0-9]+)?)", text)
    r2 = _extract_first_float(r"\bR2\s*([0-9]+(?:\.[0-9]+)?)", text)

    if s1 is not None and last_px < s1:
        if re.search(r"Downside:.*S1", text, flags=re.IGNORECASE):
            return False, "Invalid downside targets: price already below S1"

    if r1 is not None and last_px > r1:
        if re.search(r"Upside:.*R1", text, flags=re.IGNORECASE):
            return False, "Invalid upside targets: price already above R1"

    bias = (
        "BULL"
        if re.search(r"\bBias:\s*\*\*BULL", text, re.IGNORECASE)
        else "BEAR"
        if re.search(r"\bBias:\s*\*\*BEAR", text, re.IGNORECASE)
        else "NEUTRAL"
    )

    confirm = (
        "BULLISH"
        if re.search(r"\bConfirmation:\s*\*\*BULLISH", text, re.IGNORECASE)
        else "BEARISH"
        if re.search(r"\bConfirmation:\s*\*\*BEARISH", text, re.IGNORECASE)
        else "MIXED"
        if re.search(r"\bConfirmation:\s*\*\*MIXED", text, re.IGNORECASE)
        else "UNKNOWN"
    )

    if bias == "BULL" and confirm == "BEARISH":
        return False, "Contradiction: Bias=BULL but Confirmation=BEARISH"
    if bias == "BEAR" and confirm == "BULLISH":
        return False, "Contradiction: Bias=BEAR but Confirmation=BULLISH"

    return True, "OK"


def validate_output(text: str, *, mode: str) -> tuple[bool, str]:
    # Route known modes through the same validator, but with a label that
    # matches the expected format. This prevents legacy requirements (like a
    # literal "Last Price" header) from blocking on-demand formats.
    label = "analyze" if (mode or "").lower().startswith("on_demand") else None
    return validate_analysis_message(text, label=label)


def _extract_first_number_after(label: str, text: str) -> Optional[float]:
    """Find the first float-like number after the provided label."""

    match = re.search(rf"{re.escape(label)}\s*[: ]\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:  # noqa: BLE001
        return None


def build_safe_fallback(symbol: str, pivots: Optional[dict], note: str = "") -> str:
    sym_raw = (symbol or "").strip().upper()
    sym_norm = _normalize_symbol_token(sym_raw) if sym_raw else None
    sym_key = sym_norm or "".join(ch for ch in sym_raw if ch.isalnum())
    sym_display = sym_key or sym_raw or "N/A"

    def _fmt_pivot(val: Optional[object]) -> str:
        try:
            return f"{float(val):.2f}"
        except Exception:  # noqa: BLE001
            return "n/a"

    # Best-effort enrichment so standby messages still show something useful.
    try:
        snap = _get_last_price_snapshot(sym_key)
    except Exception:  # noqa: BLE001
        snap = None

    last_price_line = "• n/a"
    if snap is not None and getattr(snap, "price", None) is not None and getattr(snap, "asof_et", None) is not None:
        try:
            px = float(getattr(snap, "price"))
            asof_et = getattr(snap, "asof_et")
            asof_str = asof_et.strftime("%Y-%m-%d %H:%M ET") if hasattr(asof_et, "strftime") else "n/a"
            src = getattr(snap, "source", "n/a")
            state = market_state_label(getattr(snap, "market_state", "UNKNOWN"))
            last_price_line = f"• {sym_display}: {px:.2f} ({src} | {asof_str} | {state})"
        except Exception:  # noqa: BLE001
            last_price_line = "• n/a"

    pivots_dict = pivots if isinstance(pivots, dict) else {}
    if not pivots_dict:
        try:
            piv_info = get_latest_daily_pivots(sym_key)
        except Exception:  # noqa: BLE001
            piv_info = None
        if isinstance(piv_info, dict) and isinstance(piv_info.get("piv"), dict):
            pivots_dict = piv_info["piv"]  # type: ignore[assignment]

    pivot_line = _fmt_pivot(pivots_dict.get("P")) if pivots_dict else "n/a"

    rth_line = None
    try:
        sess = get_last_rth_session_hlc(sym_key)
    except Exception:  # noqa: BLE001
        sess = None
    if sess:
        try:
            session_date, high, low, close, _last_ts = sess
            rth_line = f"• Last RTH: H {float(high):.2f} | L {float(low):.2f} | C {float(close):.2f} ({session_date})"
        except Exception:  # noqa: BLE001
            rth_line = None

    details = (note or "").replace("_", " ").strip()

    lines: list[str] = []
    lines.append(f"🔍 On-Demand Analyze — {sym_display} (Standby)")
    lines.append("")
    if snap is not None and getattr(snap, "market_state", None):
        try:
            state = market_state_label(getattr(snap, "market_state", "UNKNOWN"))
        except Exception:  # noqa: BLE001
            state = "UNKNOWN"
        if getattr(snap, "ok", False):
            lines.append(f"🧾 Data Mode: {state} (snapshot)")
        else:
            reason = getattr(snap, "reason", None) or "no recent validated price available"
            lines.append(f"🧾 Data Mode: {state} — {reason}")
    else:
        lines.append("🧾 Data Mode: CLOSED — no recent validated price available")
    if details:
        lines.append(f"• Detail: {details}")
    lines.append("")
    lines.append("💵 **Last Price**")
    lines.append(last_price_line)
    lines.append("")
    lines.append("📐 Reference Levels (last confirmed session)")
    lines.append(f"• Pivot: {pivot_line}")
    if rth_line:
        lines.append(rth_line)
    lines.append("")
    lines.append("🧭 Coach Reminder")
    # If the market is open and we have a validated snapshot, this fallback is almost
    # always a formatting/contract issue (not "live pricing is down").
    try:
        is_open = snap is not None and getattr(snap, "ok", False) and market_state_label(getattr(snap, "market_state", "UNKNOWN")) == "OPEN"
    except Exception:  # noqa: BLE001
        is_open = False

    if is_open:
        lines.append("• Market is live — re-run /analyze now for actionable triggers")
        lines.append("• Use Pivot reclaim/loss as the first decision filter")
        lines.append("• This standby card means the full analysis contract failed, not that pricing is paused")
    else:
        lines.append("• Re-run /analyze at the open for actionable triggers")
        lines.append("• Watch for reclaim or loss of the primary pivot before taking risk")
        lines.append("• Treat this as informational only until live pricing resumes")
    lines.append("")
    lines.append("_Not financial advice._")

    return "\n".join(lines)


def build_deterministic_analysis(
    sym: str,
    last_price: Optional[float],
    last_ts: Optional[str],
    pivots: Optional[dict],
) -> str:
    """Produce a static analysis stub using only deterministic data."""

    symbol = (sym or "N/A").strip().upper() or "N/A"
    price_text = "n/a" if last_price is None else f"{last_price:.2f}"
    ts_text = last_ts or "n/a"

    pivot_line = "(no daily pivots yet)"
    if isinstance(pivots, dict) and pivots:
        parts: list[str] = []
        for key in ("P", "R1", "S1", "R2", "S2"):
            val = pivots.get(key)
            if val is None:
                parts.append(f"{key} n/a")
                continue
            try:
                parts.append(f"{key} {float(val):.2f}")
            except Exception:  # noqa: BLE001
                parts.append(f"{key} n/a")
        if parts:
            pivot_line = " | ".join(parts)

    lines = [
        f"📊 **{symbol} Analysis**",
        f"Last price: {price_text} ({ts_text})",
        "",
        "⏱ Timeframes:",
        "• Execution: 5m",
        "• Structure: 60m",
        "• Context: 1D",
        "",
        "Prev RTH pivots:",
        f"• {pivot_line}",
        "",
        "📐 Key Levels:",
        "• Use pivots above for immediate reference.",
        "",
        "🧭 Regime:",
        "• Needs fresh 1m data + signals to classify.",
        "",
        "🧠 How Pros Would Trade It",
        "• Above Pivot tilt bullish toward R1.",
        "• Below Pivot tilt bearish toward S1.",
        "",
        "🚫 Do Nothing If:",
        "• Structure or data is stale; wait for fresh signal confirmation.",
        "",
        "_Not financial advice._",
    ]
    return "\n".join(lines)


def fmt_money(x):
    try:
        return f"{float(x):.2f}"
    except Exception:  # noqa: BLE001 - formatting must never raise
        return str(x)


def fmt_signed(x):
    try:
        val = float(x)
    except Exception:  # noqa: BLE001
        return str(x)
    sign = "-" if val < 0 else ""
    return f"{sign}{abs(val):.2f}"


_EMA_WITH_NUMBER_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])ema\s*(\d+)")
_EMA_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])ema(?![A-Za-z0-9_])")


def _normalize_indicator_label(name: object) -> str:
    raw = str(name)

    def _number_repl(match: re.Match[str]) -> str:
        digits = match.group(1)
        return f"avg {digits}"

    cleaned = _EMA_WITH_NUMBER_RE.sub(_number_repl, raw)
    cleaned = _EMA_TOKEN_RE.sub("avg", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def fmt_targets(pairs) -> str:
    if not pairs:
        return "n/a"

    parts: list[str] = []
    for item in pairs:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        name, val = item
        try:
            label = _normalize_indicator_label(name)
            parts.append(f"{label} {fmt_money(val)}")
        except Exception:  # noqa: BLE001
            continue

    return " → ".join(parts) if parts else "n/a"


def _extract_technical_state(source: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(source, Mapping):
        return None

    candidates: list[Optional[object]] = []
    direct = source.get("data_quality")
    if direct is not None:
        candidates.append(direct)
    meta = source.get("meta")
    if isinstance(meta, Mapping):
        candidates.append(meta.get("data_quality"))

    for candidate in candidates:
        if isinstance(candidate, Mapping):
            state = candidate.get("technical_state")
            if isinstance(state, str):
                return state.upper()
        elif isinstance(candidate, str):
            return candidate.upper()

    return None


ACTION_LABELS = {
    "TRADE_SELECTIVELY": "TRADE SELECTIVELY",
    "WAIT": "WAIT",
    "STAND_DOWN": "STAND DOWN",
}

ACTION_EMOJI = {
    "TRADE_SELECTIVELY": "🟢",
    "WAIT": "🟡",
    "STAND_DOWN": "🔴",
}


@dataclass(frozen=True)
class DefaultActionDecision:
    code: str
    emoji: str
    label: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TradeContextSummary:
    symbol: str
    decision: DefaultActionDecision
    market_state: str
    risk_bias: str
    psychological_risks: tuple[str, ...]
    allowed: tuple[str, ...]
    caution: tuple[str, ...]
    avoid: tuple[str, ...]
    silence: bool
    silence_reasons: tuple[str, ...]
    generated_et: Optional[datetime]


def _technical_state_from_payload(payload: Mapping[str, Any]) -> str:
    source = payload.get("data_quality") if isinstance(payload, Mapping) else None
    if isinstance(source, Mapping):
        raw = source.get("technical_state")
        if isinstance(raw, Mapping):
            raw = raw.get("technical_state")
        if isinstance(raw, str):
            return raw.upper()
    return "UNKNOWN"


def _edge_value(payload: Mapping[str, Any]) -> Optional[float]:
    try:
        edge_val = payload.get("edge")
        return float(edge_val) if edge_val is not None else None
    except Exception:  # noqa: BLE001
        return None


def _gate_mode(payload: Mapping[str, Any], gate: Optional[Mapping[str, Any]]) -> str:
    if isinstance(gate, Mapping):
        mode = gate.get("mode")
        if isinstance(mode, str) and mode:
            return mode.upper()
    mode = payload.get("gate_mode")
    if isinstance(mode, str) and mode:
        return mode.upper()
    gate_payload = payload.get("gate")
    if isinstance(gate_payload, Mapping):
        mode = gate_payload.get("mode")
        if isinstance(mode, str) and mode:
            return mode.upper()
    return ""


def _bias_conflict(bias: str, confirm: str) -> bool:
    combo = (bias.upper(), confirm.upper())
    return combo in {("BULL", "BEARISH"), ("BEAR", "BULLISH")}


def _has_range_regime(payload: Mapping[str, Any]) -> bool:
    for key in ("pivot_regime", "regime"):
        raw = payload.get(key)
        if isinstance(raw, str):
            text = raw.upper()
            if any(term in text for term in ("RANGE", "BALANCE", "NEUTRAL")):
                return True
    return False


def _trend_tag(payload: Mapping[str, Any]) -> bool:
    if payload.get("pivot_extension_mode"):
        return True
    strength = payload.get("trend_strength")
    try:
        return float(strength) >= 0.6
    except Exception:  # noqa: BLE001
        return False


def _determine_default_action(
    payload: Mapping[str, Any],
    *,
    gate: Optional[Mapping[str, Any]],
    price_snapshot: Optional[PriceSnapshot],
    macro_risk: Optional[Mapping[str, Any]],
    session: str,
) -> DefaultActionDecision:
    tech_state = _technical_state_from_payload(payload)
    gate_mode = _gate_mode(payload, gate)
    edge = _edge_value(payload) or 0.0
    bias = str(payload.get("bias") or "NEUTRAL").upper()
    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper()
    conviction = str(payload.get("conviction") or "LOW").upper()

    stand_down_tags: list[str] = []
    if tech_state not in {"FRESH", "OK"}:
        stand_down_tags.append("data_stale")
    if isinstance(price_snapshot, PriceSnapshot) and not price_snapshot.ok:
        stand_down_tags.append("price_invalid")
    if gate_mode == "HARD":
        stand_down_tags.append("vol_spike")
    if bool(payload.get("stand_down")):
        stand_down_tags.append("model_stand_down")
    if isinstance(macro_risk, Mapping):
        delta = macro_risk.get("delta_min")
        try:
            if float(delta) <= 30:
                stand_down_tags.append("macro_event_imminent")
        except Exception:  # noqa: BLE001
            pass

    if stand_down_tags:
        code = "STAND_DOWN"
        return DefaultActionDecision(
            code=code,
            emoji=ACTION_EMOJI[code],
            label=ACTION_LABELS[code],
            reasons=tuple(stand_down_tags),
        )

    wait_tags: list[str] = []
    if _bias_conflict(bias, confirm):
        wait_tags.append("conflict")
    if edge < 0.05:
        wait_tags.append("low_edge")
    if conviction != "HIGH":
        wait_tags.append("conviction")
    if gate_mode in {"WATCH", "SOFT", "LIGHT"}:
        wait_tags.append("gate_watch")
    if isinstance(macro_risk, Mapping):
        delta = macro_risk.get("delta_min")
        try:
            if 30 < float(delta) <= 60:
                wait_tags.append("macro_watch")
        except Exception:  # noqa: BLE001
            pass
    if _has_range_regime(payload):
        wait_tags.append("range")
    if session in {"PRE", "AH"}:
        wait_tags.append("offhours")

    trend_active = _trend_tag(payload)

    if not wait_tags and conviction == "HIGH" and edge >= 0.08 and gate_mode in {"", "OK"}:
        reasons = ["trend" if trend_active else "structured"]
        code = "TRADE_SELECTIVELY"
        return DefaultActionDecision(
            code=code,
            emoji=ACTION_EMOJI[code],
            label=ACTION_LABELS[code],
            reasons=tuple(reasons),
        )

    if not wait_tags:
        wait_tags.append("default_wait")

    code = "WAIT"
    return DefaultActionDecision(
        code=code,
        emoji=ACTION_EMOJI[code],
        label=ACTION_LABELS[code],
        reasons=tuple(wait_tags),
    )


def _describe_market_state(
    decision: DefaultActionDecision,
    payload: Mapping[str, Any],
    macro_risk: Optional[Mapping[str, Any]],
) -> str:
    reasons = set(decision.reasons)
    if decision.code == "STAND_DOWN":
        if "macro_event_imminent" in reasons:
            return "High-impact event window; liquidity unstable"
        if "vol_spike" in reasons:
            return "Volatility regime shift in progress; no stable context yet"
        if "data_stale" in reasons or "price_invalid" in reasons:
            return "Market data incomplete; waiting for refresh"
        return "Market context unstable; stand down"

    if decision.code == "WAIT":
        if "range" in reasons:
            return "Volatility compressed, range-bound tape"
        if "conflict" in reasons:
            return "Signals mixed; no clean momentum path"
        if "macro_watch" in reasons and isinstance(macro_risk, Mapping):
            return "Awaiting catalyst; expansion trigger pending"
        if "offhours" in reasons:
            return "Off-hours tape; wait for liquidity rebuild"
        return "Volatility muted; no expansion trigger"

    if "trend" in reasons:
        return "Volatility expanding with directional follow-through"
    return "Market structure stable enough to frame risk"


def _describe_risk_bias(payload: Mapping[str, Any]) -> str:
    bias = str(payload.get("bias") or "NEUTRAL").upper()
    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper()
    if bias == "BULL" and confirm == "BULLISH":
        return "Upside asymmetry > downside risk"
    if bias == "BEAR" and confirm == "BEARISH":
        return "Downside asymmetry > upside reward"
    if bias == "BULL":
        return "Upside idea but confirmation mixed"
    if bias == "BEAR":
        return "Downside idea but confirmation mixed"
    return "Risk asymmetry unclear"


def _psychological_risk_lines(decision: DefaultActionDecision) -> tuple[str, ...]:
    reasons = set(decision.reasons)
    if decision.code == "STAND_DOWN":
        lines = [
            "Forcing trades into unstable tape",
            "Anchoring to prior bias",
        ]
        if "macro_event_imminent" in reasons:
            lines[0] = "Taking risk into event-driven volatility"
        return tuple(lines)
    if decision.code == "WAIT":
        lines = [
            "Overtrading chop",
            "Chasing small breakouts",
        ]
        if "conflict" in reasons:
            lines[1] = "Trading when confirmation disagrees"
        return tuple(lines)
    return (
        "FOMO on late breaks",
        "Relaxing stop discipline",
    )


TRADE_ENVELOPE = {
    "WAIT": {
        "allowed": ("Small-size scalps", "Defined-risk downside probes"),
        "caution": ("Call buying", "Momentum chasing"),
        "avoid": ("Full-size directional trades", "Holding for expansion without a trigger"),
    },
    "STAND_DOWN": {
        "allowed": ("Observation & prep only", "Update levels and scenarios"),
        "caution": ("Any intraday punts", "Counter-trend fades into noise"),
        "avoid": ("Adding risk into unstable tape", "Holding positions through event volatility"),
    },
    "TRADE_SELECTIVELY": {
        "allowed": ("Measured entries with confirmation", "Defined-risk continuation setups"),
        "caution": ("Counter-trend fades", "Oversizing beyond plan"),
        "avoid": ("Trading without invalidation", "Holding through opposing regime flips"),
    },
}


def _trade_envelope_for(decision: DefaultActionDecision) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    block = TRADE_ENVELOPE.get(decision.code, TRADE_ENVELOPE["WAIT"])
    return tuple(block["allowed"]), tuple(block["caution"]), tuple(block["avoid"])


def _should_silence_trade_context(
    decision: DefaultActionDecision,
    *,
    now_et: datetime,
    macro_risk: Optional[Mapping[str, Any]],
    bias: str,
    confirm: str,
    edge: float,
    session: str,
    tech_state: str,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []

    if session == "RTH":
        minutes_since_midnight = now_et.hour * 60 + now_et.minute
        open_minute = RTH_OPEN.hour * 60 + RTH_OPEN.minute
        delta_open = minutes_since_midnight - open_minute
        if 0 <= delta_open < 10:
            reasons.append("open_noise")

    if isinstance(macro_risk, Mapping):
        delta = macro_risk.get("delta_min")
        try:
            if float(delta) <= 10:
                reasons.append("macro_event_imminent")
        except Exception:  # noqa: BLE001
            pass

    if _bias_conflict(bias, confirm):
        reasons.append("conflicting_signals")

    if edge < 0.02:
        reasons.append("low_confidence")

    if session in {"WEEKEND"}:
        reasons.append("thin_liquidity")
    elif session in {"PRE", "AH"} and edge < 0.03:
        reasons.append("thin_liquidity")
    elif session == "RTH":
        minutes_since_midnight = now_et.hour * 60 + now_et.minute
        if 12 * 60 <= minutes_since_midnight <= 13 * 60 + 15 and decision.code != "TRADE_SELECTIVELY" and edge < 0.04:
            reasons.append("lunch_lull")

    if tech_state not in {"FRESH", "OK"} and decision.code != "STAND_DOWN":
        reasons.append("data_stale")

    hard_triggers = {"macro_event_imminent", "open_noise", "thin_liquidity", "data_stale"}
    hard_reasons = [reason for reason in reasons if reason in hard_triggers]
    if hard_reasons:
        return True, tuple(reasons)

    return False, tuple()


def build_trade_context_summary(
    symbol: str,
    payload: Mapping[str, Any],
    *,
    now_et: Optional[datetime] = None,
    price_snapshot: Optional[PriceSnapshot] = None,
    gate: Optional[Mapping[str, Any]] = None,
    macro_risk: Optional[Mapping[str, Any]] = None,
    session: Optional[str] = None,
) -> TradeContextSummary:
    now = now_et or _now_et()
    macro = macro_risk if macro_risk is not None else payload.get("macro_risk")
    session_code = session or payload.get("session") or market_session_et()
    decision = _determine_default_action(
        payload,
        gate=gate,
        price_snapshot=price_snapshot,
        macro_risk=macro if isinstance(macro, Mapping) else None,
        session=session_code,
    )

    market_state_line = _describe_market_state(decision, payload, macro if isinstance(macro, Mapping) else None)
    risk_bias_line = _describe_risk_bias(payload)
    psych_lines = _psychological_risk_lines(decision)
    allowed, caution, avoid = _trade_envelope_for(decision)

    edge_val = _edge_value(payload) or 0.0
    tech_state = _technical_state_from_payload(payload)
    bias = str(payload.get("bias") or "NEUTRAL").upper()
    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper()

    silence, silence_reasons = _should_silence_trade_context(
        decision,
        now_et=now,
        macro_risk=macro if isinstance(macro, Mapping) else None,
        bias=bias,
        confirm=confirm,
        edge=edge_val,
        session=session_code,
        tech_state=tech_state,
    )

    return TradeContextSummary(
        symbol=symbol.upper(),
        decision=decision,
        market_state=market_state_line,
        risk_bias=risk_bias_line,
        psychological_risks=psych_lines,
        allowed=allowed,
        caution=caution,
        avoid=avoid,
        silence=silence,
        silence_reasons=silence_reasons,
        generated_et=now,
    )


def render_trade_context(summary: TradeContextSummary) -> str:
    lines: list[str] = [f"🧠 TNT TRADE CONTEXT — {summary.symbol}"]
    lines.append("")
    lines.append("Market State:")
    lines.append(f"• {summary.market_state}")
    lines.append("")
    lines.append("Risk Bias:")
    lines.append(f"• {summary.risk_bias}")
    lines.append("")
    lines.append("Psychological Risk:")
    for entry in summary.psychological_risks:
        lines.append(f"• {entry}")
    lines.append("")
    lines.append("TRADE ENVELOPE")
    lines.append("✅ Allowed:")
    for item in summary.allowed:
        lines.append(f"• {item}")
    lines.append("")
    lines.append("⚠️ Caution:")
    for item in summary.caution:
        lines.append(f"• {item}")
    lines.append("")
    lines.append("⛔ Avoid:")
    for item in summary.avoid:
        lines.append(f"• {item}")
    lines.append("")
    lines.append(f"DEFAULT ACTION: {summary.decision.emoji} {summary.decision.label}")
    return "\n".join(lines)


def format_signal_clean(
    sym: str,
    payload: dict,
    *,
    verbose: bool = False,
    now_et: Optional[datetime] = None,
    gate: Optional[Mapping[str, Any]] = None,
    price_snapshot: Optional[PriceSnapshot] = None,
) -> str:
    summary = build_trade_context_summary(
        sym,
        payload,
        now_et=now_et,
        price_snapshot=price_snapshot,
        gate=gate,
    )

    payload["default_action_code"] = summary.decision.code
    payload["default_action_label"] = summary.decision.label
    payload["default_action_reasons"] = list(summary.decision.reasons)
    payload["default_action_silence"] = summary.silence
    payload["default_action_silence_reasons"] = list(summary.silence_reasons)

    payload["trade_context_summary"] = {
        "decision": summary.decision.code,
        "emoji": summary.decision.emoji,
        "label": summary.decision.label,
        "reasons": list(summary.decision.reasons),
        "market_state": summary.market_state,
        "risk_bias": summary.risk_bias,
        "psychological_risk": list(summary.psychological_risks),
        "allowed": list(summary.allowed),
        "caution": list(summary.caution),
        "avoid": list(summary.avoid),
        "silence": summary.silence,
        "silence_reasons": list(summary.silence_reasons),
        "generated_et": summary.generated_et.isoformat() if summary.generated_et else None,
    }

    if summary.silence:
        return SILENCE_NOTICE

    return _format_trade_context_block(sym, summary, payload, gate or {}, price_snapshot)


def _bias_marker(tag: str) -> str:
    mapping = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}
    return mapping.get(tag.upper(), "⚪")


def _confirm_marker(tag: str) -> str:
    mapping = {"BULLISH": "🟢", "BEARISH": "🔴", "MIXED": "🟡", "UNKNOWN": "⚪"}
    return mapping.get(tag.upper(), "⚪")


def _decision_mode_label(decision: DefaultActionDecision) -> str:
    mapping = {
        "TRADE_SELECTIVELY": "AGGRESSIVE",
        "WAIT": "NORMAL",
        "STAND_DOWN": "STAND-DOWN",
    }
    return mapping.get(decision.code, decision.label.upper())


def _coerce_float(value: Optional[object]) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None


def _format_ts_display(ts_iso: Optional[str]) -> str:
    if not ts_iso:
        return "n/a"
    try:
        dt = parse_iso(str(ts_iso))
    except Exception:  # noqa: BLE001
        return str(ts_iso)
    return dt.astimezone(ET).strftime("%H:%M ET")


def _price_descriptor(payload: Mapping[str, Any], price_snapshot: Optional[PriceSnapshot]) -> str:
    mode = str(payload.get("price_mode") or "").upper()
    if mode == "LIVE":
        return "live close"
    if mode == "CLOSED":
        return "last close"
    tf = str(payload.get("price_tf") or "").strip()
    if tf:
        return f"{tf} close"
    source = getattr(price_snapshot, "source", "")
    if source == "polygon_snapshot_lastTrade":
        return "live snapshot"
    if source == "db_1m_close":
        return "last 1m close"
    if source == "db_1d_close":
        return "last 1d close"
    return "price"


def _format_level_chain(pairs: Optional[Iterable[Any]], *, limit: int = 2) -> str:
    if not pairs:
        return "n/a"
    formatted: list[str] = []
    for item in pairs:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        name, value = item
        try:
            formatted.append(f"{str(name).upper()} {float(value):.2f}")
        except Exception:  # noqa: BLE001
            continue
        if len(formatted) >= limit:
            break
    return " → ".join(formatted) if formatted else "n/a"


def _format_trade_context_block(
    symbol: str,
    summary: TradeContextSummary,
    payload: Mapping[str, Any],
    gate: Mapping[str, Any],
    price_snapshot: Optional[PriceSnapshot],
) -> str:
    sym = (symbol or "?").strip().upper() or "?"

    price_val = _coerce_float(payload.get("last_price"))
    if price_val is None and isinstance(price_snapshot, PriceSnapshot):
        price_val = _coerce_float(price_snapshot.price)

    ts_iso = payload.get("last_price_ts")
    if not ts_iso and isinstance(price_snapshot, PriceSnapshot) and price_snapshot.asof_et:
        ts_iso = price_snapshot.asof_et.astimezone(timezone.utc).isoformat()
    ts_display = _format_ts_display(ts_iso)
    descriptor = _price_descriptor(payload, price_snapshot)
    price_text = f"{price_val:.2f}" if price_val is not None else "n/a"

    pivot = _coerce_float(payload.get("pivot"))
    r1 = _coerce_float(payload.get("r1"))
    r2 = _coerce_float(payload.get("r2"))
    s1 = _coerce_float(payload.get("s1"))
    s2 = _coerce_float(payload.get("s2"))

    delta = None if price_val is None or pivot is None else price_val - pivot
    if delta is None:
        vs_pivot = "n/a"
        now_line = "- Now: n/a"
    else:
        direction = "above" if delta >= 0 else "below"
        vs_pivot = f"{delta:+.2f} pts ({direction} {fmt_money(pivot)})"
        now_line = f"- Now: **{fmt_money(price_val)}** ({abs(delta):.2f} pts, {direction} **{fmt_money(pivot)}**)"

    bias = str(payload.get("bias") or "NEUTRAL").upper()
    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper()
    conviction = str(payload.get("conviction") or "LOW").upper()
    regime = str(payload.get("tnt_regime") or payload.get("pivot_regime") or payload.get("regime") or "UNKNOWN").upper()
    mode_label = _decision_mode_label(summary.decision)

    tf_exec = str(payload.get("tf_exec") or "5m")
    tf_struct = str(payload.get("tf_struct") or "60m")
    tf_ctx = str(payload.get("tf_ctx") or "1D")

    upside_chain = _format_level_chain(payload.get("upside_targets"))
    downside_chain = _format_level_chain(payload.get("downside_targets"))

    gate_mode = str(gate.get("mode") or payload.get("gate_mode") or "n/a")
    gate_reason = str(gate.get("reason") or payload.get("gate_reason") or "").strip() or "n/a"

    lines: list[str] = [f"🚨 **{sym} — Trade Context**"]
    lines.append(f"💲 **Last Price:** **{price_text}** ({descriptor} | {ts_display})")
    lines.append(f"📏 **vs Pivot:** {vs_pivot}")

    bias_line = f"**Bias:** {_bias_marker(bias)} {bias} ({_confirm_marker(confirm)} confirm: {confirm})"
    lines.append("")
    lines.append(bias_line)
    lines.append(f"🧭 Regime: {regime} | Mode: {mode_label} | Conviction: {conviction}")

    lines.append("")
    lines.append("⏱ Timeframes:")
    lines.append(f"- Execution: {tf_exec}")
    lines.append(f"- Structure: {tf_struct}")
    lines.append(f"- Context: {tf_ctx}")

    lines.append("")
    lines.append("📐 Key Levels (RTH):")
    if pivot is not None:
        lines.append(f"- Pivot: **{fmt_money(pivot)}**")
    else:
        lines.append("- Pivot: n/a")
    lines.append(now_line)
    lines.append(f"- Upside: {upside_chain}")
    lines.append(f"- Downside: {downside_chain}")

    bull_entry_level = fmt_money(pivot) if pivot is not None else "Pivot"
    lines.append("")
    lines.append("🧠 How Pros Would Trade It:")
    lines.append(f"- Bull: enter on reclaim + hold above **{bull_entry_level}**")
    lines.append(f"- Targets: {upside_chain}")
    lines.append(f"- Invalidation: lose **{bull_entry_level}**")
    lines.append(f"- Bear: enter on lose + hold below **{bull_entry_level}**")
    lines.append(f"- Targets: {downside_chain}")
    lines.append(f"- Invalidation: reclaim **{bull_entry_level}**")

    lines.append("")
    lines.append("🚫 Do Nothing If:")
    lines.append("- No break + retest confirmation (first spike only)")
    lines.append("- Choppy price around pivot (no direction)")
    lines.append("- VIX gate flips hard against the bias")
    lines.append(f"- Gate: {gate_mode} | {gate_reason}")

    vix_trend = str(payload.get("vix_trend") or "n/a")
    sqqq_dir = str(payload.get("sqqq_dir") or "n/a")
    lines.append("")
    lines.append("🧭 **Market Confirmation**")
    lines.append(f"- VIX: **{vix_trend}**")
    lines.append(f"- SQQQ: **{sqqq_dir}**")

    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)

def build_signal_payload(sym: str) -> Optional[tuple[dict, float, Dict[str, object]]]:
    """Return structured signal context, probability, and gate info."""

    symbol = (sym or "").strip().upper()
    if not symbol:
        return None

    now_et = _now_et()
    session = market_session_et()
    macro_events = load_macro_events_for_date(now_et.strftime("%Y-%m-%d"))
    macro_risk = macro_within_minutes(macro_events, 60, now_et)

    sig = get_latest_signal(symbol)
    if not sig:
        return None

    try:
        prob_up = float(sig.get("prob_up"))
    except (TypeError, ValueError):
        return None

    pivots_info = get_latest_daily_pivots(symbol)
    pivots = pivots_info.get("piv") if pivots_info and isinstance(pivots_info, dict) else None
    if not isinstance(pivots, dict):
        return None

    def _maybe_float(val):
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    pivot_val = _maybe_float(pivots.get("P"))
    r1 = _maybe_float(pivots.get("R1"))
    r2 = _maybe_float(pivots.get("R2"))
    s1 = _maybe_float(pivots.get("S1"))
    s2 = _maybe_float(pivots.get("S2"))

    if pivot_val is None or r1 is None or s1 is None:
        return None

    edge = _edge(prob_up)
    conviction = _conv(edge)
    bias = "BULL" if prob_up >= 0.53 else ("BEAR" if prob_up <= 0.47 else "NEUTRAL")

    regime = "UNKNOWN"
    tf_exec = "5m"
    tf_struct = "60m"
    tf_ctx = "1D"

    meta_raw = sig.get("meta")
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
            if isinstance(meta, dict):
                regime_val = meta.get("regime") or meta.get("market_regime")
                if regime_val:
                    regime = str(regime_val).upper()
                tf_exec = str(meta.get("tf_exec") or meta.get("tf_execution") or tf_exec)
                tf_struct = str(meta.get("tf_struct") or meta.get("tf_structure") or tf_struct)
                tf_ctx = str(meta.get("tf_ctx") or meta.get("tf_context") or tf_ctx)
        except Exception:  # noqa: BLE001
            pass

    bias_confirm, _confirm_note, confirm_detail = vix_sqqq_confirmation(tf=os.getenv("BIAS_TF", "5m"))
    vix_info = get_vix_context("1m")
    gate = vix_gating_action(float(vix_info["level"])) if vix_info else {"mode": "OK", "reason": "no vix"}

    payload = {
        "bias": bias,
        "bias_confirm": bias_confirm,
        "conviction": conviction,
        "regime": regime,
        "tf_exec": tf_exec,
        "tf_struct": tf_struct,
        "tf_ctx": tf_ctx,
        "pivot": pivot_val,
        "r1": r1,
        "r2": r2,
        "s1": s1,
        "s2": s2,
        "vix_trend": confirm_detail.get("vix_trend", "unknown"),
        "sqqq_dir": confirm_detail.get("sqqq_dir", "unknown"),
        "edge": edge,
        "model": sig.get("model_version"),
        "ts": sig.get("ts"),
        "gate_mode": gate.get("mode"),
        "gate_reason": gate.get("reason"),
        "trade_context_mode": "db",
        "trade_context_source": "model",
        "session": session,
        "generated_et": now_et.isoformat(),
    }

    payload["analysis_mode"] = payload["trade_context_mode"]
    payload["analysis_source"] = payload["trade_context_source"]

    if macro_risk:
        payload["macro_risk"] = macro_risk

    stale_limit = DATA_STALE_MAX_MIN if DATA_STALE_MAX_MIN > 0 else 3.0
    try:
        fresh = data_is_fresh(symbol, tf="1m", max_min=stale_limit)
    except Exception:  # noqa: BLE001 - if freshness check fails, assume fresh to avoid spam
        fresh = True

    last_px: Optional[float] = None
    last_ts: Optional[str] = None
    price_mode = "db"
    price_source = "db"
    price_tf_label = PRICE_TF_LABEL
    price_age_minutes: Optional[float] = None

    try:
        last_price_info = get_latest_price(symbol)
    except Exception:  # noqa: BLE001
        last_price_info = None

    if last_price_info:
        last_px, last_ts = last_price_info

    need_live = (last_px is None) or (not fresh)

    if need_live:
        polygon_ok = False
        try:
            polygon_ok = bool(symbol_supported_polygon(symbol))
        except Exception:  # noqa: BLE001
            polygon_ok = False

        if polygon_ok:
            live_px: Optional[float]
            live_ts: Optional[str]
            live_src: str
            try:
                live_px, live_ts, live_src = fetch_live_price(symbol)
            except Exception as exc:  # noqa: BLE001
                live_px = None
                live_ts = None
                live_src = f"error:{exc}"
            if live_px is not None and live_ts is not None:
                try:
                    last_px = float(live_px)
                except Exception:  # noqa: BLE001
                    last_px = None
                else:
                    last_ts = live_ts
                    if isinstance(live_src, str) and (
                        live_src.startswith("snapshot")
                        or live_src.startswith("index-snapshot")
                        or live_src.startswith("ws-cache")
                    ):
                        price_mode = "live"
                        price_source = live_src
                        price_tf_label = "live"
                    else:
                        price_mode = "closed"
                        price_source = live_src or "snapshot"
                    fresh = True
                    print(f"[PRICE] using live snapshot for {symbol} ({price_source})")
        elif not fresh:
            print(f"[PRICE][WARN] {symbol} data stale and live snapshot unavailable; using cached bar")

    payload["data_quality"] = {"technical_state": "FRESH" if fresh else "STALE"}

    if last_ts:
        try:
            ts_dt = parse_iso(str(last_ts))
            price_age_minutes = (now_et - ts_dt.astimezone(ET)).total_seconds() / 60.0
        except Exception:  # noqa: BLE001
            price_age_minutes = None

    level_ctx = None
    if last_px is not None:
        payload["last_price"] = last_px
        payload["last_price_ts"] = last_ts
        payload["price_tf"] = price_tf_label
        resolved_price_mode = "LIVE"
        if price_mode != "live":
            if session == "RTH" and fresh:
                resolved_price_mode = "LIVE"
            else:
                resolved_price_mode = "CLOSED"
        payload["price_mode"] = resolved_price_mode
        if price_age_minutes is not None:
            payload["price_age_minutes"] = price_age_minutes
        if price_mode == "live":
            payload["price_source"] = price_source
            payload["trade_context_mode"] = "on_demand"
            payload["analysis_mode"] = "on_demand"
        elif price_mode == "closed":
            payload["price_source"] = price_source
        if isinstance(pivots, dict):
            try:
                level_ctx = build_level_context(last_px, pivots)
            except Exception:  # noqa: BLE001
                level_ctx = None

    if level_ctx:
        payload["level_ctx"] = level_ctx
        payload["pivot_regime"] = level_ctx.get("regime")
        payload["pivot_extension_mode"] = bool(level_ctx.get("extension_mode"))
        payload["upside_targets"] = level_ctx.get("upside_targets")
        payload["downside_targets"] = level_ctx.get("downside_targets")

    if vix_info:
        try:
            payload["vix_level"] = float(vix_info.get("level"))
        except Exception:  # noqa: BLE001
            pass

    # TNT regime (used for discipline gating in autopost/analyze)
    try:
        tnt = tnt_regime_assess(payload, now_et=now_et)
    except Exception:  # noqa: BLE001
        tnt = {"regime": "UNKNOWN", "posture": "UNKNOWN", "permissions": {}, "reasons": ["tnt assess failed"]}
    payload["tnt"] = tnt
    payload["tnt_regime"] = tnt.get("regime")
    payload["tnt_posture"] = tnt.get("posture")
    payload["tnt_confidence"] = tnt.get("confidence")

    _maybe_append_regime_audit(payload, now_et=now_et)

    return payload, prob_up, gate


def build_trade_context_payload(sym: str) -> tuple[Optional[dict], Optional[str]]:
    built = build_signal_payload(sym)
    if not built:
        return None, "signal unavailable"

    payload, _prob, _gate = built

    ctx = payload.get("level_ctx") if isinstance(payload, dict) else None
    last_price = payload.get("last_price") if isinstance(payload, dict) else None

    if ctx and last_price is not None:
        ok, err = validate_targets_vs_price(
            float(last_price),
            ctx.get("upside_targets"),
            ctx.get("downside_targets"),
        )
        if not ok:
            return None, f"Quality gate: {err}"

    return payload, None


def build_trade_context_packet(
    sym: str,
    payload: dict,
    *,
    context_mode: str,
    live_price: Optional[float],
    live_price_ts: Optional[str],
) -> tuple[dict, Optional[dict]]:
    """Compose the AI packet plus pivots used for gating."""

    level_ctx = payload.get("level_ctx") if isinstance(payload.get("level_ctx"), dict) else None
    if level_ctx and isinstance(level_ctx.get("levels"), dict):
        pivots_for_gate: Optional[dict] = dict(level_ctx.get("levels", {}))
    else:
        pivots_for_gate = {
            "P": payload.get("pivot"),
            "R1": payload.get("r1"),
            "R2": payload.get("r2"),
            "S1": payload.get("s1"),
            "S2": payload.get("s2"),
        }

    def _safe_float(val: object) -> Optional[float]:
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    price_source = "polygon_snapshot" if context_mode == "on_demand" else "db_1m"
    price_val = live_price if context_mode == "on_demand" and live_price is not None else payload.get("last_price")
    price_val = _safe_float(price_val)
    price_ts_val = live_price_ts if context_mode == "on_demand" and live_price_ts is not None else payload.get("last_price_ts")

    pivot_levels = {
        "P": _safe_float(payload.get("pivot")),
        "R1": _safe_float(payload.get("r1")),
        "R2": _safe_float(payload.get("r2")),
        "R3": _safe_float(payload.get("r3")),
        "S1": _safe_float(payload.get("s1")),
        "S2": _safe_float(payload.get("s2")),
        "S3": _safe_float(payload.get("s3")),
    }

    if level_ctx and isinstance(level_ctx.get("levels"), dict):
        for key, val in level_ctx["levels"].items():
            if key in pivot_levels and pivot_levels.get(key) is None:
                pivot_levels[key] = _safe_float(val)

    daily_pivots = get_latest_daily_pivots(sym)
    if daily_pivots and isinstance(daily_pivots.get("piv"), dict):
        for key in ("P", "R1", "R2", "R3", "S1", "S2", "S3"):
            if pivot_levels.get(key) is None:
                pivot_levels[key] = _safe_float(daily_pivots["piv"].get(key))

    data_staleness_s: Optional[float] = None
    if price_ts_val:
        try:
            ts_dt = parse_iso(price_ts_val)
            delta = _now_utc() - ts_dt.astimezone(timezone.utc)
            data_staleness_s = max(delta.total_seconds(), 0.0)
        except Exception:  # noqa: BLE001
            data_staleness_s = None

    packet = {
        "symbol": sym,
        "current_price": price_val,
        "current_price_source": price_source,
        "current_price_ts": price_ts_val,
        "pivot": pivot_levels.get("P"),
        "r_levels": {
            "R1": pivot_levels.get("R1"),
            "R2": pivot_levels.get("R2"),
            "R3": pivot_levels.get("R3"),
        },
        "s_levels": {
            "S1": pivot_levels.get("S1"),
            "S2": pivot_levels.get("S2"),
            "S3": pivot_levels.get("S3"),
        },
        "bias": payload.get("bias"),
        "bias_confirm": payload.get("bias_confirm"),
        "regime": payload.get("pivot_regime") or payload.get("regime"),
        "conviction": payload.get("conviction"),
        "vix_trend": payload.get("vix_trend"),
        "sqqq_dir": payload.get("sqqq_dir"),
        "model": payload.get("model") or payload.get("model_version"),
        "signal_ts": payload.get("ts"),
        "notes": {
            "session": market_session_et(),
            "data_staleness_s": data_staleness_s,
        },
        "trade_context_mode": context_mode,
        "analysis_mode": context_mode,
    }

    return packet, pivots_for_gate


build_analysis_payload = build_trade_context_payload
build_analysis_packet = build_trade_context_packet


def build_autopost_payload():
    """
    Minimal safe autopost:
    - Posts daily prep for your watchlist during RTH
    - Returns None if nothing should be posted
    """

    if market_session_et() != "RTH":
        return None

    watchlist = os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",")
    watchlist = [s.strip().upper() for s in watchlist if s.strip()]
    if not watchlist:
        return None

    lines: list[str] = []
    lines.append("📌 **Autopost Check**")
    lines.append("Watchlist: " + ", ".join(watchlist))
    lines.append("")
    lines.append("⏱ Timeframes:")
    lines.append("• Execution: 5m")
    lines.append("• Structure: 60m")
    lines.append("• Context: 1D")
    lines.append("")
    lines.append("🧭 Regime:")
    lines.append("TRANSITION")
    lines.append("")
    symbols_clean = list(dict.fromkeys(watchlist))
    now_et = _now_et()
    session = market_session_et(now_et.astimezone(timezone.utc))

    last_price_block = _format_last_price_section(
        symbols_clean,
        now_et=now_et,
        session=session,
    )
    if last_price_block.strip():
        lines.append("")
        lines.append(last_price_block.rstrip("\n"))

    lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    lines.append("")
    lines.append("📐 Key Levels:")
    lines.append("• (use !daily or Analyze <symbol> for full levels)")
    lines.append("")
    lines.append("🧠 How Pros Would Trade It:")
    lines.append("• Use commands for full detail; autopost is heartbeat for now")
    lines.append("")
    lines.append("🚫 Do Nothing If:")
    lines.append("• Data is stale or signals conflict")
    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


_NOW_PROVIDER: Optional[Callable[[], datetime]] = None


def _set_now_provider(provider: Optional[Callable[[], datetime]]) -> None:
    global _NOW_PROVIDER
    _NOW_PROVIDER = provider

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "./db/tnt.db")
TF = os.getenv("TF", "1m")
MODEL_VERSION = os.getenv("MODEL_VERSION", "heuristic-v1")
HORIZON_MIN = int(os.getenv("MODEL_HORIZON_MIN", "5"))

CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or "0")
CANARY_CHANNEL_ID = int(os.getenv("DISCORD_CANARY_CHANNEL_ID", "0") or "0")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    try:
        migrate_settings_table(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] settings migration failed: {exc}")
    return conn


VALID_MODES = {"strict", "insights"}


def get_analysis_mode() -> str:
    try:
        conn = db_connect()
        mode = settings_get(conn, "analysis_mode", "insights").strip().lower()
        conn.close()
        return mode if mode in VALID_MODES else "insights"
    except Exception:  # noqa: BLE001
        return "insights"


def set_analysis_mode(mode: str) -> None:
    mode = (mode or "").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}")
    conn = db_connect()
    settings_set(conn, "analysis_mode", mode)
    conn.close()

_autopost_daily_task = None
_autopost_signal_task = None
_automation_focus_task: Optional[asyncio.Task] = None
_automation_intraday_task: Optional[asyncio.Task] = None
_automation_recap_task: Optional[asyncio.Task] = None
_automation_heartbeat_task: Optional[asyncio.Task] = None
_slash_tree_synced = False

DATA_STALE_MAX_MIN = float(os.getenv("DATA_STALE_MAX_MIN", "3") or "3")
DATA_STALE_WARN_CHANNEL_ID = int(os.getenv("DATA_STALE_WARN_CHANNEL", "0") or "0")
DATA_STALE_SYMBOL = (os.getenv("DATA_STALE_SYMBOL", "SPY") or "SPY").strip().upper()

AUTOPOST_EDGE_MIN_STRICT = _env_float("AUTOPOST_EDGE_MIN_STRICT", "0.04")
AUTOPOST_EDGE_MIN_INSIGHTS = _env_float("AUTOPOST_EDGE_MIN_INSIGHTS", "0.015")

_last_daily_post_et_date: Optional[date] = None
_last_focus_list_et_date: Optional[date] = None
_last_intraday_update_et_date: Optional[date] = None
_last_recap_et_date: Optional[date] = None
_last_heartbeat_sent_ts: Optional[float] = None
_last_signal_post_by_symbol: Dict[str, int] = {}

SignalState = Dict[str, object]
_last_signal_state: Dict[str, SignalState] = {}

_last_summary_date: Optional[date] = None
_last_morning_opt_date: Optional[date] = None
_last_vix_alert_level: Optional[float] = None
_last_vix_alert_ts: Optional[str] = None
_last_monday_date: Optional[date] = None

_data_stale_paused = False
_data_stale_lock: Optional[asyncio.Lock] = None
_data_stale_initialized = False

_bootstrap_tasks: dict[str, asyncio.Task] = {}

ANALYZE_BOOTSTRAP_TIMEOUT_S = float(os.getenv("ANALYZE_BOOTSTRAP_TIMEOUT_S", "8") or "8")

_TF_LABELS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
}
PRICE_TF_LABEL = _TF_LABELS.get(TF.lower() if isinstance(TF, str) else TF, str(TF))

_DEFAULT_FUTURES_CONTEXT_PATH = Path(__file__).resolve().parent.parent / "iqfeed_service" / "output" / "futures_context.json"
FUTURES_CONTEXT_PATH = Path(os.getenv("FUTURES_CONTEXT_PATH", "") or _DEFAULT_FUTURES_CONTEXT_PATH).expanduser()
SMART_MARKET_IQ_PATH = Path(os.getenv("SMART_MARKET_IQ_PATH", "data/smart_market_iq.json") or "data/smart_market_iq.json").expanduser()
FUTURES_CONTEXT_LABEL = os.getenv("FUTURES_CONTEXT_LABEL", "@ES#")
FUTURES_STALE_MINUTES_ACTIVE = int(os.getenv("FUTURES_STALE_MINUTES_ACTIVE", "20"))
FUTURES_STALE_MINUTES_OFFHOURS = int(os.getenv("FUTURES_STALE_MINUTES_OFFHOURS", str(12 * 60)))

OPT_MAX_LINES = int(os.getenv("OPT_MAX_LINES", "45"))
OPT_PLUS_MAX_LINES = int(os.getenv("OPT_PLUS_MAX_LINES", "120"))
OPT_NO_TRADE_MIN_EDGE = float(os.getenv("OPT_NO_TRADE_MIN_EDGE", "0.02"))
OPT_HIGH_CONV_EDGE = float(os.getenv("OPT_HIGH_CONV_EDGE", "0.05"))

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE_URL = (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")
MASSIVE_BASE_URL = (os.getenv("MASSIVE_BASE_URL") or "").rstrip("/")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
USE_MASSIVE = os.getenv("ZERO_DTE_USE_MASSIVE", "0") == "1"
POLYGON_DISABLED = os.getenv("ZERO_DTE_DISABLE_POLYGON", "0") == "1"

STALE_MIN_RTH = int(os.getenv("STALE_MIN_RTH", "5"))
STALE_MIN_OFFHOURS = int(os.getenv("STALE_MIN_OFFHOURS", "60"))

VIX_ALERTS_ENABLED = os.getenv("VIX_ALERTS_ENABLED", "0") == "1"
VIX_ALERT_CHANNEL_ID = int(os.getenv("VIX_ALERT_CHANNEL_ID", "0") or "0")
VIX_ALERT_CHECK_SEC = int(os.getenv("VIX_ALERT_CHECK_SEC", "60"))
VIX_ALERT_ABS_MOVE = float(os.getenv("VIX_ALERT_ABS_MOVE", "1.00"))
VIX_ALERT_PCT_MOVE = float(os.getenv("VIX_ALERT_PCT_MOVE", "5.0"))
VIX_GATING_ENABLED = os.getenv("VIX_GATING_ENABLED", "0") == "1"
VIX_MAX_REGIME = (os.getenv("VIX_MAX_REGIME", "HIGH / STRESS") or "HIGH / STRESS").upper()
VIX_HARD_BLOCK_LEVEL = float(os.getenv("VIX_HARD_BLOCK_LEVEL", "0"))
VIX_SOFT_BLOCK_LEVEL = float(os.getenv("VIX_SOFT_BLOCK_LEVEL", "0"))

BIAS_CONFIRM_ENABLED = os.getenv("BIAS_CONFIRM_ENABLED", "1") == "1"
BIAS_TF = os.getenv("BIAS_TF", "5m")

_REGIME_ORDER = {
    "LOW": 0,
    "LOW VOL": 0,
    "NORMAL": 1,
    "NORMAL VOL": 1,
    "ELEVATED": 2,
    "ELEVATED VOL": 2,
    "HIGH": 3,
    "HIGH / STRESS": 3,
    "STRESS": 3,
}

MONDAY_PLAYBOOK_ENABLED = os.getenv("MONDAY_PLAYBOOK_ENABLED", "0") == "1"
MONDAY_PLAYBOOK_TIME_ET = os.getenv("MONDAY_PLAYBOOK_TIME_ET", "09:25")
MONDAY_PLAYBOOK_CHANNEL_ID = int(os.getenv("MONDAY_PLAYBOOK_CHANNEL_ID", "0") or "0")
MONDAY_PLAYBOOK_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("MONDAY_PLAYBOOK_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]

MORNING_BRIEF_ENABLED = os.getenv("MORNING_BRIEF_ENABLED", "0") == "1"
MORNING_BRIEF_CHANNEL_ID = int(os.getenv("MORNING_BRIEF_CHANNEL_ID", "0") or "0")
MORNING_BRIEF_TIME_ET = os.getenv("MORNING_BRIEF_TIME_ET", "08:00")
MORNING_BRIEF_DAYS = [
    d.strip().upper()
    for d in os.getenv("MORNING_BRIEF_DAYS", "MON,TUE,WED,THU,FRI").split(",")
]

MORNING_OPT_ENABLED = os.getenv("MORNING_OPT_ENABLED", "0") == "1"
MORNING_OPT_TIME_ET = os.getenv("MORNING_OPT_TIME_ET", "09:28")
MORNING_OPT_CHANNEL_ID = int(os.getenv("MORNING_OPT_CHANNEL_ID", "0") or "0")
MORNING_OPT_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("MORNING_OPT_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]

DAILY_SUMMARY_ENABLED = os.getenv("DAILY_SUMMARY_ENABLED", "0") == "1"
DAILY_SUMMARY_TIME_ET = os.getenv("DAILY_SUMMARY_TIME_ET", "09:31")
DAILY_SUMMARY_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("DAILY_SUMMARY_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]
DAILY_SUMMARY_CHANNEL_ID = int(os.getenv("DAILY_SUMMARY_CHANNEL_ID", "0") or "0")

AI_ENABLED = os.getenv("DISCORD_AI_ENABLED", "0") == "1"
AI_MODE = (os.getenv("DISCORD_AI_MODE", "mention") or "mention").lower().strip()
AI_ROLE_ID = int(os.getenv("DISCORD_AI_ROLE_ID", "0") or "0")
AI_CHANNEL_ID = int(os.getenv("DISCORD_AI_CHANNEL_ID", "0") or "0")
AI_MODEL = os.getenv("DISCORD_AI_MODEL", "gpt-5-mini")
AI_MAX_CHARS = int(os.getenv("DISCORD_AI_MAX_CHARS", "1200") or "1200")
VERBOSE_DEFAULT = os.getenv("DISCORD_VERBOSE_DEFAULT", "0") == "1"


def _ai_enabled() -> bool:
    return os.getenv("DISCORD_AI_ENABLED", "0") in ("1", "true", "True")


def _ai_model() -> str:
    return os.getenv("DISCORD_AI_MODEL", "gpt-5-mini")


def _ai_max_chars() -> int:
    try:
        return int(os.getenv("DISCORD_AI_MAX_CHARS", "1800"))
    except Exception:  # noqa: BLE001
        return 1800


def _ai_timeout() -> float:
    try:
        return float(os.getenv("DISCORD_AI_TIMEOUT_S", "12"))
    except Exception:  # noqa: BLE001
        return 12.0


def _coach_model_name() -> str:
    # Prefer TNT_LLM_MODEL so the One Door wrapper can enforce a single locked model.
    # If unset, fall back to legacy knobs.
    return (os.getenv("TNT_LLM_MODEL") or os.getenv("OPENAI_MODEL_COACH") or _ai_model()).strip()


def _coach_max_tokens() -> int:
    try:
        value = int(os.getenv("COACH_MAX_TOKENS", "900") or "900")
    except Exception:  # noqa: BLE001
        value = 900
    return max(200, min(value, 2000))


def _extract_ai_text(resp: object) -> str:
    """Legacy helper retained for downstream compatibility."""
    text_attr = getattr(resp, "output_text", None)
    if isinstance(text_attr, str) and text_attr.strip():
        return text_attr.strip()
    return ""


def _bounded_list(
    items: Sequence[str],
    *,
    min_len: int,
    max_len: int,
    filler: str,
) -> list[str]:
    cleaned = [itm.strip() for itm in items if isinstance(itm, str) and itm.strip()]
    while len(cleaned) < min_len:
        cleaned.append(filler)
    return cleaned[:max_len]


def _compose_coach_sections(
    answer: str,
    why_lines: Sequence[str],
    key_lines: Sequence[str],
    next_line: str,
) -> str:
    lines: list[str] = [f"Answer: {answer.strip()}"]
    lines.append("Why:")
    lines.extend(f"- {line}" for line in why_lines)
    lines.append("Key level / invalidation:")
    lines.extend(f"- {line}" for line in key_lines)
    lines.append("Next Step:")
    lines.append(f"- {next_line.strip()}")
    return "\n".join(lines)


def _build_coach_limited_response(symbol: str, meta: Mapping[str, Any]) -> str:
    missing_sections: list[str] = []
    data_quality = meta.get("data_quality") if isinstance(meta, dict) else {}
    if isinstance(data_quality, dict):
        dq_missing = data_quality.get("missing_sections")
        if isinstance(dq_missing, (list, tuple)):
            missing_sections.extend(str(item) for item in dq_missing if item)
    meta_missing = meta.get("missing_sections") if isinstance(meta, dict) else []
    if isinstance(meta_missing, (list, tuple)):
        missing_sections.extend(str(item) for item in meta_missing if item)
    if not missing_sections:
        missing_sections = []

    reasons: list[str] = []
    if missing_sections:
        joined = ", ".join(sorted(set(missing_sections)))
        reasons.append(f"Missing sections: {joined}.")

    gate_info = meta.get("gate") if isinstance(meta, dict) else None
    if isinstance(gate_info, dict):
        mode = str(gate_info.get("mode") or "").upper()
        reason = gate_info.get("reason")
        if mode and mode != "OK":
            detail = f"Gate {mode}" + (f" — {reason}" if reason else "")
            reasons.append(detail)

    if not reasons:
        reasons.append("Analyze returned limited data; live inputs not confirmed.")

    why_lines = _bounded_list(
        reasons,
        min_len=2,
        max_len=4,
        filler="Respect capital until data refreshes."
    )

    key_lines = _bounded_list(
        ["Wait for fresh price and signal data before defining triggers."],
        min_len=1,
        max_len=2,
        filler="Use pivots from /analyze once live data resumes."
    )

    next_line = f"Re-run /analyze {symbol.upper()} once the feed updates."
    return _compose_coach_sections(
        "Not actionable — data not ready",
        why_lines,
        key_lines,
        next_line,
    )


def _build_coach_user_text(question: str) -> str:
    return (
        "Answer the user's question using ONLY TNT_STATE.\n"
        "Be concise, confident, and specific.\n"
        "If timestamps look stale or permissions.no_trade is true, do NOT go silent: answer in a standby mode.\n"
        "In standby mode: avoid price/level claims, give scenario-based guidance for the next session open, and list what to watch.\n"
        "Never claim certainty about tomorrow's direction.\n"
        "Never invent prices, indicators, levels, or timestamps beyond what TNT_STATE contains.\n"
        "Do not provide financial advice.\n"
        "You may reference specific option contracts ONLY if they appear in TNT_STATE; do not recommend a specific contract trade.\n\n"
        "When data is healthy (TNT_STATE.meta.data_health == OK and permissions.no_trade == false), ground conditions using these fields when available:\n"
        "- TNT_STATE.price.last\n"
        "- TNT_STATE.levels.pivots_rth (P, S1, R1)\n\n"
        "If present, you MAY also reference numeric indicators from TNT_STATE.technicals (RSI14, MACD histogram) ONLY when data is healthy; do not use them in standby mode.\n\n"
        "If TNT_STATE.context.recent_daily_trend is present, use it to describe the past few sessions' trend.\n\n"

        "If TNT_STATE.edge_profile is present, tailor tone/constraints to that profile (risk budget, preferred setups, avoid list).\n\n"
        "Respond using exactly this format:\n"
        "Answer: <bias + why in one sentence>\n"
        "Bullish only if:\n"
        "- condition\n"
        "Bearish if:\n"
        "- condition\n"
        "Invalidation:\n"
        "- level or state that cancels the idea\n"
        "Do nothing if:\n"
        "- condition that tells traders to stand aside\n\n"
        "User question:\n"
        f"{question.strip()}"
    ).strip()


def _sanitize_edge_profile(obj: object) -> Optional[dict[str, Any]]:
    if not isinstance(obj, dict):
        return None

    def _clean_str(val: object, *, max_len: int = 120) -> Optional[str]:
        if not isinstance(val, str):
            return None
        s = val.strip()
        if not s:
            return None
        return s[:max_len]

    def _clean_list(val: object, *, max_items: int = 8, max_len: int = 80) -> list[str]:
        if not isinstance(val, (list, tuple)):
            return []
        out: list[str] = []
        for item in val:
            s = _clean_str(item, max_len=max_len)
            if s:
                out.append(s)
            if len(out) >= max_items:
                break
        return out

    profile: dict[str, Any] = {}
    risk_budget = _clean_str(obj.get("risk_budget"))
    if risk_budget:
        profile["risk_budget"] = risk_budget.upper()
    hold_time = _clean_str(obj.get("hold_time"))
    if hold_time:
        profile["hold_time"] = hold_time.upper()
    profile["preferred_setups"] = _clean_list(obj.get("preferred_setups"))
    profile["avoid"] = _clean_list(obj.get("avoid"))

    notes = _clean_str(obj.get("notes"), max_len=240)
    if notes:
        profile["notes"] = notes

    # Drop empty profiles.
    if not any(profile.get(k) for k in ("risk_budget", "hold_time", "preferred_setups", "avoid", "notes")):
        return None
    return profile


def _load_global_edge_profile() -> Optional[dict[str, Any]]:
    """Load a single global edge profile (no user-id).

    Sources (first hit wins):
    1) `TNT_EDGE_PROFILE_JSON` env var
    2) SQLite settings key `edge_profile_json`
    """

    raw = (os.getenv("TNT_EDGE_PROFILE_JSON") or "").strip()
    if raw:
        try:
            obj = json.loads(raw)
            return _sanitize_edge_profile(obj)
        except Exception:  # noqa: BLE001
            return None

    try:
        with sqlite3.connect(DB_PATH) as conn:
            migrate_settings_table(conn)
            raw_db = settings_get(conn, "edge_profile_json", "").strip()
        if raw_db:
            obj = json.loads(raw_db)
            return _sanitize_edge_profile(obj)
    except Exception:  # noqa: BLE001
        return None

    return None


def _build_coach_data_snapshot_line(tnt_state: Mapping[str, Any]) -> str:
    """Deterministic one-liner that adds grounding context for /ask.

    IMPORTANT: If data health is not OK or permissions.no_trade is true, we intentionally
    avoid printing prices/levels (standby rules).
    """

    meta = tnt_state.get("meta") if isinstance(tnt_state.get("meta"), dict) else {}
    permissions = tnt_state.get("permissions") if isinstance(tnt_state.get("permissions"), dict) else {}
    levels = tnt_state.get("levels") if isinstance(tnt_state.get("levels"), dict) else {}

    data_health = str(meta.get("data_health") or "UNKNOWN").upper()
    freshness = meta.get("data_freshness_sec")
    no_trade = bool(permissions.get("no_trade"))

    freshness_txt = "n/a"
    if isinstance(freshness, (int, float)):
        freshness_txt = f"{int(freshness)}s"

    if data_health != "OK" or no_trade:
        return f"Data: health {data_health} (freshness {freshness_txt})"

    price = tnt_state.get("price") if isinstance(tnt_state.get("price"), dict) else {}
    last = price.get("last")
    last_f: Optional[float] = None
    if isinstance(last, (int, float)):
        last_f = float(last)

    piv = levels.get("pivots_rth") if isinstance(levels.get("pivots_rth"), dict) else {}
    p = piv.get("P")
    s1 = piv.get("S1")
    r1 = piv.get("R1")

    tech = tnt_state.get("technicals") if isinstance(tnt_state.get("technicals"), dict) else {}
    rsi_14 = tech.get("rsi_14")
    macd_hist = tech.get("macd_hist")
    macd_hist_pct = tech.get("macd_hist_pct")
    tf = tech.get("tf")
    vwap = tech.get("vwap")
    trend = tech.get("trend") if isinstance(tech.get("trend"), dict) else None
    trend_dir = trend.get("direction") if isinstance(trend, dict) else None
    trend_strength = trend.get("strength") if isinstance(trend, dict) else None

    def _fmt_num(val: object, nd: int = 2) -> str:
        try:
            return f"{float(val):.{nd}f}" if val is not None else "n/a"
        except Exception:  # noqa: BLE001
            return "n/a"

    def _fmt(val: object) -> str:
        return fmt_money(float(val)) if isinstance(val, (int, float)) else "n/a"

    last_txt = fmt_money(last_f) if last_f is not None else "n/a"
    tech_bits: list[str] = []
    if tf:
        tech_bits.append(f"tf {tf}")

    if isinstance(vwap, (int, float)) and last_f is not None:
        try:
            delta = float(last_f) - float(vwap)
            side = "above" if delta >= 0 else "below"
            tech_bits.append(f"VWAP {side} {_fmt_num(abs(delta), 2)}")
        except Exception:  # noqa: BLE001
            pass

    if isinstance(trend_dir, str) and trend_dir:
        if isinstance(trend_strength, (int, float)):
            tech_bits.append(f"Trend {trend_dir} ({_fmt_num(trend_strength, 2)})")
        else:
            tech_bits.append(f"Trend {trend_dir}")

    if isinstance(rsi_14, (int, float)):
        tech_bits.append(f"RSI14 {_fmt_num(rsi_14, 1)}")
    if isinstance(macd_hist, (int, float)):
        if isinstance(macd_hist_pct, (int, float)):
            tech_bits.append(f"MACD hist {_fmt_num(macd_hist, 4)} ({_fmt_num(macd_hist_pct, 3)}%)")
        else:
            tech_bits.append(f"MACD hist {_fmt_num(macd_hist, 4)}")

    tvds = tnt_state.get("tvds") if isinstance(tnt_state.get("tvds"), dict) else {}
    tvds_score = tvds.get("score")
    tvds_label = tvds.get("label")

    do_nothing = tnt_state.get("do_nothing_alert") if isinstance(tnt_state.get("do_nothing_alert"), dict) else {}
    do_nothing_enabled = bool(do_nothing.get("enabled"))

    extra_bits: list[str] = []
    if isinstance(tvds_score, (int, float)) and isinstance(tvds_label, str) and tvds_label:
        extra_bits.append(f"TVDS {float(tvds_score):.1f} {tvds_label}")
    if do_nothing_enabled:
        extra_bits.append("DO_NOTHING")

    tech_txt = (" | " + " | ".join(tech_bits)) if tech_bits else ""
    extra_txt = (" | " + " | ".join(extra_bits)) if extra_bits else ""
    return f"Data: last {last_txt} | P {_fmt(p)} | S1 {_fmt(s1)} | R1 {_fmt(r1)}{tech_txt}{extra_txt} | health {data_health} ({freshness_txt})"


_COACH_SYSTEM_ADDENDUM = (
    "COACH MODE OVERRIDE:\n"
    "- Your ONLY factual context is TNT_STATE (authoritative).\n"
    "- Follow the user's requested output format EXACTLY.\n"
    "- Do not include extra sections, headings, or commentary outside the format.\n"
    "- If data is stale/closed, answer in standby mode using recent session history from TNT_STATE if present.\n"
)


async def _compute_recent_daily_trend(symbol: str, *, sessions: int = 5) -> Optional[dict[str, object]]:
    """Return a small trend summary using the last `sessions` daily bars.

    Preference order:
    1) Local DB (tf='1d')
    2) Provider daily bars via UnifiedDataConnector (Polygon)
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    bars: list[dict[str, object]] = []

    # 1) DB bars (fast, offline)
    try:
        rows = get_last_n_bars(sym, tf="1d", n=max(int(sessions) + 3, 8))
        for ts, o, h, l, c in rows:
            try:
                bars.append(
                    {
                        "ts": str(ts),
                        "date": str(ts)[:10],
                        "open": float(o),
                        "high": float(h),
                        "low": float(l),
                        "close": float(c),
                    }
                )
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        bars = []

    # 2) Provider fallback (must be async-safe; do NOT call sync wrappers that use asyncio.run).
    if not bars:
        try:
            from datetime import datetime, timedelta

            from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector

            providers = ["polygon"]
            end = datetime.now()
            # Grab extra days to cover weekends/holidays.
            start = end - timedelta(days=max(int(sessions) * 6, 30))

            async with UnifiedDataConnector(priority=providers) as connector:
                df = await connector.get_historical_bars(sym, start, end, "1d", providers=providers)

            if df is not None and getattr(df, "empty", True) is False:
                cols = {str(c).lower(): c for c in getattr(df, "columns", [])}
                ts_col = cols.get("ts") or cols.get("timestamp")
                o_col = cols.get("open")
                h_col = cols.get("high")
                l_col = cols.get("low")
                c_col = cols.get("close")

                tail = df.tail(max(int(sessions) + 3, 8))
                for _idx, row in tail.iterrows():
                    try:
                        ts_val = row[ts_col] if ts_col is not None else None
                        if ts_val is None and hasattr(row, "name"):
                            ts_val = row.name
                        ts_str = str(ts_val) if ts_val is not None else ""
                        if not ts_str:
                            continue
                        bars.append(
                            {
                                "ts": ts_str,
                                "date": ts_str[:10],
                                "open": float(row[o_col]) if o_col is not None else float("nan"),
                                "high": float(row[h_col]) if h_col is not None else float("nan"),
                                "low": float(row[l_col]) if l_col is not None else float("nan"),
                                "close": float(row[c_col]) if c_col is not None else float("nan"),
                            }
                        )
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            bars = []

    if not bars:
        return None

    # Keep only the last N sessions with valid closes.
    bars = [b for b in bars if isinstance(b.get("close"), (int, float))]
    if len(bars) < 2:
        return None

    tail = bars[-int(sessions):] if len(bars) > int(sessions) else bars
    closes = [float(b["close"]) for b in tail]
    dates = [str(b.get("date") or "") for b in tail]

    first = closes[0]
    last = closes[-1]
    ret_pct = None
    if first not in (0.0, -0.0):
        try:
            ret_pct = (last / first - 1.0) * 100.0
        except Exception:  # noqa: BLE001
            ret_pct = None

    # Simple per-session slope (points per day).
    slope_pts = (last - first) / max(len(closes) - 1, 1)

    trend = "FLAT"
    if ret_pct is not None:
        if ret_pct >= 0.75:
            trend = "UP"
        elif ret_pct <= -0.75:
            trend = "DOWN"

    return {
        "symbol": sym,
        "sessions": len(closes),
        "dates": dates,
        "closes": closes,
        "return_pct": ret_pct,
        "slope_pts_per_session": slope_pts,
        "trend": trend,
        "last_bar": tail[-1],
        "prev_bar": tail[-2] if len(tail) >= 2 else None,
        "source": "db" if get_latest_daily_bar(sym) is not None else "provider",
    }


def _build_trend_based_coach_response(symbol: str, trend: Mapping[str, Any]) -> str:
    sym = (symbol or "").strip().upper() or "UNKNOWN"

    tr = str(trend.get("trend") or "FLAT").upper()
    sessions = trend.get("sessions")
    ret_pct = trend.get("return_pct")
    last_bar = trend.get("last_bar") if isinstance(trend.get("last_bar"), Mapping) else {}
    prev_bar = trend.get("prev_bar") if isinstance(trend.get("prev_bar"), Mapping) else {}

    def _fmt_num(x: object) -> str:
        try:
            return f"{float(x):.2f}"
        except Exception:  # noqa: BLE001
            return "n/a"

    last_close = _fmt_num(last_bar.get("close"))
    prev_high = _fmt_num(prev_bar.get("high"))
    prev_low = _fmt_num(prev_bar.get("low"))

    # Use pivots if available (DB-derived) for cleaner conditions.
    piv = None
    try:
        piv_info = get_latest_daily_pivots(sym)
        if isinstance(piv_info, dict) and isinstance(piv_info.get("piv"), dict):
            piv = piv_info.get("piv", {}).get("P")
    except Exception:  # noqa: BLE001
        piv = None
    pivot_txt = _fmt_num(piv)

    ret_txt = "n/a"
    if isinstance(ret_pct, (int, float)):
        ret_txt = f"{float(ret_pct):+.2f}%"

    sess_txt = str(sessions) if isinstance(sessions, int) else "recent"

    has_pivot = piv is not None

    if tr == "UP":
        answer = (
            f"Conditional bullish tilt: last {sess_txt} daily closes are trending up ({ret_txt}); "
            + (f"tomorrow depends on holding/reclaiming Pivot {pivot_txt}." if has_pivot else "tomorrow depends on holding the prior range and continuing higher.")
        )
        bull = (
            f"Acceptance above Pivot {pivot_txt} with strength over prior high {prev_high}."
            if has_pivot
            else f"Strength over prior high {prev_high} with follow-through."
        )
        bear = (
            f"Break below prior low {prev_low} or failure to reclaim Pivot {pivot_txt}."
            if has_pivot
            else f"Break and hold below prior low {prev_low}."
        )
        invalid = (
            f"Breakout above {prev_high} that reverses back below Pivot {pivot_txt}."
            if has_pivot
            else f"Breakout above {prev_high} that fails and returns into the prior range."
        )
        idle = "First hour chops around Pivot with no clean direction."
    elif tr == "DOWN":
        answer = (
            f"Conditional bearish tilt: last {sess_txt} daily closes are trending down ({ret_txt}); "
            + (f"tomorrow depends on rejecting Pivot {pivot_txt}." if has_pivot else "tomorrow depends on continued rejection of the prior range and making lower lows.")
        )
        bull = (
            f"Reclaim Pivot {pivot_txt} and hold above it; then watch for continuation above {prev_high}."
            if has_pivot
            else f"Reclaim and hold above prior high {prev_high}."
        )
        bear = (
            f"Acceptance below Pivot {pivot_txt} and continuation under prior low {prev_low}."
            if has_pivot
            else f"Continuation under prior low {prev_low}."
        )
        invalid = (
            f"Reclaim and hold back above Pivot {pivot_txt} after a breakdown."
            if has_pivot
            else f"Breakdown that snaps back above the prior range."
        )
        idle = "No clean reclaim/reject at Pivot in the first 30–60 minutes."
    else:
        answer = (
            f"Neutral/range tilt: last {sess_txt} daily closes are mixed/flat ({ret_txt}); "
            + (f"tomorrow is about a clean break from Pivot {pivot_txt}." if has_pivot else "tomorrow is about a clean break from the prior day range.")
        )
        bull = (
            f"Hold above Pivot {pivot_txt} and break/hold above {prev_high}."
            if has_pivot
            else f"Break/hold above prior high {prev_high}."
        )
        bear = (
            f"Lose Pivot {pivot_txt} and break/hold below {prev_low}."
            if has_pivot
            else f"Break/hold below prior low {prev_low}."
        )
        invalid = "Any breakout that fails and returns to the middle of the range."
        idle = "Price stays inside the prior day range with no expansion."

    # Last close is included as a gentle anchor (it is factual from bars).
    answer = f"{answer} (Last close: {last_close})"

    return "\n".join(
        [
            f"Answer: {answer}",
            "Bullish only if:",
            f"- {bull}",
            "Bearish if:",
            f"- {bear}",
            "Invalidation:",
            f"- {invalid}",
            "Do nothing if:",
            f"- {idle}",
        ]
    )


def _build_coach_error_response(symbol: str, reason: str) -> str:
    symbol_clean = (symbol or "").strip().upper() or "UNKNOWN"
    reason_clean = (reason or "coach error").strip().split("\n", 1)[0][:160]
    lines = [
        f"⚠️ Coach temporarily unavailable for {symbol_clean}.",
        f"Details: {reason_clean}.",
        "Posting the latest /analyze output so you still have actionable levels.",
    ]
    return "\n".join(lines)


def _normalize_coach_output(raw: str) -> tuple[str, bool]:
    answer: Optional[str] = None
    bull_lines: list[str] = []
    bear_lines: list[str] = []
    invalidation_line: Optional[str] = None
    do_nothing_line: Optional[str] = None
    section: Optional[str] = None

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("answer"):
            answer = line.split(":", 1)[1].strip() if ":" in line else line
            section = None
            continue
        if lower.startswith("bullish only if"):
            section = "bull"
            continue
        if lower.startswith("bearish if"):
            section = "bear"
            continue
        if lower.startswith("invalidation"):
            section = "invalid"
            continue
        if lower.startswith("do nothing if"):
            section = "idle"
            continue

        content = line.lstrip("-• ").strip()
        if not content:
            continue
        if section == "bull":
            bull_lines.append(content)
        elif section == "bear":
            bear_lines.append(content)
        elif section == "invalid" and not invalidation_line:
            invalidation_line = content
        elif section == "idle" and not do_nothing_line:
            do_nothing_line = content

    if not (answer and bull_lines and bear_lines and invalidation_line and do_nothing_line):
        return "", False

    bull_norm = _bounded_list(bull_lines, min_len=1, max_len=2, filler="Wait for /analyze once live data resumes.")
    bear_norm = _bounded_list(bear_lines, min_len=1, max_len=2, filler="Wait for /analyze once live data resumes.")

    composed_lines: list[str] = [f"Answer: {answer}", "Bullish only if:"]
    for entry in bull_norm:
        composed_lines.append(f"- {entry}")
    composed_lines.append("Bearish if:")
    for entry in bear_norm:
        composed_lines.append(f"- {entry}")
    composed_lines.append("Invalidation:")
    composed_lines.append(f"- {invalidation_line}")
    composed_lines.append("Do nothing if:")
    composed_lines.append(f"- {do_nothing_line}")

    return "\n".join(composed_lines), True


async def _call_openai_coach(
    *,
    question: str,
    render: RenderedPost,
    analysis_payload: Mapping[str, Any],
    recent_daily_trend: Optional[Mapping[str, Any]] = None,
    options_chain_summary: Optional[Mapping[str, Any]] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    tnt_state = build_tnt_state_from_analysis_payload(analysis_payload).state
    if recent_daily_trend is not None:
        try:
            ctx = tnt_state.get("context")
            if isinstance(ctx, dict):
                ctx["recent_daily_trend"] = dict(recent_daily_trend)
        except Exception:  # noqa: BLE001
            pass
    if options_chain_summary is not None:
        try:
            tnt_state["options_chain"] = dict(options_chain_summary)
        except Exception:  # noqa: BLE001
            pass

    # Global (non-userid) edge profile to guide coaching style.
    try:
        profile = _load_global_edge_profile()
        if profile:
            tnt_state["edge_profile"] = profile
    except Exception:  # noqa: BLE001
        pass

    # Add deterministic edge/risk fields after all context is injected.
    try:
        enrich_tnt_state(tnt_state)
    except Exception:  # noqa: BLE001
        pass
    user_text = _build_coach_user_text(question)
    result = await call_tnt_agent_async(
        tnt_state=tnt_state,
        user_text=user_text,
        label="coach",
        model=_coach_model_name(),
        max_output_tokens=_coach_max_tokens(),
        temperature=0.35,
        system_addendum=_COACH_SYSTEM_ADDENDUM,
    )
    return (result.text or "").strip(), result.prompt_sha256, result.tnt_state_sha256


async def _run_analyze_for_symbol(symbol: str) -> tuple[str, dict[str, Any]]:
    render, _ = await _fetch_on_demand_render(symbol, allow_cache=True)
    payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
    return render.text, payload


async def run_analyze_then_coach(symbol: str, question: str) -> tuple[str, RenderedPost, str, bool, float]:
    started = time_lib.time()
    sym = (symbol or "").strip().upper()

    # Global, non-symbol questions: answer deterministically when possible.
    # Example: "which stock had the most volume today?" can be answered via Polygon/Massive grouped daily
    # even if intraday bars are stale and /analyze is in stand-down.
    if _is_volume_leader_question(question):
        try:
            answer = await _answer_volume_leader_question(question)
        except Exception:  # noqa: BLE001
            answer = None
        latency_ms = (time_lib.time() - started) * 1000.0
        if answer:
            cached = _get_cached_analyze(sym)
            render = cached.render if cached is not None else RenderedPost(text="", agent_payload={})
            return answer, render, "ok", False, latency_ms

    if _is_market_session_question(question):
        latency_ms = (time_lib.time() - started) * 1000.0
        cached = _get_cached_analyze(sym)
        render = cached.render if cached is not None else RenderedPost(text="", agent_payload={})
        return _answer_market_session_question(), render, "ok", False, latency_ms

    if _is_watchlist_question(question):
        latency_ms = (time_lib.time() - started) * 1000.0
        cached = _get_cached_analyze(sym)
        render = cached.render if cached is not None else RenderedPost(text="", agent_payload={})
        return _answer_watchlist_question(), render, "ok", False, latency_ms

    cached_before = _get_cached_analyze(sym)
    analyze_text, payload = await _run_analyze_for_symbol(sym)
    cache_entry = _get_cached_analyze(sym) or cached_before
    cache_hit = cache_entry is not None and cache_entry is cached_before and cached_before is not None

    if cache_entry is not None:
        render = cache_entry.render
    else:
        render = RenderedPost(text=analyze_text, agent_payload=payload)

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    stand_down = bool(meta.get("stand_down"))
    missing_sections = meta.get("missing_sections") if isinstance(meta, dict) else []
    if isinstance(missing_sections, (list, tuple)) and missing_sections:
        stand_down = True

    # Build a small recent-history trend block (daily closes) so /ask can answer even when markets are closed.
    recent_trend = await _compute_recent_daily_trend(sym, sessions=5)

    # If the user explicitly asked for the trend, answer deterministically (avoid LLM).
    if _is_trend_question(question) and recent_trend is not None:
        coach_text = _build_trend_based_coach_response(sym, recent_trend)
        latency_ms = (time_lib.time() - started) * 1000.0
        return coach_text, render, "ok", cache_hit, latency_ms

    # Deterministic snapshot for /ask (only shown when data is healthy/tradable).
    try:
        tnt_state_for_snapshot = build_tnt_state_from_analysis_payload(payload or {}).state
    except Exception:  # noqa: BLE001
        tnt_state_for_snapshot = {}

    # Symbol-specific preplanned answers that depend on TNT_STATE.
    if _is_key_levels_question(question):
        try:
            preplanned = _answer_key_levels_question(sym, tnt_state_for_snapshot)
        except Exception:  # noqa: BLE001
            preplanned = None
        if preplanned:
            latency_ms = (time_lib.time() - started) * 1000.0
            return preplanned, render, "ok", cache_hit, latency_ms

    # Options chain microstructure (Polygon/Massive only). Cache aggressively.
    options_micro = None
    cached_opt = _get_cached_options_micro(sym)
    if cached_opt is not None:
        options_micro = cached_opt.packet
    else:
        try:
            df = await _fetch_polygon_options_chain_df(sym)
            if df is not None and getattr(df, "empty", True) is False:
                api_key, _base, provider_label = _polygon_key_and_base()
                now_et = _now_et()
                summary = summarize_options_chain(
                    df,
                    underlying=sym,
                    asof_et=now_et.isoformat(),
                    provider=provider_label,
                    max_per_side=_env_int("OPTIONS_MICRO_MAX_PER_SIDE", 8),
                ).payload
                options_micro = summary
                _set_cached_options_micro(sym, summary)
        except Exception:  # noqa: BLE001
            options_micro = None

    # If markets are closed / analysis is gated, we still want /ask to answer.
    # Prefer a deterministic trend-based answer over any standby analyze block.
    if stand_down and recent_trend is not None:
        coach_text = _build_trend_based_coach_response(sym, recent_trend)
        latency_ms = (time_lib.time() - started) * 1000.0
        return coach_text, render, "ok_standby", cache_hit, latency_ms

    # Even in stand-down (closed markets / missing sections), try the coach so /ask still answers.
    # The prompt enforces standby behavior (no invented prices/levels) when data isn't ready.

    prompt_sha: Optional[str] = None
    state_sha: Optional[str] = None
    try:
        raw_reply, prompt_sha, state_sha = await _call_openai_coach(
            question=question,
            render=render,
            analysis_payload=payload or {},
            recent_daily_trend=recent_trend,
            options_chain_summary=options_micro,
        )
        coach_text, ok = _normalize_coach_output(raw_reply)
        if stand_down:
            if ok:
                status = "ok_standby"
            else:
                # If the LLM drifts from the strict format, fall back to a deterministic
                # trend-based answer using recent daily history.
                if recent_trend is not None:
                    coach_text = _build_trend_based_coach_response(sym, recent_trend)
                    ok = True
                    status = "ok_standby"
                else:
                    coach_text = _build_coach_limited_response(sym, meta or {})
                    status = "limited"
        else:
            status = "ok" if ok else "format_error"
        if not ok:
            coach_text = _build_coach_error_response(sym, "Invalid coach format")
    except Exception as exc:  # noqa: BLE001
        print(f"[COACH][ERROR] {sym.upper()}: {exc}")
        if stand_down:
            if recent_trend is not None:
                coach_text = _build_trend_based_coach_response(sym, recent_trend)
                status = "ok_standby"
            else:
                coach_text = _build_coach_limited_response(sym, meta or {})
                status = "limited"
        else:
            coach_text = _build_coach_error_response(sym, str(exc))
            status = "error"

    if status in {"error", "format_error"}:
        try:
            msg = _format_ops_event(
                tag="llm_fail",
                label="ask",
                symbols=[sym.upper()],
                status=status,
                contracts={"agent": AUTOPOST_CONTRACT_VERSION, "chart": "1.0"},
                prompt_sha256=prompt_sha,
                tnt_state_sha256=state_sha,
                violations=["COACH_FAILED" if status == "error" else "COACH_FORMAT_ERROR"],
                audit_path=None,
            )
            await _notify_ops_quiet(client=bot, message=msg)
        except Exception:
            pass

    latency_ms = (time_lib.time() - started) * 1000.0

    # Add deterministic market snapshot when we are not in stand-down.
    try:
        if status == "ok" and isinstance(tnt_state_for_snapshot, dict) and tnt_state_for_snapshot:
            snapshot_line = _build_coach_data_snapshot_line(tnt_state_for_snapshot)
            # Only include prices/levels when the snapshot says health is OK and no_trade is false.
            if snapshot_line and "health OK" in snapshot_line:
                coach_text = f"{snapshot_line}\n{coach_text}".strip()
    except Exception:  # noqa: BLE001
        pass

    # Always anchor with Numeric Bias Pack (best-effort) before any coach text.
    try:
        from delivery.numeric_bias_pack import format_numeric_bias_pack_from_analysis_payload

        pack = format_numeric_bias_pack_from_analysis_payload(analysis_payload=payload or {})
        if pack:
            coach_text = f"{pack}\n\n{coach_text}".strip()
    except Exception:  # noqa: BLE001
        pass

    return coach_text, render, status, cache_hit, latency_ms


async def ai_render_trade_context(openai_client, packet: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, err) using packet + AI narrative (AI supplies only plan + notes)."""

    if not _ai_enabled():
        return None, "AI disabled"

    def _render(packet: dict, payload: dict) -> str:
        symbol = str(packet.get("symbol") or "?").upper()
        price_tf = PRICE_TF_LABEL
        price_val = packet.get("current_price")
        price_ts = packet.get("current_price_ts")
        pivot_val = packet.get("pivot")

        def _safe_float(val: object) -> Optional[float]:
            try:
                return float(val) if val is not None else None
            except Exception:  # noqa: BLE001
                return None

        price_float = _safe_float(price_val)
        pivot_float = _safe_float(pivot_val)

        bias_label = str(packet.get("bias") or "NEUTRAL").upper()
        confirm_label = str(packet.get("bias_confirm") or "UNKNOWN").upper()
        conviction_label = str(packet.get("conviction") or "LOW").upper()
        regime_label = str(packet.get("regime") or "UNKNOWN").upper()

        bias_note = _coerce_ai_string(payload.get("bias"))
        regime_note = _coerce_ai_string(payload.get("regime"))

        bull_plan = _coerce_ai_lines(payload.get("plan_bull"))
        bear_plan = _coerce_ai_lines(payload.get("plan_bear"))
        do_nothing = _coerce_ai_lines(payload.get("do_nothing"))

        lines: list[str] = []
        lines.append(f"🚨 **{symbol} — Trade Context**")
        lines.append(fmt_last_price(symbol, price_float, price_ts, price_tf))

        if packet.get("educational_only"):
            edge_meta = packet.get("edge")
            if isinstance(edge_meta, (int, float)):
                lines.append(f"⚠️ **LOW EDGE / EDUCATIONAL** (edge {edge_meta:.3f})")
            else:
                lines.append("⚠️ **LOW EDGE / EDUCATIONAL**")
            lines.append("")

        if price_float is not None and pivot_float is not None:
            diff = price_float - pivot_float
            direction = "above" if diff >= 0 else "below"
            lines.append(
                f"📏 **vs Pivot:** {fmt_signed(diff)} pts ({direction} {fmt_money(pivot_float)})"
            )
        else:
            lines.append("📏 **vs Pivot:** n/a")

        lines.append("")
        lines.append(
            f"**Bias:** **{bias_label}** | **Confirmation:** **{confirm_label}** | **Conviction:** **{conviction_label}**"
        )
        if bias_note:
            lines.append(f"• {bias_note}")
        lines.append(f"🧭 Regime: **{regime_label}**")
        if regime_note:
            lines.append(f"• {regime_note}")

        lines.append("")
        lines.append("📐 Key Levels (RTH):")
        if pivot_float is not None:
            lines.append(f"• Pivot: **{fmt_money(pivot_float)}**")
        else:
            lines.append("• Pivot: n/a")

        r_levels = packet.get("r_levels") if isinstance(packet.get("r_levels"), dict) else {}
        s_levels = packet.get("s_levels") if isinstance(packet.get("s_levels"), dict) else {}

        def _fmt_levels(label: str, lvl_dict: dict) -> Optional[str]:
            parts: list[str] = []
            for key in ("R1", "R2", "R3") if label == "Upside" else ("S1", "S2", "S3"):
                val = lvl_dict.get(key)
                if val is None:
                    continue
                try:
                    parts.append(f"{key} {fmt_money(val)}")
                except Exception:  # noqa: BLE001
                    continue
            if not parts:
                return None
            return f"• {label}: " + " | ".join(parts)

        r_line = _fmt_levels("Upside", r_levels)
        s_line = _fmt_levels("Downside", s_levels)
        if r_line:
            lines.append(r_line)
        else:
            lines.append("• Upside: n/a")
        if s_line:
            lines.append(s_line)
        else:
            lines.append("• Downside: n/a")

        lines.append("")
        lines.append("🧠 How Pros Would Trade It:")

        if bull_plan:
            for idx, item in enumerate(bull_plan):
                label = "Bull" if idx == 0 else "Bull cont."
                lines.append(f"• {label}: {item}")
        else:
            lines.append("• Bull: await clean reclaim of Pivot before risk on")

        if bear_plan:
            for idx, item in enumerate(bear_plan):
                label = "Bear" if idx == 0 else "Bear cont."
                lines.append(f"• {label}: {item}")
        else:
            lines.append("• Bear: wait for breakdown below Pivot with confirmation")

            VALID_MODES = {"strict", "insights"}


            def get_trade_context_mode() -> str:
                with db_connect() as conn:
                    raw = settings_get(conn, "trade_context_mode", None)
                    if raw is None:
                        raw = settings_get(conn, "analysis_mode", "insights")
                    mode = (raw or "insights").strip().lower()
                if mode not in VALID_MODES:
                    mode = "insights"
                return mode


            def set_trade_context_mode(mode: str) -> None:
                mode_clean = (mode or "insights").strip().lower()
                if mode_clean not in VALID_MODES:
                    raise ValueError(f"Invalid mode {mode_clean!r}; must be one of {sorted(VALID_MODES)}")
                with db_connect() as conn:
                    settings_set(conn, "trade_context_mode", mode_clean)
                    settings_set(conn, "analysis_mode", mode_clean)


            get_analysis_mode = get_trade_context_mode
            set_analysis_mode = set_trade_context_mode
        lines.append("")
        lines.append("_Not financial advice._")
        return "\n".join(lines)

    try:
        tnt_state = build_tnt_state_from_packet(packet).state
        user_text = (
            "Task: Draft ONLY the narrative fields for a trade context.\n"
            "Follow the schema exactly and never output prose outside JSON.\n"
            "Schema (all fields required):\n"
            '{"bias": string, "regime": string, "plan_bull": [string], "plan_bear": [string], "do_nothing": [string]}\n'
            "Rules:\n"
            "- Use ONLY facts from TNT_STATE. Do NOT invent prices, levels, times, or news.\n"
            "- Keep each string under 160 characters.\n"
            "- Focus on rationale and execution notes; reference named levels (Pivot, R1, S1) instead of creating new numbers.\n"
            "- Bullet strings should be behavioral and conditional; never provide execution verbs.\n"
            "- Maintain a calm, firm, professional tone.\n"
            "Respond with valid JSON only."
        )
        result = await asyncio.wait_for(
            call_tnt_agent_async(
                tnt_state=tnt_state,
                user_text=user_text,
                label="trade_context_json",
                model=_ai_model(),
                max_output_tokens=400,
                temperature=0.2,
            ),
            timeout=_ai_timeout(),
        )
        try:
            packet["tnt_state_sha256"] = result.tnt_state_sha256
            packet["tnt_prompt_sha256"] = result.prompt_sha256
        except Exception:  # noqa: BLE001
            pass
        raw = result.text
    except Exception as exc:  # noqa: BLE001
        return None, f"AI error: {exc}"

    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.strip("`")
        raw = raw.strip()

    if "{" in raw and "}" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        raw = raw[start:end]

    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return None, f"AI parse error: {exc}"

    if not isinstance(payload, dict):
        return None, "AI response not dict"

    for required in ("bias", "regime", "plan_bull", "plan_bear", "do_nothing"):
        if required not in payload:
            return None, f"AI missing field: {required}"

    try:
        text = _render(packet, payload)
    except Exception as exc:  # noqa: BLE001
        return None, f"render error: {exc}"

    max_chars = _ai_max_chars()
    text = text.strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."

    return text, None

EARNINGS_PROVIDER = os.getenv("EARNINGS_PROVIDER", "earningsapi").lower()
EARNINGS_API_KEY = os.getenv("EARNINGS_API_KEY", "")
EARNINGS_WATCHLIST = [
    s.strip().upper()
    for s in os.getenv(
        "EARNINGS_WATCHLIST",
        "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA",
    ).split(",")
    if s.strip()
]

MACRO_EVENTS_FILE = os.getenv("MACRO_EVENTS_FILE", "data/macro_events.csv")

BRIEF_STATE_PATH = os.getenv("BRIEF_STATE_PATH", "db/brief_state.json")
BOT_ALERT_CHANNEL_ID = int(os.getenv("BOT_ALERT_CHANNEL_ID", "0") or "0")

ANALYZE_WORDS = ("analyze", "analysis", "setup", "thoughts", "outlook", "view")
TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
ANALYZE_SYNONYMS = {
    "TESLA": "TSLA",
    "GOOGLE": "GOOGL",
    "GOOG": "GOOGL",
    "ALPHABET": "GOOGL",
    "FACEBOOK": "META",
    "META": "META",
    "APPLE": "AAPL",
    "AMAZON": "AMZN",
    "MICROSOFT": "MSFT",
    "NVDA": "NVDA",
    "NVIDIA": "NVDA",
    "SPX": "I:SPX",
    "VIX": "I:VIX",

    # Synthetic futures proxies (ETF-based; Polygon-supported).
    # These are convenience aliases only — not real futures symbols.
    "ES": "SPY",
    "NQ": "QQQ",
    "RTY": "IWM",
    # Rates proxy (10Y note futures conceptually maps to duration).
    "ZN": "TLT",
    # Crude proxy.
    "CL": "USO",
}

QUESTION_STOPWORDS = {
    "IS",
    "ARE",
    "THE",
    "WHAT",
    "WHATS",
    "SHOULD",
    "IM",
    "I",
    "YOU",
    "WE",
    "THEY",
    "IT",
    "THIS",
    "THAT",
    "FOR",
    "TO",
    "A",
    "AN",
    "ON",
    "IN",
    "AT",
    "OF",
    "DO",
    "BE",
    "BULLISH",
    "BEARISH",
    "PLAN",
    "RIGHT",
    "NOW",
    "CURRENTLY",
    "TRADING",
    "TRADE",
    "PLAY",
    "IDEA",
    "UP",
    "DOWN",
    "HELP",
    "PLEASE",
}

_SYMBOL_RE = re.compile(r"\b([A-Z]{1,5})(?:\b|[^\w])")
_STOP_TOKENS = {
    "I",
    "A",
    "AN",
    "THE",
    "IS",
    "ARE",
    "WAS",
    "WERE",
    "TO",
    "OF",
    "ON",
    "IN",
    "TSLA?",
    "SPY?",
    "QQQ?",
}


def _normalize_symbol_token(token: str) -> Optional[str]:
    raw = (token or "").strip().upper()
    if not raw:
        return None

    # Preserve Polygon index ticker format (e.g. I:SPX, I:VIX).
    m = re.match(r"^I\s*[: ]\s*([A-Z0-9]{1,6})$", raw)
    if m:
        return f"I:{m.group(1)}"

    cleaned = re.sub(r"[^A-Z0-9]", "", raw)
    if not cleaned:
        return None
    if cleaned in QUESTION_STOPWORDS:
        return None
    if len(cleaned) > 6:
        return None
    return ANALYZE_SYNONYMS.get(cleaned, cleaned)


def _detect_symbol_in_text(text: str) -> Optional[str]:
    if not text:
        return None
    upper_text = text.upper()
    candidates = list(TICKER_RE.findall(upper_text))
    cash_tags = re.findall(r"\$([A-Z]{1,5})", upper_text)
    candidates.extend(cash_tags)
    for candidate in candidates:
        symbol = _normalize_symbol_token(candidate)
        if symbol:
            return symbol
    return None


def _extract_symbol_from_text(text: str) -> Optional[str]:
    symbol = _detect_symbol_in_text(text)
    if symbol:
        return symbol
    if not text:
        return None

    upper_text = text.upper()
    hits = _SYMBOL_RE.findall(upper_text)
    cleaned: list[str] = []
    for hit in hits:
        candidate = hit.strip().replace("$", "")
        if not candidate:
            continue
        if candidate in _STOP_TOKENS:
            continue
        if len(candidate) == 1:
            continue
        cleaned.append(ANALYZE_SYNONYMS.get(candidate, candidate))

    for preferred in ("SPY", "QQQ", "IWM", "DIA"):
        if preferred in cleaned:
            return preferred

    return cleaned[0] if cleaned else None


def timeframe_block_lines(*, execution: str, structure: str, context: str) -> list[str]:
    return [
        "⏱ Timeframes:",
        f"• Execution: {execution}",
        f"• Structure: {structure}",
        f"• Context: {context}",
    ]


def extract_analysis_request(text: str):
    t = (text or "").strip()
    if not t:
        return None
    tl = t.lower()
    if not any(w in tl for w in ANALYZE_WORDS):
        return None
    ticks = TICKER_RE.findall(t.upper())
    if not ticks:
        return None
    primary = ticks[0]
    return ANALYZE_SYNONYMS.get(primary, primary)


def build_mega_cap_analysis(symbol: str) -> str | None:
    sym = symbol.upper()

    dp = get_latest_daily_pivots(sym)
    if not dp or "piv" not in dp:
        return None

    piv = dp["piv"]
    P = float(piv["P"])
    R1 = float(piv["R1"])
    S1 = float(piv["S1"])

    sig = get_latest_signal(sym)
    v = get_vix_context("1m")
    hourly_state = _hourly_regime_state(sym)
    structure_stale = hourly_state is None

    vix_gate = None
    if v and "level" in v:
        try:
            vix_gate = vix_gating_action(float(v["level"]))
        except Exception:  # noqa: BLE001
            vix_gate = None
    if not vix_gate:
        vix_gate = {"mode": "OK", "reason": ""}

    has_signal = bool(sig)
    prob_up = float(sig["prob_up"]) if has_signal else None
    edge = abs(prob_up - 0.5) if prob_up is not None else 0.0
    bias_label = (
        "BULLISH"
        if prob_up is not None and prob_up >= 0.53
        else "BEARISH"
        if prob_up is not None and prob_up <= 0.47
        else "NEUTRAL"
    )
    bias_dir = 0
    if prob_up is not None:
        if prob_up >= 0.53:
            bias_dir = 1
        elif prob_up <= 0.47:
            bias_dir = -1

    regime_info = derive_regime(prob_up, hourly_state)
    regime_label = regime_info["label"]
    aligned = has_signal and regime_info.get("regime_dir") and regime_info.get("regime_dir") == bias_dir

    conviction, conviction_notes = determine_conviction(
        edge=edge,
        aligned=bool(aligned),
        structure_stale=structure_stale,
        has_signal=has_signal,
        gate_mode=vix_gate.get("mode", "OK"),
    )

    structure_label = "60m"
    if structure_stale:
        structure_label += " (STALE)"

    timeframe_lines = timeframe_block_lines(
        execution=PRICE_TF_LABEL,
        structure=structure_label,
        context="1D",
    )

    lines: list[str] = []
    lines.append(f"📊 Asset: {sym}")

    badge_emoji, badge_text = classify_regime_badge(regime_label, conviction, vix_gate.get("mode", "OK"))
    lines.append(f"{badge_emoji} {badge_text}")
    lines.extend(timeframe_lines)
    if BIAS_CONFIRM_ENABLED:
        lines.append("")
        lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    lines.append("")

    lines.append(f"🧭 Regime: {regime_label}")
    for reason in regime_info.get("reasons", []):
        lines.append(f"• {reason}")

    lines.append(f"🎯 Conviction: {conviction} (edge {edge:.3f})")
    for note in conviction_notes:
        lines.append(f"• {note}")

    if v and "level" in v:
        lvl = float(v["level"])
        gate_mode = vix_gate.get("mode", "OK").upper()
        gate_reason = vix_gate.get("reason", "")
        vix_line = f"VIX {lvl:.2f}"
        if gate_mode != "OK":
            vix_line += f" | gate {gate_mode}"
            if gate_reason:
                vix_line += f" ({gate_reason})"
        lines.append(vix_line)

    lines.append("")
    if has_signal and prob_up is not None:
        prob_down = 1.0 - prob_up
        lines.append(
            f"📈 Signal: Prob↑ {prob_up:.3f} | Prob↓ {prob_down:.3f} | Horizon {HORIZON_MIN}m | Model {sig['model_version']}"
        )
        lines.append(f"Signal ts: {sig['ts']}")
    else:
        lines.append("📈 Signal: unavailable (levels-only mode)")

    lines.append("")
    lines.append("📐 Key Levels:")
    lines.append(f"P {P:.2f} | R1 {R1:.2f} | S1 {S1:.2f}")

    daily_bias = compute_daily_bias(sym, dp)
    if daily_bias:
        lines.append("")
        lines.append(f"📊 Daily Bias: {daily_bias['label']} ({daily_bias['score']:+.2f})")
        lines.append(f"Confidence: {daily_bias['confidence']} (as of {daily_bias['as_of']} ET)")
        for item in daily_bias.get("context", []):
            lines.append(f"• {item}")

    weekly_structure = compute_weekly_structure(sym)
    if weekly_structure:
        lines.append("")
        lines.append("🧱 Weekly Structure")
        if weekly_structure.get("vwap") is not None:
            lines.append(f"• VWAP {weekly_structure['vwap']:.2f}")
        else:
            lines.append("• VWAP unavailable")
        if weekly_structure.get("prior_high") is not None and weekly_structure.get("prior_low") is not None:
            lines.append(f"• Prior High {weekly_structure['prior_high']:.2f}")
            lines.append(f"• Prior Low {weekly_structure['prior_low']:.2f}")
        else:
            lines.append("• Prior week levels unavailable")

    regime_lines = build_hourly_regime_block(sym, hourly_state)
    if regime_lines:
        lines.append("")
        lines.extend(regime_lines)

    if conviction == "LOW":
        playbook = {
            "selection": "NO-TRADE",
            "orientation": None,
            "reasons": ["Conviction LOW → wait for clarity"],
        }
    else:
        playbook = select_active_playbook(
            prob_up if prob_up is not None else 0.5,
            bias_label,
            conviction,
            hourly_state,
            vix_gate,
        )

    if playbook:
        lines.append("")
        orientation = playbook.get("orientation")
        if orientation:
            lines.append(f"🎯 Active Playbook: {playbook['selection']} ({orientation})")
        else:
            lines.append(f"🎯 Active Playbook: {playbook['selection']}")
        lines.append("Reason:")
        for reason in playbook.get("reasons", []):
            lines.append(f"• {reason}")

    target_level = f"R1 {R1:.2f}" if bias_dir > 0 else f"S1 {S1:.2f}" if bias_dir < 0 else f"Pivot {P:.2f}"
    if conviction == "LOW":
        do_nothing_clause = (
            f"🚫 Do nothing unless conviction improves (currently LOW) and price resolves beyond {target_level} with volume support."
        )
    else:
        do_nothing_clause = (
            f"🚫 Do nothing unless price breaks and retests {target_level} with confirming volume."
        )

    lines.append("")
    lines.append(do_nothing_clause)
    lines.append("")
    lines.append("_Not financial advice._")

    return "\n".join(lines)


def maybe_ai_analysis(symbol: str) -> Optional[str]:
    """Wrapper to build the preferred AI-style analysis, if data supports it."""

    try:
        return build_mega_cap_analysis(symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"[ANALYZE] builder error for {symbol}: {exc}")
        return None


def safe_json_loads(data: object) -> dict:
    if isinstance(data, dict):
        return dict(data)
    if data is None:
        return {}
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001 - fall through to return {}
            return {}
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            try:
                return json.loads(text.replace("'", '"'))
            except Exception:  # noqa: BLE001 - tolerate malformed payloads
                return {}
    return {}


def edge_from_prob(prob_up: float) -> float:
    return float(math.fabs(prob_up - 0.5))


def conviction_from_edge(edge: float) -> str:
    if edge < OPT_NO_TRADE_MIN_EDGE:
        return "LOW"
    if edge < OPT_HIGH_CONV_EDGE:
        return "MED"
    return "HIGH"


def bias_from_prob(prob_up: float) -> str:
    if prob_up >= 0.53:
        return "BULL"
    if prob_up <= 0.47:
        return "BEAR"
    return "NEUTRAL"


def clamp_lines(lines: list[str], max_lines: int) -> str:
    limit = max(1, max_lines)
    if len(lines) <= limit:
        return "\n".join(lines)
    head = lines[: limit - 1]
    return "\n".join(head + ["…(trimmed)"])


def fmt_num(x, nd: int = 2) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except Exception:  # noqa: BLE001 - fall back if conversion fails
        return "n/a"


def load_brief_state() -> Dict[str, object]:
    try:
        if os.path.exists(BRIEF_STATE_PATH):
            with open(BRIEF_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_brief_state(state: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(BRIEF_STATE_PATH), exist_ok=True)
    with open(BRIEF_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


async def notify_ops(msg: str) -> None:
    try:
        if BOT_ALERT_CHANNEL_ID:
            ch = bot.get_channel(BOT_ALERT_CHANNEL_ID)
            if ch:
                await ch.send(f"⚠️ **BOT ALERT**\n{msg}")
    except Exception:  # noqa: BLE001
        pass


async def safe_send(
    channel,
    text: str,
    *,
    kind: str = "analysis",
    symbol: str = "",
    pivots: Optional[dict] = None,
    analysis_mode: str = "db",
    output_mode: str = "strict",
    label: str = "",
    files: Optional[list[tuple[str, bytes]]] = None,
):

    if not QUALITY_GATE_ENABLED:
        if files:
            discord_files = [discord.File(fp=io.BytesIO(blob), filename=name) for name, blob in files]
            return await channel.send(text, files=discord_files)
        return await channel.send(text)

    if kind == "analysis":
        ok, reason = validate_analysis_message(text, label)
    else:
        ok, reason = True, "ok"

    if ok:
        if files:
            discord_files = [discord.File(fp=io.BytesIO(blob), filename=name) for name, blob in files]
            return await channel.send(text, files=discord_files)
        return await channel.send(text)

    if QUALITY_GATE_LOG_FAILS:
        print(f"[GATE] blocked {kind} {symbol}: {reason}")

    if QUALITY_GATE_DEBUG and kind == "analysis" and reason:
        try:
            await channel.send(f"Gate blocked {symbol or 'analysis'}: `{reason}`")
        except Exception:  # noqa: BLE001 - debugging aid only
            pass

    if QUALITY_GATE_FALLBACK_ENABLED and kind == "analysis":
        detail = reason or "Quality gate blocked an unvalidated response."
        fallback = build_safe_fallback(
            symbol or "UNKNOWN",
            pivots,
            note=detail,
        )
        if files:
            discord_files = [discord.File(fp=io.BytesIO(blob), filename=name) for name, blob in files]
            return await channel.send(fallback, files=discord_files)
        return await channel.send(fallback)

    return None


async def safe_send_rate_limited(
    send_target,
    text: str,
    *,
    requester_id: Optional[int],
    request_kind: str,
    client: Optional[object] = None,
    label: str = "",
    kind: str = "text",
    queue_mode: str = "raise",
) -> Optional[object]:
    """Rate-limited wrapper for sending a message (used for on-demand text).

    Enforces per-user cooldowns and burst queue semantics. If queued and
    queue_mode == 'raise', raises PublishQueuedError after enqueueing.
    """

    start_burst_queue_loop()
    render = RenderedPost(text=text or "", agent_payload={})

    if requester_id is None:
        # No requester => treat as non-user-scoped send.
        return await safe_send(send_target, text, kind=kind, symbol="", pivots=None, analysis_mode="coach", output_mode="coach", label=label)

    if not _RATE_QUEUE_ENABLED and (queue_mode or "").lower() == "raise":
        # Still compute wait for a useful error.
        async with _PUBLISH_RATE_LOCK:
            wait_s, _reasons = _compute_rate_wait_seconds(
                label=label or "ask",
                symbol="",
                render=render,
                requester_id=requester_id,
                request_kind=request_kind,
            )
        if wait_s > 0.001:
            raise PublishQueuedError(wait_seconds=wait_s, due_ts=_now_ts() + wait_s, key=_queue_key(label=label or "ask", symbol="", request_kind=request_kind))

    async with _PUBLISH_RATE_LOCK:
        wait_s, reasons = _compute_rate_wait_seconds(
            label=label or "ask",
            symbol="",
            render=render,
            requester_id=requester_id,
            request_kind=request_kind,
        )
        if wait_s > 0.001:
            key = _queue_key(label=label or "ask", symbol="", request_kind=request_kind)
            due_ts = _now_ts() + wait_s
            if _RATE_QUEUE_ENABLED:
                global _QUEUE_SEQ
                qp = _QueuedPublish(
                    due_ts=due_ts,
                    created_ts=_now_ts(),
                    key=key,
                    coro_factory=lambda: safe_send_rate_limited(
                        send_target,
                        text,
                        requester_id=requester_id,
                        request_kind=request_kind,
                        client=client,
                        label=label,
                        kind=kind,
                        queue_mode="silent",
                    ),
                    label=label or "ask",
                    symbol="",
                )
                _QUEUE_SEQ += 1
                _QUEUE_BY_KEY[key] = qp
                heapq.heappush(_QUEUE_HEAP, (qp.due_ts, _QUEUE_SEQ, qp))
                if (queue_mode or "").lower() == "raise":
                    raise PublishQueuedError(wait_seconds=wait_s, due_ts=due_ts, key=key)
                return None
            if (queue_mode or "").lower() == "raise":
                raise PublishQueuedError(wait_seconds=wait_s, due_ts=due_ts, key=key)
            return None

    msg = await safe_send(
        send_target,
        text,
        kind=kind,
        symbol="",
        pivots=None,
        analysis_mode="coach",
        output_mode="coach",
        label=label,
    )
    _mark_rate_usage(
        label=label or "ask",
        symbol="",
        render=render,
        requester_id=requester_id,
        request_kind=request_kind,
    )
    return msg


MEGA_CAP = {"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"}

INDEX_HEAVY = {
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "JPM",
    "XOM",
    "UNH",
    "AVGO",
    "LLY",
    "COST",
    "AMD",
    "NFLX",
    "ADBE",
    "CRM",
}

HIGH_KEYWORDS = [
    "fomc",
    "rate decision",
    "press conference",
    "minutes",
    "cpi",
    "pce",
    "nonfarm",
    "payroll",
    "jobs report",
    "unemployment",
    "jolts",
    "gdp",
    "retail sales",
]

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
RTH_START = time(9, 31)
PM_OPEN = time(4, 0)
AH_CLOSE = time(20, 0)


def parse_iso(ts: str) -> datetime:
    """Parse an ISO8601 timestamp into a timezone-aware datetime."""
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    # SQLite rows often store timestamps without an explicit offset.
    # Treat naive timestamps as UTC so downstream astimezone() calls work.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def now_utc_iso() -> str:
    """Return the current UTC time as ISO8601 string."""
    return _now_utc().isoformat()


def market_session_et(dt_utc: Optional[datetime] = None) -> str:
    """Classify the current US equity session in Eastern Time."""
    dt_utc = dt_utc or _now_utc()
    dt_et = dt_utc.astimezone(ET)
    if dt_et.weekday() >= 5:
        return "WEEKEND"
    t = dt_et.time()
    if RTH_START <= t < RTH_CLOSE:
        return "RTH"
    if PM_OPEN <= t < RTH_START:
        return "PRE"
    if RTH_CLOSE <= t < AH_CLOSE:
        return "AH"
    return "CLOSED"


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


def _pivot_expand(levels: dict) -> dict:
    """Return a copy with ladder extensions populated when possible."""

    expanded = dict(levels or {})

    P = expanded.get("P")
    R1 = expanded.get("R1")
    R2 = expanded.get("R2")
    S1 = expanded.get("S1")
    S2 = expanded.get("S2")

    if any(v is None for v in (P, R1, R2, S1, S2)):
        return expanded

    rng = (R2 - S2) / 2.0 if R2 is not None and S2 is not None else None
    if rng is None:
        return expanded

    if expanded.get("R3") is None:
        expanded["R3"] = P + 2.0 * rng
    if expanded.get("S3") is None:
        expanded["S3"] = P - 2.0 * rng
    if expanded.get("R4") is None:
        expanded["R4"] = expanded["R3"] + rng
    if expanded.get("S4") is None:
        expanded["S4"] = expanded["S3"] - rng

    return expanded


def classify_pivot_regime(price: float, levels: dict) -> str:
    """Tag regime based on current price relative to pivot ladder."""

    P = levels.get("P")
    R1 = levels.get("R1")
    R2 = levels.get("R2")
    S1 = levels.get("S1")
    S2 = levels.get("S2")

    if any(v is None for v in (P, R1, R2, S1, S2)):
        return "UNKNOWN"

    if price >= R2:
        return "ABOVE_R2"
    if price >= R1:
        return "ABOVE_R1"
    if price > P:
        return "ABOVE_P"
    if price <= S2:
        return "BELOW_S2"
    if price <= S1:
        return "BELOW_S1"
    return "BELOW_P"


def build_level_context(price: float, levels_in: dict) -> dict:
    """Return structured overhead/support context with broken level relabeling."""

    levels = _pivot_expand(levels_in or {})

    P = levels.get("P")
    R1 = levels.get("R1")
    R2 = levels.get("R2")
    R3 = levels.get("R3")
    R4 = levels.get("R4")
    S1 = levels.get("S1")
    S2 = levels.get("S2")
    S3 = levels.get("S3")
    S4 = levels.get("S4")

    regime = classify_pivot_regime(price, levels)

    overhead: list[tuple[str, float]] = []
    support: list[tuple[str, float]] = []
    broken: list[tuple[str, float]] = []

    def _add_if(name: str, val, arr: list[tuple[str, float]]):
        if val is None:
            return
        try:
            arr.append((name, float(val)))
        except Exception:  # noqa: BLE001
            return

    _add_if("R4", R4, overhead)
    _add_if("R3", R3, overhead)
    _add_if("R2", R2, overhead)
    _add_if("R1", R1, overhead)
    _add_if("P", P, overhead)
    _add_if("S1", S1, support)
    _add_if("S2", S2, support)
    _add_if("S3", S3, support)
    _add_if("S4", S4, support)

    overhead_now: list[tuple[str, float]] = []
    support_now: list[tuple[str, float]] = []

    for name, val in overhead:
        if val > price:
            overhead_now.append((name, val))
        else:
            broken.append((name, val))

    for name, val in support:
        if val < price:
            support_now.append((name, val))
        else:
            broken.append((name, val))

    overhead_now.sort(key=lambda x: x[1])
    support_now.sort(key=lambda x: -x[1])

    extension_mode = False
    if R2 is not None and price >= R2:
        extension_mode = True
    if S2 is not None and price <= S2:
        extension_mode = True

    upside_targets = overhead_now[:2]
    downside_targets = support_now[:2]

    overhead_from_broken = sorted(
        [(name, val) for (name, val) in broken if val > price],
        key=lambda x: x[1],
    )[:3]

    return {
        "levels": levels,
        "regime": regime,
        "extension_mode": extension_mode,
        "upside_targets": upside_targets,
        "downside_targets": downside_targets,
        "overhead": overhead_now,
        "support": support_now,
        "overhead_from_broken": overhead_from_broken,
        "broken": broken,
    }


def validate_targets_vs_price(price: float, upside_targets, downside_targets) -> tuple[bool, str]:
    for name, val in (upside_targets or []):
        if val <= price:
            return False, f"Invalid upside target {name}={val:.2f} at/under price {price:.2f}"

    for name, val in (downside_targets or []):
        if val >= price:
            return False, f"Invalid downside target {name}={val:.2f} at/over price {price:.2f}"

    return True, ""


def setup_score(prob_up: float, gate_mode: str, conviction: str) -> int:
    edge = abs(prob_up - 0.5)
    base = int(min(100, max(0, edge * 2000)))
    if gate_mode.upper() == "SOFT":
        base = int(base * 0.7)
    if gate_mode.upper() == "HARD":
        base = 0
    if conviction.upper().startswith("LOW"):
        base = int(base * 0.7)
    return base


def _format_ts(ts: Optional[str]) -> str:
    if not ts:
        return "unknown"
    try:
        return parse_iso(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts


def _format_ts_et_clock(ts: Optional[str]) -> str:
    if not ts:
        return "unknown"
    try:
        dt_et = parse_iso(ts).astimezone(ET)
        return dt_et.strftime("%H:%M ET")
    except Exception:  # noqa: BLE001
        return str(ts)


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


def latest_bar_age_min(symbol: str = "SPY", tf: str = "1m") -> float | None:
    """Return the age in minutes of the most recent bar for symbol/tf."""

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT max(ts) FROM prices WHERE symbol=? AND tf=?",
            (symbol.upper(), tf),
        ).fetchone()

    ts = row[0] if row else None
    if not ts:
        return None

    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None

    now = _now_utc()
    return (now - dt).total_seconds() / 60.0


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
    with sqlite3.connect(DB_PATH) as conn:
        cols = "ts, open, high, low, close"
        if with_volume:
            cols += ", volume"
        rows = _rows(
            conn,
            f"""
            SELECT {cols}
            FROM prices
            WHERE symbol=? AND tf=?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (symbol.upper(), tf, n),
        )
    return list(reversed(rows))


def _latest_session_bars(symbol: str) -> Optional[Dict[str, object]]:
    rows = get_last_n_bars(symbol, tf="1m", n=1200, with_volume=True)
    if not rows:
        return None
    sessions: Dict[date, List[Dict[str, object]]] = {}
    for row in rows:
        if len(row) >= 6:
            ts, o, h, l, c, vol = row[:6]
        else:
            ts, o, h, l, c = row[:5]
            vol = 0.0
        try:
            dt_utc = parse_iso(ts)
        except Exception:  # noqa: BLE001
            continue
        dt_et = dt_utc.astimezone(ET)
        if dt_et.weekday() >= 5:
            continue
        if not (RTH_OPEN <= dt_et.time() <= RTH_CLOSE):
            continue
        key = dt_et.date()
        sessions.setdefault(key, []).append(
            {
                "ts": ts,
                "dt": dt_et,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(vol or 0.0),
            }
        )
    if not sessions:
        return None
    session_date = sorted(sessions.keys())[-1]
    bars = sorted(sessions[session_date], key=lambda item: item["dt"])
    if not bars:
        return None
    return {"date": session_date, "bars": bars}


def _session_history_bars(symbol: str, days: int = 3) -> list[Dict[str, object]]:
    rows = get_last_n_bars(symbol, tf="1m", n=4000, with_volume=True)
    if not rows:
        return []
    sessions: Dict[date, List[Dict[str, object]]] = {}
    for row in rows:
        if len(row) >= 6:
            ts, o, h, l, c, vol = row[:6]
        else:
            ts, o, h, l, c = row[:5]
            vol = 0.0
        try:
            dt_et = parse_iso(ts).astimezone(ET)
        except Exception:  # noqa: BLE001
            continue
        if dt_et.weekday() >= 5:
            continue
        if not (RTH_OPEN <= dt_et.time() <= RTH_CLOSE):
            continue
        key = dt_et.date()
        sessions.setdefault(key, []).append(
            {
                "ts": ts,
                "dt": dt_et,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(vol or 0.0),
            }
        )
    ordered = sorted(sessions.keys())
    selected = ordered[-days:]
    result: list[Dict[str, object]] = []
    for key in selected:
        bars = sorted(sessions[key], key=lambda item: item["dt"])
        if bars:
            result.append({"date": key, "bars": bars})
    return result


def _rows_to_df(rows: list[tuple]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["ts", "o", "h", "l", "c", "v"])
    has_volume = len(rows[0]) >= 6
    columns = ["ts", "o", "h", "l", "c", "v"] if has_volume else ["ts", "o", "h", "l", "c"]
    df = pd.DataFrame(rows, columns=columns)
    if "v" not in df:
        df["v"] = 0.0
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts", "o", "h", "l", "c"])
    df = df.sort_values("ts").reset_index(drop=True)
    for col in ("o", "h", "l", "c", "v"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["o", "h", "l", "c"])
    df["v"] = df["v"].fillna(0.0)
    return df


def _drop_incomplete_bar(df: pd.DataFrame, tf_label: str) -> pd.DataFrame:
    if df.empty:
        return df
    try:
        last_ts = df["ts"].iloc[-1]
        if not isinstance(last_ts, pd.Timestamp):
            last_ts = pd.to_datetime(last_ts, utc=True, errors="coerce")
    except Exception:  # noqa: BLE001
        return df
    if pd.isna(last_ts):
        return df
    if tf_label == "1D":
        return df
    threshold_map = {"60m": pd.Timedelta(hours=1), "5m": pd.Timedelta(minutes=5), "1m": pd.Timedelta(minutes=1)}
    max_age = threshold_map.get(tf_label)
    if max_age is None:
        return df
    if pd.Timestamp.utcnow() - last_ts < max_age:
        trimmed = df.iloc[:-1].copy()
        return trimmed.reset_index(drop=True)
    return df


def _resample_from_1m(symbol: str, minutes: int, limit: int) -> pd.DataFrame:
    window = max(limit * minutes * 2, 400)
    rows = get_last_n_bars(symbol, tf="1m", n=window, with_volume=True)
    df = _rows_to_df(rows)
    if df.empty:
        return df
    df = df.set_index("ts")
    agg = (
        df.resample(f"{minutes}min", label="right", closed="right")
        .agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"})
        .dropna(subset=["o", "h", "l", "c"])
        .reset_index()
    )
    return agg.tail(limit)


def _agent_bars_for_symbol(symbol: str) -> Dict[str, pd.DataFrame]:
    plan = [
        ("1D", "1d", 400),
        ("60m", "60m", 360),
        ("5m", "5m", 480),
        ("1m", "1m", 800),
    ]
    result: Dict[str, pd.DataFrame] = {}
    for tf_label, tf_db, limit in plan:
        rows = get_last_n_bars(symbol, tf=tf_db, n=limit + 10, with_volume=True)
        df = _rows_to_df(rows)
        if df.empty and tf_label not in {"1D", "1m"}:
            res_minutes = 60 if tf_label == "60m" else 5
            df = _resample_from_1m(symbol, res_minutes, limit + 10)
        if df.empty:
            continue
        df = _drop_incomplete_bar(df, tf_label)
        if df.empty:
            continue
        result[tf_label] = df.tail(limit)
    return result


def _agent_levels_for_symbol(symbol: str) -> tuple[Dict[str, float], list[Dict[str, float | str]]]:
    levels: Dict[str, float] = {}
    custom: list[Dict[str, float | str]] = []

    piv = get_latest_daily_pivots(symbol)
    if piv and isinstance(piv.get("piv"), dict):
        piv_vals = piv["piv"]
        mapping = {"P": "pivot", "R1": "r1", "S1": "s1", "R2": "r2", "S2": "s2"}
        for src, dest in mapping.items():
            try:
                val = piv_vals.get(src)
                if val is not None:
                    num = float(val)
                    if math.isfinite(num):
                        levels[dest] = num
            except Exception:  # noqa: BLE001
                continue
        try:
            close_val = float(piv.get("C")) if piv.get("C") is not None else None
        except Exception:  # noqa: BLE001
            close_val = None
        if close_val is not None and math.isfinite(close_val):
            custom.append({"label": "Prior Close", "price": close_val, "note": "Previous RTH close"})

    session = _latest_session_bars(symbol)
    if session and session.get("bars"):
        bars = session["bars"]
        highs = [float(b.get("high", 0.0)) for b in bars]
        lows = [float(b.get("low", 0.0)) for b in bars]
        closes = [float(b.get("close", 0.0)) for b in bars]
        vols = [float(b.get("volume", 0.0)) for b in bars]
        total_vol = sum(vols)
        vwap = None
        if total_vol > 0:
            typical_prices = [
                (float(b.get("high", 0.0)) + float(b.get("low", 0.0)) + float(b.get("close", 0.0))) / 3.0
                for b in bars
            ]
            vwap = sum(tp * vol for tp, vol in zip(typical_prices, vols)) / total_vol if total_vol else None
        session_date = session.get("date")
        if highs:
            high_val = max(highs)
            if math.isfinite(high_val):
                custom.append({"label": "Session High", "price": high_val, "note": "Latest RTH high"})
        if lows:
            low_val = min(lows)
            if math.isfinite(low_val):
                custom.append({"label": "Session Low", "price": low_val, "note": "Latest RTH low"})
        if closes:
            close_val = closes[-1]
            if math.isfinite(close_val):
                custom.append({"label": "Session Close", "price": close_val, "note": "Latest RTH close"})
        if vwap is not None and math.isfinite(vwap):
            note = f"{session_date} RTH VWAP" if session_date else "Latest RTH VWAP"
            custom.append({"label": "Session VWAP", "price": vwap, "note": note})

    filtered_custom = [
        item
        for item in custom
        if isinstance(item.get("price"), (int, float)) and math.isfinite(float(item["price"]))
    ]
    return levels, filtered_custom


def _build_agent_payload(
    symbols: Sequence[str],
    *,
    generated_at: datetime,
    post_type: str,
    extra_meta: Optional[Dict[str, Any]] = None,
    sections: Optional[Dict[str, Any]] = None,
    market_participation: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    unique_symbols = [sym.upper() for sym in symbols if sym]
    unique_symbols = list(dict.fromkeys(unique_symbols))
    if not unique_symbols:
        return None

    technical_state: Dict[str, Any] = {}
    pattern_candidates: Dict[str, Any] = {}

    stale_symbols: Dict[str, Any] = {}
    missing_symbols: list[str] = []
    freshness_limit = DATA_STALE_MAX_MIN if DATA_STALE_MAX_MIN > 0 else 3.0

    for sym in unique_symbols:
        try:
            fresh = data_is_fresh(sym, tf="1m", max_min=freshness_limit)
        except Exception:  # noqa: BLE001 - guard against data service hiccups
            fresh = False
        if not fresh:
            stale_symbols[sym] = {"tf": "1m", "max_age_min": freshness_limit}
            continue

        bars_by_tf = _agent_bars_for_symbol(sym)
        if not bars_by_tf:
            missing_symbols.append(sym)
            continue
        levels, custom_levels = _agent_levels_for_symbol(sym)
        package = build_agent_tech_package(
            symbol=sym,
            bars_by_tf=bars_by_tf,
            levels=levels or None,
            custom_levels=custom_levels or None,
        )
        technical_state.update(package.get("technical_state", {}))
        pattern_candidates.update(package.get("pattern_candidates", {}))

    meta: Dict[str, Any] = {
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "symbols": unique_symbols,
        "post_type": post_type,
    }
    if extra_meta:
        meta.update(extra_meta)

    technical_quality: str
    if stale_symbols:
        technical_quality = "STALE"
    elif missing_symbols:
        technical_quality = "MISSING"
    elif technical_state:
        technical_quality = "FRESH"
    else:
        technical_quality = "UNKNOWN"

    data_quality: Dict[str, Any] = {
        "technical_state": technical_quality,
    }
    if pattern_candidates:
        data_quality["pattern_candidates"] = "FRESH"
    elif technical_quality != "FRESH":
        data_quality["pattern_candidates"] = technical_quality

    meta["data_quality"] = data_quality
    if stale_symbols:
        meta["stale_symbols"] = stale_symbols
    if missing_symbols:
        meta["missing_symbols"] = missing_symbols

    payload: Dict[str, Any] = {"meta": meta}
    if sections:
        payload["sections"] = sections
    if technical_state:
        payload["technical_state"] = technical_state
    if pattern_candidates:
        payload["pattern_candidates"] = pattern_candidates
    if market_participation:
        payload["market_participation"] = market_participation

    if len(payload) == 1:
        dq = meta.get("data_quality")
        if isinstance(dq, dict):
            if dq.get("technical_state") == "FRESH":
                return None
        elif dq == "FRESH":  # backward compatibility guard
            return None

    return payload


def _render_with_agent_context(
    lines: Sequence[str],
    symbols: Sequence[str],
    *,
    generated_at: datetime,
    post_type: str,
    extra_meta: Optional[Dict[str, Any]] = None,
    sections: Optional[Dict[str, Any]] = None,
    market_participation: Optional[Dict[str, Any]] = None,
    precomputed_agent_payload: Optional[Dict[str, Any]] = None,
) -> RenderedPost:
    text = "\n".join(lines)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    agent_payload = precomputed_agent_payload
    if agent_payload is None:
        agent_payload = _build_agent_payload(
            symbols,
            generated_at=generated_at,
            post_type=post_type,
            extra_meta=extra_meta,
            sections=sections,
            market_participation=market_participation,
        )
    return RenderedPost(text=text, agent_payload=agent_payload)


def compute_daily_bias(symbol: str, pivot_payload: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Blend session context into a weighted daily bias score with guardrail notes."""
    sessions = _session_history_bars(symbol, days=3)
    if not sessions:
        return None
    session = sessions[-1]
    bars: List[Dict[str, object]] = session["bars"]
    if len(bars) < 10:
        return None
    open_price = bars[0]["open"]
    last_price = bars[-1]["close"]
    last_dt: datetime = bars[-1]["dt"]
    high_price = max(b["high"] for b in bars)
    low_price = min(b["low"] for b in bars)
    if open_price <= 0 or high_price <= 0 or low_price <= 0:
        return None

    def _clamp(val: float, low: float, high: float) -> float:
        return max(low, min(high, val))

    context: List[str] = []
    score = 0.0

    piv = pivot_payload.get("piv") if pivot_payload else None
    if piv:
        P = float(piv.get("P", 0.0))
        R1 = float(piv.get("R1", P))
        S1 = float(piv.get("S1", P))
        if last_price >= R1:
            score += 0.45
            context.append("Holding above R1 (strong)")
        elif last_price >= P:
            score += 0.25
            context.append("Above pivot (constructive)")
        elif last_price <= S1:
            score -= 0.45
            context.append("Below S1 (pressure)")
        else:
            score -= 0.25
            context.append("Below pivot (headwind)")

    if len(sessions) >= 2:
        prev_bars = sessions[-2]["bars"]
        prev_close = prev_bars[-1]["close"] if prev_bars else None
    else:
        prev_close = None
    if prev_close and prev_close > 0:
        gap = (open_price - prev_close) / prev_close
        gap_adj = _clamp(gap * 4.0, -0.4, 0.4)
        score += gap_adj
        if gap > 0:
            context.append(f"Overnight gap up {gap:+.2%}")
        elif gap < 0:
            context.append(f"Overnight gap down {gap:+.2%}")
        else:
            context.append("No overnight gap")
    else:
        context.append("Previous close unavailable")

    vix = get_vix_context("1m")
    if vix and "regime" in vix:
        regime = vix["regime"]
        vix_weight = {
            "LOW VOL": 0.20,
            "NORMAL VOL": 0.0,
            "ELEVATED VOL": -0.20,
            "HIGH / STRESS": -0.35,
        }.get(regime, 0.0)
        score += vix_weight
        context.append(f"VIX regime {regime} ({vix_weight:+.2f} impact)")
    else:
        context.append("VIX regime unavailable")

    early_cutoff = time(10, 30)
    early_bars = [b for b in bars if b["dt"].time() <= early_cutoff]
    if len(early_bars) >= 5:
        early_close = early_bars[-1]["close"]
        early_ret = (early_close - open_price) / open_price
        early_adj = _clamp(early_ret * 5.0, -0.35, 0.35)
        score += early_adj
        context.append(f"Early session move {early_ret:+.2%}")
        early_range = max(b["high"] for b in early_bars) - min(b["low"] for b in early_bars)
        full_range = high_price - low_price if high_price > low_price else 0.0
        if full_range > 0:
            range_util = _clamp(early_range / full_range, 0.0, 1.0)
            context.append(f"Early range used {range_util:.0%} of session range")
    else:
        context.append("Early session data insufficient")

    score = _clamp(score, -1.0, 1.0)

    if score >= 0.6:
        label = "Bullish"
    elif score >= 0.25:
        label = "Slightly Bullish"
    elif score <= -0.6:
        label = "Bearish"
    elif score <= -0.25:
        label = "Slightly Bearish"
    else:
        label = "Neutral"

    abs_score = abs(score)
    if abs_score >= 0.65:
        confidence = "High"
    elif abs_score >= 0.35:
        confidence = "Medium"
    else:
        confidence = "Low"

    return {
        "score": float(score),
        "label": label,
        "confidence": confidence,
        "as_of": last_dt.strftime("%H:%M"),
        "context": context,
        "bars": len(bars),
        "open": open_price,
        "last": last_price,
    }


def compute_weekly_structure(symbol: str) -> Optional[Dict[str, object]]:
    """Derive weekly VWAP (current week) and prior-week extremes from 1m RTH bars."""
    rows = get_last_n_bars(symbol, tf="1m", n=6000, with_volume=True)
    if not rows:
        return None

    week_bars: Dict[date, List[Dict[str, float]]] = {}
    for row in rows:
        if len(row) >= 6:
            ts, _o, h, l, c, vol = row[:6]
        else:
            ts, _o, h, l, c = row[:5]
            vol = 0.0
        try:
            dt_et = parse_iso(ts).astimezone(ET)
        except Exception:  # noqa: BLE001
            continue
        if dt_et.weekday() >= 5:
            continue
        if not (RTH_OPEN <= dt_et.time() <= RTH_CLOSE):
            continue
        week_start = dt_et.date() - timedelta(days=dt_et.weekday())
        week_bars.setdefault(week_start, []).append(
            {
                "close": float(c),
                "high": float(h),
                "low": float(l),
                "volume": float(vol or 0.0),
                "typical": (float(h) + float(l) + float(c)) / 3.0,
            }
        )

    if not week_bars:
        return None

    sorted_weeks = sorted(week_bars.keys())
    current_week = sorted_weeks[-1]
    current = week_bars[current_week]

    vwap_num = 0.0
    vwap_den = 0.0
    for bar in current:
        vol = bar["volume"]
        if vol <= 0:
            continue
        vwap_num += bar["typical"] * vol
        vwap_den += vol
    vwap = (vwap_num / vwap_den) if vwap_den > 0 else None

    prior_high = None
    prior_low = None
    prior_week = None
    if len(sorted_weeks) >= 2:
        prior_week = sorted_weeks[-2]
        prior = week_bars[prior_week]
        if prior:
            prior_high = max(bar["high"] for bar in prior)
            prior_low = min(bar["low"] for bar in prior)

    return {
        "week_start": current_week,
        "vwap": vwap,
        "prior_start": prior_week,
        "prior_high": prior_high,
        "prior_low": prior_low,
    }


def derive_regime(prob_up: Optional[float], hourly_state: Optional[Dict[str, object]]) -> Dict[str, object]:
    reasons: List[str] = []
    if hourly_state is None:
        reasons.append("60m structure unavailable")
        return {
            "label": "TRANSITION",
            "reasons": reasons,
            "bias_dir": 0,
            "regime_dir": 0,
            "range_state": None,
            "conflict": False,
            "structure_available": False,
        }

    range_info = hourly_state.get("range_info")
    range_state = range_info[0] if range_info else None

    hourly_regime = str(hourly_state.get("regime", "NEUTRAL")).upper()
    if range_state == "expanding":
        regime_label = "VOL_EXPANSION"
        reasons.append("Range expanding vs 10-bar avg")
    elif range_state == "compressing":
        regime_label = "VOL_COMPRESSION"
        reasons.append("Range compressing vs 10-bar avg")
    else:
        if hourly_regime == "BULLISH":
            regime_label = "TREND_UP"
            reasons.append("60m structure trending higher")
        elif hourly_regime == "BEARISH":
            regime_label = "TREND_DOWN"
            reasons.append("60m structure trending lower")
        else:
            regime_label = "RANGE"
            reasons.append("60m structure flat")

    bias_dir = 0
    if prob_up is not None:
        if prob_up >= 0.53:
            bias_dir = 1
        elif prob_up <= 0.47:
            bias_dir = -1

    regime_dir = 0
    if regime_label == "TREND_UP":
        regime_dir = 1
    elif regime_label == "TREND_DOWN":
        regime_dir = -1

    conflict = False
    if regime_dir and bias_dir and regime_dir != bias_dir:
        conflict = True
        reasons.append("5m bias conflicts with 60m regime")
        regime_label = "TRANSITION"
        regime_dir = 0

    return {
        "label": regime_label,
        "reasons": reasons,
        "bias_dir": bias_dir,
        "regime_dir": regime_dir,
        "range_state": range_state,
        "conflict": conflict,
        "structure_available": True,
    }


def determine_conviction(
    *,
    edge: float,
    aligned: bool,
    structure_stale: bool,
    has_signal: bool,
    gate_mode: str,
) -> tuple[str, List[str]]:
    notes: List[str] = []
    gate_mode = (gate_mode or "OK").upper()

    if gate_mode == "HARD":
        notes.append("VIX gate HARD → stand down")
        return "LOW", notes

    if not has_signal:
        notes.append("Model signal unavailable → levels-only mode")
        return "LOW", notes

    if structure_stale:
        notes.append("60m structure stale → conviction capped")
        return "LOW", notes

    if aligned and edge >= 0.08:
        return "HIGH", notes

    if 0.04 <= edge < 0.08:
        if aligned:
            return "MEDIUM", notes
        notes.append("5m bias not aligned with 60m → conviction downgraded")
        return "LOW", notes

    notes.append("Edge < 0.04 → conviction LOW")
    return "LOW", notes


def classify_regime_badge(regime_label: str, conviction: str, gate_mode: str) -> tuple[str, str]:
    """Map regime and gating context to a quick-glance badge."""
    regime = (regime_label or "").upper()
    conviction = (conviction or "").upper()
    gate_mode = (gate_mode or "OK").upper()

    if gate_mode == "HARD" or conviction == "LOW":
        return "🔴", "NO TRADE"

    if regime in {"TREND_UP", "TREND_DOWN", "VOL_EXPANSION"}:
        return "🟢", "TREND"

    if regime in {"RANGE", "VOL_COMPRESSION"}:
        return "🟡", "RANGE"

    return "🔴", "NO TRADE"


def select_active_playbook(
    prob_up: float,
    bias_label: str,
    conviction: str,
    regime_state: Optional[Dict[str, object]],
    vix_gate: Optional[Dict[str, object]],
) -> Dict[str, object]:
    bias_label = (bias_label or "").upper()
    conviction = (conviction or "").upper()
    gate_mode = (vix_gate or {}).get("mode", "OK").upper()

    bias_dir = 1 if bias_label == "BULLISH" else -1 if bias_label == "BEARISH" else 0
    regime_label = "UNKNOWN"
    regime_dir = 0
    regime_unavailable = regime_state is None
    if regime_state:
        regime_label = str(regime_state.get("regime", "UNKNOWN"))
        if regime_label == "BULLISH":
            regime_dir = 1
        elif regime_label == "BEARISH":
            regime_dir = -1
        elif regime_label == "NEUTRAL":
            regime_dir = 0

    reasons: List[str] = []

    if gate_mode == "HARD":
        reasons.append("VIX gate HARD (volatility stress)")
        return {"selection": "NO-TRADE", "orientation": None, "reasons": reasons}

    if conviction == "LOW":
        reasons.append("Conviction LOW (edge limited)")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — size down / wait")
        return {"selection": "NO-TRADE", "orientation": None, "reasons": reasons}

    if bias_dir == 0:
        reasons.append(f"Bias neutral (prob_up {prob_up:.2f})")
        if regime_unavailable:
            reasons.append("60m regime unavailable")
        else:
            reasons.append(f"60m regime {regime_label.lower()}")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Neutral", "reasons": reasons}

    if regime_unavailable:
        reasons.append("60m regime unavailable — default to range setups")
        reasons.append(f"Bias {'bullish' if bias_dir > 0 else 'bearish'}")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Neutral", "reasons": reasons}

    if regime_dir == 0:
        reasons.append("60m regime neutral")
        reasons.append(f"Bias {'bullish' if bias_dir > 0 else 'bearish'}")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Neutral", "reasons": reasons}

    if bias_dir != regime_dir:
        reasons.append("Bias and 60m regime conflict")
        reasons.append(
            f"Bias {'bullish' if bias_dir > 0 else 'bearish'} vs 60m {regime_label.lower()}"
        )
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Mixed", "reasons": reasons}

    orientation = "Bullish" if bias_dir > 0 else "Bearish"
    reasons.append(
        f"Bias {'>' if bias_dir > 0 else '<'} 0 (prob_up {prob_up:.2f})"
    )
    reasons.append(f"60m regime {regime_label.lower()}")
    reasons.append(f"Conviction {conviction}")
    if gate_mode == "SOFT":
        reasons.append("VIX gate SOFT — favor defined-risk spreads")

    return {"selection": "TREND", "orientation": orientation, "reasons": reasons}


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    alpha = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period
    for val in values[period:]:
        ema_val = alpha * val + (1 - alpha) * ema_val
    return ema_val


def _resample_to_60m(bars: List[tuple]) -> List[Dict[str, float]]:
    buckets: List[Dict[str, float]] = []
    for row in bars:
        if len(row) >= 6:
            ts, o, h, l, c, vol = row[:6]
            vol = float(vol or 0.0)
        else:
            ts, o, h, l, c = row[:5]
            vol = 0.0
        dt = parse_iso(ts)
        bucket_key = dt.replace(minute=0, second=0, microsecond=0)
        if not buckets or buckets[-1]["hour"] != bucket_key:
            buckets.append(
                {
                    "hour": bucket_key,
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": vol,
                }
            )
        else:
            bucket = buckets[-1]
            bucket["high"] = max(bucket["high"], float(h))
            bucket["low"] = min(bucket["low"], float(l))
            bucket["close"] = float(c)
            bucket["volume"] += vol
    return buckets


def _higher_lows(bars: List[Dict[str, float]], span: int = 3) -> Optional[bool]:
    if len(bars) < span:
        return None
    lows = [bars[i]["low"] for i in range(-span, 0)]
    return all(x <= y for x, y in zip(lows, lows[1:]))


def _range_state(bars: List[Dict[str, float]], lookback: int = 10) -> Optional[Tuple[str, float]]:
    if len(bars) <= lookback:
        return None
    ranges = [max(b["high"] - b["low"], 0.0) for b in bars]
    last_range = ranges[-1]
    hist = [r for r in ranges[-(lookback + 1):-1] if r > 0]
    if not hist:
        return None
    avg_range = sum(hist) / len(hist)
    if avg_range <= 0:
        return None
    ratio = last_range / avg_range
    if ratio >= 1.2:
        state = "expanding"
    elif ratio <= 0.8:
        state = "compressing"
    else:
        state = "steady"
    return state, ratio


def _hourly_regime_state(symbol: str) -> Optional[Dict[str, object]]:
    raw = get_last_n_bars(symbol, tf="1m", n=3600, with_volume=True)
    if len(raw) < 120:
        return None
    hourly = _resample_to_60m(raw)
    if len(hourly) < 12:
        return None

    closes = [b["close"] for b in hourly]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    last_close = closes[-1]

    total_vol = sum(b["volume"] for b in hourly if b["volume"] > 0)
    vwap = None
    price_above_vwap = None
    distance_pct = None
    if total_vol > 0:
        pv = sum(b["close"] * b["volume"] for b in hourly)
        vwap = pv / total_vol if total_vol else None
        if vwap:
            price_above_vwap = last_close >= vwap
            if vwap != 0:
                distance_pct = (last_close - vwap) / vwap * 100.0

    higher_lows = _higher_lows(hourly)
    range_info = _range_state(hourly)

    # Internal indicators are allowed for gating, but must never be displayed.
    try:
        from delivery.tnt_internal_signals import compute_internal_signals, derive_policy_outputs
    except Exception:  # noqa: BLE001
        compute_internal_signals = None  # type: ignore[assignment]
        derive_policy_outputs = None  # type: ignore[assignment]

    policy = None
    if compute_internal_signals and derive_policy_outputs:
        closes_1m = []
        try:
            closes_1m = [float(row[4]) for row in raw if row and len(row) > 4 and row[4] is not None]
        except Exception:  # noqa: BLE001
            closes_1m = []

        sig = compute_internal_signals(closes_1m[-240:], last_price=float(last_close) if last_close is not None else None)
        rs = range_info[0] if range_info else None
        rr = range_info[1] if range_info else None
        policy = derive_policy_outputs(sig, range_state=rs, range_ratio=rr)

    score = 0.0
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            score += 1.0
        elif ema20 < ema50:
            score -= 1.0
    if price_above_vwap is True:
        score += 1.0
    elif price_above_vwap is False:
        score -= 1.0
    if higher_lows is True:
        score += 0.5
    elif higher_lows is False:
        score -= 0.5
    if range_info:
        state, _ratio = range_info
        if state == "expanding":
            score += 0.25
        elif state == "compressing":
            score -= 0.25

    regime = "NEUTRAL"
    emoji = "⚖️"
    if score >= 1.5:
        regime = "BULLISH"
        emoji = "📈"
    elif score <= -1.5:
        regime = "BEARISH"
        emoji = "📉"

    return {
        "regime": regime,
        "emoji": emoji,
        "score": score,
        "vwap": vwap,
        "price_above_vwap": price_above_vwap,
        "distance_vwap_pct": distance_pct,
        "ema20": ema20,
        "ema50": ema50,
        "higher_lows": higher_lows,
        "range_info": range_info,
        "policy": policy,
    }


def build_hourly_regime_block(symbol: str, state: Optional[Dict[str, object]] = None) -> Optional[List[str]]:
    state = state or _hourly_regime_state(symbol)
    if not state:
        return None

    regime_raw = str(state.get("regime", "NEUTRAL")).upper()
    regime_map = {
        "BULLISH": "TREND_UP",
        "BEARISH": "TREND_DOWN",
        "NEUTRAL": "RANGE",
    }
    lines: List[str] = [f"{state['emoji']} 60-min Posture: {regime_map.get(regime_raw, regime_raw)}"]

    policy = state.get("policy")
    if isinstance(policy, dict):
        directive = str(policy.get("directive") or "AGGRESSION REDUCED")
        gates = policy.get("gates")
        if isinstance(gates, (list, tuple)):
            gates_text = ", ".join(str(x) for x in gates if x)
        else:
            gates_text = "NONE"
        momentum_only = bool(policy.get("momentum_only"))

        lines.append(f"• Permission: {directive}")
        lines.append(f"• Gates: {gates_text or 'NONE'}")
        if momentum_only:
            lines.append("• Constraint: MOMENTUM-ONLY")
    else:
        lines.append("• Permission: AGGRESSION REDUCED")
        lines.append("• Gates: NONE")

    lines.append("")
    return lines


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
        age_days = (_now_et().date() - bar_dt_et.date()).days
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


def _refresh_daily_bar_from_polygon(symbol: str, *, lookback_days: int = 14) -> bool:
    sym = (symbol or "").strip().upper()
    if not sym:
        return False

    api_key, base_url, provider = _polygon_key_and_base()
    if not api_key:
        return False

    # Use the same symbol mapping as the ingestion scripts.
    api_symbol = polygon_ingest.SYMBOL_API_MAP.get(sym, sym)

    now_utc = _now_utc()
    start_date = (now_utc - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
    end_date = now_utc.strftime("%Y-%m-%d")
    url = f"{base_url.rstrip('/')}/v2/aggs/ticker/{api_symbol}/range/1/day/{start_date}/{end_date}"

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
        return False

    if resp.status_code != 200:
        return False

    try:
        data = resp.json()
    except Exception:
        return False

    results = data.get("results") or []
    if not isinstance(results, list) or not results:
        return False

    # Take the last completed daily bar.
    last = results[-1]
    try:
        open_val = float(last.get("o"))
        high_val = float(last.get("h"))
        low_val = float(last.get("l"))
        close_val = float(last.get("c"))
        volume_val = float(last.get("v") or 0.0)
        t_ms = int(last.get("t"))
    except Exception:
        return False

    # Normalize timestamp to 16:00 ET so "Source" matches the RTH close.
    try:
        dt_utc = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
        dt_et = dt_utc.astimezone(ET)
        close_et = datetime.combine(dt_et.date(), RTH_CLOSE, tzinfo=ET)
        ts_utc_iso = close_et.astimezone(timezone.utc).isoformat()
    except Exception:
        return False

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
    except Exception:
        return False
    return True


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
    daily = get_latest_daily_bar(symbol)
    if daily:
        ts, _o, high, low, close = daily
        if _should_refresh_daily_bar(ts):
            if _refresh_daily_bar_from_polygon(symbol):
                refreshed = get_latest_daily_bar(symbol)
                if refreshed:
                    daily = refreshed
                    ts, _o, high, low, close = daily

        # If we're in RTH and the stored daily bar isn't for today, fall back to
        # intraday H/L/C from 1m RTH bars so pivots don't appear "2 days old".
        try:
            now_et = _now_utc().astimezone(ET)
            if market_session_et(now_et.astimezone(timezone.utc)) == "RTH":
                daily_date = str(ts)[:10]
                today_date = now_et.date().isoformat()
                if daily_date != today_date:
                    rows = get_last_n_bars(str(symbol), tf="1m")
                    highs: list[float] = []
                    lows: list[float] = []
                    last_close: float | None = None
                    last_ts: str | None = None
                    for rts, _ro, rh, rl, rc in rows:
                        dt_utc = parse_iso(str(rts))
                        dt_et = dt_utc.astimezone(ET)
                        if dt_et.date().isoformat() != today_date:
                            continue
                        clock = dt_et.time()
                        if not (RTH_OPEN <= clock < RTH_CLOSE):
                            continue
                        try:
                            highs.append(float(rh))
                            lows.append(float(rl))
                            last_close = float(rc)
                            last_ts = str(rts)
                        except Exception:
                            continue

                    if highs and lows and last_close is not None and last_ts:
                        piv = pivots_from_hlc(max(highs), min(lows), float(last_close))
                        return {"ts": last_ts, "session": today_date, "H": max(highs), "L": min(lows), "C": float(last_close), "piv": piv}
        except Exception:
            pass

        piv = pivots_from_hlc(float(high), float(low), float(close))
        return {"ts": ts, "H": float(high), "L": float(low), "C": float(close), "piv": piv}

    # No daily bar available locally — try to fetch one from Polygon/Massive.
    if _refresh_daily_bar_from_polygon(symbol):
        daily2 = get_latest_daily_bar(symbol)
        if daily2:
            ts, _o, high, low, close = daily2
            piv = pivots_from_hlc(float(high), float(low), float(close))
            return {"ts": ts, "H": float(high), "L": float(low), "C": float(close), "piv": piv}

    sess = get_last_rth_session_hlc(symbol)
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


def get_latest_signal(symbol: str):
    symbol = symbol.upper()
    conn = sqlite3.connect(os.getenv("DB_PATH", "db/tnt.db"))
    cur = conn.cursor()

    cur.execute(
        "SELECT ts, horizon_min, prob_up, prob_down, model_version, meta "
        "FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (symbol,)
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    ts, horizon_min, prob_up, prob_down, model_version, meta = row
    return {
        "ts": ts,
        "horizon_min": int(horizon_min),
        "prob_up": float(prob_up),
        "prob_down": float(prob_down),
        "model_version": model_version,
        "meta": meta or ""
    }


def classify_bias(prob_up: float) -> str:
    return bias_from_prob(prob_up)


def classify_conviction(prob_up: float) -> str:
    return conviction_from_edge(edge_from_prob(prob_up))


def _utc_now_iso():
    return _now_utc().isoformat()


def explain_no_trade(
    sym: str,
    prob_up: float | None,
    gate_mode: str,
    confirm_bias: str,
    confirm_note: str,
    mode: Optional[str] = None,
):
    reasons: list[str] = []

    mode_norm = (mode or "strict").strip().lower()
    edge_floor = _edge_threshold_for_mode(mode_norm)

    if prob_up is None:
        reasons.append("No model signal available yet for this symbol.")
        edge = None
    else:
        edge = abs(prob_up - 0.5)
        if edge < edge_floor:
            reasons.append(
                f"Edge below threshold ({edge:.3f} < {edge_floor:.3f}, mode {mode_norm})."
            )
        else:
            reasons.append(f"Edge meets threshold ({edge:.3f}).")

    if gate_mode == "HARD":
        reasons.append("Volatility gate is HARD (stand down).")
    elif gate_mode == "SOFT":
        reasons.append("Volatility gate is SOFT (reduce risk; prefer spreads).")
    else:
        reasons.append("Volatility gate OK.")

    if confirm_bias in ("UNKNOWN", "NEUTRAL"):
        reasons.append(f"Confirmation neutral/unknown: {confirm_note}.")
    else:
        reasons.append(f"Confirmation supports direction: {confirm_note}.")

    next_check = "Next check: after break + retest at Pivot/S1/R1 or in ~5–10 minutes."
    return reasons, next_check


def get_latest_signal_context(symbol: str) -> tuple[list[str], Optional[tuple]]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA table_info(signals)")
            table_cols = {info[1] for info in cur.fetchall()}
        except sqlite3.OperationalError:
            return [], None

        if "ts" not in table_cols or "prob_up" not in table_cols:
            return [], None

        select_cols = ["ts", "prob_up"]
        for name in ("prob_down", "model_version", "model", "edge", "ret_1m", "trend", "vol_z"):
            if name in table_cols:
                select_cols.append(name)

        sql = "SELECT {cols} FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1".format(
            cols=", ".join(select_cols)
        )

        try:
            cur.execute(sql, (symbol.upper(),))
            row = cur.fetchone()
        except sqlite3.OperationalError:
            return [], None

    if not row:
        return [], None
    return select_cols, row


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


def fetch_earnings_for_date(date_et: str) -> list[dict]:
    """Fetch earnings calendar rows for a given ET date."""
    if not EARNINGS_API_KEY:
        return []
    if EARNINGS_PROVIDER != "earningsapi":
        return []
    url = f"https://api.earningsapi.com/v1/calendar/{date_et}"
    try:
        resp = requests.get(url, params={"apikey": EARNINGS_API_KEY}, timeout=15)
        print(f"[EARNINGS] date={date_et} status={resp.status_code} bytes={len(resp.text)}")
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    rows: list[dict] = []
    for bucket, label in [("pre", "BMO"), ("after", "AMC"), ("notSupplied", "TAS")]:
        items = data.get(bucket) or []
        for entry in items:
            sym = (entry.get("symbol") or entry.get("ticker") or "").upper()
            if not sym:
                continue
            if EARNINGS_WATCHLIST and sym not in EARNINGS_WATCHLIST:
                continue
            rows.append({"symbol": sym, "when": label})

    return rows


def get_latest_two_closes(symbol: str, tf: str = "5m") -> Optional[Dict[str, object]]:
    symbol = symbol.upper()
    db_path = os.getenv("DB_PATH", DB_PATH)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ts, close FROM prices WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 2",
            (symbol, tf),
        )
        rows = cur.fetchall()
    if len(rows) < 2:
        return None
    (ts0, c0), (ts1, c1) = rows[0], rows[1]
    return {
        "ts_new": ts0,
        "c_new": float(c0),
        "ts_prev": ts1,
        "c_prev": float(c1),
    }


def _direction_from_two_closes(data: Optional[Dict[str, object]]) -> str:
    if not data:
        return "unknown"
    c_new = float(data["c_new"])
    c_prev = float(data["c_prev"])
    if c_new > c_prev:
        return "up"
    if c_new < c_prev:
        return "down"
    return "flat"


def vix_trend(tf: str = "5m") -> str:
    return _direction_from_two_closes(get_latest_two_closes("VIX", tf=tf))


def sqqq_dir(tf: str = "5m") -> str:
    return _direction_from_two_closes(get_latest_two_closes("SQQQ", tf=tf))


def vix_sqqq_confirmation(tf: str = "5m") -> Tuple[str, str, Dict[str, object]]:
    """Return directional confirmation bias info from VIX/SQQQ pairs."""

    vix_data = get_latest_two_closes("VIX", tf=tf)
    sqqq_data = get_latest_two_closes("SQQQ", tf=tf)
    vt = _direction_from_two_closes(vix_data)
    sd = _direction_from_two_closes(sqqq_data)

    detail: Dict[str, object] = {
        "tf": tf,
        "vix_trend": vt,
        "sqqq_dir": sd,
        "vix_ts_new": vix_data["ts_new"] if vix_data else None,
        "vix_ts_prev": vix_data["ts_prev"] if vix_data else None,
        "sqqq_ts_new": sqqq_data["ts_new"] if sqqq_data else None,
        "sqqq_ts_prev": sqqq_data["ts_prev"] if sqqq_data else None,
    }

    if vt == "unknown" or sd == "unknown":
        return "UNKNOWN", "insufficient VIX/SQQQ data", detail

    if vt == "up" and sd == "up":
        return "BEARISH", "VIX rising + SQQQ rising -> downside confirmed", detail

    if vt == "down" and sd == "down":
        return "BULLISH", "VIX falling + SQQQ falling -> upside confirmed", detail

    if vt == "up" and sd in ("flat", "down"):
        return "NEUTRAL", "VIX up but SQQQ not confirming -> volatility without direction", detail

    if vt in ("flat", "down") and sd == "up":
        return "NEUTRAL", "SQQQ up but VIX not confirming -> possible chop/hedging", detail

    return "NEUTRAL", "mixed signals -> treat as neutral / range rules", detail


def render_directional_confirmation_block(tf: str = "5m") -> str:
    bias, note, detail = vix_sqqq_confirmation(tf=tf)
    icon = "🟢" if bias == "BULLISH" else ("🔴" if bias == "BEARISH" else ("🟡" if bias == "NEUTRAL" else "🟠"))
    return (
        "🧭 **Directional Confirmation (VIX + SQQQ)**\n"
        f"• TF: {detail.get('tf', '?')} | VIX trend: **{detail.get('vix_trend', '?')}** | SQQQ dir: **{detail.get('sqqq_dir', '?')}**\n"
        f"• {icon} Bias confirm: **{bias}** — {note}"
    )


def earnings_within_window(symbol: str, now_et: datetime, window_minutes: int = 48 * 60) -> Optional[dict]:
    if not EARNINGS_API_KEY:
        return None
    symbol = symbol.upper()
    for offset in range(0, 3):
        target_date = now_et.date() + timedelta(days=offset)
        date_str = target_date.strftime("%Y-%m-%d")
        try:
            rows = fetch_earnings_for_date(date_str)
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            continue
        for row in rows:
            if (row.get("symbol") or "").upper() != symbol:
                continue
            when = (row.get("when") or "").upper()
            if when == "BMO":
                event_time = time(8, 30)
            elif when == "AMC":
                event_time = time(16, 30)
            else:
                event_time = time(12, 0)
            event_dt = datetime.combine(target_date, event_time, tzinfo=ET)
            delta_min = (event_dt - now_et).total_seconds() / 60.0
            if 0 <= delta_min <= window_minutes:
                label = "within 24h" if delta_min <= 24 * 60 else "within 48h"
                return {
                    "symbol": symbol,
                    "when": when,
                    "event_dt": event_dt,
                    "delta_min": delta_min,
                    "window": label,
                }
    return None


def load_macro_events_for_date(date_et: str) -> list[dict]:
    events: list[dict] = []
    if not os.path.exists(MACRO_EVENTS_FILE):
        return events
    try:
        with open(MACRO_EVENTS_FILE, "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if (row.get("date_et") or "").strip() != date_et:
                    continue
                events.append(
                    {
                        "time_et": (row.get("time_et") or "").strip(),
                        "title": (row.get("title") or "").strip(),
                        "impact": (row.get("impact") or "").strip().upper() or "MED",
                    }
                )
    except Exception:  # noqa: BLE001
        return []

    def _sort_key(event: dict) -> str:
        return event.get("time_et") or "99:99"

    return sorted(events, key=_sort_key)


def macro_within_minutes(events: Optional[list[dict]], minutes: int, now_et: datetime) -> Optional[dict]:
    if not events:
        return None
    for item in events:
        t = (item.get("time_et") or "").strip()
        if not t:
            continue
        try:
            hh, mm = [int(x) for x in t.split(":")]
            ev_dt = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            delta_min = (ev_dt - now_et).total_seconds() / 60.0
            if 0 <= delta_min <= minutes and (item.get("impact", "").upper() == "HIGH"):
                return item | {"delta_min": delta_min, "event_dt": ev_dt}
        except Exception:  # noqa: BLE001
            continue
    return None


def format_morning_brief(symbols: Optional[list[str]] = None) -> str:
    now_et = _now_et()
    today_et = now_et.strftime("%Y-%m-%d")
    target_date_et = next_market_date_et(now_et)
    sess = market_session_et(now_et)
    lines = [f"📈 *Morning Brief — {target_date_et}*"]
    if target_date_et != today_et:
        lines.append(f"🗓️ Note: using next market day (today is {today_et}).")
    lines.append("")
    lines.append(f"🕗 Session: {sess} (ET {now_et.strftime('%H:%M')})")

    vix = get_vix_context("1m")
    if vix:
        lines.append(f"🔥 VIX: {vix['level']:.2f} ({vix['regime']})")
        gate = vix_gating_action(float(vix["level"]))
        if gate["mode"] == "HARD":
            lines.append("Posture: **STAND DOWN**")
        elif gate["mode"] == "SOFT":
            lines.append("Posture: **CAUTION**")
        else:
            lines.append("Posture: **NORMAL**")

    macro = load_macro_events_for_date(target_date_et)

    banner = macro_risk_banner(macro, label=f"Macro Risk ({target_date_et} ET)")
    if banner:
        lines.append(banner)

    tomorrow_et = next_calendar_day_et(target_date_et)
    macro_tom = load_macro_events_for_date(tomorrow_et)
    banner_tom = macro_risk_banner(macro_tom, label=f"Tomorrow Risk ({tomorrow_et} ET)")
    if banner_tom:
        lines.append(banner_tom)
    lines.append("")
    lines.append("📅 **Macro Events (ET)**")
    if macro:
        for e in macro[:10]:
            lines.append(
                f"• {e.get('time_et','')} — {impact_icon(e.get('impact'))} {e.get('title','')}".strip()
            )
    else:
        lines.append("• (none listed)")

        earn = fetch_earnings_for_date(target_date_et)
        lines.append("")
        lines.append("💼 **Earnings**")

        if earn:
            bmo = sorted({x["symbol"] for x in earn if x.get("when") == "BMO"})
            amc = sorted({x["symbol"] for x in earn if x.get("when") == "AMC"})
            tas = sorted({x["symbol"] for x in earn if x.get("when") == "TAS"})

            if bmo:
                lines.append(f"• Before Market Open: {fmt_earnings_list(bmo)}")
            if amc:
                lines.append(f"• After Market Close: {fmt_earnings_list(amc)}")
            if tas:
                lines.append(f"• Time not supplied: {fmt_earnings_list(tas)}")

            if not (bmo or amc or tas):
                lines.append("• (none on watchlist)")
        else:
            if not EARNINGS_API_KEY:
                lines.append("• (earnings disabled: missing API key)")
            else:
                lines.append("• (none on watchlist / provider empty or rate-limited)")

    symbols = symbols or ["SPY", "QQQ", "IWM"]
    lines.append("")
    lines.append("🔎 **Levels / Plan**")
    for symbol in symbols:
        piv_data = get_latest_daily_pivots(symbol)
        if not piv_data:
            lines.append(f"{symbol}: (no pivots yet)")
            continue
        pivots = piv_data["piv"]
        lines.append(f"{symbol}: P {pivots['P']:.2f} | R1 {pivots['R1']:.2f} | S1 {pivots['S1']:.2f}")

    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


def next_weekday(d: date) -> date:
    # Mon-Fri only (MVP-safe; holidays not detected without an external calendar)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def next_market_date_et(now_et: datetime | None = None) -> str:
    now_et = now_et or _now_et()
    d = now_et.date()
    if d.weekday() >= 5:
        d = next_weekday(d)
    return d.strftime("%Y-%m-%d")


def next_calendar_day_et(d_str: str) -> str:
    # d_str = YYYY-MM-DD
    y, m, d = [int(x) for x in d_str.split("-")]
    dd = date(y, m, d) + timedelta(days=1)
    return dd.strftime("%Y-%m-%d")


def earnings_tag(sym: str) -> str:
    sym = sym.upper()
    tags: list[str] = []
    if sym in MEGA_CAP:
        tags.append("⭐")
    if sym in INDEX_HEAVY:
        tags.append("📌")
    return f" {''.join(tags)}" if tags else ""


def fmt_earnings_list(syms: list[str], limit: int = 18) -> str:
    syms = syms[:limit]
    return ", ".join([f"{s}{earnings_tag(s)}" for s in syms])


def is_high_event(e: dict) -> bool:
    title = (e.get("title") or "").lower()
    impact = (e.get("impact") or "").upper()
    if impact == "HIGH":
        return True
    return any(k in title for k in HIGH_KEYWORDS)


def macro_risk_banner(events: list[dict], label: str = "Macro Risk") -> str | None:
    if not events:
        return None

    hi = [e for e in events if is_high_event(e)]
    if not hi:
        return None

    hi = sorted(hi, key=lambda x: (x.get("time_et") or "99:99"))[:2]
    parts: list[str] = []
    for e in hi:
        t = (e.get("time_et") or "").strip()
        title = (e.get("title") or "").strip()
        parts.append(f"{impact_icon(e.get('impact'))} {t} {title}".strip())

    return f"🚨 {label}: " + " | ".join(parts)


def impact_icon(impact: str) -> str:
    impact = (impact or "").upper()
    if impact == "HIGH":
        return "🔴"
    if impact == "MED":
        return "🟠"
    if impact == "LOW":
        return "🟡"
    return "🟠"


def load_brief_state() -> Dict[str, object]:
    try:
        if os.path.exists(BRIEF_STATE_PATH):
            with open(BRIEF_STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_brief_state(state: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(BRIEF_STATE_PATH), exist_ok=True)
    with open(BRIEF_STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


async def notify_ops(message: str) -> None:
    try:
        if BOT_ALERT_CHANNEL_ID:
            channel = bot.get_channel(BOT_ALERT_CHANNEL_ID)
            if channel:
                await channel.send(f"⚠️ **BOT ALERT**\n{message}")
    except Exception:  # noqa: BLE001
        pass


def options_structures(bias: str, vol_mode: str, conviction: str) -> list[str]:
    if vol_mode == "HARD":
        return ["Stand down (volatility stress). If trading: very small defined-risk only."]

    suggestions: list[str] = []
    bias = bias.upper()
    conviction = conviction.upper()
    vol_mode = vol_mode.upper()

    if bias == "BULL":
        suggestions.append("Bullish: Call debit spread (defined risk)")
        if vol_mode == "OK" and conviction in {"MED", "HIGH"}:
            suggestions.append("Alternative: Long call (only if conviction HIGH and levels confirm)")
    elif bias == "BEAR":
        suggestions.append("Bearish: Put debit spread (defined risk)")
        if vol_mode == "OK" and conviction in {"MED", "HIGH"}:
            suggestions.append("Alternative: Long put (only if conviction HIGH and levels confirm)")
    else:
        suggestions.append("Neutral: Iron condor / short premium *only* if range holds and VIX not elevated")
        suggestions.append("Neutral alt: Calendar/diagonal (if expecting IV change)")

    if vol_mode == "SOFT":
        suggestions.append("Vol elevated: favor spreads; avoid naked long premium unless strong edge")

    return suggestions


def build_playbooks(symbol: str, prob_up: float, piv: dict, vix_gate_mode: str, session: str) -> list[dict]:
    P = float(piv["P"])
    R1 = float(piv["R1"])
    S1 = float(piv["S1"])
    bias = classify_bias(prob_up)
    conv = classify_conviction(prob_up)

    if vix_gate_mode == "HARD":
        return [
            {
                "title": "STAND DOWN (volatility stress)",
                "body": [
                    "No new 0DTE trades recommended under HARD VIX gating.",
                    "If you must trade: defined-risk only, smallest size, strict time stop.",
                ],
                "structures": [
                    "0DTE: debit spreads only (small)",
                    "Weeklies: debit spreads / calendars (small)",
                ],
            }
        ]

    out: list[dict] = []

    trend_struct_0dte = "0DTE: debit spread (call spread if bull / put spread if bear)"
    trend_struct_wkly = "Weeklies: debit spread or diagonal (if you want more time)"

    trend_rules = [
        f"Trigger (bull): reclaim/hold above Pivot **P {P:.2f}** → target R1 **{R1:.2f}**.",
        f"Trigger (bear): lose/hold below Pivot **P {P:.2f}** → target S1 **{S1:.2f}**.",
        "Confirmation: wait for break + retest (avoid first spike).",
        "Stops: invalidation = return back through P after entry; add time-stop (e.g., 20–40 min).",
    ]
    if vix_gate_mode == "SOFT":
        trend_rules.append("VIX CAUTION: prefer spreads; avoid naked long premium unless conviction HIGH.")

    out.append(
        {
            "title": "PLAYBOOK 1 — TREND (best if breakout/breakdown)",
            "body": trend_rules,
            "structures": [trend_struct_0dte, trend_struct_wkly],
            "notes": f"Bias={bias}, Conviction={conv} (prob_up={prob_up:.2f})",
        }
    )

    range_struct_0dte = (
        "0DTE: defined-risk (iron condor) *only if range holds* OR quick debit scalps at edges"
    )
    range_struct_wkly = "Weeklies: calendars/diagonals (if expecting chop + IV shift)"

    range_rules = [
        f"Range zone: between **S1 {S1:.2f}** and **R1 {R1:.2f}** (use P as magnet).",
        "Long mean-reversion idea: reactions at S1/P/R1 — take only clean rejections, not mid-range noise.",
        "No-trade condition: if price is accelerating and closing outside S1/R1 → switch to TREND playbook.",
        "Risk: defined-risk only; cap max loss per play; add a time stop if chop persists.",
    ]
    if vix_gate_mode == "SOFT":
        range_rules.append("VIX CAUTION: range trades OK only if price action is orderly; reduce frequency.")

    out.append(
        {
            "title": "PLAYBOOK 2 — RANGE / MEAN REVERSION (best if chop)",
            "body": range_rules,
            "structures": [range_struct_0dte, range_struct_wkly],
            "notes": "Use when price is respecting levels and volatility is not exploding.",
        }
    )

    return out


def _day_ok(now_et: datetime) -> bool:
    day_names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    return day_names[now_et.weekday()] in MORNING_BRIEF_DAYS


async def morning_brief_loop() -> None:
    state = load_brief_state()

    while True:
        try:
            if not MORNING_BRIEF_ENABLED or not MORNING_BRIEF_CHANNEL_ID:
                await asyncio.sleep(60)
                continue

            now_et = _now_et()

            # weekday filter
            map_days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
            if map_days[now_et.weekday()] not in MORNING_BRIEF_DAYS:
                await asyncio.sleep(300)
                continue

            # target time
            hh, mm = [int(x) for x in MORNING_BRIEF_TIME_ET.split(":")]
            target = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)

            # one post per calendar day (persists across restarts)
            key = f"brief_posted_{now_et.date().isoformat()}"
            already = bool(state.get(key, False))

            if now_et >= target and not already:
                ch = bot.get_channel(MORNING_BRIEF_CHANNEL_ID)
                if not ch:
                    await notify_ops(
                        f"Morning brief channel not found: channel_id={MORNING_BRIEF_CHANNEL_ID}"
                    )
                else:
                    msg = format_morning_brief()
                    await ch.send(msg)

                    state[key] = True
                    state["last_post_ts"] = now_et.isoformat()
                    save_brief_state(state)

            await asyncio.sleep(20)

        except Exception as exc:  # noqa: BLE001
            await notify_ops(f"Morning brief loop exception: `{exc}`")
            await asyncio.sleep(60)


async def morning_opt_loop() -> None:
    global _last_morning_opt_date

    target_time = _parse_time_et(MORNING_OPT_TIME_ET, time(9, 28))

    while True:
        try:
            if not MORNING_OPT_ENABLED:
                await asyncio.sleep(60)
                continue

            channel_id = MORNING_OPT_CHANNEL_ID or CHANNEL_ID
            if not channel_id:
                await asyncio.sleep(60)
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            target_dt = now_et.replace(
                hour=target_time.hour,
                minute=target_time.minute,
                second=0,
                microsecond=0,
            )

            if now_et < target_dt:
                await asyncio.sleep(30)
                continue

            if _last_morning_opt_date == now_et.date():
                await asyncio.sleep(60)
                continue

            vix = get_vix_context("1m")
            gate = vix_gating_action(float(vix["level"])) if vix else {"mode": "OK", "reason": ""}
            if gate.get("mode", "").upper() == "HARD":
                print(f"[OPT928] {now_et.date()}: skip (VIX gate HARD)")
                _last_morning_opt_date = now_et.date()
                await asyncio.sleep(60)
                continue

            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"[OPT928][WARN] cannot fetch channel {channel_id}: {exc}")
                    await asyncio.sleep(60)
                    continue

            header = "📣 **09:28 ET Options Toolkit**"
            if vix:
                header += f" | VIX {vix['level']:.2f} ({vix['regime']})"
            gate_note = gate.get("mode", "OK")
            if gate.get("reason"):
                header += f" | gate {gate_note} ({gate['reason']})"
            else:
                header += f" | gate {gate_note}"
            await channel.send(header)

            symbols = MORNING_OPT_SYMBOLS or ["SPY", "QQQ", "IWM"]
            for sym in symbols:
                payload, err = build_opt_payload(sym)
                if err:
                    await channel.send(err)
                    continue
                await channel.send(render_opt_clean(payload))

            _last_morning_opt_date = now_et.date()
            await asyncio.sleep(90)

        except Exception as exc:  # noqa: BLE001
            print(f"[OPT928][ERROR] {exc}")
            await asyncio.sleep(60)


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
    arrow = "↑" if prob_up >= 0.5 else "↓"
    lines = [
        f"📊 **{symbol}** model `{model_version or MODEL_VERSION}`",
        f"• Horizon {horizon}m | Prob↑ {prob_up:.3f} {arrow} | Prob↓ {prob_down:.3f}",
        f"• Edge {edge:.3f} | Signal time {_format_ts(ts)}",
    ]
    if price_info:
        price, price_ts = price_info
        lines.append(f"• Last price {price:.2f} @ {_format_ts(price_ts)}")
    extras = _extract_meta(meta)
    if extras:
        lines.append("• Meta: " + " | ".join(f"{k}={v:.2f}" for k, v in extras.items()))
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
    now_et = _now_et()
    lines = [
        "📌 **Daily Summary (signals + pivots)**",
        f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}",
    ]

    futures_block = _build_futures_section(post_type="daily_summary", now_ts=now_et.isoformat())
    if futures_block:
        lines.append("")
        lines.append(futures_block.rstrip("\n"))

    mode_setting = get_analysis_mode()
    for sym in symbols:
        sym = sym.upper()
        lines.append(f"\n**{sym}**")
        piv = get_latest_daily_pivots(sym)
        if piv:
            vals = piv["piv"]
            lines.append(
                "• Prev RTH pivots: P {P:.2f} | R1 {R1:.2f} | S1 {S1:.2f} | R2 {R2:.2f} | S2 {S2:.2f}".format(**vals)
            )
        else:
            lines.append("• (no daily pivots yet)")
        cols, sig = get_latest_signal_context(sym)
        have_signal = bool(sig)
        if have_signal:
            data = {col: sig[idx] for idx, col in enumerate(cols)}
            prob_up = float(data.get("prob_up", 0.5) or 0.5)
            edge_val = data.get("edge")
            edge = float(edge_val) if edge_val is not None else abs(prob_up - 0.5)
            conv = conviction_from_edge(edge)
            model_name = str(data.get("model_version") or data.get("model") or "unknown")
            lines.append(
                f"• Signal: prob_up {prob_up:.2f} | edge {edge:.2f} | conv {conv} | model {model_name}"
            )
        else:
            vix_ctx = get_vix_context("1m")
            vix_level = float(vix_ctx["level"]) if vix_ctx and "level" in vix_ctx else None
            gate_mode = vix_gating_action(vix_level)["mode"] if vix_level is not None else "OK"
            confirm_bias, confirm_note, _ = vix_sqqq_confirmation(tf=os.getenv("BIAS_TF", BIAS_TF))
            reasons, next_check = explain_no_trade(
                sym,
                None,
                gate_mode,
                confirm_bias,
                confirm_note,
                mode=mode_setting,
            )
            lines.append("• Signal status: **NO TRADE**")
            lines.append(f"• Last evaluated: {_utc_now_iso()}")
            if reasons:
                lines.append("• Reasons:")
                for reason in reasons[:3]:
                    lines.append(f"  - {reason}")
            lines.append(f"• {next_check}")

        if have_signal:
            acc = rolling_accuracy(sym, horizon_min=HORIZON_MIN, n=50, tf="1m")
            if acc and acc["acc"] is not None:
                lines.append(f"• Rolling acc: {acc['acc']*100:.1f}% ({acc['hits']}/{acc['matured']})")
            elif acc:
                lines.append(f"• Rolling acc: n/a (coverage {acc['matured']}/{acc['considered']})")
            else:
                lines.append("• Rolling acc: n/a")
    lines.append("\n_Not financial advice._")
    return "\n".join(lines)


def format_monday_playbook(symbols: list[str]) -> str:
    session = market_session_et()
    title = "Monday Open Playbook" if _now_et().weekday() == 0 else "Next RTH Playbook"
    lines = [f"📘 **{title} (levels + regimes + plan)**", f"ET: {_now_et().strftime('%Y-%m-%d %H:%M')} | Session={session}"]
    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, _confirm_note, _ = vix_sqqq_confirmation(tf=tf_bias)
    primary_sym = symbols[0] if symbols else "SPY"
    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig else None
    edge_val = _edge(prob_up) if prob_up is not None else 0.0
    min_edge = AUTOPOST_EDGE_MIN_STRICT

    signal_regime: Optional[str] = None
    if sig and sig.get("meta"):
        try:
            sig_meta = json.loads(sig["meta"])
        except Exception:  # noqa: BLE001
            sig_meta = None
        if isinstance(sig_meta, dict):
            regime_val = sig_meta.get("regime") or sig_meta.get("market_regime")
            if isinstance(regime_val, str):
                signal_regime = regime_val.upper()
    strong_edge = prob_up is not None and edge_val >= min_edge
    market_status = "ACTIVE" if confirm_bias not in ("NEUTRAL", "UNKNOWN") and strong_edge else "WAIT MODE"
    lines.append(f"Market Status: **{market_status}**")
    lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    lines.append("")
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
                    f"• Bull case: reclaim/hold above **P {vals['P']:.2f}** → target **R1 {vals['R1']:.2f}**",
                    f"• Bear case: lose/hold below **P {vals['P']:.2f}** → test **S1 {vals['S1']:.2f}**",
                    "• If inside S1-R1: treat as range until break + retest",
                ]
            )
        else:
            lines.append("• (no daily pivots yet)")
    lines.append("\n_Not financial advice._")
    return "\n".join(lines)


async def daily_summary_loop() -> None:
    global _last_summary_date
    while True:
        try:
            now_et = _now_et()
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
            now_et = _now_et()
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
                            "📘 **Monday Playbook**\nET: "
                            + _now_et().strftime("%Y-%m-%d %H:%M")
                            + "\n⚠️ Market session is CLOSED. Holiday or unexpected closure—verify market hours."
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


def _now_et() -> datetime:
    provider = _NOW_PROVIDER
    if provider is not None:
        dt = provider()
        if dt is None:
            raise ValueError("_NOW_PROVIDER returned None")
        if dt.tzinfo is None:
            return dt.replace(tzinfo=ET)
        return dt.astimezone(ET)

    if ET_TZ:
        return datetime.now(ET_TZ)

    return datetime.now(timezone.utc).astimezone(ET)


def _now_utc() -> datetime:
    return _now_et().astimezone(timezone.utc)


def _now_naive() -> datetime:
    return _now_et().replace(tzinfo=None)


def _parse_hhmm(s: str, default: str = "09:25") -> tuple[int, int]:
    t = (s or "").strip() or default
    hh, mm = t.split(":")
    return int(hh), int(mm)


def _ensure_watchlist_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            added_ts TEXT NOT NULL
        )
        """
    )

    existing = {row[1] for row in conn.execute("PRAGMA table_info(watchlist)")}
    altered = False
    for column, ddl in WATCHLIST_EXTRA_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE watchlist ADD COLUMN {column} {ddl}")
            altered = True
    if altered:
        conn.commit()

def migrate_settings_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL,
            updated_ts TEXT NOT NULL
        )
        """
    )
    conn.commit()


def settings_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    cur = conn.cursor()
    row = cur.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def settings_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    from datetime import datetime, timezone

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings(k,v,updated_ts) VALUES(?,?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_ts=excluded.updated_ts",
        (key, value, _now_utc().isoformat()),
    )
    conn.commit()


def _watchlist_record_attempt(symbol: str, attempt_ts: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist(symbol, added_ts) VALUES (?, ?)",
            (symbol, attempt_ts),
        )
        conn.execute(
            "UPDATE watchlist SET last_attempt_ts=?, last_error=NULL WHERE symbol=?",
            (attempt_ts, symbol),
        )
        conn.commit()


def _watchlist_record_success(symbol: str, ready_ts: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        conn.execute(
            "UPDATE watchlist SET last_ready_ts=?, last_error=NULL WHERE symbol=?",
            (ready_ts, symbol),
        )
        conn.commit()


def _watchlist_record_error(symbol: str, attempt_ts: str, error: str) -> None:
    truncated = (error or "")[:240]
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist(symbol, added_ts) VALUES (?, ?)",
            (symbol, attempt_ts),
        )
        conn.execute(
            "UPDATE watchlist SET last_error=?, last_attempt_ts=? WHERE symbol=?",
            (truncated or None, attempt_ts, symbol),
        )
        conn.commit()


def _fetch_watchlist_symbols() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        cursor = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol ASC")
        rows = cursor.fetchall()
    raw = [row[0] for row in rows if row and row[0]]
    symbols = _filter_startup_symbols(raw)
    if symbols:
        return symbols
    if _STARTUP_SYMBOL_SET:
        return STARTUP_SYMBOLS
    return [str(row[0]).upper() for row in rows if row and row[0]]


def _fetch_watchlist_entries() -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure_watchlist_schema(conn)
            migrate_settings_table(conn)
            cursor = conn.execute(
                "SELECT symbol, added_ts, last_ready_ts, last_attempt_ts, last_error FROM watchlist ORDER BY symbol"
            )
            col_names = [desc[0] for desc in cursor.description or []]
            rows = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] fetch watchlist entries failed: {exc}")
        return []

    entries: list[dict] = []
    for row in rows:
        entry = {}
        for idx, name in enumerate(col_names):
            entry[name] = row[idx]
        entries.append(entry)
    if not _STARTUP_SYMBOL_SET:
        return entries
    filtered: list[dict] = []
    for entry in entries:
        sym = str(entry.get("symbol") or "").upper()
        if sym in _STARTUP_SYMBOL_SET:
            entry = dict(entry)
            entry["symbol"] = sym
            filtered.append(entry)
    return filtered


def _get_watchlist() -> list[str]:
    try:
        symbols = _fetch_watchlist_symbols()
        if symbols:
            return symbols
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] watchlist fetch failed: {exc}")

    fallback_env = os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",")
    fallback = _filter_startup_symbols(fallback_env)
    if fallback:
        return fallback
    return STARTUP_SYMBOLS


def _bootstrap_backfill_symbol(symbol: str, *, days: int = BOOTSTRAP_BACKFILL_DAYS) -> tuple[int, int]:
    now = _now_utc()
    start = now - timedelta(days=days)
    end = now + timedelta(minutes=1)
    api_symbol = polygon_ingest.SYMBOL_API_MAP.get(symbol, symbol)
    bars = polygon_ingest.polygon_aggs_1m(api_symbol, start, end)
    if not isinstance(bars, list):
        return 0, 0

    with sqlite3.connect(DB_PATH) as conn:
        inserted = polygon_ingest.upsert_bars(conn, symbol, bars, source="polygon_bootstrap")
    return len(bars), inserted


def _bootstrap_write_daily_bar(symbol: str) -> bool:
    session = _latest_session_bars(symbol)
    if not session:
        return False

    bars = session.get("bars", [])
    if not bars:
        return False

    open_val = float(bars[0]["open"])
    high_val = max(float(b["high"]) for b in bars)
    low_val = min(float(b["low"]) for b in bars)
    close_val = float(bars[-1]["close"])
    volume_val = sum(float(b.get("volume", 0.0)) for b in bars)

    session_date = session["date"]
    close_et = datetime.combine(session_date, RTH_CLOSE, tzinfo=ET)
    ts_utc = close_et.astimezone(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        price_columns = {row[1] for row in conn.execute("PRAGMA table_info(prices)")}
        columns = ["symbol", "tf", "ts", "open", "high", "low", "close"]
        values: list[object] = [symbol, "1d", ts_utc, open_val, high_val, low_val, close_val]

        if "volume" in price_columns:
            columns.append("volume")
            values.append(volume_val)
        if "source" in price_columns:
            columns.append("source")
            values.append("bootstrap")
        if "is_partial" in price_columns:
            columns.append("is_partial")
            values.append(0)

        placeholders = ",".join(["?"] * len(values))
        conn.execute(
            f"INSERT OR REPLACE INTO prices({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    return True


def _bootstrap_run_model(symbol: str) -> None:
    from model import run_model as model_run

    backup_symbols = list(getattr(model_run, "SYMBOLS", []))
    try:
        model_run.SYMBOLS = [symbol]
        model_run.main()
    finally:
        model_run.SYMBOLS = backup_symbols


def _ensure_symbol_ready_sync(symbol: str) -> dict:
    sym = symbol.upper()
    attempt_ts = _now_utc().isoformat()
    result: dict[str, object] = {
        "symbol": sym,
        "ok": False,
        "already_ready": False,
        "fetched_bars": 0,
        "inserted_bars": 0,
        "daily_written": False,
        "signal": None,
        "steps": [],
        "errors": [],
        "warnings": [],
    }

    try:
        _watchlist_record_attempt(sym, attempt_ts)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"watchlist init failed: {exc}")
        return result

    freshness_limit = DATA_STALE_MAX_MIN if DATA_STALE_MAX_MIN > 0 else 3.0
    existing_signal = get_latest_signal(sym)
    fresh = data_is_fresh(sym, tf="1m", max_min=freshness_limit)

    if fresh and existing_signal:
        result["ok"] = True
        result["already_ready"] = True
        result["signal"] = existing_signal
        _watchlist_record_success(sym, attempt_ts)
        return result

    try:
        if not symbol_supported_polygon(sym):
            raise RuntimeError("Data provider does not report trades for this symbol")
    except Exception as exc:  # noqa: BLE001
        message = f"polygon availability failed: {exc}"
        result["errors"].append(message)
        _watchlist_record_error(sym, attempt_ts, message)
        return result

    try:
        fetched, inserted = _bootstrap_backfill_symbol(sym)
        result["fetched_bars"] = fetched
        result["inserted_bars"] = inserted
        result.setdefault("steps", []).append(f"backfill fetched {fetched} bars (inserted {inserted})")
    except Exception as exc:  # noqa: BLE001
        message = f"backfill failed: {exc}"
        result["errors"].append(message)
        _watchlist_record_error(sym, attempt_ts, message)
        return result

    try:
        daily_written = _bootstrap_write_daily_bar(sym)
        result["daily_written"] = daily_written
        if daily_written:
            result.setdefault("steps", []).append("daily bar updated")
    except Exception as exc:  # noqa: BLE001
        message = f"daily build failed: {exc}"
        result["errors"].append(message)

    before_ts = existing_signal.get("ts") if isinstance(existing_signal, dict) else None
    try:
        _bootstrap_run_model(sym)
    except Exception as exc:  # noqa: BLE001
        message = f"model run failed: {exc}"
        result["errors"].append(message)

    latest_signal = get_latest_signal(sym)
    if latest_signal:
        result["signal"] = latest_signal
        latest_ts = latest_signal.get("ts")
        if latest_ts and latest_ts != before_ts:
            result.setdefault("steps", []).append(f"new signal {latest_ts}")

    fresh_after = data_is_fresh(sym, tf="1m", max_min=freshness_limit)

    if latest_signal and (fresh_after or result.get("inserted_bars", 0) > 0):
        result["ok"] = True
        if not fresh_after:
            result.setdefault("warnings", []).append("data still stale after bootstrap")
        _watchlist_record_success(sym, attempt_ts)
    else:
        if not fresh_after:
            result["errors"].append("data still stale after bootstrap")
        if not latest_signal:
            result["errors"].append("no signal available after bootstrap")
        _watchlist_record_error(sym, attempt_ts, "; ".join(str(e) for e in result["errors"]) or "bootstrap incomplete")

    print(
        "[BOOTSTRAP] {sym} ok={ok} fetched={fetched} inserted={inserted} errors={errors}".format(
            sym=sym,
            ok=result.get("ok"),
            fetched=result.get("fetched_bars"),
            inserted=result.get("inserted_bars"),
            errors=len(result.get("errors", [])),
        )
    )

    return result


async def ensure_symbol_ready(symbol: str) -> dict:
    return await asyncio.to_thread(_ensure_symbol_ready_sync, symbol)


def _format_bootstrap_result(result: dict) -> str:
    sym = result.get("symbol", "UNKNOWN")
    lines: list[str] = []

    if result.get("ok"):
        if result.get("already_ready"):
            lines.append(f"✅ **{sym}** already had fresh data and signal.")
        else:
            lines.append(f"✅ **{sym}** bootstrapped.")
            lines.append(
                "• Bars inserted: {inserted} (fetched {fetched})".format(
                    inserted=result.get("inserted_bars", 0),
                    fetched=result.get("fetched_bars", 0),
                )
            )
            if result.get("daily_written"):
                lines.append("• Daily bar updated from latest RTH session")
        signal = result.get("signal") or {}
        ts = signal.get("ts")
        prob_up = signal.get("prob_up")
        model_version = signal.get("model_version")
        if ts and prob_up is not None:
            try:
                prob_fmt = f"{float(prob_up):.3f}"
            except Exception:  # noqa: BLE001
                prob_fmt = str(prob_up)
            lines.append(f"• Signal: ts {ts} | prob_up {prob_fmt} | model {model_version}")
        elif ts:
            lines.append(f"• Signal: {ts}")
        else:
            lines.append("• Model has not produced a signal yet")
        if result.get("errors"):
            lines.append("⚠️ Notes:")
            for note in result["errors"][:3]:
                lines.append(f"• {note}")
        warnings = result.get("warnings") or []
        if warnings:
            if not result.get("errors"):
                lines.append("⚠️ Notes:")
            for warn in warnings[:3]:
                lines.append(f"• {warn}")
    else:
        lines.append(f"⚠️ **{sym}** bootstrap failed.")
        errors = result.get("errors") or ["Unknown error"]
        for err in errors[:4]:
            lines.append(f"• {err}")

    return "\n".join(lines)


def _schedule_bootstrap(symbol: str, channel: discord.abc.MessageableChannel, *, announce: bool = False) -> bool:
    sym = symbol.upper()
    existing = _bootstrap_tasks.get(sym)
    if existing and not existing.done():
        return False

    task = asyncio.create_task(_run_bootstrap(sym, channel, announce=announce))
    _bootstrap_tasks[sym] = task
    task.add_done_callback(lambda _task, s=sym: _bootstrap_tasks.pop(s, None))
    return True


async def _run_bootstrap(symbol: str, channel: discord.abc.MessageableChannel, *, announce: bool = False) -> None:
    try:
        if announce:
            await safe_send(channel, f"🔄 Bootstrapping {symbol}…", kind="status", symbol=symbol, pivots=None)

        result = await ensure_symbol_ready(symbol)
        message = _format_bootstrap_result(result)
        if message:
            await safe_send(channel, message, kind="status", symbol=symbol, pivots=None)
    except Exception as exc:  # noqa: BLE001
        print(f"[BOOTSTRAP][ERROR] {symbol}: {exc}")
        try:
            await safe_send(
                channel,
                f"⚠️ Bootstrap for {symbol} crashed: {exc}",
                kind="status",
                symbol=symbol,
                pivots=None,
            )
        except Exception:  # noqa: BLE001
            pass

def _edge(prob_up: float) -> float:
    return abs(prob_up - 0.5)


def _conv(edge: float) -> str:
    if edge < 0.02:
        return "LOW"
    if edge < 0.05:
        return "MED"
    return "HIGH"


def _edge_threshold_for_mode(mode: Optional[str]) -> float:
    mode_norm = (mode or "strict").strip().lower()
    if mode_norm == "insights":
        return AUTOPOST_EDGE_MIN_INSIGHTS
    return AUTOPOST_EDGE_MIN_STRICT


def _extract_signal_state(payload: dict) -> Optional[SignalState]:
    if not isinstance(payload, dict):
        return None

    regime = str(payload.get("pivot_regime") or payload.get("regime") or "").strip().upper()
    if not regime:
        return None

    state: SignalState = {"regime": regime}
    state["extension_mode"] = bool(payload.get("pivot_extension_mode"))

    def _safe_float(val: object) -> Optional[float]:
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    state["pivot"] = _safe_float(payload.get("pivot"))
    state["last_price"] = _safe_float(payload.get("last_price"))
    return state


def _record_signal_state(sym: str, state: Optional[SignalState]) -> None:
    if state:
        _last_signal_state[sym] = state
    else:
        _last_signal_state.pop(sym, None)


def _has_invalidation_break(prev_state: SignalState, state: SignalState) -> bool:
    pivot = state.get("pivot")
    if pivot is None:
        pivot = prev_state.get("pivot")
    if pivot is None:
        return False

    try:
        prev_price = float(prev_state.get("last_price"))
        curr_price = float(state.get("last_price"))
    except (TypeError, ValueError):
        return False

    if not math.isfinite(prev_price) or not math.isfinite(curr_price):
        return False

    def _side(price: float) -> str:
        if price > pivot:
            return "above"
        if price < pivot:
            return "below"
        return "at"

    prev_side = _side(prev_price)
    curr_side = _side(curr_price)
    if prev_side == curr_side:
        return False

    if prev_side == "above" and curr_side in {"below", "at"}:
        return True
    if prev_side == "below" and curr_side in {"above", "at"}:
        return True
    if prev_side == "at" and curr_side != "at":
        return True
    return False


def _should_post_signal(sym: str, prob_up: float, gate_mode: str, *, mode: str, state: Optional[SignalState]) -> tuple[bool, str]:
    mode_norm = (mode or "strict").strip().lower()
    e = _edge(prob_up)
    min_edge = _edge_threshold_for_mode(mode_norm)

    if gate_mode == "HARD":
        return False, "VIX gate HARD"

    if e < min_edge:
        return False, f"edge {e:.3f} < min {min_edge:.3f} ({mode_norm})"

    # cooldown per symbol
    cooldown = _env_int("AUTOPOST_SIGNAL_COOLDOWN_SEC", "600")
    now = int(_now_utc().timestamp())
    last = _last_signal_post_by_symbol.get(sym, 0)
    if now - last < cooldown:
        return False, f"cooldown {cooldown}s"

    if state:
        regime = str(state.get("regime") or "").upper()
        if regime and regime != "UNKNOWN":
            prev_state = _last_signal_state.get(sym)
            if prev_state:
                prev_regime = str(prev_state.get("regime") or "").upper()
                same_regime = prev_regime == regime
                same_extension = bool(prev_state.get("extension_mode")) == bool(state.get("extension_mode"))
                if same_regime and same_extension and not _has_invalidation_break(prev_state, state):
                    return False, "regime unchanged"

    return True, "ok"


def _get_stale_lock() -> asyncio.Lock:
    global _data_stale_lock
    if _data_stale_lock is None:
        _data_stale_lock = asyncio.Lock()
    return _data_stale_lock


def _format_ts_et(ts_iso: Optional[str]) -> str:
    if not ts_iso:
        return "unknown"
    try:
        dt_et = parse_iso(ts_iso).astimezone(ET)
        return dt_et.strftime("%Y-%m-%d %H:%M ET")
    except Exception:  # noqa: BLE001
        return str(ts_iso)


def _calc_data_stale(symbol: str = DATA_STALE_SYMBOL) -> tuple[bool, Optional[float], Optional[str]]:
    if DATA_STALE_MAX_MIN <= 0:
        return False, None, None
    bar = get_latest_bar(symbol, tf="1m")
    if not bar:
        return True, None, None
    ts_iso = bar[0]
    try:
        ts_dt = parse_iso(ts_iso)
    except Exception:  # noqa: BLE001
        return True, None, ts_iso
    age_min = (_now_utc() - ts_dt).total_seconds() / 60.0
    return age_min > DATA_STALE_MAX_MIN, age_min, ts_iso


async def _fetch_warn_channel() -> Optional[discord.abc.MessageableChannel]:
    # Ops-first: adapter health should land in #tnt-ops by default.
    warn_channel_id = DATA_STALE_WARN_CHANNEL_ID or _OPS_CHANNEL_ID or CHANNEL_ID
    if not warn_channel_id:
        return None
    channel = bot.get_channel(warn_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(warn_channel_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Unable to fetch warn channel {warn_channel_id}: {exc}")
            return None
    return channel


async def _send_stale_notification(message: str, *, key: str) -> None:
    channel = await _fetch_warn_channel()
    if channel is None:
        print(f"[WARN] {message}")
        return

    min_sec = float(_OPS_NOTICE_STALE_MIN_SEC)
    if min_sec > 0:
        now = _now_ts()
        k = (key or "").strip().lower()
        if k:
            async with _OPS_NOTICE_LOCK:
                last = _OPS_LAST_NOTICE_TS.get(k)
                if last is not None and (now - last) < min_sec:
                    return
                _OPS_LAST_NOTICE_TS[k] = now
    try:
        await safe_send(channel, message, kind="status", symbol="", pivots=None)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Failed to send stale notification: {exc}")


async def _update_data_stale_state() -> bool:
    if DATA_STALE_MAX_MIN <= 0:
        return False

    lock = _get_stale_lock()
    async with lock:
        global _data_stale_paused, _data_stale_initialized
        stale, age_min, ts_iso = _calc_data_stale()
        first_check = not _data_stale_initialized
        _data_stale_initialized = True
        if stale:
            if not _data_stale_paused:
                _data_stale_paused = True
                age_text = "unknown"
                if age_min is not None:
                    age_text = f"{age_min:.1f} min"
                ts_text = _format_ts_et(ts_iso)
                if first_check:
                    print(
                        "[WARN] Autopost stale on startup; notification suppressed to avoid duplicate Discord alerts."
                    )
                else:
                    msg = _format_ops_event(
                        tag="adapter",
                        label="data_feed",
                        symbols=[DATA_STALE_SYMBOL],
                        status="stale_data",
                        contracts={"agent": AUTOPOST_CONTRACT_VERSION, "chart": "1.0"},
                        violations=[f"DATA_STALE age_min={age_text} ts={ts_text}"],
                        audit_path=None,
                    )
                    await _send_stale_notification(msg, key=f"adapter:data_stale:{DATA_STALE_SYMBOL}")
            return True

        if _data_stale_paused:
            _data_stale_paused = False
            age_text = "unknown"
            if age_min is not None:
                age_text = f"{age_min:.1f} min"
            ts_text = _format_ts_et(ts_iso)
            msg = _format_ops_event(
                tag="adapter",
                label="data_feed",
                symbols=[DATA_STALE_SYMBOL],
                status="fresh_data",
                contracts={"agent": AUTOPOST_CONTRACT_VERSION, "chart": "1.0"},
                violations=[f"DATA_FRESH age_min={age_text} ts={ts_text}"],
                audit_path=None,
            )
            await _send_stale_notification(msg, key=f"adapter:data_fresh:{DATA_STALE_SYMBOL}")
        return False


async def _autopost_stale_guard() -> bool:
    if DATA_STALE_MAX_MIN <= 0:
        return False
    return await _update_data_stale_state()


def _safe_float(val: Optional[object]) -> Optional[float]:
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


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


def _futures_context_enabled() -> bool:
    """Return whether Futures Context should be used/rendered.

    Futures context was historically produced by the IQFeed stack. If IQFeed is disabled,
    disable futures context entirely (no gating, no section output) *unless* the user has
    explicitly configured a futures context file via FUTURES_CONTEXT_PATH.
    """
    setting = (os.getenv("FUTURES_CONTEXT_ENABLED", "1") or "1").strip().lower()
    if setting in {"0", "false", "off", "no"}:
        return False

    # If an explicit futures context path is configured, allow it even when IQFeed is off.
    explicit_path = (os.getenv("FUTURES_CONTEXT_PATH", "") or "").strip()
    if explicit_path:
        return True

    iqfeed_enabled = (os.getenv("IQFEED_ENABLED", "1") or "1").strip().lower()
    if iqfeed_enabled in {"0", "false", "off", "no"}:
        return False
    return True


def _load_futures_context_payload(now_ts: Optional[str] = None) -> dict:
    if not _futures_context_enabled():
        return {"ok": False, "disabled": True}

    path_value = FUTURES_CONTEXT_PATH
    path = Path(path_value) if not isinstance(path_value, Path) else path_value
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

    if now_ts:
        try:
            now_et = parse_iso(now_ts).astimezone(ET)
        except Exception:  # noqa: BLE001
            now_et = _now_et()
    else:
        now_et = _now_et()

    computed_ts = ctx.get("computed_ts") or payload.get("computed_ts")
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
    age_minutes = (now_et - computed_dt).total_seconds() / 60.0
    payload["age_minutes"] = age_minutes

    session = market_session_et(now_et.astimezone(timezone.utc))
    payload["session"] = session

    threshold_min = FUTURES_STALE_MINUTES_ACTIVE if session in {"PRE", "RTH"} else FUTURES_STALE_MINUTES_OFFHOURS
    if age_minutes > threshold_min:
        payload["ok"] = False
        payload["reason"] = f"stale ({int(age_minutes)} min old)"
        return payload

    payload["ok"] = bool(raw_payload.get("ok", True))
    payload["session_date"] = ctx.get("session_date") or payload.get("session_date")
    return payload


def _futures_context_status(now_ts: Optional[str] = None) -> tuple[dict, str, Optional[str]]:
    """Return futures payload plus a normalized availability flag."""

    payload = _load_futures_context_payload(now_ts)
    if payload.get("disabled"):
        return payload, "disabled", None
    if payload.get("ok", True):
        return payload, "fresh", None

    raw_reason = str(payload.get("reason") or "unavailable")
    reason_lower = raw_reason.lower()
    if "stale" in reason_lower:
        status = "stale"
    elif "missing" in reason_lower:
        status = "missing"
    else:
        status = "unavailable"
    return payload, status, raw_reason


def _format_futures_context_lines(
    payload: dict,
    *,
    post_type: str = "default",
    show_reason: bool = False,
) -> list[str]:
    if not _futures_context_enabled() or (isinstance(payload, dict) and payload.get("disabled")):
        return []
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

    def _on_range_line() -> str:
        return (
            "• ON range:"
            f" H {_fmt(_safe_float(ctx.get('on_high')))}"
            f" / L {_fmt(_safe_float(ctx.get('on_low')))}"
            f" / Anchor {_fmt(_safe_float(ctx.get('on_vwap')))}"
        )

    def _intraday_summary_line() -> Optional[str]:
        on_vwap = _safe_float(ctx.get("on_vwap"))
        last_price = _safe_float(ctx.get("last_price") or ctx.get("last"))
        if on_vwap is None:
            return None
        if last_price is None:
            return f"• ON Anchor {_fmt(on_vwap)} (latest price pending)."
        relation = "holding above" if last_price >= on_vwap else "lost"
        return f"• ES {relation} ON Anchor ({_fmt(on_vwap)})."

    if post_type in {"daily_prep", "after_hours", "pre_market", "default"}:
        lines.append(_on_range_line())
        lines.append(
            "• RTH:"
            f" H {_fmt(_safe_float(ctx.get('rth_high')))}"
            f" / L {_fmt(_safe_float(ctx.get('rth_low')))}"
        )
        lines.append(
            "• Prior RTH:"
            f" H {_fmt(_safe_float(ctx.get('prior_rth_high')))}"
            f" / L {_fmt(_safe_float(ctx.get('prior_rth_low')))}"
            f" / Close {_fmt(_safe_float(ctx.get('prior_close')))}"
        )
    elif post_type == "daily_summary":
        lines.append(_on_range_line())
    elif post_type == "focus_list":
        lines.append(_on_range_line())
    elif post_type == "intraday_update":
        short_line = _intraday_summary_line()
        if short_line:
            lines.append(short_line)
        else:
            lines.append(_on_range_line())
    else:
        lines.append(_on_range_line())

    posture_line = _derive_futures_posture(ctx)
    if posture_line:
        if post_type in {"daily_summary", "focus_list"}:
            lines.append(posture_line.replace("Overnight", "ON"))
        elif post_type not in {"intraday_update"}:
            lines.append(posture_line)

    session_date = ctx.get("session_date") or payload.get("session_date")
    computed_ts = ctx.get("computed_ts") or payload.get("computed_dt") or payload.get("computed_ts")
    if post_type not in {"daily_summary", "intraday_update", "focus_list"}:
        timestamp_bits: list[str] = []
        if isinstance(session_date, str) and session_date:
            timestamp_bits.append(session_date)
        if isinstance(computed_ts, str) and computed_ts:
            try:
                timestamp_bits.append(_format_ts_et(computed_ts))
            except Exception:
                timestamp_bits.append(str(computed_ts))
        if timestamp_bits:
            lines.append("• Updated: " + " | ".join(timestamp_bits))

    return [line for line in lines if line.strip()]


def _build_futures_section(
    *,
    post_type: str = "default",
    now_ts: Optional[str] = None,
    show_reason: bool = False,
    payload: Optional[dict] = None,
) -> str:
    if not _futures_context_enabled():
        return ""
    payload = payload if payload is not None else _load_futures_context_payload(now_ts)
    lines = _format_futures_context_lines(payload, post_type=post_type, show_reason=show_reason)
    if not lines:
        return ""
    return "\n".join([
        f"📊 Futures Context — {FUTURES_CONTEXT_LABEL}",
        *lines,
        "",
    ])


def _normalize_section_lines(raw: Optional[object], default: list[str], *, prefix: str = "• ") -> list[str]:
    if raw is None:
        return default

    candidates: list[str] = []
    if isinstance(raw, str):
        candidates.append(raw)
    elif isinstance(raw, dict):
        if isinstance(raw.get("lines"), (list, tuple)):
            candidates.extend(str(item) for item in raw["lines"])
        else:
            label = raw.get("label") or raw.get("title") or raw.get("name")
            note = raw.get("note") or raw.get("summary") or raw.get("text")
            if label and note:
                candidates.append(f"{label}: {note}")
            elif note:
                candidates.append(str(note))
            elif label:
                candidates.append(str(label))
            for key, value in raw.items():
                if key in {"label", "title", "name", "note", "summary", "text", "lines"}:
                    continue
                candidates.append(f"{key}: {value}")
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                label = item.get("label") or item.get("symbol") or item.get("name") or item.get("title")
                note = item.get("note") or item.get("summary") or item.get("text") or item.get("value")
                if label and note:
                    candidates.append(f"{label}: {note}")
                elif label:
                    candidates.append(str(label))
                elif note:
                    candidates.append(str(note))
                else:
                    candidates.append(str(item))
            else:
                candidates.append(str(item))
    else:
        candidates.append(str(raw))

    normalized: list[str] = []
    prefix_trim = prefix.strip()
    for candidate in candidates:
        clean = str(candidate).strip()
        if not clean:
            continue
        if prefix_trim and clean.startswith(prefix_trim):
            normalized.append(clean)
        else:
            normalized.append(f"{prefix}{clean}")

    return normalized or default


def _fetch_section_lines(fetcher_name: str, *, post_type: str, default: list[str]) -> list[str]:
    func = globals().get(fetcher_name)
    if not callable(func):
        return default
    try:
        result = func(post_type=post_type)
    except TypeError:
        result = func()
    except Exception:  # noqa: BLE001 - fall back to defaults when fixture fetch fails
        return default
    return _normalize_section_lines(result, default)


def _section_lines(
    explicit: Optional[object],
    fetcher_name: str,
    *,
    post_type: str,
    default: list[str],
    prefix: str = "• ",
) -> list[str]:
    if explicit is not None:
        return _normalize_section_lines(explicit, default, prefix=prefix)
    return _fetch_section_lines(fetcher_name, post_type=post_type, default=default)


def get_levels_focus(*, post_type: str = "unknown") -> list[str]:
    """Default levels fetcher for focus_list/intraday_update builders.

    Builders call this via `_section_lines()` if no explicit levels payload is provided.
    Keep it lightweight and safe: rely on cached/latest daily pivots.
    """

    symbols = _get_watchlist() if callable(globals().get("_get_watchlist")) else ["SPY", "QQQ", "IWM"]
    symbols = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if not symbols:
        symbols = ["SPY"]

    lines: list[str] = []
    for sym in symbols[:6]:
        piv = get_latest_daily_pivots(sym)
        if piv and isinstance(piv.get("piv"), dict):
            vals = piv["piv"]
            try:
                p = float(vals.get("P")) if vals.get("P") is not None else None
                r1 = float(vals.get("R1")) if vals.get("R1") is not None else None
                s1 = float(vals.get("S1")) if vals.get("S1") is not None else None
            except Exception:  # noqa: BLE001
                p = r1 = s1 = None
            if any(v is not None for v in (p, r1, s1)):
                p_s = "n/a" if p is None else f"{p:.2f}"
                r1_s = "n/a" if r1 is None else f"{r1:.2f}"
                s1_s = "n/a" if s1 is None else f"{s1:.2f}"
                lines.append(f"• {sym}: P {p_s} | R1 {r1_s} | S1 {s1_s}")
                continue
        lines.append(f"• {sym}: levels unavailable")

    return lines


def get_regime_snapshot(*, post_type: str = "unknown") -> list[str]:
    """Default regime snapshot fetcher used by focus_list/pre_market builders."""

    primary_sym = "SPY"
    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig and sig.get("prob_up") is not None else None
    edge_val: Optional[float] = _edge(prob_up) if prob_up is not None else None
    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, _confirm_note, _confirm_ctx = vix_sqqq_confirmation(tf=tf_bias)
    smiq, _pg = _resolve_market_participation(confirm_bias, edge_val)
    regime = str((smiq.get("regime") if isinstance(smiq, dict) else None) or "UNKNOWN").upper()
    bias = str(confirm_bias or "UNKNOWN").upper()
    edge_s = "n/a" if edge_val is None else f"{edge_val:.2f}"
    return [f"• Participation regime: {regime}", f"• Bias confirmation: {bias}", f"• Signal edge: {edge_s}"]


def build_daily_prep_render(symbols: Optional[Sequence[str]] = None) -> RenderedPost:
    """Plan-of-record briefing with explicit stand-down logic."""

    symbols_clean = [sym.upper() for sym in (symbols or []) if sym]
    if not symbols_clean:
        symbols_clean = list(DEFAULT_DAILY_PREP_SYMBOLS)
    else:
        symbols_clean = list(dict.fromkeys(symbols_clean))
        if symbols_clean == ["SPY"]:
            for candidate in DEFAULT_DAILY_PREP_SYMBOLS:
                if candidate not in symbols_clean:
                    symbols_clean.append(candidate)
    if not symbols_clean:
        symbols_clean = ["SPY"]

    now_et = _now_et()
    now_iso = now_et.isoformat()
    futures_payload, futures_status, futures_reason = _futures_context_status(now_iso)
    session = market_session_et(now_et.astimezone(timezone.utc))

    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, _confirm_note, _detail = vix_sqqq_confirmation(tf=tf_bias)
    primary_sym = symbols_clean[0]
    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig else None
    edge_val = _edge(prob_up) if prob_up is not None else 0.0
    min_edge = AUTOPOST_EDGE_MIN_STRICT

    signal_regime: Optional[str] = None
    if sig and sig.get("meta"):
        try:
            sig_meta = json.loads(sig["meta"])
        except Exception:  # noqa: BLE001
            sig_meta = None
        if isinstance(sig_meta, dict):
            regime_val = sig_meta.get("regime") or sig_meta.get("market_regime")
            if isinstance(regime_val, str):
                signal_regime = regime_val.upper()

    stand_down_reasons: list[str] = []
    if confirm_bias in ("NEUTRAL", "UNKNOWN"):
        stand_down_reasons.append(f"Directional confirmation {confirm_bias.lower()}.")
    if prob_up is None:
        stand_down_reasons.append("Primary signal offline (no current probability).")
    elif edge_val < min_edge:
        stand_down_reasons.append(f"Signal edge {edge_val:.2f} below strict floor {min_edge:.2f}.")
    if futures_status not in {"fresh", "disabled"}:
        reason = futures_reason or futures_status
        stand_down_reasons.append(f"Futures context {reason}.")

    smiq, pg = _resolve_market_participation(confirm_bias, edge_val)
    participation_caution: Optional[str] = None
    if pg.impact == "STAND_DOWN":
        conflict_reason = _summarize_participation_conflict(pg)
        if conflict_reason not in stand_down_reasons:
            stand_down_reasons.append(conflict_reason)
    elif pg.impact == "CAUTION":
        participation_caution = _summarize_participation_caution(confirm_bias, pg)

    pg_dict = _gate_result_to_dict(pg)
    impact = pg_dict.get("impact")
    if impact == "CAUTION":
        pg_dict["reason"] = _summarize_participation_caution(confirm_bias, pg)
    elif impact == "STAND_DOWN":
        pg_dict["reason"] = _summarize_participation_conflict(pg)

    market_participation_ctx = {
        "raw": smiq,
        "gate": pg_dict,
    }

    stand_down = bool(stand_down_reasons)
    stand_down_due_to_futures = futures_status not in {"fresh", "disabled"} and any(
        "Futures context" in reason for reason in stand_down_reasons
    )
    primary_reason = stand_down_reasons[0] if stand_down_reasons else ""

    def _summarize_clear_condition(reason: str) -> str:
        reason_clean = (reason or "").rstrip(".")
        if reason_clean.startswith("Signal edge"):
            return "signal edge lifts back above the strict floor"
        if reason_clean.startswith("Directional confirmation"):
            return "directional confirmation reconfirms"
        if reason_clean.startswith("Primary signal offline"):
            return "the primary signal is back online"
        if reason_clean.lower().startswith("market participation"):
            return "market participation gate clears"
        if "Futures context" in reason_clean:
            return "futures context refreshes"
        return "stand-down triggers clear"

    clear_condition_summary = _summarize_clear_condition(primary_reason)
    status_label = "STAND DOWN" if stand_down else "ACTIVE"
    edge_display = f"{edge_val:.2f}" if prob_up is not None else "n/a"

    lines: list[str] = []
    lines.append("📌 **Daily Prep — Plan of Record**")
    lines.append(f"ET stamp: {now_et.strftime('%Y-%m-%d %H:%M')} | Session: {session}")
    lines.append(f"Status: **{status_label}** | Bias: **{confirm_bias}** | Edge: {edge_display}")

    last_price_block = _format_last_price_section(
        symbols_clean,
        now_et=now_et,
        session=session,
    )
    if last_price_block.strip():
        lines.append("")
        lines.append(last_price_block.rstrip("\n"))

    lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))

    if stand_down:
        lines.append("")
        lines.append("🚫 Stand-Down Triggers:")
        for reason in stand_down_reasons:
            lines.append(f"• {reason}")
    else:
        lines.append("")
        lines.append("✅ Playbook Focus:")
        if participation_caution:
            lines.append(f"• Participation caution: {participation_caution}")
        lines.append("• Work with clean breaks/retests at Pivot, S1, or R1.")
        lines.append("• Size down if confirmation softens mid-session.")

    lines.append("")
    lines.append("⏱ Timeframes:")
    lines.append("• Execution: 5m")
    lines.append("• Structure: 60m")
    lines.append("• Context: 1D")

    lines.append("")
    lines.append("🔎 Regime Read:")
    regime_note = "• Transitioning at key levels — wait for cash tape confirmation."
    lines.append(regime_note)

    futures_block = _build_futures_section(
        post_type="daily_prep",
        now_ts=now_iso,
        show_reason=True,
        payload=futures_payload,
    )
    if futures_block:
        lines.append("")
        lines.append(futures_block.rstrip("\n"))

    pivot_map: Dict[str, Any] = {}
    lines.append("")
    lines.append("📐 Key Levels (RTH):")
    for sym in symbols_clean:
        dp = get_latest_daily_pivots(sym)
        pivot_map[sym] = dp
        if not dp or "piv" not in dp:
            lines.append(f"• **{sym}**: (no pivots yet)")
            continue
        piv = dp["piv"]
        p_val = float(piv["P"])
        r1 = float(piv["R1"])
        s1 = float(piv["S1"])
        lines.append(f"• **{sym}**: P {p_val:.2f} | R1 {r1:.2f} | S1 {s1:.2f}")

    lines.append("")
    options_insert_idx = len(lines)
    lines.append("🧠 How Pros Would Trade It:")
    if stand_down:
        if stand_down_due_to_futures:
            lines.append("• Stand-down: wait for futures context to refresh and bias to reconfirm before deploying risk.")
        else:
            lines.append(f"• Stand-down: stay sidelined until {clear_condition_summary}.")
        lines.append("• Alternate: keep prep manual; let confirmation + liquidity rebuild before re-engaging.")
    else:
        lines.append("• Trend setup: break + hold above Pivot/S1/R1 before adding risk.")
        lines.append("• Range setup: fade edges only with confirmation plus volume agreement.")

    lines.append("")
    lines.append("🛡 Risk Controls:")
    if stand_down:
        if stand_down_due_to_futures:
            lines.append("• Invalidation: stay sidelined until futures context refreshes and bias reconfirms.")
        else:
            lines.append(f"• Invalidation: stand-down clears once {clear_condition_summary}.")
    else:
        lines.append("• Invalidation: lose Pivot/S1 with momentum or bias flips NEUTRAL/UNKNOWN.")

    lines.append("")
    lines.append("🚫 Do Nothing If:")
    lines.append("• Confirmation flips NEUTRAL/UNKNOWN around Pivot.")
    if futures_status not in {"fresh", "disabled"}:
        lines.append("• Futures context stays stale or a high-impact macro release is <15m away.")
    else:
        example_reason = primary_reason.rstrip(".") if primary_reason else "stand-down triggers remain"
        lines.append(f"• Stand-down triggers remain unresolved (e.g., {example_reason}).")

    lines.append("")
    lines.append(f"ET stamp: {now_et.strftime('%Y-%m-%d %H:%M ET')}")
    lines.append("_Not financial advice._")

    section_data: Dict[str, Any] = {
        "futures_context": futures_payload,
        "pivots": pivot_map,
        "stand_down_reasons": stand_down_reasons,
        "regime_notes": [regime_note.replace("• ", "")],
        "market_participation": market_participation_ctx,
    }

    extra_meta: Dict[str, Any] = {
        "futures_status": futures_status,
        "stand_down": stand_down,
        "bias": confirm_bias,
        "edge": edge_val,
        "participation_gate": pg_dict,
    }
    if prob_up is not None:
        extra_meta["prob_up"] = prob_up

    agent_payload = _build_agent_payload(
        symbols_clean,
        generated_at=now_et,
        post_type="daily_prep",
        extra_meta=extra_meta,
        sections=section_data,
        market_participation=market_participation_ctx,
    )

    technical_state = _extract_technical_state(agent_payload) or "UNKNOWN"
    primary_pivot = pivot_map.get(primary_sym)
    pivot_present = bool(
        isinstance(primary_pivot, dict)
        and isinstance(primary_pivot.get("piv"), dict)
        and primary_pivot["piv"].get("P") is not None
    )
    bias_label = "BULL"
    if confirm_bias == "BEARISH":
        bias_label = "BEAR"
    elif confirm_bias not in {"BULLISH", "BEARISH"}:
        bias_label = "NEUTRAL"

    options_meta = {
        "bias": bias_label,
        "confirm": confirm_bias,
        "regime": signal_regime or "UNKNOWN",
        "pivot_present": pivot_present,
        "stand_down": stand_down,
        "technical_state": technical_state,
    }

    trend_strength = extra_meta.get("trend_strength") or section_data.get("trend_strength") if isinstance(section_data, dict) else None
    if isinstance(trend_strength, (int, float)):
        options_meta["trend_score"] = float(trend_strength)

    options_section = build_options_framework(options_meta)
    if options_section and not validate_options_framework(options_section):
        insertion = options_section.splitlines() + [""]
        max_chars = int(DEFAULT_MAX_CHARS_BY_PAYLOAD.get("daily_prep", DEFAULT_MAX_CHARS_DEFAULT))
        trial_lines = list(lines)
        trial_lines[options_insert_idx:options_insert_idx] = insertion
        if len("\n".join(trial_lines)) <= max_chars:
            lines[options_insert_idx:options_insert_idx] = insertion

    return _render_with_agent_context(
        lines,
        symbols_clean,
        generated_at=now_et,
        post_type="daily_prep",
        extra_meta=extra_meta,
        sections=section_data,
        market_participation=market_participation_ctx,
        precomputed_agent_payload=agent_payload,
    )


def build_daily_prep_payload(symbols: list[str]) -> str:
    render = build_daily_prep_render(symbols)
    return render.text


def build_after_hours_render(
    symbols: Optional[Sequence[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> RenderedPost:
    symbols_clean = [sym.upper() for sym in (symbols or []) if sym]

    now_et = _now_et()
    futures_payload, futures_status, _ = _futures_context_status(now_et.isoformat())
    lines: list[str] = []
    lines.append("🌙 **After Hours Rundown**")
    lines.append(f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    futures_block = _build_futures_section(
        post_type="after_hours",
        now_ts=now_et.isoformat(),
        show_reason=True,
        payload=futures_payload,
    )
    if futures_block:
        lines.append(futures_block.rstrip("\n"))
        lines.append("")

    if symbols_clean:
        lines.append("📊 Coverage:")
        lines.append(f"• Monitoring {', '.join(symbols_clean)} into tomorrow.")
        lines.append("")

    regime_lines = _section_lines(
        regime,
        "get_regime_snapshot",
        post_type="after_hours",
        default=["• Regime snapshot pending."],
    )
    lines.append("📈 Regime Snapshot:")
    lines.extend(regime_lines)
    lines.append("")

    level_lines = _section_lines(
        levels,
        "get_levels_focus",
        post_type="after_hours",
        default=["• Levels fixture pending."],
    )
    lines.append("📐 Levels To Review:")
    lines.extend(level_lines)
    lines.append("")

    options_lines = _section_lines(
        options,
        "get_options_focus",
        post_type="after_hours",
        default=["• Options flow placeholder."],
    )
    lines.append("📝 Options Flow:")
    lines.extend(options_lines)
    lines.append("")
    lines.append("_Not financial advice._")

    section_data: Dict[str, Any] = {
        "futures_context": futures_payload,
        "regime_lines": regime_lines,
        "levels_lines": level_lines,
        "options_focus": options_lines,
    }

    return _render_with_agent_context(
        lines,
        symbols_clean,
        generated_at=now_et,
        post_type="after_hours",
        extra_meta={"futures_status": futures_status},
        sections=section_data,
    )


def build_after_hours_payload(
    symbols: Optional[list[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    render = build_after_hours_render(symbols, regime=regime, levels=levels, options=options)
    return render.text


def build_pre_market_render(
    symbols: Optional[Sequence[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> RenderedPost:
    symbols_clean = [sym.upper() for sym in (symbols or []) if sym]

    now_et = _now_et()
    futures_payload, futures_status, _ = _futures_context_status(now_et.isoformat())
    lines: list[str] = []
    lines.append("🌅 **Pre-Market Briefing**")
    lines.append(f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    futures_block = _build_futures_section(
        post_type="pre_market",
        now_ts=now_et.isoformat(),
        show_reason=True,
        payload=futures_payload,
    )
    if futures_block:
        lines.append(futures_block.rstrip("\n"))
        lines.append("")

    if symbols_clean:
        lines.append("🎯 Opening Watch:")
        lines.append(f"• Primary symbols: {', '.join(symbols_clean)}")
        lines.append("")

    regime_lines = _section_lines(
        regime,
        "get_regime_snapshot",
        post_type="pre_market",
        default=["• Regime snapshot pending."],
    )
    lines.append("📈 Regime Snapshot:")
    lines.extend(regime_lines)
    lines.append("")

    level_lines = _section_lines(
        levels,
        "get_levels_focus",
        post_type="pre_market",
        default=["• Levels fixture pending."],
    )
    lines.append("📐 Key Levels For The Open:")
    lines.extend(level_lines)
    lines.append("")

    options_lines = _section_lines(
        options,
        "get_options_focus",
        post_type="pre_market",
        default=["• Options flow placeholder."],
    )
    lines.append("📝 Options Flow Highlights:")
    lines.extend(options_lines)
    lines.append("")
    lines.append("_Not financial advice._")

    section_data: Dict[str, Any] = {
        "futures_context": futures_payload,
        "regime_lines": regime_lines,
        "levels_lines": level_lines,
        "options_focus": options_lines,
    }

    return _render_with_agent_context(
        lines,
        symbols_clean,
        generated_at=now_et,
        post_type="pre_market",
        extra_meta={"futures_status": futures_status},
        sections=section_data,
    )


def build_pre_market_payload(
    symbols: Optional[list[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    render = build_pre_market_render(symbols, regime=regime, levels=levels, options=options)
    return render.text


def build_focus_list_render(
    symbols: Optional[Sequence[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> RenderedPost:
    now_et = _now_et()
    now_iso = now_et.isoformat()
    futures_payload, futures_status, futures_reason = _futures_context_status(now_iso)

    stand_down_reasons: list[str] = []
    futures_guard_reason = futures_reason or futures_status
    if futures_status != "fresh":
        stand_down_reasons.append(f"Futures context {futures_guard_reason}.")

    symbols_clean = [sym.upper() for sym in (symbols or STARTUP_SYMBOLS) if sym]
    if not symbols_clean:
        symbols_clean = ["SPY"]
    primary_sym = symbols_clean[0]

    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, _confirm_note, _confirm_ctx = vix_sqqq_confirmation(tf=tf_bias)
    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig and sig.get("prob_up") is not None else None
    edge_val: Optional[float] = _edge(prob_up) if prob_up is not None else None

    smiq, pg = _resolve_market_participation(confirm_bias, edge_val)
    participation_caution: Optional[str] = None
    if pg.impact == "STAND_DOWN":
        stand_down_reasons.append(_summarize_participation_conflict(pg))
    elif pg.impact == "CAUTION":
        participation_caution = _summarize_participation_caution(confirm_bias, pg)

    stand_down = bool(stand_down_reasons)
    stand_reason = stand_down_reasons[0] if stand_down_reasons else None
    pg_dict = _gate_result_to_dict(pg)
    impact = pg_dict.get("impact")
    if impact == "CAUTION":
        pg_dict["reason"] = _summarize_participation_caution(confirm_bias, pg)
    elif impact == "STAND_DOWN":
        pg_dict["reason"] = _summarize_participation_conflict(pg)
    market_participation_ctx = {"raw": smiq, "gate": pg_dict}

    # Hard gate: if price/levels disagree in scale, do not post anything else.
    integrity_message = PREFLIGHT_INTEGRITY_STANDDOWN_TEXT
    primary_snap = _get_last_price_snapshot(primary_sym)
    primary_price = primary_snap.px
    dp_primary = get_latest_daily_pivots(primary_sym)
    primary_pivot = None
    if dp_primary and isinstance(dp_primary.get("piv"), dict):
        try:
            primary_pivot = float(dp_primary["piv"].get("P")) if dp_primary["piv"].get("P") is not None else None
        except Exception:  # noqa: BLE001
            primary_pivot = None
    integrity_conflict = False
    if primary_price is not None and primary_pivot is not None and primary_price > 0 and primary_pivot > 0:
        rel = abs(primary_price - primary_pivot) / max(primary_price, primary_pivot)
        if rel > 0.20 and abs(primary_price - primary_pivot) > 10.0:
            integrity_conflict = True

    lines: list[str] = []
    lines.append("🎯 Focus List — Next RTH")
    lines.append(f"ET stamp: {now_et.strftime('%Y-%m-%d %H:%M')} ET")
    lines.append("")
    if integrity_conflict:
        lines.append("Stand-Down: **ON**")
        lines.append(f"Reason: {integrity_message}")
        lines.append("")
        lines.append(FOOTER_DISCLAIMER)

        return _render_with_agent_context(
            lines,
            symbols_clean,
            generated_at=now_et,
            post_type="focus_list",
            extra_meta={
                "futures_status": futures_status,
                "stand_down": True,
                "stand_reason": integrity_message,
                "data_integrity_conflict": True,
            },
            sections={
                "futures_context": futures_payload,
                "market_participation": market_participation_ctx,
            },
            market_participation=market_participation_ctx,
        )

    lines.append(f"Stand-Down: **{'ON' if stand_down else 'OFF'}**")
    if stand_down:
        lines.append(f"Reason: {stand_reason or 'Stand-down engaged.'}")
    else:
        lines.append("Reason: n/a")

    if participation_caution:
        lines.append(f"Participation caution: {participation_caution}")

    aggression_label = "PROHIBITED" if stand_down else "REDUCED"
    momentum_only = "false"
    no_trade = "true" if stand_down else "false"
    reason_bits = [bit for bit in [stand_reason, participation_caution] if bit]
    reasons_csv = "; ".join(reason_bits) if reason_bits else "n/a"
    lines.append(
        f"🚦 Permissions: Aggression **{aggression_label}** | Momentum-only: {momentum_only} | No-trade: {no_trade}"
    )
    lines.append(f"Reason(s): {reasons_csv}")

    participation_section = _format_participation_section(smiq, pg_dict).rstrip("\n")
    lines.append("")
    lines.append(participation_section)

    futures_block = _build_futures_section(
        post_type="focus_list",
        now_ts=now_iso,
        show_reason=True,
        payload=futures_payload,
    ).rstrip("\n")
    if futures_block:
        lines.append("")
        lines.append(futures_block)

    # Guidance
    default_behavior = "stand-down" if stand_down else ("reduced participation" if participation_caution else "reduced participation")
    one_line_focus = "Confirm posture only at key zones; wait for gates to resolve."
    lines.append("")
    lines.append("📈 Guidance")
    lines.append(f"• Primary focus: {one_line_focus}")
    lines.append(f"• Default behavior: {default_behavior}")

    regime_lines = _section_lines(
        regime,
        "get_regime_snapshot",
        post_type="focus_list",
        default=["• Regime snapshot pending."],
    )
    lines.append("")
    lines.append("📈 Regime Snapshot")
    lines.extend(regime_lines)

    # Focus symbols
    lines.append("")
    lines.append("🔭 Focus Symbols")
    for sym in symbols_clean:
        lines.append(f"• {sym}: observe behavior at key zones; wait for posture confirmation.")

    # Levels in play
    level_lines = _section_lines(
        levels,
        "get_levels_focus",
        post_type="focus_list",
        default=["Levels pending."],
    )
    lines.append("")
    lines.append("📐 Levels In Play")
    lines.extend(level_lines)

    # Options environment (context only)
    options_lines = _section_lines(
        options,
        "get_options_focus",
        post_type="focus_list",
        default=[
            "• Gamma: NORMAL",
            "• Theta: NORMAL",
            "• Vol Regime: NORMAL",
        ],
    )

    def _extract_value(prefixes: tuple[str, ...]) -> Optional[str]:
        for raw in options_lines:
            text = str(raw or "").strip()
            if not text:
                continue
            text = text.lstrip("•").strip()
            low = text.lower()
            for p in prefixes:
                if low.startswith(p):
                    if ":" in text:
                        return text.split(":", 1)[1].strip() or None
                    return text[len(p) :].strip() or None
        return None

    gamma_val = _extract_value(("gamma", "gamma:")) or "NORMAL"
    theta_val = _extract_value(("theta", "theta:")) or "NORMAL"
    vol_val = _extract_value(("vol regime", "vol_regime", "vol", "vol:")) or "NORMAL"
    lines.append("")
    lines.append("🧠 Options Environment (context only)")
    lines.append(f"• Gamma: {gamma_val}")
    lines.append(f"• Theta: {theta_val}")
    lines.append(f"• Vol Regime: {vol_val}")

    # Guardrails
    lines.append("")
    lines.append("🚫 Guardrails")
    lines.append("• If data becomes stale/degraded, stand down.")
    lines.append("• If market participation conflicts with bias, reduce participation.")
    lines.append("• If futures context is stale, stand down until refresh.")

    lines.append("")
    lines.append(FOOTER_DISCLAIMER)
    extra_meta: Dict[str, Any] = {
        "futures_status": futures_status,
        "stand_down": stand_down,
        "bias": confirm_bias,
        "edge": edge_val,
        "participation_gate": pg_dict,
    }
    if prob_up is not None:
        extra_meta["prob_up"] = prob_up
    if stand_down and stand_reason:
        extra_meta["stand_reason"] = stand_reason

    section_data: Dict[str, Any] = {
        "futures_context": futures_payload,
        "regime_lines": regime_lines,
        "levels_lines": level_lines,
        "options_focus": options_lines,
        "market_participation": market_participation_ctx,
    }

    return _render_with_agent_context(
        lines,
        symbols_clean,
        generated_at=now_et,
        post_type="focus_list",
        extra_meta=extra_meta,
        sections=section_data,
        market_participation=market_participation_ctx,
    )


def build_focus_list_payload(
    symbols: Optional[list[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    render = build_focus_list_render(symbols, regime=regime, levels=levels, options=options)
    return render.text


def build_intraday_update_render(
    symbols: Optional[Sequence[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> RenderedPost:
    MAX_CHARS = DEFAULT_MAX_CHARS_BY_PAYLOAD.get("intraday_update", 900)

    now_et = _now_et()
    now_iso = now_et.isoformat()
    futures_payload, futures_status, futures_reason = _futures_context_status(now_iso)

    session_value = futures_payload.get("session") if isinstance(futures_payload, dict) else None
    if not session_value:
        session_value = market_session_et(now_et.astimezone(timezone.utc))
    session_label = str(session_value or "UNK").upper()

    symbols_clean = [sym.upper() for sym in (symbols or STARTUP_SYMBOLS) if sym]
    if not symbols_clean:
        symbols_clean = ["SPY"]
    primary_sym = symbols_clean[0]

    stand_down_triggers: list[dict[str, str]] = []
    input_alerts: list[str] = []

    def _join_reason(parts: Sequence[str]) -> str:
        clean_parts: list[str] = []
        for part in parts:
            if part is None:
                continue
            text = str(part).strip()
            if not text or text.lower() == "none":
                continue
            clean_parts.append(text.rstrip("."))
        if not clean_parts:
            return ""
        return ". ".join(clean_parts) + "."

    def _add_stand_down(reason: str, clear: str, guidance: Optional[str] = None) -> None:
        text = _join_reason([reason, guidance])
        clear_text = clear.strip()
        if clear_text:
            clear_text = clear_text.rstrip(".") + "."
        stand_down_triggers.append({"reason": text, "clear": clear_text or "Stand-down triggers clear."})

    def _add_input_alert(reason: str, guidance: Optional[str] = None) -> None:
        note = _join_reason([reason])
        if note:
            input_alerts.append(note)

    futures_guard_reason = futures_reason or futures_status
    if futures_status != "fresh":
        age_note: Optional[str] = None
        if isinstance(futures_payload, dict):
            age_val = futures_payload.get("age_minutes")
            if isinstance(age_val, (int, float)) and age_val >= 0:
                age_note = f"{int(round(age_val))}m old"
        reason_bits: list[str] = [f"Futures context {futures_guard_reason}"]
        if age_note and age_note not in str(futures_guard_reason):
            reason_bits[-1] = reason_bits[-1] + f" ({age_note})"
        reason_text = reason_bits[0]
        clear_summary = "Futures context refreshes with a fresh timestamp"
        if session_label == "RTH":
            _add_input_alert(reason_text)
        else:
            _add_stand_down(reason_text, clear_summary, None)

    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, confirm_note, _confirm_ctx = vix_sqqq_confirmation(tf=tf_bias)
    confirmation_short: Optional[str] = confirm_note if isinstance(confirm_note, str) and confirm_note.strip() else None
    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig and sig.get("prob_up") is not None else None
    edge_val: Optional[float] = _edge(prob_up) if prob_up is not None else None
    edge_floor = AUTOPOST_EDGE_MIN_STRICT
    plan_mode = bool(edge_val is not None and edge_val < edge_floor)
    plan_mode_reason: Optional[str] = None
    if plan_mode and edge_val is not None:
        plan_mode_reason = f"Signal edge {edge_val:.2f} below strict floor {edge_floor:.2f}"

    smiq, pg = _resolve_market_participation(confirm_bias, edge_val)
    participation_caution: Optional[str] = None
    if pg.impact == "STAND_DOWN":
        _add_stand_down(
            _summarize_participation_conflict(pg),
            "Market participation gate clears as breadth rebuilds",
        )
    elif pg.impact == "CAUTION":
        participation_caution = _summarize_participation_caution(confirm_bias, pg)

    stand_down = bool(stand_down_triggers)
    stand_reason = stand_down_triggers[0]["reason"] if stand_down_triggers else None
    pg_dict = _gate_result_to_dict(pg)
    impact = pg_dict.get("impact")
    if impact == "CAUTION":
        pg_dict["reason"] = _summarize_participation_caution(confirm_bias, pg)
    elif impact == "STAND_DOWN":
        pg_dict["reason"] = _summarize_participation_conflict(pg)
    market_participation_ctx = {"raw": smiq, "gate": pg_dict}

    last_snapshots: dict[str, PriceSnapshot] = {
        sym: _get_last_price_snapshot(sym)
        for sym in symbols_clean
    }
    last_prices: dict[str, Optional[float]] = {sym: snap.px for sym, snap in last_snapshots.items()}
    stale_flags: dict[str, bool] = {sym: snap.stale for sym, snap in last_snapshots.items()}

    def _pivot_triplet(symbol: str) -> dict[str, Optional[float]]:
        if isinstance(levels, dict):
            level_payload = levels.get(symbol)
            if isinstance(level_payload, dict):
                piv = level_payload.get("piv") or level_payload
            else:
                piv = None
        else:
            piv = None
        dp = get_latest_daily_pivots(symbol)
        if dp and isinstance(dp.get("piv"), dict):
            piv = dp["piv"]
        result = {"P": None, "R1": None, "S1": None}
        if isinstance(piv, dict):
            for key in ("P", "R1", "S1"):
                try:
                    val = piv.get(key)
                    result[key] = float(val) if val is not None else None
                except Exception:  # noqa: BLE001
                    result[key] = None
        return result

    spy_levels = _pivot_triplet("SPY")
    qqq_levels = _pivot_triplet("QQQ") if "QQQ" in symbols_clean else None

    bias_label = classify_bias(prob_up) if prob_up is not None else (confirm_bias or "UNKNOWN")
    conviction_label = classify_conviction(prob_up) if prob_up is not None else "LOW"
    regime_label = str((smiq.get("regime") if isinstance(smiq, dict) else None) or "UNKNOWN").upper()

    integrity_message = PREFLIGHT_INTEGRITY_STANDDOWN_TEXT
    integrity_conflict = False
    if last_prices.get("SPY") is not None and spy_levels.get("P") is not None:
        lp = float(last_prices["SPY"])  # type: ignore[arg-type]
        pv = float(spy_levels["P"])  # type: ignore[arg-type]
        if lp > 0 and pv > 0:
            rel = abs(lp - pv) / max(lp, pv)
            if rel > 0.20 and abs(lp - pv) > 10.0:
                integrity_conflict = True

    header = f"⏱ Intraday Update ({session_label}) — {now_et.strftime('%Y-%m-%d %H:%M ET')}"
    footer = FOOTER_DISCLAIMER

    if integrity_conflict:
        text = "\n".join([header, integrity_message, footer])
        return _render_with_agent_context(
            text.split("\n"),
            symbols_clean,
            generated_at=now_et,
            post_type="intraday_update",
            extra_meta={
                "futures_status": futures_status,
                "stand_down": True,
                "stand_reason": integrity_message,
                "data_integrity_conflict": True,
                "session": session_label,
            },
            sections={
                "futures_context": futures_payload,
                "market_participation": market_participation_ctx,
            },
            market_participation=market_participation_ctx,
        )

    last_csv = ", ".join(
        f"{sym} {last_prices.get(sym):.2f}" if isinstance(last_prices.get(sym), (int, float)) else f"{sym} n/a"
        for sym in symbols_clean
    )

    posture_line = f"🧭 Posture: {bias_label.upper()} | Regime: {regime_label} | Conv: {conviction_label.upper()}"
    aggression_label = "PROHIBITED" if stand_down else ("REDUCED" if participation_caution else "REDUCED")
    momentum_only = "true" if regime_label == "MOMENTUM" else "false"
    no_trade = "true" if stand_down else "false"
    reasons_parts: list[str] = []
    if stand_reason:
        reasons_parts.append(stand_reason)
    if participation_caution:
        reasons_parts.append(f"Participation caution: {participation_caution}")
    if plan_mode_reason:
        reasons_parts.append(plan_mode_reason)
    reasons_csv = ", ".join(reasons_parts) or "n/a"

    def _levels_compact(symbol: str, triplet: dict[str, Optional[float]]) -> str:
        def _f(v: Optional[float]) -> str:
            return "n/a" if v is None else f"{v:.2f}"
        return f"P {_f(triplet.get('P'))} / R1 {_f(triplet.get('R1'))} / S1 {_f(triplet.get('S1'))}"

    futures_alignment = "ALIGNED"
    if futures_status != "fresh":
        futures_alignment = "CONFLICT"
    else:
        posture = str((futures_payload or {}).get("posture") if isinstance(futures_payload, dict) else "").lower()
        if any(word in posture for word in ("mixed", "balance", "neutral")):
            futures_alignment = "MIXED"

    text_lines: list[str] = []
    text_lines.append(header)
    text_lines.append(f"💵 Last: {last_csv}")
    text_lines.append("")
    text_lines.append(posture_line)
    text_lines.append(
        f"🚦 Permissions: Aggression **{aggression_label}** | Stand-down: {'ON' if stand_down else 'OFF'} | Momentum-only: {momentum_only} | No-trade: {no_trade}"
    )
    text_lines.append(f"Reason(s): {reasons_csv}")
    text_lines.append("")
    text_lines.append("📐 Key Levels")
    text_lines.append(f"SPY: {_levels_compact('SPY', spy_levels)}")
    if qqq_levels:
        text_lines.append(f"QQQ: {_levels_compact('QQQ', qqq_levels)}")
    text_lines.append("")
    text_lines.append(f"🟦 Futures Alignment: {futures_alignment}")
    text_lines.append("Event Risk: NONE")
    text_lines.append("")
    text_lines.append("✅ Allowed")
    text_lines.append("• Observe posture; reduce participation until confirmation.")
    text_lines.append("")
    text_lines.append("🚫 Prohibited")
    text_lines.append("• Escalating aggression when posture is not confirmed.")
    text_lines.append("• Any action during degraded inputs.")
    text_lines.append("")
    text_lines.append("Invalidation: posture degrades or data becomes stale/degraded.")
    text_lines.append("")
    text_lines.append(footer)

    text = "\n".join(text_lines)
    if len(text) > MAX_CHARS:
        # Keep the contract-safe core lines only.
        text = "\n".join(
            [
                header,
                f"💵 Last: {last_csv}",
                posture_line,
                f"🚦 Permissions: Aggression **{aggression_label}** | Momentum-only: {momentum_only} | No-trade: {no_trade}",
                f"Reason(s): {reasons_csv}",
                footer,
            ]
        )

    render_lines = text.split("\n")
    required_lines = list(render_lines)

    section_data: Dict[str, Any] = {
        "futures_context": futures_payload,
        "market_participation": market_participation_ctx,
        "input_alerts": input_alerts,
        "stand_down_triggers": stand_down_triggers,
        "plan_mode": {
            "active": plan_mode,
            "reason": plan_mode_reason,
            "edge_floor": edge_floor,
        },
        "last_prices": {
            sym: {
                "price": last_prices.get(sym),
                "stale": stale_flags.get(sym, True),
            }
            for sym in symbols_clean
        },
        "levels": {
            "SPY": spy_levels,
            "QQQ": qqq_levels,
        },
        "optional_lines": render_lines[len(required_lines):],
    }

    extra_meta: Dict[str, Any] = {
        "futures_status": futures_status,
        "stand_down": stand_down,
        "bias": bias_label,
        "edge": edge_val,
        "participation_gate": pg_dict,
        "plan_mode": plan_mode,
        "session": session_label,
    }
    if prob_up is not None:
        extra_meta["prob_up"] = prob_up
    if stand_down and stand_reason:
        extra_meta["stand_reason"] = stand_reason
    if plan_mode and plan_mode_reason:
        extra_meta["plan_mode_reason"] = plan_mode_reason
    if participation_caution:
        extra_meta["participation_caution"] = participation_caution
    if confirmation_short:
        extra_meta["confirmation"] = confirmation_short

    return _render_with_agent_context(
        render_lines,
        symbols_clean,
        generated_at=now_et,
        post_type="intraday_update",
        extra_meta=extra_meta,
        sections=section_data,
        market_participation=market_participation_ctx,
    )


def build_intraday_update_payload(
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    render = build_intraday_update_render(regime=regime, levels=levels, options=options)
    return render.text


def build_close_recap_render(
    symbols: Optional[Sequence[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> RenderedPost:
    """Text-only close recap (v1). No charts unless explicitly added later."""

    MAX_CHARS = DEFAULT_MAX_CHARS_BY_PAYLOAD.get("recap", 900)

    now_et = _now_et()
    now_iso = now_et.isoformat()

    futures_payload, futures_status, _futures_reason = _futures_context_status(now_iso)

    symbols_clean = [sym.upper() for sym in (symbols or STARTUP_SYMBOLS) if sym]
    if not symbols_clean:
        symbols_clean = ["SPY"]
    primary_sym = symbols_clean[0]

    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, confirm_note, _confirm_ctx = vix_sqqq_confirmation(tf=tf_bias)
    confirmation_short: Optional[str] = confirm_note if isinstance(confirm_note, str) and confirm_note.strip() else None

    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig and sig.get("prob_up") is not None else None
    bias_label = classify_bias(prob_up) if prob_up is not None else (confirm_bias or "UNKNOWN")
    conviction_label = classify_conviction(prob_up) if prob_up is not None else "LOW"

    smiq, pg = _resolve_market_participation(confirm_bias, _edge(prob_up) if prob_up is not None else None)
    pg_dict = _gate_result_to_dict(pg)
    impact = pg_dict.get("impact")
    if impact == "CAUTION":
        pg_dict["reason"] = _summarize_participation_caution(confirm_bias, pg)
    elif impact == "STAND_DOWN":
        pg_dict["reason"] = _summarize_participation_conflict(pg)
    market_participation_ctx = {"raw": smiq, "gate": pg_dict}

    last_snapshots: dict[str, PriceSnapshot] = {sym: _get_last_price_snapshot(sym) for sym in symbols_clean}
    last_prices: dict[str, Optional[float]] = {sym: snap.px for sym, snap in last_snapshots.items()}

    def _pivot_triplet(symbol: str) -> dict[str, Optional[float]]:
        dp = get_latest_daily_pivots(symbol)
        piv = dp.get("piv") if dp and isinstance(dp.get("piv"), dict) else None
        result = {"P": None, "R1": None, "S1": None}
        if isinstance(piv, dict):
            for key in ("P", "R1", "S1"):
                try:
                    val = piv.get(key)
                    result[key] = float(val) if val is not None else None
                except Exception:  # noqa: BLE001
                    result[key] = None
        return result

    spy_levels = _pivot_triplet("SPY")
    qqq_levels = _pivot_triplet("QQQ") if "QQQ" in symbols_clean else None

    regime_label = str((smiq.get("regime") if isinstance(smiq, dict) else None) or "UNKNOWN").upper()
    stand_down = bool(pg_dict.get("impact") == "STAND_DOWN")
    footer = FOOTER_DISCLAIMER

    last_csv = ", ".join(
        f"{sym} {last_prices.get(sym):.2f}" if isinstance(last_prices.get(sym), (int, float)) else f"{sym} n/a"
        for sym in symbols_clean
    )

    def _levels_compact(triplet: dict[str, Optional[float]]) -> str:
        def _f(v: Optional[float]) -> str:
            return "n/a" if v is None else f"{v:.2f}"
        return f"P {_f(triplet.get('P'))} / R1 {_f(triplet.get('R1'))} / S1 {_f(triplet.get('S1'))}"

    header = f"🧾 Close Recap — {now_et.strftime('%Y-%m-%d %H:%M ET')}"
    posture_line = f"🧭 Close posture: {bias_label.upper()} | Regime: {regime_label} | Conv: {conviction_label.upper()}"
    permissions = "Stand-down: ON" if stand_down else "Stand-down: OFF"
    futures_alignment = "FRESH" if futures_status == "fresh" else "DEGRADED"

    lines: list[str] = []
    lines.append(header)
    lines.append(f"💵 Last: {last_csv}")
    lines.append("")
    lines.append(posture_line)
    if confirmation_short:
        lines.append(f"Confirm: {confirmation_short}")
    lines.append(f"🚦 {permissions} | Futures: {futures_alignment}")
    lines.append("")
    lines.append("📐 Key Levels")
    lines.append(f"SPY: {_levels_compact(spy_levels)}")
    if qqq_levels:
        lines.append(f"QQQ: {_levels_compact(qqq_levels)}")
    lines.append("")
    lines.append("What changed:")
    lines.append("• Close posture is the only thing that matters now; plan the next session, don’t chase this one.")
    lines.append("• If inputs were degraded or participation was thin, keep risk reduced until conditions improve.")
    lines.append("")
    lines.append(footer)

    text = "\n".join(lines).strip()
    if len(text) > MAX_CHARS:
        text = "\n".join([header, posture_line, f"💵 Last: {last_csv}", footer])

    extra_meta: Dict[str, Any] = {
        "futures_status": futures_status,
        "stand_down": stand_down,
        "bias": bias_label,
        "session": "CLOSE",
    }
    if prob_up is not None:
        extra_meta["prob_up"] = prob_up
    if confirmation_short:
        extra_meta["confirmation"] = confirmation_short

    return _render_with_agent_context(
        text.split("\n"),
        symbols_clean,
        generated_at=now_et,
        post_type="recap",
        extra_meta=extra_meta,
        sections={
            "futures_context": futures_payload,
            "market_participation": market_participation_ctx,
        },
        market_participation=market_participation_ctx,
    )


async def build_signal_alert_payload(sym: str, *, mode: str) -> Optional[tuple[str, str]]:
    """Build a mode-aware autopost payload, returning (text, output_mode)."""

    built = build_signal_payload(sym)
    if not built:
        return None

    payload, prob_up, gate = built
    gate_mode = (gate.get("mode") or "OK").upper()

    mode_norm = (mode or "strict").strip().lower()
    mode_norm = mode_norm if mode_norm in VALID_MODES else "strict"

    edge_val = _edge(prob_up)
    payload["edge"] = edge_val
    payload["edge_threshold"] = _edge_threshold_for_mode(mode_norm)
    strict_floor = AUTOPOST_EDGE_MIN_STRICT
    insights_floor = AUTOPOST_EDGE_MIN_INSIGHTS
    payload["educational_only"] = (
        mode_norm == "insights"
        and edge_val < strict_floor
        and edge_val >= insights_floor
    )

    ctx = payload.get("level_ctx")
    last_price = payload.get("last_price")
    if ctx and last_price is not None:
        ok_targets, target_err = validate_targets_vs_price(
            float(last_price),
            ctx.get("upside_targets"),
            ctx.get("downside_targets"),
        )
        if not ok_targets:
            reason = f"Quality gate: {target_err or 'target validation failed'}"
            print(f"[AUTOPOST] skip {sym}: {reason}")
            return None

    ok_tnt, reason_tnt = tnt_regime_gate(payload, output_mode=mode_norm)
    if not ok_tnt:
        print(f"[AUTOPOST] skip {sym}: {reason_tnt}")
        return None

    state = _extract_signal_state(payload)
    ok, reason = _should_post_signal(sym, prob_up, gate_mode, mode=mode_norm, state=state)
    if not ok:
        if reason and "cooldown" not in reason.lower() and "regime unchanged" not in reason.lower():
            print(f"[AUTOPOST] skip {sym}: {reason}")
        return None

    context_mode = (payload.get("trade_context_mode") or payload.get("analysis_mode") or "db").strip().lower()
    if context_mode not in {"db", "on_demand"}:
        context_mode = "db"

    payload["trade_context_mode"] = context_mode
    payload["analysis_mode"] = context_mode

    context_source = payload.get("trade_context_source") or payload.get("analysis_source") or "model"
    payload["trade_context_source"] = context_source
    payload["analysis_source"] = context_source

    payload["trade_context_output_mode"] = mode_norm
    payload["analysis_output_mode"] = mode_norm

    live_price_override: Optional[float] = None
    live_price_ts: Optional[str] = None
    if context_mode == "on_demand":
        if isinstance(payload.get("last_price"), (int, float)):
            live_price_override = float(payload["last_price"])
        live_price_ts = payload.get("last_price_ts") if isinstance(payload.get("last_price_ts"), str) else None

    packet, _ = build_trade_context_packet(
        sym,
        payload,
        context_mode=context_mode,
        live_price=live_price_override,
        live_price_ts=live_price_ts,
    )
    packet["trade_context_output_mode"] = payload["trade_context_output_mode"]
    packet["analysis_output_mode"] = payload["analysis_output_mode"]
    packet["edge"] = payload.get("edge")
    packet["edge_threshold"] = payload.get("edge_threshold")
    packet["educational_only"] = payload.get("educational_only")
    payload["trade_context_packet"] = packet
    payload["analysis_packet"] = packet

    if payload["trade_context_output_mode"] == "insights":
        ai_text, ai_err = await ai_render_trade_context(None, packet)
        if ai_err:
            print(f"[AUTOPOST] insights fallback {sym}: {ai_err}")
        if ai_text:
            ok_ai, reason_ai = validate_output(ai_text, mode="insights")
            if ok_ai:
                _last_signal_post_by_symbol[sym] = int(_now_utc().timestamp())
                _record_signal_state(sym, state)
                return ai_text, "insights"
            print(f"[AUTOPOST] insights blocked {sym}: {reason_ai}")

    fallback = format_signal_clean(
        sym,
        payload,
        verbose=False,
        now_et=_now_et(),
        gate=gate,
        price_snapshot=_get_last_price_snapshot(sym),
    )
    ok_fb, reason_fb = validate_output(fallback, mode="strict")
    if ok_fb:
        _last_signal_post_by_symbol[sym] = int(_now_utc().timestamp())
        _record_signal_state(sym, state)
        return fallback, "strict"

    print(f"[AUTOPOST] strict fallback blocked {sym}: {reason_fb}")
    return None


async def autopost_daily_loop(channel):
    await bot.wait_until_ready()
    global _last_daily_post_et_date

    hh, mm = _parse_hhmm(os.getenv("AUTOPOST_DAILY_TIME_ET", "09:25"))
    cooldown_hours = _env_int("AUTOPOST_DAILY_COOLDOWN_HOURS", "20")
    poll_sec = 30

    print(f"[OK] autopost_daily_loop running (time_et={hh:02d}:{mm:02d}, cooldown={cooldown_hours}h)")

    while not bot.is_closed():
        try:
            if not _env_bool("AUTOPOST_DAILY_ENABLED", "0"):
                await asyncio.sleep(poll_sec)
                continue

            max_min = float(os.getenv("DATA_STALE_MAX_MIN", "3"))
            age = latest_bar_age_min("SPY", "1m")
            if age is None or age > max_min:
                print(f"[WARN] data stale: SPY age_min={age}; autopost paused")
                await _autopost_stale_guard()
                await asyncio.sleep(poll_sec)
                continue

            if await _autopost_stale_guard():
                await asyncio.sleep(poll_sec)
                continue

            now_et = _now_et()
            # weekdays only by default
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= due and (_last_daily_post_et_date != now_et.date()):
                symbols = _get_watchlist()
                render = build_daily_prep_render(symbols)
                await _publish_autopost_render(
                    channel,
                    render,
                    builder="build_daily_prep_render",
                    label="daily_prep",
                    symbol="",
                    context_mode="db",
                    context_output_mode="strict",
                )

                _last_daily_post_et_date = now_et.date()
                print(f"[OK] daily prep posted for {now_et.date()}")

            await asyncio.sleep(poll_sec)

        except ContractViolationError as exc:
            print(f"[WARN] autopost_daily_loop contract violation: {exc}")
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(poll_sec)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] autopost_daily_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll_sec)


async def autopost_signal_loop(channel):
    await bot.wait_until_ready()
    poll = _env_int("AUTOPOST_SIGNAL_POLL_SEC", "45")
    print(f"[OK] autopost_signal_loop running (poll={poll}s)")

    while not bot.is_closed():
        try:
            if not _env_bool("AUTOPOST_SIGNAL_ENABLED", "0"):
                await asyncio.sleep(poll)
                continue

            max_min = float(os.getenv("DATA_STALE_MAX_MIN", "3"))
            age = latest_bar_age_min("SPY", "1m")
            if age is None or age > max_min:
                print(f"[WARN] data stale: SPY age_min={age}; autopost paused")
                await _autopost_stale_guard()
                await asyncio.sleep(poll)
                continue

            if await _autopost_stale_guard():
                await asyncio.sleep(poll)
                continue

            symbols = _get_watchlist()
            mode_setting = get_analysis_mode()
            # Post at most 1 alert per poll cycle to avoid spam
            posted = False
            for sym in symbols:
                result = await build_signal_alert_payload(sym, mode=mode_setting)
                if result:
                    text, output_mode = result
                    await safe_send(
                        channel,
                        text,
                            # Signal alerts are not full analysis cards; bypass the
                            # analysis-format validator to prevent confusing stand-down
                            # fallbacks like "Missing section: Last Price".
                            kind="status",
                        symbol=sym,
                        pivots=None,
                        analysis_mode="db",
                        output_mode=output_mode,
                            label="signal_alert",
                    )
                    posted = True
                    break

            await asyncio.sleep(poll)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] autopost_signal_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll)


def _ensure_autopost_task():
    """Schedules BOTH daily + signal autopost loops if enabled. Idempotent."""
    global _autopost_daily_task, _autopost_signal_task

    if not _env_bool("AUTOPOST_ENABLED", "0"):
        print("[OK] autopost: disabled (AUTOPOST_ENABLED=0)")
        return

    canary_id = CANARY_CHANNEL_ID
    if canary_id:
        channel_id = canary_id
    else:
        channel_id = _env_int("AUTOPOST_CHANNEL_ID", "0")

    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        print(f"[WARN] autopost: channel not found/invalid id={channel_id}; skipping tasks.")
        return

    if canary_id:
        print(f"[OK] autopost routed to canary channel id={channel_id}")

    if _env_bool("AUTOPOST_DAILY_ENABLED", "1"):
        if not _autopost_daily_task or _autopost_daily_task.done():
            _autopost_daily_task = bot.loop.create_task(autopost_daily_loop(channel))
            print("[OK] autopost daily task scheduled")

    if _env_bool("AUTOPOST_SIGNAL_ENABLED", "1"):
        if not _autopost_signal_task or _autopost_signal_task.done():
            _autopost_signal_task = bot.loop.create_task(autopost_signal_loop(channel))
            print("[OK] autopost signal task scheduled")


def _automation_enabled() -> bool:
    """Primary switch for go-live automation.

    Back-compat: if `TNT_AUTOMATION_ENABLED` is not set, fall back to `AUTOPOST_ENABLED`.
    """

    if os.getenv("TNT_AUTOMATION_ENABLED") is None:
        return _env_bool("AUTOPOST_ENABLED", "0")
    return _env_bool("TNT_AUTOMATION_ENABLED", "0")


def _resolve_automation_channel():
    canary_id = CANARY_CHANNEL_ID
    channel_id = canary_id or _env_int("AUTOPOST_CHANNEL_ID", "0")
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        print(f"[WARN] automation: channel not found/invalid id={channel_id}; skipping tasks.")
        return None
    if canary_id:
        print(f"[OK] automation routed to canary channel id={channel_id}")
    return channel


async def automation_focus_list_loop(channel):
    await bot.wait_until_ready()
    global _last_focus_list_et_date

    hh, mm = _parse_hhmm(os.getenv("TNT_FOCUS_LIST_TIME_ET", "08:10"))
    poll_sec = 30
    print(f"[OK] automation_focus_list_loop running (time_et={hh:02d}:{mm:02d})")

    while not bot.is_closed():
        try:
            if not _env_bool("TNT_FOCUS_LIST_ENABLED", "1"):
                await asyncio.sleep(poll_sec)
                continue

            max_min = float(os.getenv("DATA_STALE_MAX_MIN", "3"))
            age = latest_bar_age_min("SPY", "1m")
            if age is None or age > max_min:
                print(f"[WARN] data stale: SPY age_min={age}; focus_list paused")
                await _autopost_stale_guard()
                await asyncio.sleep(poll_sec)
                continue
            if await _autopost_stale_guard():
                await asyncio.sleep(poll_sec)
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= due and (_last_focus_list_et_date != now_et.date()):
                symbols = _get_watchlist()
                render = build_focus_list_render(symbols)
                primary = symbols[0].strip().upper() if symbols else "SPY"
                await _publish_autopost_render(
                    channel,
                    render,
                    builder="build_focus_list_render",
                    label="focus_list",
                    symbol=primary,
                    context_mode="db",
                    context_output_mode="strict",
                )
                _last_focus_list_et_date = now_et.date()
                print(f"[OK] focus_list posted for {now_et.date()}")

            await asyncio.sleep(poll_sec)

        except ContractViolationError as exc:
            print(f"[WARN] automation_focus_list_loop contract violation: {exc}")
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(poll_sec)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_focus_list_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll_sec)


async def automation_intraday_update_loop(channel):
    await bot.wait_until_ready()
    global _last_intraday_update_et_date

    hh, mm = _parse_hhmm(os.getenv("TNT_INTRADAY_UPDATE_TIME_ET", "12:00"))
    poll_sec = 30
    print(f"[OK] automation_intraday_update_loop running (time_et={hh:02d}:{mm:02d})")

    while not bot.is_closed():
        try:
            if not _env_bool("TNT_INTRADAY_UPDATE_ENABLED", "1"):
                await asyncio.sleep(poll_sec)
                continue

            max_min = float(os.getenv("DATA_STALE_MAX_MIN", "3"))
            age = latest_bar_age_min("SPY", "1m")
            if age is None or age > max_min:
                print(f"[WARN] data stale: SPY age_min={age}; intraday_update paused")
                await _autopost_stale_guard()
                await asyncio.sleep(poll_sec)
                continue
            if await _autopost_stale_guard():
                await asyncio.sleep(poll_sec)
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= due and (_last_intraday_update_et_date != now_et.date()):
                symbols = _get_watchlist()
                render = build_intraday_update_render(symbols)
                primary = symbols[0].strip().upper() if symbols else "SPY"
                await _publish_autopost_render(
                    channel,
                    render,
                    builder="build_intraday_update_render",
                    label="intraday_update",
                    symbol=primary,
                    context_mode="db",
                    context_output_mode="strict",
                )
                _last_intraday_update_et_date = now_et.date()
                print(f"[OK] intraday_update posted for {now_et.date()}")

            await asyncio.sleep(poll_sec)

        except ContractViolationError as exc:
            print(f"[WARN] automation_intraday_update_loop contract violation: {exc}")
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(poll_sec)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_intraday_update_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll_sec)


async def automation_recap_loop(channel):
    await bot.wait_until_ready()
    global _last_recap_et_date

    hh, mm = _parse_hhmm(os.getenv("TNT_RECAP_TIME_ET", "16:10"))
    poll_sec = 30
    print(f"[OK] automation_recap_loop running (time_et={hh:02d}:{mm:02d})")

    while not bot.is_closed():
        try:
            if not _env_bool("TNT_RECAP_ENABLED", "1"):
                await asyncio.sleep(poll_sec)
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= due and (_last_recap_et_date != now_et.date()):
                symbols = _get_watchlist()
                render = build_close_recap_render([s.strip().upper() for s in symbols if s][:8])
                primary = symbols[0].strip().upper() if symbols else "SPY"
                await _publish_autopost_render(
                    channel,
                    render,
                    builder="build_close_recap_render",
                    label="recap",
                    symbol=primary,
                    context_mode="db",
                    context_output_mode="strict",
                )
                _last_recap_et_date = now_et.date()
                print(f"[OK] recap posted for {now_et.date()}")

            await asyncio.sleep(poll_sec)

        except ContractViolationError as exc:
            print(f"[WARN] automation_recap_loop contract violation: {exc}")
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(poll_sec)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_recap_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll_sec)


async def automation_heartbeat_loop(channel):
    await bot.wait_until_ready()
    global _last_heartbeat_sent_ts

    interval_min = float(os.getenv("TNT_HEARTBEAT_INTERVAL_MIN", "60") or "60")
    rth_only = _env_bool("TNT_HEARTBEAT_RTH_ONLY", "1")
    poll_sec = 30
    print(f"[OK] automation_heartbeat_loop running (interval_min={interval_min})")

    while not bot.is_closed():
        try:
            if not _env_bool("TNT_HEARTBEAT_ENABLED", "1") or interval_min <= 0:
                await asyncio.sleep(poll_sec)
                continue

            now_ts = time_lib.time()
            if _last_heartbeat_sent_ts is not None and (now_ts - _last_heartbeat_sent_ts) < interval_min * 60.0:
                await asyncio.sleep(poll_sec)
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue
            if rth_only:
                if not (RTH_OPEN <= now_et.time() <= RTH_CLOSE):
                    await asyncio.sleep(poll_sec)
                    continue

            age = latest_bar_age_min("SPY", "1m")
            queue_len = len(_QUEUE_HEAP) if isinstance(_QUEUE_HEAP, list) else 0

            parts: list[str] = []
            parts.append("online")
            parts.append(f"SPY_1m_age_min={age if age is not None else 'n/a'}")
            parts.append(f"queue_depth={queue_len}")
            if _last_focus_list_et_date is not None:
                parts.append(f"last_focus={_last_focus_list_et_date.isoformat()}")
            if _last_intraday_update_et_date is not None:
                parts.append(f"last_intraday={_last_intraday_update_et_date.isoformat()}")
            if _last_recap_et_date is not None:
                parts.append(f"last_recap={_last_recap_et_date.isoformat()}")

            msg = _format_ops_event(
                tag="heartbeat",
                label="health",
                symbols=["SPY"],
                status="ok",
                contracts={"agent": AUTOPOST_CONTRACT_VERSION, "chart": "1.0"},
                violations=[" ".join(parts)],
                audit_path=None,
            )
            await _notify_ops_quiet(client=bot, message=msg)
            _last_heartbeat_sent_ts = now_ts
            await asyncio.sleep(poll_sec)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_heartbeat_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll_sec)


def _ensure_tnt_automation_tasks() -> None:
    """Schedules at most 4 automation tasks (go-live safe set). Idempotent."""
    global _automation_focus_task, _automation_intraday_task, _automation_recap_task, _automation_heartbeat_task

    if not _automation_enabled():
        print("[OK] automation: disabled (TNT_AUTOMATION_ENABLED/AUTOPOST_ENABLED=0)")
        return

    channel = _resolve_automation_channel()
    if channel is None:
        return

    if _env_bool("TNT_FOCUS_LIST_ENABLED", "1"):
        if not _automation_focus_task or _automation_focus_task.done():
            _automation_focus_task = bot.loop.create_task(automation_focus_list_loop(channel))
            print("[OK] automation focus_list task scheduled")

    if _env_bool("TNT_INTRADAY_UPDATE_ENABLED", "1"):
        if not _automation_intraday_task or _automation_intraday_task.done():
            _automation_intraday_task = bot.loop.create_task(automation_intraday_update_loop(channel))
            print("[OK] automation intraday_update task scheduled")

    if _env_bool("TNT_RECAP_ENABLED", "1"):
        if not _automation_recap_task or _automation_recap_task.done():
            _automation_recap_task = bot.loop.create_task(automation_recap_loop(channel))
            print("[OK] automation recap task scheduled")

    if _env_bool("TNT_HEARTBEAT_ENABLED", "1"):
        if not _automation_heartbeat_task or _automation_heartbeat_task.done():
            _automation_heartbeat_task = bot.loop.create_task(automation_heartbeat_loop(channel))
            print("[OK] automation heartbeat task scheduled")


@bot.event
async def on_ready() -> None:
    user = bot.user
    guilds = ", ".join(g.name for g in bot.guilds) if bot.guilds else "n/a"
    print(f"[OK] Logged in as {user} (guilds: {guilds})")

    # Retired commands: these are handled via mention routing (cli.discord_bot).
    # Prevent accidental re-registration if someone enables TNT_DELIVERY_SYNC_SLASH.
    for name in ("ask", "analyze"):
        try:
            bot.tree.remove_command(name)
        except Exception:
            pass

    # Startup verification: ensure TNT is reading the on-disk system prompt.
    # Logs path + first ~60 chars + sha256 so we can spot stale/embedded prompts.
    try:
        load_tnt_system_prompt(log=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[TNT][PROMPT][ERROR] Failed to load tnt_system_prompt.txt: {exc}")
    global _slash_tree_synced
    if not _slash_tree_synced:
        # IMPORTANT: This delivery bot shares the same Discord application/token as the
        # dedicated slash bot (cli.discord_bot). Calling tree.sync() here will overwrite
        # the application's slash commands with ONLY the small subset defined in this file.
        # Default: do NOT sync from the delivery bot.
        if _env_bool("TNT_DELIVERY_SYNC_SLASH", "0"):
            try:
                await bot.tree.sync()
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Slash command sync failed: {exc}")
            else:
                _slash_tree_synced = True
                print("[OK] Slash command tree synced")
        else:
            _slash_tree_synced = True
            print("[OK] Slash command sync skipped (TNT_DELIVERY_SYNC_SLASH=0)")

    try:
        _ensure_tnt_automation_tasks()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] automation scheduler error: {exc}")


@bot.event
async def on_message(message: discord.Message) -> None:
    # Never respond to our own messages.
    if bot.user and message.author.id == bot.user.id:
        return
    # Default: ignore other bots. Optional: allow Concierge to react to alert-bot posts.
    if message.author.bot:
        if concierge_throttle is None or not concierge_throttle.allow_other_bot_messages_for_auto():
            return
    try:
        content = message.content or ""
        if content.strip().lower().startswith("!analyze"):
            sym = None
        else:
            sym = extract_analysis_request(content)
        if sym:
            dp = get_latest_daily_pivots(sym)
            pivots = dp.get("piv") if dp and isinstance(dp, dict) else None
            try:
                price_info = get_latest_price(sym)
            except Exception:  # noqa: BLE001
                price_info = None
            last_px, last_ts = price_info if price_info else (None, None)

            signal_bundle = build_signal_payload(sym)
            if signal_bundle:
                clean_payload, _prob_stub, gate_info = signal_bundle
            else:
                clean_payload, gate_info = None, None

            ai_text = maybe_ai_analysis(sym)
            if ai_text:
                ok, reason = validate_analysis_message(ai_text, label=f"analyze_{sym.lower()}")
                if ok:
                    await message.channel.send(ai_text)
                else:
                    print(f"[ANALYZE] AI blocked {sym}: {reason}")
                    if QUALITY_GATE_DEBUG and reason:
                        try:
                            await message.channel.send(
                                f"Gate blocked AI response for {sym}: `{reason}`"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    if clean_payload:
                        fallback = format_signal_clean(
                            sym,
                            clean_payload,
                            verbose=True,
                            now_et=_now_et(),
                            gate=gate_info,
                            price_snapshot=_get_last_price_snapshot(sym),
                        )
                        if reason:
                            fallback += f"\n\n_Fallback reason: {reason}."
                    else:
                        note = reason or ""
                        fallback = build_safe_fallback(sym, pivots, note=note)
                    fb_ok, fb_reason = validate_analysis_message(fallback, label=f"analyze_{sym.lower()}")
                    if not fb_ok:
                        print(f"[ANALYZE] fallback flagged {sym}: {fb_reason}")
                    await message.channel.send(fallback)
            else:
                if clean_payload:
                    fallback = format_signal_clean(
                        sym,
                        clean_payload,
                        verbose=True,
                        now_et=_now_et(),
                        gate=gate_info,
                        price_snapshot=_get_last_price_snapshot(sym),
                    )
                else:
                    fallback = build_safe_fallback(sym, pivots, note="no AI response")
                fb_ok, fb_reason = validate_analysis_message(fallback, label=f"analyze_{sym.lower()}")
                if not fb_ok:
                    print(f"[ANALYZE] fallback flagged {sym}: {fb_reason}")
                    if QUALITY_GATE_DEBUG and fb_reason:
                        try:
                            await message.channel.send(
                                f"Gate blocked fallback for {sym}: `{fb_reason}`"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                await message.channel.send(fallback)
            await bot.process_commands(message)
            return
    except Exception as exc:  # noqa: BLE001
        await message.channel.send(f"⚠️ Analyze error: {type(exc).__name__}: {exc}")
        await bot.process_commands(message)
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

    if not AI_ENABLED:
        # Concierge is separate from LLM AI; it may still be enabled.
        pass
    if message.guild is None:
        await bot.process_commands(message)
        return
    if message.content.strip().startswith("!"):
        await bot.process_commands(message)
        return

    mentioned_user = bool(bot.user and bot.user in message.mentions)
    mentioned_role = bool(AI_ROLE_ID) and any(r.id == AI_ROLE_ID for r in message.role_mentions)
    triggered = mentioned_user or mentioned_role

    # --- Concierge (deterministic, throttled; non-blocking + additive) ---
    if schedule_concierge_nudge is not None:
        schedule_concierge_nudge(
            message,
            triggered=bool(triggered),
            now_et_fn=_now_et,
            get_last_price_snapshot=_get_last_price_snapshot,
            get_latest_signal_context=get_latest_signal_context,
            get_vix_context=get_vix_context,
            vix_gating_action=vix_gating_action,
            vix_gating_enabled=VIX_GATING_ENABLED,
            safe_send=safe_send,
        )

    # --- LLM AI mention/channel mode ---
    if not AI_ENABLED:
        await bot.process_commands(message)
        return
    if AI_MODE == "mention" and not triggered:
        await bot.process_commands(message)
        return
    if AI_MODE == "channel" and message.channel.id != AI_CHANNEL_ID:
        await bot.process_commands(message)
        return

    user_text = message.content
    if bot.user:
        user_text = user_text.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    if AI_ROLE_ID:
        user_text = user_text.replace(f"<@&{AI_ROLE_ID}>", "")
    user_text = user_text.strip()
    if not user_text:
        await message.reply("Ask me a question 🙂", mention_author=False)
        await bot.process_commands(message)
        return

    # --- Decision Lock (from Concierge) ---
    # If a HARD lock is active, constrain the LLM to risk-management only.
    decision_lock_enabled = os.getenv("TNT_DECISION_LOCK_ENABLED", "1") == "1"
    if decision_lock_enabled and triggered and decision_lock is not None and ConciergeLockState is not None:
        try:
            lock = decision_lock.get_lock(
                guild_id=getattr(getattr(message, "guild", None), "id", None),
                channel_id=getattr(getattr(message, "channel", None), "id", None),
            )
        except Exception:
            lock = None
        if lock is not None and getattr(lock, "state", None) == ConciergeLockState.HARD:
            reason = str(getattr(lock, "reason", "") or "")
            try:
                ttl = int(float(os.getenv("TNT_DECISION_LOCK_TTL_SEC", "900") or "900"))
            except Exception:
                ttl = 900
            try:
                ch_name = getattr(message.channel, "name", None)
                ch_name_s = ch_name if isinstance(ch_name, str) else "(unknown)"
            except Exception:
                ch_name_s = "(unknown)"
            print(
                "decision_lock_enforced level=HARD channel={channel} reason={reason} ttl={ttl}".format(
                    channel=ch_name_s,
                    reason=(reason or "").replace("\n", " ")[:200],
                    ttl=ttl,
                )
            )
            lock_banner = (
                "DECISION LOCK: HARD\n"
                + (f"Reason: {reason}\n" if reason else "")
                + "Constraint: Do NOT provide trade entries/exits, sizing, or signals to initiate risk. "
                + "Respond with risk-management, what to watch for to unlock, and how to stand down.\n"
            )
            user_text = lock_banner + "\nUser message: " + user_text

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
    pivots_for_gate: Optional[dict] = None
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
            pivots_for_gate = pivots
            below, above = nearest_levels(last_close or float(dc), pivots, k=2)
            market_context += (
                f"Daily pivots (from 1d bar @ {dts}): P={pivots['P']:.2f} R1={pivots['R1']:.2f} S1={pivots['S1']:.2f} "
                f"R2={pivots['R2']:.2f} S2={pivots['S2']:.2f}\n"
                f"Nearest supports: {', '.join([f'{n} {v:.2f}' for n, v in below]) or 'none'}\n"
                f"Nearest resistances: {', '.join([f'{n} {v:.2f}' for n, v in above]) or 'none'}\n"
            )
        cols, sig = get_latest_signal_context(requested)
        if sig:
            data = {col: sig[idx] for idx, col in enumerate(cols)}
            prob_up = float(data.get("prob_up", 0.5) or 0.5)
            prob_down = data.get("prob_down")
            prob_down_val = float(prob_down) if prob_down is not None else (1.0 - prob_up)
            edge_val = data.get("edge")
            edge = float(edge_val) if edge_val is not None else abs(prob_up - 0.5)
            model_name = str(data.get("model_version") or data.get("model") or "unknown")
            conv = conviction_from_edge(edge)
            sig_ts = data.get("ts") or (sig[0] if len(sig) > 0 else "n/a")
            market_context += (
                f"Latest signal: ts={sig_ts}, model={model_name}, prob_up={prob_up:.3f}, prob_down={prob_down_val:.3f}, "
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
            trend_raw = data.get("trend")
            vol_z_raw = data.get("vol_z")
            if trend_raw is not None and vol_z_raw is not None:
                trend = float(trend_raw)
                vol_z = float(vol_z_raw)
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
        "Answer using ONLY TNT_STATE. If stale or no_trade, stand down.\n"
    ).strip()

    async with message.channel.typing():
        try:
            result = await call_tnt_agent_async(
                tnt_state=tnt_state,
                user_text=prompt,
                label="discord_ai",
                model=_ai_model(),
                max_output_tokens=420,
                temperature=0.25,
            )
            reply_text = (result.text or "").strip() or "(No response text returned.)"
            trimmed = reply_text[:AI_MAX_CHARS]
            await safe_send(
                message.channel,
                trimmed,
                # This is free-form assistant text, not a structured analysis card.
                kind="text",
                symbol=requested or "",
                pivots=pivots_for_gate,
                output_mode="strict",
                label="discord_ai",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[AI ERROR] {exc}")
            await message.reply(f"[AI error] {exc}", mention_author=False)

    await bot.process_commands(message)


@bot.command(name="tf", aliases=["timeframe"])
async def timeframe_cmd(ctx: commands.Context) -> None:
    lines = timeframe_block_lines(
        execution=PRICE_TF_LABEL,
        structure="60m",
        context="1D",
    )
    lines.append(f"Signal horizon: {HORIZON_MIN}m probabilistic forward estimate")
    lines.append("Guardrail: Intraday only - not for swing, overnight, or earnings plays")
    await ctx.send("\n".join(lines))


@bot.command()
async def health(ctx: commands.Context) -> None:
    now = _now_utc().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = _rows(conn, "SELECT symbol, max(ts) FROM prices GROUP BY symbol")
    lines = [f"🩺 Health Check (UTC {now})"]
    for sym, ts in rows:
        lines.append(f"- {sym}: {_format_ts(ts)}")
    await ctx.send("\n".join(lines))


AnalyzeSendFn = Callable[[str], Awaitable[None]]


async def _dispatch_analyze_request(
    *,
    channel: discord.abc.Messageable,
    symbol_input: str,
    detail: str,
    send: AnalyzeSendFn,
    source: str,
) -> None:
    raw_symbol = (symbol_input or "").strip()
    raw_detail = (detail or "").strip()

    def _is_mode_token(token: str) -> bool:
        lowered = token.lower()
        return lowered in {"insights", "verbose", "full", "strict", "brief", "short"}

    def _is_symbol_candidate(token: str) -> bool:
        if not token:
            return False
        cleaned = re.sub(r"[^A-Za-z0-9]", "", token)
        if not cleaned:
            return False
        if len(cleaned) > 6:
            return False
        return cleaned.isalpha() or (cleaned[:-1].isalpha() and cleaned[-1].isdigit())

    symbol_tokens = [token for token in re.split(r"\s+", raw_symbol) if token]
    detail_tokens_field = [token for token in re.split(r"\s+", raw_detail) if token]

    inferred_symbol = symbol_tokens[0] if symbol_tokens else ""
    combined_detail_tokens: list[str] = []
    if len(symbol_tokens) > 1:
        combined_detail_tokens.extend(symbol_tokens[1:])
    combined_detail_tokens.extend(detail_tokens_field)

    if not inferred_symbol and combined_detail_tokens:
        inferred_symbol = combined_detail_tokens[0]
        combined_detail_tokens = combined_detail_tokens[1:]

    if (not _is_symbol_candidate(inferred_symbol)
            and combined_detail_tokens
            and _is_symbol_candidate(combined_detail_tokens[0])):
        inferred_symbol = combined_detail_tokens[0]
        combined_detail_tokens = combined_detail_tokens[1:]

    symbol_input = inferred_symbol.strip() or "SPY"

    detail_tokens = [token.lower() for token in combined_detail_tokens if token]
    refresh_tokens = {"refresh", "fresh", "force", "reload", "update"}
    force_refresh = any(token in refresh_tokens for token in detail_tokens)
    if force_refresh:
        detail_tokens = [token for token in detail_tokens if token not in refresh_tokens]

    cache_mode = "no-cache" if force_refresh else "prefer-cache"
    print(f"[cmd] analyze ({source}) invoked symbol={symbol_input!r} detail_tokens={detail_tokens!r} mode={cache_mode}")

    try:
        render, _ = await _fetch_on_demand_render(symbol_input, allow_cache=not force_refresh)
    except Exception as exc:  # noqa: BLE001
        print(f"[ANALYZE][ERROR] build render failed for {symbol_input!r}: {exc}")
        await send(f"⚠️ Analyze build failed for **{symbol_input.upper()}**: {exc}")
        return

    symbol_meta, context_mode, context_output_mode, meta = _trade_context_meta_from_render(render, symbol_input)

    mode_token = next((token for token in detail_tokens if token in {"insights", "verbose", "full", "strict", "brief", "short"}), None)

    if mode_token in {"insights", "verbose", "full"}:
        context_output_mode = "insights"
    elif mode_token in {"strict", "brief", "short"}:
        context_output_mode = "strict"

    payload = render.agent_payload
    if isinstance(payload, dict):
        meta_dict = payload.get("meta")
        if not isinstance(meta_dict, dict):
            meta_dict = {}
            payload["meta"] = meta_dict
        meta_dict["trade_context_mode"] = context_mode
        meta_dict["analysis_mode"] = context_mode
        meta_dict["trade_context_output_mode"] = context_output_mode
        meta_dict["analysis_output_mode"] = context_output_mode

    if channel is None:
        await send("⚠️ Analyze aborted: channel unavailable.")
        return

    label = f"analyze_{symbol_meta.lower()}" if symbol_meta and symbol_meta != "N/A" else "analyze"

    stand_down = bool(meta.get("stand_down")) if isinstance(meta, dict) else False

    publish_kwargs: dict[str, object] = {}
    if stand_down and DEV_ALLOW_VIOLATIONS:
        publish_kwargs["strict_contracts"] = False
        publish_kwargs["allow_contract_violations"] = True

    try:
        await _publish_autopost_render(
            channel,
            render,
            builder="on_demand_analyze",
            label=label,
            symbol=symbol_meta,
            context_mode=context_mode,
            context_output_mode=context_output_mode,
            **publish_kwargs,
        )
    except ContractViolationError as exc:
        await send(f"⚠️ Analyze contract violation: {exc}")
        print(f"[ANALYZE][VIOLATION] {symbol_meta}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await send(f"⚠️ Analyze autopost failed: {exc}")
        print(f"[ANALYZE][ERROR] autopost failed for {symbol_meta}: {exc}")
        return

    missing_sections = meta.get("missing_sections") if isinstance(meta, dict) else []
    missing_list = [str(item) for item in missing_sections] if isinstance(missing_sections, (list, tuple)) else []

    if stand_down and symbol_meta not in {"", "N/A"}:
        if not missing_list or "Model Signal" in missing_list:
            _schedule_bootstrap(symbol_meta, channel)

    print(f"[cmd] analyze posted symbol={symbol_meta} stand_down={stand_down} source={source}")
    await send(f"✅ Posted analyze for **{symbol_meta}**.")


@bot.command(name="analyze")
async def analyze_cmd(ctx: commands.Context, sym: str = "SPY", detail: str = "") -> None:
    channel = ctx.channel
    if channel is None:
        await ctx.reply("⚠️ Channel unavailable for analyze.")
        return

    await _dispatch_analyze_request(
        channel=channel,
        symbol_input=sym,
        detail=detail,
        send=ctx.send,
        source="prefix",
    )


@bot.tree.command(name="analyze", description="Publish TNT analyze output to this channel")
@app_commands.describe(symbol="Ticker symbol (add tokens like 'strict' or 'refresh' after the ticker)")
async def analyze_slash(interaction: discord.Interaction, symbol: str = "SPY") -> None:
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("⚠️ Channel unavailable for analyze.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    async def _send(msg: str) -> None:
        await interaction.followup.send(msg)

    await _dispatch_analyze_request(
        channel=channel,
        symbol_input=symbol,
        detail="",
        send=_send,
        source="slash",
    )


@bot.tree.command(name="ask", description="Ask the TNT coach about a symbol")
@app_commands.describe(question="Trading question to ask the coach", symbol="Optional ticker override, e.g. TSLA")
async def ask_slash(interaction: discord.Interaction, question: str, symbol: str = "") -> None:
    channel = interaction.channel
    if channel is None:
        await interaction.response.send_message("⚠️ Channel unavailable for ask.", ephemeral=True)
        return

    question_clean = (question or "").strip()
    if not question_clean:
        await interaction.response.send_message("⚠️ Provide a question for the coach.", ephemeral=True)
        return

    override_symbol = _normalize_symbol_token(symbol) if symbol else None
    detected_symbol = _extract_symbol_from_text(question_clean)
    chosen_symbol = override_symbol or detected_symbol
    if not chosen_symbol:
        await interaction.response.send_message("Add a symbol like TSLA / SPY / NVDA so I can analyze it.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        coach_text, render, status, cache_hit, latency_ms = await run_analyze_then_coach(chosen_symbol, question_clean)
    except Exception as exc:  # noqa: BLE001
        print(f"[ASK][ERROR] {chosen_symbol.upper()}: {exc}")
        await interaction.followup.send(
            _build_coach_error_response(chosen_symbol, "Internal error"),
            ephemeral=True,
        )
        return

    trimmed = coach_text[:AI_MAX_CHARS]
    message = await safe_send(
        interaction.followup,
        trimmed,
        kind="text",
        symbol=chosen_symbol,
        pivots=None,
        analysis_mode="coach",
        output_mode="coach",
        label="ask",
    )

    if status in {"error", "format_error"}:
        render_text = render.text if isinstance(render, RenderedPost) else ""
        if render_text:
            fallback_intro = "Here’s the latest /analyze output while the coach is offline:"
            fallback = f"{fallback_intro}\n\n{render_text}"
        else:
            fallback = "Here’s the latest /analyze output while the coach is offline: (no render available)"

        fallback_trimmed = fallback[:_ai_max_chars()]
        await safe_send(
            interaction.followup,
            fallback_trimmed,
            kind="analysis",
            symbol=chosen_symbol,
            pivots=None,
            analysis_mode="coach",
            output_mode="coach-fallback",
            # Ensure the analysis validator treats this as on-demand analyze output.
            label=f"analyze_{chosen_symbol.lower()}",
        )

    message_id = getattr(message, "id", None) if message is not None else None
    try:
        ask_audit_path = _write_ask_audit(
            question=question_clean,
            symbol=chosen_symbol,
            status=status,
            cache_hit=cache_hit,
            coach_text=trimmed,
            render=render,
            channel=channel,
            latency_ms=latency_ms,
            message_id=message_id,
        )

        if status in {"error", "format_error"}:
            try:
                msg = _format_ops_event(
                    tag="llm_fail",
                    label="ask",
                    symbols=[chosen_symbol.upper()],
                    status=status,
                    contracts={"agent": AUTOPOST_CONTRACT_VERSION, "chart": "1.0"},
                    violations=["ASK_FAILED" if status == "error" else "ASK_FORMAT_ERROR"],
                    audit_path=ask_audit_path,
                )
                await _notify_ops_quiet(client=bot, message=msg)
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        print(f"[ASK][AUDIT][WARN] {chosen_symbol.upper()}: {exc}")


@bot.command(name="mode")
async def mode_cmd(ctx: commands.Context, mode: str = "") -> None:
    mode = (mode or "").strip().lower()

    if not mode:
        cur = get_analysis_mode()
        await ctx.send(f"✅ Current analysis mode: **{cur.upper()}**")
        return

    if mode not in VALID_MODES:
        await ctx.send("⚠️ Usage: `!mode insights` or `!mode strict`")
        return

    try:
        set_analysis_mode(mode)
        await ctx.send(f"✅ Mode set: **{mode.upper()}**")
    except Exception as exc:  # noqa: BLE001
        await ctx.send(f"⚠️ Could not set mode: `{exc}`")


@bot.command(name="ensure", aliases=["bootstrap"])
async def ensure_cmd(ctx: commands.Context, symbol: str = "") -> None:
    sym = (symbol or "").strip().upper()
    if not sym:
        await ctx.send("Usage: !ensure <symbol>")
        return

    scheduled = _schedule_bootstrap(sym, ctx.channel, announce=True)
    if scheduled:
        await ctx.send(f"🔄 Bootstrapping **{sym}**. I'll report back here.")
    else:
        await ctx.send(f"⏳ Bootstrap already running for **{sym}**.")


@bot.command(name="watchlist")
async def watchlist_cmd(ctx: commands.Context) -> None:
    entries = _fetch_watchlist_entries()
    if not entries:
        symbols = _get_watchlist()
        if not symbols:
            await ctx.send("⚠️ Watchlist empty.")
            return
        await ctx.send("Watchlist: " + ", ".join(symbols))
        return

    lines: list[str] = ["📋 **Watchlist**"]
    for entry in entries[:20]:
        symbol = str(entry.get("symbol") or "?").upper()
        ready = entry.get("last_ready_ts") or "n/a"
        attempt = entry.get("last_attempt_ts") or "n/a"
        error = entry.get("last_error")
        line = f"• {symbol}: ready {ready} | attempt {attempt}"
        if error:
            err_preview = str(error)[:120]
            line += f" | error {err_preview}"
        lines.append(line)

    await ctx.send("\n".join(lines))


@bot.command(name="playbook")
async def playbook_cmd(ctx: commands.Context) -> None:
    symbols = MONDAY_PLAYBOOK_SYMBOLS or ["SPY", "QQQ", "IWM"]

    built = build_signal_payload("SPY")
    payload = built[0] if built else None
    tnt = payload.get("tnt") if isinstance(payload, dict) else None
    tnt_regime = str((tnt or {}).get("regime") or "UNKNOWN").upper()

    if tnt_regime not in {"TREND", "RANGE"}:
        await ctx.send(tnt_no_trade_checklist(payload if isinstance(payload, dict) else None))
        return

    await ctx.send(format_monday_playbook(symbols))


@bot.command(name="why")
async def why_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    sym = (symbol or "").strip().upper() or "SPY"
    built = build_signal_payload(sym)
    if not built:
        await ctx.send(f"⚠️ No signal payload available for {sym}.")
        return
    payload, _prob, gate = built
    why_ctx = tnt_why_payload(payload, gate)

    tnt = payload.get("tnt") if isinstance(payload, dict) else None
    tnt_regime = str((tnt or {}).get("regime") or "UNKNOWN").upper()
    tnt_posture = str((tnt or {}).get("posture") or "UNKNOWN").upper()

    conf = why_ctx.get("confirmation") if isinstance(why_ctx.get("confirmation"), dict) else {}
    vix_gate = why_ctx.get("vix_gate") if isinstance(why_ctx.get("vix_gate"), dict) else {}

    lines: list[str] = []
    lines.append(f"🔍 **WHY — {sym}**")
    lines.append(f"• Regime: **{tnt_regime}** | Posture: **{tnt_posture}**")
    lines.append(
        f"• Confirmation: **{conf.get('state','?')}** (VIX {conf.get('vix_trend','?')}, SQQQ {conf.get('sqqq_dir','?')})"
    )
    lines.append(f"• Pivot distance: **{why_ctx.get('distance_to_pivot','n/a')}**")
    lines.append(f"• Edge/Conv: **{why_ctx.get('edge','n/a')}** / **{why_ctx.get('conviction','n/a')}**")
    lines.append(f"• VIX gate: **{vix_gate.get('mode','?')}** | {vix_gate.get('reason','n/a')}")
    lines.append(f"• Freshness: **{why_ctx.get('freshness','n/a')}**")
    lines.append("_Not financial advice._")
    await ctx.send("\n".join(lines))


@bot.command(name="daily")
async def daily_cmd(ctx: commands.Context) -> None:
    await ctx.send(format_daily_summary(DAILY_SUMMARY_SYMBOLS))


def _build_status_message(symbol: str) -> str:
    """Return the TNT status summary for the requested symbol."""

    sym = (symbol or "SPY").strip().upper() or "SPY"

    mode_setting = get_analysis_mode()

    def latest_ts(s: str, tf: str) -> Optional[str]:
        db_path = os.getenv("DB_PATH", "db/tnt.db")
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            row = cur.execute("select max(ts) from prices where symbol=? and tf=?", (s, tf)).fetchone()
        return row[0] if row else None

    bias_tf = os.getenv("BIAS_TF", "5m")
    latest_1m = latest_ts(sym, "1m")
    latest_bias = latest_ts(sym, bias_tf)

    confirm_bias, confirm_note, _ = vix_sqqq_confirmation(tf=bias_tf)
    v = get_vix_context("1m")
    vix_level = float(v["level"]) if v and "level" in v else None
    gate_mode = vix_gating_action(vix_level)["mode"] if vix_level is not None else "OK"

    sig = get_latest_signal(sym)
    prob_up = float(sig["prob_up"]) if sig else None
    reasons, next_check = explain_no_trade(
        sym,
        prob_up,
        gate_mode,
        confirm_bias,
        confirm_note,
        mode=mode_setting,
    )

    lines: list[str] = []
    lines.append(f"🧪 **TNT Status: {sym}**")
    lines.append(f"• latest 1m ts: {latest_1m}")
    lines.append(f"• latest {bias_tf} ts: {latest_bias}")
    lines.append(render_directional_confirmation_block(tf=bias_tf))
    lines.append(f"• VIX gate: **{gate_mode}**")
    lines.append("")
    lines.append("Signal:")
    if sig and prob_up is not None:
        lines.append(f"• prob_up: {prob_up:.3f} | model `{sig['model_version']}` | ts {sig['ts']}")
    else:
        lines.append("• (none)")
    lines.append("")
    lines.append("Reasons / Next:")
    for reason in reasons[:4]:
        lines.append(f"• {reason}")
    lines.append(f"• {next_check}")
    lines.append("_Not financial advice._")

    return "\n".join(lines)


@bot.command(name="status")
async def status_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    await ctx.send(_build_status_message(symbol))


@bot.tree.command(name="status", description="Show TNT status summary for a symbol")
@app_commands.describe(symbol="Ticker symbol to inspect, e.g. SPY")
async def status_slash(interaction: discord.Interaction, symbol: str = "SPY") -> None:
    message = _build_status_message(symbol)
    await interaction.response.send_message(message)


@bot.command(name="brief")
async def brief_cmd(ctx: commands.Context) -> None:
    await ctx.send(format_morning_brief())


def build_opt_payload(sym: str) -> tuple[Optional[dict], Optional[str]]:
    sig = get_latest_signal(sym)
    if not sig:
        return None, f"🧩 **{sym} Options**\n• No signal found yet."

    dp = get_latest_daily_pivots(sym)
    if not dp or "piv" not in dp:
        return None, f"🧩 **{sym} Options**\n• No daily pivots found yet."

    meta = safe_json_loads(sig.get("meta"))

    prob_up = float(sig["prob_up"])
    edge = edge_from_prob(prob_up)
    conviction = conviction_from_edge(edge)
    bias = bias_from_prob(prob_up)

    now_et = _now_et()
    session = market_session_et(now_et)
    macro_events = load_macro_events_for_date(now_et.strftime("%Y-%m-%d"))
    macro_risk = macro_within_minutes(macro_events, 60, now_et)
    earnings_risk = earnings_within_window(sym, now_et)
    v = get_vix_context("1m")
    gate = {"mode": "OK", "reason": "", "mult": 1.0}
    vix_level = None
    if v and "level" in v:
        try:
            vix_level = float(v["level"])
            gate = vix_gating_action(vix_level)
        except Exception:  # noqa: BLE001
            vix_level = None

    piv = dp["piv"]
    P = float(piv["P"])
    R1 = float(piv["R1"])
    S1 = float(piv["S1"])
    R2 = float(piv.get("R2", R1))
    S2 = float(piv.get("S2", S1))

    no_trade: list[str] = []
    if gate["mode"] == "HARD":
        no_trade.append("VIX gate is HARD (vol stress) → stand down.")
    if conviction == "LOW":
        no_trade.append(f"Low conviction (edge {edge:.3f}) → wait for confirmation / reduce frequency.")

    price_ts = meta.get("price_ts") or meta.get("last_price_ts") or ""
    em_points = meta.get("em_points")
    regime = meta.get("regime") or "unknown"
    distP = meta.get("dist_to_P")
    distR1 = meta.get("dist_to_R1")
    distS1 = meta.get("dist_to_S1")

    score = setup_score(prob_up, str(gate.get("mode", "")).upper(), conviction)

    payload = {
        "sym": sym,
        "sig": sig,
        "prob_up": prob_up,
        "edge": edge,
        "conviction": conviction,
        "bias": bias,
        "session": session,
        "gate": gate,
        "vix_level": vix_level,
        "piv": {"P": P, "R1": R1, "S1": S1, "R2": R2, "S2": S2},
        "meta": meta,
        "price_ts": price_ts,
        "em_points": em_points,
        "regime": regime,
        "dists": {"P": distP, "R1": distR1, "S1": distS1},
        "no_trade": no_trade,
        "setup_score": score,
        "macro_risk": macro_risk,
        "earnings_risk": earnings_risk,
    }
    return payload, None


def render_opt_clean(p: dict) -> str:
    sym = p["sym"]
    piv = p["piv"]
    gate_mode = p["gate"].get("mode", "OK")
    vix_level = p.get("vix_level")
    prob_up = p["prob_up"]
    edge = p["edge"]
    bias = p["bias"]
    conv = p["conviction"]
    sig = p["sig"]
    price_ts = p.get("price_ts")
    session = p.get("session", "unknown")
    badge = freshness_badge(price_ts, session)

    P, R1, S1, R2, S2 = (piv[key] for key in ("P", "R1", "S1", "R2", "S2"))

    lines: list[str] = []
    lines.append(f"🎯 **{sym} Options Toolkit (0DTE + Weeklies)**")
    lines.append(f"Signal ts: {sig.get('ts', 'n/a')} | model `{sig.get('model_version', 'n/a')}`")
    lines.append(freshness_badge(p.get("price_ts", ""), p.get("session", "unknown")))
    if price_ts:
        lines.append(f"Price ts: {price_ts}")
    status_line = f"{badge} | Session: {session}"
    if session in {"CLOSED", "WEEKEND"}:
        status_line += " | Levels only (market closed)"
    lines.append(status_line)
    lines.append(
        "Prob↑ **{up}** | Prob↓ **{down}** | Edge **{edge}** | Conviction **{conv}**".format(
            up=fmt_num(prob_up, 3),
            down=fmt_num(1 - prob_up, 3),
            edge=fmt_num(edge, 3),
            conv=conv,
        )
    )
    score_val = setup_score(prob_up, gate_mode, conv)
    lines.append(f"Setup score: **{score_val}/100**")
    macro_risk = p.get("macro_risk")
    if macro_risk:
        lines.append(
            "⚠️ Macro risk: HIGH impact {title} at {time} ET (in {delta:.0f}m)".format(
                title=macro_risk.get("title", "event"),
                time=macro_risk.get("time_et", "?"),
                delta=float(macro_risk.get("delta_min", 0.0)),
            )
        )
    earnings_risk = p.get("earnings_risk")
    if earnings_risk:
        event_dt = earnings_risk.get("event_dt")
        if isinstance(event_dt, datetime):
            event_str = event_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        else:
            event_str = "soon"
        lines.append(
            "⚠️ Earnings risk: {sym} {window} ({when}) — {ts}".format(
                sym=sym,
                window=earnings_risk.get("window", "soon"),
                when=earnings_risk.get("when", ""),
                ts=event_str,
            )
        )
    if prob_up > 0.5:
        lean = "slight BULL lean"
    elif prob_up < 0.5:
        lean = "slight BEAR lean"
    else:
        lean = "flat"
    bias_line = f"Bias: **{bias}** ({lean}) | VIX mode: **{gate_mode}**"
    if vix_level is not None:
        bias_line += f" | VIX {fmt_num(vix_level)}"
    lines.append(bias_line)

    if BIAS_CONFIRM_ENABLED:
        lines.append("")
        lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))

    lines.append("")
    lines.append("**Key levels (RTH pivots)**")
    lines.append("• P **{P}** | R1 **{R1}** | S1 **{S1}**".format(P=fmt_num(P), R1=fmt_num(R1), S1=fmt_num(S1)))

    lines.append("")
    lines.append("🟦 **PLAYBOOK 1 — TREND (break + retest)**")
    lines.append(
        "• Bull trigger: reclaim/hold above **P {P}** → target **R1 {R1}** (then R2 {R2})".format(
            P=fmt_num(P), R1=fmt_num(R1), R2=fmt_num(R2)
        )
    )
    lines.append(
        "• Bear trigger: lose/hold below **P {P}** → target **S1 {S1}** (then S2 {S2})".format(
            P=fmt_num(P), S1=fmt_num(S1), S2=fmt_num(S2)
        )
    )
    lines.append("• Confirmation: break + retest (avoid first spike).")
    lines.append(
        "• Risk: defined-risk only; invalidation = clean return back through P; add a time-stop (20–45m)."
    )
    lines.append("• Structures:")
    lines.append("  - 0DTE: debit spread (call spread if bull / put spread if bear)")
    lines.append("  - Weeklies: debit spread or diagonal (more time, less decay)")

    lines.append("")
    lines.append("🟨 **PLAYBOOK 2 — RANGE (only if chop holds)**")
    lines.append(
        "• Range zone: **S1 {S1}** ↔ **R1 {R1}** (P as magnet).".format(
            S1=fmt_num(S1), R1=fmt_num(R1)
        )
    )
    lines.append("• Only trade edges (S1/P/R1 reactions); avoid mid-range noise.")
    lines.append("• No-trade switch: strong closes outside S1/R1 → revert to TREND playbook.")
    lines.append("• Structures:")
    lines.append("  - 0DTE: small defined-risk only (experienced traders); avoid if momentum is expanding")
    lines.append("  - Weeklies: calendars/diagonals if expecting continued chop + IV shift")

    if p.get("no_trade"):
        lines.append("")
        lines.append("🚫 **Caution / No-trade flags**")
        for note in p["no_trade"][:4]:
            lines.append(f"• {note}")

    lines.append("")
    lines.append("_Not financial advice. No strikes/entries/sizing provided._")
    return clamp_lines(lines, OPT_MAX_LINES)


def render_opt_pro(p: dict) -> str:
    sym = p["sym"]
    piv = p["piv"]
    meta = p.get("meta", {})
    sig = p["sig"]
    session = p.get("session", "unknown")
    badge = freshness_badge(p.get("price_ts"), session)

    lines: list[str] = []
    lines.append(f"🧠 **{sym} OPT+ (Dense Pro)**")
    lines.append(
        "session={session} vix_mode={mode} vix={vix}".format(
            session=p.get("session", "n/a"),
            mode=p["gate"].get("mode", "OK"),
            vix=fmt_num(p.get("vix_level")),
        )
    )
    levels_note = " | Levels only (market closed)" if session in {"CLOSED", "WEEKEND"} else ""
    lines.append(f"{badge}{levels_note}")
    lines.append(
        "signal_ts={ts} model={model} prob_up={prob_up} edge={edge} conv={conv} bias={bias}".format(
            ts=sig.get("ts", "n/a"),
            model=sig.get("model_version", "n/a"),
            prob_up=fmt_num(p["prob_up"], 4),
            edge=fmt_num(p["edge"], 4),
            conv=p.get("conviction", "n/a"),
            bias=p.get("bias", "n/a"),
        )
    )
    if p["prob_up"] > 0.5:
        lean = "slight_BULL"
    elif p["prob_up"] < 0.5:
        lean = "slight_BEAR"
    else:
        lean = "flat"
    lines.append(f"lean={lean}")
    if BIAS_CONFIRM_ENABLED:
        lines.append("")
        lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    macro_risk = p.get("macro_risk")
    if macro_risk:
        lines.append(
            "macro_risk=HIGH {title} @{time}ET ({delta:.0f}m)".format(
                title=macro_risk.get("title", "event"),
                time=macro_risk.get("time_et", "?"),
                delta=float(macro_risk.get("delta_min", 0.0)),
            )
        )
    earnings_risk = p.get("earnings_risk")
    if earnings_risk:
        event_dt = earnings_risk.get("event_dt")
        if isinstance(event_dt, datetime):
            event_str = event_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        else:
            event_str = "soon"
        lines.append(
            "earnings_risk={sym} {window} ({when}) @ {ts}".format(
                sym=sym,
                window=earnings_risk.get("window", "soon"),
                when=earnings_risk.get("when", ""),
                ts=event_str,
            )
        )
    score_val = setup_score(p["prob_up"], p["gate"].get("mode", "OK"), p.get("conviction", "n/a"))
    lines.append(f"setup_score={score_val}/100")
    lines.append(
        "pivots: P={P} R1={R1} S1={S1} R2={R2} S2={S2}".format(
            P=fmt_num(piv["P"]),
            R1=fmt_num(piv["R1"]),
            S1=fmt_num(piv["S1"]),
            R2=fmt_num(piv["R2"]),
            S2=fmt_num(piv["S2"]),
        )
    )
    if p.get("price_ts"):
        lines.append(f"price_ts={p['price_ts']}")

    lines.append("")
    lines.append("meta (selected):")
    keys = [
        "regime",
        "dist_to_P",
        "dist_to_R1",
        "dist_to_S1",
        "trend_1m",
        "ret_1m",
        "ret_5m",
        "atr_1m",
        "atr_5m",
        "vol_z",
        "em_points",
        "macro_risk",
        "earnings_risk",
    ]
    for key in keys:
        if key not in meta:
            continue
        value = meta[key]
        if isinstance(value, (int, float)):
            lines.append(f"• {key}={fmt_num(value, 3)}")
        else:
            lines.append(f"• {key}={value}")

    P, R1, S1, R2, S2 = (piv[key] for key in ("P", "R1", "S1", "R2", "S2"))

    lines.append("")
    lines.append("PLAYBOOK 1: TREND")
    lines.append(f"- bull: reclaim P({fmt_num(P)}) + hold → R1({fmt_num(R1)}) → R2({fmt_num(R2)})")
    lines.append(f"- bear: lose P({fmt_num(P)}) + hold → S1({fmt_num(S1)}) → S2({fmt_num(S2)})")
    lines.append("- confirm: break + retest; avoid first impulse candle.")
    lines.append("- invalidation: clean cross back through P after entry.")
    lines.append("- time stop: 20–45m if no follow-through.")
    lines.append("- structures: 0DTE debit spreads default; weeklies debit/diagonal.")

    lines.append("")
    lines.append("PLAYBOOK 2: RANGE")
    lines.append(f"- zone: S1({fmt_num(S1)}) ↔ R1({fmt_num(R1)}); P({fmt_num(P)}) magnet.")
    lines.append("- entries: only at edges w/ rejection; no mid-range trades.")
    lines.append("- switch: strong closes outside S1/R1 → TREND playbook.")
    lines.append("- structures: 0DTE defined-risk only; weeklies calendars/diagonals.")

    if p.get("no_trade"):
        lines.append("")
        lines.append("NO-TRADE FLAGS:")
        for note in p["no_trade"]:
            lines.append(f"- {note}")

    lines.append("")
    lines.append("note: no strikes/expiry/entries; structures + triggers only. NFA.")
    return clamp_lines(lines, OPT_PLUS_MAX_LINES)


@bot.command(name="opt")
async def opt_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    sym = symbol.upper()
    payload, err = build_opt_payload(sym)
    if err:
        await ctx.send(err)
        return
    pivots = payload.get("piv") if payload else None
    await safe_send(
        ctx.channel,
        render_opt_clean(payload),
        kind="analysis",
        symbol=sym,
        pivots=pivots,
        output_mode="strict",
    )


@bot.command(name="optplus", aliases=["optp", "opt_plus"])
async def opt_plus_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    sym = symbol.upper()
    payload, err = build_opt_payload(sym)
    if err:
        await ctx.send(err)
        return
    pivots = payload.get("piv") if payload else None
    await safe_send(
        ctx.channel,
        render_opt_pro(payload),
        kind="analysis",
        symbol=sym,
        pivots=pivots,
        output_mode="strict",
    )


@bot.listen("on_message")
async def _opt_plus_alias_router(message: discord.Message) -> None:
    if message.author.bot:
        return
    content = (message.content or "").strip()
    if not content:
        return
    lowered = content.lower()
    if not any(lowered.startswith(prefix) for prefix in ("!opt+", "!opt_plus")):
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    command = bot.get_command("optplus")
    if not command:
        return

    ctx.command = command
    ctx.invoked_with = "opt+"
    await bot.invoke(ctx)


@bot.command(name="vix")
async def vix_cmd(ctx: commands.Context) -> None:
    v = get_vix_context("1m")
    if not v:
        await ctx.send("⚠️ No VIX data found in DB yet. (Check ingest symbol mapping.)")
        return
    await ctx.send(f"🧠 VIX: **{v['level']:.2f}** ({v['regime']}) as of {v['ts']}")


@bot.command(name="bias")
async def bias_cmd(ctx: commands.Context, tf: str | None = None) -> None:
    use_tf = (tf or BIAS_TF).lower()
    if use_tf not in ("1m", "5m", "15m", "60m"):
        use_tf = BIAS_TF

    bias, note, detail = vix_sqqq_confirmation(tf=use_tf)
    icon = "🟢" if bias == "BULLISH" else ("🔴" if bias == "BEARISH" else ("🟡" if bias == "NEUTRAL" else "🟠"))

    lines: list[str] = []
    lines.append("🧭 **Bias Explainer (VIX + SQQQ)**")
    lines.append(f"TF: **{use_tf}**")
    lines.append(f"• VIX trend: **{detail.get('vix_trend', '?')}**")
    lines.append(f"• SQQQ direction: **{detail.get('sqqq_dir', '?')}**")
    lines.append(f"{icon} Bias confirmation: **{bias}**")
    lines.append(f"Reason: {note}")
    lines.append("")
    lines.append("🚫 **Do Nothing If**")
    lines.append("• Bias confirmation is NEUTRAL/UNKNOWN")
    lines.append("• Price is chopping around pivot / mixed signals persist")
    lines.append("")
    lines.append("_Not financial advice._")
    await ctx.send("\n".join(lines))


@bot.command(name="regime")
async def regime_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    sym = (symbol or "").strip().upper() or "SPY"
    built = build_signal_payload(sym)
    if not built:
        await ctx.send(f"⚠️ No signal payload available for {sym}.")
        return

    payload, _prob, _gate = built
    tnt = payload.get("tnt") if isinstance(payload, dict) else None
    if not isinstance(tnt, dict):
        await ctx.send(f"⚠️ TNT regime unavailable for {sym}.")
        return

    regime = str(tnt.get("regime") or "UNKNOWN").upper()
    posture = str(tnt.get("posture") or "UNKNOWN").upper()
    conf = tnt.get("confidence")
    conf_txt = "n/a"
    if isinstance(conf, (int, float)):
        conf_txt = f"{float(conf):.2f}"

    perms = tnt.get("permissions") if isinstance(tnt.get("permissions"), dict) else {}
    def _perm(k: str) -> str:
        return str(perms.get(k) or "n/a").upper()

    reasons = tnt.get("reasons") if isinstance(tnt.get("reasons"), list) else []
    why = ", ".join(str(x) for x in reasons[:6]) if reasons else "n/a"

    flips = tnt_flip_triggers(payload)

    lines: list[str] = []
    lines.append(f"🧭 **TNT Regime** — **{sym}**")
    lines.append(f"• Regime: **{regime}** | Posture: **{posture}** | Confidence: **{conf_txt}**")
    lines.append("• Permissions:")
    lines.append(f"  - Trend: {_perm('trend_continuation')} | Pullbacks: {_perm('pullbacks')} | Breakouts: {_perm('breakouts')}")
    lines.append(f"  - Mean reversion: {_perm('mean_reversion')} | Countertrend: {_perm('countertrend')} | Size: {_perm('size')}")
    lines.append("• Flip Triggers:")
    lines.append(f"  - {flips.get('bull')}")
    lines.append(f"  - {flips.get('bear')}")
    lines.append(f"  - {flips.get('neutral')}")
    lines.append(f"• Why: {why}")
    lines.append("_Not financial advice._")
    await ctx.send("\n".join(lines))


@bot.command(name="vixtrend")
async def vixtrend_cmd(ctx: commands.Context, minutes: int = 30) -> None:
    now = get_latest_close("VIX", "1m")
    if not now:
        await ctx.send("⚠️ No VIX data found in DB yet.")
        return
    now_dt = parse_iso(now["ts"])
    past = get_close_at_or_before("VIX", "1m", (now_dt - timedelta(minutes=int(minutes))).isoformat())
    if not past:
        await ctx.send(f"⚠️ Not enough VIX history to compute {minutes}m trend.")
        return
    chg = now["close"] - past["close"]
    pct = (chg / past["close"]) * 100 if past["close"] else 0.0
    direction = "UP" if chg > 0 else ("DOWN" if chg < 0 else "FLAT")
    await ctx.send(
        f"📈 VIX trend ({minutes}m): **{direction}** | {past['close']:.2f} → {now['close']:.2f} "
        f"({chg:+.2f}, {pct:+.2f}%)"
    )


def _format_signal_details(symbol: str, signal: Dict[str, object]) -> list[str]:
    ts = signal.get("ts")
    prob_up = float(signal.get("prob_up", 0.5))
    prob_down = float(signal.get("prob_down", 0.5))
    model_version = str(signal.get("model_version") or MODEL_VERSION)
    edge = float(signal.get("edge") or abs(prob_up - 0.5))
    meta_numeric: Dict[str, float] = signal.get("meta_numeric") or {}
    arrow = "↑" if prob_up >= 0.5 else "↓"
    lines = [
        f"Signal: **{symbol}** {arrow} prob_up={prob_up:.3f} prob_down={prob_down:.3f}",
        f"Model `{model_version}` | Edge {edge:.3f} | Conv {conviction_from_edge(edge)}",
        f"Signal ts: {_format_ts(ts)}",
    ]
    if meta_numeric:
        lines.append("Meta: " + " | ".join(f"{k}={v:.2f}" for k, v in meta_numeric.items()))
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
        await ctx.send(f"⚠️ No signal found for {symbol}.")
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
        await ctx.send(f"⚠️ No pivots available for {symbol}.")
        return
    lines = [f"🎯 {symbol} pivots"]
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
        await ctx.send("⚠️ VIX context unavailable.")
        return
    mode, reason = vix_gating_decision(vix)
    msg = f"VIX {vix['level']:.2f} ({vix['regime']}) -> gate {mode}"
    if reason:
        msg += f" | {reason}"
    await ctx.send(msg)


def main() -> None:
    entrypoint = "delivery.discord_bot"
    mode = (os.getenv("TNT_RUN_MODE", "dev") or "dev").strip().lower() or "dev"

    def _normalize_env_value(value: str) -> str:
        value = value.strip()
        if len(value) >= 2:
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
        return value.strip()

    def _dotenv_get_value(path: Path, key: str) -> str | None:
        if not path.exists() or not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != key:
                continue
            return _normalize_env_value(v)
        return None

    def _resolve_discord_token() -> str | None:
        for env_key in ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"):
            raw = os.getenv(env_key)
            if raw and raw.strip():
                return _normalize_env_value(raw)
        for env_path in (Path.cwd() / ".env.local", Path.cwd() / ".env"):
            val = _dotenv_get_value(env_path, "DISCORD_BOT_TOKEN")
            if val:
                return val
        return None

    def _ensure_dir_writable(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".tnt_write_probe"
        probe.write_text("ok\n", encoding="utf-8")
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass

    def _env_onoff(name: str, default: str = "0") -> str:
        return "on" if (os.getenv(name, default) == "1") else "off"

    try:
        print(f"[TNT][START] mode={mode} entrypoint={entrypoint} pid={os.getpid()}")
        ttl = (os.getenv("TNT_DECISION_LOCK_TTL_SEC", "900") or "900").strip()
        print(
            "[TNT][ENV] concierge={concierge} auto_concierge={auto} bot_to_bot={bot_to_bot} decision_lock={dl} decision_lock_ttl={ttl} memory={mem}".format(
                concierge=_env_onoff("TNT_CONCIERGE_ENABLED", "0"),
                auto=_env_onoff("TNT_CONCIERGE_AUTO", "0"),
                bot_to_bot=_env_onoff("TNT_CONCIERGE_ALLOW_BOT_MESSAGES", "0"),
                dl=_env_onoff("TNT_DECISION_LOCK_ENABLED", "1"),
                ttl=ttl,
                mem=_env_onoff("TNT_CONCIERGE_MEMORY", "0"),
            )
        )
    except Exception:
        pass

    # Filesystem preflight.
    try:
        for d in (Path("logs"), Path("config"), Path("cache")):
            _ensure_dir_writable(d)
    except Exception as exc:
        print(f"[FATAL] Filesystem preflight failed: {exc}")
        sys.exit(2)

    token = _resolve_discord_token()
    if not token:
        print("[FATAL] DISCORD_BOT_TOKEN not set (env or .env.local/.env).")
        print("        Set DISCORD_BOT_TOKEN in your shell, or add it to .env.local in the repo root.")
        sys.exit(1)
    try:
        bot.run(token)
    except KeyboardInterrupt:
        print("[STOP] Keyboard interrupt, shutting down bot")
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] Discord bot failed to start: {type(exc).__name__}: {exc}")
        print("        Common causes: invalid token, missing privileged intents in Discord Dev Portal, or gateway connectivity issues.")
        sys.exit(1)


if __name__ == "__main__":
    main()
