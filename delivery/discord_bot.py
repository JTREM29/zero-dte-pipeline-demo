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
import random
import json
import platform
import traceback
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
from delivery.coach_consistency import (
    CoachTokens,
    apply_consistency_guard,
    apply_candidate_truth_guard,
    build_observational_envelope,
    normalize_coach_output,
    tokens_from_tnt_state,
)

from delivery.tnt_final_phrasing import (
    build_candidate_reply,
    build_futures_overview,
    build_greeting,
    build_market_context,
    build_market_best_effort_futures_only,
    build_market_snapshot_missing,
    build_symbol_context,
)

from delivery.candidates_contract import (
    AI_TRADE_CANDIDATES_BOUNDARY_SENTENCE,
    AI_TRADE_CANDIDATES_CANONICAL_DEFINITION,
    AI_TRADE_CANDIDATES_MISINTERPRETATION_CORRECTION_SENTENCE,
    AI_TRADE_CANDIDATES_NO_CANDIDATE_SENTENCE,
)

from delivery.channel_router import decide_route, router_enabled

from services.context.ctx_reader import (
    IntentSpec,
    load_required_ctx,
    redis_client_for_ctx,
    resolve_intent_and_required_keys,
)
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


@dataclass(slots=True)
class _CheapQANewsCacheEntry:
    lines: list[str]
    ts: float


@dataclass(slots=True)
class _CheapQAEarningsCacheEntry:
    line: str
    ts: float


_CHEAP_QA_NEWS_CACHE: dict[str, _CheapQANewsCacheEntry] = {}
_CHEAP_QA_EARNINGS_CACHE: dict[str, _CheapQAEarningsCacheEntry] = {}

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

_CHEAP_QA_NEWS_TTL_SEC = _env_float("CHEAP_QA_NEWS_TTL_SEC", 300.0)
_CHEAP_QA_EARNINGS_TTL_SEC = _env_float("CHEAP_QA_EARNINGS_TTL_SEC", 3600.0)
_CHEAP_QA_CACHE_MAX = _env_int("CHEAP_QA_CACHE_MAX", 128)

_ETF_CONSTITUENTS_TTL_SEC = _env_float("ETF_CONSTITUENTS_TTL_SEC", 21600.0)


def _env_bool(name: str, fallback: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(fallback)
    return raw in {"1", "true", "yes", "on"}


def _cheapqa_enrich_enabled() -> bool:
    # Default ON: user wants premium feel without LLM.
    return _env_bool("CHEAP_QA_ENRICH", True)


def _cheapqa_enrich_news_enabled() -> bool:
    return _env_bool("CHEAP_QA_ENRICH_NEWS", True)


def _cheapqa_enrich_earnings_enabled() -> bool:
    return _env_bool("CHEAP_QA_ENRICH_EARNINGS", True)


def _cheapqa_enrich_futures_enabled() -> bool:
    return _env_bool("CHEAP_QA_ENRICH_FUTURES", True)


def _cheapqa_allow_coach_fallback() -> bool:
    # Default OFF to preserve the historical no-LLM guarantee for CHEAP_QA.
    return _env_bool("CHEAP_QA_ALLOW_COACH_FALLBACK", False)


def _cheapqa_cache_trim(cache: dict[str, object]) -> None:
    try:
        if _CHEAP_QA_CACHE_MAX <= 0 or len(cache) <= _CHEAP_QA_CACHE_MAX:
            return
        # Drop oldest entries by ts if present.
        while len(cache) > _CHEAP_QA_CACHE_MAX:
            oldest_key = None
            oldest_ts = None
            for k, v in list(cache.items()):
                ts = getattr(v, "ts", None)
                if ts is None:
                    continue
                if oldest_ts is None or float(ts) < float(oldest_ts):
                    oldest_ts = float(ts)
                    oldest_key = k
            if oldest_key is None:
                break
            cache.pop(oldest_key, None)
    except Exception:
        return


async def _cheapqa_latest_news_lines(symbol: str) -> list[str]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return []

    entry = _CHEAP_QA_NEWS_CACHE.get(sym)
    if entry is not None and _CHEAP_QA_NEWS_TTL_SEC > 0:
        try:
            if (time_lib.time() - float(entry.ts)) <= float(_CHEAP_QA_NEWS_TTL_SEC):
                return list(entry.lines)
        except Exception:
            pass

    key = _resolve_massive_api_key()
    if not key:
        return []

    base = (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").strip().rstrip("/")
    try:
        from services.news.massive_benzinga_news import fetch_benzinga_news

        since = _now_utc().replace(tzinfo=timezone.utc) - timedelta(hours=24)
        items = await fetch_benzinga_news(
            base_url=base,
            api_key=key,
            tickers=[sym],
            published_since_utc=since,
            limit=2,
            timeout_s=float(_env_int("CHEAP_QA_NEWS_TIMEOUT_S", 8)),
        )
    except Exception:
        items = []

    lines: list[str] = []
    for it in (items or [])[:1]:
        try:
            headline = str(getattr(it, "headline", "") or "").strip()
            url = str(getattr(it, "url", "") or "").strip()
            if headline and url:
                lines.append(f"📰 Headline (24h): {headline} — {url}")
            elif headline:
                lines.append(f"📰 Headline (24h): {headline}")
        except Exception:
            continue

    _CHEAP_QA_NEWS_CACHE[sym] = _CheapQANewsCacheEntry(lines=list(lines), ts=time_lib.time())
    _cheapqa_cache_trim(_CHEAP_QA_NEWS_CACHE)
    return lines


async def _cheapqa_next_earnings_line(symbol: str, *, now_utc: datetime | None = None) -> str | None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    entry = _CHEAP_QA_EARNINGS_CACHE.get(sym)
    if entry is not None and _CHEAP_QA_EARNINGS_TTL_SEC > 0:
        try:
            if (time_lib.time() - float(entry.ts)) <= float(_CHEAP_QA_EARNINGS_TTL_SEC):
                return str(entry.line or "").strip() or None
        except Exception:
            pass

    key = _resolve_earnings_api_key()
    if not key:
        return None

    base = (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").strip().rstrip("/")
    base_now_utc = now_utc
    try:
        if base_now_utc is not None and base_now_utc.tzinfo is None:
            base_now_utc = base_now_utc.replace(tzinfo=timezone.utc)
    except Exception:
        base_now_utc = None
    if base_now_utc is None:
        base_now_utc = _now_utc().replace(tzinfo=timezone.utc)
    now_et = base_now_utc.astimezone(ET)
    try:
        from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings, pick_next_earnings

        recs = await fetch_benzinga_earnings(
            base_url=base,
            api_key=key,
            tickers=[sym],
            start_date=now_et.date(),
            end_date=(now_et.date() + timedelta(days=int(_env_int("CHEAP_QA_EARNINGS_LOOKAHEAD_DAYS", 120)))),
            limit=200,
            timeout_s=float(_env_int("CHEAP_QA_EARNINGS_TIMEOUT_S", 10)),
        )
        nxt = pick_next_earnings(recs, symbol=sym, now_utc=base_now_utc)
    except Exception:
        nxt = None

    line: str | None = None
    if nxt is not None:
        try:
            dt_et = nxt.ts_utc.astimezone(ET)
            when = "BMO" if dt_et.hour < 12 else ("AMC" if dt_et.hour >= 16 else "TAS")
            conf = "confirmed" if bool(getattr(nxt, "confirmed", True)) else "unconfirmed"
            line = f"📅 Next earnings: {dt_et.date().isoformat()} ({when}, ET) — {conf}"
        except Exception:
            line = None

    _CHEAP_QA_EARNINGS_CACHE[sym] = _CheapQAEarningsCacheEntry(line=str(line or "").strip(), ts=time_lib.time())
    _cheapqa_cache_trim(_CHEAP_QA_EARNINGS_CACHE)
    return line


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


def _massive_key_and_base() -> tuple[Optional[str], str, str]:
    """Return (api_key, base_url, provider_label) for Massive.

    NOTE: Massive is used for Benzinga feeds. Do not route Polygon market-data
    endpoints through Massive.
    """

    use_massive = os.getenv("ZERO_DTE_USE_MASSIVE", "0") == "1"

    # Massive key policy (ops): prefer MASSIVE_API_KEY; allow falling back
    # to POLYGON_API_KEY for deployments where Massive uses the same key.
    massive_key = (os.getenv("MASSIVE_API_KEY") or "").strip()
    if not massive_key:
        massive_key = (os.getenv("POLYGON_API_KEY") or "").strip()

    massive_base = (os.getenv("MASSIVE_BASE_URL") or "").strip().rstrip("/")
    if use_massive and not massive_key:
        raise RuntimeError(
            "ZERO_DTE_USE_MASSIVE=1 but MASSIVE_API_KEY/POLYGON_API_KEY is missing; set MASSIVE_API_KEY (or reuse POLYGON_API_KEY) and MASSIVE_BASE_URL=https://api.massive.com"
        )
    return (massive_key or None), (massive_base or "https://api.massive.com"), "massive"


def _resolve_massive_api_key() -> str:
    """Resolve the Massive API key.

    Policy: MASSIVE_API_KEY preferred; fallback to POLYGON_API_KEY allowed.
    Supports simple `${VAR}` / `$VAR` references.
    """

    raw = (os.getenv("MASSIVE_API_KEY") or "").strip()
    if raw:
        expanded, _ = _expand_env_reference(raw)
        if expanded:
            return expanded

    raw = (os.getenv("POLYGON_API_KEY") or "").strip()
    return raw


def _resolve_massive_api_key_source() -> str:
    raw = (os.getenv("MASSIVE_API_KEY") or "").strip()
    if raw:
        expanded, _ = _expand_env_reference(raw)
        if expanded:
            return "MASSIVE_API_KEY"
    if (os.getenv("POLYGON_API_KEY") or "").strip():
        return "POLYGON_API_KEY"
    return "missing"


def _polygon_key_and_base() -> tuple[Optional[str], str, str]:
    """Return (api_key, base_url, provider_label) for Polygon market data."""

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


class QualityGateBlockedError(RuntimeError):
    def __init__(self, *, kind: str, label: str, symbol: str, reason: str):
        self.kind = str(kind or "analysis")
        self.label = str(label or "")
        self.symbol = str(symbol or "")
        self.reason = str(reason or "Quality gate blocked an unvalidated response.")
        super().__init__(f"Gate blocked {self.kind} {self.symbol}: {self.reason}")


async def _fetch_etf_constituents(composite_ticker: str) -> Optional[set[str]]:
    """Fetch ETF constituents tickers for a composite ticker (e.g., SPY)."""

    sym = (composite_ticker or "").strip().upper()
    if not sym:
        return None

    cached = _get_cached_etf_constituents(sym)
    if cached is not None:
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
    state = f"Market session={detail} as of {now_et.strftime('%Y-%m-%d %H:%M')} ET."
    return "\n".join(
        [
            "A) State",
            state,
            "",
            "B) Why",
            "- This is a session/status question (no trade recommendation implied).",
            "",
            "C) Action",
            "- If you need trade guidance: ask a symbol-specific question (or use /trade for the card).",
            "",
            "D) Risk + invalidation",
            "- n/a",
        ]
    )


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
    state = "Watchlist is empty." if not symbols else f"Watchlist: {', '.join(symbols)}."
    return "\n".join(
        [
            "A) State",
            state,
            "",
            "B) Why",
            "- This is a universe/symbols question (no trade recommendation implied).",
            "",
            "C) Action",
            "- Pick a symbol from the watchlist and ask a specific question.",
            "",
            "D) Risk + invalidation",
            "- n/a",
        ]
    )


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
            "A) State",
            f"Regime/Bias per TNT_STATE; key levels snapshot: {answer}",
            "",
            "B) Why",
            "- This answer is level-based and uses only TNT_STATE pivots.",
            "",
            "C) Action",
            f"- If bias is BULLISH: {bull_cond}",
            f"- If bias is BEARISH: {bear_cond}",
            f"- DO NOTHING if: {idle}",
            "",
            "D) Risk + invalidation",
            f"- Invalidation: {invalid}",
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
    # On weekends/holidays, there may be zero contracts for "today"; if the caller
    # didn't specify an expiration, roll forward to the next available expiry.
    now_et = _now_et()
    exp0 = (expiration_ymd or now_et.date().isoformat())[:10]
    exp = exp0

    # Get an approximate underlying price to bound strikes.
    # Prefer last-trade snapshot, but fall back to `fetch_live_price()` (aggs/prev) when missing.
    underlying_px = None
    try:
        price_snap = _get_last_price_snapshot(sym)
        if price_snap is not None:
            px_val = getattr(price_snap, "px", None)
            if px_val is None:
                px_val = getattr(price_snap, "price", None)
            if px_val is not None:
                underlying_px = float(px_val)
    except Exception:  # noqa: BLE001
        underlying_px = None

    if not isinstance(underlying_px, (int, float)) or not underlying_px or float(underlying_px) <= 0:
        try:
            px, _ts_iso, _src = await asyncio.to_thread(fetch_live_price, sym)
            if px is not None:
                px_f = float(px)
                if px_f > 0:
                    underlying_px = px_f
        except Exception:
            pass

    strike_min = None
    strike_max = None
    if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
        window_abs = None
        try:
            window_abs = float(os.getenv("TNT_OI_WINDOW_ABS", "10"))
        except Exception:
            window_abs = 10.0
        if not (isinstance(window_abs, (int, float)) and window_abs and window_abs > 0 and window_abs < 1_000_000):
            window_abs = None

        if window_abs is not None:
            strike_min = float(underlying_px) - float(window_abs)
            strike_max = float(underlying_px) + float(window_abs)
        else:
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
        exp_candidates: list[str] = [exp]
        if expiration_ymd is None:
            try:
                base = now_et.date()
                exp_candidates = [(base + timedelta(days=i)).isoformat()[:10] for i in range(0, 15)]
            except Exception:
                exp_candidates = [exp]

        results: list[Any] = []
        contracts: dict[str, Any] | None = None

        for exp_try in exp_candidates:
            params: dict[str, Any] = {
                "underlying_ticker": underlying,
                "expiration_date": exp_try,
                "limit": int(max_contracts),
            }
            if strike_min is not None and strike_max is not None:
                # Polygon expects strike_price.gte / strike_price.lte
                params["strike_price.gte"] = f"{strike_min:.6f}"
                params["strike_price.lte"] = f"{strike_max:.6f}"

            contracts = await _get_json(session, "/v3/reference/options/contracts", params=params)
            if not contracts or contracts.get("status") != "OK":
                # If the caller explicitly requested an expiry, don't try to be clever.
                if expiration_ymd is not None:
                    return None
                continue

            results = contracts.get("results") or []
            if isinstance(results, list) and results:
                exp = exp_try
                break
            if expiration_ymd is not None:
                return None

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


async def _fetch_polygon_atm_straddle_df(
    symbol: str,
    *,
    expiration_ymd: Optional[str] = None,
    strike_window_pct: float = 0.06,
    max_contracts: int = 120,
) -> Optional[pd.DataFrame]:
    """Fetch a *tiny* chain snapshot containing the ATM call + put.

    This is intentionally optimized for fast expected-move estimation (ATM straddle)
    and avoids N-per-contract snapshot calls.

    Returns a DataFrame compatible with `compute_expected_move_from_chain_df`.
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    api_key, base_url, _provider = _polygon_key_and_base()
    if not api_key:
        return None

    underlying = _map_underlying_for_options(sym)

    # Determine expiration date candidates (default: today ET, roll forward).
    now_et = _now_et()
    exp0 = (expiration_ymd or now_et.date().isoformat())[:10]
    exp_candidates: list[str] = [exp0]
    if expiration_ymd is None:
        try:
            base = now_et.date()
            exp_candidates = [(base + timedelta(days=i)).isoformat()[:10] for i in range(0, 15)]
        except Exception:
            exp_candidates = [exp0]

    # Underlying price for strike window.
    try:
        price_snap = _get_last_price_snapshot(sym)
        underlying_px = float(price_snap.px) if price_snap and price_snap.px is not None else None
    except Exception:  # noqa: BLE001
        underlying_px = None
    if not isinstance(underlying_px, (int, float)) or not underlying_px or underlying_px <= 0:
        return None

    window = max(float(strike_window_pct), 0.0)
    strike_min = float(underlying_px) * (1.0 - window)
    strike_max = float(underlying_px) * (1.0 + window)

    # Process-wide guard for Polygon options HTTP calls.
    global _POLYGON_OPTIONS_HTTP_SEM
    try:
        _POLYGON_OPTIONS_HTTP_SEM  # type: ignore[name-defined]
    except Exception:
        _POLYGON_OPTIONS_HTTP_SEM = asyncio.Semaphore(max(int(os.getenv("TNT_MAX_POLYGON_OPTIONS_HTTP", "12")), 1))

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

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        best: dict[str, dict[str, Any]] | None = None
        best_strike: float | None = None
        best_exp: str | None = None

        for exp_try in exp_candidates:
            params: dict[str, Any] = {
                "underlying_ticker": underlying,
                "expiration_date": exp_try,
                "limit": int(max_contracts),
                "strike_price.gte": f"{strike_min:.6f}",
                "strike_price.lte": f"{strike_max:.6f}",
            }
            contracts = await _get_json(session, "/v3/reference/options/contracts", params=params)
            if not contracts or contracts.get("status") != "OK":
                if expiration_ymd is not None:
                    return None
                continue
            results = contracts.get("results") or []
            if not isinstance(results, list) or not results:
                if expiration_ymd is not None:
                    return None
                continue

            # Build strike -> {call, put} and pick closest strike with both legs.
            by_strike: dict[float, dict[str, Any]] = {}
            for it in results:
                if not isinstance(it, dict):
                    continue
                try:
                    strike = float(it.get("strike_price"))
                except Exception:
                    continue
                ctype = str(it.get("contract_type") or "").strip().lower()
                ticker = str(it.get("ticker") or "").strip()
                if ctype not in {"call", "put"} or not ticker:
                    continue
                bucket = by_strike.setdefault(strike, {})
                # Prefer first seen; list is already bounded.
                if ctype not in bucket:
                    bucket[ctype] = {"ticker": ticker, "strike": strike}

            candidates = [(abs(s - float(underlying_px)), s) for s, legs in by_strike.items() if "call" in legs and "put" in legs]
            if not candidates:
                if expiration_ymd is not None:
                    return None
                continue
            candidates.sort(key=lambda x: x[0])
            best_strike = float(candidates[0][1])
            best = by_strike.get(best_strike)
            best_exp = exp_try
            break

        if not best or best_strike is None or not best_exp:
            return None

        async def _snap(ticker: str) -> Optional[dict[str, Any]]:
            snap = await _get_json(session, f"/v3/snapshot/options/{underlying}/{ticker}")
            if not snap or snap.get("status") != "OK":
                return None
            res = snap.get("results") if isinstance(snap.get("results"), dict) else None
            return res if isinstance(res, dict) else None

        call_ticker = str(best.get("call", {}).get("ticker") or "").strip()
        put_ticker = str(best.get("put", {}).get("ticker") or "").strip()
        if not call_ticker or not put_ticker:
            return None

        call_res, put_res = await asyncio.gather(_snap(call_ticker), _snap(put_ticker))
        if not call_res or not put_res:
            return None

        def _row(res: dict[str, Any], *, ctype: str) -> dict[str, Any]:
            greeks = res.get("greeks") if isinstance(res.get("greeks"), dict) else {}
            under = res.get("underlying_asset") if isinstance(res.get("underlying_asset"), dict) else {}
            day = res.get("day") if isinstance(res.get("day"), dict) else {}
            prev_day = res.get("prev_day")
            if not isinstance(prev_day, dict):
                prev_day = res.get("prevDay")
            prev_day = prev_day if isinstance(prev_day, dict) else {}
            return {
                "symbol": str(res.get("ticker") or ""),
                "strike": best_strike,
                "type": ctype,
                "expiration": best_exp,
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
                "underlying_price": under.get("price") if under.get("price") is not None else underlying_px,
            }

        rows = [_row(call_res, ctype="call"), _row(put_res, ctype="put")]

    return pd.DataFrame(rows) if rows else None


async def _fetch_polygon_straddle_with_wings(
    symbol: str,
    *,
    expiration_ymd: str,
    underlying: float,
    wing_dollars: int = 5,
    timeout_s: float = 3.0,
) -> Optional[dict[str, Any]]:
    """Fetch an earnings-time expected-move snapshot with ±$wings.

    Deterministic and bounded:
    - 1 contracts list call
    - 6 option snapshot calls (call+put at 3 strikes)

    Returns a dict shaped like `expected_move` (new schema) or None.
    """

    sym = (symbol or "").strip().upper()
    exp = (expiration_ymd or "").strip()[:10]
    try:
        under_px = float(underlying)
    except Exception:
        under_px = 0.0

    if not sym or not exp or not (under_px > 0):
        return None

    try:
        wing = int(wing_dollars)
    except Exception:
        wing = 5
    wing = max(1, min(25, int(wing)))

    api_key, base_url, _provider = _polygon_key_and_base()
    if not api_key:
        return None

    underlying_ticker = _map_underlying_for_options(sym)

    # Strike bounds: just wide enough to reliably include atm±wing.
    strike_min = max(0.01, float(under_px) - float(max(wing * 3, 10)))
    strike_max = float(under_px) + float(max(wing * 3, 10))

    # Process-wide guard for Polygon options HTTP calls.
    global _POLYGON_OPTIONS_HTTP_SEM
    try:
        _POLYGON_OPTIONS_HTTP_SEM  # type: ignore[name-defined]
    except Exception:
        _POLYGON_OPTIONS_HTTP_SEM = asyncio.Semaphore(max(int(os.getenv("TNT_MAX_POLYGON_OPTIONS_HTTP", "12")), 1))

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

    def _mid_from_snapshot(res: dict[str, Any]) -> float | None:
        try:
            day = res.get("day") if isinstance(res.get("day"), dict) else {}
            b = day.get("bid")
            a = day.get("ask")
            last = day.get("close")
            try:
                b = float(b) if b is not None else None
            except Exception:
                b = None
            try:
                a = float(a) if a is not None else None
            except Exception:
                a = None
            if b is not None and a is not None and a >= b and (a + b) > 0:
                return 0.5 * (float(a) + float(b))
            try:
                return float(last) if last is not None else None
            except Exception:
                return None
        except Exception:
            return None

    def _nearest_strike(available: list[float], target: float) -> float | None:
        if not available:
            return None
        try:
            return float(min(available, key=lambda s: abs(float(s) - float(target))))
        except Exception:
            return None

    timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_s)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        params: dict[str, Any] = {
            "underlying_ticker": underlying_ticker,
            "expiration_date": exp,
            "limit": 600,
            "strike_price.gte": f"{strike_min:.6f}",
            "strike_price.lte": f"{strike_max:.6f}",
        }

        contracts = await _get_json(session, "/v3/reference/options/contracts", params=params)
        if not contracts or contracts.get("status") != "OK":
            return None
        results = contracts.get("results") or []
        if not isinstance(results, list) or not results:
            return None

        by_strike: dict[float, dict[str, str]] = {}
        for it in results:
            if not isinstance(it, dict):
                continue
            try:
                strike = float(it.get("strike_price"))
            except Exception:
                continue
            ctype = str(it.get("contract_type") or "").strip().lower()
            ticker = str(it.get("ticker") or "").strip()
            if ctype not in {"call", "put"} or not ticker:
                continue
            bucket = by_strike.setdefault(strike, {})
            if ctype not in bucket:
                bucket[ctype] = ticker

        strikes = sorted([s for s, legs in by_strike.items() if "call" in legs and "put" in legs])
        if not strikes:
            return None

        atm_strike = _nearest_strike(strikes, float(under_px))
        if atm_strike is None:
            return None

        down_strike = _nearest_strike(strikes, float(atm_strike) - float(wing))
        up_strike = _nearest_strike(strikes, float(atm_strike) + float(wing))

        def _tickers_for_strike(s: float) -> tuple[str, str] | None:
            legs = by_strike.get(float(s)) or {}
            c = str(legs.get("call") or "").strip()
            p = str(legs.get("put") or "").strip()
            if not c or not p:
                return None
            return c, p

        atm = _tickers_for_strike(float(atm_strike))
        if atm is None:
            return None

        # Required legs: ATM call+put. Wings are best-effort.
        legs: list[tuple[str, str, float]] = [(atm[0], "call", float(atm_strike)), (atm[1], "put", float(atm_strike))]

        if down_strike is not None:
            t = _tickers_for_strike(float(down_strike))
            if t is not None:
                legs.extend([(t[0], "call", float(down_strike)), (t[1], "put", float(down_strike))])
        if up_strike is not None:
            t = _tickers_for_strike(float(up_strike))
            if t is not None:
                legs.extend([(t[0], "call", float(up_strike)), (t[1], "put", float(up_strike))])

        async def _snap(ticker: str) -> Optional[dict[str, Any]]:
            snap = await _get_json(session, f"/v3/snapshot/options/{underlying_ticker}/{ticker}")
            if not snap or snap.get("status") != "OK":
                return None
            res = snap.get("results") if isinstance(snap.get("results"), dict) else None
            return res if isinstance(res, dict) else None

        snaps = await asyncio.gather(*[_snap(t[0]) for t in legs])

        # Build a mini table: strike -> {call_mid, put_mid}
        table: dict[float, dict[str, float]] = {}
        for (ticker, ctype, strike), res in zip(legs, snaps):
            if not res:
                continue
            mid = _mid_from_snapshot(res)
            if mid is None or not (mid >= 0):
                continue
            row = table.setdefault(float(strike), {})
            row[f"{ctype}_mid"] = float(mid)

        atm_row = table.get(float(atm_strike)) or {}
        call_mid = atm_row.get("call_mid")
        put_mid = atm_row.get("put_mid")
        if call_mid is None or put_mid is None:
            return None

        atm_straddle = float(call_mid) + float(put_mid)
        if not (atm_straddle > 0):
            return None

        expected_move_pct = (atm_straddle / float(under_px)) * 100.0 if under_px > 0 else None

        out: dict[str, Any] = {
            "asof_ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "expiry": exp,
            "underlying": float(under_px),
            "atm": {
                "strike": float(atm_strike),
                "call_mid": float(call_mid),
                "put_mid": float(put_mid),
                "straddle": float(atm_straddle),
                "pct": float(expected_move_pct) if expected_move_pct is not None else None,
                "lower": float(max(0.0, float(under_px) - float(atm_straddle))),
                "upper": float(float(under_px) + float(atm_straddle)),
            },
            "wings": {},
        }

        wings: dict[str, Any] = {}
        if down_strike is not None:
            drow = table.get(float(down_strike)) or {}
            if drow.get("call_mid") is not None or drow.get("put_mid") is not None:
                wings["down_5"] = {
                    "strike": float(down_strike),
                    "call_mid": (float(drow["call_mid"]) if drow.get("call_mid") is not None else None),
                    "put_mid": (float(drow["put_mid"]) if drow.get("put_mid") is not None else None),
                }
        if up_strike is not None:
            urow = table.get(float(up_strike)) or {}
            if urow.get("call_mid") is not None or urow.get("put_mid") is not None:
                wings["up_5"] = {
                    "strike": float(up_strike),
                    "call_mid": (float(urow["call_mid"]) if urow.get("call_mid") is not None else None),
                    "put_mid": (float(urow["put_mid"]) if urow.get("put_mid") is not None else None),
                }

        out["wings"] = wings

        # Risk shape: best-effort only when both wing costs are present.
        try:
            from services.calendar.earnings_options import classify_wing_risk_shape, compute_wing_skew_score

            if "up_5" in wings and "down_5" in wings:
                up_cost = (float(wings["up_5"].get("call_mid") or 0.0) + float(wings["up_5"].get("put_mid") or 0.0))
                down_cost = (float(wings["down_5"].get("call_mid") or 0.0) + float(wings["down_5"].get("put_mid") or 0.0))
                score = compute_wing_skew_score(atm_straddle=atm_straddle, up_cost=up_cost, down_cost=down_cost)
                label = classify_wing_risk_shape(score=score) if score is not None else None
                if label and score is not None:
                    out["risk_shape"] = {"label": str(label), "score": float(score)}
        except Exception:
            pass

        return out


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

    def _compute_smart_market_iq_from_etf_proxies() -> Dict[str, Any]:
        """Best-effort regime snapshot derived from ETF proxy ratios.

        This is intentionally lightweight and contract-safe:
        - Computes only regime + a small meta pack.
        - Does not require any internal Redis context.
        - Returns MISSING on any failure.
        """
        try:
            from delivery.on_demand_data import polygon_aggs
        except Exception:  # noqa: BLE001
            print("[SMIQ][WARN] compute unavailable: delivery.on_demand_data import failed")
            return {"data_quality": "MISSING", "regime": "UNKNOWN", "reason": "import_failed"}

        polygon_key_present = bool((os.getenv("POLYGON_API_KEY", "") or "").strip())
        if not polygon_key_present:
            print("[SMIQ][WARN] compute blocked: POLYGON_API_KEY missing in process env")
            return {"data_quality": "MISSING", "regime": "UNKNOWN", "reason": "no_key"}

        tz = ET_TZ or timezone.utc
        now_et = datetime.now(tz)

        # Proxy set (kept consistent with /risk_on_off):
        # ES proxy: SPY, NQ proxy: QQQ, RTY proxy: IWM, rates proxy: TLT/SHY, crude proxy: USO
        sym_spy = "SPY"
        sym_qqq = "QQQ"
        sym_iwm = "IWM"
        sym_tlt = "TLT"
        sym_shy = "SHY"
        sym_uso = "USO"

        try:
            window_days = int(os.getenv("SMART_MARKET_IQ_PROXY_WINDOW_DAYS", "80"))
        except Exception:
            window_days = 80
        window_days = min(max(window_days, 45), 260)

        try:
            lookback = int(os.getenv("SMART_MARKET_IQ_PROXY_LOOKBACK_DAYS", "20"))
        except Exception:
            lookback = 20
        lookback = min(max(lookback, 5), 60)

        try:
            payloads = {
                sym_spy: polygon_aggs(sym_spy, multiplier=1, timespan="day", days=window_days),
                sym_qqq: polygon_aggs(sym_qqq, multiplier=1, timespan="day", days=window_days),
                sym_iwm: polygon_aggs(sym_iwm, multiplier=1, timespan="day", days=window_days),
                sym_tlt: polygon_aggs(sym_tlt, multiplier=1, timespan="day", days=window_days),
                sym_shy: polygon_aggs(sym_shy, multiplier=1, timespan="day", days=window_days),
                sym_uso: polygon_aggs(sym_uso, multiplier=1, timespan="day", days=window_days),
            }
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "POLYGON_API_KEY" in msg:
                print("[SMIQ][WARN] compute blocked: POLYGON_API_KEY missing (runtime)")
            else:
                print(f"[SMIQ][WARN] compute failed: {type(exc).__name__}: {exc}")
            reason = "no_key" if "POLYGON_API_KEY" in msg else "fetch_failed"
            return {"data_quality": "MISSING", "regime": "UNKNOWN", "reason": reason}

        def _extract_close_map(payload: dict[str, Any]) -> dict[str, float]:
            out: dict[str, float] = {}
            for row in (payload.get("results") or []):
                try:
                    t_ms = float(row.get("t"))
                    c = float(row.get("c"))
                except Exception:
                    continue
                if c <= 0:
                    continue
                dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(tz)
                out[dt.date().isoformat()] = float(c)
            return out

        maps = {k: _extract_close_map(v) for k, v in payloads.items()}
        counts = {k: len(v) for k, v in maps.items()}
        if any(int(n) < (lookback + 6) for n in counts.values()):
            print(f"[SMIQ][WARN] compute insufficient history: lookback={lookback} window_days={window_days} counts={counts}")
            return {"data_quality": "MISSING", "regime": "UNKNOWN", "reason": "insufficient_history", "meta": {"counts": counts}}

        def _ratio_series(num: str, den: str) -> list[float]:
            a = maps.get(num) or {}
            b = maps.get(den) or {}
            common = sorted(set(a.keys()) & set(b.keys()))
            ys: list[float] = []
            for k in common:
                c_a = a.get(k)
                c_b = b.get(k)
                if c_a is None or c_b is None or c_b <= 0:
                    continue
                ys.append(float(c_a) / float(c_b))
            return ys

        r_growth = _ratio_series(sym_qqq, sym_spy)  # growth vs broad
        r_breadth = _ratio_series(sym_iwm, sym_spy)  # small caps vs broad
        r_duration = _ratio_series(sym_tlt, sym_shy)  # duration vs cash
        r_inflation = _ratio_series(sym_uso, sym_spy)  # crude proxy vs broad

        lens = {
            "QQQ/SPY": len(r_growth),
            "IWM/SPY": len(r_breadth),
            "TLT/SHY": len(r_duration),
            "USO/SPY": len(r_inflation),
        }
        if min(lens.values()) < (lookback + 3):
            print(f"[SMIQ][WARN] compute insufficient overlap: lookback={lookback} lens={lens} counts={counts}")
            return {
                "data_quality": "MISSING",
                "regime": "UNKNOWN",
                "reason": "insufficient_overlap",
                "meta": {"counts": counts, "lens": lens},
            }

        def _pct_change(values: list[float], lb: int) -> float:
            if len(values) < 2:
                return 0.0
            idx = max(0, len(values) - 1 - lb)
            base = float(values[idx])
            last = float(values[-1])
            if base == 0:
                return 0.0
            return (last / base - 1.0) * 100.0

        def _sgn(x: float) -> int:
            if x > 0:
                return 1
            if x < 0:
                return -1
            return 0

        chg_growth = _pct_change(r_growth, lookback)
        chg_breadth = _pct_change(r_breadth, lookback)
        chg_duration = _pct_change(r_duration, lookback)
        chg_inflation = _pct_change(r_inflation, lookback)

        dir_growth = _sgn(chg_growth)
        dir_breadth = _sgn(chg_breadth)
        dir_duration = _sgn(chg_duration)
        dir_inflation = _sgn(chg_inflation)

        # Composite score (+ risk-on, - defensive). Duration rising is defensive, so subtract.
        score = int(dir_growth + dir_breadth - dir_duration + dir_inflation)
        if score > 0:
            regime_sign = 1
        elif score < 0:
            regime_sign = -1
        else:
            regime_sign = 0

        aligned = 0
        if regime_sign != 0:
            for v in (dir_growth, dir_breadth, -dir_duration, dir_inflation):
                if int(v) == int(regime_sign):
                    aligned += 1
        conflict = bool(regime_sign == 0 or aligned <= 2)

        if conflict:
            regime = "MIXED"
        elif regime_sign > 0:
            regime = "RISK_ON"
        else:
            regime = "DEFENSIVE"

        print(
            "[SMIQ][OK] computed "
            f"regime={regime} score={score} aligned={aligned}/4 conflict={int(conflict)} "
            f"lookback={lookback} window_days={window_days} counts={counts}"
        )

        return {
            "timestamp_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
            "data_quality": "OK",
            "regime": regime,
            "top_gainers": [],
            "top_losers": [],
            "meta": {
                "source": "polygon_etf_proxy_ratios",
                "lookback_days": int(lookback),
                "window_days": int(window_days),
                "score": int(score),
                "aligned": int(aligned),
                "conflict": bool(conflict),
                "chg_qqq_spy_20d": float(chg_growth),
                "chg_iwm_spy_20d": float(chg_breadth),
                "chg_tlt_shy_20d": float(chg_duration),
                "chg_uso_spy_20d": float(chg_inflation),
                "counts": counts,
            },
        }

    def _maybe_refresh_smart_market_iq() -> None:
        # Never do network I/O under pytest; tests should provide fixtures/mocks.
        if "pytest" in sys.modules:
            return

        # Optional kill switch.
        if os.getenv("SMART_MARKET_IQ_REFRESH_ON_READ", "1") != "1":
            print("[SMIQ] refresh skipped: SMART_MARKET_IQ_REFRESH_ON_READ!=1")
            return

        try:
            max_age_sec = int(os.getenv("SMART_MARKET_IQ_MAX_AGE_SEC", "3600"))
        except Exception:
            max_age_sec = 3600
        max_age_sec = max(0, max_age_sec)

        try:
            if SMART_MARKET_IQ_PATH.exists() and max_age_sec > 0:
                mtime = SMART_MARKET_IQ_PATH.stat().st_mtime
                age = (datetime.now(timezone.utc) - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds()
                if age <= float(max_age_sec):
                    print(f"[SMIQ] refresh skipped: fresh age_sec={age:.0f} max_age_sec={max_age_sec} path={SMART_MARKET_IQ_PATH}")
                    return
                print(f"[SMIQ] refresh needed: stale age_sec={age:.0f} max_age_sec={max_age_sec} path={SMART_MARKET_IQ_PATH}")
            else:
                if not SMART_MARKET_IQ_PATH.exists():
                    print(f"[SMIQ] refresh needed: file missing path={SMART_MARKET_IQ_PATH}")
                else:
                    print(f"[SMIQ] refresh needed: max_age_sec={max_age_sec} path={SMART_MARKET_IQ_PATH}")
        except Exception as exc:
            print(f"[SMIQ][WARN] refresh stat failed: {type(exc).__name__}: {exc}")

        print(f"[SMIQ] refresh start polygon_key_present={int(bool((os.getenv('POLYGON_API_KEY','') or '').strip()))}")
        data = _compute_smart_market_iq_from_etf_proxies()
        if not isinstance(data, dict):
            print("[SMIQ][WARN] refresh aborted: compute returned non-dict")
            return
        if str(data.get("data_quality") or "").strip().upper() != "OK":
            print(f"[SMIQ][WARN] refresh aborted: compute quality={(data.get('data_quality') or 'UNKNOWN')}")
            return

        try:
            SMART_MARKET_IQ_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SMART_MARKET_IQ_PATH.with_suffix(SMART_MARKET_IQ_PATH.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, SMART_MARKET_IQ_PATH)
            print(
                "[SMIQ][OK] refresh wrote "
                f"path={SMART_MARKET_IQ_PATH} regime={str(data.get('regime') or 'UNKNOWN').upper()} "
                f"ts={data.get('timestamp_et') or 'n/a'}"
            )
        except Exception:
            print("[SMIQ][WARN] refresh write failed")
            return

    try:
        _maybe_refresh_smart_market_iq()
        if not SMART_MARKET_IQ_PATH.exists():
            polygon_key_present = bool((os.getenv("POLYGON_API_KEY", "") or "").strip())
            return {
                "data_quality": "MISSING",
                "regime": "UNKNOWN",
                "reason": "no_key" if not polygon_key_present else "file_missing",
            }
        raw = SMART_MARKET_IQ_PATH.read_text(encoding="utf-8")
        if not raw.strip():
            return {"data_quality": "MISSING", "regime": "UNKNOWN", "reason": "empty_file"}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"data_quality": "MISSING", "regime": "UNKNOWN", "reason": "bad_payload"}
        return data
    except Exception:  # noqa: BLE001 - downstream logic handles missing data
        return {"data_quality": "MISSING", "regime": "UNKNOWN", "reason": "read_failed"}


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
    reason = (smiq.get("reason") if isinstance(smiq, dict) else None) or None
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

    regime_label = f"**{regime}**"
    if str(regime).strip().upper() == "UNKNOWN" and reason:
        # Self-debugging, low-noise reason code.
        regime_label = f"**{regime}** ({str(reason).strip().lower()})"

    lines = [
        "📊 Market Participation (TNT)",
        f"• Regime: {regime_label} | Quality: **{quality}** | Gate: **{gate_label}**",
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

    macro_regime = payload.get("macro_regime") if isinstance(payload.get("macro_regime"), Mapping) else None
    macro_regime_risk = ""
    macro_regime_posture = ""
    if isinstance(macro_regime, Mapping):
        try:
            macro_regime_risk = str(macro_regime.get("macro_risk") or "").upper().strip()
            macro_regime_posture = str(macro_regime.get("posture") or "").upper().replace("_", " ").strip()
        except Exception:  # noqa: BLE001
            macro_regime_risk = ""
            macro_regime_posture = ""

    macro_regime_flag = (macro_regime_risk == "HIGH") or (macro_regime_posture == "STAND DOWN")
    macro_flag = bool(macro_risk) or macro_regime_flag

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
    if macro_regime_flag:
        reasons.append(f"macro regime={macro_regime_posture or macro_regime_risk or 'risk'}")
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


def freshness_badge(price_ts: Optional[object], session: str = "unknown") -> str:
    """Human-friendly freshness indicator for price timestamps.

    Returns a short badge string; must never raise.
    """

    session_norm = str(session or "unknown").upper().strip()
    ts_raw = str(price_ts or "").strip()
    if not ts_raw:
        return "⚪ price_ts n/a"

    try:
        ts_dt = parse_iso(ts_raw)
        now_utc = _now_utc()
        ts_utc = ts_dt.astimezone(timezone.utc) if ts_dt.tzinfo else ts_dt.replace(tzinfo=timezone.utc)
        age_min = max(0.0, (now_utc - ts_utc).total_seconds() / 60.0)
    except Exception:  # noqa: BLE001
        return "⚪ price_ts unreadable"

    # When markets are closed, freshness isn't actionable; avoid alarming badges.
    if session_norm in {"CLOSED", "WEEKEND"}:
        return f"⚪ age {age_min:.0f}m (market closed)"

    try:
        limit_min = float(PRICE_STALE_LIMIT_RTH) if session_norm == "RTH" else float(PRICE_STALE_LIMIT_AH)
    except Exception:  # noqa: BLE001
        limit_min = 5.0

    if age_min <= limit_min:
        return f"🟢 fresh ({age_min:.1f}m)"
    if age_min <= limit_min * 2:
        return f"🟡 stale ({age_min:.1f}m)"
    return f"🔴 stale ({age_min:.0f}m)"


def _coerce_ai_string(value: Optional[object], *, max_len: int = 240) -> str:
    if value is None:
        return ""

    try:
        if isinstance(value, str):
            text = value
        elif isinstance(value, (list, tuple)):
            parts: list[str] = []
            for item in value:
                if item is None:
                    continue
                s = str(item).strip()
                if s:
                    parts.append(s)
            text = "; ".join(parts)
        elif isinstance(value, dict):
            # Compact & stable: good for embeds/logs.
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(value)
    except Exception:  # noqa: BLE001 - coercion must never break rendering
        return ""

    text = (text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join([chunk for chunk in text.split("\n") if chunk]).strip()

    if max_len and len(text) > max_len:
        text = text[: max(0, max_len - 1)].rstrip() + "…"

    return text


def _coerce_ai_lines(value: Optional[object], *, max_lines: int = 6, max_len: int = 140) -> list[str]:
    """Convert LLM-ish values (list/str/etc) into clean bullet lines."""

    if value is None:
        return []

    raw_lines: list[str]
    if isinstance(value, str):
        raw_lines = [ln.strip() for ln in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        raw_lines = [str(item).strip() for item in value if item is not None]
    else:
        raw_lines = [_coerce_ai_string(value, max_len=max_len)]

    cleaned: list[str] = []
    for ln in raw_lines:
        if not ln:
            continue
        # Normalize bullets if the model included them.
        for prefix in ("- ", "• ", "* "):
            if ln.startswith(prefix):
                ln = ln[len(prefix) :].strip()
                break
        if not ln:
            continue
        if max_len and len(ln) > max_len:
            ln = ln[: max(0, max_len - 1)].rstrip() + "…"
        cleaned.append(ln)
        if max_lines and len(cleaned) >= max_lines:
            break

    return cleaned


def _parse_time_et(value: str, default: time) -> time:
    """Parse HH:MM into a datetime.time (ET clock time)."""

    raw = (value or "").strip()
    if not raw:
        return default

    try:
        hh_s, mm_s = raw.split(":", 1)
        hh = int(hh_s)
        mm = int(mm_s)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return default
        return time(hh, mm)
    except Exception:  # noqa: BLE001
        return default

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
    # Precedence (local runtime): .env < .env.local
    # .env.local is treated as the authoritative local runtime config and should
    # override any stale inherited env vars from the parent shell.
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
        super().__init__(f"Queued by cadence limiter in ~{wait_seconds:.1f}s")
        self.wait_seconds = float(wait_seconds)
        self.due_ts = float(due_ts)
        self.key = str(key)


class PublishThrottledError(RuntimeError):
    """Raised when an interactive send would be rate-limited but must never queue."""

    def __init__(self, *, wait_seconds: float, key: str):
        super().__init__(f"Throttled by cadence limiter (~{wait_seconds:.1f}s); not queued")
        self.wait_seconds = float(wait_seconds)
        self.key = str(key)


# --- Go-live cadence / rate limits (v1 defaults) ---
_RATE_LABEL_MIN_SEC = float(os.getenv("TNT_RATE_LABEL_MIN_SEC", "300") or "300")
_RATE_POSTURE_SYMBOL_MIN_SEC = float(os.getenv("TNT_RATE_POSTURE_SYMBOL_MIN_SEC", "1800") or "1800")
_RATE_CHARTS_MAX_PER_HOUR = int(os.getenv("TNT_RATE_CHARTS_MAX_PER_HOUR", "12") or "12")
_RATE_USER_CHART_MIN_SEC = float(os.getenv("TNT_RATE_USER_CHART_MIN_SEC", "600") or "600")
_RATE_USER_TEXT_MIN_SEC = float(os.getenv("TNT_RATE_USER_TEXT_MIN_SEC", "20") or "20")
_RATE_QUEUE_ENABLED = (os.getenv("TNT_RATE_QUEUE_ENABLED", "1") == "1")

# Interactive policy:
# - Humans (slash commands / ask/coach/context and on-demand charts) should never be time-window queued.
# - Keep time-window queueing for autopost/batch.
_TNT_DISABLE_COOLDOWNS = (os.getenv("TNT_DISABLE_COOLDOWNS", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
# Default OFF: interactive requests should never be time-window queued.
_TNT_INTERACTIVE_QUEUE_ENABLED = (os.getenv("TNT_INTERACTIVE_QUEUE_ENABLED", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}

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

    if _TNT_DISABLE_COOLDOWNS:
        return 0.0, ["disable_cooldowns"]

    # Interactive bypass: never apply time-window throttles to human-triggered requests.
    # (Inflight render control is handled elsewhere; this is only send/publish cadence.)
    if kind_norm in {"on_demand_text", "on_demand_chart"} and not _TNT_INTERACTIVE_QUEUE_ENABLED:
        return 0.0, ["interactive_bypass"]

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
        "polygon",
        "massive",
        "databento",
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
    publish_id: Optional[str] = None,
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
        "publish_id": publish_id,
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

    # Candidate truth projection (canonical ctx snapshot embed).
    # Allows auditing: "recommended X (or NONE) at that time — and why".
    try:
        canon = agent_payload.get("canonical_ctx") if isinstance(agent_payload, dict) else None
        sym_ctx = canon.get("symbol") if isinstance(canon, dict) else None
        cand = sym_ctx.get("candidates") if isinstance(sym_ctx, dict) else None
        if isinstance(cand, dict):
            payload["ctx_sym_candidates_status"] = cand.get("status")
            payload["ctx_sym_candidates_as_of_et"] = cand.get("as_of_et")
            payload["ctx_sym_candidates_top"] = cand.get("top") if isinstance(cand.get("top"), dict) else None
        else:
            payload["ctx_sym_candidates_status"] = None
            payload["ctx_sym_candidates_as_of_et"] = None
            payload["ctx_sym_candidates_top"] = None
    except Exception:
        payload["ctx_sym_candidates_status"] = None
        payload["ctx_sym_candidates_as_of_et"] = None
        payload["ctx_sym_candidates_top"] = None
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
    publish_id: Optional[str] = None,
    kind: str = "analysis",
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
    if publish_id is None:
        try:
            import uuid

            publish_id = uuid.uuid4().hex[:10]
        except Exception:
            publish_id = None
    if strict_contracts is None:
        strict_contracts = STRICT_CONTRACTS

    # Harden: operational autoposts should never be treated as analysis.
    # This prevents quality-gate fallbacks like "Missing section: Last Price"
    # from blocking non-analysis status posts.
    kind_norm = (kind or "analysis").strip().lower()
    label_norm = (label or "").strip().lower()
    builder_norm = (builder or "").strip().lower()
    if (
        label_norm.startswith("daily_outlook")
        or label_norm.startswith("weekly_outlook")
        or label_norm.startswith("earnings_daily")
        or label_norm.startswith("earnings_results")
        or builder_norm
        in {
            "build_daily_market_outlook_render",
            "build_weekly_market_outlook_render",
            "build_tomorrow_earnings_macro_render",
            "build_earnings_results_today_render",
        }
    ):
        kind_norm = "status"
    if kind_norm not in {"analysis", "status", "text"}:
        kind_norm = "analysis"
    kind = kind_norm

    # Production-safe defaults: only allow relaxing contracts in explicit dev runs.
    if strict_contracts is False and not DEV_ALLOW_VIOLATIONS:
        strict_contracts = True
    if allow_contract_violations and not DEV_ALLOW_VIOLATIONS:
        allow_contract_violations = False

    # Stop-the-bleeding guard: if a scheduled render collapses to a stand-down,
    # suppress repeated posts for the same label within a cooldown window.
    try:
        if channel is not None and _is_stand_down_render(render):
            dedupe_sec = _env_int("TNT_AUTOMATION_STANDDOWN_DEDUPE_SEC", "3600")
            if dedupe_sec > 0:
                global _last_automation_standdown_by_label
                last_map = globals().get("_last_automation_standdown_by_label")
                if not isinstance(last_map, dict):
                    _last_automation_standdown_by_label = {}
                    last_map = _last_automation_standdown_by_label

                now_s = int(_now_utc().timestamp())
                last_s = int(last_map.get(label, 0) or 0)
                if now_s - last_s < dedupe_sec:
                    try:
                        elapsed_ms = (time_lib.perf_counter() - start_ts) * 1000.0
                        _write_autopost_audit(
                            render=render,
                            builder=builder,
                            label=label,
                            channel=channel,
                            asof_et=asof_et,
                            status="stand_down_deduped",
                            symbol=symbol,
                            context_mode=context_mode,
                            context_output_mode=context_output_mode,
                            violations=[f"STANDDOWN_DEDUPE:{dedupe_sec}s"],
                            publish_id=publish_id,
                            message_id=None,
                            latency_ms=elapsed_ms,
                            fallback_text=None,
                        )
                    except Exception:
                        pass
                    return
                last_map[label] = now_s
    except Exception:
        pass

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
                            publish_id=publish_id,
                            kind=kind,
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
                            publish_id=publish_id,
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
                raise_on_block=True,
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
                publish_id=publish_id,
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
    # Outlook is operational; if the agent mentions EMA without EMA data, scrub EMA
    # tokens before strict contract validation.
    try:
        builder_norm = (builder or "").strip().lower()
    except Exception:
        builder_norm = ""
    if builder_norm in {"build_daily_market_outlook_render", "build_weekly_market_outlook_render"}:
        try:
            payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
            tech = payload.get("technical_state")
            has_ema_data = False
            if isinstance(tech, dict):
                for sym_data in tech.values():
                    if not isinstance(sym_data, dict):
                        continue
                    timeframes = sym_data.get("timeframes")
                    if not isinstance(timeframes, dict):
                        continue
                    for tf_state in timeframes.values():
                        if not isinstance(tf_state, dict):
                            continue
                        ema_map = tf_state.get("ema")
                        if isinstance(ema_map, dict) and ema_map:
                            has_ema_data = True
                            break
                    if has_ema_data:
                        break
            if not has_ema_data:
                txt = render.text or ""
                if _EMA_TOKEN_RE.search(txt) or _EMA_WITH_NUMBER_RE.search(txt):
                    render.text = _sanitize_ema_language(txt)
        except Exception:
            pass
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
                publish_id=publish_id,
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
                    kind=kind,
                    symbol=symbol,
                    pivots=None,
                    analysis_mode=context_mode,
                    output_mode=context_output_mode,
                    label=label,
                    files=render.files,
                    raise_on_block=True,
                )
            except Exception as exc:
                status = "send_failed"
                try:
                    print(
                        f"[WARN] publish failed label={label} status={status} publish_id={publish_id} "
                        f"exc={type(exc).__name__}: {exc}"
                    )
                except Exception:
                    pass
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
                    publish_id=publish_id,
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
                raise_on_block=True,
            )
        except Exception as exc:
            status = "fallback_send_failed"
            try:
                print(
                    f"[WARN] publish failed label={label} status={status} publish_id={publish_id} "
                    f"exc={type(exc).__name__}: {exc}"
                )
            except Exception:
                pass
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
                publish_id=publish_id,
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
            kind=kind,
            symbol=symbol,
            pivots=None,
            analysis_mode=context_mode,
            output_mode=context_output_mode,
            label=label,
            files=render.files,
            raise_on_block=True,
        )
        _mark_rate_usage(
            label=label,
            symbol=symbol,
            render=render,
            requester_id=requester_id,
            request_kind=request_kind,
        )
    except Exception as exc:
        status = "send_failed"
        try:
            print(
                f"[WARN] publish failed label={label} status={status} publish_id={publish_id} "
                f"exc={type(exc).__name__}: {exc}"
            )
        except Exception:
            pass
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
            publish_id=publish_id,
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


def _sanitize_ema_language(raw: object) -> str:
    """Remove EMA references from free-form text.

    Used to keep strict contract validation consistent when EMA data isn't present.
    """

    text = str(raw or "")

    def _number_repl(match: re.Match[str]) -> str:
        digits = match.group(1)
        return f"avg {digits}"

    cleaned = _EMA_WITH_NUMBER_RE.sub(_number_repl, text)
    cleaned = _EMA_TOKEN_RE.sub("avg", cleaned)
    return cleaned


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

    lines: list[str] = [f"🚨 **{sym} — Market Context**"]
    lines.append("_This is context, not a trade command._")
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
    lines.append("🧠 Thesis Map (not a trade command):")
    lines.append(f"- Bull thesis strengthens if price reclaims and holds above **{bull_entry_level}**")
    lines.append(f"- Upside levels to watch: {upside_chain}")
    lines.append(f"- Bull thesis weakens if price loses **{bull_entry_level}**")
    lines.append(f"- Bear thesis strengthens if price loses and holds below **{bull_entry_level}**")
    lines.append(f"- Downside levels to watch: {downside_chain}")
    lines.append(f"- Bear thesis weakens if price reclaims **{bull_entry_level}**")

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

    # Optional: attach cached macro regime (controller-side) for posture gating.
    try:
        if (os.getenv("TNT_MACRO_REGIME_ENABLED", "1") or "1") == "1":
            from services.macro_regime import get_cached_macro_regime
            from services.redis_env import redis_client

            r = redis_client(timeout_s=0.4, decode_responses=True)
            reg = get_cached_macro_regime(r)
            if isinstance(reg, dict) and reg:
                payload["macro_regime"] = reg
    except Exception:  # noqa: BLE001
        pass

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
    lines.append("🧠 Notes (not a trade command):")
    lines.append("• Use commands for full detail; autopost is a heartbeat")
    lines.append("")
    lines.append("🚫 Do Nothing If:")
    lines.append("• Data is stale or indicators conflict")
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
_automation_paperdesk_trades_task: Optional[asyncio.Task] = None
_automation_macro_calendar_task: Optional[asyncio.Task] = None
_automation_heartbeat_task: Optional[asyncio.Task] = None
_automation_earnings_task: Optional[asyncio.Task] = None
_automation_earnings_results_task: Optional[asyncio.Task] = None
_automation_earnings_results_prewarm_task: Optional[asyncio.Task] = None
_automation_daily_outlook_task: Optional[asyncio.Task] = None
_automation_weekly_outlook_task: Optional[asyncio.Task] = None
_automation_divergence_watch_task: Optional[asyncio.Task] = None
_automation_earnings_pressure_watch_task: Optional[asyncio.Task] = None
_slash_tree_synced = False

_last_automation_posted_by_key: Dict[str, int] = {}

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

ContextState = Dict[str, object]
_last_context_post_by_symbol: Dict[str, int] = {}
_last_context_state: Dict[str, ContextState] = {}
_context_global_post_ts: deque[int] = deque(maxlen=256)

_last_divergence_post_by_key: Dict[str, int] = {}
_divergence_global_post_ts: deque[int] = deque(maxlen=256)

_last_earnings_pressure_post_by_key: Dict[str, int] = {}

_last_summary_date: Optional[date] = None
_last_morning_opt_date: Optional[date] = None
_last_vix_alert_level: Optional[float] = None
_last_news_db_mismatch_warn_ts: float | None = None
_last_vix_alert_ts: Optional[str] = None
_last_monday_date: Optional[date] = None

_data_stale_paused = False
_data_stale_lock: Optional[asyncio.Lock] = None
_data_stale_initialized = False

_feed_stale_paused = False
_feed_stale_lock: Optional[asyncio.Lock] = None
_feed_stale_initialized = False

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
# TNT agent calls are model-locked in delivery.tnt_llm; keep a stable, non-drifting default here.
AI_MODEL = "gpt-5.2"
AI_MAX_CHARS = int(os.getenv("DISCORD_AI_MAX_CHARS", "1200") or "1200")
VERBOSE_DEFAULT = os.getenv("DISCORD_VERBOSE_DEFAULT", "0") == "1"


def _ai_enabled() -> bool:
    return os.getenv("DISCORD_AI_ENABLED", "0") in ("1", "true", "True")


def _ai_model() -> str:
    # All TNT model calls must use the locked model to avoid drift.
    try:
        from delivery.tnt_llm import TNT_LOCKED_MODEL

        return TNT_LOCKED_MODEL
    except Exception:  # noqa: BLE001
        return "gpt-5.2"


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
    # Legacy knobs like OPENAI_MODEL_COACH are intentionally ignored here to prevent drift.
    try:
        from delivery.tnt_llm import TNT_LOCKED_MODEL
    except Exception:  # noqa: BLE001
        TNT_LOCKED_MODEL = "gpt-5.2"

    requested = (os.getenv("TNT_LLM_MODEL") or "").strip()
    if requested and requested != TNT_LOCKED_MODEL:
        return TNT_LOCKED_MODEL
    return TNT_LOCKED_MODEL


def _coach_max_tokens() -> int:
    try:
        value = int(os.getenv("COACH_MAX_TOKENS", "900") or "900")
    except Exception:  # noqa: BLE001
        value = 900
    return max(200, min(value, 2000))


def _coach_max_tokens_for_ask() -> int:
    """Lower cap for /ask fallback coaching to prevent runaway token spend."""

    try:
        value = int(os.getenv("COACH_MAX_TOKENS_ASK", "450") or "450")
    except Exception:  # noqa: BLE001
        value = 450
    return max(150, min(value, 1200))


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
        filler="Respect capital until data refreshes.",
    )

    next_line = f"Re-run /analyze {symbol.upper()} once the feed updates."
    return "\n".join(
        [
            "A) State",
            f"Not actionable for {symbol.upper()} — data not ready.",
            "",
            "B) Why",
            *[f"- {line}" for line in why_lines],
            "",
            "C) Action",
            "- DO NOTHING until data is healthy.",
            f"- {next_line}",
            "",
            "D) Risk + invalidation",
            "- n/a",
        ]
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
        "Respond using exactly this envelope with these headings (no extra sections):\n"
        "A) State\n"
        "B) Why\n"
        "C) Action\n"
        "D) Risk + invalidation\n\n"
        "Rules:\n"
        "- If permissions.no_trade is true OR data is stale, you MUST lead C) Action with DO NOTHING.\n"
        "- Do not invent prices/levels/indicators; only reference what TNT_STATE contains.\n\n"
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
    "COACH MODE OVERRIDE (CONSISTENCY CONTRACT):\n"
    "- Your ONLY factual context is TNT_STATE (authoritative).\n"
    "- TNT_STATE.context.canonical_ctx (if present) is the canonical snapshot layer (ctx:market + ctx:sym).\n"
    "- ctx:sym:{SYM}.candidates (if present) is the ONLY source of truth for structure recommendations.\n"
    "  - Language discipline: call them 'AI Trade Candidates' / 'AI Option Structure Candidates' (NOT signals, entries, calls, predictions, or trades).\n"
    f"  - Canonical definition (do not deviate): {AI_TRADE_CANDIDATES_CANONICAL_DEFINITION}\n"
    f"  - If the user treats a candidate like a signal/entry, you MUST respond verbatim: '{AI_TRADE_CANDIDATES_MISINTERPRETATION_CORRECTION_SENTENCE}'\n"
    f"  - If candidates.status=APPROVED and candidates.top exists: surface ONLY that candidate using the template: context → name → why it fits → '{AI_TRADE_CANDIDATES_BOUNDARY_SENTENCE}' → invalidation → when NOT to take it.\n"
    f"  - If candidates.status=NONE: you MUST say: '{AI_TRADE_CANDIDATES_NO_CANDIDATE_SENTENCE}' Then briefly say why. Do NOT apologize.\n"
    "  - If candidates.status=STALE or ERROR: you MUST refuse to surface a candidate (observational only).\n"
    "- If canonical ctx is missing or incomplete, you MUST degrade to observational mode (say what's missing; do not invent).\n"
    "- You are NOT allowed to contradict TNT tokens (bias/regime/permissions).\n"
    "- If permissions.no_trade is true, you MUST lead with DO NOTHING (no entries).\n"
    "- For geopolitics/news questions: do not claim breaking developments unless they appear in canonical ctx news fields; otherwise speak in general mechanics + explicitly say you can't confirm headlines.\n"
    "- Always output exactly this envelope with these headings (no extra sections):\n"
    "  A) State\n"
    "  B) Why\n"
    "  C) Action\n"
    "  D) Risk + invalidation\n"
    "- Confidence language must map to confidence bands: HIGH=direct; MED=conditional; LOW=wait.\n"
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

    # This response is intentionally scenario-based (daily history only).
    # Last close is included as a factual anchor from bars.
    answer = f"{answer} (Last close: {last_close})"

    return "\n".join(
        [
            "A) State",
            f"Daily trend snapshot for {sym}: {answer}",
            "",
            "B) Why",
            "- This is derived from recent daily bars (not intraday).",
            "- Treat it as context, not a trigger.",
            "",
            "C) Action",
            "- DO NOTHING until you have fresh /analyze and healthy intraday context.",
            f"- If you still want a scenario: bullish only if {bull}",
            f"- Bearish only if {bear}",
            f"- DO NOTHING if {idle}",
            "",
            "D) Risk + invalidation",
            f"- Invalidation: {invalid}",
        ]
    )


def _build_coach_error_response(symbol: str, reason: str) -> str:
    symbol_clean = (symbol or "").strip().upper() or "UNKNOWN"
    reason_clean = (reason or "coach error").strip().split("\n", 1)[0][:160]
    return "\n".join(
        [
            "A) State",
            f"Coach temporarily unavailable for {symbol_clean}.",
            "",
            "B) Why",
            f"- {reason_clean}.",
            "",
            "C) Action",
            "- DO NOTHING until coaching recovers.",
            "- Use the latest /analyze output for levels and gates.",
            "",
            "D) Risk + invalidation",
            "- n/a",
        ]
    )


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
    max_output_tokens: int | None = None,
) -> tuple[str, Optional[str], Optional[str]]:
    tnt_state = build_tnt_state_from_analysis_payload(analysis_payload).state

    # Canonical ctx snapshot injection (ctx:*). This is the single source of truth.
    try:
        canon = analysis_payload.get("canonical_ctx")
        if isinstance(canon, Mapping):
            ctx = tnt_state.get("context")
            if isinstance(ctx, dict):
                # Keep small; callers already pre-trim the ctx payload.
                ctx["canonical_ctx"] = dict(canon)
    except Exception:  # noqa: BLE001
        pass
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
        max_output_tokens=int(max_output_tokens) if isinstance(max_output_tokens, int) else _coach_max_tokens(),
        temperature=0.35,
        system_addendum=_COACH_SYSTEM_ADDENDUM,
    )
    return (result.text or "").strip(), result.prompt_sha256, result.tnt_state_sha256


async def _run_analyze_for_symbol(symbol: str) -> tuple[str, dict[str, Any]]:
    render, _ = await _fetch_on_demand_render(symbol, allow_cache=True)
    payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
    return render.text, payload


async def run_analyze_then_coach(
    symbol: str,
    question: str,
    *,
    user_id: int | None = None,
    channel_id: int | None = None,
    source: str | None = None,
    is_admin: bool = False,
    owner_available: bool = False,
    draining: bool = False,
    safe_mode: bool = False,
    live_hours: bool = False,
    fast_mode: bool = False,
    cheap_mode: bool = False,
) -> tuple[str, RenderedPost, str, bool, float]:
    started = time_lib.time()
    sym_in = (symbol or "").strip().upper()

    # Consistency Contract: canonical ctx snapshots must be present.
    ctx_missing: list[str] = []
    ctx_market: dict | None = None
    ctx_sym: dict | None = None
    spec: IntentSpec | None = None
    r_ctx = None
    try:
        from services.context.ctx_mode import ctx_enabled as _ctx_enabled
    except Exception:
        _ctx_enabled = None
    ctx_enabled = bool(_ctx_enabled()) if _ctx_enabled is not None else ((os.getenv("CTX_SNAPSHOT_ENABLED", "0") or "0").strip() == "1")
    if ctx_enabled:
        try:
            r_ctx = redis_client_for_ctx()
            spec = resolve_intent_and_required_keys(message_text=question or "", r=r_ctx, provided_symbol=sym_in or None)
            ctx_market, ctx_sym, ctx_missing = load_required_ctx(r=r_ctx, spec=spec, consumer="coach")
        except Exception:
            ctx_market, ctx_sym, ctx_missing = None, None, ["ctx:redis_unavailable"]
    else:
        ctx_missing = ["CTX_SNAPSHOT_DISABLED"]

    # Use validated symbol from the spec when present.
    sym = (spec.symbol if spec is not None else sym_in) or sym_in

    # CHEAP_QA lane: no LLM, no /analyze/worker dependency; may use lightweight HTTP APIs.
    # This is intended for low-token Q&A that can still answer earnings/news.
    if bool(cheap_mode) and not bool(fast_mode):
        from datetime import timedelta

        def _render_env_receipt(*, decision: str, reason: str) -> str:
            ctx_age_s = None
            try:
                if isinstance(ctx_market, dict) and ctx_market.get("ts_utc") is not None:
                    ctx_age_s = max(0, int(time_lib.time()) - int(float(ctx_market.get("ts_utc"))))
            except Exception:
                ctx_age_s = None
            ctx_label = "missing" if not isinstance(ctx_market, dict) else (f"{ctx_age_s}s" if ctx_age_s is not None else "unknown")
            return f"mode=CHEAP_QA | ctx_age={ctx_label} | decision={decision} | reason={reason} | llm=0ms"

        def _truncate_lines(lines: list[str], *, max_chars: int = 1700) -> str:
            out: list[str] = []
            used = 0
            for ln in lines:
                s = (ln or "").rstrip()
                if not s:
                    candidate = ""
                else:
                    candidate = s
                extra = (len(candidate) + 1) if out else len(candidate)
                if used + extra > max_chars:
                    break
                out.append(candidate)
                used += extra
            text = "\n".join(out).strip()
            if len(text) > max_chars:
                text = text[: max_chars - 3].rstrip() + "..."
            return text

        def _extract_iso_date(text: str) -> str | None:
            import re

            raw = str(text or "")
            m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
            if not m:
                return None
            return str(m.group(1) or "").strip() or None

        def _infer_when_from_dt_et(dt_et: datetime) -> str:
            try:
                if int(dt_et.hour) < 12:
                    return "BMO"
                if int(dt_et.hour) >= 16:
                    return "AMC"
                return "TAS"
            except Exception:
                return "TAS"

        try:
            from services.nl.nl_router import route_text as _route_text

            nl = _route_text(text=question or "")
        except Exception:
            nl = None

        def _is_futures_root(sym0: str) -> bool:
            s = (sym0 or "").strip().upper()
            if not s:
                return False
            # Operators can override this list without code changes.
            roots_raw = os.getenv(
                "CHEAP_QA_FUTURES_ROOTS",
                "ES,NQ,YM,RTY,CL,GC,SI,NG,HG,ZB,ZN,ZF,ZT,6E,6J,6B,6A,6C",
            )
            roots = {x.strip().upper() for x in str(roots_raw or "").split(",") if x.strip()}
            return s in roots

        def _fmt_num(val: object, *, digits: int = 2) -> str:
            try:
                if val is None:
                    return "n/a"
                f = float(val)
                if not (f == f):
                    return "n/a"
                return f"{f:.{int(digits)}f}"
            except Exception:
                return "n/a"

        def _fmt_int(val: object) -> str:
            try:
                if val is None:
                    return "n/a"
                return f"{int(float(val)):,}"
            except Exception:
                return "n/a"

        def _fmt_money(val: object) -> str:
            s = _fmt_num(val, digits=2)
            return "n/a" if s == "n/a" else f"${s}"

        def _fmt_signed(val: object, *, digits: int = 2) -> str:
            try:
                if val is None:
                    return "n/a"
                f = float(val)
                if not (f == f):
                    return "n/a"
                sign = "+" if f >= 0 else ""
                return f"{sign}{f:.{int(digits)}f}"
            except Exception:
                return "n/a"

        # NEWS (Massive/Benzinga)
        try:
            if nl is not None and getattr(nl, "route", None) == "news":
                sym_news = (getattr(nl, "symbol", None) or sym or "").strip().upper() or None
                base = (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").strip().rstrip("/")
                key = _resolve_massive_api_key()
                if not key:
                    msg = "📰 News: missing MASSIVE_API_KEY (or POLYGON_API_KEY fallback)."
                    latency_ms = (time_lib.time() - started) * 1000.0
                    receipt = _render_env_receipt(decision="news", reason="missing_key")
                    return (
                        msg,
                        RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                        "ok_degraded",
                        False,
                        latency_ms,
                    )

                try:
                    from services.news.massive_benzinga_news import fetch_benzinga_news

                    since = _now_utc().replace(tzinfo=timezone.utc) - timedelta(hours=24)
                    limit = max(3, min(8, _env_int("CHEAP_QA_NEWS_LIMIT", 5)))
                    items = await fetch_benzinga_news(
                        base_url=base,
                        api_key=key,
                        tickers=[sym_news] if sym_news else None,
                        published_since_utc=since,
                        limit=int(limit),
                        timeout_s=float(_env_int("CHEAP_QA_NEWS_TIMEOUT_S", 10)),
                    )
                except Exception:
                    items = []

                lines: list[str] = []
                title = f"📰 News (24h){' — ' + sym_news if sym_news else ''}"
                lines.append(title)
                if items:
                    for it in items[: int(limit)]:
                        try:
                            tickers = it.tickers if hasattr(it, "tickers") else []
                            tick_str = f" [{', '.join(tickers[:4])}]" if tickers else ""
                        except Exception:
                            tick_str = ""
                        headline = str(getattr(it, "headline", "") or "").strip()
                        url = str(getattr(it, "url", "") or "").strip()
                        if url:
                            lines.append(f"• {headline}{tick_str}\n  {url}")
                        else:
                            lines.append(f"• {headline}{tick_str}")
                else:
                    lines.append("• No recent headlines returned (provider empty / rate limited)")

                out = _truncate_lines(lines, max_chars=1700)
                latency_ms = (time_lib.time() - started) * 1000.0
                receipt = _render_env_receipt(decision="news", reason="ok" if items else "empty")
                return (
                    out,
                    RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                    "ok" if items else "ok_degraded",
                    False,
                    latency_ms,
                )
        except Exception:
            # Never let CHEAP_QA break the caller; fall through to other modes.
            pass

        # EARNINGS (Massive/Benzinga)
        try:
            if nl is not None and getattr(nl, "route", None) == "earnings":
                now_et = _now_et()
                now_utc = _now_utc().replace(tzinfo=timezone.utc)
                watchlist = [s.strip().upper() for s in (os.getenv("EARNINGS_WATCHLIST") or "").split(",") if s.strip()]

                base = (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").strip().rstrip("/")
                key = _resolve_earnings_api_key()

                if not key:
                    msg = "📅 Earnings: missing API key (set EARNINGS_API_KEY or MASSIVE_API_KEY / POLYGON_API_KEY fallback)."
                    latency_ms = (time_lib.time() - started) * 1000.0
                    receipt = _render_env_receipt(decision="earnings", reason="missing_key")
                    return (
                        msg,
                        RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                        "ok_degraded",
                        False,
                        latency_ms,
                    )

                handler = str(getattr(nl, "handler", "") or "")
                if handler == "earnings_symbol":
                    sym_e = (getattr(nl, "symbol", None) or sym or "").strip().upper()
                    if not sym_e:
                        msg = "📅 Earnings: provide a ticker (e.g., 'AAPL earnings date')."
                        latency_ms = (time_lib.time() - started) * 1000.0
                        receipt = _render_env_receipt(decision="earnings_symbol", reason="missing_symbol")
                        return (
                            msg,
                            RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                            "ok_degraded",
                            False,
                            latency_ms,
                        )

                    # Query a forward window and pick the next upcoming.
                    try:
                        from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings, pick_next_earnings

                        start_d = now_et.date()
                        end_d = (now_et.date() + timedelta(days=int(_env_int("CHEAP_QA_EARNINGS_LOOKAHEAD_DAYS", 90))))
                        recs = await fetch_benzinga_earnings(
                            base_url=base,
                            api_key=key,
                            tickers=[sym_e],
                            start_date=start_d,
                            end_date=end_d,
                            limit=200,
                            timeout_s=float(_env_int("CHEAP_QA_EARNINGS_TIMEOUT_S", 12)),
                        )
                        nxt = pick_next_earnings(recs, symbol=sym_e, now_utc=now_utc)
                    except Exception:
                        nxt = None

                    if nxt is None:
                        out = f"📅 Earnings — {sym_e}\n• No upcoming earnings found in the next ~90 days (or provider returned empty)."
                        latency_ms = (time_lib.time() - started) * 1000.0
                        receipt = _render_env_receipt(decision="earnings_symbol", reason="no_hits")
                        return (
                            out,
                            RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                            "ok_degraded",
                            False,
                            latency_ms,
                        )

                    dt_et = nxt.ts_utc.astimezone(ET)
                    when = _infer_when_from_dt_et(dt_et)
                    date_et = dt_et.date().isoformat()
                    days = (dt_et.date() - now_et.date()).days
                    conf = "confirmed" if bool(getattr(nxt, "confirmed", True)) else "unconfirmed"
                    out = (
                        f"📅 Earnings — {sym_e}\n"
                        f"• Next: {date_et} ({when}, ET) — {conf}\n"
                        f"• Timing: {days} day(s) away\n"
                        "Context only • Not financial advice"
                    )
                    latency_ms = (time_lib.time() - started) * 1000.0
                    receipt = _render_env_receipt(decision="earnings_symbol", reason="ok")
                    return (
                        out,
                        RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                        "ok",
                        False,
                        latency_ms,
                    )

                # earnings_calendar (window)
                q_lower = str(question or "").lower()
                explicit_date = _extract_iso_date(question or "")

                # Default: tomorrow (next weekday).
                if explicit_date:
                    start_d = date.fromisoformat(explicit_date)
                    end_d = start_d
                    label = f"{explicit_date}"
                elif "today" in q_lower:
                    start_d = now_et.date()
                    end_d = now_et.date()
                    label = f"{start_d.isoformat()}"
                elif "tomorrow" in q_lower:
                    start_d = next_weekday(now_et.date() + timedelta(days=1))
                    end_d = start_d
                    label = f"{start_d.isoformat()}"
                elif "next week" in q_lower:
                    # Next week's Monday..Friday
                    d0 = now_et.date()
                    days_ahead = (7 - d0.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    monday = d0 + timedelta(days=days_ahead)
                    start_d = monday
                    end_d = monday + timedelta(days=4)
                    label = f"{start_d.isoformat()}..{end_d.isoformat()}"
                elif "this week" in q_lower:
                    d0 = now_et.date()
                    start_d = d0
                    end_d = d0 + timedelta(days=max(0, 4 - d0.weekday()))
                    label = f"{start_d.isoformat()}..{end_d.isoformat()}"
                else:
                    start_d = next_weekday(now_et.date() + timedelta(days=1))
                    end_d = start_d
                    label = f"{start_d.isoformat()}"

                # Safety: do not fetch a full-market calendar; require a watchlist.
                if not watchlist:
                    out = (
                        "📅 Earnings (watchlist-only)\n"
                        "• EARNINGS_WATCHLIST is empty, so TNT won’t pull the full market calendar in cheap mode.\n"
                        "• Ask a single ticker (e.g., 'AAPL earnings date') or set EARNINGS_WATCHLIST."
                    )
                    latency_ms = (time_lib.time() - started) * 1000.0
                    receipt = _render_env_receipt(decision="earnings_calendar", reason="empty_watchlist")
                    return (
                        out,
                        RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                        "ok_degraded",
                        False,
                        latency_ms,
                    )

                try:
                    from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings

                    recs = await fetch_benzinga_earnings(
                        base_url=base,
                        api_key=key,
                        tickers=watchlist,
                        start_date=start_d,
                        end_date=end_d,
                        limit=800,
                        timeout_s=float(_env_int("CHEAP_QA_EARNINGS_TIMEOUT_S", 12)),
                    )
                except Exception:
                    recs = []

                by_date: dict[str, dict[str, list[str]]] = {}
                for r in recs or []:
                    try:
                        dt_et = r.ts_utc.astimezone(ET)
                        d = dt_et.date().isoformat()
                        when = _infer_when_from_dt_et(dt_et)
                        by_date.setdefault(d, {}).setdefault(when, []).append(str(r.symbol or "").strip().upper())
                    except Exception:
                        continue

                lines = [f"📅 Earnings (watchlist) — {label} (ET)"]
                if not by_date:
                    lines.append("• None found for tracked symbols")
                else:
                    for d in sorted(by_date.keys()):
                        buckets = by_date[d]
                        parts: list[str] = []
                        for k in ("BMO", "AMC", "TAS"):
                            syms = sorted({s for s in (buckets.get(k) or []) if s})
                            if syms:
                                parts.append(f"{k}: {', '.join(syms[:12])}{'…' if len(syms) > 12 else ''}")
                        lines.append(f"• {d}: " + (" | ".join(parts) if parts else "(none)"))
                lines.append("Context only • Not financial advice")

                out = _truncate_lines(lines, max_chars=1700)
                latency_ms = (time_lib.time() - started) * 1000.0
                receipt = _render_env_receipt(decision="earnings_calendar", reason="ok" if by_date else "empty")
                return (
                    out,
                    RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                    "ok" if by_date else "ok_degraded",
                    False,
                    latency_ms,
                )
        except Exception:
            pass

        # Premium snapshot fallback for any ticker (Polygon equities/indices, Databento futures via FuturesStore).
        # This is the primary "any ticker" answer path when cheap_mode is enabled.
        try:
            sym_snap = (sym or "").strip().upper()
            if sym_snap:
                ql = str(question or "").strip().lower()

                # FUTURES roots: prefer ingest quote; fallback to Databento historical (inside FuturesStore).
                if _is_futures_root(sym_snap) or ("futures" in ql and _is_futures_root(sym_snap)):
                    quote = None
                    try:
                        from services.futures.futures_store import FuturesStore

                        store = FuturesStore(r_ctx) if r_ctx is not None else FuturesStore()
                        quotes = await asyncio.to_thread(store.get_quotes_with_fallback, [sym_snap])
                        quote = quotes.get(sym_snap) if isinstance(quotes, dict) else None
                    except Exception:
                        quote = None

                    lines = [f"📈 TNT Snapshot — {sym_snap} (Futures)"]
                    if quote is None:
                        lines.append("• Quote: unavailable (futures ingest offline and Databento fallback missing)")
                        lines.append("• Check: futures ingest heartbeat / DATABENTO_API_KEY")
                        status = "ok_degraded"
                    else:
                        try:
                            ts_et = datetime.fromtimestamp(int(getattr(quote, "ts_utc", 0)), tz=timezone.utc).astimezone(ET)
                            ts_lab = ts_et.strftime("%Y-%m-%d %H:%M:%S ET")
                        except Exception:
                            ts_lab = "n/a"
                        px = getattr(quote, "px", None)
                        chg = getattr(quote, "chg", None)
                        chg_pct = getattr(quote, "chg_pct", None)
                        src = str(getattr(quote, "source", "") or "").strip() or "unknown"
                        lines.append(f"• Last: {_fmt_num(px, digits=2)} | Δ {_fmt_signed(chg, digits=2)} ({_fmt_signed(chg_pct, digits=2)}%)")
                        lines.append(f"• As of: {ts_lab} | source={src}")

                        # Premium futures enrichment: posture/context from futures scores (Redis-backed).
                        try:
                            if _cheapqa_enrich_enabled() and _cheapqa_enrich_futures_enabled():
                                from services.futures.futures_context_line import WARN_AGE_S_DEFAULT, render_futures_context_line

                                scores = await asyncio.to_thread(store.get_scores)
                                scores_max_age = int(os.getenv("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120") or "120")
                                warn_age = int(os.getenv("TNT_FUTURES_INGEST_SCORES_WARN_AGE_SEC", str(WARN_AGE_S_DEFAULT)) or str(WARN_AGE_S_DEFAULT))
                                now = int(time_lib.time())
                                ctx_line = render_futures_context_line(
                                    scores,
                                    now_epoch=now,
                                    max_age_sec=int(max(30, min(scores_max_age, 3600))),
                                    warn_age_sec=int(max(60, min(warn_age, 3600))),
                                )
                                if ctx_line:
                                    lines.append(ctx_line)
                        except Exception:
                            pass
                        status = "ok"

                    out = _truncate_lines(lines, max_chars=1700)
                    latency_ms = (time_lib.time() - started) * 1000.0
                    receipt = _render_env_receipt(decision="snapshot_futures", reason=status)
                    return (
                        out,
                        RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                        status,
                        False,
                        latency_ms,
                    )

                # Indices (Polygon index snapshot) if the user provided I: symbols.
                if sym_snap.startswith("I:"):
                    payload = None
                    try:
                        from delivery.on_demand_data import extract_index_last_prev_close_ts, polygon_index_snapshot

                        payload = await asyncio.to_thread(polygon_index_snapshot, sym_snap)
                        last, prev_close, ts_iso = extract_index_last_prev_close_ts(payload if isinstance(payload, dict) else {})
                    except Exception:
                        last, prev_close, ts_iso = None, None, None

                    lines = [f"📈 TNT Snapshot — {sym_snap} (Index)"]
                    if last is None and prev_close is None:
                        lines.append("• Quote: unavailable (missing/invalid POLYGON_API_KEY or snapshot empty)")
                        status = "ok_degraded"
                    else:
                        px = last if last is not None else prev_close
                        delta = None
                        pct = None
                        if px is not None and prev_close is not None and prev_close:
                            try:
                                delta = float(px) - float(prev_close)
                                pct = (float(delta) / float(prev_close)) * 100.0
                            except Exception:
                                delta, pct = None, None
                        ts_lab = None
                        try:
                            if ts_iso:
                                ts_lab = parse_iso(str(ts_iso)).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
                        except Exception:
                            ts_lab = None
                        lines.append(f"• Last: {_fmt_num(px, digits=2)} | Δ {_fmt_signed(delta, digits=2)} ({_fmt_signed(pct, digits=2)}%)")
                        if prev_close is not None:
                            lines.append(f"• Prev close: {_fmt_num(prev_close, digits=2)}")
                        if ts_lab:
                            lines.append(f"• As of: {ts_lab}")
                        status = "ok"

                    out = _truncate_lines(lines, max_chars=1700)
                    latency_ms = (time_lib.time() - started) * 1000.0
                    receipt = _render_env_receipt(decision="snapshot_index", reason=status)
                    return (
                        out,
                        RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                        status,
                        False,
                        latency_ms,
                    )

                # Equities/ETFs (Polygon stock snapshot).
                payload = None
                try:
                    from delivery.on_demand_data import polygon_snapshot

                    payload = await asyncio.to_thread(polygon_snapshot, sym_snap)
                except Exception:
                    payload = None

                ticker = (payload or {}).get("ticker") if isinstance(payload, dict) else None
                ticker = ticker if isinstance(ticker, dict) else {}
                last_trade = ticker.get("lastTrade") if isinstance(ticker.get("lastTrade"), dict) else {}
                last_quote = ticker.get("lastQuote") if isinstance(ticker.get("lastQuote"), dict) else {}
                day = ticker.get("day") if isinstance(ticker.get("day"), dict) else {}
                prev = ticker.get("prevDay") if isinstance(ticker.get("prevDay"), dict) else {}

                # Price + timestamp (best-effort).
                last_px = last_trade.get("p")
                last_ts = last_trade.get("t")
                if last_px is None:
                    last_px = last_quote.get("P") or last_quote.get("p")
                    last_ts = last_quote.get("t")
                if last_px is None:
                    last_px = day.get("c")
                    last_ts = day.get("t")
                if last_px is None:
                    last_px = prev.get("c")
                    last_ts = prev.get("t")

                prev_close = prev.get("c")
                delta = None
                pct = None
                if last_px is not None and prev_close not in (None, 0, 0.0):
                    try:
                        delta = float(last_px) - float(prev_close)
                        pct = (float(delta) / float(prev_close)) * 100.0
                    except Exception:
                        delta, pct = None, None

                ts_lab = None
                try:
                    if last_ts is not None:
                        # Polygon uses ms epoch.
                        ts_s = float(last_ts) / 1000.0 if float(last_ts) > 1e10 else float(last_ts)
                        ts_lab = datetime.fromtimestamp(ts_s, tz=timezone.utc).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
                except Exception:
                    ts_lab = None

                lines = [f"💎 TNT Snapshot — {sym_snap}"]
                if last_px is None:
                    lines.append("• Quote: unavailable (missing/invalid POLYGON_API_KEY or unsupported symbol)")
                    lines.append("• Tip: if this is a futures root, ask with a futures symbol like ES/NQ/CL")
                    status = "ok_degraded"
                else:
                    lines.append(f"• Last: {_fmt_money(last_px)} | Δ {_fmt_money(delta)} ({_fmt_signed(pct, digits=2)}%)")

                    o = day.get("o")
                    h = day.get("h")
                    l = day.get("l")
                    c = day.get("c")
                    v = day.get("v")
                    vw = day.get("vw")
                    rng = f"O {_fmt_money(o)} H {_fmt_money(h)} L {_fmt_money(l)} C {_fmt_money(c)}"
                    lines.append(f"• Day: {rng} | VWAP {_fmt_money(vw)} | Vol {_fmt_int(v)}")

                    if prev_close is not None:
                        lines.append(f"• Prev close: {_fmt_money(prev_close)}")
                    if ts_lab:
                        lines.append(f"• As of: {ts_lab}")

                    # Premium enrichments (still no LLM) — bounded + cached.
                    try:
                        if _cheapqa_enrich_enabled() and _cheapqa_enrich_news_enabled():
                            news_lines = await _cheapqa_latest_news_lines(sym_snap)
                            if news_lines:
                                lines.extend(news_lines[:1])
                    except Exception:
                        pass
                    try:
                        if _cheapqa_enrich_enabled() and _cheapqa_enrich_earnings_enabled():
                            asof_utc = None
                            try:
                                if last_ts is not None:
                                    ts_s = float(last_ts) / 1000.0 if float(last_ts) > 1e10 else float(last_ts)
                                    asof_utc = datetime.fromtimestamp(ts_s, tz=timezone.utc)
                            except Exception:
                                asof_utc = None
                            e_line = await _cheapqa_next_earnings_line(sym_snap, now_utc=asof_utc)
                            if e_line:
                                lines.append(e_line)
                    except Exception:
                        pass

                    lines.append("• Ask: 'earnings date' or 'why is it moving' for details")
                    status = "ok"

                out = _truncate_lines(lines, max_chars=1700)
                latency_ms = (time_lib.time() - started) * 1000.0
                receipt = _render_env_receipt(decision="snapshot_stock", reason=status)
                return (
                    out,
                    RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                    status,
                    False,
                    latency_ms,
                )
        except Exception:
            pass

        # If CHEAP_QA couldn't route/answer, fall back to FAST_QA templates (still no LLM).
        # By default, CHEAP_QA preserves a no-LLM guarantee.
        # Operators may enable a last-resort coach fallback for /ask via CHEAP_QA_ALLOW_COACH_FALLBACK=1.
        try:
            if _cheapqa_allow_coach_fallback() and nl is not None and getattr(nl, "route", None) == "coach":
                sym_fb = (sym or "").strip().upper()
                if sym_fb:
                    try:
                        analyze_timeout = float(os.getenv("CHEAP_QA_COACH_ANALYZE_TIMEOUT_S", "14") or "14")
                    except Exception:
                        analyze_timeout = 14.0

                    try:
                        render_text, analysis_payload = await asyncio.wait_for(_run_analyze_for_symbol(sym_fb), timeout=max(2.0, min(analyze_timeout, 30.0)))
                    except Exception:
                        render_text, analysis_payload = "", {}

                    if isinstance(analysis_payload, dict) and analysis_payload:
                        llm_started = time_lib.time()
                        try:
                            coach_text, _prompt_sha, _state_sha = await _call_openai_coach(
                                question=question,
                                render=RenderedPost(text=str(render_text or ""), agent_payload=dict(analysis_payload)),
                                analysis_payload=analysis_payload,
                                max_output_tokens=_coach_max_tokens_for_ask(),
                            )
                        except Exception as e:
                            coach_text = _build_coach_error_response(sym_fb, str(e))

                        coach_text = str(coach_text or "").strip()
                        if coach_text:
                            max_chars = _ai_max_chars()
                            if len(coach_text) > max_chars:
                                coach_text = coach_text[: max(0, max_chars - 3)].rstrip() + "..."
                            latency_ms = (time_lib.time() - started) * 1000.0
                            llm_ms = (time_lib.time() - llm_started) * 1000.0
                            receipt = f"mode=CHEAP_QA | decision=coach_fallback | reason=ok | llm={int(llm_ms)}ms"
                            return (
                                coach_text,
                                RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
                                "ok",
                                False,
                                latency_ms,
                            )
        except Exception:
            pass

        cheap_fallback = (
            "Context: cheap Q&A mode (no LLM).\n"
            "I can answer: premium ticker snapshot + earnings dates + recent headlines.\n"
            "For trade plans/levels, use `/trade` or `/regime`, or ask a specific symbol context.\n"
            "Confidence: LOW"
        )
        latency_ms = (time_lib.time() - started) * 1000.0
        receipt = _render_env_receipt(decision="fallback", reason="no_match")
        return (
            cheap_fallback,
            RenderedPost(text="", agent_payload={"cheap_mode": True, "cheap_receipt": receipt}),
            "ok_degraded",
            False,
            latency_ms,
        )

    # FAST_QA lane: never waits on /analyze, workers, options chain, or any heavy semaphores.
    # This mode intentionally returns deterministic templates only.
    if bool(fast_mode):
        try:
            from delivery.fastqa_state import update_fastqa_last as _update_fastqa_last
            from delivery.fastqa_state import record_fastqa_event as _record_fastqa_event
        except Exception:  # pragma: no cover - defensive
            _update_fastqa_last = None
            _record_fastqa_event = None

        def _env_flag(name: str, default: str = "0") -> bool:
            try:
                return (os.getenv(name, default) or default).strip() == "1"
            except Exception:
                return False

        def _now_utc() -> int:
            try:
                return int(time_lib.time())
            except Exception:
                return 0

        def _age_s(ts_utc: object) -> int | None:
            try:
                if ts_utc is None:
                    return None
                ts_i = int(float(ts_utc))
                return max(0, _now_utc() - ts_i)
            except Exception:
                return None

        def _pick_first_int(d: dict | None, keys: tuple[str, ...]) -> int | None:
            if not isinstance(d, dict):
                return None
            for k in keys:
                try:
                    v = d.get(k)
                    if v is None:
                        continue
                    return int(float(v))
                except Exception:
                    continue
            return None

        # Build a small receipt proving FAST_QA (no heavy, no LLM).
        ctx_age = _age_s(ctx_market.get("ts_utc")) if isinstance(ctx_market, dict) else None
        fut = (ctx_market.get("futures") if isinstance(ctx_market, dict) else None) if ctx_market is not None else None
        fut_age = _pick_first_int(fut, ("hb_age_sec", "hb_age_s", "age_sec", "age_s"))
        fut_degraded = False
        try:
            fut_degraded = bool(fut.get("degraded")) if isinstance(fut, dict) else False
        except Exception:
            fut_degraded = False

        fut_ok = (fut is not None) and (not fut_degraded) and (fut_age is None or fut_age <= 120)
        fut_label = "OK" if fut_ok else ("DEGRADED" if fut is not None else "MISSING")
        ctx_label = "missing" if (not ctx_market) else (f"{ctx_age}s" if ctx_age is not None else "unknown")
        fut_age_label = f"{fut_age}s" if fut_age is not None else "unknown"
        receipt = f"mode=FAST_QA | ctx_age={ctx_label} | fut={fut_label} age={fut_age_label} | llm=0ms"

        def _record(*, decision: str, reason: str, latency_ms: float, output_text: str | None = None) -> None:
            if _update_fastqa_last is None:
                return
            try:
                _update_fastqa_last(
                    ts_utc=float(time_lib.time()),
                    channel_id=channel_id,
                    user_id=user_id,
                    mode=str(source or "unknown"),
                    decision=str(decision),
                    reason=str(reason),
                    latency_ms=float(latency_ms),
                    ctx_ok=bool(isinstance(ctx_market, dict) and bool(ctx_market)),
                    ctx_age_s=ctx_age,
                    futures_ok=bool(fut_ok),
                    futures_age_s=fut_age,
                )
            except Exception:
                return

            if _record_fastqa_event is None:
                return
            try:
                _record_fastqa_event(
                    ts_utc=float(time_lib.time()),
                    decision=str(decision),
                    latency_ms=float(latency_ms),
                    output_text=str(output_text or ""),
                )
            except Exception:
                return

        if _env_flag("TNT_FAST_QA_DEBUG_LOG", "0") or _env_flag("TNT_FAST_QA_DEBUG", "0"):
            try:
                print(f"[FAST_QA] {receipt} user_id={user_id} channel_id={channel_id} source={source}")
            except Exception:
                pass

        # --- Moderator Authority (single boss) ---
        intercept_intents = set()
        try:
            from services.moderator.moderator_authority import Intent as _ModIntent
            from services.moderator.moderator_authority import decide_reply as _moderator_decide_reply

            intercept_intents = {
                _ModIntent.TRADE_ACTION,
                _ModIntent.RISK_STRUCTURE,
                _ModIntent.DATA_DIAGNOSTIC,
            }
            mod_decision = _moderator_decide_reply(
                question=question or "",
                symbol=sym or None,
                ctx_market=ctx_market,
                ctx_sym=ctx_sym,
                redis_client=r_ctx,
            )
        except Exception:
            mod_decision = None

        if mod_decision is not None and getattr(mod_decision, "intent", None) in intercept_intents:
            try:
                from delivery.moderator_audit import ModeratorAudit

                audit = ModeratorAudit.from_env()
                audit.log(
                    decision=mod_decision,
                    source=str(source or "coach"),
                    user_id=user_id,
                    channel_id=channel_id,
                    is_admin=bool(is_admin),
                    owner_available=bool(owner_available),
                    draining=bool(draining),
                    safe_mode=bool(safe_mode),
                    live_hours=bool(live_hours),
                    text=str(question or ""),
                    ctx_market=ctx_market,
                    ctx_sym=ctx_sym,
                )
            except Exception:
                pass

            latency_ms = (time_lib.time() - started) * 1000.0
            _record(
                decision="FAST_QA_MODERATED",
                reason=f"moderator_intent={getattr(mod_decision, 'intent', None)}",
                latency_ms=latency_ms,
                output_text=str(getattr(mod_decision, "reply", "") or ""),
            )
            return (
                str(getattr(mod_decision, "reply", "")).strip(),
                RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
                "ok_moderated",
                False,
                latency_ms,
            )

        # Deterministic templates (no /analyze dependency).
        try:
            if spec is not None and spec.intent == "FUTURES_OVERVIEW":
                latency_ms = (time_lib.time() - started) * 1000.0
                out = build_futures_overview(mkt=ctx_market)
                _record(
                    decision="FAST_QA",
                    reason=f"intent=FUTURES_OVERVIEW why={getattr(spec, 'why', '')}",
                    latency_ms=latency_ms,
                    output_text=out,
                )
                return (
                    out,
                    RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
                    "ok",
                    False,
                    latency_ms,
                )

            if spec is not None and spec.intent == "GREETING":
                latency_ms = (time_lib.time() - started) * 1000.0
                out = build_greeting(mkt=ctx_market)
                _record(
                    decision="FAST_QA",
                    reason=f"intent=GREETING why={getattr(spec, 'why', '')}",
                    latency_ms=latency_ms,
                    output_text=out,
                )
                return (
                    out,
                    RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
                    "ok",
                    False,
                    latency_ms,
                )

            if spec is not None and spec.intent == "MARKET_CONTEXT":
                latency_ms = (time_lib.time() - started) * 1000.0
                out = build_market_context(mkt=ctx_market, tnt_state={})
                _record(
                    decision="FAST_QA",
                    reason=f"intent=MARKET_CONTEXT why={getattr(spec, 'why', '')}",
                    latency_ms=latency_ms,
                    output_text=out,
                )
                return (
                    out,
                    RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
                    "ok",
                    False,
                    latency_ms,
                )

            if spec is not None and spec.intent == "CANDIDATE_REQUEST":
                merged_sym = ctx_sym
                try:
                    from services.context.ctx_reader import load_ctx_candidates

                    cand_ctx = load_ctx_candidates(r=r_ctx, symbol=sym) if r_ctx is not None else None
                    if isinstance(cand_ctx, dict):
                        base = dict(ctx_sym) if isinstance(ctx_sym, dict) else {}
                        base["candidates"] = cand_ctx
                        merged_sym = base
                except Exception:
                    merged_sym = ctx_sym

                msg = build_candidate_reply(symbol=sym, sym_ctx=merged_sym)
                latency_ms = (time_lib.time() - started) * 1000.0
                status = "ok" if isinstance(ctx_sym, dict) else "ok_standby"
                _record(
                    decision="FAST_QA",
                    reason=f"intent=CANDIDATE_REQUEST why={getattr(spec, 'why', '')} status={status}",
                    latency_ms=latency_ms,
                    output_text=msg,
                )
                return (
                    msg,
                    RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
                    status,
                    False,
                    latency_ms,
                )

            if spec is not None and spec.intent == "SYMBOL_CONTEXT":
                latency_ms = (time_lib.time() - started) * 1000.0
                if ctx_sym is None:
                    msg = build_symbol_context(symbol=sym, mkt=ctx_market, sym_ctx=None, tnt_state={})
                    _record(
                        decision="FAST_QA",
                        reason=f"intent=SYMBOL_CONTEXT why={getattr(spec, 'why', '')} status=ok_standby",
                        latency_ms=latency_ms,
                        output_text=msg,
                    )
                    return (
                        msg,
                        RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
                        "ok_standby",
                        False,
                        latency_ms,
                    )
                msg = build_symbol_context(symbol=sym, mkt=ctx_market, sym_ctx=ctx_sym, tnt_state={})
                _record(
                    decision="FAST_QA",
                    reason=f"intent=SYMBOL_CONTEXT why={getattr(spec, 'why', '')} status=ok",
                    latency_ms=latency_ms,
                    output_text=msg,
                )
                return (
                    msg,
                    RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
                    "ok",
                    False,
                    latency_ms,
                )
        except Exception:
            pass

        # Last-resort FAST_QA fallback (never punts, never queues, never blocks).
        latency_ms = (time_lib.time() - started) * 1000.0
        fallback = (
            "Context: fast Q&A mode (no heavy workers).\n"
            "Market Context — limited\n"
            "Bottom line: I can answer market/futures posture + risk framing immediately; for charts/renders use `/oi` or `/chart`.\n"
            "Confidence: LOW"
        )
        _record(
            decision="FAST_QA_FALLBACK",
            reason="no_fast_template_match",
            latency_ms=latency_ms,
            output_text=fallback,
        )
        return (
            fallback,
            RenderedPost(text="", agent_payload={"fast_mode": True, "fast_receipt": receipt}),
            "ok_degraded",
            False,
            latency_ms,
        )

    # --- Moderator Authority (single boss) ---
    # Deterministic safety/risk responses that must run *before* any coach/LLM calls.
    intercept_intents = set()
    try:
        from services.moderator.moderator_authority import Intent as _ModIntent
        from services.moderator.moderator_authority import decide_reply as _moderator_decide_reply

        intercept_intents = {
            _ModIntent.TRADE_ACTION,
            _ModIntent.RISK_STRUCTURE,
            _ModIntent.DATA_DIAGNOSTIC,
        }
        mod_decision = _moderator_decide_reply(
            question=question or "",
            symbol=sym or None,
            ctx_market=ctx_market,
            ctx_sym=ctx_sym,
            redis_client=r_ctx,
        )
    except Exception:
        mod_decision = None

    # Only intercept the intents where deterministic safety is required.
    if mod_decision is not None and getattr(mod_decision, "intent", None) in intercept_intents:
        # Audit log (best-effort; never breaks reply)
        try:
            from delivery.moderator_audit import ModeratorAudit

            # Keep a process-level singleton to avoid re-parsing env on each message.
            global _MOD_AUDIT  # type: ignore[declare-usage]
            try:
                _MOD_AUDIT
            except Exception:
                _MOD_AUDIT = ModeratorAudit.from_env()  # type: ignore[misc]

            _MOD_AUDIT.log(
                decision=mod_decision,
                source=str(source or "coach"),
                user_id=user_id,
                channel_id=channel_id,
                is_admin=bool(is_admin),
                owner_available=bool(owner_available),
                draining=bool(draining),
                safe_mode=bool(safe_mode),
                live_hours=bool(live_hours),
                text=str(question or ""),
                ctx_market=ctx_market,
                ctx_sym=ctx_sym,
            )
        except Exception:
            pass

        latency_ms = (time_lib.time() - started) * 1000.0
        cached = _get_cached_analyze(sym)
        render = cached.render if cached is not None else RenderedPost(text="", agent_payload={})
        # Status is conservative; downstream should not treat this as a full snapshot answer.
        return str(getattr(mod_decision, "reply", "")).strip(), render, "ok_moderated", False, latency_ms

    # Live-hours contract: if the market snapshot isn't present, reply with best-effort
    # futures ingest posture (no guessing, explicit confidence + stand-aside guidance).
    if ctx_enabled and not isinstance(ctx_market, dict):
        now = int(time_lib.time())
        hb_age_s: int | None = None
        hb_msg: str | None = None
        ingest_state: str | None = None
        ingest_reason: str | None = None
        futures_line: str | None = None
        confidence = "LOW"
        ops_note: str | None = None

        try:
            from services.futures.futures_context_line import compute_context
            from services.futures.futures_store import FuturesStore
            from services.observability.futures_ingest_health import classify_futures_ingest

            store = FuturesStore(r_ctx) if r_ctx is not None else FuturesStore()
            hb_ts, hb_msg = await asyncio.to_thread(store.get_heartbeat)
            status = await asyncio.to_thread(store.get_status)
            scores = await asyncio.to_thread(store.get_scores)

            if hb_ts is not None:
                hb_age_s = max(0, now - int(hb_ts))

            scores_present = scores is not None
            scores_age_s: int | None = None
            if scores is not None:
                try:
                    scores_age_s = max(0, now - int(scores.updated_utc))
                except Exception:
                    scores_age_s = None

            try:
                hb_max_age = int(os.getenv("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", "90") or "90")
            except Exception:
                hb_max_age = 90
            try:
                scores_max_age = int(os.getenv("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120") or "120")
            except Exception:
                scores_max_age = 120

            st_note = str((status or {}).get("note") or "").strip()

            ingest_state, ingest_reason = classify_futures_ingest(
                hb_age_s=hb_age_s,
                scores_age_s=scores_age_s,
                scores_present=bool(scores_present),
                note=st_note,
                hb_msg=hb_msg,
                hb_max_age=int(hb_max_age),
                scores_max_age=int(scores_max_age),
            )

            # Human-readable posture line from scores (if present).
            ctx = compute_context(scores, now_epoch=now, max_age_sec=int(scores_max_age))
            if ctx.state != "MISSING":
                bits: list[str] = []
                bits.append(f"Futures posture: {ctx.trend} / {ctx.mode}")
                if isinstance(ctx.divergence, (int, float)):
                    bits.append(f"Δ={float(ctx.divergence):.2f} ({ctx.divergence_level})")
                if isinstance(ctx.vol_mult, (int, float)):
                    bits.append(f"vol_mult={float(ctx.vol_mult):.2f}")
                if isinstance(ctx.age_sec, int):
                    bits.append(f"scores_age={int(ctx.age_sec)}s" + (" (aging)" if ctx.state != "OK" else ""))
                futures_line = " | ".join([b for b in bits if b]).strip()
            else:
                futures_line = "Futures posture: awaiting prints (scores missing)"

            # Deterministic confidence: never HIGH here.
            if isinstance(hb_age_s, int) and hb_age_s <= int(hb_max_age) and scores_present:
                # Even when scores are present, this is still a degraded view.
                confidence = "MED" if (scores_age_s is not None and scores_age_s <= int(scores_max_age)) else "LOW"
            else:
                confidence = "LOW"
        except Exception:
            # If futures ingest isn't reachable, still respond with an explicit
            # low-confidence stand-aside posture (no punts during live hours).
            futures_line = None
            hb_age_s = None
            hb_msg = None
            ingest_state = None
            ingest_reason = None
            confidence = "LOW"
            ops_note = "futures ingest unavailable"

        latency_ms = (time_lib.time() - started) * 1000.0
        text = build_market_best_effort_futures_only(
            futures_line=futures_line,
            hb_age_sec=hb_age_s,
            hb_msg=hb_msg,
            ingest_state=ingest_state,
            ingest_reason=ingest_reason,
            confidence=confidence,
            ops_note=ops_note,
        )
        return text, RenderedPost(text="", agent_payload={}), "ok_degraded", False, latency_ms

    def _market_only_reply(*, mkt: dict | None, spec: IntentSpec | None) -> str | None:
        if not isinstance(mkt, dict):
            return None
        fut = mkt.get("futures") if isinstance(mkt.get("futures"), dict) else {}
        bias = str(fut.get("bias") or "UNKNOWN").strip().upper() or "UNKNOWN"
        regime = str(fut.get("regime") or "UNKNOWN").strip().upper() or "UNKNOWN"
        hb_age = fut.get("hb_age_sec")
        hb_msg = str(fut.get("hb_msg") or "").strip()
        degraded = bool(fut.get("degraded"))

        news = mkt.get("news") if isinstance(mkt.get("news"), dict) else {}
        news_state = str(news.get("market_state") or "").strip().upper() or "UNKNOWN"
        title = str(news.get("market_last_title") or "").strip()

        lines = []
        if spec is not None and spec.intent == "FUTURES_OVERVIEW":
            lines.append("Futures (from market snapshot):")
        else:
            lines.append("Market context (from market snapshot):")

        lines.append(f"- Futures bias: {bias} | Regime: {regime}")
        if hb_age is not None:
            lines.append(f"- Futures heartbeat age: {int(hb_age)}s{' (degraded)' if degraded else ''}")
        if hb_msg:
            lines.append(f"- Futures status: {hb_msg}")
        lines.append(f"- News state: {news_state}" + (f" | Latest: {title}" if title else ""))

        # Optional nuance: if a symbol was mentioned for futures, acknowledge without requiring ctx:sym.
        if spec is not None and spec.intent == "FUTURES_OVERVIEW" and spec.symbol:
            lines.append(f"\nNote: {spec.symbol} often tracks ES directionally; this uses ES-based futures posture only.")

        return "\n".join(lines).strip()

    # Futures overview is market-only; never require /analyze.
    if spec is not None and spec.intent == "FUTURES_OVERVIEW":
        latency_ms = (time_lib.time() - started) * 1000.0
        return build_futures_overview(mkt=ctx_market), RenderedPost(text="", agent_payload={}), "ok", False, latency_ms

    # Greetings are lightweight and market-only; avoid dumping a full market context.
    if spec is not None and spec.intent == "GREETING":
        latency_ms = (time_lib.time() - started) * 1000.0
        return build_greeting(mkt=ctx_market), RenderedPost(text="", agent_payload={}), "ok", False, latency_ms

    # Market context is market-only; prefer a deterministic template.
    if spec is not None and spec.intent == "MARKET_CONTEXT":
        try:
            _, payload_mkt = await _run_analyze_for_symbol("SPY")
            tnt_state_mkt = build_tnt_state_from_analysis_payload(payload_mkt or {}).state
            enrich_tnt_state(tnt_state_mkt)
        except Exception:  # noqa: BLE001
            tnt_state_mkt = {}

        latency_ms = (time_lib.time() - started) * 1000.0
        cached = _get_cached_analyze("SPY")
        render = cached.render if cached is not None else RenderedPost(text="", agent_payload={})
        return build_market_context(mkt=ctx_market, tnt_state=tnt_state_mkt), render, "ok", False, latency_ms

    # Candidate requests are strictly driven by the canonical candidates snapshot.
    if spec is not None and spec.intent == "CANDIDATE_REQUEST":
        # Candidates are canonical in ctx:sym:{SYM}.candidates; merge into ctx_sym for rendering.
        merged_sym = ctx_sym
        try:
            from services.context.ctx_reader import load_ctx_candidates

            if r_ctx is not None:
                cand_ctx = load_ctx_candidates(r=r_ctx, symbol=sym)
            else:
                cand_ctx = None
            if isinstance(cand_ctx, dict):
                base = dict(ctx_sym) if isinstance(ctx_sym, dict) else {}
                base["candidates"] = cand_ctx
                merged_sym = base
        except Exception:  # noqa: BLE001
            merged_sym = ctx_sym

        msg = build_candidate_reply(symbol=sym, sym_ctx=merged_sym)
        latency_ms = (time_lib.time() - started) * 1000.0
        cached = _get_cached_analyze(sym)
        render = cached.render if cached is not None else RenderedPost(text="", agent_payload={})
        status = "ok" if isinstance(ctx_sym, dict) else "ok_standby"
        return msg, render, status, False, latency_ms

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

    # Attach canonical ctx to the analysis payload for downstream TNT_STATE injection.
    try:
        if isinstance(payload, dict):
            payload["canonical_ctx"] = {
                "enabled": bool(ctx_enabled),
                "missing": list(ctx_missing),
                "market": ctx_market if isinstance(ctx_market, dict) else None,
                "symbol": ctx_sym if isinstance(ctx_sym, dict) else None,
            }
    except Exception:  # noqa: BLE001
        pass

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

    # Deterministic snapshot for /ask.
    try:
        tnt_state_for_snapshot = build_tnt_state_from_analysis_payload(payload or {}).state
    except Exception:  # noqa: BLE001
        tnt_state_for_snapshot = {}

    # Extract confirm/confidence tokens (best-effort) from the analysis payload.
    confirm_tok = None
    confidence_tok = None
    try:
        meta2 = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
        sig2 = meta2.get("signal_payload") if isinstance(meta2.get("signal_payload"), Mapping) else {}
        confirm_tok = str(sig2.get("bias_confirm") or "").strip().upper() or None
        tnt2 = sig2.get("tnt") if isinstance(sig2.get("tnt"), Mapping) else {}
        conf_raw = tnt2.get("confidence")
        if isinstance(conf_raw, (int, float)):
            confidence_tok = float(conf_raw)
    except Exception:  # noqa: BLE001
        confirm_tok = None
        confidence_tok = None

    # If intent requires a symbol but we didn't validate one, fall back to Market Context.
    if spec is not None and spec.intent in {"SYMBOL_CONTEXT", "CANDIDATE_REQUEST"} and not spec.symbol:
        try:
            _, payload_mkt = await _run_analyze_for_symbol("SPY")
            tnt_state_mkt = build_tnt_state_from_analysis_payload(payload_mkt or {}).state
            enrich_tnt_state(tnt_state_mkt)
        except Exception:  # noqa: BLE001
            tnt_state_mkt = {}
        latency_ms = (time_lib.time() - started) * 1000.0
        cached = _get_cached_analyze("SPY")
        render = cached.render if cached is not None else RenderedPost(text="", agent_payload={})
        return build_market_context(mkt=ctx_market, tnt_state=tnt_state_mkt), render, "ok", False, latency_ms

    # If canonical ctx is missing, do not guess. Prefer a symbol-stale template when only the symbol snapshot is missing.
    if ctx_missing:
        missing_set = {str(m).strip() for m in (ctx_missing or []) if str(m).strip()}
        has_sym_missing = any(str(m).startswith("ctx:sym:") for m in missing_set)
        if isinstance(ctx_market, dict) and has_sym_missing:
            msg = build_symbol_context(symbol=sym, mkt=ctx_market, sym_ctx=None, tnt_state={})
        else:
            msg = build_market_snapshot_missing()
        latency_ms = (time_lib.time() - started) * 1000.0
        return msg, render, "ok_standby", cache_hit, latency_ms

    # Symbol-specific preplanned answers that depend on TNT_STATE.
    if _is_key_levels_question(question):
        try:
            preplanned = _answer_key_levels_question(sym, tnt_state_for_snapshot)
        except Exception:  # noqa: BLE001
            preplanned = None
        if preplanned:
            latency_ms = (time_lib.time() - started) * 1000.0
            return preplanned, render, "ok", cache_hit, latency_ms

    # Options chain microstructure (provider-specific). Cache aggressively.
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

                # Publish lightweight OI walls for other features (best-effort, non-blocking).
                try:
                    from technicals.oi_walls import set_oi_walls

                    metrics = summary.get("metrics") if isinstance(summary, dict) else None
                    if isinstance(metrics, dict):
                        cw = metrics.get("call_wall") if isinstance(metrics.get("call_wall"), dict) else {}
                        pw = metrics.get("put_wall") if isinstance(metrics.get("put_wall"), dict) else {}

                        ttl_s = _env_int("OI_WALLS_TTL_SEC", 1800)
                        set_oi_walls(
                            sym,
                            call_wall=_coerce_float(cw.get("strike")),
                            put_wall=_coerce_float(pw.get("strike")),
                            ts_et=str(summary.get("asof_et") or "").strip() or None,
                            px=_coerce_float(summary.get("underlying_price")),
                            call_oi=_coerce_float(cw.get("open_interest")),
                            put_oi=_coerce_float(pw.get("open_interest")),
                            expiry=str(summary.get("expiration") or "").strip() or None,
                            ttl_s=int(ttl_s),
                        )
                except Exception:  # noqa: BLE001
                    pass

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

    # Locked phrasing spec: Symbol Context uses a deterministic template (no freeform coach output).
    if spec is not None and spec.intent == "SYMBOL_CONTEXT":
        latency_ms = (time_lib.time() - started) * 1000.0
        if ctx_sym is None:
            msg = build_symbol_context(symbol=sym, mkt=ctx_market, sym_ctx=None, tnt_state=tnt_state_for_snapshot or {})
            return msg, render, "ok_standby", cache_hit, latency_ms
        msg = build_symbol_context(symbol=sym, mkt=ctx_market, sym_ctx=ctx_sym, tnt_state=tnt_state_for_snapshot or {})
        return msg, render, "ok", cache_hit, latency_ms

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
        coach_text, ok = normalize_coach_output(raw_reply)
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

        # Final lock: enforce hard consistency rules (no contradictions, no entries when blocked).
        try:
            tok = tokens_from_tnt_state(symbol=sym, tnt_state=tnt_state_for_snapshot or {}, confirm=confirm_tok, confidence=confidence_tok)
            coach_text = apply_consistency_guard(coach_text, tokens=tok)
        except Exception:  # noqa: BLE001
            pass

        # Candidate truth lock: if user is asking for a structure/trade, only speak from ctx candidates.
        try:
            cand = (ctx_sym or {}).get("candidates") if isinstance(ctx_sym, dict) else None
            if isinstance(cand, dict):
                coach_text = apply_candidate_truth_guard(coach_text, candidates=cand, question=question)
        except Exception:  # noqa: BLE001
            pass
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

    def _inject_into_state(envelope_text: str, inserts: list[str]) -> str:
        if not inserts:
            return envelope_text
        lines = (envelope_text or "").splitlines()
        if not lines:
            return envelope_text
        if lines[0].strip() != "A) State":
            return envelope_text

        expanded: list[str] = []
        for block in inserts:
            for ln in (block or "").splitlines():
                if ln.strip():
                    expanded.append(ln.rstrip())
        if not expanded:
            return envelope_text

        rest = lines[1:]
        # Avoid stacking extra blank lines.
        while rest and not rest[0].strip():
            rest = rest[1:]

        return "\n".join(["A) State", *expanded, "", *rest]).strip()

    inserts: list[str] = []

    # Add deterministic market snapshot inside A) State when we are not in stand-down.
    try:
        if status == "ok" and isinstance(tnt_state_for_snapshot, dict) and tnt_state_for_snapshot:
            snapshot_line = _build_coach_data_snapshot_line(tnt_state_for_snapshot)
            # Only include prices/levels when the snapshot says health is OK.
            if snapshot_line and "health OK" in snapshot_line:
                inserts.append(snapshot_line)
    except Exception:  # noqa: BLE001
        pass

    # Add Numeric Bias Pack inside A) State (best-effort).
    try:
        from delivery.numeric_bias_pack import format_numeric_bias_pack_from_analysis_payload

        pack = format_numeric_bias_pack_from_analysis_payload(analysis_payload=payload or {})
        if pack:
            inserts.append(pack)
    except Exception:  # noqa: BLE001
        pass

    coach_text = _inject_into_state(coach_text, inserts)

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


def _earnings_provider_runtime() -> str:
    """Return the effective earnings provider from the current environment."""

    return (os.getenv("EARNINGS_PROVIDER", "earningsapi") or "earningsapi").strip().lower()


def _earnings_allow_fallback_keys() -> bool:
    """Whether earnings may reuse POLYGON/MASSIVE API keys.

    - In live runtime: default ON (1) to satisfy ops expectations.
    - Under pytest: default OFF (0) to keep tests deterministic.
    """

    default = "0" if "pytest" in sys.modules else "1"
    return _env_bool("EARNINGS_ALLOW_FALLBACK_KEYS", default)


def _expand_env_reference(value: str) -> tuple[str, str | None]:
    """Expand simple env references like `${POLYGON_API_KEY}` or `$POLYGON_API_KEY`.

    Returns (expanded_value, referenced_var_name). If no expansion happened,
    referenced_var_name is None.
    """

    v = (value or "").strip()
    if not v:
        return "", None

    m = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", v)
    if not m:
        m = re.fullmatch(r"\$([A-Z0-9_]+)", v)
    if not m:
        return v, None

    var_name = m.group(1)
    expanded = (os.getenv(var_name) or "").strip()
    return expanded, var_name


def _resolve_earnings_api_key() -> str:
    """Resolve the earnings API key.

    Ops policy: reuse existing Polygon/Massive API keys if a dedicated
    EARNINGS_API_KEY is not configured.
    """

    primary = (os.getenv("EARNINGS_API_KEY") or "").strip()
    if primary:
        expanded_primary, _ = _expand_env_reference(primary)
        if expanded_primary:
            return expanded_primary

    provider = _earnings_provider_runtime()

    # Massive/Benzinga earnings uses Massive key universe.
    if provider in {"massive_benzinga", "massive-benzinga", "massive"}:
        return _resolve_massive_api_key()

    # Provider-specific policy: earningsapi.com requires its own API key.
    # Reusing Polygon/Massive keys yields a hard 401/403 and masks misconfig.
    if provider == "earningsapi":
        # Ops override: allow explicitly opting into fallback keys if the
        # deployment truly uses a shared key.
        if not _env_bool("EARNINGS_EARNINGSAPI_ALLOW_FALLBACK_KEYS", "0"):
            return ""

    if not _earnings_allow_fallback_keys():
        return ""

    return (os.getenv("POLYGON_API_KEY") or "").strip() or (os.getenv("MASSIVE_API_KEY") or "").strip()


def _resolve_earnings_api_key_source() -> str:
    raw_primary = (os.getenv("EARNINGS_API_KEY") or "").strip()
    if raw_primary:
        expanded_primary, _ = _expand_env_reference(raw_primary)
        if expanded_primary:
            return "EARNINGS_API_KEY"

    provider = _earnings_provider_runtime()
    if provider in {"massive_benzinga", "massive-benzinga", "massive"}:
        return _resolve_massive_api_key_source()

    if provider == "earningsapi":
        if not _env_bool("EARNINGS_EARNINGSAPI_ALLOW_FALLBACK_KEYS", "0"):
            return "missing"
    if not _earnings_allow_fallback_keys():
        return "missing"
    if (os.getenv("POLYGON_API_KEY") or "").strip():
        return "POLYGON_API_KEY"
    if (os.getenv("MASSIVE_API_KEY") or "").strip():
        return "MASSIVE_API_KEY"
    return "missing"


EARNINGS_API_KEY = _resolve_earnings_api_key()
EARNINGS_WATCHLIST = [
    s.strip().upper()
    for s in os.getenv(
        "EARNINGS_WATCHLIST",
        "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA",
    ).split(",")
    if s.strip()
]

MACRO_EVENTS_FILE = os.getenv("TNT_MACRO_EVENTS_FILE") or os.getenv("MACRO_EVENTS_FILE") or "data/macro_events.csv"

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
    "AM",
    "THE",
    "WHAT",
    "WHATS",
    "WHEN",
    "WHERE",
    "WHY",
    "HOW",
    "SHOULD",
    "WOULD",
    "WILL",
    "CAN",
    "COULD",
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
    "DOES",
    "DID",
    "BE",
    "HAS",
    "HAVE",
    "ER",
    "TODAY",
    "NEXT",
    "WEEK",
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

    # Indicator keywords that often appear in questions.
    # Prevents `rsi mstr` from parsing RSI as the symbol.
    "RSI",
    "MACD",
    "SMA",
    "EMA",
    "VWAP",
    "TA",
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

    # Common indicator keywords that should not be treated as symbols.
    "RSI",
    "MACD",
    "SMA",
    "EMA",
    "VWAP",
    "TA",
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
    raise_on_block: bool = False,
):

    from services.discord_files import files_from_name_bytes

    if not QUALITY_GATE_ENABLED:
        if files:
            discord_files = files_from_name_bytes(files)
            return await channel.send(text, files=discord_files)
        return await channel.send(text)

    if kind == "analysis":
        ok, reason = validate_analysis_message(text, label)
    else:
        ok, reason = True, "ok"

    if ok:
        if files:
            discord_files = files_from_name_bytes(files)
            return await channel.send(text, files=discord_files)
        return await channel.send(text)

    if QUALITY_GATE_LOG_FAILS:
        print(f"[GATE] blocked {kind} {symbol}: {reason}")

    if raise_on_block:
        raise QualityGateBlockedError(kind=kind, label=label, symbol=symbol, reason=reason)

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
            discord_files = files_from_name_bytes(files)
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

    queue_mode_l = (queue_mode or "raise").strip().lower()
    # Only start the background burst queue loop if we might actually enqueue.
    if queue_mode_l not in {"noqueue", "throttle"}:
        start_burst_queue_loop()
    render = RenderedPost(text=text or "", agent_payload={})

    if requester_id is None:
        # No requester => treat as non-user-scoped send.
        return await safe_send(send_target, text, kind=kind, symbol="", pivots=None, analysis_mode="coach", output_mode="coach", label=label)

    if not _RATE_QUEUE_ENABLED and queue_mode_l == "raise":
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
            if queue_mode_l in {"noqueue"}:
                return None
            if queue_mode_l in {"throttle"}:
                raise PublishThrottledError(wait_seconds=wait_s, key=key)

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
                if queue_mode_l == "raise":
                    raise PublishQueuedError(wait_seconds=wait_s, due_ts=due_ts, key=key)
                return None
            if queue_mode_l == "raise":
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
    def _safe_json_load(raw: Any) -> dict | None:
        if raw is None:
            return None
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            s = str(raw)
        except Exception:
            return None
        s = s.strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def _format_candidate_one_line(cand: Mapping[str, Any]) -> str:
        side = str(cand.get("side") or "").strip().upper()
        strategy = str(cand.get("strategy") or cand.get("name") or "").strip()
        dte = cand.get("dte")
        dte_text = ""
        try:
            if dte is not None:
                dte_text = f" ({int(dte)}DTE)"
        except Exception:
            dte_text = ""
        parts = [p for p in [side, strategy] if p]
        core = " ".join(parts).strip() or "(unlabeled)"
        return f"{core}{dte_text}"

    def _append_candidates_blurb(base_lines: list[str]) -> list[str]:
        # Keep tests deterministic (no Redis dependency).
        if "pytest" in sys.modules:
            return base_lines
        if not _env_bool("AUTOPOST_CANDIDATES_MESSAGING", "0"):
            return base_lines
        try:
            from services.context.ctx_mode import ctx_enabled as _ctx_enabled
        except Exception:
            _ctx_enabled = None
        if _ctx_enabled is not None:
            if not _ctx_enabled():
                return
        else:
            if (os.getenv("CTX_SNAPSHOT_ENABLED", "0") or "0").strip() != "1":
                return
            return base_lines
        if any("AI Trade Candidates" in line for line in base_lines):
            return base_lines
        if not symbols:
            return base_lines

        sym = (symbols[0] or "").strip().upper()
        if not sym:
            return base_lines

        try:
            r_ctx = redis_client_for_ctx()
            ctx_sym = _safe_json_load(r_ctx.get(f"ctx:sym:{sym}"))
        except Exception:
            ctx_sym = None

        if not isinstance(ctx_sym, dict):
            return base_lines

        candidates = ctx_sym.get("candidates")
        if not isinstance(candidates, dict):
            return base_lines

        status = str(candidates.get("status") or "UNKNOWN").strip().upper()
        top = candidates.get("top")
        top_line = ""
        if isinstance(top, dict):
            top_line = _format_candidate_one_line(top)

        out = list(base_lines)
        out.append("")
        out.append("🧠 AI Trade Candidates")
        if status == "APPROVED" and top_line:
            out.append(f"• Eligible option structure candidate: {top_line}")
            out.append(f"• {AI_TRADE_CANDIDATES_BOUNDARY_SENTENCE}")
        elif status == "NONE":
            out.append(f"• {AI_TRADE_CANDIDATES_NO_CANDIDATE_SENTENCE}")
        else:
            out.append(f"• Candidates unavailable ({status}). No structure candidate is surfaced.")
        return out

    out_lines = _append_candidates_blurb(list(lines))
    text = "\n".join(out_lines)
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
# --- Earnings calendar health (ops /config_health) ---
# Best-effort: stored in-process only (resets on restart).
_EARNINGS_CALENDAR_LAST_FETCH: dict[str, object] = {
    "ts_utc": None,
    "date_et": None,
    "http_status": None,
    "rows": None,
    "error": None,
    "provider": None,
}


def earnings_calendar_last_fetch() -> dict[str, object]:
    """Return the last earnings calendar fetch status (best-effort)."""
    return dict(_EARNINGS_CALENDAR_LAST_FETCH)


def _set_earnings_calendar_last_fetch(
    *,
    date_et: str,
    provider: str,
    http_status: int | None,
    rows: int | None,
    error: str | None,
) -> None:
    try:
        _EARNINGS_CALENDAR_LAST_FETCH.update(
            {
                "ts_utc": _now_utc().isoformat(),
                "date_et": str(date_et or ""),
                "http_status": http_status,
                "rows": rows,
                "error": error,
                "provider": str(provider or ""),
            }
        )
    except Exception:
        # Never fail the caller.
        pass


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() in {"na", "n/a", "nan", "none", "null", "-"}:
        return None
    s = s.replace(",", "").replace("$", "")
    try:
        return float(s)
    except Exception:
        return None


def _coerce_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return bool(int(value))
        except Exception:
            return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in {"true", "1", "yes", "y", "confirmed"}:
        return True
    if s in {"false", "0", "no", "n", "projected", "estimated"}:
        return False
    return None


def _pick_first(entry: dict, keys: list[str]) -> object | None:
    for k in keys:
        if k not in entry:
            continue
        v = entry.get(k)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _earnings_row_score(row: dict) -> int:
    score = 0
    for k in ("eps_est", "rev_est", "eps_actual", "rev_actual"):
        if row.get(k) is not None:
            score += 2
    if (row.get("company") or "").strip():
        score += 1
    if row.get("confirmed") is True:
        score += 1
    return score


def fetch_earnings_for_date(date_et: str) -> list[dict]:
    """Fetch earnings calendar rows for a given ET date."""
    provider = _earnings_provider_runtime()
    api_key = _resolve_earnings_api_key()
    watchlist = [s.strip().upper() for s in (os.getenv("EARNINGS_WATCHLIST") or "").split(",") if s.strip()]
    # Default to auto so we don't silently render empty earnings posts if the
    # watchlist is empty or doesn't match the provider's symbols.
    watchlist_mode = (os.getenv("EARNINGS_WATCHLIST_MODE") or "auto").strip().lower()
    if watchlist_mode not in {"watchlist", "all", "auto"}:
        watchlist_mode = "watchlist"

    if not api_key:
        _set_earnings_calendar_last_fetch(
            date_et=date_et,
            provider=provider,
            http_status=None,
            rows=0,
            error="missing_api_key",
        )
        return []

    if provider in {"massive_benzinga", "massive-benzinga", "massive"}:
        base = (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").strip().rstrip("/")
        url = f"{base}/benzinga/v1/earnings"
        key = str(api_key or "").strip()

        try:
            retries = int(os.getenv("EARNINGS_CALENDAR_HTTP_RETRIES", "2") or "2")
        except Exception:
            retries = 2
        retries = max(0, min(6, int(retries)))

        try:
            timeout_s = float(os.getenv("EARNINGS_CALENDAR_TIMEOUT_S", "15") or "15")
        except Exception:
            timeout_s = 15.0
        timeout_s = max(3.0, min(60.0, float(timeout_s)))

        def _get_with_retry(params: dict) -> "requests.Response":
            last_exc: Exception | None = None
            for attempt in range(int(retries) + 1):
                try:
                    resp0 = requests.get(url, params=params, timeout=timeout_s)
                    status0 = int(resp0.status_code)
                    if status0 == 429 or 500 <= status0 <= 599:
                        # Bounded backoff with a little jitter.
                        if attempt < int(retries):
                            backoff = min(10.0, (1.25 ** attempt) * 1.5)
                            try:
                                backoff = backoff * (0.85 + 0.3 * random.random())
                            except Exception:
                                pass
                            time_lib.sleep(float(backoff))
                            continue
                    return resp0
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt < int(retries):
                        time_lib.sleep(min(6.0, 0.75 * (attempt + 1)))
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("earnings calendar fetch failed")

        def _infer_when_from_time_utc(ts_utc_s: str | None) -> str:
            s = (ts_utc_s or "").strip()
            if not s:
                return "TAS"
            ss = s.lower()
            if ss in {"bmo", "pre", "premarket", "before", "before_open", "before open"}:
                return "BMO"
            if ss in {"amc", "post", "postmarket", "after", "after_close", "after close"}:
                return "AMC"
            # Benzinga earnings often provides time as HH:MM:SS UTC.
            try:
                hh = int(s.split(":", 2)[0])
                mm = int(s.split(":", 2)[1])
                # Treat the provided UTC time on the given date.
                dt_utc = datetime.fromisoformat(f"{date_et}T{hh:02d}:{mm:02d}:00+00:00")
                dt_et = dt_utc.astimezone(ET)
                if dt_et.hour < 12:
                    return "BMO"
                if dt_et.hour >= 16:
                    return "AMC"
                return "TAS"
            except Exception:
                return "TAS"

        try:
            # Upstreams differ on query params; try a small ordered set and stop
            # once we observe a non-empty payload.
            base_params = {
                "apiKey": key,
                "limit": 5000,
                "sort": "last_updated.desc",
            }

            date_variants: list[dict[str, object]] = [
                {"date": date_et},
                {"date_from": date_et, "date_to": date_et},
                {"from": date_et, "to": date_et},
                {"start_date": date_et, "end_date": date_et},
                {"start": date_et, "end": date_et},
                {"dateFrom": date_et, "dateTo": date_et},
                {"startDate": date_et, "endDate": date_et},
            ]

            want_symbols: set[str] = set(watchlist) if watchlist else set()
            sym_variants: list[dict[str, object]] = [{}]
            if want_symbols and watchlist_mode in {"watchlist", "auto"}:
                joined = ",".join(sorted(want_symbols))
                sym_variants = [
                    {"tickers": joined},
                    {"symbols": joined},
                    {"ticker": joined},
                    {"symbol": joined},
                    {},
                ]

            items: list[object] = []
            last_status: int | None = None
            last_bytes: int | None = None
            for sp in sym_variants:
                for dp in date_variants:
                    resp = _get_with_retry({**base_params, **sp, **dp})
                    last_status = int(resp.status_code)
                    last_bytes = len(resp.text)
                    if last_status != 200:
                        continue
                    payload = resp.json()
                    raw = payload.get("results") if isinstance(payload, dict) else []
                    if not isinstance(raw, list):
                        raw = []
                    items = raw
                    if items:
                        break
                if items:
                    break

            print(
                f"[EARNINGS] provider=massive_benzinga date={date_et} status={last_status} bytes={last_bytes}"
            )
            if int(last_status or 0) != 200:
                _set_earnings_calendar_last_fetch(
                    date_et=date_et,
                    provider=provider,
                    http_status=int(last_status or 0) if last_status is not None else None,
                    rows=0,
                    error="http_error",
                )
                return []
        except Exception:  # noqa: BLE001
            _set_earnings_calendar_last_fetch(
                date_et=date_et,
                provider=provider,
                http_status=None,
                rows=0,
                error="exception",
            )
            return []

        def _build_rows(*, restrict_to: set[str] | None) -> list[dict]:
            best_by_key: dict[tuple[str, str], dict] = {}
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                sym = (entry.get("ticker") or entry.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                if restrict_to is not None and sym not in restrict_to:
                    continue
                when = _infer_when_from_time_utc(str(entry.get("time") or ""))
                k = (sym, when)

                company = str(
                    _pick_first(
                        entry,
                        [
                            "company",
                            "company_name",
                            "companyName",
                            "name",
                            "short_name",
                            "shortName",
                        ],
                    )
                    or ""
                ).strip()

                eps_est = _coerce_float(
                    _pick_first(
                        entry,
                        [
                            "estimated_eps",
                            "estimatedEps",
                            "eps_est",
                            "epsEstimate",
                            "eps_estimate",
                            "epsEstimated",
                            "eps_mean_estimate",
                        ],
                    )
                )
                rev_est = _coerce_float(
                    _pick_first(
                        entry,
                        [
                            "estimated_revenue",
                            "estimatedRevenue",
                            "revenue_est",
                            "revenueEstimate",
                            "revenue_estimate",
                            "rev_est",
                            "revEstimate",
                            "revenue_mean_estimate",
                        ],
                    )
                )
                eps_actual = _coerce_float(
                    _pick_first(
                        entry,
                        [
                            "eps",
                            "actual_eps",
                            "actualEps",
                            "eps_actual",
                            "reported_eps",
                            "reportedEps",
                        ],
                    )
                )
                rev_actual = _coerce_float(
                    _pick_first(
                        entry,
                        [
                            "revenue",
                            "actual_revenue",
                            "actualRevenue",
                            "revenue_actual",
                            "reported_revenue",
                            "reportedRevenue",
                        ],
                    )
                )
                confirmed = _coerce_bool(
                    _pick_first(
                        entry,
                        [
                            "confirmed",
                            "is_confirmed",
                            "isConfirmed",
                            "time_confirmed",
                            "timeConfirmed",
                            "date_confirmed",
                            "dateConfirmed",
                        ],
                    )
                )

                row = {
                    "symbol": sym,
                    "when": when,
                    "company": company,
                    "eps_est": eps_est,
                    "rev_est": rev_est,
                    "eps_actual": eps_actual,
                    "rev_actual": rev_actual,
                    "confirmed": confirmed,
                }

                prev = best_by_key.get(k)
                if prev is None or _earnings_row_score(row) > _earnings_row_score(prev):
                    best_by_key[k] = row

            when_order = {"BMO": 0, "AMC": 1, "TAS": 2}
            return sorted(
                best_by_key.values(),
                key=lambda r: (
                    str(r.get("symbol") or ""),
                    when_order.get(str(r.get("when") or ""), 9),
                ),
            )

        watch_set = set(watchlist) if watchlist else set()
        rows_watchlist = _build_rows(restrict_to=watch_set) if watch_set else []
        rows_all = _build_rows(restrict_to=None)

        if watchlist_mode == "all":
            rows = rows_all
        elif watchlist_mode == "auto":
            rows = rows_watchlist if rows_watchlist else rows_all
        else:
            rows = rows_watchlist

        _set_earnings_calendar_last_fetch(
            date_et=date_et,
            provider=provider,
            http_status=200,
            rows=len(rows),
            error=None,
        )
        return rows

    if provider != "earningsapi":
        _set_earnings_calendar_last_fetch(
            date_et=date_et,
            provider=provider,
            http_status=None,
            rows=0,
            error="provider_not_supported",
        )
        return []

    url = f"https://api.earningsapi.com/v1/calendar/{date_et}"
    try:
        resp = requests.get(url, params={"apikey": str(api_key or "").strip()}, timeout=15)
        print(f"[EARNINGS] provider=earningsapi date={date_et} status={resp.status_code} bytes={len(resp.text)}")
        if resp.status_code != 200:
            _set_earnings_calendar_last_fetch(
                date_et=date_et,
                provider=provider,
                http_status=int(resp.status_code),
                rows=0,
                error="http_error",
            )
            return []
        data = resp.json()
    except Exception:  # noqa: BLE001
        _set_earnings_calendar_last_fetch(
            date_et=date_et,
            provider=provider,
            http_status=None,
            rows=0,
            error="exception",
        )
        return []
    rows = []
    for bucket, label in [("pre", "BMO"), ("after", "AMC"), ("notSupplied", "TAS")]:
        items = data.get(bucket) or []
        for entry in items:
            sym = (entry.get("symbol") or entry.get("ticker") or "").upper()
            if not sym:
                continue
            if watchlist and sym not in watchlist:
                continue
            rows.append({"symbol": sym, "when": label})

    _set_earnings_calendar_last_fetch(
        date_et=date_et,
        provider=provider,
        http_status=200,
        rows=len(rows),
        error=None,
    )
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

    # Earnings: always render a section (hard fallback, never silent).
    try:
        earn = fetch_earnings_for_date(target_date_et)
    except Exception:  # noqa: BLE001
        earn = []

    provider = _earnings_provider_runtime()
    earnings_key_present = bool(_resolve_earnings_api_key())
    watchlist = [
        s.strip().upper()
        for s in (os.getenv("EARNINGS_WATCHLIST") or "").split(",")
        if s.strip()
    ]

    # Best-effort: use the in-process last-fetch telemetry to label data health.
    # This avoids misreporting "no earnings" when the upstream request failed.
    last_fetch = earnings_calendar_last_fetch()
    last_fetch_matches = str(last_fetch.get("date_et") or "") == str(target_date_et or "")
    last_http_status = last_fetch.get("http_status") if last_fetch_matches else None
    last_error = str(last_fetch.get("error") or "") if last_fetch_matches else ""
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


def build_tomorrow_earnings_macro_render(
    *,
    target_date_et: Optional[str] = None,
    now_et: Optional[datetime] = None,
) -> RenderedPost:
    """Daily post: tomorrow's earnings + macro, with hard fallbacks.

    Safety: never silently returns an empty post; always renders explicit
    "(none listed)" / "missing API key" messaging.
    """

    now_et = now_et or _now_et()
    if target_date_et is None:
        # Tomorrow's next weekday (holidays not detected).
        target_d = next_weekday(now_et.date() + timedelta(days=1))
        target_date_et = target_d.strftime("%Y-%m-%d")

    macro_file_present = os.path.exists(MACRO_EVENTS_FILE)
    macro = load_macro_events_for_date(target_date_et)

    macro_age_min: Optional[float] = None
    macro_mtime_utc: Optional[str] = None
    if macro_file_present:
        try:
            mtime = float(os.path.getmtime(MACRO_EVENTS_FILE))
            now_ts = float(_now_utc().timestamp())
            macro_age_min = max(0.0, (now_ts - mtime) / 60.0)
            macro_mtime_utc = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except Exception:
            macro_age_min = None
            macro_mtime_utc = None

    try:
        earn = fetch_earnings_for_date(target_date_et)
    except Exception:  # noqa: BLE001
        earn = []

    provider = _earnings_provider_runtime()
    # Under pytest, tests monkeypatch EARNINGS_API_KEY for determinism.
    # In live runtime, resolve from environment (supports provider-specific key policy).
    if "pytest" in sys.modules:
        earnings_key_present = bool(EARNINGS_API_KEY)
    else:
        earnings_key_present = bool(_resolve_earnings_api_key())
    watchlist = [
        s.strip().upper()
        for s in (os.getenv("EARNINGS_WATCHLIST") or "").split(",")
        if s.strip()
    ]

    # Best-effort: use the in-process last-fetch telemetry to label data health.
    # This avoids misreporting "no earnings" when the upstream request failed.
    last_fetch = earnings_calendar_last_fetch()
    last_fetch_matches = str(last_fetch.get("date_et") or "") == str(target_date_et or "")
    last_http_status = last_fetch.get("http_status") if last_fetch_matches else None
    last_error = str(last_fetch.get("error") or "") if last_fetch_matches else ""

    updated_utc = _now_utc().replace(tzinfo=timezone.utc)

    updated_et = now_et.astimezone(ET)
    updated_et_stamp = updated_et.strftime("%Y-%m-%d %H:%M ET")

    lines: list[str] = []
    lines.append("📅 Tomorrow’s Earnings + Macro")
    lines.append(f"🕒 Expected Tomorrow • For: {target_date_et} (ET)")

    banner = macro_risk_banner(macro, label="High-impact macro tomorrow")
    if banner:
        lines.append(banner)
        lines.append("⚠️ Volatility likely outside regular earnings flows")

    lines.append("")
    lines.append("🌐 Macro Events (ET)")
    if macro:
        for e in macro[:12]:
            t = (e.get("time_et") or "").strip()
            title = (e.get("title") or "").strip()
            impact = (e.get("impact") or "").strip()
            body = f"{impact_icon(impact)} {title}".strip()
            if not body:
                body = "(untitled event)"
            if t:
                lines.append(f"• {t} — {body}")
            else:
                lines.append(f"• {body}")
    else:
        if not macro_file_present:
            lines.append(f"• Macro calendar missing: {MACRO_EVENTS_FILE}")
        else:
            lines.append("• No scheduled high-impact macro events")

    lines.append("")
    lines.append("💼 Earnings")
    if earn:
        bmo_rows = sorted([x for x in earn if x.get("when") == "BMO"], key=lambda r: str(r.get("symbol") or ""))
        amc_rows = sorted([x for x in earn if x.get("when") == "AMC"], key=lambda r: str(r.get("symbol") or ""))
        tas_rows = sorted([x for x in earn if x.get("when") == "TAS"], key=lambda r: str(r.get("symbol") or ""))

        if bmo_rows:
            lines.append(f"• BMO: {fmt_earnings_expected_inline(bmo_rows, limit=12)}")
        if amc_rows:
            lines.append(f"• AMC: {fmt_earnings_expected_inline(amc_rows, limit=12)}")
        if tas_rows:
            lines.append(f"• Time not supplied: {fmt_earnings_expected_inline(tas_rows, limit=12)}")
        if not (bmo_rows or amc_rows or tas_rows):
            lines.append("• None found (watchlist empty)")
    else:
        if not earnings_key_present:
            lines.append("• Earnings disabled: missing API key")
        elif last_error == "provider_not_supported":
            lines.append(f"• Earnings provider not supported: {provider}")
        elif last_error == "http_error" and last_http_status is not None:
            lines.append(f"• Earnings calendar fetch failed (http {int(last_http_status)})")
        elif last_error == "exception":
            lines.append("• Earnings calendar fetch failed (exception)")
        else:
            lines.append("• No notable earnings scheduled for tracked symbols")

    # Tier A: inline headlines (cache-only, no firehose).
    try:
        headlines_limit = max(3, min(5, _env_int("TNT_EARNINGS_HEADLINES_LIMIT", "4")))
    except Exception:
        headlines_limit = 4
    try:
        post_syms = sorted({str(x.get("symbol") or "").strip().upper() for x in (earn or []) if isinstance(x, dict)})
        headlines = _load_latest_headlines_for_symbols(symbols=post_syms, window_hours=48.0, limit=headlines_limit)
    except Exception:
        headlines = []

    now_utc = now_et.astimezone(timezone.utc)

    lines.append("")
    lines.append("Latest Headlines (48h)")
    if headlines:
        lines.extend(_format_headlines_compact(headlines, now_utc=now_utc, limit=headlines_limit))
    else:
        lines.append("• No material headlines impacting tracked names (48h)")

    lines.append("")
    if macro_age_min is None:
        macro_age = "n/a"
    elif macro_age_min >= 24 * 60:
        macro_age = "historical (>24h)"
    else:
        macro_age = f"{macro_age_min:.1f}m"

    lines.append(f"Updated: {updated_et_stamp}")

    if not earnings_key_present:
        earnings_age = "disabled"
    elif last_error == "http_error" and last_http_status is not None:
        earnings_age = f"http_{int(last_http_status)}"
    elif last_error == "provider_not_supported":
        earnings_age = "unsupported"
    elif last_error == "exception":
        earnings_age = "error"
    else:
        # Default to "live" for normal/unknown cases (incl. tests that stub fetch).
        earnings_age = "live"

    lines.append(f"Data age: macro={macro_age} | earnings={earnings_age}")
    lines.append("Context only • Not financial advice")

    section_data: Dict[str, Any] = {
        "target_date_et": target_date_et,
        "macro_events": macro,
        "earnings_rows": earn,
        "macro_calendar_file": MACRO_EVENTS_FILE,
        "macro_calendar_present": macro_file_present,
        "macro_calendar_mtime_utc": macro_mtime_utc,
        "macro_calendar_age_min": macro_age_min,
        "earnings_provider": provider,
        "earnings_last_fetch": last_fetch if last_fetch_matches else None,
        "earnings_watchlist": sorted(watchlist),
        "updated_utc": updated_utc.isoformat(),
    }

    extra_meta: Dict[str, Any] = {
        "target_date_et": target_date_et,
        "macro_count": len(macro),
        "earnings_count": len(earn),
        "earnings_enabled": bool(earnings_key_present),
        "macro_calendar_age_min": macro_age_min,
        "headlines_count": len(headlines) if isinstance(headlines, list) else 0,
        "source": str(provider or "").strip() or "unknown",
    }

    # Earnings/macro posts intentionally use symbols=[]; still emit a minimal
    # agent payload so autopost proof lines can report counts/source reliably.
    precomputed_agent_payload: Dict[str, Any] = {
        "meta": {
            "generated_at": now_et.astimezone(timezone.utc).isoformat(),
            "symbols": [],
            "post_type": "earnings_tomorrow",
            "data_quality": {"technical_state": "N/A"},
            **extra_meta,
        },
        "sections": section_data,
    }

    return _render_with_agent_context(
        lines,
        symbols=[],
        generated_at=now_et,
        post_type="earnings_tomorrow",
        extra_meta=extra_meta,
        sections=section_data,
        precomputed_agent_payload=precomputed_agent_payload,
    )


def _surprise_pct(actual: float | None, est: float | None) -> float | None:
    if actual is None or est is None:
        return None
    try:
        a = float(actual)
        e = float(est)
    except Exception:
        return None
    if e == 0:
        return None
    return (a - e) / abs(e) * 100.0


def _beat_tag(actual: float | None, est: float | None, tol: float = 1e-6) -> str | None:
    if actual is None or est is None:
        return None
    try:
        a = float(actual)
        e = float(est)
    except Exception:
        return None
    if abs(a - e) <= tol:
        return "INLINE"
    return "BEAT" if a > e else "MISS"


def build_earnings_results_today_render(
    *,
    target_date_et: Optional[str] = None,
    now_et: Optional[datetime] = None,
) -> RenderedPost:
    """Daily post: today's earnings results (actual vs estimates) with hard fallbacks."""

    now_et = now_et or _now_et()
    if target_date_et is None:
        target_date_et = now_et.strftime("%Y-%m-%d")

    provider = _earnings_provider_runtime()
    if "pytest" in sys.modules:
        earnings_key_present = bool(EARNINGS_API_KEY)
    else:
        earnings_key_present = bool(_resolve_earnings_api_key())
    updated_et_stamp = now_et.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")

    try:
        earn = fetch_earnings_for_date(target_date_et)
    except Exception:  # noqa: BLE001
        earn = []

    last_fetch = earnings_calendar_last_fetch()
    last_fetch_matches = str(last_fetch.get("date_et") or "") == str(target_date_et or "")
    last_http_status = last_fetch.get("http_status") if last_fetch_matches else None
    last_error = str(last_fetch.get("error") or "") if last_fetch_matches else ""

    def _has_any_actual(r: dict) -> bool:
        return r.get("eps_actual") is not None or r.get("rev_actual") is not None

    results = [r for r in (earn or []) if isinstance(r, dict) and _has_any_actual(r)]
    bmo_rows = sorted([x for x in results if x.get("when") == "BMO"], key=lambda r: str(r.get("symbol") or ""))
    amc_rows = sorted([x for x in results if x.get("when") == "AMC"], key=lambda r: str(r.get("symbol") or ""))
    tas_rows = sorted([x for x in results if x.get("when") == "TAS"], key=lambda r: str(r.get("symbol") or ""))

    why_moved_count = 0

    mode = (os.getenv("TNT_EARNINGS_RESULTS_MODE", "majors") or "majors").strip().lower()
    if mode not in {"majors", "all"}:
        mode = "majors"

    majors_env = os.getenv("TNT_EARNINGS_RESULTS_MAJORS", "AAPL,MSFT,TSLA,META,NVDA") or ""
    majors = {s.strip().upper() for s in majors_env.split(",") if s.strip()}
    try:
        majors_max = int(_env_int("TNT_EARNINGS_RESULTS_MAJORS_MAX", "4"))
    except Exception:
        majors_max = 4
    majors_max = max(1, min(6, majors_max))

    try:
        others_max = int(_env_int("TNT_EARNINGS_RESULTS_OTHERS_MAX", "8"))
    except Exception:
        others_max = 8
    others_max = max(6, min(12, others_max))

    results_by_sym: dict[str, dict] = {}
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        if sym:
            results_by_sym[sym] = r

    majors_present = [s for s in sorted(majors) if s in results_by_sym]
    majors_present = majors_present[:majors_max]

    lines: list[str] = []
    lines.append("📊 Earnings Results — Today (ET)")
    session_label = "Post-Market / BMO as applicable"
    if bmo_rows and not (amc_rows or tas_rows):
        session_label = "BMO"
    elif amc_rows and not (bmo_rows or tas_rows):
        session_label = "AMC"
    lines.append(f"Date: {target_date_et} • Session: {session_label}")
    lines.append("")
    lines.append("🧾 Results (Watchlist)")

    def _why_it_moved_line(sym: str) -> str | None:
        """Best-effort single-line catalyst from cached news (material-only)."""

        if not sym:
            return None
        try:
            window_h = float(os.getenv("TNT_EARNINGS_WHY_MOVED_WINDOW_HOURS", "48") or "48")
        except Exception:
            window_h = 48.0
        window_h = max(6.0, min(168.0, float(window_h)))

        try:
            hs = _load_latest_headlines_for_symbols(symbols=[sym], window_hours=window_h, limit=1)
        except Exception:
            hs = []
        if not hs:
            return None

        it = hs[0]
        tag = _headline_tag_compact(str(it.get("tag") or "NEWS"))
        title = str(it.get("headline") or "").strip()
        url = str(it.get("url") or "").strip()
        dt = it.get("published_utc")
        age = None
        if isinstance(dt, datetime):
            age = _time_ago_short(dt.astimezone(timezone.utc), now_et.astimezone(timezone.utc))
        if not title:
            return None

        prefix = "  ↳ 📰 Why it moved"
        if age:
            prefix += f" ({age})"
        url_clean = _clean_url_for_display(url) if url else ""
        if url_clean:
            return f"{prefix}: [{tag}] {title} <{url_clean}>"
        return f"{prefix}: [{tag}] {title}"

    def _fmt_row(r: dict) -> str:
        sym = str(r.get("symbol") or "").strip().upper()
        eps_a = r.get("eps_actual")
        eps_e = r.get("eps_est")
        rev_a = r.get("rev_actual")
        rev_e = r.get("rev_est")

        parts: list[str] = []
        if eps_a is not None:
            tag = _beat_tag(eps_a, eps_e)
            sp = _surprise_pct(eps_a, eps_e)
            if eps_e is not None and tag and sp is not None:
                parts.append(f"EPS {_fmt_eps(eps_a)} vs {_fmt_eps(eps_e)} ({sp:+.1f}% {tag})")
            elif eps_e is not None:
                parts.append(f"EPS {_fmt_eps(eps_a)} vs {_fmt_eps(eps_e)}")
            else:
                parts.append(f"EPS {_fmt_eps(eps_a)}")

        if rev_a is not None:
            tag = _beat_tag(rev_a, rev_e)
            sp = _surprise_pct(rev_a, rev_e)
            a_s = _fmt_money_compact(rev_a)
            e_s = _fmt_money_compact(rev_e) if rev_e is not None else None
            if e_s is not None and tag and sp is not None:
                parts.append(f"Rev {a_s} vs {e_s} ({sp:+.1f}% {tag})")
            elif e_s is not None:
                parts.append(f"Rev {a_s} vs {e_s}")
            else:
                parts.append(f"Rev {a_s}")

        if r.get("confirmed") is True:
            parts.append("confirmed")

        body = " | ".join([p for p in parts if p])
        return f"{sym}{earnings_tag(sym)} — {body}".strip()

    def _emit_bucket(label: str, bucket: list[dict]) -> None:
        nonlocal why_moved_count
        if not bucket:
            return
        lines.append(f"• {label}:")
        for r in bucket[:12]:
            row_line = f"  • {_fmt_row(r)}"
            lines.append(row_line)
            sym = str(r.get("symbol") or "").strip().upper()
            why = _why_it_moved_line(sym)
            if why:
                lines.append(why)
                why_moved_count += 1

    def _emit_majors_block() -> None:
        nonlocal why_moved_count
        if not majors_present:
            return

        lines.append("Major Movers")
        lines.append("(tight summary • reaction risk is context, not direction)")
        lines.append("")

        for sym in majors_present:
            r = results_by_sym.get(sym) or {}
            eps_a = r.get("eps_actual")
            eps_e = r.get("eps_est")
            rev_a = r.get("rev_actual")
            rev_e = r.get("rev_est")

            tag = _beat_tag(eps_a, eps_e)
            if tag is None:
                tag = _beat_tag(rev_a, rev_e)
            badge = "⚖️"
            if tag == "BEAT":
                badge = "⭐"
            elif tag == "MISS":
                badge = "⚠️"

            implied_pct = None
            try:
                cal = _load_earnings_cache_payload(sym)
                if isinstance(cal, dict) and cal.get("expected_move_pct") is not None:
                    implied_pct = float(cal.get("expected_move_pct"))
            except Exception:
                implied_pct = None

            base = _risk_bucket(implied_pct=implied_pct)
            if tag == "MISS":
                reaction = "High" if base in {"MODERATE", "HIGH"} else "Moderate"
            elif tag == "BEAT":
                reaction = "Elevated" if base in {"MODERATE", "HIGH"} else "Moderate"
            else:
                reaction = "Moderate" if base != "LOW" else "Low"

            implied_s = "n/a" if implied_pct is None else f"±{abs(float(implied_pct)):.1f}%"
            when = str(r.get("when") or "").strip().upper()
            when_s = when if when in {"BMO", "AMC", "TAS"} else None

            lines.append(f"{sym} {badge} {tag.title() if isinstance(tag, str) else 'Result'}")

            if eps_a is not None:
                sp = _surprise_pct(eps_a, eps_e)
                if eps_e is not None and sp is not None and isinstance(tag, str):
                    lines.append(f"EPS: {_fmt_eps(eps_a)} vs {_fmt_eps(eps_e)} ({sp:+.1f}% {tag})")
                elif eps_e is not None:
                    lines.append(f"EPS: {_fmt_eps(eps_a)} vs {_fmt_eps(eps_e)}")
                else:
                    lines.append(f"EPS: {_fmt_eps(eps_a)}")

            if rev_a is not None:
                a_s = _fmt_money_compact(rev_a)
                e_s = _fmt_money_compact(rev_e) if rev_e is not None else None
                sp = _surprise_pct(rev_a, rev_e)
                tag_r = _beat_tag(rev_a, rev_e)
                if e_s is not None and sp is not None and isinstance(tag_r, str):
                    lines.append(f"Revenue: {a_s} vs {e_s} ({sp:+.1f}% {tag_r})")
                elif e_s is not None:
                    lines.append(f"Revenue: {a_s} vs {e_s}")
                else:
                    lines.append(f"Revenue: {a_s}")

            if when_s:
                lines.append(f"Report Timing: {when_s}")
            if implied_s != "n/a":
                lines.append(f"Implied Move: {implied_s}")
            lines.append(f"Reaction Risk: {reaction}")

            why = _why_it_moved_line(sym)
            if why:
                lines.append(why)
                why_moved_count += 1

            lines.append("")

        other = max(0, len(results) - len(majors_present))
        if other and mode == "majors":
            lines.append(f"Other watchlist results: {other}")

        if mode != "all":
            return

        # All-mode: majors first, then a capped Others section.
        try:
            majors_set = set(majors_present)
        except Exception:
            majors_set = set()
        others = [
            r
            for r in (results or [])
            if isinstance(r, dict) and str(r.get("symbol") or "").strip().upper() not in majors_set
        ]
        others = sorted(others, key=lambda rr: str(rr.get("symbol") or ""))
        if not others:
            return

        lines.append("")
        lines.append("Others (watchlist)")

        shown = 0
        for r in others[:others_max]:
            try:
                lines.append(f"  • {_fmt_row(r)}")
            except Exception:
                continue
            sym = str(r.get("symbol") or "").strip().upper()
            why = _why_it_moved_line(sym)
            if why:
                lines.append(why)
                why_moved_count += 1
            shown += 1

        remaining = max(0, len(others) - shown)
        if remaining:
            lines.append(f"  +{remaining} more (use /earnings_results to list)")

    if majors_present:
        _emit_majors_block()
    else:
        _emit_bucket("BMO", bmo_rows)
        _emit_bucket("AMC", amc_rows)
        _emit_bucket("Time not supplied", tas_rows)

    if not results:
        if not earnings_key_present:
            lines.append("• Earnings disabled: missing API key")
        elif last_error == "provider_not_supported":
            lines.append(f"• Earnings provider not supported: {provider}")
        elif last_error == "http_error" and last_http_status is not None:
            lines.append(f"• Earnings calendar fetch failed (http {int(last_http_status)})")
        elif last_error == "exception":
            lines.append("• Earnings calendar fetch failed (exception)")
        else:
            lines.append("• No reported results yet for tracked symbols")

    # Add-on: Implied vs Realized (cache-only via cal:earnings:{SYM}).
    try:
        post_syms = sorted({str(x.get("symbol") or "").strip().upper() for x in (results or []) if isinstance(x, dict)})
        vol_rows = _load_volatility_reality_for_symbols(symbols=post_syms, limit=3)
    except Exception:
        vol_rows = []

    lines.append("")
    lines.append("📊 Volatility Reality Check")
    if vol_rows:
        for vr in vol_rows:
            sym = str(vr.get("symbol") or "").strip().upper()
            implied = vr.get("implied_pct")
            realized = vr.get("realized_pct")
            ratio = vr.get("ratio")
            verdict = str(vr.get("verdict") or "").strip().upper() or "UNKNOWN"

            implied_s = "n/a" if implied is None else f"±{float(implied):.1f}%"
            realized_s = "n/a" if realized is None else f"{float(realized):.1f}%"
            ratio_s = "n/a" if ratio is None else f"{float(ratio):.2f}x"
            lines.append(f"• {sym}{earnings_tag(sym)} — Implied {implied_s} • Realized {realized_s} • Ratio {ratio_s} • {verdict}")
    else:
        lines.append("Implied vs Realized: Pending")
        lines.append("Not enough cached data yet")
        lines.append("(requires expected_move_pct + realized move)")

    # Tier A: inline headlines (cache-only).
    try:
        headlines_limit = max(3, min(5, _env_int("TNT_EARNINGS_HEADLINES_LIMIT", "4")))
    except Exception:
        headlines_limit = 4
    try:
        post_syms = sorted({str(x.get("symbol") or "").strip().upper() for x in (results or []) if isinstance(x, dict)})
        headlines, hl_stats = _load_latest_headlines_for_symbols(
            symbols=post_syms,
            window_hours=48.0,
            limit=headlines_limit,
            return_stats=True,
        )
    except Exception:
        headlines = []
        hl_stats = {"source": "redis", "sym_raw": 0, "sym_kept": 0, "market_raw": 0, "material_only": True}

    # Proof-grade health log for "why no headlines" nights.
    try:
        r_ctx = redis_client_for_ctx()
        ck = getattr(getattr(r_ctx, "connection_pool", None), "connection_kwargs", {}) or {}
        print(
            "[EARNINGS_RESULTS] headlines_source=redis "
            f"db={ck.get('db')} host={ck.get('host')} "
            f"sym_hits={int(hl_stats.get('sym_raw') or 0)} market_hits={int(hl_stats.get('market_raw') or 0)}"
        )

        # Guardrail: detect poller/bot Redis DB mismatch via shared heartbeat file.
        try:
            from pathlib import Path

            hb_path = Path("logs") / "news_poller_heartbeat.json"
            if hb_path.exists():
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
            else:
                hb = None

            poller_db = None
            if isinstance(hb, dict):
                poller_db = hb.get("db")
            bot_db = ck.get("db")

            if poller_db is not None and bot_db is not None and str(poller_db) != str(bot_db):
                global _last_news_db_mismatch_warn_ts
                now_ts = float(time_lib.time())
                if _last_news_db_mismatch_warn_ts is None or (now_ts - float(_last_news_db_mismatch_warn_ts)) >= 3600.0:
                    _last_news_db_mismatch_warn_ts = now_ts
                    print(f"[WARN][NEWS] poller_db={poller_db} bot_db={bot_db} (headlines may be empty)")
        except Exception:
            pass
    except Exception:
        pass

    now_utc = now_et.astimezone(timezone.utc)

    lines.append("")
    lines.append("📰 Headlines (Last 48h)")
    if headlines:
        lines.extend(_format_headlines_compact(headlines, now_utc=now_utc, limit=headlines_limit))
    else:
        raw_n = int(hl_stats.get("sym_raw") or 0) if isinstance(hl_stats, dict) else 0
        kept_n = int(hl_stats.get("sym_kept") or 0) if isinstance(hl_stats, dict) else 0
        material_only = bool(hl_stats.get("material_only")) if isinstance(hl_stats, dict) else True
        if material_only and raw_n > 0 and kept_n == 0:
            lines.append(f"No material headlines (filter=material_only, raw={raw_n}, kept=0)")
        else:
            lines.append("No cached headlines for tracked names")
            lines.append("(verify News poller + Redis cache)")

    lines.append("")
    lines.append(f"Updated: {updated_et_stamp}")

    if not earnings_key_present:
        earnings_age = "disabled"
    elif last_error == "http_error" and last_http_status is not None:
        earnings_age = f"http_{int(last_http_status)}"
    elif last_error == "provider_not_supported":
        earnings_age = "unsupported"
    elif last_error == "exception":
        earnings_age = "error"
    else:
        earnings_age = "live"

    lines.append(f"Data: earnings-{earnings_age}")
    lines.append("Context only • Not financial advice")

    section_data: Dict[str, Any] = {
        "target_date_et": target_date_et,
        "earnings_provider": provider,
        "earnings_last_fetch": last_fetch if last_fetch_matches else None,
        "earnings_rows": earn,
        "results_rows": results,
        "updated_utc": _now_utc().replace(tzinfo=timezone.utc).isoformat(),
    }

    extra_meta: Dict[str, Any] = {
        "target_date_et": target_date_et,
        "results_count": len(results),
        "earnings_count": len(earn),
        "earnings_enabled": bool(earnings_key_present),
        "headlines_count": len(headlines) if isinstance(headlines, list) else 0,
        "headlines_sym_hits": int(hl_stats.get("sym_raw") or 0) if isinstance(hl_stats, dict) else 0,
        "headlines_market_hits": int(hl_stats.get("market_raw") or 0) if isinstance(hl_stats, dict) else 0,
        "vol_rows_count": len(vol_rows) if isinstance(vol_rows, list) else 0,
        "why_moved_count": int(why_moved_count),
        "source": str(provider or "").strip() or "unknown",
        "earnings_results_mode": str(mode),
    }

    precomputed_agent_payload: Dict[str, Any] = {
        "meta": {
            "generated_at": now_et.astimezone(timezone.utc).isoformat(),
            "symbols": [],
            "post_type": "earnings_results_today",
            "data_quality": {"technical_state": "N/A"},
            **extra_meta,
        },
        "sections": section_data,
    }

    return _render_with_agent_context(
        lines,
        symbols=[],
        generated_at=now_et,
        post_type="earnings_results_today",
        extra_meta=extra_meta,
        sections=section_data,
        precomputed_agent_payload=precomputed_agent_payload,
    )


def next_weekday(d: date) -> date:
    # Mon-Fri only (MVP-safe; holidays not detected without an external calendar)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _parse_weekday_et(raw: str | None, fallback: int = 6) -> int | None:
    """Parse an ET weekday into Python weekday int (MON=0..SUN=6).

    Special values:
    - ANY: caller-defined behavior (typically: use "today"'s weekday).
    """

    s = (raw or "").strip().upper()
    if not s:
        return int(fallback)

    if s in {"ANY", "ALL", "*"}:
        return None

    try:
        n = int(s)
        if 0 <= n <= 6:
            return n
    except Exception:
        pass

    mapping = {
        "MON": 0,
        "TUE": 1,
        "WED": 2,
        "THU": 3,
        "FRI": 4,
        "SAT": 5,
        "SUN": 6,
    }
    return int(mapping.get(s, fallback))


def _automation_dedupe_key(*, label: str, target_date_et: str) -> str:
    return f"tnt:automation:{label}:{target_date_et}"


def _automation_should_post_once_per_target_date(*, label: str, target_date_et: str, ttl_sec: int) -> bool:
    """Return True if we should attempt posting for this (label,target_date).

    This is check-only. Call `_automation_mark_posted_once_per_target_date(...)`
    only after a successful publish.
    """

    key = _automation_dedupe_key(label=label, target_date_et=target_date_et)

    try:
        r_ctx = redis_client_for_ctx()
    except Exception:
        r_ctx = None

    if r_ctx is not None:
        try:
            return not bool(r_ctx.exists(key))
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation dedupe redis check error: {type(exc).__name__}: {exc}")

    # Fallback: best-effort in-memory (no cross-restart guarantees).
    now_s = int(_now_utc().timestamp())
    last_s = int(_last_automation_posted_by_key.get(key, 0) or 0)
    return not ((now_s - last_s) < max(60, int(ttl_sec)))


def _automation_mark_posted_once_per_target_date(*, label: str, target_date_et: str, ttl_sec: int) -> None:
    """Mark a successful automation post in Redis; fallback to in-memory."""

    key = _automation_dedupe_key(label=label, target_date_et=target_date_et)

    try:
        r_ctx = redis_client_for_ctx()
    except Exception:
        r_ctx = None

    if r_ctx is not None:
        try:
            payload = json.dumps({"target_date_et": target_date_et, "ts": _utc_now_iso()}, ensure_ascii=False)
            r_ctx.set(key, payload, ex=max(60, int(ttl_sec)))
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation dedupe redis mark error: {type(exc).__name__}: {exc}")

    try:
        _last_automation_posted_by_key[key] = int(_now_utc().timestamp())
    except Exception:
        pass


def _headline_tag_from_text(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("earnings", "eps", "revenue", "quarter", "q1", "q2", "q3", "q4", "guidance")):
        return "EARNINGS"
    if any(k in t for k in ("guidance", "outlook", "forecast", "raises guidance", "cuts guidance")):
        return "GUIDANCE"
    if any(k in t for k in ("upgrade", "downgrade", "price target", "pt ", "initiates coverage", "reiterates")):
        return "ANALYST"
    if any(k in t for k in ("cpi", "ppi", "nfp", "fomc", "powell", "fed", "treasury", "rates", "jobless")):
        return "MACRO"
    if any(k in t for k in ("sec", "doj", "lawsuit", "settlement", "probe", "investigation", "antitrust")):
        return "LEGAL"
    if any(k in t for k in ("acquires", "acquisition", "merger", "buyout", "m&a", "takeover", "deal")):
        return "M&A"
    if any(k in t for k in ("launch", "released", "release", "product", "ai", "chip", "iphone", "gpu")):
        return "PRODUCT"
    return "NEWS"


def _append_recent_news_context_line(
    text: str,
    *,
    symbol: str,
    now_utc: datetime | None = None,
    window_min: int = 120,
) -> str:
    """Append a single-line news context callout (cache-only).

    Designed for alerts: one line, deterministic, no upstream calls.
    """

    base = (text or "").rstrip()
    sym = str(symbol or "").strip().upper()
    if not base or not sym:
        return base
    if "📰 Context:" in base:
        return base

    try:
        win = max(5, min(360, int(window_min)))
    except Exception:
        win = 120

    now_utc = (now_utc or _now_utc()).replace(tzinfo=timezone.utc)
    try:
        hs = _load_latest_headlines_for_symbols(symbols=[sym], window_hours=max(1.0, win / 60.0), limit=1)
    except Exception:
        hs = []
    if not hs:
        return base

    it = hs[0]
    dt = it.get("published_utc")
    if not isinstance(dt, datetime):
        return base
    dt_utc = dt.astimezone(timezone.utc)
    age_min = (now_utc - dt_utc).total_seconds() / 60.0
    if age_min < 0 or age_min > float(win):
        return base

    tag = str(it.get("tag") or "NEWS").strip().upper() or "NEWS"
    title = str(it.get("headline") or "").strip()
    url = str(it.get("url") or "").strip()
    if not title:
        return base

    age_s = f"{age_min:.0f}m"
    if url:
        line = f"📰 Context: [{tag}] {title} ({age_s}) <{url}>"
    else:
        line = f"📰 Context: [{tag}] {title} ({age_s})"

    return base + "\n" + line


def _decode_redis_json(v: object) -> dict[str, object] | None:
    if not v:
        return None
    try:
        if isinstance(v, (bytes, bytearray)):
            s = v.decode("utf-8", errors="replace")
        else:
            s = str(v)
        s = s.strip()
        if not s:
            return None
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _volatility_verdict_from_ratio(ratio: float | None) -> str:
    if ratio is None:
        return "UNKNOWN"
    try:
        r = float(ratio)
    except Exception:
        return "UNKNOWN"
    if r >= 1.5:
        return "UNDERPRICED"
    if r >= 0.75:
        return "FAIR"
    return "OVERPRICED"


def _load_volatility_reality_for_symbols(*, symbols: list[str], limit: int = 3) -> list[dict[str, object]]:
    """Cache-first implied vs realized move for earnings names.

    Source: canonical `cal:earnings:{SYM}` blob written by calendar/enrichment.
    - implied_pct: expected_move_pct
    - realized_pct: abs(history[0].move_pct)

    Never calls upstream APIs; returns [] on cache miss/unavailable.
    """

    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if not syms:
        return []

    try:
        r_ctx = redis_client_for_ctx()
    except Exception:
        r_ctx = None
    if r_ctx is None:
        return []

    keys = [f"cal:earnings:{s}" for s in syms]
    raw_vals: list[object] = []
    try:
        if hasattr(r_ctx, "mget"):
            raw = r_ctx.mget(keys)
            if isinstance(raw, list):
                raw_vals = list(raw)
        else:
            raw_vals = [r_ctx.get(k) for k in keys]
    except Exception:
        raw_vals = []

    out: list[dict[str, object]] = []
    for sym, raw in zip(syms, raw_vals, strict=False):
        payload = _decode_redis_json(raw)
        if not payload:
            continue

        implied = payload.get("expected_move_pct")
        hist = payload.get("history")
        realized = None
        if isinstance(hist, list) and hist:
            h0 = hist[0] if isinstance(hist[0], dict) else None
            if isinstance(h0, dict):
                realized = h0.get("move_pct")

        try:
            implied_f = float(implied) if implied is not None else None
        except Exception:
            implied_f = None
        try:
            realized_f = abs(float(realized)) if realized is not None else None
        except Exception:
            realized_f = None

        ratio = None
        if implied_f is not None and implied_f != 0 and realized_f is not None:
            ratio = realized_f / abs(implied_f)

        if implied_f is None or realized_f is None or ratio is None:
            continue

        out.append(
            {
                "symbol": sym,
                "implied_pct": implied_f,
                "realized_pct": realized_f,
                "ratio": ratio,
                "verdict": _volatility_verdict_from_ratio(ratio),
            }
        )

    # Rank by most "surprising" (ratio) then by realized magnitude.
    out.sort(key=lambda d: (float(d.get("ratio") or 0.0), float(d.get("realized_pct") or 0.0)), reverse=True)
    return out[: max(0, min(6, int(limit)))]


def _parse_dt_maybe_utc(value: object) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _time_ago_short(dt_utc: datetime, now_utc: datetime) -> str:
    try:
        delta_s = max(0.0, (now_utc - dt_utc).total_seconds())
    except Exception:
        return "?"
    mins = delta_s / 60.0
    if mins < 90:
        return f"{int(round(mins))}m"
    hrs = mins / 60.0
    if hrs < 36:
        return f"{int(round(hrs))}h"
    days = hrs / 24.0
    return f"{int(round(days))}d"


def _clean_url_for_display(url: str, *, max_len: int = 120) -> str:
    """Return a stable URL string suitable for inline display.

    - Strips query + fragment to reduce noise
    - Falls back to scheme+host if still too long
    """

    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw
        cleaned = parsed._replace(query="", fragment="")
        out = urlunparse(cleaned)
        if len(out) <= max(20, int(max_len)):
            return out
        root = urlunparse(cleaned._replace(path="", params=""))
        return root
    except Exception:
        return raw


def _headline_tag_compact(tag: str) -> str:
    t = str(tag or "").strip().upper() or "NEWS"
    tag_map = {
        "EARNINGS": "EARN",
        "GUIDANCE": "EARN",
        "MACRO": "MACRO",
        "REG": "REG",
        "LEGAL": "REG",
        "M&A": "M&A",
        "ANALYST": "ANLST",
        "RATING": "ANLST",
        "PRODUCT": "PROD",
        "NEWS": "NEWS",
    }
    return tag_map.get(t, t[:8])


def _format_headlines_compact(
    headlines: list[dict[str, object]] | None,
    *,
    now_utc: datetime,
    limit: int = 4,
) -> list[str]:
    """Format cached headlines into 1-line, non-spammy bullets."""

    items = list(headlines or [])[: max(0, int(limit))]
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tag = _headline_tag_compact(str(it.get("tag") or "NEWS"))
        sym = str(it.get("symbol") or "").strip().upper()
        title = str(it.get("headline") or "").strip()
        url = str(it.get("url") or "").strip()
        dt = it.get("published_utc")
        age = None
        if isinstance(dt, datetime):
            age = _time_ago_short(dt.astimezone(timezone.utc), now_utc)

        if not title:
            continue

        parts: list[str] = []
        if age:
            parts.append(age)
        parts.append(f"[{tag}]")
        if sym and sym != "MARKET":
            parts.append(f"{sym}:")
        parts.append(title)

        line = " ".join(parts)
        if url:
            line_url = _clean_url_for_display(url)
            if line_url:
                line = f"{line} <{line_url}>"
        out.append(f"• {line}")

    return out


def _count_cached_earnings_within_days(
    *,
    symbols: list[str],
    now_utc: datetime,
    days: float = 2.0,
) -> int:
    """Cache-only count of symbols with an upcoming earnings timestamp.

    Reads `cal:earnings:{SYM}` and checks `ts_utc` field.
    Returns 0 if Redis unavailable or cache miss.
    """

    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if not syms:
        return 0

    try:
        r_ctx = redis_client_for_ctx()
    except Exception:
        r_ctx = None
    if r_ctx is None:
        return 0

    try:
        from services.calendar.calendar_keys import cal_earnings_key

        keys = [cal_earnings_key(s) for s in syms]
    except Exception:
        keys = [f"cal:earnings:{s}" for s in syms]

    try:
        raw_vals = r_ctx.mget(keys) if hasattr(r_ctx, "mget") else [r_ctx.get(k) for k in keys]
        if not isinstance(raw_vals, list):
            raw_vals = []
    except Exception:
        raw_vals = []

    horizon_s = max(0.0, float(days) * 24.0 * 3600.0)
    count = 0
    for raw in raw_vals:
        payload = _decode_redis_json(raw)
        if not payload:
            continue
        ts = payload.get("ts_utc")
        if not ts:
            continue
        dt = _parse_dt_maybe_utc(ts)
        if dt is None:
            continue
        delta_s = (dt - now_utc).total_seconds()
        if 0 <= delta_s <= horizon_s:
            count += 1

    return count


def _next_macro_event_any(events: list[dict] | None, now_et: datetime) -> dict | None:
    if not events:
        return None
    best: dict | None = None
    best_delta = None
    for item in events:
        t = (item.get("time_et") or "").strip()
        if not t:
            continue
        try:
            hh, mm = [int(x) for x in t.split(":")]
            ev_dt = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            delta_min = (ev_dt - now_et).total_seconds() / 60.0
            if delta_min < 0:
                continue
            if best_delta is None or delta_min < best_delta:
                best_delta = float(delta_min)
                best = dict(item)
                best["delta_min"] = float(delta_min)
                best["event_dt"] = ev_dt
        except Exception:  # noqa: BLE001
            continue
    return best


def _load_latest_headlines_for_symbols(
    *,
    symbols: list[str],
    window_hours: float = 48.0,
    limit: int = 3,
    material_only_override: bool | None = None,
    return_stats: bool = False,
) -> list[dict[str, object]] | tuple[list[dict[str, object]], dict[str, object]]:
    """Return a ranked set of cached headlines for the given symbols.

    Uses Redis-backed NewsService history layer populated by scripts/news_poller.py.
    Never throws; returns [] on cache miss/unavailable.
    """

    syms = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if not syms:
        empty: list[dict[str, object]] = []
        if return_stats:
            return empty, {"source": "redis", "sym_raw": 0, "sym_kept": 0, "market_raw": 0, "material_only": True}
        return empty

    try:
        r_ctx = redis_client_for_ctx()
    except Exception:
        r_ctx = None
    if r_ctx is None:
        empty: list[dict[str, object]] = []
        if return_stats:
            return empty, {"source": "redis", "sym_raw": 0, "sym_kept": 0, "market_raw": 0, "material_only": True}
        return empty

    try:
        from services.news.news_service import NewsService

        svc = NewsService(r_ctx)
    except Exception:
        empty: list[dict[str, object]] = []
        if return_stats:
            return empty, {"source": "redis", "sym_raw": 0, "sym_kept": 0, "market_raw": 0, "material_only": True}
        return empty

    now_utc = _now_utc().replace(tzinfo=timezone.utc)
    cutoff = now_utc - timedelta(hours=float(window_hours or 48.0))

    candidates: list[dict[str, object]] = []
    seen_ids: set[str] = set()

    sym_raw = 0
    sym_kept = 0
    market_raw = 0

    # Pull a few per symbol, then rank globally (keeps it tight and "Bloomberg-ish").
    per_sym = max(3, min(10, int(limit) * 3))
    want = set(syms)

    for sym in syms:
        try:
            items = svc.get_recent_items(symbol=sym, limit=per_sym)
        except Exception:
            items = []

        for it in items:
            if not isinstance(it, dict):
                continue
            nid = str(it.get("id") or "").strip()
            if not nid or nid in seen_ids:
                continue

            headline = str(it.get("headline") or it.get("title") or "").strip()
            url = str(it.get("url") or "").strip()
            dt = _parse_dt_maybe_utc(it.get("published_utc") or it.get("published") or it.get("created"))
            if not headline or dt is None:
                continue
            if dt < cutoff:
                continue

            sym_raw += 1

            tickers = it.get("tickers")
            tickers_list: list[str] = []
            if isinstance(tickers, list):
                tickers_list = [str(t).strip().upper() for t in tickers if t and str(t).strip()]
            elif isinstance(tickers, str):
                tickers_list = [t.strip().upper() for t in tickers.split(",") if t and t.strip()]

            primary = None
            for t in tickers_list:
                if t in want:
                    primary = t
                    break
            if primary is None:
                primary = sym

            # Prefer ingest-time classification if present; else classify locally.
            tags: list[str] = []
            severity = str(it.get("severity") or "").strip().upper() if isinstance(it, dict) else ""
            if isinstance(it.get("tags"), list):
                tags = [str(t).strip().lower() for t in (it.get("tags") or []) if str(t).strip()]

            if not tags:
                try:
                    from services.news.news_rules import classify_news

                    tags, severity_raw = classify_news(it)
                    severity = str(severity or severity_raw or "").strip().upper()
                except Exception:
                    tags = []

            # Material-only filter (default ON): keep only strong catalysts.
            # NOTE: Earnings nights often have headlines without upstream tags; treat earnings-like text as material.
            material_only = _env_bool("TNT_HEADLINES_MATERIAL_ONLY", "1") if material_only_override is None else bool(material_only_override)
            if material_only:
                material_tags = {"macro", "earnings", "guidance", "m&a", "sec", "fda"}
                tag_guess = _headline_tag_from_text(headline)
                is_material = bool(set(tags) & material_tags) or severity in {"HIGH"} or tag_guess in {
                    "EARNINGS",
                    "GUIDANCE",
                    "MACRO",
                    "LEGAL",
                    "M&A",
                    "ANALYST",
                    "PRODUCT",
                }
                if not is_material:
                    continue

            sym_kept += 1

            tag = "NEWS"
            if tags:
                # Surface the strongest tag first.
                pref = ["macro", "earnings", "guidance", "sec", "fda", "m&a", "rating"]
                chosen = None
                for p in pref:
                    if p in tags:
                        chosen = p
                        break
                chosen = chosen or tags[0]
                tag_map = {
                    "macro": "MACRO",
                    "earnings": "EARNINGS",
                    "guidance": "EARNINGS",
                    "sec": "REG",
                    "fda": "REG",
                    "m&a": "M&A",
                    "rating": "ANALYST",
                }
                tag = tag_map.get(str(chosen), str(chosen).upper())
            else:
                tag = _headline_tag_from_text(headline)

            score = 0
            # Prefer highly-specific single-name items.
            if tickers_list and len(tickers_list) <= 2:
                score += 2
            # Prefer keyword-matched catalysts.
            if tag != "NEWS":
                score += 3
            if severity == "HIGH":
                score += 3
            # Small recency bump.
            age_h = max(0.0, (now_utc - dt).total_seconds() / 3600.0)
            score += int(max(0.0, 48.0 - min(48.0, age_h)) / 12.0)

            candidates.append(
                {
                    "id": nid,
                    "symbol": primary,
                    "tag": tag,
                    "headline": headline,
                    "url": url,
                    "published_utc": dt,
                    "_score": score,
                }
            )
            seen_ids.add(nid)

    # Health probe: market bucket hits (for diagnostics only; does not affect ranking).
    try:
        market_items = svc.get_recent_items(symbol="MARKET", limit=per_sym)
    except Exception:
        market_items = []
    for it in (market_items or []):
        if not isinstance(it, dict):
            continue
        headline = str(it.get("headline") or it.get("title") or "").strip()
        dt = _parse_dt_maybe_utc(it.get("published_utc") or it.get("published") or it.get("created"))
        if not headline or dt is None:
            continue
        if dt < cutoff:
            continue
        market_raw += 1

    def _sort_key(x: dict[str, object]) -> tuple[int, float]:
        sc = int(x.get("_score") or 0)
        dt = x.get("published_utc")
        ts = float(dt.timestamp()) if isinstance(dt, datetime) else 0.0
        return (sc, ts)

    candidates.sort(key=_sort_key, reverse=True)
    out = candidates[: max(0, min(6, int(limit)))]
    for x in out:
        x.pop("_score", None)

    if return_stats:
        material_only = _env_bool("TNT_HEADLINES_MATERIAL_ONLY", "1") if material_only_override is None else bool(material_only_override)
        return out, {
            "source": "redis",
            "sym_raw": int(sym_raw),
            "sym_kept": int(sym_kept),
            "market_raw": int(market_raw),
            "material_only": bool(material_only),
        }
    return out


async def automation_earnings_daily_loop(channel):
    await bot.wait_until_ready()

    hh, mm = _parse_hhmm(os.getenv("TNT_EARNINGS_DAILY_TIME_ET", "18:00"))
    poll_sec = _env_int("TNT_EARNINGS_DAILY_POLL_SEC", "60")
    dedupe_ttl_sec = _env_int("TNT_EARNINGS_DAILY_DEDUPE_TTL_SEC", str(3 * 24 * 3600))
    print(
        "[OK] automation_earnings_daily_loop running "
        f"(time_et={hh:02d}:{mm:02d}, dedupe_ttl={dedupe_ttl_sec}s)"
    )

    while not bot.is_closed():
        try:
            if not _env_bool("EARNINGS_AUTOPOST_ENABLED", "0"):
                await asyncio.sleep(max(5, poll_sec))
                continue

            now_et = _now_et()
            due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et < due:
                await asyncio.sleep(max(5, poll_sec))
                continue

            target_d = next_weekday(now_et.date() + timedelta(days=1))
            target_date_et = target_d.strftime("%Y-%m-%d")

            key = _automation_dedupe_key(label="earnings_daily", target_date_et=target_date_et)

            should_post = _automation_should_post_once_per_target_date(
                label="earnings_daily",
                target_date_et=target_date_et,
                ttl_sec=dedupe_ttl_sec,
            )

            if not should_post:
                print(f"[AUTOPOST][EARNINGS] deduped key={key} target_date_et={target_date_et} kind=status")
            else:
                print(f"[AUTOPOST][EARNINGS] posting key={key} target_date_et={target_date_et} kind=status")

            render = await asyncio.to_thread(
                build_tomorrow_earnings_macro_render,
                target_date_et=target_date_et,
                now_et=now_et,
            )

            try:
                payload = render.agent_payload or {}
                meta = payload.get("meta", {}) if isinstance(payload, dict) else {}

                rows = int(meta.get("earnings_count") or 0)
                headlines = 1 if int(meta.get("headlines_count") or 0) > 0 else 0
                source = str(meta.get("source") or "").strip() or "unknown"

                print(
                    "[AUTOPOST][EARNINGS][PROOF] "
                    f"kind=status expected=1 results=0 macro=1 headlines={headlines} rows={rows} source={source}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] [AUTOPOST][EARNINGS][PROOF] failed: {type(exc).__name__}: {exc}")

            if not should_post:
                await asyncio.sleep(60)
                continue

            publish_id = None
            try:
                import uuid

                publish_id = uuid.uuid4().hex[:10]
            except Exception:
                publish_id = None
            try:
                print(f"[AUTOPOST][EARNINGS] publish_id={publish_id} key={key} target_date_et={target_date_et}")
            except Exception:
                pass
            await _publish_autopost_render(
                channel,
                render,
                builder="build_tomorrow_earnings_macro_render",
                label="earnings_daily",
                symbol="",
                publish_id=publish_id,
                kind="status",
                context_mode="db",
                context_output_mode="strict",
            )

            _automation_mark_posted_once_per_target_date(
                label="earnings_daily",
                target_date_et=target_date_et,
                ttl_sec=dedupe_ttl_sec,
            )
            print(f"[OK] earnings_daily posted label=earnings_daily target_date_et={target_date_et} publish_id={publish_id}")
            await asyncio.sleep(60)

        except ContractViolationError as exc:
            print(f"[WARN] automation_earnings_daily_loop contract violation: {exc}")
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(max(5, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_earnings_daily_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(5, poll_sec))


async def automation_earnings_results_loop(channel):
    await bot.wait_until_ready()

    times_s = (os.getenv("TNT_EARNINGS_RESULTS_TIMES_ET") or os.getenv("TNT_EARNINGS_RESULTS_TIME_ET") or "18:10").strip()
    poll_sec = _env_int("TNT_EARNINGS_RESULTS_POLL_SEC", "60")
    dedupe_ttl_sec = _env_int("TNT_EARNINGS_RESULTS_DEDUPE_TTL_SEC", str(3 * 24 * 3600))

    times: list[tuple[int, int]] = []
    for part in [p.strip() for p in times_s.split(",") if p.strip()]:
        try:
            hh, mm = _parse_hhmm(part)
            times.append((hh, mm))
        except Exception:
            continue
    if not times:
        times = [(18, 10)]

    times_label = ",".join([f"{h:02d}:{m:02d}" for (h, m) in times])
    print(
        "[OK] automation_earnings_results_loop running "
        f"(times_et={times_label}, dedupe_ttl={dedupe_ttl_sec}s)"
    )

    while not bot.is_closed():
        try:
            if not _env_bool("EARNINGS_RESULTS_AUTOPOST_ENABLED", "0"):
                await asyncio.sleep(max(5, poll_sec))
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(max(5, poll_sec))
                continue

            target_date_et = now_et.strftime("%Y-%m-%d")

            posted_any = False
            for idx, (hh, mm) in enumerate(times, start=1):
                due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if now_et < due:
                    continue

                label = f"earnings_results_{idx}"
                key = _automation_dedupe_key(label=label, target_date_et=target_date_et)

                should_post = _automation_should_post_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                )

                if not should_post:
                    print(f"[AUTOPOST][EARNINGS_RESULTS] deduped key={key} target_date_et={target_date_et} kind=status")
                else:
                    print(f"[AUTOPOST][EARNINGS_RESULTS] posting key={key} target_date_et={target_date_et} kind=status")

                render = await asyncio.to_thread(
                    build_earnings_results_today_render,
                    target_date_et=target_date_et,
                    now_et=now_et,
                )

                try:
                    payload = render.agent_payload or {}
                    meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
                    sections = payload.get("sections", {}) if isinstance(payload, dict) else {}
                    results_rows = sections.get("results_rows") if isinstance(sections, dict) else None

                    beats = misses = inline = 0
                    if isinstance(results_rows, list):
                        for r in results_rows:
                            if not isinstance(r, dict):
                                continue
                            tag = None
                            eps_a = r.get("eps_actual")
                            eps_e = r.get("eps_est")
                            if eps_a is not None and eps_e is not None:
                                tag = _beat_tag(eps_a, eps_e)
                            else:
                                rev_a = r.get("rev_actual")
                                rev_e = r.get("rev_est")
                                if rev_a is not None and rev_e is not None:
                                    tag = _beat_tag(rev_a, rev_e)

                            if tag == "BEAT":
                                beats += 1
                            elif tag == "MISS":
                                misses += 1
                            elif tag == "INLINE":
                                inline += 1

                    rows = int(meta.get("results_count") or 0)
                    headlines = 1 if int(meta.get("headlines_count") or 0) > 0 else 0
                    why_moved = 1 if int(meta.get("why_moved_count") or 0) > 0 else 0

                    print(
                        "[AUTOPOST][EARNINGS_RESULTS][PROOF] "
                        f"kind=status rows={rows} beats={beats} misses={misses} inline={inline} "
                        f"reality_check=1 why_moved={why_moved} headlines={headlines}"
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] [AUTOPOST][EARNINGS_RESULTS][PROOF] failed: {type(exc).__name__}: {exc}")

                if not should_post:
                    continue

                publish_id = None
                try:
                    import uuid

                    publish_id = uuid.uuid4().hex[:10]
                except Exception:
                    publish_id = None
                try:
                    print(f"[AUTOPOST][EARNINGS_RESULTS] publish_id={publish_id} key={key} target_date_et={target_date_et}")
                except Exception:
                    pass
                await _publish_autopost_render(
                    channel,
                    render,
                    builder="build_earnings_results_today_render",
                    label=label,
                    symbol="",
                    publish_id=publish_id,
                    kind="status",
                    context_mode="db",
                    context_output_mode="strict",
                )

                _automation_mark_posted_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                )
                print(f"[OK] earnings_results posted label={label} target_date_et={target_date_et} publish_id={publish_id}")
                posted_any = True

            await asyncio.sleep(60 if posted_any else max(5, poll_sec))

        except ContractViolationError as exc:
            print(f"[WARN] automation_earnings_results_loop contract violation: {exc}")
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(max(5, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_earnings_results_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(5, poll_sec))


async def automation_earnings_results_prewarm_loop() -> None:
    """Pre-warm earnings inputs before scheduled results posts.

    Default times (ET): 07:45 and 15:45.
    Best-effort warmers (no posting):
    - Earnings results API fetch for today (watchlist-only by design)
    - Expected-move cache refresh
    - Per-symbol headline cache refresh
    """

    await bot.wait_until_ready()

    times_s = (os.getenv("TNT_EARNINGS_RESULTS_PREWARM_TIMES_ET") or "07:45,15:45").strip()
    poll_sec = _env_int("TNT_EARNINGS_RESULTS_PREWARM_POLL_SEC", "30")
    dedupe_ttl_sec = _env_int("TNT_EARNINGS_RESULTS_PREWARM_DEDUPE_TTL_SEC", str(2 * 24 * 3600))

    times: list[tuple[int, int]] = []
    for part in [p.strip() for p in times_s.split(",") if p.strip()]:
        try:
            hh, mm = _parse_hhmm(part)
            times.append((hh, mm))
        except Exception:
            continue
    if not times:
        times = [(7, 45), (15, 45)]

    times_label = ",".join([f"{h:02d}:{m:02d}" for (h, m) in times])
    print(f"[OK] automation_earnings_results_prewarm_loop running (times_et={times_label}, dedupe_ttl={dedupe_ttl_sec}s)")

    last_skip_reason: str | None = None
    last_skip_ts: float = 0.0

    def _log_skip(reason: str) -> None:
        nonlocal last_skip_reason, last_skip_ts
        try:
            now_ts = float(time_lib.time())
            if reason != last_skip_reason or (now_ts - float(last_skip_ts)) >= 300.0:
                last_skip_reason = str(reason)
                last_skip_ts = now_ts
                print(f"[EARNINGS_RESULTS][PREWARM][SKIP] reason={reason}")
        except Exception:
            return

    while not bot.is_closed():
        try:
            if not _env_bool("EARNINGS_RESULTS_AUTOPOST_ENABLED", "0"):
                _log_skip("autopost_disabled")
                await asyncio.sleep(max(5, poll_sec))
                continue
            if not _env_bool("EARNINGS_RESULTS_PREWARM_ENABLED", "1"):
                _log_skip("prewarm_disabled")
                await asyncio.sleep(max(5, poll_sec))
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                _log_skip("weekend")
                await asyncio.sleep(max(5, poll_sec))
                continue

            target_date_et = now_et.strftime("%Y-%m-%d")

            ran_any = False
            for idx, (hh, mm) in enumerate(times, start=1):
                due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if now_et < due:
                    continue

                label = f"earnings_results_prewarm_{idx}"
                key = _automation_dedupe_key(label=label, target_date_et=target_date_et)

                should_run = _automation_should_post_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                )
                if not should_run:
                    _log_skip(f"already_warmed_recently:{label}")
                    continue

                watchlist = [s.strip().upper() for s in (os.getenv("EARNINGS_WATCHLIST") or "").split(",") if s.strip()]
                majors_env = os.getenv("TNT_EARNINGS_RESULTS_MAJORS", "AAPL,MSFT,TSLA,META,NVDA") or ""
                majors = [s.strip().upper() for s in majors_env.split(",") if s.strip()]
                symbols = list(dict.fromkeys([*watchlist, *majors]))

                try:
                    max_syms = int(_env_int("TNT_EARNINGS_RESULTS_PREWARM_MAX_SYMBOLS", "30"))
                except Exception:
                    max_syms = 30
                symbols = symbols[: max(5, min(60, int(max_syms)))]

                # Step 1: best-effort earnings fetch for today.
                if not bool(_resolve_earnings_api_key()):
                    _log_skip("missing_api_key")
                try:
                    await asyncio.to_thread(fetch_earnings_for_date, target_date_et)
                except Exception:
                    pass

                em_ok = 0
                news_ok = 0

                # Step 2: warm expected move + headlines using existing bounded helpers.
                try:
                    from scripts.earnings_precache import _redis_client as _ep_redis_client
                    from scripts.earnings_precache import _refresh_expected_move_one as _ep_refresh_expected_move_one
                    from scripts.earnings_precache import _refresh_news_one as _ep_refresh_news_one

                    r = _ep_redis_client()

                    try:
                        em_timeout = float(os.getenv("TNT_EARNINGS_RESULTS_PREWARM_EM_TIMEOUT_SEC", "3.0") or "3.0")
                    except Exception:
                        em_timeout = 3.0
                    try:
                        news_timeout = float(os.getenv("TNT_EARNINGS_RESULTS_PREWARM_NEWS_TIMEOUT_SEC", "8.0") or "8.0")
                    except Exception:
                        news_timeout = 8.0
                    try:
                        news_limit = int(os.getenv("TNT_EARNINGS_RESULTS_PREWARM_NEWS_LIMIT", "20") or "20")
                    except Exception:
                        news_limit = 20
                    try:
                        news_ttl = int(os.getenv("TNT_EARNINGS_RESULTS_PREWARM_NEWS_TTL_SEC", str(6 * 3600)) or str(6 * 3600))
                    except Exception:
                        news_ttl = 6 * 3600

                    concurrency = max(1, int(_env_int("TNT_EARNINGS_RESULTS_PREWARM_CONCURRENCY", "4")))
                    sem = asyncio.Semaphore(concurrency)

                    async def _one_em(sym: str) -> None:
                        nonlocal em_ok
                        async with sem:
                            try:
                                res = await _ep_refresh_expected_move_one(symbol=sym, timeout_s=float(em_timeout))
                                if isinstance(res, dict) and res.get("expected_move_written"):
                                    em_ok += 1
                            except Exception:
                                return

                    async def _one_news(sym: str) -> None:
                        nonlocal news_ok
                        async with sem:
                            try:
                                res = await _ep_refresh_news_one(
                                    r=r,
                                    symbol=sym,
                                    timeout_s=float(news_timeout),
                                    lookback_hours=24,
                                    limit=int(news_limit),
                                    ttl_sec=int(news_ttl),
                                )
                                if isinstance(res, dict) and int(res.get("stored") or 0) > 0:
                                    news_ok += 1
                            except Exception:
                                return

                    await asyncio.gather(*[_one_em(s) for s in symbols])
                    await asyncio.gather(*[_one_news(s) for s in symbols])
                except Exception:
                    pass

                try:
                    print(
                        f"[EARNINGS_RESULTS][PREWARM] key={key} target_date_et={target_date_et} symbols={len(symbols)} em_ok={em_ok} news_ok={news_ok}"
                    )
                except Exception:
                    pass

                _automation_mark_posted_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                )
                ran_any = True

            await asyncio.sleep(60 if ran_any else max(5, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_earnings_results_prewarm_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(5, poll_sec))


def build_daily_market_outlook_render(*, now_et: datetime | None = None) -> RenderedPost:
    """Once/day market outlook post.

    Anti-regret rules:
    - Conditions only (no trade calls)
    - Cache-first: macro CSV + Redis news history
    """

    now_et = now_et or _now_et()
    session = market_session_et(now_et.astimezone(timezone.utc))
    today_et = now_et.strftime("%Y-%m-%d")

    macro_events = load_macro_events_for_date(today_et)
    macro_banner = macro_risk_banner(macro_events, label=f"Macro Risk ({today_et} ET)")

    symbols = [
        s.strip().upper()
        for s in (os.getenv("DAILY_OUTLOOK_SYMBOLS", "SPY,QQQ,IWM") or "").split(",")
        if s.strip()
    ]
    symbols = list(dict.fromkeys(symbols))

    try:
        headlines_limit = _env_int("TNT_OUTLOOK_HEADLINES_LIMIT", "4")
    except Exception:
        headlines_limit = 4
    headlines_limit = max(3, min(5, int(headlines_limit)))

    try:
        headlines = _load_latest_headlines_for_symbols(
            symbols=["MARKET"] + symbols,
            window_hours=24.0,
            limit=headlines_limit,
        )
    except Exception:
        headlines = []

    now_utc = now_et.astimezone(timezone.utc)

    lines: list[str] = []
    lines.append("🌅 Daily Market Outlook")
    lines.append(f"ET stamp: {now_et.strftime('%Y-%m-%d %H:%M ET')} | Session: {session}")

    try:
        snap = get_regime_snapshot(post_type="daily_outlook")
    except Exception:
        snap = []
    if snap:
        lines.append("")
        lines.append("📍 Conditions Snapshot")
        lines.extend(snap[:5])

    # Deterministic “OH WOW” callout (cache-only inputs).
    wow_reasons: list[str] = []
    if macro_banner:
        wow_reasons.append("high-impact macro on deck")

    try:
        wow_news_window_min = max(30, min(180, _env_int("TNT_OUTLOOK_WOW_NEWS_WINDOW_MIN", "90")))
    except Exception:
        wow_news_window_min = 90

    recent_news = 0
    for it in (headlines or []):
        dt = it.get("published_utc") if isinstance(it, dict) else None
        if not isinstance(dt, datetime):
            continue
        age_min = (now_utc - dt.astimezone(timezone.utc)).total_seconds() / 60.0
        if 0 <= age_min <= float(wow_news_window_min):
            recent_news += 1
    if recent_news >= 4:
        wow_reasons.append(f"news cluster ({recent_news} in {wow_news_window_min}m)")

    try:
        wow_days = float(os.getenv("TNT_OUTLOOK_WOW_EARNINGS_DAYS", "2") or "2")
    except Exception:
        wow_days = 2.0
    try:
        wow_earnings_thresh = max(1, min(10, _env_int("TNT_OUTLOOK_WOW_EARNINGS_THRESH", "3")))
    except Exception:
        wow_earnings_thresh = 3

    wow_tickers_env = (os.getenv("TNT_OUTLOOK_WOW_EARNINGS_TICKERS") or "").strip()
    if wow_tickers_env:
        wow_tickers = [s.strip().upper() for s in wow_tickers_env.split(",") if s.strip()]
    else:
        wow_tickers = sorted(set(MEGA_CAP))

    mega_earnings = _count_cached_earnings_within_days(symbols=wow_tickers, now_utc=now_utc, days=wow_days)
    if mega_earnings >= wow_earnings_thresh:
        wow_reasons.append(f"mega-cap earnings cluster ({mega_earnings} ≤{int(round(wow_days * 24))}h)")

    if wow_reasons:
        lines.append("")
        lines.append("⚡ OH WOW: " + "; ".join(wow_reasons[:2]))

    # Crown Jewel: Today’s Setup (conditions-only).
    setup_lines: list[str] = []
    try:
        tf_bias = os.getenv("BIAS_TF", BIAS_TF)
        confirm_bias, _confirm_note, _detail = vix_sqqq_confirmation(tf=tf_bias)
        sig = get_latest_signal("SPY")
        prob_up = float(sig["prob_up"]) if sig and sig.get("prob_up") is not None else None
        edge_val = _edge(prob_up) if prob_up is not None else None
        smiq, pg = _resolve_market_participation(confirm_bias, edge_val)
        regime = str((smiq.get("regime") if isinstance(smiq, dict) else None) or "UNKNOWN").upper()
        try:
            quality = str((smiq.get("data_quality") if isinstance(smiq, dict) else None) or "UNKNOWN").upper()
            reason = (smiq.get("reason") if isinstance(smiq, dict) else None) or None
            ts_et = (smiq.get("timestamp_et") if isinstance(smiq, dict) else None) or None
            extra = f" reason={str(reason).strip().lower()}" if reason else ""
            extra += f" ts={ts_et}" if ts_et else ""
            print(f"[SMIQ][OUTLOOK] participation regime={regime} quality={quality}{extra}")
        except Exception:
            pass

        setup_lines.append(f"• Bias confirm: {str(confirm_bias or 'UNKNOWN').upper()} | Participation regime: {regime}")
        if edge_val is not None:
            if prob_up is not None:
                setup_lines.append(f"• Signal edge: {edge_val:.2f} (prob_up={prob_up:.2f})")
            else:
                setup_lines.append(f"• Signal edge: {edge_val:.2f}")
        if getattr(pg, "impact", ""):
            setup_lines.append(f"• Breadth gate: {getattr(pg, 'impact', 'UNKNOWN')} ({getattr(pg, 'state', 'UNKNOWN')})")
    except Exception:
        setup_lines = []

    nxt = _next_macro_event_any(macro_events, now_et)
    if nxt:
        try:
            mins = float(nxt.get("delta_min") or 0.0)
        except Exception:
            mins = 0.0
        t = str(nxt.get("time_et") or "").strip()
        title = str(nxt.get("title") or "").strip() or "(macro event)"
        imp = str(nxt.get("impact") or "").strip()
        setup_lines.append(f"• Next macro: {t} — {impact_icon(imp)} {title} (in ~{mins:.0f}m)".strip())
    else:
        setup_lines.append("• Next macro: none scheduled remaining today (per calendar)")

    if headlines:
        setup_lines.append(f"• News heat: {recent_news}/{len(headlines)} material headlines in last {wow_news_window_min}m")

    setup_lines = setup_lines[:4]
    if setup_lines:
        lines.append("")
        lines.append("👑 Today’s Setup (conditions only)")
        lines.extend(setup_lines)

    lines.append("")
    lines.append("🗓️ Macro (ET)")
    if macro_banner:
        lines.append(macro_banner)
    if macro_events:
        for e in macro_events[:8]:
            t = (e.get("time_et") or "").strip()
            title = (e.get("title") or "").strip()
            imp = (e.get("impact") or "").strip()
            lines.append(f"• {t} — {impact_icon(imp)} {title}".strip())
    else:
        lines.append("• No scheduled macro releases found")

    lines.append("")
    lines.append("📰 Material Headlines (24h, cache-only)")
    if headlines:
        lines.extend(_format_headlines_compact(headlines, now_utc=now_utc, limit=headlines_limit))
    else:
        lines.append("• No material headlines in last 24h")

    lines.append("")
    lines.append("🧭 What To Watch (conditions only)")
    risk_item = macro_within_minutes(macro_events, 60, now_et)
    if risk_item:
        mins = float(risk_item.get("delta_min") or 0.0)
        lines.append(f"• High-impact macro in ~{mins:.0f}m — expect volatility + whipsaws")
    else:
        lines.append("• No high-impact macro in the next hour (per calendar)")
    lines.append("• If headlines hit mid-session, re-check conditions before adding risk")
    lines.append("• If conditions conflict, reduce size or stand down")

    lines.append("")
    lines.append("Context only • Not financial advice")

    section_data: Dict[str, Any] = {
        "date_et": today_et,
        "macro_events": macro_events,
        "macro_banner": macro_banner,
        "headlines": headlines,
        "symbols": symbols,
        "updated_utc": _now_utc().replace(tzinfo=timezone.utc).isoformat(),
    }

    extra_meta: Dict[str, Any] = {
        "date_et": today_et,
        "headlines_count": len(headlines or []),
        "macro_count": len(macro_events or []),
    }

    return _render_with_agent_context(
        lines,
        symbols=[],
        generated_at=now_et,
        post_type="daily_market_outlook",
        extra_meta=extra_meta,
        sections=section_data,
    )


def build_weekly_market_outlook_render(*, now_et: datetime | None = None) -> RenderedPost:
    """Once/week market outlook post.

    MVP-safe: reuse the Daily Outlook formatter (cache-only) but label it as weekly.
    """

    now_et = now_et or _now_et()
    base = build_daily_market_outlook_render(now_et=now_et)

    lines = (base.text or "").splitlines()
    if lines:
        lines[0] = "🗓️ Weekly Market Outlook"
        if len(lines) >= 2:
            lines[1] = f"🕒 Week of {now_et.strftime('%Y-%m-%d')} (ET)"

    payload = base.agent_payload if isinstance(base.agent_payload, dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if isinstance(meta, dict):
        meta["post_type"] = "weekly_market_outlook"
        meta["weekly"] = True
    payload["meta"] = meta

    return RenderedPost(text="\n".join(lines), agent_payload=payload, files=base.files)


async def automation_daily_market_outlook_loop(channel):
    await bot.wait_until_ready()

    times_s = (os.getenv("DAILY_OUTLOOK_TIMES_ET") or os.getenv("DAILY_OUTLOOK_TIME_ET") or "08:45").strip()
    poll_sec = _env_int("DAILY_OUTLOOK_POLL_SEC", "30")
    dedupe_ttl_sec = _env_int("DAILY_OUTLOOK_DEDUPE_TTL_SEC", str(36 * 3600))

    times: list[tuple[int, int]] = []
    for part in [p.strip() for p in times_s.split(",") if p.strip()]:
        try:
            hh, mm = _parse_hhmm(part)
            times.append((hh, mm))
        except Exception:
            continue
    if not times:
        times = [(8, 45)]

    times_label = ",".join([f"{h:02d}:{m:02d}" for (h, m) in times])
    print(
        "[OK] automation_daily_market_outlook_loop running "
        f"(times_et={times_label}, dedupe_ttl={dedupe_ttl_sec}s)"
    )

    while not bot.is_closed():
        try:
            if not _env_bool("DAILY_OUTLOOK_ENABLED", "0"):
                await asyncio.sleep(max(5, poll_sec))
                continue

            # Outlook is operational by default. Allow env to request a kind, but
            # publishing layer will still harden operational builders to status.
            outlook_kind = (os.getenv("TNT_OUTLOOK_KIND", "status") or "status").strip().lower() or "status"
            if outlook_kind == "analysis":
                print("[WARN] DAILY_OUTLOOK is operational; forcing kind=status (env requested analysis)")
                outlook_kind = "status"

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(max(5, poll_sec))
                continue

            target_date_et = now_et.strftime("%Y-%m-%d")

            posted_any = False
            for idx, (hh, mm) in enumerate(times, start=1):
                due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if now_et < due:
                    continue

                label = f"daily_outlook_{idx}"
                key = _automation_dedupe_key(label=label, target_date_et=target_date_et)
                if not _automation_should_post_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                ):
                    print(
                        f"[AUTOPOST][OUTLOOK][DAILY_OUTLOOK] deduped key={key} "
                        f"target_date_et={target_date_et} kind={outlook_kind}"
                    )

                    # Still emit a logs-only proof marker (cache-only) so operators
                    # can validate the live formatter without re-posting.
                    try:
                        render = await asyncio.to_thread(build_daily_market_outlook_render, now_et=now_et)
                        body = (render.text or "")
                        has_wow = int("⚡ OH WOW" in body)
                        has_setup = int("👑 Today’s Setup" in body)
                        payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
                        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                        headlines_n = int(meta.get("headlines_count") or 0) if isinstance(meta, dict) else 0
                        print(
                            f"[AUTOPOST][OUTLOOK][PROOF] kind={outlook_kind} wow={has_wow} setup={has_setup} headlines={headlines_n}"
                        )
                    except Exception:
                        pass

                    await asyncio.sleep(60)
                    continue

                print(
                    f"[AUTOPOST][OUTLOOK][DAILY_OUTLOOK] posting key={key} "
                    f"target_date_et={target_date_et} kind={outlook_kind}"
                )
                render = await asyncio.to_thread(build_daily_market_outlook_render, now_et=now_et)

                # Logs-only proof marker (avoid dumping full body).
                try:
                    body = (render.text or "")
                    has_wow = int("⚡ OH WOW" in body)
                    has_setup = int("👑 Today’s Setup" in body)
                    payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
                    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                    headlines_n = int(meta.get("headlines_count") or 0) if isinstance(meta, dict) else 0
                    print(
                        f"[AUTOPOST][OUTLOOK][PROOF] kind={outlook_kind} wow={has_wow} setup={has_setup} headlines={headlines_n}"
                    )
                except Exception:
                    pass
                publish_id = None
                try:
                    import uuid

                    publish_id = uuid.uuid4().hex[:10]
                except Exception:
                    publish_id = None
                try:
                    print(f"[AUTOPOST][OUTLOOK][DAILY_OUTLOOK] publish_id={publish_id} key={key} target_date_et={target_date_et}")
                except Exception:
                    pass
                try:
                    await _publish_autopost_render(
                        channel,
                        render,
                        builder="build_daily_market_outlook_render",
                        label=label,
                        symbol="",
                        publish_id=publish_id,
                        kind=outlook_kind,
                        context_mode="db",
                        context_output_mode="strict",
                    )
                except ContractViolationError as exc:
                    channel_id = getattr(channel, "id", None)
                    print(
                        f"[WARN] publish failed label={label} status=contract_violation publish_id={publish_id} "
                        f"channel_id={channel_id} channel_source=automation kind={outlook_kind} dedupe_key={key} "
                        f"exc_type={type(exc).__name__} exc={exc}"
                    )
                    if STRICT_CONTRACTS:
                        raise
                    await asyncio.sleep(max(5, poll_sec))
                    continue

                _automation_mark_posted_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                )
                print(f"[OK] daily_outlook posted label={label} target_date_et={target_date_et} publish_id={publish_id}")
                posted_any = True

            await asyncio.sleep(60 if posted_any else max(5, poll_sec))

        except ContractViolationError:
            # Contract violations are logged as proof-grade publish failures at the attempt level.
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(max(5, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_daily_market_outlook_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(5, poll_sec))


async def automation_weekly_market_outlook_loop(channel):
    await bot.wait_until_ready()

    day_et = _parse_weekday_et(os.getenv("WEEKLY_OUTLOOK_DAY_ET") or os.getenv("WEEKLY_OUTLOOK_DOW_ET"), fallback=6)
    times_s = (os.getenv("WEEKLY_OUTLOOK_TIMES_ET") or os.getenv("WEEKLY_OUTLOOK_TIME_ET") or "18:00").strip()
    poll_sec = _env_int("WEEKLY_OUTLOOK_POLL_SEC", "60")
    dedupe_ttl_sec = _env_int("WEEKLY_OUTLOOK_DEDUPE_TTL_SEC", str(8 * 24 * 3600))
    dedupe_scope = (os.getenv("WEEKLY_OUTLOOK_DEDUPE_SCOPE") or "date").strip().lower() or "date"
    if dedupe_scope in {"iso_week", "isoweek", "weekly"}:
        dedupe_scope = "week"
    if dedupe_scope not in {"date", "week"}:
        dedupe_scope = "date"

    times: list[tuple[int, int]] = []
    for part in [p.strip() for p in times_s.split(",") if p.strip()]:
        try:
            hh, mm = _parse_hhmm(part)
            times.append((hh, mm))
        except Exception:
            continue
    if not times:
        times = [(18, 0)]

    configured_day_label = "ANY" if day_et is None else ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][day_et]
    times_label = ",".join([f"{h:02d}:{m:02d}" for (h, m) in times])
    print(
        "[OK] automation_weekly_market_outlook_loop running "
        f"(day_et={configured_day_label}, times_et={times_label}, dedupe_scope={dedupe_scope}, dedupe_ttl={dedupe_ttl_sec}s)"
    )

    while not bot.is_closed():
        try:
            if not _env_bool("WEEKLY_OUTLOOK_ENABLED", "0"):
                await asyncio.sleep(max(5, poll_sec))
                continue

            outlook_kind = (os.getenv("TNT_OUTLOOK_KIND", "status") or "status").strip().lower() or "status"
            if outlook_kind == "analysis":
                print("[WARN] WEEKLY_OUTLOOK is operational; forcing kind=status (env requested analysis)")
                outlook_kind = "status"

            now_et = _now_et()
            if day_et is not None and now_et.weekday() != day_et:
                await asyncio.sleep(max(5, poll_sec))
                continue

            effective_day_et = now_et.weekday() if day_et is None else day_et
            day_label = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"][effective_day_et]

            if dedupe_scope == "week":
                iso = now_et.isocalendar()
                # ISO week token (week starts Monday). Keeps ANY-mode truly weekly.
                target_date_et = f"{int(iso.year)}-W{int(iso.week):02d}"
            else:
                target_date_et = now_et.strftime("%Y-%m-%d")
            posted_any = False

            for idx, (hh, mm) in enumerate(times, start=1):
                due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if now_et < due:
                    continue

                label = f"weekly_outlook_{idx}" if dedupe_scope == "week" else f"weekly_outlook_{day_label.lower()}_{idx}"
                key = _automation_dedupe_key(label=label, target_date_et=target_date_et)

                if not _automation_should_post_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                ):
                    print(
                        f"[AUTOPOST][WEEKLY_OUTLOOK] deduped key={key} "
                        f"target_date_et={target_date_et} kind={outlook_kind}"
                    )
                    try:
                        render = await asyncio.to_thread(build_weekly_market_outlook_render, now_et=now_et)
                        body = (render.text or "")
                        has_wow = int("⚡ OH WOW" in body)
                        has_setup = int("👑 Today’s Setup" in body)
                        payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
                        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                        headlines_n = int(meta.get("headlines_count") or 0) if isinstance(meta, dict) else 0
                        print(
                            f"[AUTOPOST][WEEKLY_OUTLOOK][PROOF] kind={outlook_kind} "
                            f"wow={has_wow} setup={has_setup} headlines={headlines_n}"
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(60)
                    continue

                print(
                    f"[AUTOPOST][WEEKLY_OUTLOOK] posting key={key} "
                    f"target_date_et={target_date_et} kind={outlook_kind}"
                )
                render = await asyncio.to_thread(build_weekly_market_outlook_render, now_et=now_et)

                try:
                    body = (render.text or "")
                    has_wow = int("⚡ OH WOW" in body)
                    has_setup = int("👑 Today’s Setup" in body)
                    payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
                    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                    headlines_n = int(meta.get("headlines_count") or 0) if isinstance(meta, dict) else 0
                    print(
                        f"[AUTOPOST][WEEKLY_OUTLOOK][PROOF] kind={outlook_kind} "
                        f"wow={has_wow} setup={has_setup} headlines={headlines_n}"
                    )
                except Exception:
                    pass

                publish_id = None
                try:
                    import uuid

                    publish_id = uuid.uuid4().hex[:10]
                except Exception:
                    publish_id = None
                try:
                    print(f"[AUTOPOST][WEEKLY_OUTLOOK] publish_id={publish_id} key={key} target_date_et={target_date_et}")
                except Exception:
                    pass
                await _publish_autopost_render(
                    channel,
                    render,
                    builder="build_weekly_market_outlook_render",
                    label=label,
                    symbol="",
                    publish_id=publish_id,
                    kind=outlook_kind,
                    context_mode="db",
                    context_output_mode="strict",
                )
                _automation_mark_posted_once_per_target_date(
                    label=label,
                    target_date_et=target_date_et,
                    ttl_sec=dedupe_ttl_sec,
                )
                print(f"[OK] weekly_outlook posted label={label} target_date_et={target_date_et} publish_id={publish_id}")
                posted_any = True

            await asyncio.sleep(60 if posted_any else max(5, poll_sec))

        except ContractViolationError as exc:
            print(f"[WARN] automation_weekly_market_outlook_loop contract violation: {exc}")
            if STRICT_CONTRACTS:
                raise
            await asyncio.sleep(max(5, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_weekly_market_outlook_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(5, poll_sec))


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


def _fmt_eps(v: float | None) -> str | None:
    if v is None:
        return None
    try:
        return f"${float(v):.2f}"
    except Exception:
        return None


def _fmt_money_compact(v: float | None) -> str | None:
    if v is None:
        return None
    try:
        x = float(v)
    except Exception:
        return None
    ax = abs(x)
    if ax >= 1_000_000_000:
        return f"${x/1_000_000_000:.2f}B"
    if ax >= 1_000_000:
        return f"${x/1_000_000:.1f}M"
    if ax >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:.0f}"


def fmt_earnings_expected_inline(rows: list[dict], limit: int = 12) -> str:
    parts: list[str] = []
    for r in (rows or [])[: max(1, int(limit))]:
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        eps_est = _fmt_eps(r.get("eps_est"))
        rev_est = _fmt_money_compact(r.get("rev_est"))
        confirmed = r.get("confirmed")

        meta: list[str] = []
        if eps_est is not None:
            meta.append(f"EPS {eps_est}")
        if rev_est is not None:
            meta.append(f"Rev {rev_est}")
        if confirmed is True:
            meta.append("confirmed")
        elif confirmed is False and (eps_est is not None or rev_est is not None):
            meta.append("projected")

        if meta:
            parts.append(f"{sym}{earnings_tag(sym)} ({', '.join(meta)})")
        else:
            parts.append(f"{sym}{earnings_tag(sym)}")

    return ", ".join(parts) if parts else "(none)"


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


def _extract_context_state(payload: dict) -> Optional[ContextState]:
    state = _extract_signal_state(payload)
    if not state:
        return None
    out: ContextState = dict(state)
    out["bias_confirm"] = str(payload.get("bias_confirm") or "").strip().upper()
    out["tnt_posture"] = str(payload.get("tnt_posture") or "").strip().upper()
    return out


def _should_post_context(sym: str, state: ContextState) -> tuple[bool, str]:
    cooldown = _env_int("AUTOPOST_CONTEXT_COOLDOWN_SEC", "600")
    now = int(_now_utc().timestamp())
    last = _last_context_post_by_symbol.get(sym, 0)
    if now - last < cooldown:
        return False, f"cooldown {cooldown}s"

    # Optional global throttle (context-only alerts), to avoid channel spam.
    max_per_hour = _env_int("AUTOPOST_CONTEXT_GLOBAL_MAX_PER_HOUR", "16")
    if max_per_hour > 0:
        cutoff = now - 3600
        while _context_global_post_ts and _context_global_post_ts[0] < cutoff:
            _context_global_post_ts.popleft()
        if len(_context_global_post_ts) >= max_per_hour:
            return False, f"global cap {max_per_hour}/h"

    prev = _last_context_state.get(sym)
    if not prev:
        post_on_seed = os.getenv("AUTOPOST_CONTEXT_POST_ON_SEED", "0") in {"1", "true", "yes", "on"}
        return (True, "seed") if post_on_seed else (False, "seed")

    prev_regime = str(prev.get("regime") or "").strip().upper()
    curr_regime = str(state.get("regime") or "").strip().upper()
    if prev_regime and curr_regime and prev_regime != curr_regime and curr_regime != "UNKNOWN":
        return True, "regime changed"

    try:
        if _has_invalidation_break(prev, state):
            return True, "pivot side changed"
    except Exception:  # noqa: BLE001
        pass

    prev_confirm = str(prev.get("bias_confirm") or "").strip().upper()
    curr_confirm = str(state.get("bias_confirm") or "").strip().upper()
    if prev_confirm and curr_confirm and prev_confirm != curr_confirm and curr_confirm != "UNKNOWN":
        return True, "confirmation changed"

    prev_posture = str(prev.get("tnt_posture") or "").strip().upper()
    curr_posture = str(state.get("tnt_posture") or "").strip().upper()
    if prev_posture and curr_posture and prev_posture != curr_posture and curr_posture != "UNKNOWN":
        return True, "posture changed"

    return False, "unchanged"


def _context_quiet_hours_active(*, now_et: datetime) -> bool:
    """Return True when context-only alerts should be suppressed due to quiet hours.

    Env: AUTOPOST_CONTEXT_QUIET_HOURS_ET="HH:MM-HH:MM" (wrap-around supported).
    Example: "20:00-07:00".
    """

    raw = str(os.getenv("AUTOPOST_CONTEXT_QUIET_HOURS_ET", "") or "").strip()
    if not raw or "-" not in raw:
        return False

    start_s, end_s = [p.strip() for p in raw.split("-", 1)]

    def _parse_hhmm_s(s: str) -> tuple[int, int] | None:
        try:
            hh, mm = [int(x) for x in s.split(":", 1)]
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh, mm
        except Exception:  # noqa: BLE001
            return None
        return None

    start = _parse_hhmm_s(start_s)
    end = _parse_hhmm_s(end_s)
    if not start or not end:
        return False

    now_min = int(now_et.hour) * 60 + int(now_et.minute)
    start_min = int(start[0]) * 60 + int(start[1])
    end_min = int(end[0]) * 60 + int(end[1])

    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= now_min < end_min

    # Wraps midnight.
    return now_min >= start_min or now_min < end_min


def _map_context_regime(payload: dict, *, mode: str) -> str:
    bias = str(payload.get("bias") or "").strip().upper()
    confirm = str(payload.get("bias_confirm") or "").strip().upper()
    posture = str(payload.get("tnt_posture") or "").strip().upper()
    edge = payload.get("edge")

    try:
        edge_f = float(edge)
    except Exception:  # noqa: BLE001
        edge_f = None

    min_edge = _edge_threshold_for_mode((mode or "strict").strip().lower())

    if posture in {"STAND_DOWN", "DO_NOTHING"}:
        return "BALANCED (No edge)"
    if confirm == "NEUTRAL":
        return "TRANSITION (Mixed confirmation)"

    if edge_f is None:
        return "BALANCED (No edge)"
    if edge_f < float(min_edge):
        return "BALANCED (No edge)"
    if bias == "BULL":
        return "BULLISH"
    if bias == "BEAR":
        return "BEARISH"
    return "BALANCED (No edge)"


def _context_confidence(payload: dict) -> str:
    dq = payload.get("data_quality") if isinstance(payload.get("data_quality"), dict) else {}
    tech = str(dq.get("technical_state") or "").strip().upper()
    age_min = payload.get("price_age_minutes")
    try:
        age_f = float(age_min) if age_min is not None else None
    except Exception:  # noqa: BLE001
        age_f = None

    if tech == "STALE":
        return "Low (snapshot stale)"
    if age_f is not None and age_f > 3:
        return "Low (price aging)"
    return "Medium (fresh snapshot; non-trade context)"


def _context_triggers(payload: dict) -> str:
    pivot = payload.get("pivot")
    r1 = payload.get("r1")
    s1 = payload.get("s1")

    def _f(v: object) -> str:
        try:
            return f"{float(v):.2f}"
        except Exception:  # noqa: BLE001
            return "n/a"

    p = _f(pivot)
    r = _f(r1)
    s = _f(s1)
    return f"Bull: reclaim+hold above Pivot {p} (then R1 {r}) | Bear: lose+hold below Pivot {p} (then S1 {s})"


async def build_context_alert_payload(sym: str, *, mode: str) -> Optional[str]:
    built = build_signal_payload(sym)
    if not built:
        return None

    payload, prob_up, gate = built
    mode_norm = (mode or "strict").strip().lower()
    mode_norm = mode_norm if mode_norm in VALID_MODES else "strict"

    state = _extract_context_state(payload)
    if not state:
        _last_context_state.pop(sym, None)
        return None

    # Evaluate against prior state BEFORE updating, otherwise changes never trigger.
    ok, reason = _should_post_context(sym, state)

    # Always keep the latest state cached for future comparisons.
    _last_context_state[sym] = state

    if not ok:
        return None

    now_et = _now_et()
    if _context_quiet_hours_active(now_et=now_et):
        return None

    # Non-trade context alert: always ends with Bottom line + Triggers + Confidence.
    edge_val = _edge(prob_up)
    min_edge = _edge_threshold_for_mode(mode_norm)
    confirm = str(payload.get("bias_confirm") or "").strip().upper()
    confirm_label = confirm if confirm in {"BULLISH", "BEARISH", "NEUTRAL"} else "MIXED"
    regime = _map_context_regime(payload, mode=mode_norm)
    conf = _context_confidence(payload)
    trig = _context_triggers(payload)

    now_utc_s = int(_now_utc().timestamp())
    _last_context_post_by_symbol[sym] = now_utc_s
    _context_global_post_ts.append(now_utc_s)

    title = str(os.getenv("AUTOPOST_CONTEXT_TITLE", "Market Posture Update") or "Market Posture Update").strip()
    if not title:
        title = "Market Posture Update"

    return (
        f"ℹ️ {title} — {sym} ({reason})\n"
        f"Regime: {regime}\n"
        "Bottom line: DO NOTHING\n"
        f"Triggers: {trig}\n"
        f"Confidence: {conf} (edge {edge_val:.3f} vs min {min_edge:.3f}; confirm {confirm_label})"
    )


def _resolve_ai_trade_journal_channel() -> Optional[object]:
    """Resolve the AI trade journal channel (if configured).

    Env (preferred): AI_TRADE_JOURNAL_CHANNEL_ID
    Fallback: JOURNAL_CHANNEL_ID (when JOURNAL_ENABLED=1)
    """

    try:
        cid = int(os.getenv("AI_TRADE_JOURNAL_CHANNEL_ID", "0") or "0")
    except Exception:
        cid = 0
    if cid <= 0:
        try:
            if _env_bool("JOURNAL_ENABLED", "0"):
                cid = int(os.getenv("JOURNAL_CHANNEL_ID", "0") or "0")
        except Exception:
            cid = 0
    if cid <= 0:
        return None
    try:
        return bot.get_channel(cid)
    except Exception:
        return None


def _should_post_divergence(*, key: str, now_s: int) -> tuple[bool, str]:
    cooldown = _env_int("AUTOPOST_DIVERGENCE_COOLDOWN_SEC", "900")
    last = int(_last_divergence_post_by_key.get(key, 0) or 0)
    if cooldown > 0 and (now_s - last) < int(cooldown):
        return False, f"cooldown {cooldown}s"

    max_per_hour = _env_int("AUTOPOST_DIVERGENCE_GLOBAL_MAX_PER_HOUR", "8")
    if max_per_hour > 0:
        cutoff = now_s - 3600
        while _divergence_global_post_ts and _divergence_global_post_ts[0] < cutoff:
            _divergence_global_post_ts.popleft()
        if len(_divergence_global_post_ts) >= max_per_hour:
            return False, f"global cap {max_per_hour}/h"

    return True, "ok"


def _divergence_watch_enabled() -> bool:
    # Legacy name (autopost) + new automation name.
    if _env_bool("AUTOPOST_DIVERGENCE_ENABLED", "0"):
        return True
    if _env_bool("TNT_DIVERGENCE_WATCH_ENABLED", "0"):
        return True
    return False


def _earnings_pressure_watch_enabled() -> bool:
    return _env_bool("TNT_EARNINGS_PRESSURE_WATCH_ENABLED", "0")


def _load_earnings_cache_payload(symbol: str) -> dict[str, object] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    try:
        r_ctx = redis_client_for_ctx()
    except Exception:
        r_ctx = None
    if r_ctx is None:
        return None
    try:
        raw = r_ctx.get(f"cal:earnings:{sym}")
    except Exception:
        raw = None
    payload = _decode_redis_json(raw)
    return payload if isinstance(payload, dict) else None


def _risk_regime_label(*, smiq_regime: str, vix_regime: str) -> str:
    # Tight, readable buckets for an earnings pre-watch.
    s = str(smiq_regime or "").strip().upper() or "UNKNOWN"
    v = str(vix_regime or "").strip().upper() or "UNKNOWN"
    if s == "DEFENSIVE" or v in {"HIGH", "HIGH / STRESS", "STRESS"}:
        return "DEFENSIVE"
    if s == "RISK_ON" and v in {"LOW", "LOW VOL", "NORMAL", "NORMAL VOL"}:
        return "RISK_ON"
    return "MIXED"


def _volatility_bucket(vix_regime: str) -> str:
    v = str(vix_regime or "").strip().upper() or "UNKNOWN"
    if v in {"LOW", "LOW VOL", "NORMAL", "NORMAL VOL"}:
        return "muted"
    if v in {"ELEVATED", "ELEVATED VOL"}:
        return "building"
    if v in {"HIGH", "HIGH / STRESS", "STRESS"}:
        return "elevated"
    return "building"


def _participation_slope_from_gate(impact: str) -> str:
    i = str(impact or "").strip().upper() or "UNKNOWN"
    if i == "ALLOW":
        return "rising"
    if i == "CAUTION":
        return "flat"
    if i == "STAND_DOWN":
        return "falling"
    return "flat"


def _price_state_from_hourly(symbol: str) -> str:
    st = _hourly_regime_state(symbol)
    if not st:
        return "range-bound"

    range_info = st.get("range_info")
    rs = range_info[0] if isinstance(range_info, (list, tuple)) and range_info else None

    dist = st.get("distance_vwap_pct")
    try:
        dist_f = abs(float(dist)) if dist is not None else None
    except Exception:
        dist_f = None

    if rs == "compressing":
        return "compressing"
    if rs == "expanding":
        # If stretched away from VWAP, call it extended; else it's just grinding.
        if dist_f is not None and dist_f >= 1.0:
            return "extended"
        return "grinding"
    return "range-bound"


def _pressure_bucket(*, bias: str, impact: str) -> str:
    b = str(bias or "").strip().upper() or "UNKNOWN"
    i = str(impact or "").strip().upper() or "UNKNOWN"
    if i in {"CAUTION", "STAND_DOWN"}:
        if b == "BULL":
            return "offer-dominant"
        if b == "BEAR":
            return "bid-dominant"
    return "neutral"


def _risk_bucket(*, implied_pct: float | None) -> str:
    if implied_pct is None:
        return "MODERATE"
    try:
        x = float(implied_pct)
    except Exception:
        return "MODERATE"
    if x >= 8.0:
        return "HIGH"
    if x >= 4.0:
        return "MODERATE"
    return "LOW"


async def build_earnings_pressure_watch_payload(symbol: str) -> str | None:
    """Build a context-only Earnings Pressure Watch post.

    Cache-first: uses `cal:earnings:{SYM}` if available.
    Falls back gracefully when any component is missing.
    """

    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    if not _earnings_pressure_watch_enabled():
        return None

    now_et = _now_et()
    if _context_quiet_hours_active(now_et=now_et):
        return None

    now_utc = now_et.astimezone(timezone.utc)

    payload = _load_earnings_cache_payload(sym)
    if not payload:
        return None

    dt_utc = _parse_dt_maybe_utc(payload.get("ts_utc"))
    if dt_utc is None:
        return None
    dt_et = dt_utc.astimezone(ET)

    # Only post inside a lookahead window.
    lookahead_h = float(_positive_env_float("TNT_EARNINGS_PRESSURE_LOOKAHEAD_HOURS", "36"))
    delta_h = (dt_et - now_et).total_seconds() / 3600.0
    if delta_h < 0 or delta_h > lookahead_h:
        return None

    target_date_et = dt_et.date().isoformat()
    key = f"tnt:earnings_pressure:{sym}:{target_date_et}"
    now_s = int(now_utc.timestamp())
    cooldown_s = int(_env_int("TNT_EARNINGS_PRESSURE_COOLDOWN_SEC", str(12 * 3600)))
    last_s = int(_last_earnings_pressure_post_by_key.get(key, 0) or 0)
    if (now_s - last_s) < max(300, cooldown_s):
        return None

    # Market participation regime acts as the macro "sponsorship" proxy.
    try:
        sig = await build_signal_payload(sym)
    except Exception:
        sig = None
    bias = str((sig or {}).get("bias") or "NEUTRAL").strip().upper()
    edge = None
    try:
        if isinstance((sig or {}).get("edge"), (int, float)):
            edge = float((sig or {})["edge"])  # type: ignore[index]
    except Exception:
        edge = None

    smiq, pg_obj = _resolve_market_participation(bias if bias in {"BULL", "BEAR"} else None, edge)
    pg = _gate_result_to_dict(pg_obj)
    smiq_regime = str(smiq.get("regime") or "UNKNOWN").strip().upper()
    impact = str(pg.get("impact") or "UNKNOWN").strip().upper()
    reason = str(pg.get("reason") or "").strip()

    # Implied move (%): prefer cached expected_move_pct.
    implied_pct = None
    try:
        if payload.get("expected_move_pct") is not None:
            implied_pct = float(payload.get("expected_move_pct"))
    except Exception:
        implied_pct = None
    implied_s = "n/a" if implied_pct is None else f"±{abs(implied_pct):.1f}%"

    # VIX regime → volatility bucket.
    vix_ctx = get_vix_context("1m")
    vix_regime = str((vix_ctx or {}).get("regime") or "UNKNOWN").strip().upper()

    # Price + participation + pressure
    price_state = _price_state_from_hourly(sym)
    participation_slope = _participation_slope_from_gate(impact)
    pressure = _pressure_bucket(bias=bias, impact=impact)
    vol_bucket = _volatility_bucket(vix_regime)

    divergence = (bias in {"BULL", "BEAR"}) and (impact in {"CAUTION", "STAND_DOWN"})
    divergence_s = "YES" if divergence else "NO"

    div_type = "None"
    if divergence:
        if bias == "BULL":
            div_type = "Price ↑ / Participation ↓"
        elif bias == "BEAR":
            div_type = "Price flat / Pressure ↑"

    # Reaction bias (not a trade call).
    base_risk = _risk_bucket(implied_pct=implied_pct)
    up_risk = base_risk
    down_risk = base_risk

    if divergence and bias == "BULL":
        up_risk = "LOW" if base_risk != "HIGH" else "MODERATE"
        down_risk = "HIGH"
    elif divergence and bias == "BEAR":
        up_risk = "HIGH"
        down_risk = "LOW" if base_risk != "HIGH" else "MODERATE"

    if vix_regime in {"HIGH", "HIGH / STRESS", "STRESS"}:
        # In stress, widen both tails by one notch.
        if up_risk == "LOW":
            up_risk = "MODERATE"
        if down_risk == "LOW":
            down_risk = "MODERATE"
        if up_risk == "MODERATE" and base_risk == "HIGH":
            up_risk = "HIGH"
        if down_risk == "MODERATE" and base_risk == "HIGH":
            down_risk = "HIGH"

    risk_regime = _risk_regime_label(smiq_regime=smiq_regime, vix_regime=vix_regime)

    # Event label.
    event_date = dt_et.strftime("%Y-%m-%d")
    event_time = dt_et.strftime("%H:%M ET")
    # If provider supplied a coarse when (BMO/AMC/TAS), surface it.
    when = str(payload.get("when") or payload.get("session") or "").strip().upper()
    when_s = f" ({when})" if when in {"BMO", "AMC", "TAS"} else ""

    footer_on = _env_bool("TNT_EARNINGS_PRESSURE_FOOTER", "1")

    lines: list[str] = []
    lines.append("EARNINGS PRESSURE WATCH")
    lines.append("")
    lines.append(f"Symbol: {sym}")
    lines.append(f"Event: Earnings {event_date} {event_time}{when_s}")
    lines.append(f"Implied Move: {implied_s}")
    lines.append(f"Regime: {risk_regime}")
    lines.append("")
    lines.append("Pre-Earnings Market State")
    lines.append("")
    lines.append(f"Price: {price_state}")
    lines.append(f"Participation: {participation_slope}")
    lines.append(f"Pressure: {pressure}")
    lines.append(f"Volatility: {vol_bucket}")
    lines.append("")
    lines.append("Pressure Assessment")
    lines.append("")
    lines.append(f"Divergence detected: {divergence_s}")
    lines.append(f"Type: {div_type}")
    if reason:
        lines.append(f"Gate note: {reason}")
    lines.append("")
    lines.append("This does not predict direction.")
    lines.append("It identifies stored pressure going into the earnings catalyst.")
    lines.append("")
    lines.append("Reaction Bias (Not a Trade Call)")
    lines.append("")
    lines.append(f"Upside reaction risk: {up_risk}")
    lines.append(f"Downside reaction risk: {down_risk}")
    lines.append("")
    lines.append("Bias reflects how price is likely to react,")
    lines.append("not where it “should” go.")
    lines.append("")
    lines.append("Trader Guidance")
    lines.append("")
    lines.append("Expect faster-than-normal resolution")
    lines.append("Avoid pre-event over-positioning")
    lines.append("Post-earnings confirmation > prediction")
    lines.append("")
    lines.append("TNT Interpretation")
    lines.append("")
    lines.append("Earnings act as a stress test on existing structure.")
    lines.append("When pressure exists, reactions tend to be asymmetric.")
    if footer_on:
        lines.append("")
        lines.append("Context alert • No trade issued • Monitor post-earnings follow-through")

    msg = "\n".join(lines).strip()

    # Basic contract sanity (avoid noisy failures).
    try:
        violations = contract_violations(msg, max_chars=DEFAULT_MAX_CHARS_DEFAULT, max_lines=DEFAULT_MAX_LINES)
        if violations:
            return None
    except Exception:
        pass

    _last_earnings_pressure_post_by_key[key] = now_s
    return msg


async def automation_earnings_pressure_watch_loop(channel: object) -> None:
    await bot.wait_until_ready()

    poll_sec = float(_positive_env_float("TNT_EARNINGS_PRESSURE_POLL_SEC", "120"))
    symbols = [s.strip().upper() for s in (os.getenv("TNT_EARNINGS_PRESSURE_SYMBOLS") or os.getenv("EARNINGS_WATCHLIST") or "").split(",") if s.strip()]
    if not symbols:
        symbols = ["SPY"]

    print(f"[OK] automation_earnings_pressure_watch_loop running (poll_sec={poll_sec:.0f} symbols={symbols})")

    while not bot.is_closed():
        try:
            if not _automation_enabled() or not _earnings_pressure_watch_enabled():
                await asyncio.sleep(max(30.0, poll_sec))
                continue

            out_ch = _resolve_ai_trade_journal_channel() or channel

            posted_any = False
            for sym in symbols:
                msg = await build_earnings_pressure_watch_payload(sym)
                if not msg:
                    continue
                try:
                    await safe_send(
                        out_ch,
                        msg,
                        kind="status",
                        symbol=sym,
                        pivots=None,
                        analysis_mode="db",
                        output_mode="strict",
                        label="earnings_pressure_watch",
                    )
                    posted_any = True
                except Exception:
                    pass

            await asyncio.sleep(max(30.0, poll_sec) if posted_any else max(60.0, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_earnings_pressure_watch_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(60.0, poll_sec))


async def build_divergence_watch_payload(sym: str, *, mode: str) -> Optional[str]:
    """Return a lightweight divergence watch alert (context-only).

    Today this uses TNT's market participation gate as a proxy for
    "price/bias vs sponsorship" divergence. It is intentionally conservative
    and spam-protected.
    """

    if not _divergence_watch_enabled():
        return None

    built = build_signal_payload(sym)
    if not built:
        return None

    payload, prob_up, _gate = built
    bias = str(payload.get("bias") or "").strip().upper()
    if bias not in {"BULL", "BEAR"}:
        return None

    edge_val = _edge(float(prob_up))
    mode_norm = (mode or "strict").strip().lower()
    min_edge = _edge_threshold_for_mode(mode_norm)

    # If we're running in strict mode, still allow divergence watches as long
    # as they clear the insights floor.
    if edge_val < float(AUTOPOST_EDGE_MIN_INSIGHTS):
        return None

    smiq, pg_obj = _resolve_market_participation(bias, edge_val)
    pg = _gate_result_to_dict(pg_obj)
    regime = str((smiq.get("regime") if isinstance(smiq, dict) else "UNKNOWN") or "UNKNOWN").strip().upper()
    quality = str((smiq.get("data_quality") if isinstance(smiq, dict) else "UNKNOWN") or "UNKNOWN").strip().upper()
    impact = str((pg.get("impact") if isinstance(pg, dict) else "UNKNOWN") or "UNKNOWN").strip().upper()
    reason = str((pg.get("reason") if isinstance(pg, dict) else "") or "").strip()

    # Avoid alerting on missing/stale participation snapshots.
    if quality in {"MISSING", "STALE", "PARTIAL", "UNKNOWN"} and regime == "UNKNOWN":
        return None

    if impact not in {"CAUTION", "STAND_DOWN"}:
        return None

    # Classify divergence direction.
    if bias == "BULL":
        headline = "Price/bias bullish, participation weak"
        side = "bearish"
    else:
        headline = "Price/bias bearish, participation supportive"
        side = "bullish"

    now_et = _now_et()
    if _context_quiet_hours_active(now_et=now_et):
        return None

    now_s = int(_now_utc().timestamp())
    key = f"{sym}:{side}:{regime}:{impact}"
    ok, why = _should_post_divergence(key=key, now_s=now_s)
    if not ok:
        return None

    _last_divergence_post_by_key[key] = now_s
    _divergence_global_post_ts.append(now_s)

    title = str(os.getenv("AUTOPOST_DIVERGENCE_TITLE", "Divergence Watch") or "Divergence Watch").strip() or "Divergence Watch"
    edge_note = f"edge {edge_val:.3f} vs min {float(min_edge):.3f}" if min_edge is not None else f"edge {edge_val:.3f}"

    # Keep message compact and unmistakably "insight".
    return (
        f"⚠️ {title} ({payload.get('tf_exec') or '1m'}) — {sym} ({why})\n"
        f"{headline}\n"
        f"Participation: {regime} (quality {quality}, gate {impact})\n"
        f"Why: {reason or 'n/a'}\n"
        f"Context only — not a trade signal. ({edge_note})"
    )


async def automation_divergence_watch_loop(channel: object) -> None:
    """Periodic divergence watch as an INSIGHT post.

    This runs under the TNT automation scheduler (not the legacy autopost).
    """

    await bot.wait_until_ready()
    poll_sec = float(_positive_env_float("TNT_DIVERGENCE_WATCH_POLL_SEC", "60"))
    mode_setting = (os.getenv("TNT_DIVERGENCE_WATCH_MODE") or "insights").strip().lower() or "insights"
    symbols = [s.strip().upper() for s in (os.getenv("TNT_DIVERGENCE_WATCH_SYMBOLS", "SPY") or "SPY").split(",") if s.strip()]
    if not symbols:
        symbols = ["SPY"]

    print(f"[OK] automation_divergence_watch_loop running (poll_sec={poll_sec:.0f} symbols={symbols} mode={mode_setting})")

    while not bot.is_closed():
        try:
            if not _automation_enabled() or not _divergence_watch_enabled():
                await asyncio.sleep(max(15.0, poll_sec))
                continue

            # Route to journal when available; otherwise fall back to the provided channel.
            out_ch = _resolve_ai_trade_journal_channel() or channel

            posted = False
            for sym in symbols:
                msg = await build_divergence_watch_payload(sym, mode=mode_setting)
                if not msg:
                    continue
                try:
                    await safe_send(
                        out_ch,
                        msg,
                        kind="status",
                        symbol=sym,
                        pivots=None,
                        analysis_mode="db",
                        output_mode="strict",
                        label="divergence_watch",
                    )
                    posted = True
                except Exception:
                    pass
                break

            # Even when nothing posts, keep polling.
            await asyncio.sleep(max(15.0, poll_sec) if not posted else max(30.0, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_divergence_watch_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(30.0, poll_sec))


def _get_stale_lock() -> asyncio.Lock:
    global _data_stale_lock
    if _data_stale_lock is None:
        _data_stale_lock = asyncio.Lock()
    return _data_stale_lock


def _get_feed_stale_lock() -> asyncio.Lock:
    global _feed_stale_lock
    if _feed_stale_lock is None:
        _feed_stale_lock = asyncio.Lock()
    return _feed_stale_lock


def _format_age_compact(age_s: Optional[float]) -> str:
    if age_s is None:
        return "unknown"
    try:
        a = max(0, int(float(age_s)))
    except Exception:
        return "unknown"
    if a < 90:
        return f"{a}s"
    if a < 3600:
        return f"{a // 60}m"
    return f"{a // 3600}h{(a % 3600) // 60:02d}m"


def _calc_feed_stale() -> tuple[bool, list[str], dict[str, dict[str, object]]]:
    """Return (required_stale, stale_feeds, details) for hb:* feeds."""

    try:
        from services.observability.feed_heartbeat import summarize_feeds

        _line, details = summarize_feeds()
    except Exception:
        return True, ["feeds_unavailable"], {}

    stale: list[str] = []
    required_stale = False
    for name, meta in details.items():
        status = str(meta.get("status") or "").upper().strip()
        req = bool(meta.get("required"))
        if status in {"OK"}:
            continue
        stale.append(name)
        if req:
            required_stale = True
    return required_stale, stale, details


async def _update_feed_stale_state() -> bool:
    """Return True if required feeds are stale and automation should pause."""

    lock = _get_feed_stale_lock()
    async with lock:
        global _feed_stale_paused, _feed_stale_initialized

        required_stale, stale_feeds, details = _calc_feed_stale()
        first_check = not _feed_stale_initialized
        _feed_stale_initialized = True

        if stale_feeds:
            if not _feed_stale_paused:
                _feed_stale_paused = True

                viol: list[str] = []
                for f in stale_feeds[:8]:
                    meta = details.get(f) or {}
                    age_s = meta.get("age_s")
                    status = str(meta.get("status") or "").upper().strip() or "UNKNOWN"
                    req = bool(meta.get("required"))
                    tag = f"{f}{'*' if req else ''}"
                    viol.append(f"{tag} {status} age={_format_age_compact(age_s)}")

                msg = "🚨 Feed stale: " + "; ".join(viol)
                if required_stale:
                    msg += " | Alerts disabled until fresh."

                # Even on first check, we want this loud in canary/ops.
                await _send_stale_notification(msg, key="feeds:stale")
            return bool(required_stale)

        if _feed_stale_paused:
            _feed_stale_paused = False
            msg = "✅ Feeds fresh again. Alerts re-enabled." if required_stale is False else "✅ Feeds updated."
            await _send_stale_notification(msg, key="feeds:fresh")
        return False


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
        # Even if bar-level stale checking is disabled, we still want feed-level
        # heartbeat gating for canary/ops safety.
        return await _update_feed_stale_state()

    data_stale = await _update_data_stale_state()
    feed_stale = await _update_feed_stale_state()
    return bool(data_stale or feed_stale)


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
            try:
                win = _env_int("TNT_ALERT_NEWS_CONTEXT_WINDOW_MIN", "120")
            except Exception:
                win = 120
            ai_text = _append_recent_news_context_line(ai_text, symbol=sym, window_min=int(win))
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
    try:
        win = _env_int("TNT_ALERT_NEWS_CONTEXT_WINDOW_MIN", "120")
    except Exception:
        win = 120
    fallback = _append_recent_news_context_line(fallback, symbol=sym, window_min=int(win))
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

                # Divergence watch (context-only): prefer routing to the AI trade journal.
                div_msg = await build_divergence_watch_payload(sym, mode=mode_setting)
                if div_msg:
                    journal = _resolve_ai_trade_journal_channel() or channel
                    await safe_send(
                        journal,
                        div_msg,
                        kind="status",
                        symbol=sym,
                        pivots=None,
                        analysis_mode="db",
                        output_mode="strict",
                        label="divergence_watch",
                    )
                    posted = True
                    break

                # Non-trade context alert fallback (keeps channel active even when gated).
                ctx_msg = await build_context_alert_payload(sym, mode=mode_setting)
                if ctx_msg:
                    await safe_send(
                        channel,
                        ctx_msg,
                        kind="status",
                        symbol=sym,
                        pivots=None,
                        analysis_mode="db",
                        output_mode="strict",
                        label="context_alert",
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

    Safety: requires explicit opt-in via `TNT_AUTOMATION_ENABLED=1`.
    This prevents unrelated legacy autopost flags from accidentally enabling
    scheduled posts.
    """

    return _env_bool("TNT_AUTOMATION_ENABLED", "0")


def _macro_calendar_refresh_enabled() -> bool:
    """Enable macro calendar refresh from Massive (Benzinga economic calendar).

    Default OFF; must be explicitly enabled.
    """

    if _env_bool("TNT_MACRO_CALENDAR_REFRESH_ENABLED", "0"):
        return True
    if _env_bool("MACRO_CALENDAR_REFRESH_ENABLED", "0"):
        return True
    return False


def _is_stand_down_render(render: RenderedPost) -> bool:
    try:
        payload = render.agent_payload if isinstance(render.agent_payload, dict) else {}
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        if isinstance(meta, dict) and bool(meta.get("stand_down")):
            return True
    except Exception:
        pass

    txt = (render.text or "").strip().upper()
    return txt.startswith("⚠️ STAND DOWN") or txt == PREFLIGHT_INTEGRITY_STANDDOWN_TEXT.strip().upper()


def _paper_desk_enabled() -> bool:
    """Master toggle for Paper Desk-style scheduled posts.

    Default OFF; must be explicitly enabled.
    """

    # Canonical name is `TNT_PAPER_DESK_ENABLED`, but older configs (including
    # this repo's `.env.local`) used `PAPERDESK_ENABLED`.
    # Treat the legacy name as an alias to avoid silently disabling Paper Desk.
    if os.getenv("TNT_PAPER_DESK_ENABLED") is not None:
        return _env_bool("TNT_PAPER_DESK_ENABLED", "0")
    if os.getenv("PAPERDESK_ENABLED") is not None:
        return _env_bool("PAPERDESK_ENABLED", "0")
    if os.getenv("PAPER_DESK_ENABLED") is not None:
        return _env_bool("PAPER_DESK_ENABLED", "0")
    return False


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


def _resolve_earnings_calendar_channel():
    """Resolve the destination channel for earnings calendar posts.

    Preference order:
    - CALENDAR_EARNINGS_CHANNEL_ID (explicit)
    - EARNINGS_POST_CHANNEL_ID (legacy/alias)
    - AUTOPOST_CHANNEL_ID (fallback)
    - CANARY_CHANNEL_ID (last resort)
    """

    canary_id = CANARY_CHANNEL_ID
    calendar_id = _env_int("CALENDAR_EARNINGS_CHANNEL_ID", "0")
    earnings_post_id = _env_int("EARNINGS_POST_CHANNEL_ID", "0")
    autopost_id = _env_int("AUTOPOST_CHANNEL_ID", "0")

    channel_id = int(calendar_id or earnings_post_id or autopost_id or canary_id or 0)
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        print(
            "[WARN] earnings_calendar: channel not found/invalid "
            f"id={channel_id} (calendar={calendar_id} earnings_post={earnings_post_id} autopost={autopost_id} canary={canary_id})"
        )
        return None
    return channel


def _resolve_daily_outlook_channel():
    """Resolve destination channel for Daily Market Outlook.

    Preference order:
    - DAILY_OUTLOOK_CHANNEL_ID
    - AUTOPOST_CHANNEL_ID
    - CANARY_CHANNEL_ID
    """

    canary_id = CANARY_CHANNEL_ID
    outlook_id = _env_int("DAILY_OUTLOOK_CHANNEL_ID", "0")
    autopost_id = _env_int("AUTOPOST_CHANNEL_ID", "0")

    channel_id = int(outlook_id or autopost_id or canary_id or 0)
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        print(
            "[WARN] daily_outlook: channel not found/invalid "
            f"id={channel_id} (daily_outlook={outlook_id} autopost={autopost_id} canary={canary_id})"
        )
        return None
    return channel


def _redact_url(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    # Redact userinfo/password in URLs like redis://:pass@host:6379/0
    try:
        if "://" in s and "@" in s:
            scheme, rest = s.split("://", 1)
            userinfo, tail = rest.split("@", 1)
            if ":" in userinfo:
                return f"{scheme}://***:***@{tail}"
            return f"{scheme}://***@{tail}"
    except Exception:
        pass
    return s


def _log_startup_banner() -> None:
    try:
        parts: list[str] = []
        parts.append("[TNT][CONFIG] delivery bot startup")
        parts.append(f"[TNT][CONFIG] QUALITY_GATE_ENABLED={int(bool(QUALITY_GATE_ENABLED))} STRICT_CONTRACTS={int(bool(STRICT_CONTRACTS))} RATE_QUEUE_ENABLED={int(bool(_RATE_QUEUE_ENABLED))}")

        # Automation (scheduled) flags.
        parts.append(
            "[TNT][CONFIG] automation="
            f"enabled:{int(_env_bool('TNT_AUTOMATION_ENABLED','0'))} "
            f"paper_desk:{int(_paper_desk_enabled())} "
            f"focus:{int(_env_bool('TNT_FOCUS_LIST_ENABLED','0'))} "
            f"intraday:{int(_env_bool('TNT_INTRADAY_UPDATE_ENABLED','0'))} "
            f"recap:{int(_env_bool('TNT_RECAP_ENABLED','0'))} "
            f"heartbeat:{int(_env_bool('TNT_HEARTBEAT_ENABLED','0'))}"
        )

        # Debug: show which paper desk flag source is present.
        paperdesk_src = "default"
        if os.getenv("TNT_PAPER_DESK_ENABLED") is not None:
            paperdesk_src = "TNT_PAPER_DESK_ENABLED"
        elif os.getenv("PAPERDESK_ENABLED") is not None:
            paperdesk_src = "PAPERDESK_ENABLED"
        elif os.getenv("PAPER_DESK_ENABLED") is not None:
            paperdesk_src = "PAPER_DESK_ENABLED"
        parts.append(f"[TNT][CONFIG] paper_desk_flag_source={paperdesk_src}")

        parts.append(
            "[TNT][CONFIG] standdown_dedupe="
            + str(_env_int('TNT_AUTOMATION_STANDDOWN_DEDUPE_SEC', '3600'))
            + "s"
        )

        # Legacy autopost flags (left visible for debugging).
        parts.append(
            "[TNT][CONFIG] legacy_autopost="
            f"enabled:{int(_env_bool('AUTOPOST_ENABLED','0'))} "
            f"daily:{int(_env_bool('AUTOPOST_DAILY_ENABLED','0'))} "
            f"signal:{int(_env_bool('AUTOPOST_SIGNAL_ENABLED','0'))}"
        )

        # Premium posting.
        parts.append(f"[TNT][CONFIG] triple_expiry_posting_enabled={int(_env_bool('TNT_TRIPLE_EXPIRY_ENABLED','0'))}")
        parts.append(f"[TNT][CONFIG] earnings_autopost_enabled={int(_env_bool('EARNINGS_AUTOPOST_ENABLED','0'))}")
        parts.append(f"[TNT][CONFIG] earnings_daily_time_et={os.getenv('TNT_EARNINGS_DAILY_TIME_ET','18:00')}")
        parts.append(f"[TNT][CONFIG] earnings_results_autopost_enabled={int(_env_bool('EARNINGS_RESULTS_AUTOPOST_ENABLED','0'))}")
        parts.append(
            "[TNT][CONFIG] earnings_results_times_et="
            + str(
                (os.getenv('TNT_EARNINGS_RESULTS_TIMES_ET') or os.getenv('TNT_EARNINGS_RESULTS_TIME_ET') or '18:10').strip()
            )
        )
        parts.append(f"[TNT][CONFIG] earnings_provider={os.getenv('EARNINGS_PROVIDER','earningsapi')}")
        parts.append(f"[TNT][CONFIG] earnings_watchlist={(os.getenv('EARNINGS_WATCHLIST','') or '').strip()}")
        parts.append(f"[TNT][CONFIG] earnings_api_key_set={bool(_resolve_earnings_api_key())}")
        parts.append(f"[TNT][CONFIG] earnings_api_key_source={_resolve_earnings_api_key_source()}")
        parts.append(f"[TNT][CONFIG] macro_events_file={MACRO_EVENTS_FILE}")
        parts.append(f"[TNT][CONFIG] macro_refresh_enabled={int(_macro_calendar_refresh_enabled())}")
        parts.append(f"[TNT][CONFIG] macro_refresh_days_ahead={_env_int('TNT_MACRO_CALENDAR_DAYS_AHEAD', '21')}")
        parts.append(f"[TNT][CONFIG] macro_refresh_countries={(os.getenv('TNT_MACRO_CALENDAR_COUNTRIES','US') or 'US').strip()}")

        # Routing.
        canary = CANARY_CHANNEL_ID
        autopost_chan = _env_int('AUTOPOST_CHANNEL_ID', '0')
        calendar_chan = _env_int('CALENDAR_EARNINGS_CHANNEL_ID', '0')
        earnings_post_chan = _env_int('EARNINGS_POST_CHANNEL_ID', '0')
        daily_outlook_chan = _env_int('DAILY_OUTLOOK_CHANNEL_ID', '0')
        ops_chan = BOT_ALERT_CHANNEL_ID
        parts.append(
            f"[TNT][CONFIG] channels=canary:{canary or 0} autopost:{autopost_chan} "
            f"daily_outlook:{daily_outlook_chan} "
            f"calendar_earnings:{calendar_chan} earnings_post:{earnings_post_chan} ops:{ops_chan or 0}"
        )

        # Outlook polish knobs (logs-only proof of config).
        outlook_kind = (os.getenv('TNT_OUTLOOK_KIND', 'status') or 'status').strip().lower() or 'status'
        parts.append(f"[TNT][CONFIG] outlook_kind={outlook_kind}")
        parts.append(f"[TNT][CONFIG] outlook_headlines_limit={_env_int('TNT_OUTLOOK_HEADLINES_LIMIT', '4')}")
        parts.append(f"[TNT][CONFIG] outlook_wow_news_window_min={_env_int('TNT_OUTLOOK_WOW_NEWS_WINDOW_MIN', '90')}")
        parts.append(f"[TNT][CONFIG] outlook_wow_earnings_thresh={_env_int('TNT_OUTLOOK_WOW_EARNINGS_THRESH', '3')}")

        # Weekly Outlook schedule (logs-only proof of config).
        parts.append(f"[TNT][CONFIG] weekly_outlook_enabled={int(_env_bool('WEEKLY_OUTLOOK_ENABLED','0'))}")
        parts.append(
            "[TNT][CONFIG] weekly_outlook_day_et="
            + str((os.getenv('WEEKLY_OUTLOOK_DAY_ET') or os.getenv('WEEKLY_OUTLOOK_DOW_ET') or 'SUN').strip())
        )
        parts.append(
            "[TNT][CONFIG] weekly_outlook_times_et="
            + str((os.getenv('WEEKLY_OUTLOOK_TIMES_ET') or os.getenv('WEEKLY_OUTLOOK_TIME_ET') or '18:00').strip())
        )
        parts.append(
            "[TNT][CONFIG] weekly_outlook_dedupe_scope="
            + str((os.getenv('WEEKLY_OUTLOOK_DEDUPE_SCOPE') or 'date').strip())
        )

        # Weekly Outlook computed dedupe key preview (logs-only proof, no posting).
        # This prints BOTH a "now" preview and a "next scheduled due" preview to avoid confusion
        # when e.g. WEEKLY_OUTLOOK_DAY_ET=SUN but today is mid-week.
        try:
            now_et = _now_et()

            raw_scope = (os.getenv('WEEKLY_OUTLOOK_DEDUPE_SCOPE') or 'date').strip().lower() or 'date'
            scope = 'week' if raw_scope in {'week', 'iso_week', 'isoweek', 'weekly'} else 'date'

            day_cfg_raw = os.getenv('WEEKLY_OUTLOOK_DAY_ET') or os.getenv('WEEKLY_OUTLOOK_DOW_ET')
            day_cfg = _parse_weekday_et(day_cfg_raw, fallback=6)

            times_s = (os.getenv('WEEKLY_OUTLOOK_TIMES_ET') or os.getenv('WEEKLY_OUTLOOK_TIME_ET') or '18:00').strip()
            times: list[tuple[int, int]] = []
            for part in [p.strip() for p in str(times_s).split(',') if p.strip()]:
                try:
                    hh, mm = _parse_hhmm(part)
                    times.append((hh, mm))
                except Exception:
                    continue
            if not times:
                times = [(18, 0)]
            times.sort()

            def _target_token(dt_et: datetime) -> str:
                if scope == 'week':
                    iso = dt_et.isocalendar()
                    return f"{int(iso.year)}-W{int(iso.week):02d}"
                return dt_et.strftime('%Y-%m-%d')

            def _key_preview_for(dt_et: datetime) -> tuple[str, str]:
                effective_day = dt_et.weekday() if day_cfg is None else int(day_cfg)
                day_label_local = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'][effective_day]
                target = _target_token(dt_et)

                keys: list[str] = []
                for idx in range(1, len(times) + 1):
                    label = f"weekly_outlook_{idx}" if scope == 'week' else f"weekly_outlook_{day_label_local.lower()}_{idx}"
                    keys.append(_automation_dedupe_key(label=label, target_date_et=target))

                preview_local = ','.join(keys[:3])
                if len(keys) > 3:
                    preview_local = f"{preview_local},+{len(keys) - 3} more"
                return preview_local, target

            # "Now" preview (purely informational).
            preview_now, target_now = _key_preview_for(now_et)
            parts.append(f"[TNT][CONFIG] weekly_outlook_dedupe_key_preview_now={preview_now}")

            # "Next scheduled due" preview: the next future occurrence of the configured schedule.
            # - If DAY=ANY: next future time today, else tomorrow at first time.
            # - If DAY fixed: next occurrence of that weekday at the earliest future time, else next week.
            due_ref = now_et.replace(second=0, microsecond=0)

            if day_cfg is None:
                # ANY: daily schedule
                candidate = None
                for (hh, mm) in times:
                    dt = due_ref.replace(hour=hh, minute=mm)
                    if dt > now_et:
                        candidate = dt
                        break
                if candidate is None:
                    hh, mm = times[0]
                    candidate = (due_ref + timedelta(days=1)).replace(hour=hh, minute=mm)
                next_due = candidate
            else:
                # Fixed weekday
                target_wd = int(day_cfg)
                today_wd = due_ref.weekday()
                days_ahead = (target_wd - today_wd) % 7
                base_day = due_ref + timedelta(days=days_ahead)

                candidate = None
                # If the base day is today, pick the next future time; otherwise pick earliest.
                if days_ahead == 0:
                    for (hh, mm) in times:
                        dt = base_day.replace(hour=hh, minute=mm)
                        if dt > now_et:
                            candidate = dt
                            break
                if candidate is None:
                    hh, mm = times[0]
                    # If today has passed, roll one week forward.
                    if days_ahead == 0:
                        base_day = base_day + timedelta(days=7)
                    candidate = base_day.replace(hour=hh, minute=mm)
                next_due = candidate

            preview_next, _target_next = _key_preview_for(next_due)

            # Backward-compatible name: now points at the next scheduled due key.
            parts.append(f"[TNT][CONFIG] weekly_outlook_dedupe_key_preview={preview_next}")
            parts.append(f"[TNT][CONFIG] weekly_outlook_dedupe_key_preview_next_due={preview_next}")
        except Exception:
            pass

        # Redis/job plumbing (avoid leaking credentials).
        redis_url = os.getenv('REDIS_URL') or os.getenv('TNT_REDIS_URL')
        if redis_url:
            parts.append(f"[TNT][CONFIG] redis_url={_redact_url(redis_url)}")
        else:
            parts.append(
                "[TNT][CONFIG] redis="
                f"host:{os.getenv('TNT_REDIS_HOST','127.0.0.1')} "
                f"port:{os.getenv('TNT_REDIS_PORT','6379')} "
                f"db:{os.getenv('TNT_REDIS_DB','0')} "
                f"queue:{os.getenv('TNT_REDIS_QUEUE','tnt:jobs')}"
            )

        worker_base = os.getenv('TNT_WORKER_BASE_URL') or os.getenv('ARTIFACT_BASE_URL') or os.getenv('WORKER_BASE_URL')
        if worker_base:
            parts.append(f"[TNT][CONFIG] worker_base_url={worker_base}")

        for line in parts:
            print(line)
    except Exception as exc:  # noqa: BLE001
        print(f"[TNT][CONFIG][WARN] banner failed: {type(exc).__name__}: {exc}")


async def automation_focus_list_loop(channel):
    await bot.wait_until_ready()
    global _last_focus_list_et_date

    hh, mm = _parse_hhmm(os.getenv("TNT_FOCUS_LIST_TIME_ET", "08:10"))
    poll_sec = 30
    print(f"[OK] automation_focus_list_loop running (time_et={hh:02d}:{mm:02d})")

    while not bot.is_closed():
        try:
            if not _paper_desk_enabled() or not _env_bool("TNT_FOCUS_LIST_ENABLED", "0"):
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
            if not _paper_desk_enabled() or not _env_bool("TNT_INTRADAY_UPDATE_ENABLED", "0"):
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
            if not _paper_desk_enabled() or not _env_bool("TNT_RECAP_ENABLED", "0"):
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


def _paperdesk_trades_loop_enabled() -> bool:
    """Enable Paper Desk paper-trade simulation loop.

    This is separate from the legacy "Paper Desk-style scheduled posts" (focus/intraday/recap).
    Default: ON when Paper Desk is enabled.
    """

    if not _paper_desk_enabled():
        return False
    return _env_bool("PAPERDESK_TRADES_LOOP_ENABLED", "1")


def _paperdesk_trades_channel() -> Optional[object]:
    # Default to the explicit Paper Desk trades channel.
    channel_id = _env_int("PAPER_DESK_TRADES_CHANNEL_ID", 0)
    if channel_id <= 0:
        return None
    ch = bot.get_channel(int(channel_id))
    return ch


def _paperdesk_time_stop_et() -> tuple[int, int]:
    try:
        hh, mm = _parse_hhmm(os.getenv("PAPERDESK_TIME_STOP_ET", "15:55"))
        return int(hh), int(mm)
    except Exception:
        return 15, 55


def _paperdesk_stop_pct() -> float:
    try:
        v = float(os.getenv("PAPERDESK_STOP_PCT", "0.003") or "0.003")
        return max(0.0005, min(v, 0.02))
    except Exception:
        return 0.003


def _paperdesk_target_r() -> float:
    try:
        v = float(os.getenv("PAPERDESK_TARGET_R", "2.0") or "2.0")
        return max(0.5, min(v, 6.0))
    except Exception:
        return 2.0


def _paperdesk_edge_min() -> float:
    # Prefer explicit Paper Desk edge; fall back to autopost strict floor.
    try:
        return float(os.getenv("PAPERDESK_EDGE_MIN", str(AUTOPOST_EDGE_MIN_STRICT)) or str(AUTOPOST_EDGE_MIN_STRICT))
    except Exception:
        return float(AUTOPOST_EDGE_MIN_STRICT)


def _paperdesk_symbols() -> list[str]:
    raw = (os.getenv("PAPERDESK_SYMBOLS") or "").strip()
    out: list[str] = []
    if raw:
        for part in raw.split(","):
            sym = (part or "").strip().upper()
            if sym:
                out.append(sym)
    if out:
        # De-dupe preserve order
        seen = set()
        deduped: list[str] = []
        for s in out:
            if s in seen:
                continue
            seen.add(s)
            deduped.append(s)
        return deduped
    try:
        wl = _get_watchlist()
        return [str(s).strip().upper() for s in wl if str(s).strip()][:16] or ["SPY"]
    except Exception:
        return ["SPY"]


def _paperdesk_db_insert_decision(*, day: str, symbol: str, action: str, regime: str | None, confirmation: str | None, edge: float | None, reason: str, meta_json: str) -> None:
    try:
        conn = db_connect()
        conn.execute(
            "INSERT INTO paperdesk_decisions (day, ts, symbol, action, regime, confirmation, edge, reason, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                day,
                datetime.now(timezone.utc).isoformat(),
                symbol,
                action,
                regime,
                confirmation,
                edge,
                reason,
                meta_json,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _paperdesk_db_list_open_trades() -> list[dict]:
    try:
        conn = db_connect()
        conn.row_factory = sqlite3.Row  # type: ignore[name-defined]
        rows = conn.execute(
            "SELECT id, day, ts_open, symbol, direction, entry, stop, target, pd_id FROM paperdesk_trades WHERE status='OPEN' ORDER BY ts_open ASC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _paperdesk_db_has_open_trade(symbol: str) -> bool:
    try:
        conn = db_connect()
        row = conn.execute(
            "SELECT 1 FROM paperdesk_trades WHERE status='OPEN' AND symbol=? LIMIT 1",
            (symbol,),
        ).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


def _paperdesk_db_count_trades_for_day(*, day: str, symbol: str) -> int:
    try:
        conn = db_connect()
        row = conn.execute(
            "SELECT COUNT(1) FROM paperdesk_trades WHERE day=? AND symbol=?",
            (day, symbol),
        ).fetchone()
        conn.close()
        return int(row[0] if row else 0)
    except Exception:
        return 0


def _paperdesk_db_open_trade(
    *,
    day: str,
    symbol: str,
    direction: str,
    regime: str,
    entry: float,
    stop: float,
    target: float,
    pd_id: str,
    meta_json: str,
) -> None:
    conn = db_connect()
    conn.execute(
        "INSERT INTO paperdesk_trades (day, ts_open, symbol, direction, regime, entry, stop, target, status, meta_json, pd_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (day, datetime.now(timezone.utc).isoformat(), symbol, direction, str(regime), float(entry), float(stop), float(target), "OPEN", meta_json, pd_id),
    )
    conn.commit()
    conn.close()


def _paperdesk_db_close_trade(*, trade_id: int, exit_px: float, result_r: float, exit_reason: str) -> None:
    conn = db_connect()
    conn.execute(
        "UPDATE paperdesk_trades SET ts_close=?, exit=?, result_r=?, status='CLOSED', exit_reason=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), float(exit_px), float(result_r), str(exit_reason), int(trade_id)),
    )
    conn.commit()
    conn.close()


async def automation_paperdesk_trades_loop() -> None:
    """Lightweight Paper Desk paper-trade simulator.

    Posts open/close log lines to `PAPER_DESK_TRADES_CHANNEL_ID` and persists to SQLite `paperdesk_trades`.
    """

    await bot.wait_until_ready()

    poll_sec = max(5.0, float(os.getenv("PAPERDESK_POLL_SEC", "20") or "20"))
    max_open = _env_int("PAPERDESK_MAX_OPEN_TRADES", 2)
    max_per_symbol_day = _env_int("PAPERDESK_MAX_TRADES_PER_DAY_PER_SYMBOL", 1)

    print(f"[OK] automation_paperdesk_trades_loop running (poll_sec={poll_sec})")

    while not bot.is_closed():
        try:
            if not _paperdesk_trades_loop_enabled():
                await asyncio.sleep(poll_sec)
                continue

            channel = _paperdesk_trades_channel()
            if channel is None:
                await asyncio.sleep(poll_sec)
                continue

            now_et = _now_et()
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            # Close logic (runs even if we aren't opening new trades).
            hh_stop, mm_stop = _paperdesk_time_stop_et()
            t_stop = now_et.replace(hour=hh_stop, minute=mm_stop, second=0, microsecond=0)
            open_trades = _paperdesk_db_list_open_trades()
            for tr in open_trades:
                sym = str(tr.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                snap = _get_last_price_snapshot(sym)
                if not getattr(snap, "ok", False) or getattr(snap, "price", None) is None:
                    continue
                px = float(snap.price)

                direction = str(tr.get("direction") or "").strip().upper()
                entry = float(tr.get("entry") or 0.0)
                stop = float(tr.get("stop") or 0.0)
                target = float(tr.get("target") or 0.0)
                pd_id = str(tr.get("pd_id") or "")
                trade_id = int(tr.get("id") or 0)
                if trade_id <= 0 or entry <= 0:
                    continue

                exit_reason = None
                if now_et >= t_stop:
                    exit_reason = "TIME_STOP"
                else:
                    if direction == "LONG":
                        if stop and px <= stop:
                            exit_reason = "STOP"
                        elif target and px >= target:
                            exit_reason = "TARGET"
                    elif direction == "SHORT":
                        if stop and px >= stop:
                            exit_reason = "STOP"
                        elif target and px <= target:
                            exit_reason = "TARGET"

                if not exit_reason:
                    continue

                denom = abs(entry - stop) if stop else 0.0
                if denom <= 0:
                    r = 0.0
                else:
                    if direction == "SHORT":
                        r = (entry - px) / denom
                    else:
                        r = (px - entry) / denom

                _paperdesk_db_close_trade(trade_id=trade_id, exit_px=px, result_r=float(r), exit_reason=str(exit_reason))
                try:
                    await channel.send(f"STC {sym} {direction} exit={px:.2f} R={r:+.2f} reason={exit_reason} PD={pd_id}".strip())
                except Exception:
                    pass

            # Open logic (RTH-only).
            if not (RTH_OPEN <= now_et.time() <= RTH_CLOSE):
                await asyncio.sleep(poll_sec)
                continue

            if len(open_trades) >= max(1, int(max_open)):
                await asyncio.sleep(poll_sec)
                continue

            day = now_et.date().isoformat()
            stop_pct = _paperdesk_stop_pct()
            target_r = _paperdesk_target_r()
            edge_min = _paperdesk_edge_min()

            for sym in _paperdesk_symbols():
                if len(_paperdesk_db_list_open_trades()) >= max(1, int(max_open)):
                    break
                if _paperdesk_db_has_open_trade(sym):
                    continue
                if max_per_symbol_day > 0 and _paperdesk_db_count_trades_for_day(day=day, symbol=sym) >= int(max_per_symbol_day):
                    continue

                payload_out = None
                try:
                    payload_out = build_signal_payload(sym)
                except Exception:
                    payload_out = None
                if not payload_out:
                    continue
                payload, prob_up, gate_info = payload_out

                bias = str(payload.get("bias") or "").strip().upper()
                if bias not in {"BULL", "BEAR"}:
                    _paperdesk_db_insert_decision(
                        day=day,
                        symbol=sym,
                        action="SKIP",
                        regime=str(payload.get("regime") or "") or None,
                        confirmation=str(payload.get("bias_confirm") or "") or None,
                        edge=float(payload.get("edge") or 0.0),
                        reason=f"bias={bias or 'NONE'}",
                        meta_json=json.dumps({"payload": payload, "gate": gate_info}, separators=(",", ":"), ensure_ascii=False),
                    )
                    continue

                gate_mode = str(payload.get("gate_mode") or "OK").strip().upper() or "OK"
                if gate_mode not in {"OK", "ALLOW", "PASS"}:
                    _paperdesk_db_insert_decision(
                        day=day,
                        symbol=sym,
                        action="SKIP",
                        regime=str(payload.get("regime") or "") or None,
                        confirmation=str(payload.get("bias_confirm") or "") or None,
                        edge=float(payload.get("edge") or 0.0),
                        reason=f"gate_mode={gate_mode}",
                        meta_json=json.dumps({"payload": payload, "gate": gate_info}, separators=(",", ":"), ensure_ascii=False),
                    )
                    continue

                edge_val = float(payload.get("edge") or 0.0)
                if edge_val < float(edge_min):
                    _paperdesk_db_insert_decision(
                        day=day,
                        symbol=sym,
                        action="SKIP",
                        regime=str(payload.get("regime") or "") or None,
                        confirmation=str(payload.get("bias_confirm") or "") or None,
                        edge=edge_val,
                        reason=f"edge {edge_val:.3f} < min {float(edge_min):.3f}",
                        meta_json=json.dumps({"payload": payload, "gate": gate_info}, separators=(",", ":"), ensure_ascii=False),
                    )
                    continue

                snap = _get_last_price_snapshot(sym)
                if not getattr(snap, "ok", False) or getattr(snap, "price", None) is None:
                    _paperdesk_db_insert_decision(
                        day=day,
                        symbol=sym,
                        action="SKIP",
                        regime=str(payload.get("regime") or "") or None,
                        confirmation=str(payload.get("bias_confirm") or "") or None,
                        edge=edge_val,
                        reason=f"price_unavailable:{getattr(snap,'reason',None) or 'unknown'}",
                        meta_json=json.dumps({"payload": payload, "gate": gate_info}, separators=(",", ":"), ensure_ascii=False),
                    )
                    continue

                entry = float(snap.price)
                direction = "LONG" if bias == "BULL" else "SHORT"
                regime_val = (str(payload.get("regime") or "") or "").strip() or "UNKNOWN"
                if direction == "LONG":
                    stop = entry * (1.0 - stop_pct)
                    target = entry + (abs(entry - stop) * target_r)
                else:
                    stop = entry * (1.0 + stop_pct)
                    target = entry - (abs(stop - entry) * target_r)

                pd_id = f"PD-{day.replace('-', '')}-{int(time_lib.time())}-{sym}-{direction[0]}"
                meta_json = json.dumps(
                    {
                        "payload": payload,
                        "gate": gate_info,
                        "prob_up": float(prob_up),
                        "edge_min": float(edge_min),
                        "stop_pct": float(stop_pct),
                        "target_r": float(target_r),
                        "source": "automation_paperdesk_trades_loop",
                    },
                    separators=(",", ":"),
                    ensure_ascii=False,
                )

                _paperdesk_db_open_trade(
                    day=day,
                    symbol=sym,
                    direction=direction,
                    regime=regime_val,
                    entry=entry,
                    stop=stop,
                    target=target,
                    pd_id=pd_id,
                    meta_json=meta_json,
                )
                _paperdesk_db_insert_decision(
                    day=day,
                    symbol=sym,
                    action="OPEN",
                    regime=regime_val,
                    confirmation=str(payload.get("bias_confirm") or "") or None,
                    edge=edge_val,
                    reason=f"bias={bias} edge={edge_val:.3f}",
                    meta_json=meta_json,
                )
                try:
                    await channel.send(
                        f"BTO {sym} {direction} qty=1 entry={entry:.2f} stop={stop:.2f} target={target:.2f} edge={edge_val:.3f} PD={pd_id}".strip()
                    )
                except Exception:
                    pass

            await asyncio.sleep(poll_sec)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_paperdesk_trades_loop error: {type(exc).__name__}: {exc}")
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
            if not _env_bool("TNT_HEARTBEAT_ENABLED", "0") or interval_min <= 0:
                await asyncio.sleep(poll_sec)
                continue

            # Always run feed-level stale checks (even if we aren't due to send
            # the periodic heartbeat message yet). This is our canary/ops
            # siren for missing workers.
            try:
                await _update_feed_stale_state()
            except Exception:
                pass

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

            feed_line = None
            try:
                from services.observability.feed_heartbeat import summarize_feeds

                feed_line, _feeds_meta = summarize_feeds()
            except Exception:
                feed_line = None

            parts: list[str] = []
            parts.append("online")
            parts.append(f"SPY_1m_age_min={age if age is not None else 'n/a'}")
            parts.append(f"queue_depth={queue_len}")
            if feed_line:
                parts.append(feed_line)
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


async def automation_macro_calendar_refresh_loop() -> None:
    """Refresh macro calendar from Massive -> CSV + Redis.

    - Source: Massive `/benzinga/v1/economic-calendar`
    - Outputs:
      - CSV: `MACRO_EVENTS_FILE` (used by morning brief/outlook rendering)
      - Redis: `cal:macro:upcoming` (used by macro blackout gate)
    """

    poll_sec = float(_positive_env_float("TNT_MACRO_CALENDAR_REFRESH_POLL_SEC", "21600"))
    max_age_sec = float(_positive_env_float("TNT_MACRO_CALENDAR_MAX_AGE_SEC", "43200"))
    days_ahead = int(_env_int("TNT_MACRO_CALENDAR_DAYS_AHEAD", "21"))
    countries_raw = (os.getenv("TNT_MACRO_CALENDAR_COUNTRIES") or "US").strip()
    countries = [c.strip().upper() for c in countries_raw.split(",") if c.strip()]

    def _resolve_massive_key() -> str:
        key = (os.getenv("MASSIVE_API_KEY") or "").strip()
        if key:
            return key
        return (os.getenv("POLYGON_API_KEY") or "").strip()

    def _resolve_massive_base() -> str:
        return (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").strip().rstrip("/")

    print(
        "[OK] automation_macro_calendar_refresh_loop running "
        f"(poll_sec={poll_sec:.0f} max_age_sec={max_age_sec:.0f} days_ahead={days_ahead} countries={countries})"
    )

    while True:
        try:
            if not _automation_enabled() or not _macro_calendar_refresh_enabled():
                await asyncio.sleep(max(30.0, poll_sec))
                continue

            api_key = _resolve_massive_key()
            base_url = _resolve_massive_base()
            if not api_key:
                await asyncio.sleep(max(60.0, poll_sec))
                continue

            out_path = Path(MACRO_EVENTS_FILE)

            # Avoid hammering the upstream: only refresh if missing or stale.
            should_refresh = True
            if out_path.exists():
                try:
                    age_s = max(0.0, float(_now_utc().timestamp()) - float(out_path.stat().st_mtime))
                    if age_s < max_age_sec:
                        should_refresh = False
                except Exception:
                    should_refresh = True

            if not should_refresh:
                await asyncio.sleep(max(60.0, poll_sec))
                continue

            from services.calendar.calendar_service import CalendarService
            from services.calendar.massive_benzinga_macro import fetch_benzinga_economic_calendar

            now_utc = _now_utc().replace(tzinfo=timezone.utc)
            start = now_utc.date()
            end = (now_utc + timedelta(days=max(1, days_ahead))).date()

            items = await fetch_benzinga_economic_calendar(
                base_url=base_url,
                api_key=api_key,
                start_date=start,
                end_date=end,
                limit=500,
                timeout_s=float(_env_float("TNT_MACRO_CALENDAR_TIMEOUT_S", "15")),
                countries=countries,
            )

            # Write CSV used by briefs.
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=["date_et", "time_et", "impact", "title", "type", "ts_utc", "source"],
                )
                w.writeheader()
                for it in items:
                    dt_et = it.ts_utc.astimezone(ET)
                    w.writerow(
                        {
                            "date_et": dt_et.strftime("%Y-%m-%d"),
                            "time_et": dt_et.strftime("%H:%M"),
                            "impact": (it.impact or "MED").upper(),
                            "title": it.title,
                            "type": it.type,
                            "ts_utc": it.ts_utc.isoformat(),
                            "source": it.source,
                        }
                    )
            try:
                tmp_path.replace(out_path)
            except Exception:
                # Best-effort fallback.
                try:
                    shutil.move(str(tmp_path), str(out_path))
                except Exception:
                    pass

            # Update Redis macro calendar for gates (best-effort).
            try:
                r = redis_client_for_ctx()
                if r is not None:
                    try:
                        r.delete("cal:macro:upcoming")
                    except Exception:
                        pass
                    CalendarService(r).set_macro_upcoming([it.to_macro_payload() for it in items])
            except Exception:
                pass

            print(f"[OK] macro calendar refreshed: items={len(items)} file={out_path}")
            await asyncio.sleep(max(60.0, poll_sec))

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] automation_macro_calendar_refresh_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(max(60.0, poll_sec))


def _ensure_tnt_automation_tasks() -> None:
    """Schedules a small go-live-safe set of automation tasks. Idempotent."""
    global _automation_focus_task, _automation_intraday_task, _automation_recap_task, _automation_paperdesk_trades_task, _automation_macro_calendar_task, _automation_heartbeat_task, _automation_earnings_task, _automation_earnings_results_task, _automation_earnings_results_prewarm_task, _automation_daily_outlook_task, _automation_weekly_outlook_task, _automation_divergence_watch_task, _automation_earnings_pressure_watch_task

    if not _automation_enabled():
        print("[OK] automation: disabled (TNT_AUTOMATION_ENABLED=0)")
        return

    channel = _resolve_automation_channel()
    if channel is None:
        return

    if _macro_calendar_refresh_enabled():
        if not _automation_macro_calendar_task or _automation_macro_calendar_task.done():
            _automation_macro_calendar_task = bot.loop.create_task(automation_macro_calendar_refresh_loop())
            print("[OK] automation macro_calendar_refresh task scheduled")

    # Defaults are intentionally OFF; enable explicitly per post type.
    if _env_bool("EARNINGS_AUTOPOST_ENABLED", "0"):
        if not _automation_earnings_task or _automation_earnings_task.done():
            earnings_channel = _resolve_earnings_calendar_channel() or channel
            _automation_earnings_task = bot.loop.create_task(automation_earnings_daily_loop(earnings_channel))
            print("[OK] automation earnings_daily task scheduled")

    if _env_bool("EARNINGS_RESULTS_AUTOPOST_ENABLED", "0"):
        # Prewarm is ON by default when results autopost is enabled.
        if _env_bool("EARNINGS_RESULTS_PREWARM_ENABLED", "1"):
            if not _automation_earnings_results_prewarm_task or _automation_earnings_results_prewarm_task.done():
                _automation_earnings_results_prewarm_task = bot.loop.create_task(automation_earnings_results_prewarm_loop())
                print("[OK] automation earnings_results prewarm task scheduled")

        if not _automation_earnings_results_task or _automation_earnings_results_task.done():
            earnings_channel = _resolve_earnings_calendar_channel() or channel
            _automation_earnings_results_task = bot.loop.create_task(automation_earnings_results_loop(earnings_channel))
            print("[OK] automation earnings_results task scheduled")

    if _env_bool("DAILY_OUTLOOK_ENABLED", "0"):
        if not _automation_daily_outlook_task or _automation_daily_outlook_task.done():
            outlook_channel = _resolve_daily_outlook_channel() or channel
            _automation_daily_outlook_task = bot.loop.create_task(automation_daily_market_outlook_loop(outlook_channel))
            print("[OK] automation daily_outlook task scheduled [AUTOPOST][OUTLOOK]")

    if _env_bool("WEEKLY_OUTLOOK_ENABLED", "0"):
        if not _automation_weekly_outlook_task or _automation_weekly_outlook_task.done():
            outlook_channel = _resolve_daily_outlook_channel() or channel
            _automation_weekly_outlook_task = bot.loop.create_task(automation_weekly_market_outlook_loop(outlook_channel))
            print("[OK] automation weekly_outlook task scheduled [AUTOPOST][WEEKLY_OUTLOOK]")

    if _paper_desk_enabled() and _env_bool("TNT_FOCUS_LIST_ENABLED", "0"):
        if not _automation_focus_task or _automation_focus_task.done():
            _automation_focus_task = bot.loop.create_task(automation_focus_list_loop(channel))
            print("[OK] automation focus_list task scheduled")

    if _paper_desk_enabled() and _env_bool("TNT_INTRADAY_UPDATE_ENABLED", "0"):
        if not _automation_intraday_task or _automation_intraday_task.done():
            _automation_intraday_task = bot.loop.create_task(automation_intraday_update_loop(channel))
            print("[OK] automation intraday_update task scheduled")

    if _paper_desk_enabled() and _env_bool("TNT_RECAP_ENABLED", "0"):
        if not _automation_recap_task or _automation_recap_task.done():
            _automation_recap_task = bot.loop.create_task(automation_recap_loop(channel))
            print("[OK] automation recap task scheduled")

    if _paperdesk_trades_loop_enabled():
        if not _automation_paperdesk_trades_task or _automation_paperdesk_trades_task.done():
            _automation_paperdesk_trades_task = bot.loop.create_task(automation_paperdesk_trades_loop())
            print("[OK] automation paperdesk trades task scheduled")

    if _env_bool("TNT_HEARTBEAT_ENABLED", "0"):
        if not _automation_heartbeat_task or _automation_heartbeat_task.done():
            _automation_heartbeat_task = bot.loop.create_task(automation_heartbeat_loop(channel))
            print("[OK] automation heartbeat task scheduled")

    if _divergence_watch_enabled():
        if not _automation_divergence_watch_task or _automation_divergence_watch_task.done():
            _automation_divergence_watch_task = bot.loop.create_task(automation_divergence_watch_loop(channel))
            print("[OK] automation divergence_watch task scheduled")

    if _earnings_pressure_watch_enabled():
        if not _automation_earnings_pressure_watch_task or _automation_earnings_pressure_watch_task.done():
            _automation_earnings_pressure_watch_task = bot.loop.create_task(automation_earnings_pressure_watch_loop(channel))
            print("[OK] automation earnings_pressure_watch task scheduled")


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

    _log_startup_banner()
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


async def maybe_handle_ask_tnt_earnings_embed(message: "discord.Message") -> bool:
    """Deterministic earnings embed handler for #ask-tnt free-text.

    Returns True if handled (embed posted), else False.

    NOTE: This is intentionally reusable by other bot entrypoints (e.g. `cli.discord_bot`)
    so we can keep a single Discord gateway session while still serving the legacy
    earnings embed UX.
    """

    def _route_channel_id(ch: object) -> int:
        """Return the routing channel id.

        - For normal text channels, route by the channel id.
        - For threads, route by the parent channel id.

        Avoid using TextChannel.parent_id (category id), since routing env vars are
        configured with actual channel ids.
        """

        try:
            if isinstance(ch, discord.Thread):
                try:
                    pid = int(getattr(ch, "parent_id", 0) or 0)
                except Exception:
                    pid = 0
                if pid:
                    return pid
                try:
                    parent = getattr(ch, "parent", None)
                    return int(getattr(parent, "id", 0) or 0)
                except Exception:
                    return 0
        except Exception:
            pass

        try:
            return int(getattr(ch, "id", 0) or 0)
        except Exception:
            return 0

    try:
        # Only apply in guild channels, never DMs.
        if message.guild is None:
            return False

        content_raw = str(message.content or "")
        content_clean = content_raw.strip()
        if not content_clean:
            return False
        # Don't intercept bot commands.
        if content_clean.startswith("!"):
            return False

        # Enforce ASK_TNT routing even if router isn't explicitly enabled.
        # IMPORTANT: route by the *channel* name ("ask-tnt"), not the category.
        ch = getattr(message, "channel", None)
        channel_id_for_routing = _route_channel_id(ch)
        route_name = ""
        try:
            if isinstance(ch, discord.Thread):
                parent = getattr(ch, "parent", None)
                route_name = str(getattr(parent, "name", "") or "") or str(getattr(ch, "name", "") or "")
            else:
                route_name = str(getattr(ch, "name", "") or "")
        except Exception:
            route_name = ""

        decision = decide_route(
            channel_id=int(channel_id_for_routing or 0),
            content=content_clean,
            channel_name=route_name,
        )
        if decision.action != "allow_full":
            return False

        lowered = content_clean.lower()
        debug_earn = str(os.getenv("TNT_EARNINGS_EMBED_DEBUG", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}

        def _is_earnings_intent(text_lower: str) -> bool:
            if not text_lower:
                return False
            # Keep conservative; avoid capturing general "earnings season" chatter.
            if "earnings" in text_lower or "earning" in text_lower:
                return True
            # Tokenized "ER" shorthand.
            return (" er" in f" {text_lower} ")

        if not _is_earnings_intent(lowered):
            return False

        # Calendar-style earnings queries ("who has earnings today", "earnings next week", etc)
        # should not be forced into a single-symbol embed. Answer deterministically from the
        # cached watchlist universe.
        try:
            from delivery.qa_router import looks_like_earnings_calendar_query

            is_calendar_query = looks_like_earnings_calendar_query(text=content_clean)
        except Exception:
            is_calendar_query = False

        if is_calendar_query:
            try:
                r_cal = redis_client_for_ctx()
            except Exception:
                r_cal = None

            if r_cal is None:
                return False

            # Use an explicit watchlist universe to avoid full-market enumeration.
            raw_wl = (os.getenv("EARNINGS_WATCHLIST") or os.getenv("EARNINGS_AUTOPOST_SYMBOLS") or "").strip()
            watchlist = [s.strip().upper() for s in raw_wl.split(",") if s.strip()]
            watchlist = sorted({s for s in watchlist if s and s.isalpha() and (1 <= len(s) <= 6)})

            if not watchlist:
                try:
                    await message.reply(
                        "📅 Earnings (watchlist-only)\n"
                        "• No watchlist configured (set EARNINGS_WATCHLIST or EARNINGS_AUTOPOST_SYMBOLS).\n"
                        "• Or ask a single ticker (e.g., 'AAPL earnings date').",
                        mention_author=False,
                    )
                except Exception:
                    pass
                return True

            from datetime import date

            def _parse_iso_utc(s: str) -> datetime | None:
                try:
                    dtu = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                    if dtu.tzinfo is None:
                        dtu = dtu.replace(tzinfo=timezone.utc)
                    return dtu.astimezone(timezone.utc)
                except Exception:
                    return None

            now_utc = datetime.now(timezone.utc)
            now_et = now_utc.astimezone(ET)
            q_lower = lowered

            # Date window (ET, inclusive).
            explicit_date = None
            try:
                import re as _re

                m = _re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", content_clean)
                explicit_date = m.group(1) if m else None
            except Exception:
                explicit_date = None

            if explicit_date:
                start_d = date.fromisoformat(str(explicit_date))
                end_d = start_d
                label = str(explicit_date)
            elif "today" in q_lower:
                start_d = now_et.date()
                end_d = start_d
                label = "today"
            elif "tomorrow" in q_lower:
                start_d = now_et.date() + timedelta(days=1)
                end_d = start_d
                label = "tomorrow"
            elif "next week" in q_lower:
                d0 = now_et.date()
                days_ahead = (7 - d0.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                monday = d0 + timedelta(days=days_ahead)
                start_d = monday
                end_d = monday + timedelta(days=6)
                label = "next week"
            elif "this week" in q_lower:
                d0 = now_et.date()
                start_d = d0 - timedelta(days=int(d0.weekday()))
                end_d = start_d + timedelta(days=6)
                label = "this week"
            else:
                # Default: upcoming (next 7 calendar days).
                start_d = now_et.date()
                end_d = start_d + timedelta(days=6)
                label = "next 7d"

            from services.calendar.calendar_service import CalendarService
            from services.calendar.earnings_embeds import UpcomingEarningsItem, build_earnings_today_embed, build_earnings_upcoming_embed

            cal = CalendarService(r_cal)
            refreshed_utc = None
            try:
                refreshed_utc = cal.get_earnings_last_refresh_utc()
            except Exception:
                refreshed_utc = None

            items: list[UpcomingEarningsItem] = []
            for sym in watchlist:
                ev = None
                try:
                    ev = cal.get_earnings(symbol=sym)
                except Exception:
                    ev = None
                if not isinstance(ev, dict):
                    continue

                dt_ev = _parse_iso_utc(str(ev.get("ts_utc") or ""))
                if dt_ev is None:
                    continue

                d_et = dt_ev.astimezone(ET).date()
                if not (start_d <= d_et <= end_d):
                    continue

                items.append(
                    UpcomingEarningsItem(
                        symbol=sym,
                        ts_utc_iso=str(ev.get("ts_utc") or ""),
                        confirmed=bool(ev.get("confirmed", False)),
                        session=str(ev.get("session") or "UNKNOWN"),
                        expected_move_pct=(float(ev.get("expected_move_pct")) if ev.get("expected_move_pct") is not None else None),
                    )
                )

            # Embed selection: "today" uses a compact single-bucket view; other windows use buckets.
            if start_d == end_d and start_d == now_et.date():
                embed = build_earnings_today_embed(items, refreshed_utc=refreshed_utc, title="Earnings Today (watchlist)")
            else:
                days = max(1, int((end_d - start_d).days) + 1)
                embed = build_earnings_upcoming_embed(items, days=days, refreshed_utc=refreshed_utc)
                try:
                    embed.title = f"Earnings ({label}, watchlist)"
                except Exception:
                    pass

            try:
                await message.reply(embed=embed, mention_author=False)
            except Exception:
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass
            return True

        def _last_earnings_symbol_key(msg: "discord.Message") -> str | None:
            try:
                gid = int(getattr(getattr(msg, "guild", None), "id", 0) or 0)
                cid = int(getattr(getattr(msg, "channel", None), "id", 0) or 0)
                uid = int(getattr(getattr(msg, "author", None), "id", 0) or 0)
            except Exception:
                return None
            if gid <= 0 or cid <= 0 or uid <= 0:
                return None
            return f"tnt:ask_tnt:last_earnings_symbol:{gid}:{cid}:{uid}"

        def _yesno_earnings_window(text_lower: str) -> str | None:
            t = (text_lower or "").strip().lower()
            if not t:
                return None
            if "earnings" not in t and "earning" not in t and (" er" not in f" {t} "):
                return None

            yn = any(w in f" {t} " for w in (" does ", " do ", " is ", " are ", " did ", " have ", " has "))
            if not yn:
                return None

            if "tomorrow" in t:
                return "tomorrow"
            if "today" in t:
                return "today"
            if "next week" in t:
                return "next_week"
            if "this week" in t:
                return "this_week"
            return None

        # Symbol-only earnings queries.
        # NOTE: Do not use extract_analysis_request() here; it's analyze-focused and can
        # misclassify question words (e.g., WHO) as tickers.
        sym_e: str | None = None
        try:
            sym_e = _detect_symbol_in_text(content_clean)
        except Exception:
            sym_e = None

        # Follow-up context: if user asks "earnings tomorrow?" without a ticker.
        if not sym_e:
            try:
                r_ctx = redis_client_for_ctx()
                k = _last_earnings_symbol_key(message)
                if r_ctx is not None and k:
                    v = r_ctx.get(k)
                    if v:
                        sym_e = _normalize_symbol_token(str(v))
            except Exception:
                sym_e = None

        sym_e = (sym_e or "").strip().upper()
        if not sym_e:
            return False

        if debug_earn:
            try:
                ch_name = str(getattr(getattr(message, "channel", None), "name", "") or "")
            except Exception:
                ch_name = ""
            print(f"[EARNINGS_EMBED] trigger=1 channel={ch_name!r} sym={sym_e} content={content_clean!r}")

        yesno_window = _yesno_earnings_window(lowered)

        from services.calendar.calendar_service import CalendarService
        from services.calendar.earnings_embeds import build_earnings_embed
        from services.calendar.earnings_miss_gate import should_show_retrieving_once
        from services.news.news_service import NewsService

        # Remember this symbol for short follow-ups in the same channel.
        try:
            r_set = redis_client_for_ctx()
            k = _last_earnings_symbol_key(message)
            if r_set is not None and k:
                # 6 hours is enough for conversational follow-ups, and avoids stale drift.
                r_set.setex(k, 6 * 3600, sym_e)
        except Exception:
            pass

        def _load_payload() -> tuple[dict[str, Any] | None, int | None, list[dict[str, Any]], str, str]:
            r = None
            try:
                r = redis_client_for_ctx()
            except Exception:
                r = None

            ev = None
            refreshed = None
            news_items: list[dict[str, Any]] = []

            if r is not None:
                try:
                    cal = CalendarService(r)
                    ev = cal.get_earnings(symbol=sym_e)
                    refreshed = cal.get_earnings_refreshed_utc(symbol=sym_e)

                    # If the canonical blob doesn't include history (common), pull the
                    # most recent items from the optional history layer.
                    try:
                        if isinstance(ev, dict):
                            hist = ev.get("history")
                            if not isinstance(hist, list) or len(hist) == 0:
                                ev["history"] = cal.get_earnings_history(symbol=sym_e, limit=4)
                    except Exception:
                        pass

                    # If upstream feeds only provide a very-far future placeholder (e.g., ~1y+ out),
                    # infer a nearer *estimated* earnings date from the most recent history event.
                    try:
                        if isinstance(ev, dict):
                            ts_raw = str(ev.get("ts_utc") or "")
                            if ts_raw:
                                from datetime import datetime, timedelta, timezone

                                def _parse_iso_utc(s: str) -> datetime | None:
                                    try:
                                        dtu = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                                        if dtu.tzinfo is None:
                                            dtu = dtu.replace(tzinfo=timezone.utc)
                                        return dtu.astimezone(timezone.utc)
                                    except Exception:
                                        return None

                                now_utc = datetime.now(timezone.utc)
                                dt_ev = _parse_iso_utc(ts_raw)

                                try:
                                    max_fwd_days = int(os.getenv("TNT_EARNINGS_MAX_FORWARD_DAYS", "240") or "240")
                                except Exception:
                                    max_fwd_days = 240
                                max_fwd_days = max(30, min(900, int(max_fwd_days)))

                                if dt_ev is not None and (dt_ev - now_utc).total_seconds() > float(max_fwd_days) * 86400.0:
                                    # Grab the most recent historical event timestamp.
                                    hist_items = ev.get("history") if isinstance(ev.get("history"), list) else []
                                    dt_hist_max = None
                                    for it in hist_items:
                                        if not isinstance(it, dict):
                                            continue
                                        dt_h = _parse_iso_utc(str(it.get("ts_utc") or ""))
                                        if dt_h is None:
                                            continue
                                        if dt_hist_max is None or dt_h > dt_hist_max:
                                            dt_hist_max = dt_h

                                    # Only infer if history is reasonably recent.
                                    if dt_hist_max is not None and (now_utc - dt_hist_max).total_seconds() < 240.0 * 86400.0:
                                        try:
                                            infer_days = int(os.getenv("TNT_EARNINGS_INFER_CADENCE_DAYS", "91") or "91")
                                        except Exception:
                                            infer_days = 91
                                        infer_days = max(60, min(120, int(infer_days)))

                                        dt_inf = dt_hist_max + timedelta(days=int(infer_days))
                                        ev["ts_utc"] = dt_inf.isoformat()
                                        ev["confirmed"] = False
                                        ev["source"] = str(ev.get("source") or "") or "inferred"
                                        ev["_ts_utc_inferred"] = True
                    except Exception:
                        pass
                except Exception:
                    ev, refreshed = None, None

                try:
                    news_items = NewsService(r).get_recent_items(symbol=sym_e, limit=10)
                except Exception:
                    news_items = []

            missing_mode = "retrieving"
            if not ev:
                try:
                    if r is not None and should_show_retrieving_once(r, sym_e):
                        missing_mode = "retrieving"
                    else:
                        missing_mode = "sparse"
                except Exception:
                    missing_mode = "sparse"

            # For yes/no questions, never show the looping "retrieving" UX.
            if yesno_window is not None:
                missing_mode = "sparse"

            # Price snapshot (best-effort).
            price_line = "Price: unavailable (no recent price)"
            try:
                snap = _get_last_price_snapshot(sym_e)
                px_raw = getattr(snap, "px", None)
                if px_raw is None:
                    px_raw = getattr(snap, "price", None)

                src = str(getattr(snap, "source", "none") or "none")
                asof = getattr(snap, "asof_et", None)

                if px_raw is not None:
                    px = float(px_raw)
                    label = "last trade"
                    if src == "db_1d_close":
                        label = "last close"
                    elif src == "db_1m_close":
                        label = "last 1m close"
                    elif src == "cached_analyze":
                        label = "cached"

                    when = ""
                    try:
                        if asof is not None:
                            hh = int(getattr(asof, "hour", 0))
                            mm = int(getattr(asof, "minute", 0))
                            ap = "am" if hh < 12 else "pm"
                            hh12 = hh % 12
                            if hh12 == 0:
                                hh12 = 12
                            dow = str(asof.strftime("%a"))
                            when = f"{dow} {hh12}:{mm:02d}{ap} ET"
                    except Exception:
                        when = ""

                    price_line = f"Price: ${px:.2f} ({label})"
                    if when:
                        price_line = f"Price: ${px:.2f} ({label} • {when})"
            except Exception:
                price_line = "Price: unavailable (no recent price)"

            return ev, refreshed, news_items, price_line, missing_mode

        ev, refreshed, news_items, price_line, missing_mode = await asyncio.to_thread(_load_payload)

        # If this is a yes/no question (today/tomorrow/this week/next week), add a direct answer line.
        try:
            if yesno_window is not None:
                dt_ev = None
                if isinstance(ev, dict):
                    ts_raw = str(ev.get("ts_utc") or "").strip()
                    if ts_raw:
                        try:
                            dt_ev = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                            if dt_ev.tzinfo is None:
                                dt_ev = dt_ev.replace(tzinfo=timezone.utc)
                            dt_ev = dt_ev.astimezone(timezone.utc)
                        except Exception:
                            dt_ev = None

                now_et = _now_et()
                ans = None
                if dt_ev is not None:
                    ev_date_et = dt_ev.astimezone(ET).date()
                    if yesno_window == "today":
                        want = now_et.date()
                        ans = (ev_date_et == want)
                    elif yesno_window == "tomorrow":
                        want = now_et.date() + timedelta(days=1)
                        ans = (ev_date_et == want)
                    elif yesno_window in {"this_week", "next_week"}:
                        # Weeks anchored to ET Monday..Sunday.
                        wd = int(now_et.weekday())
                        this_mon = now_et.date() - timedelta(days=wd)
                        if yesno_window == "this_week":
                            start = this_mon
                        else:
                            start = this_mon + timedelta(days=7)
                        end = start + timedelta(days=6)
                        ans = (start <= ev_date_et <= end)

                if ans is True:
                    answer_line = f"Answer: YES — {sym_e} is on the earnings calendar for that window."
                elif ans is False:
                    answer_line = f"Answer: NO — {sym_e} is not on the earnings calendar for that window."
                else:
                    answer_line = f"Answer: UNKNOWN — {sym_e} doesn’t have a confirmed earnings date in cache right now."

                price_line = (f"{answer_line}\n{price_line}" if price_line else answer_line)
        except Exception:
            pass

        # If the cache is missing for this symbol, best-effort fetch + seed it.
        # This keeps the rich embed usable for symbols outside the scheduled watchlist.
        try:
            if ev is None:
                r_seed = None
                try:
                    r_seed = redis_client_for_ctx()
                except Exception:
                    r_seed = None
                if r_seed is not None:
                    from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings, pick_next_earnings
                    from services.calendar.calendar_service import CalendarService as _Cal

                    key_m, base_m, _prov = _massive_key_and_base()
                    if key_m:
                        now_et = _now_et()
                        now_utc = _now_utc().replace(tzinfo=timezone.utc)
                        try:
                            lookahead_days = int(os.getenv("TNT_EARNINGS_LOOKAHEAD_DAYS", "180") or "180")
                        except Exception:
                            lookahead_days = 180
                        lookahead_days = max(30, min(365, int(lookahead_days)))

                        recs = None
                        try:
                            recs = await asyncio.wait_for(
                                fetch_benzinga_earnings(
                                    base_url=base_m,
                                    api_key=key_m or "",
                                    tickers=[sym_e],
                                    start_date=now_et.date().isoformat(),
                                    end_date=(now_et.date() + timedelta(days=int(lookahead_days))).isoformat(),
                                    limit=800,
                                    timeout_s=10.0,
                                ),
                                timeout=12.0,
                            )
                        except Exception:
                            recs = None

                        if recs:
                            nxt = None
                            try:
                                nxt = pick_next_earnings(recs, symbol=sym_e, now_utc=now_utc)
                            except Exception:
                                nxt = None
                            if nxt is not None:
                                cal2 = _Cal(r_seed)
                                try:
                                    cal2.set_earnings(symbol=sym_e, ts_utc=nxt.ts_utc, confirmed=bool(nxt.confirmed), source=str(nxt.source or "benzinga"), ttl_sec=14 * 24 * 3600)
                                except Exception:
                                    pass
                                try:
                                    cal2.cache_earnings_history(symbol=sym_e, items=[x.to_jsonable() for x in recs], ttl_sec=90 * 24 * 3600)
                                except Exception:
                                    pass
                                try:
                                    ev = cal2.get_earnings(symbol=sym_e)
                                    refreshed = cal2.get_earnings_refreshed_utc(symbol=sym_e)
                                    if isinstance(ev, dict):
                                        hist = ev.get("history")
                                        if not isinstance(hist, list) or len(hist) == 0:
                                            ev["history"] = cal2.get_earnings_history(symbol=sym_e, limit=4)
                                except Exception:
                                    pass
        except Exception:
            pass

        # Best-effort: if premium fields are missing, compute expected move from
        # the Polygon ATM straddle so the embed matches the legacy card.
        try:
            if isinstance(ev, dict) and (ev.get("expected_move_pct") is None or not isinstance(ev.get("expected_move"), dict)):
                api_key, _base_url, _provider = _polygon_key_and_base()
                if api_key:
                    try:
                        timeout_s = float(os.getenv("TNT_EARNINGS_EXPECTED_MOVE_TIMEOUT_S", "6") or "6")
                    except Exception:
                        timeout_s = 6.0

                    df = None
                    try:
                        df = await asyncio.wait_for(_fetch_polygon_atm_straddle_df(sym_e), timeout=max(1.0, float(timeout_s)))
                    except Exception:
                        df = None

                    if df is not None:
                        try:
                            from services.calendar.earnings_options import compute_expected_move_from_chain_df

                            res = compute_expected_move_from_chain_df(df)
                        except Exception:
                            res = None

                        if isinstance(res, dict) and res.get("expected_move_pct") is not None:
                            try:
                                em_pct = float(res.get("expected_move_pct"))
                            except Exception:
                                em_pct = None

                            if em_pct is not None:
                                ev["expected_move_pct"] = em_pct
                                ev["liquidity_risk"] = str(res.get("liquidity_risk") or ev.get("liquidity_risk") or "MED")
                                ev["expected_move_source"] = "options_chain_atm_straddle"
                                ev["expected_move_refreshed_utc"] = _now_utc().replace(tzinfo=timezone.utc).isoformat()

                                details = res.get("details") if isinstance(res.get("details"), dict) else {}
                                under = None
                                straddle = None
                                try:
                                    under = float(details.get("underlying_price")) if details.get("underlying_price") is not None else None
                                except Exception:
                                    under = None
                                try:
                                    straddle = float(details.get("straddle_mid")) if details.get("straddle_mid") is not None else None
                                except Exception:
                                    straddle = None

                                if under is not None and straddle is not None and under > 0 and straddle > 0:
                                    ev["expected_move"] = {
                                        "underlying": float(under),
                                        "straddle": float(straddle),
                                        "pct": float(em_pct),
                                        "upper": float(under + straddle),
                                        "lower": float(max(0.0, under - straddle)),
                                        "atm_strike": details.get("atm_strike"),
                                    }

                                iv_state = ev.get("iv_state") if isinstance(ev.get("iv_state"), dict) else {}
                                try:
                                    if res.get("front_iv") is not None and iv_state.get("front_iv") is None:
                                        iv_state["front_iv"] = float(res.get("front_iv"))
                                except Exception:
                                    pass
                                ev["iv_state"] = iv_state

                                # Optional write-back so future embeds are instant.
                                try:
                                    wb = str(os.getenv("TNT_EARNINGS_WRITEBACK_EXPECTED_MOVE", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
                                except Exception:
                                    wb = True
                                if wb:
                                    try:
                                        r2 = redis_client_for_ctx()
                                        from services.calendar.calendar_service import CalendarService as _Cal

                                        _Cal(r2).set_earnings_blob(symbol=sym_e, blob=ev, ttl_sec=14 * 24 * 3600)
                                        # Reflect the write-back immediately in the embed.
                                        refreshed = int(datetime.now(timezone.utc).timestamp())
                                    except Exception:
                                        pass
        except Exception:
            pass

        # Best-effort: if last-4 reactions are missing, enrich/backfill on-demand
        # so symbols outside the watchlist (e.g., AMD) still show recent reactions.
        try:
            need_reactions = False
            if isinstance(ev, dict):
                hist = ev.get("history") if isinstance(ev.get("history"), list) else []
                if not hist:
                    need_reactions = True
                else:
                    have_move = False
                    for it in hist:
                        if isinstance(it, dict) and it.get("move_pct") is not None:
                            have_move = True
                            break
                    if not have_move:
                        need_reactions = True

            if need_reactions:
                try:
                    from scripts.earnings_enrich_cache import enrich_symbol

                    try:
                        tmo = float(os.getenv("TNT_EARNINGS_ENRICH_ON_DEMAND_TIMEOUT_S", "12") or "12")
                    except Exception:
                        tmo = 12.0
                    tmo = max(2.0, min(30.0, float(tmo)))

                    res = await asyncio.wait_for(
                        enrich_symbol(symbol=sym_e, write=True, compute_expected_move=False, compute_reactions=True),
                        timeout=tmo,
                    )
                    if getattr(res, "wrote", False):
                        try:
                            r3 = redis_client_for_ctx()
                            from services.calendar.calendar_service import CalendarService as _Cal

                            cal3 = _Cal(r3)
                            ev = cal3.get_earnings(symbol=sym_e)
                            refreshed = cal3.get_earnings_refreshed_utc(symbol=sym_e)
                            if isinstance(ev, dict):
                                hist = ev.get("history")
                                if not isinstance(hist, list) or len(hist) == 0:
                                    ev["history"] = cal3.get_earnings_history(symbol=sym_e, limit=4)
                        except Exception:
                            # Still update the UX freshness marker if we did work.
                            refreshed = int(datetime.now(timezone.utc).timestamp())
                except Exception:
                    pass
        except Exception:
            pass

        embed = build_earnings_embed(
            sym_e,
            ev,
            refreshed,
            news_items=news_items,
            price_line=price_line,
            missing_mode=missing_mode,
            premium=True,
        )
        try:
            await message.reply(embed=embed, mention_author=False)
        except Exception:
            await message.channel.send(embed=embed)
        return True
    except Exception:
        if str(os.getenv("TNT_EARNINGS_EMBED_DEBUG", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                print("[EARNINGS_EMBED] trigger=0 (exception)")
                traceback.print_exc()
            except Exception:
                pass
        return False


@bot.event
async def on_message(message: discord.Message) -> None:
    def _route_channel_id(ch: object) -> int:
        """Return the routing channel id.

        - For normal text channels, route by the channel id.
        - For threads, route by the parent channel id.

        Avoid using TextChannel.parent_id (category id), since routing env vars are
        configured with actual channel ids.
        """

        try:
            if isinstance(ch, discord.Thread):
                try:
                    pid = int(getattr(ch, "parent_id", 0) or 0)
                except Exception:
                    pid = 0
                if pid:
                    return pid
                try:
                    parent = getattr(ch, "parent", None)
                    return int(getattr(parent, "id", 0) or 0)
                except Exception:
                    return 0
        except Exception:
            pass

        try:
            return int(getattr(ch, "id", 0) or 0)
        except Exception:
            return 0

    # Never respond to our own messages.
    if bot.user and message.author.id == bot.user.id:
        return
    # Default: ignore other bots. Optional: allow Concierge to react to alert-bot posts.
    if message.author.bot:
        if concierge_throttle is None or not concierge_throttle.allow_other_bot_messages_for_auto():
            return

    # Channel router contract: delivery bot must not answer in restricted channels.
    try:
        if router_enabled():
            ch = getattr(message, "channel", None)
            channel_id_for_routing = _route_channel_id(ch)

            try:
                parent = getattr(ch, "parent", None)
                parent_name = str(getattr(parent, "name", "") or "")
            except Exception:
                parent_name = ""
            try:
                channel_name = str(getattr(ch, "name", "") or "")
            except Exception:
                channel_name = ""

            decision = decide_route(
                channel_id=int(channel_id_for_routing or 0),
                content=str(message.content or ""),
                channel_name=(parent_name or channel_name),
            )
            if decision.action != "allow_full":
                return
    except Exception:
        pass

    try:
        if await maybe_handle_ask_tnt_earnings_embed(message):
            await bot.process_commands(message)
            return
    except Exception:
        # Best-effort: never block normal message handling.
        if str(os.getenv("TNT_EARNINGS_EMBED_DEBUG", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}:
            try:
                print("[EARNINGS_EMBED] trigger=0 (exception)")
                traceback.print_exc()
            except Exception:
                pass
        pass
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
        msg = f"[ANALYZE][ERROR] build render failed for {symbol_input!r}: {exc}"
        print(msg)
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

    _BOT_SINGLETON_LOCK: object | None = None

    def _acquire_bot_singleton_lock() -> bool:
        """Best-effort single-instance lock for local/dev runs.

        Prevents two bot processes (same token) from simultaneously responding to
        the same channel message, which can manifest as duplicate posts.

        Override with `TNT_ALLOW_MULTIPLE_BOTS=1`.
        """

        allow_multi = str(os.getenv("TNT_ALLOW_MULTIPLE_BOTS", "") or "").strip().lower() in {"1", "true", "yes"}
        if allow_multi:
            return True

        try:
            lock_dir = Path("logs")
            lock_dir.mkdir(parents=True, exist_ok=True)

            # Token-aware lock even when the token is only in dotenv files.
            token = (os.getenv("DISCORD_BOT_TOKEN") or "").strip()
            if not token:
                try:
                    project_root = Path(__file__).resolve().parents[1]
                    for env_path in (project_root / ".env.local", project_root / ".env"):
                        try:
                            raw = env_path.read_text(encoding="utf-8", errors="ignore")
                        except Exception:
                            continue
                        for line in (raw or "").splitlines():
                            t = (line or "").strip()
                            if not t or t.startswith("#"):
                                continue
                            if t.lower().startswith("export "):
                                t = t[7:].strip()
                            if "=" not in t:
                                continue
                            k, v = t.split("=", 1)
                            if (k or "").strip() != "DISCORD_BOT_TOKEN":
                                continue
                            token = (v or "").strip().strip('"').strip("'")
                            break
                        if token:
                            break
                except Exception:
                    pass

            lock_id = "default"
            if token:
                lock_id = hashlib.sha1(token.encode("utf-8")).hexdigest()[:12]
            lock_path = lock_dir / f"tnt_discord_bot.{lock_id}.lock"
            fh = open(lock_path, "a+", encoding="utf-8")

            # Ensure the lock handle is not inheritable. If a child process inherits
            # this handle, it can appear to "also" hold the singleton lock and we end
            # up with two running bot processes.
            try:
                os.set_inheritable(fh.fileno(), False)
            except Exception:
                pass

            try:
                # Windows: msvcrt lock (non-blocking)
                import msvcrt  # type: ignore

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    try:
                        fh.close()
                    except Exception:
                        pass
                    return False
            except Exception:
                # POSIX: fcntl flock (non-blocking)
                try:
                    import fcntl  # type: ignore

                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        try:
                            fh.close()
                        except Exception:
                            pass
                        return False
                except Exception:
                    # If locking isn't supported, don't block startup.
                    return True

            # Write PID stamp (informational).
            try:
                fh.seek(0)
                fh.truncate(0)
                fh.write(f"pid={os.getpid()}\n")
                fh.flush()
            except Exception:
                pass

            nonlocal _BOT_SINGLETON_LOCK
            _BOT_SINGLETON_LOCK = fh
            return True
        except Exception:
            return True

    if not _acquire_bot_singleton_lock():
        print("[FATAL] Another TNT Discord bot process is already running (singleton lock active).")
        print("        Stop the other process or set TNT_ALLOW_MULTIPLE_BOTS=1 to override.")
        sys.exit(2)

    def _boot_version_marker() -> str:
        """Best-effort version marker for auditability.

        Prefer git SHA when available; fall back to a cheap file-bytes hash.
        """

        try:
            git = str((_git_sha() or "")).strip()
            if git and git != "unknown":
                return f"git:{git[:12]}"
        except Exception:
            pass

        try:
            p = Path(__file__).resolve()
            st = p.stat()

            try:
                with p.open("rb") as f:
                    head = f.read(64 * 1024)
                h = hashlib.sha1(head + f"|{st.st_size}".encode("utf-8")).hexdigest()[:12]
                return f"file:{h}"
            except Exception:
                h = hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode("utf-8")).hexdigest()[:12]
                return f"file_meta:{h}"
        except Exception:
            return "unknown"

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
        started_at = time_lib.strftime("%Y-%m-%dT%H:%M:%SZ", time_lib.gmtime())
        version = _boot_version_marker()
        py = sys.version.split()[0]
        plat = platform.system() or "unknown"
        osname = os.name
        print(
            f"[BOOT] discord_bot loaded entrypoint={entrypoint} version={version} "
            f"py={py} platform={plat} osname={osname} started_at={started_at} pid={os.getpid()}"
        )
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
