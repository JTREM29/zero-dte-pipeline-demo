import os
import re
import time
import sqlite3
import threading
import random
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Any, Dict, List, Mapping, Optional, Tuple, TypedDict, TYPE_CHECKING
from zoneinfo import ZoneInfo

import requests

from zero_dte_pipeline.tech import build_agent_tech_package
from zero_dte_pipeline.tech.contracts import enforce_line_cap, sanitize_render_text
from market_data.last_price import LastPrice, detect_market_state

try:  # local optional dependency
    from delivery.spx_snapshot import SpxQuoteSnapshot, load_spx_snapshot
except Exception:  # noqa: BLE001 - degrade gracefully if helper unavailable
    SpxQuoteSnapshot = None  # type: ignore[assignment]
    load_spx_snapshot = None  # type: ignore[assignment]


POLYGON_BASE_URL = (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")
ET = ZoneInfo("America/New_York")


class SmartMarketIQPayload(TypedDict, total=False):
    """Schema for market participation JSON snapshots (vendor-neutral)."""

    timestamp_et: str
    data_quality: str
    top_gainers: List[str]
    top_losers: List[str]
    regime: str


_CACHE: Dict[Tuple[str, str], Tuple[float, Any]] = {}

_CACHE_LOCK = threading.Lock()

# Hard bound cache size to avoid memory growth.
_CACHE_MAX = max(int(os.getenv("TNT_ON_DEMAND_CACHE_MAX", "4096")), 0)

# Cap concurrent blocking Polygon HTTP calls (requests.get) across threads.
# This is a process-level guard to avoid rate-limit storms during Discord bursts.
_POLYGON_HTTP_MAX = max(int(os.getenv("TNT_MAX_POLYGON_HTTP", "8")), 1)
_POLYGON_HTTP_SEM = threading.BoundedSemaphore(_POLYGON_HTTP_MAX)

_POLYGON_HTTP_LOCK = threading.Lock()
_POLYGON_HTTP_ACTIVE = 0
_POLYGON_HTTP_PEAK = 0
_POLYGON_HTTP_429_TS: deque[float] = deque()
_POLYGON_HTTP_5XX_TS: deque[float] = deque()

# TTL cache for Polygon last-crypto trade lookups.
# key -> (ts, price, asof_ms)
_POLY_LAST_CRYPTO_TTL: Dict[str, Tuple[float, float, Optional[int]]] = {}
_POLY_LAST_CRYPTO_LOCK = threading.Lock()


def _prune_15m(dq: deque[float], *, now: float) -> None:
    cutoff = float(now) - 900.0
    while dq and float(dq[0]) < cutoff:
        dq.popleft()


def get_polygon_http_stats() -> dict[str, int]:
    """Best-effort health stats for Polygon agg HTTP throttling."""
    now = time.time()
    with _POLYGON_HTTP_LOCK:
        _prune_15m(_POLYGON_HTTP_429_TS, now=now)
        _prune_15m(_POLYGON_HTTP_5XX_TS, now=now)
        return {
            "cap": int(_POLYGON_HTTP_MAX),
            "active": int(_POLYGON_HTTP_ACTIVE),
            "peak_active": int(_POLYGON_HTTP_PEAK),
            "err_429_15m": int(len(_POLYGON_HTTP_429_TS)),
            "err_5xx_15m": int(len(_POLYGON_HTTP_5XX_TS)),
        }

if TYPE_CHECKING:  # pragma: no cover - import-time hinting only
    from delivery.discord_bot import RenderedPost
    from market_data.last_price import LastPrice as PriceSnapshot
else:
    RenderedPost = Any  # type: ignore[assignment]
    PriceSnapshot = Any  # type: ignore[assignment]

def cache_get(sym: str, key: str):
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get((sym, key))
        if not cached:
            return None
        expires_at, value = cached
        if now > expires_at:
            _CACHE.pop((sym, key), None)
            return None
        return value

def cache_set(sym: str, key: str, val: Any, ttl_sec: int) -> None:
    with _CACHE_LOCK:
        _CACHE[(sym, key)] = (time.time() + ttl_sec, val)

        if _CACHE_MAX > 0 and len(_CACHE) > int(_CACHE_MAX):
            # Prune expired first.
            now = time.time()
            expired: list[tuple[str, str]] = []
            for k, (exp, _v) in list(_CACHE.items()):
                try:
                    if float(exp) < now:
                        expired.append(k)
                except Exception:
                    continue
            for k in expired:
                _CACHE.pop(k, None)

            # If still too large, drop arbitrary entries.
            while len(_CACHE) > int(_CACHE_MAX):
                try:
                    _CACHE.pop(next(iter(_CACHE)))
                except Exception:
                    break


def _default_aggs_ttl_sec(timespan: str) -> int:
    ts = (timespan or "").strip().lower()
    if ts in {"second", "sec", "s"}:
        return 10
    if ts in {"minute", "min", "m"}:
        return 15
    if ts in {"hour", "h"}:
        return 60
    if ts in {"day", "d"}:
        return 60 * 60
    return 30


def _polygon_request_json(url: str, *, params: dict[str, Any], timeout: int = 20, max_retries: int = 2) -> Dict[str, Any]:
    # Requests-based Polygon calls are blocking; guard with a semaphore.
    # Basic retry for transient 429/5xx to reduce burst failures.
    try:
        connect_timeout = float(os.getenv("TNT_HTTP_CONNECT_TIMEOUT_SEC", "5"))
    except Exception:
        connect_timeout = 5.0
    try:
        read_timeout = float(os.getenv("TNT_HTTP_READ_TIMEOUT_SEC", "15"))
    except Exception:
        read_timeout = 15.0
    try:
        total_timeout = float(os.getenv("TNT_HTTP_TOTAL_TIMEOUT_SEC", str(timeout)))
    except Exception:
        total_timeout = float(timeout)
    total_timeout = max(1.0, float(total_timeout))
    deadline = time.time() + total_timeout

    try:
        backoff_base = float(os.getenv("TNT_HTTP_RETRY_BASE_SEC", "0.4"))
    except Exception:
        backoff_base = 0.4
    try:
        backoff_cap = float(os.getenv("TNT_HTTP_RETRY_MAX_SEC", "2.0"))
    except Exception:
        backoff_cap = 2.0

    attempt = 0
    while True:
        attempt += 1
        acquired = False
        with _POLYGON_HTTP_SEM:
            acquired = True
            with _POLYGON_HTTP_LOCK:
                global _POLYGON_HTTP_ACTIVE, _POLYGON_HTTP_PEAK
                _POLYGON_HTTP_ACTIVE += 1
                if _POLYGON_HTTP_ACTIVE > _POLYGON_HTTP_PEAK:
                    _POLYGON_HTTP_PEAK = _POLYGON_HTTP_ACTIVE
            try:
                remaining = max(0.0, float(deadline - time.time()))
                if remaining <= 0.0:
                    raise TimeoutError("Polygon request total timeout exceeded")
                # requests timeout=(connect, read)
                try:
                    resp = requests.get(
                        url,
                        params=params,
                        timeout=(float(connect_timeout), float(min(read_timeout, remaining))),
                    )
                except requests.RequestException as exc:
                    # Transient network failure: retry with backoff if allowed.
                    if attempt <= max_retries:
                        delay = min(float(backoff_cap), float(backoff_base) * (2.0 ** float(attempt - 1)))
                        delay = float(delay) + (0.05 * float(backoff_base) * float(random.random()))
                        remaining2 = max(0.0, float(deadline - time.time()))
                        if remaining2 <= 0.0:
                            raise TimeoutError("Polygon request total timeout exceeded") from exc
                        time.sleep(min(delay, remaining2))
                        continue
                    raise
            finally:
                with _POLYGON_HTTP_LOCK:
                    _POLYGON_HTTP_ACTIVE = max(0, int(_POLYGON_HTTP_ACTIVE) - 1)

        # Record transient errors for health dashboards.
        try:
            now = time.time()
            with _POLYGON_HTTP_LOCK:
                if int(resp.status_code) == 429:
                    _POLYGON_HTTP_429_TS.append(now)
                    _prune_15m(_POLYGON_HTTP_429_TS, now=now)
                if int(resp.status_code) in {500, 502, 503, 504}:
                    _POLYGON_HTTP_5XX_TS.append(now)
                    _prune_15m(_POLYGON_HTTP_5XX_TS, now=now)
        except Exception:
            pass

        if resp.status_code in {429, 500, 502, 503, 504} and attempt <= max_retries:
            # Capped exponential backoff with a touch of jitter.
            delay = min(float(backoff_cap), float(backoff_base) * (2.0 ** float(attempt - 1)))
            delay = float(delay) + (0.05 * float(backoff_base) * float(random.random()))
            remaining = max(0.0, float(deadline - time.time()))
            if remaining <= 0.0:
                raise TimeoutError("Polygon request total timeout exceeded")
            time.sleep(min(delay, remaining))
            continue

        resp.raise_for_status()
        return resp.json()


def polygon_last_crypto(pair: str, api_key: str, ttl_sec: int = 10) -> Tuple[Optional[float], Optional[int]]:
    """Best-effort last trade for crypto pair (e.g., 'BTC-USD').

    Returns (price, asof_ms) or (None, None). Never raises.
    Uses a small in-process TTL cache to avoid bursty repeat calls.
    """

    try:
        ttl = int(ttl_sec)
    except Exception:
        ttl = 10
    ttl = max(1, min(int(ttl), 60))

    pair_norm = (pair or "").strip().upper()
    if not pair_norm or not api_key:
        return None, None

    now = float(time.time())
    key = f"last_crypto:{pair_norm}"
    try:
        with _POLY_LAST_CRYPTO_LOCK:
            hit = _POLY_LAST_CRYPTO_TTL.get(key)
        if hit and (now - float(hit[0])) <= float(ttl):
            return float(hit[1]), (int(hit[2]) if hit[2] is not None else None)
    except Exception:
        pass

    # Polygon endpoints vary by style; normalize pair into a path.
    # Accept BTC-USD, BTC/USD, or BTCUSD; prefer BTC/USD.
    path = pair_norm.replace("-", "/")
    if "/" not in path and len(path) >= 6:
        # Heuristic: ABCDEF -> ABC/DEF (e.g., BTCUSD)
        path = path[:-3] + "/" + path[-3:]

    # Prefer base URL env for on-prem / proxies.
    url = f"{POLYGON_BASE_URL}/v1/last/crypto/{path}"

    price: Optional[float] = None
    asof_ms: Optional[int] = None
    try:
        payload = _polygon_request_json(url, params={"apiKey": api_key}, timeout=8, max_retries=1)
        last = payload.get("last") if isinstance(payload, dict) else None
        if isinstance(last, dict):
            raw_px = last.get("price")
            if raw_px is None:
                raw_px = last.get("p")
            raw_ts = last.get("timestamp")
            if raw_ts is None:
                raw_ts = last.get("t")
            if raw_px is not None:
                try:
                    price = float(raw_px)
                except Exception:
                    price = None
            if raw_ts is not None:
                try:
                    asof_ms = int(raw_ts)
                except Exception:
                    asof_ms = None
    except Exception:
        price, asof_ms = None, None

    if price is not None:
        try:
            with _POLY_LAST_CRYPTO_LOCK:
                _POLY_LAST_CRYPTO_TTL[key] = (now, float(price), (int(asof_ms) if asof_ms is not None else None))
        except Exception:
            pass
        return float(price), (int(asof_ms) if asof_ms is not None else None)

    return None, None


def _db_path() -> str:
    return os.getenv("DB_PATH", "db/tnt.db")


def _ensure_last_ticks_table(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS last_ticks (
            symbol TEXT PRIMARY KEY,
            price REAL,
            ts TEXT,
            source TEXT,
            recv_ts TEXT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_last_ticks_ts ON last_ticks(ts)")
    con.commit()


def db_last_tick(symbol: str, *, max_age_sec: float = 5.0) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Return a fresh WS tick from SQLite (price, ts_iso, source) or (None, None, None)."""

    sym = (symbol or "").strip().upper()
    if not sym:
        return None, None, None

    con = sqlite3.connect(_db_path())
    try:
        _ensure_last_ticks_table(con)
        cur = con.cursor()
        row = cur.execute(
            "select price, ts, source from last_ticks where symbol=?",
            (sym,),
        ).fetchone()
        if not row:
            return None, None, None

        price_raw, ts_raw, src_raw = row
        try:
            price = float(price_raw) if price_raw is not None else None
        except Exception:
            price = None

        ts_iso = str(ts_raw) if ts_raw is not None else None
        src = str(src_raw) if src_raw is not None else None

        if price is None or price <= 0 or not ts_iso:
            return None, None, None

        # Freshness check.
        try:
            dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            return None, None, None

        if age_sec > float(max_age_sec):
            return None, None, None

        return price, ts_iso, src
    finally:
        con.close()


def db_latest_close(symbol: str, tf: str) -> Tuple[Optional[float], Optional[str]]:
    con = sqlite3.connect(_db_path())
    try:
        cur = con.cursor()
        row = cur.execute(
            "select close, ts from prices where symbol=? and tf=? order by ts desc limit 1",
            (symbol, tf),
        ).fetchone()
        if not row:
            return None, None
        price_raw, ts_raw = row
        price_val: Optional[float]
        try:
            price_val = float(price_raw) if price_raw is not None else None
        except Exception:
            price_val = None
        ts_str = str(ts_raw) if ts_raw is not None else None
        return price_val, ts_str
    finally:
        con.close()

def db_latest_ts(symbol: str, tf: str) -> Optional[str]:
    con = sqlite3.connect(_db_path())
    try:
        cur = con.cursor()
        row = cur.execute(
            "select max(ts) from prices where symbol=? and tf=?",
            (symbol, tf),
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()

def bar_age_min(ts_iso: str) -> float:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def _ts_to_et(ts_iso: Optional[str]) -> Optional[datetime]:
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)

def is_fresh(symbol: str, tf: str = "1m", max_min: float = 3.0) -> bool:
    ts = db_latest_ts(symbol, tf)
    if not ts:
        return False
    return bar_age_min(ts) <= max_min


def _polygon_key() -> str:
    key = os.getenv("POLYGON_API_KEY", "")
    if not key:
        raise RuntimeError("POLYGON_API_KEY not set")
    return key

def polygon_snapshot(symbol: str) -> Dict[str, Any]:
    url = f"{POLYGON_BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}"
    return _polygon_request_json(
        url,
        params={"apiKey": _polygon_key()},
        timeout=15,
        max_retries=2,
    )


def polygon_index_snapshot(index_symbol: str) -> Dict[str, Any]:
    """Fetch Polygon index snapshot.

    Examples:
    - "I:VIX"
    - "I:SPX"

    This returns the raw vendor payload; callers should extract only small
    contract-safe fields.
    """

    sym = (index_symbol or "").strip()
    if not sym:
        raise ValueError("index_symbol cannot be blank")
    sym_enc = quote(sym, safe="")
    url = f"{POLYGON_BASE_URL}/v2/snapshot/indices/{sym_enc}"
    return _polygon_request_json(
        url,
        params={"apiKey": _polygon_key()},
        timeout=15,
        max_retries=2,
    )


def extract_index_last_prev_close_ts(payload: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Best-effort extraction of (last, prev_close, ts_iso) from an index snapshot."""

    if not isinstance(payload, dict):
        return None, None, None

    last: Optional[float] = None
    prev_close: Optional[float] = None
    ts_iso: Optional[str] = None

    # Some Polygon index snapshots include `value` as a number or a dict.
    value = payload.get("value")
    if isinstance(value, (int, float)):
        try:
            last = float(value)
        except Exception:
            last = None
    elif isinstance(value, dict):
        for k in ("price", "close", "value", "last"):
            if value.get(k) is not None:
                try:
                    last = float(value.get(k))
                    break
                except Exception:
                    last = None

    # Some responses may include `last`, `close`, or nested session values.
    if last is None:
        for k in ("last", "close", "c"):
            if payload.get(k) is not None:
                try:
                    last = float(payload.get(k))
                    break
                except Exception:
                    last = None

    # Previous close (best effort).
    prev = payload.get("prevDay")
    if isinstance(prev, dict):
        for k in ("c", "close", "price"):
            if prev.get(k) is not None:
                try:
                    prev_close = float(prev.get(k))
                    break
                except Exception:
                    prev_close = None
    if prev_close is None and payload.get("prev_close") is not None:
        try:
            prev_close = float(payload.get("prev_close"))
        except Exception:
            prev_close = None

    # Timestamp (best effort).
    for raw_ts in (payload.get("updated"), payload.get("lastUpdated"), payload.get("t"), payload.get("timestamp")):
        ts_iso = _ts_to_iso(raw_ts)
        if ts_iso:
            break

    return last, prev_close, ts_iso


def polygon_aggs(
    symbol: str,
    *,
    multiplier: int,
    timespan: str,
    days: int,
    cache_ttl_sec: int | None = None,
) -> Dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return {}

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=int(days))
    end_date = now.strftime("%Y-%m-%d")
    start_date = start.strftime("%Y-%m-%d")

    ttl = _default_aggs_ttl_sec(timespan) if cache_ttl_sec is None else max(int(cache_ttl_sec), 0)
    cache_key = f"aggs:v2:{sym}:{int(multiplier)}:{(timespan or '').strip().lower()}:{start_date}:{end_date}:asc:adj"
    if ttl > 0:
        cached = cache_get(sym, cache_key)
        if isinstance(cached, dict):
            return cached

    url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{sym}/range/{int(multiplier)}/{timespan}/{start_date}/{end_date}"
    payload = _polygon_request_json(
        url,
        params={
            "apiKey": _polygon_key(),
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
        },
        timeout=20,
        max_retries=2,
    )

    if ttl > 0:
        cache_set(sym, cache_key, payload, ttl)
    return payload

def polygon_aggs_1m(symbol: str, minutes: int = 180, *, cache_ttl_sec: int | None = None) -> Dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return {}

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=int(minutes))
    end_date = now.strftime("%Y-%m-%d")
    start_date = start.strftime("%Y-%m-%d")

    ttl = _default_aggs_ttl_sec("minute") if cache_ttl_sec is None else max(int(cache_ttl_sec), 0)
    cache_key = f"aggs:v2:{sym}:1:minute:{start_date}:{end_date}:asc:adj"
    if ttl > 0:
        cached = cache_get(sym, cache_key)
        if isinstance(cached, dict):
            return cached

    url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{sym}/range/1/minute/{start_date}/{end_date}"
    payload = _polygon_request_json(
        url,
        params={
            "apiKey": _polygon_key(),
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
        },
        timeout=20,
        max_retries=2,
    )

    if ttl > 0:
        cache_set(sym, cache_key, payload, ttl)
    return payload


def fetch_last_agg_close(symbol: str, api_key: str):
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(minutes=15)).date().isoformat()
    to = now.date().isoformat()

    sym_enc = quote(str(symbol or "").strip(), safe="")
    url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{sym_enc}/range/1/minute/{frm}/{to}"
    try:
        data = _polygon_request_json(
            url,
            params={
                "adjusted": "true",
                "sort": "desc",
                "limit": 50,
                "apiKey": api_key,
            },
            timeout=10,
            max_retries=1,
        )
    except Exception:
        return None

    results = data.get("results") or []
    if not results:
        return None

    last = results[0]
    try:
        return float(last["c"]), "aggs(1m).c", last.get("t")
    except Exception:
        return None

def _ts_to_iso(raw: Any) -> Optional[str]:
    if raw is None:
        return None

    # Snapshot fields can be numeric epoch (s/ms/ns) or ISO strings (e.g. ticker.updated).
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            value = float(s)
        except Exception:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        else:
            if value == 0:
                return None
    else:
        try:
            value = float(raw)
        except Exception:
            return None

    if value == 0:
        return None

    try:
        if value > 1e18:  # nanoseconds
            dt = datetime.fromtimestamp(value / 1e9, tz=timezone.utc)
        elif value > 1e12:  # milliseconds
            dt = datetime.fromtimestamp(value / 1e3, tz=timezone.utc)
        else:  # assume seconds
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def _extract_snapshot_price(snap: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    t = (snap or {}).get("ticker") or {}

    lt = t.get("lastTrade") or {}
    if isinstance(lt, dict) and lt.get("p") is not None:
        try:
            px = float(lt["p"])
        except Exception:
            px = 0.0
        if px > 0:
            return px, "lastTrade"

    lq = t.get("lastQuote") or {}
    if isinstance(lq, dict):
        bp = lq.get("bp")
        ap = lq.get("ap")
        if bp is None:
            bp = lq.get("bid")
        if bp is None and "p" in lq:
            try:
                bp = float(lq.get("p"))
            except Exception:
                bp = None
        if ap is None:
            ap = lq.get("ask")
        if ap is None and "P" in lq:
            try:
                ap = float(lq.get("P"))
            except Exception:
                ap = None
        if bp is not None and ap is not None:
            try:
                mid = (float(bp) + float(ap)) / 2.0
            except Exception:
                mid = 0.0
            if mid > 0:
                return mid, "lastQuote(mid)"
        if bp is not None:
            try:
                bid = float(bp)
            except Exception:
                bid = 0.0
            if bid > 0:
                return bid, "lastQuote(bid)"
        if ap is not None:
            try:
                ask = float(ap)
            except Exception:
                ask = 0.0
            if ask > 0:
                return ask, "lastQuote(ask)"

    mn = t.get("min") or {}
    if isinstance(mn, dict) and mn.get("c") is not None:
        try:
            px = float(mn["c"])
        except Exception:
            px = 0.0
        if px > 0:
            return px, "min.c"

    pd = t.get("prevDay") or {}
    if isinstance(pd, dict) and pd.get("c") is not None:
        try:
            px = float(pd["c"])
        except Exception:
            px = 0.0
        if px > 0:
            return px, "prevDay.c"

    return None, None


def _snapshot_timestamp(snap: Dict[str, Any], source: Optional[str]) -> Optional[str]:
    def _approx_prev_rth_close_iso() -> str:
        now_utc = datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)

        close_date = now_et.date()
        # If it's before the (approx) close time today, use the prior weekday close.
        if (now_et.hour * 60 + now_et.minute) < (16 * 60):
            close_date = close_date - timedelta(days=1)

        # Roll back to the most recent weekday.
        while close_date.weekday() >= 5:
            close_date = close_date - timedelta(days=1)

        close_et = datetime(close_date.year, close_date.month, close_date.day, 16, 0, tzinfo=ET)
        return close_et.astimezone(timezone.utc).isoformat()

    ticker = (snap or {}).get("ticker") or {}
    raw = None
    if source == "lastTrade":
        raw = (ticker.get("lastTrade") or {}).get("t")
    elif source and source.startswith("lastQuote"):
        raw = (ticker.get("lastQuote") or {}).get("t")
    elif source == "min.c":
        raw = (ticker.get("min") or {}).get("t")
    elif source == "prevDay.c":
        raw = (ticker.get("prevDay") or {}).get("t")

    if raw is None:
        raw = ticker.get("updated")

    ts = _ts_to_iso(raw)
    if ts:
        return ts

    # Snapshot sometimes returns prices (e.g., prevDay.c) but omits timestamps (or gives 0).
    # Synthesize an approximate prior RTH close timestamp so downstream validation can succeed.
    if source == "prevDay.c":
        return _approx_prev_rth_close_iso()
    return None


def fetch_live_price(symbol: str) -> Tuple[Optional[float], Optional[str], str]:
    sym = (symbol or "").strip()
    sym_upper = sym.upper()
    is_index = sym_upper.startswith("I:")

    # Prefer a fresh WS tick cache if available.
    try:
        ws_price, ws_ts, ws_src = db_last_tick(sym, max_age_sec=5.0)
    except Exception:
        ws_price, ws_ts, ws_src = None, None, None
    if ws_price is not None and ws_ts:
        return float(ws_price), ws_ts, f"ws-cache:{ws_src or 'last_ticks'}"

    cached = cache_get(sym, "snapshot_price")
    if cached:
        return cached

    snap = None
    if is_index:
        try:
            snap = polygon_index_snapshot(sym_upper)
        except Exception:
            snap = None
    else:
        try:
            snap = polygon_snapshot(sym)
        except Exception:
            snap = None

    price = None
    src_key: Optional[str] = None
    ts_iso: Optional[str] = None
    source_label = "snapshot"

    if snap:
        if is_index:
            last, prev_close, idx_ts = extract_index_last_prev_close_ts(snap)
            if last is not None:
                price = float(last)
                ts_iso = idx_ts
                source_label = "index-snapshot"
            elif prev_close is not None:
                price = float(prev_close)
                ts_iso = idx_ts
                source_label = "index-prev_close"
        else:
            snap_price, snap_key = _extract_snapshot_price(snap)
            if snap_price is not None:
                price = float(snap_price)
                src_key = snap_key
                ts_iso = _snapshot_timestamp(snap, snap_key)
                source_label = f"snapshot:{snap_key or 'unknown'}"

    if price is None:
        # Fallback to recent 1m aggregation close when snapshot lacks a usable price.
        try:
            agg = fetch_last_agg_close(sym, _polygon_key())
        except RuntimeError:
            return None, None, "api-key-missing"

        if not agg:
            return None, None, "snapshot-unavailable"

        price, agg_src, t_raw = agg
        ts_iso = _ts_to_iso(t_raw)
        source_label = str(agg_src or "agg")

    payload = (float(price), ts_iso, source_label)
    cache_set(sym, "snapshot_price", payload, ttl_sec=5)
    cache_set(sym, "sym_supported", True, ttl_sec=600)
    return payload


def _resolve_on_demand_price(
    symbol: str,
    now_et: datetime,
    *,
    allow_closed: bool,
) -> Dict[str, Any]:
    field_defaults: Dict[str, Any] = {
        "snapshot": None,
        "last_price": None,
        "last_price_ts": None,
        "mode": "UNAVAILABLE",
        "age_minutes": None,
        "source": "none",
        "accepted": False,
    }
    market_state = detect_market_state(now_et)

    try:
        live_price, live_ts, live_src = fetch_live_price(symbol)
    except Exception:
        live_price, live_ts, live_src = None, None, "error"

    if live_price is not None and live_ts:
        try:
            age_min = bar_age_min(live_ts)
        except Exception:
            age_min = None
        snapshot = LastPrice(
            symbol,
            float(live_price),
            _ts_to_et(live_ts),
            "polygon_snapshot_lastTrade",
            market_state,
            age_min,
            True,
        )
        return {
            **field_defaults,
            "snapshot": snapshot,
            "last_price": float(live_price),
            "last_price_ts": live_ts,
            "mode": "LIVE",
            "age_minutes": age_min,
            "source": live_src,
            "accepted": True,
        }

    price_1m, ts_1m = db_latest_close(symbol, "1m")
    source = "db_1m_close"
    if price_1m is None or not ts_1m:
        price_1m, ts_1m = db_latest_close(symbol, "1d")
        source = "db_1d_close"

    if price_1m is None or not ts_1m:
        snapshot = LastPrice(symbol, None, None, "none", market_state, None, False, "missing_last_price")
        return {
            **field_defaults,
            "snapshot": snapshot,
        }

    try:
        age_min = bar_age_min(ts_1m)
    except Exception:
        age_min = None
    accepted = allow_closed
    reason = None if accepted else "closed_price_blocked"
    snapshot = LastPrice(
        symbol,
        float(price_1m),
        _ts_to_et(ts_1m),
        source if source in {"db_1m_close", "db_1d_close"} else "db_1m_close",
        market_state,
        age_min,
        accepted,
        reason,
    )
    return {
        **field_defaults,
        "snapshot": snapshot,
        "last_price": float(price_1m),
        "last_price_ts": ts_1m,
        "mode": "CLOSED",
        "age_minutes": age_min,
        "source": source,
        "accepted": accepted,
    }

def symbol_supported_polygon(symbol: str) -> bool:
    entry = _CACHE.get((symbol, "sym_supported"))
    if entry:
        expires_at, value = entry
        if time.time() <= expires_at:
            if value is True:
                return True
            if value is False:
                return False
        else:
            _CACHE.pop((symbol, "sym_supported"), None)

    try:
        price, ts_iso, _ = fetch_live_price(symbol)
        ok = price is not None and ts_iso is not None
    except Exception:
        ok = False
    ttl = 600 if ok else 30
    cache_set(symbol, "sym_supported", ok, ttl_sec=ttl)
    return ok


def _get_delivery_module():
    import delivery.discord_bot as delivery  # local import to avoid circular dependency

    return delivery


def _build_last_price_section(symbols: List[str], delivery_module: Any) -> str:
    now_et = delivery_module._now_et()
    session = delivery_module.market_session_et(now_et.astimezone(timezone.utc))
    try:
        block = delivery_module._format_last_price_section(symbols, now_et=now_et, session=session)
    except Exception:
        return "💵 **Last Price**\n• (unavailable)"
    return block or "💵 **Last Price**\n• (unavailable)"


def _build_pivots_section(symbol: str, delivery_module: Any) -> Optional[str]:
    info = delivery_module.get_latest_daily_pivots(symbol)
    piv = info.get("piv") if isinstance(info, dict) else None
    if not isinstance(piv, dict):
        return None

    # Full classic pivot ladder (R above pivot, S below pivot).
    ladder_keys = ("R3", "R2", "R1", "P", "S1", "S2", "S3")
    parts: List[str] = []
    for key in ladder_keys:
        val = piv.get(key)
        if val is None:
            continue
        try:
            parts.append(f"{key} {float(val):.2f}")
        except Exception:
            continue

    if not parts:
        return None

    ts_iso = info.get("ts") if isinstance(info, dict) else None
    session = info.get("session") if isinstance(info, dict) else None
    stamp = delivery_module._format_ts_et(ts_iso) if ts_iso else None

    lines: List[str] = ["📐 **Pivots (RTH)**", f"• {' | '.join(parts)}"]
    if session:
        lines.append(f"• Session: {session}")
    if stamp:
        lines.append(f"• Source: {stamp}")
    return "\n".join(lines)


def _build_signal_section(
    symbol: str,
    delivery_module: Any,
    *,
    verbose: bool,
    now_et: datetime,
    price_snapshot: Optional[PriceSnapshot],
) -> tuple[Optional[str], Optional[dict], Optional[dict]]:
    try:
        built = delivery_module.build_signal_payload(symbol)
    except Exception:
        return None, None, None
    if not built:
        return None, None, None

    payload, _prob, gate = built
    text = delivery_module.format_signal_clean(
        symbol,
        payload,
        verbose=verbose,
        now_et=now_et,
        gate=gate,
        price_snapshot=price_snapshot,
    )
    return text, payload, gate


def _normalize_symbol(symbol: str) -> str:
    raw = (symbol or "").strip().upper()
    if not raw:
        return ""

    match = re.match(r"^I\s*[: ]\s*([A-Z0-9]+)$", raw)
    if match:
        return f"I:{match.group(1)}"

    return "".join(ch for ch in raw if ch.isalnum())


def _spx_addon_enabled() -> bool:
    raw = os.getenv("ENABLE_ON_DEMAND_SPX", "1")
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _build_spx_addon_block(delivery_module: Any) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not _spx_addon_enabled() or load_spx_snapshot is None:
        return None, None

    session = delivery_module.market_session_et()

    try:
        snapshot: Optional[SpxQuoteSnapshot] = load_spx_snapshot(session=session)
    except Exception:  # noqa: BLE001
        snapshot = None

    try:
        piv_info = delivery_module.get_latest_daily_pivots("SPX")
    except Exception:  # noqa: BLE001
        piv_info = None

    piv = piv_info.get("piv") if isinstance(piv_info, dict) else None
    pivot_val: Optional[float] = None
    if isinstance(piv, dict) and piv.get("P") is not None:
        try:
            pivot_val = float(piv["P"])
        except Exception:  # noqa: BLE001
            pivot_val = None

    header = "**SPX (IQFeed)**"
    lines: List[str] = [header]
    meta: Dict[str, Any] = {"session": session}
    if pivot_val is not None:
        meta["pivot"] = pivot_val

    if snapshot is None:
        if session in {"WEEKEND", "CLOSED"}:
            lines.append("• Market closed — SPX live tape unavailable")
            meta.update({"status": "UNAVAILABLE", "reason": "market_closed_no_snapshot"})
        else:
            lines.append("• SPX data unavailable (IQFeed feed offline)")
            meta.update({"status": "UNAVAILABLE", "reason": "no_snapshot"})
        return "\n".join(lines), meta

    ts_iso: Optional[str] = None
    if snapshot.ts_utc is not None:
        ts_iso = snapshot.ts_utc.astimezone(timezone.utc).isoformat()
    stamp = delivery_module._format_ts_et(ts_iso) if ts_iso else "n/a"

    if snapshot.message:
        lines.append(f"• {snapshot.message}")

    if snapshot.last is not None:
        label = "Last"
        if snapshot.status == "CLOSED":
            label = "Last settled"
        elif snapshot.status == "STALE":
            label = "Last (stale)"
        lines.append(f"• {label}: {snapshot.last:,.2f} ({stamp})")

    if pivot_val is not None and snapshot.last is not None:
        delta = snapshot.last - pivot_val
        direction = "above" if delta >= 0 else "below"
        pivot_label = "Prior Pivot" if snapshot.status == "CLOSED" else "Pivot"
        lines.append(f"• {pivot_label}: {pivot_val:,.2f} ({abs(delta):.2f} {direction})")

    tape_bits: List[str] = []
    if snapshot.tape_bias:
        tape_bits.append(f"Tape: {snapshot.tape_bias}")
    if snapshot.trend:
        tape_bits.append(f"Trend: {snapshot.trend}")
    if tape_bits:
        lines.append("• " + " | ".join(tape_bits))

    # Indicators may be computed internally (e.g., VWAP/RSI) but must not be displayed.
    if snapshot.status == "STALE":
        lines.append("• Permission: AGGRESSION PROHIBITED (stale tape)")
    elif snapshot.rsi is not None and (snapshot.rsi <= 30.0 or snapshot.rsi >= 70.0):
        lines.append("• Policy: Countertrend attempts are high risk")

    sr_bits: List[str] = []
    if snapshot.support is not None:
        sr_bits.append(f"S {snapshot.support:,.2f}")
    if snapshot.resistance is not None:
        sr_bits.append(f"R {snapshot.resistance:,.2f}")
    if sr_bits:
        lines.append("• " + " | ".join(sr_bits))

    if snapshot.sample_count:
        lines.append(f"• Samples: {snapshot.sample_count}")

    meta.update(
        {
            "ts_iso": ts_iso,
            "trend": snapshot.trend,
            "tape_bias": snapshot.tape_bias,
            "sample_count": snapshot.sample_count,
            "vwap": snapshot.vwap,
            "rsi": snapshot.rsi,
            "support": snapshot.support,
            "resistance": snapshot.resistance,
            "status": snapshot.status,
            "freshness_sec": snapshot.freshness_sec,
            "message": snapshot.message,
        }
    )

    if pivot_val is not None and snapshot.last is not None:
        meta["pivot_delta"] = snapshot.last - pivot_val

    return "\n".join(lines), meta


def _build_technical_package(
    symbol: str,
    delivery_module: Any,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], str, str]:
    try:
        bars_by_tf = delivery_module._agent_bars_for_symbol(symbol)
    except Exception:  # noqa: BLE001 - fallback on failure
        bars_by_tf = {}

    if not bars_by_tf:
        return None, None, "MISSING", "MISSING"

    try:
        levels, custom_levels = delivery_module._agent_levels_for_symbol(symbol)
    except Exception:  # noqa: BLE001 - indicators proceed without custom levels
        levels = {}
        custom_levels = []

    try:
        package = build_agent_tech_package(
            symbol=symbol,
            bars_by_tf=bars_by_tf,
            levels=levels or None,
            custom_levels=custom_levels or None,
        )
    except Exception:  # noqa: BLE001 - propagate as missing for UX continuity
        return None, None, "ERROR", "ERROR"

    tech_state = package.get("technical_state") if isinstance(package, dict) else None
    pattern_candidates = package.get("pattern_candidates") if isinstance(package, dict) else None

    tech_quality = "FRESH" if tech_state else "MISSING"
    pattern_quality = "FRESH" if pattern_candidates else "MISSING"
    return tech_state, pattern_candidates, tech_quality, pattern_quality


def _compose_analyze_render(
    delivery_module: Any,
    *,
    text: str,
    symbol: str,
    stand_down: bool,
    missing: List[str],
    context_mode: Optional[str] = None,
    output_mode: Optional[str] = None,
    context_source: Optional[str] = None,
    analysis_mode: Optional[str] = None,
    analysis_source: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
    technical_state: Optional[Dict[str, Any]] = None,
    pattern_candidates: Optional[Dict[str, Any]] = None,
    technical_quality: Optional[str] = None,
    pattern_quality: Optional[str] = None,
    price_quality: Optional[str] = None,
    scenario_flags: Optional[Dict[str, Any]] = None,
) -> "RenderedPost":
    prepared_text = sanitize_render_text(text or "")
    prepared_text = enforce_line_cap(prepared_text)
    prepared_text = sanitize_render_text(prepared_text)

    symbol_meta = symbol or "N/A"
    symbols_meta = [symbol_meta] if symbol_meta and symbol_meta != "N/A" else []

    if stand_down:
        dq_state = "MISSING_INPUTS"
    else:
        if technical_quality:
            dq_state = technical_quality
        elif technical_state:
            dq_state = "FRESH"
        else:
            dq_state = "MISSING"

    data_quality: Dict[str, Any] = {"technical_state": dq_state}
    if price_quality:
        data_quality["price"] = price_quality
    if pattern_quality:
        data_quality["pattern_candidates"] = pattern_quality
    elif pattern_candidates:
        data_quality["pattern_candidates"] = "FRESH"
    elif dq_state != "FRESH":
        data_quality["pattern_candidates"] = dq_state
    if stand_down and missing:
        data_quality["missing_sections"] = list(missing)

    resolved_mode = (context_mode or analysis_mode or "db").strip().lower()
    if resolved_mode not in {"db", "on_demand", "stand_down"}:
        resolved_mode = "db"

    resolved_output = (output_mode or "strict").strip().lower()
    if resolved_output not in {"strict", "insights"}:
        resolved_output = "strict"

    resolved_source = (context_source or analysis_source or "model").strip()
    if not resolved_source:
        resolved_source = "model"

    meta: Dict[str, Any] = {
        "post_type": "on_demand_analyze",
        "symbols": symbols_meta,
        "trade_context_mode": resolved_mode,
        "trade_context_output_mode": resolved_output,
        "trade_context_source": resolved_source,
        "analysis_mode": resolved_mode,
        "analysis_output_mode": resolved_output,
        "analysis_source": resolved_source,
        "stand_down": stand_down,
        "missing_sections": list(missing),
        "data_quality": data_quality,
        "display_symbol": symbol_meta,
    }
    if scenario_flags:
        meta["scenario_flags"] = dict(scenario_flags)
    if extras:
        meta.update(extras)

    payload: Dict[str, Any] = {
        "type": "on_demand_analyze",
        "symbol": symbol_meta,
        "meta": meta,
    }
    if technical_state:
        payload["technical_state"] = technical_state
    if pattern_candidates:
        payload["pattern_candidates"] = pattern_candidates

    return delivery_module.RenderedPost(text=prepared_text, agent_payload=payload)


def _build_analyze_stand_down_render(
    delivery_module: Any,
    *,
    symbol_display: str,
    symbol_meta: str,
    missing: List[str],
    reason: str,
    action_line: str,
    last_price_section: Optional[str],
    futures_block: Optional[str],
    price_context: Optional[Dict[str, Any]] = None,
    signal_payload: Optional[Dict[str, Any]] = None,
    spx_block: Optional[str] = None,
    spx_meta: Optional[Dict[str, Any]] = None,
    issue: Optional[str] = None,
) -> "RenderedPost":
    lines: List[str] = [f"⚠️ STAND DOWN — {reason}"]

    # Always anchor the response with a deterministic numeric pack when possible.
    try:
        from delivery.numeric_bias_pack import format_numeric_bias_pack_from_signal_payload

        if isinstance(signal_payload, dict):
            pack = format_numeric_bias_pack_from_signal_payload(
                symbol=symbol_display,
                signal_payload=signal_payload,
                price_context=price_context,
            )
            if pack:
                lines.extend(["", pack])
    except Exception:
        pass

    lines.append(f"Symbol: **{symbol_display or 'N/A'}**")
    if missing:
        lines.append(f"Missing: {', '.join(missing)}")
    if issue:
        lines.append(f"Issue: {issue}")
    lines.append(action_line)
    lines.append("_Not financial advice._")

    if last_price_section:
        lines.extend(["", last_price_section])
    if futures_block:
        lines.extend(["", futures_block])
    if spx_block:
        lines.extend(["", spx_block])

    text = "\n".join(lines)
    extras: Dict[str, Any] = {
        "display_symbol": symbol_display or "N/A",
        "issue": issue,
        "last_price_available": bool(last_price_section),
        "futures_section": bool(futures_block),
        "spx_addon": {
            "enabled": bool(spx_block),
            "source": "iqfeed_sqlite" if spx_block else None,
            "metrics": spx_meta,
        },
    }

    # Ensure downstream /ask has enough context to avoid treating the feed as DOWN
    # when price is live but other sections (signal/levels) are missing.
    if price_context:
        extras["price_context"] = dict(price_context)
    if signal_payload:
        extras["signal_payload"] = dict(signal_payload)

    return _compose_analyze_render(
        delivery_module,
        text=text,
        symbol=symbol_meta,
        stand_down=True,
        missing=missing,
        context_mode="stand_down",
        output_mode="strict",
        context_source="stand_down",
        extras=extras,
    )


def _build_regime_section(symbol: str, delivery_module: Any) -> Optional[str]:
    try:
        regime_lines = delivery_module.build_hourly_regime_block(symbol) or []
    except Exception:
        regime_lines = []
    regime_lines = [line for line in regime_lines if line and line.strip()]
    if not regime_lines:
        return None
    return "🧭 **Regime Snapshot**\n" + "\n".join(regime_lines)


def _build_on_demand_data_snapshot_block(
    *,
    symbol: str,
    price_ctx: Mapping[str, Any],
    signal_payload: Optional[Mapping[str, Any]],
    technical_state: Optional[Mapping[str, Any]],
) -> Optional[str]:
    """Return a deterministic snapshot block for on-demand /analyze.

    Designed to be short, consistent across symbols, and fully data-grounded.
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    last_price = price_ctx.get("last_price")
    age_minutes = price_ctx.get("age_minutes")
    source = price_ctx.get("source")

    def _fmt_money(val: object, nd: int = 2) -> str:
        try:
            return f"{float(val):,.{nd}f}" if val is not None else "n/a"
        except Exception:  # noqa: BLE001
            return "n/a"

    def _fmt_num(val: object, nd: int = 2) -> str:
        try:
            return f"{float(val):.{nd}f}" if val is not None else "n/a"
        except Exception:  # noqa: BLE001
            return "n/a"

    bits: list[str] = []

    if isinstance(last_price, (int, float)):
        age_txt = "n/a"
        if isinstance(age_minutes, (int, float)):
            age_txt = f"{float(age_minutes):.1f}m"
        src_txt = str(source) if source else "UNKNOWN"
        bits.append(f"Last {_fmt_money(last_price)} (age {age_txt}, src {src_txt})")

    # TNT regime (discipline gate) snapshot
    if isinstance(signal_payload, Mapping):
        tnt_ctx = signal_payload.get("tnt")
        if isinstance(tnt_ctx, Mapping):
            tnt_regime = str(tnt_ctx.get("regime") or "UNKNOWN").upper()
            tnt_posture = str(tnt_ctx.get("posture") or "UNKNOWN").upper()
            conf = tnt_ctx.get("confidence")
            conf_txt = "n/a"
            if isinstance(conf, (int, float)):
                conf_txt = f"{float(conf):.2f}"
            bits.append(f"TNT: {tnt_regime} | posture {tnt_posture} | conf {conf_txt}")

    p = signal_payload.get("pivot") if isinstance(signal_payload, Mapping) else None
    s1 = signal_payload.get("s1") if isinstance(signal_payload, Mapping) else None
    r1 = signal_payload.get("r1") if isinstance(signal_payload, Mapping) else None
    if any(isinstance(x, (int, float)) for x in (p, s1, r1)):
        bits.append(f"P {_fmt_money(p)} | S1 {_fmt_money(s1)} | R1 {_fmt_money(r1)}")

    # technical_state is shaped like {SYMBOL: {timeframes: {tf: {...}}}}
    tf_exec = (
        str(signal_payload.get("tf_exec"))
        if isinstance(signal_payload, Mapping) and signal_payload.get("tf_exec")
        else "5m"
    )
    tf_state = None
    if isinstance(technical_state, Mapping):
        sym_state = technical_state.get(sym)
        if isinstance(sym_state, Mapping):
            tf_map = sym_state.get("timeframes") if isinstance(sym_state.get("timeframes"), Mapping) else {}
            tf_state = tf_map.get(tf_exec)
            if not isinstance(tf_state, Mapping):
                for candidate in ("5m", "1m", "60m", "1D"):
                    tf_state = tf_map.get(candidate)
                    if isinstance(tf_state, Mapping):
                        tf_exec = candidate
                        break

    if isinstance(tf_state, Mapping):
        vwap = tf_state.get("vwap")
        rsi_14 = tf_state.get("rsi_14")
        macd_hist = tf_state.get("macd_hist")
        macd_hist_pct = tf_state.get("macd_hist_pct")
        trend = tf_state.get("trend") if isinstance(tf_state.get("trend"), Mapping) else None

        tf_bits: list[str] = [f"tf {tf_exec}"]

        if isinstance(vwap, (int, float)) and isinstance(last_price, (int, float)):
            try:
                delta = float(last_price) - float(vwap)
                side = "above" if delta >= 0 else "below"
                tf_bits.append(f"VWAP {side} {_fmt_num(abs(delta), 2)}")
            except Exception:  # noqa: BLE001
                pass

        if isinstance(trend, Mapping):
            direction = trend.get("direction")
            strength = trend.get("strength")
            if isinstance(direction, str) and direction:
                if isinstance(strength, (int, float)):
                    tf_bits.append(f"Trend {direction} ({_fmt_num(strength, 2)})")
                else:
                    tf_bits.append(f"Trend {direction}")

        if isinstance(rsi_14, (int, float)):
            tf_bits.append(f"RSI14 {_fmt_num(rsi_14, 1)}")

        if isinstance(macd_hist, (int, float)):
            if isinstance(macd_hist_pct, (int, float)):
                tf_bits.append(f"MACD hist {_fmt_num(macd_hist, 4)} ({_fmt_num(macd_hist_pct, 3)}%)")
            else:
                tf_bits.append(f"MACD hist {_fmt_num(macd_hist, 4)}")

        if len(tf_bits) > 1:
            bits.append(" | ".join(tf_bits))

    if not bits:
        return None

    return "📌 **Data Snapshot**\n" + "\n".join(f"• {b}" for b in bits)


def _macro_regime_state_snapshot_sync() -> Optional[dict]:
    """Best-effort snapshot of shared macro regime state (from /risk_on_off).

    This is intentionally dependency-light and safe under partial initialization.
    """

    try:
        # Local import to avoid circular import at module load time.
        import cli.discord_bot as cli_bot  # type: ignore
    except Exception:
        return None

    try:
        snap = dict(getattr(cli_bot, "_MACRO_REGIME_STATE", {}) or {})
        if not snap:
            return None

        # Enforce the same max-age semantics used elsewhere.
        max_age = int(getattr(cli_bot, "_macro_state_max_age_sec")())
        ts = float(snap.get("ts") or 0.0)
        if ts > 0 and (time.time() - ts) > float(max_age):
            return None
        return snap
    except Exception:
        return None


def _conflict_one_liner(taxonomy: str) -> str:
    t = (taxonomy or "").strip().upper()
    if not t:
        return "Mixed cross-asset signals."
    mapping = {
        "GROWTH_VS_DURATION": "Growth vs duration mismatch.",
        "GROWTH_vs_DURATION": "Growth vs duration mismatch.",
        "SMALLCAP_LAG": "Small-caps lagging risk proxies.",
        "COMMODITY_SPIKE": "Commodity impulse distorting risk read.",
        "DEFENSIVE_BID": "Defensive bid conflicting with risk-on.",
    }
    return mapping.get(t, "Mixed cross-asset signals.")


def _format_asof_et_from_ts(ts: float) -> Optional[str]:
    try:
        if not ts or ts <= 0:
            return None
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(ET)
        return dt.strftime("%b %d %H:%M ET")
    except Exception:
        return None


def _build_macro_futures_context_section(now_iso: str, delivery_module: Any) -> str:
    # NOTE: No new API calls. Uses the macro regime state already cached/persisted by /risk_on_off.
    header = "📈 **Macro Futures Context (Proxies)**"

    state = _macro_regime_state_snapshot_sync()
    if not state:
        return "\n".join([header, "• Macro state unavailable (run /risk_on_off)"])

    regime_label = str(state.get("regime_label") or "").strip() or "Mixed"
    action = str(state.get("action") or "").strip() or "HOLD"
    try:
        agreement = int(state.get("agreement")) if state.get("agreement") is not None else None
    except Exception:
        agreement = None
    try:
        strength = float(state.get("strength_sigma")) if state.get("strength_sigma") is not None else None
    except Exception:
        strength = None
    try:
        stability = int(state.get("stability_days")) if state.get("stability_days") is not None else None
    except Exception:
        stability = None

    bits: list[str] = [f"ES→SPY: {regime_label} ({action})"]
    if agreement is not None:
        bits.append(f"{agreement}/4")
    if strength is not None:
        bits.append(f"{strength:.1f}σ")
    if stability is not None:
        bits.append(f"{stability}d")

    lines: list[str] = [
        header,
        "• " + " | ".join(bits),
        "• NQ→QQQ: Growth leadership proxy (QQQ/SPY).",
        "• RTY→IWM: Broad participation proxy (IWM/SPY).",
        "• ZN→TLT: Duration bid proxy (TLT/SHY).",
        "• CL→USO: Commodity/inflation impulse proxy (USO/SPY).",
    ]

    try:
        if bool(state.get("conflict_flag")):
            tax = str(state.get("conflict_taxonomy") or "").strip()
            hint = _conflict_one_liner(tax)
            if tax:
                lines.append(f"⚠️ Conflict: {tax} — {hint}")
            else:
                lines.append(f"⚠️ Conflict: {hint}")
    except Exception:
        pass

    try:
        asof = _format_asof_et_from_ts(float(state.get("ts") or 0.0))
    except Exception:
        asof = None
    if asof:
        lines.append(f"As-of {asof}")

    return "\n".join([ln for ln in lines if ln and str(ln).strip()])


def build_on_demand_analyze_render(symbol: str) -> "RenderedPost":
    delivery_module = _get_delivery_module()


    sym_input = (symbol or "").strip()
    sym_clean = _normalize_symbol(sym_input)
    symbol_display = sym_clean or (sym_input.upper() or "N/A")
    symbol_meta = sym_clean or "N/A"

    action_line = "Action: retry in 60s or run /status"

    now_et = delivery_module._now_et()
    now_iso = now_et.isoformat()
    futures_block = _build_macro_futures_context_section(now_iso, delivery_module)
    spx_block, spx_meta = _build_spx_addon_block(delivery_module)

    if not sym_clean:
        missing = ["Ticker symbol"]
        return _build_analyze_stand_down_render(
            delivery_module,
            symbol_display=symbol_display,
            symbol_meta=symbol_meta,
            missing=missing,
            reason="Data Not Ready",
            action_line=action_line,
            last_price_section=None,
            futures_block=futures_block,
            price_context=None,
            signal_payload=None,
            spx_block=None,
            spx_meta=None,
            issue="Provide a valid alphanumeric ticker.",
        )

    analysis_mode_setting = str(delivery_module.get_analysis_mode()).strip().lower()
    if analysis_mode_setting not in {"strict", "insights"}:
        analysis_mode_setting = "strict"
    verbose = analysis_mode_setting == "insights"
    allow_closed_analysis = os.getenv("ALLOW_CLOSED_ANALYSIS", "0").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }

    try:
        price_snapshot = delivery_module._resolve_last_price(sym_clean, now_et=now_et)
    except Exception:
        price_snapshot = delivery_module._resolve_last_price(sym_clean)

    price_ctx = _resolve_on_demand_price(sym_clean, now_et, allow_closed=allow_closed_analysis)
    candidate_snapshot = price_ctx.get("snapshot")
    if candidate_snapshot is not None and (
        not getattr(price_snapshot, "ok", False) or getattr(candidate_snapshot, "ok", False)
    ):
        price_snapshot = candidate_snapshot

    price_ok = bool(getattr(price_snapshot, "ok", False))
    price_quality: Optional[str] = None
    if price_ctx.get("mode") == "LIVE" and price_ctx.get("accepted"):
        price_quality = "LIVE"
    elif price_ctx.get("mode") == "CLOSED" and price_ctx.get("accepted"):
        price_quality = "CLOSED_OK"
    elif price_ctx.get("mode") == "CLOSED":
        price_quality = "CLOSED_BLOCKED"
    elif price_ctx.get("mode") == "UNAVAILABLE":
        price_quality = "MISSING"

    scenario_flags: Dict[str, Any] = {}
    if price_ctx.get("mode") == "CLOSED" and price_ctx.get("accepted"):
        scenario_flags["market_closed"] = True

    try:
        last_price_section = _build_last_price_section([sym_clean], delivery_module)
    except Exception:
        last_price_section = "💵 **Last Price**\n• (unavailable)"

    if not price_ok:
        issue = price_snapshot.reason or "unknown"
        if price_ctx.get("mode") == "CLOSED" and not price_ctx.get("accepted"):
            issue = "Closed market fallback disabled (set ALLOW_CLOSED_ANALYSIS=1)"
        pivots_block = _build_pivots_section(sym_clean, delivery_module)
        return _build_analyze_stand_down_render(
            delivery_module,
            symbol_display=symbol_display,
            symbol_meta=symbol_meta,
            missing=["Validated price"],
            reason="Price Feed Offline",
            action_line=action_line,
            last_price_section=last_price_section,
            futures_block=futures_block,
            price_context={
                "mode": price_ctx.get("mode"),
                "source": price_ctx.get("source"),
                "age_minutes": price_ctx.get("age_minutes"),
                "accepted": price_ctx.get("accepted"),
                "last_price_ts": price_ctx.get("last_price_ts"),
                "last_price": price_ctx.get("last_price"),
            },
            signal_payload=None,
            spx_block=spx_block,
            spx_meta=spx_meta,
            issue=issue,
        )

    pivots_block = _build_pivots_section(sym_clean, delivery_module)
    regime_block = _build_regime_section(sym_clean, delivery_module)

    signal_block, signal_payload, gate_info = _build_signal_section(
        sym_clean,
        delivery_module,
        verbose=verbose,
        now_et=now_et,
        price_snapshot=price_snapshot,
    )

    # Discipline gate: in strict mode, refuse unsafe regimes.
    if isinstance(signal_payload, dict):
        try:
            ok_tnt, reason_tnt = delivery_module.tnt_regime_gate(
                signal_payload,
                output_mode=analysis_mode_setting,
            )
        except Exception:
            ok_tnt, reason_tnt = True, "tnt gate unavailable"

        if not ok_tnt and analysis_mode_setting == "strict":
            # Provide explicit flip triggers + why-not-trading summary.
            issue_lines: list[str] = [str(reason_tnt or "Discipline gate")]
            try:
                flips = delivery_module.tnt_flip_triggers(signal_payload)
            except Exception:
                flips = None
            if isinstance(flips, dict):
                issue_lines.append("Flip Triggers:")
                issue_lines.append(f"- {flips.get('bull')}")
                issue_lines.append(f"- {flips.get('bear')}")
                issue_lines.append(f"- {flips.get('neutral')}")

            try:
                why_ctx = delivery_module.tnt_why_payload(signal_payload, gate_info)
            except Exception:
                why_ctx = None
            if isinstance(why_ctx, dict):
                conf = why_ctx.get("confirmation") if isinstance(why_ctx.get("confirmation"), dict) else {}
                vg = why_ctx.get("vix_gate") if isinstance(why_ctx.get("vix_gate"), dict) else {}
                issue_lines.append("Why (audit):")
                issue_lines.append(
                    f"- confirmation {conf.get('state','?')} (VIX {conf.get('vix_trend','?')}, SQQQ {conf.get('sqqq_dir','?')})"
                )
                issue_lines.append(f"- pivot distance {why_ctx.get('distance_to_pivot','n/a')}")
                issue_lines.append(f"- edge/conv {why_ctx.get('edge','n/a')} / {why_ctx.get('conviction','n/a')}")
                issue_lines.append(f"- vix gate {vg.get('mode','?')} ({vg.get('reason','n/a')})")
                issue_lines.append(f"- freshness {why_ctx.get('freshness','n/a')}")

            return _build_analyze_stand_down_render(
                delivery_module,
                symbol_display=symbol_display,
                symbol_meta=symbol_meta,
                missing=[],
                reason="Discipline Gate",
                action_line="Action: switch to insights mode, or wait for conditions to improve",
                last_price_section=last_price_section,
                futures_block=futures_block,
                price_context={
                    "mode": price_ctx.get("mode"),
                    "source": price_ctx.get("source"),
                    "age_minutes": price_ctx.get("age_minutes"),
                    "accepted": price_ctx.get("accepted"),
                    "last_price_ts": price_ctx.get("last_price_ts"),
                    "last_price": price_ctx.get("last_price"),
                },
                signal_payload=signal_payload,
                spx_block=spx_block,
                spx_meta=spx_meta,
                issue="\n".join([ln for ln in issue_lines if ln and str(ln).strip()]),
            )

    missing_sections: List[str] = []
    if not signal_payload:
        missing_sections.append("Model Signal")
    if not pivots_block:
        missing_sections.append("Key Levels (API snapshot)")

    if missing_sections:
        # Even when we stand down (missing model signal / levels), include whatever pivots we do have
        # so /ask can still reason with a valid price+levels context.
        stand_down_piv: Dict[str, Any] = {
            "session": "RTH" if 9 <= now_et.hour <= 16 else "UNKNOWN",
            "pivot": None,
            "r1": None,
            "r2": None,
            "s1": None,
            "s2": None,
        }
        try:
            piv_info = delivery_module.get_latest_daily_pivots(sym_clean)
            piv = piv_info.get("piv") if isinstance(piv_info, dict) else None
            if isinstance(piv, dict):
                stand_down_piv["pivot"] = piv.get("P")
                stand_down_piv["r1"] = piv.get("R1")
                stand_down_piv["r2"] = piv.get("R2")
                stand_down_piv["s1"] = piv.get("S1")
                stand_down_piv["s2"] = piv.get("S2")
        except Exception:
            pass

        issue_bits: List[str] = []
        if "Model Signal" in missing_sections:
            issue_bits.append("Model signal not ready")
        if "Key Levels (API snapshot)" in missing_sections:
            issue_bits.append("Key levels unavailable")
        issue_text = " | ".join(issue_bits) or None

        return _build_analyze_stand_down_render(
            delivery_module,
            symbol_display=symbol_display,
            symbol_meta=symbol_meta,
            missing=missing_sections,
            reason="Data Not Ready",
            action_line=action_line,
            last_price_section=last_price_section,
            futures_block=futures_block,
            price_context={
                "mode": price_ctx.get("mode"),
                "source": price_ctx.get("source"),
                "age_minutes": price_ctx.get("age_minutes"),
                "accepted": price_ctx.get("accepted"),
                "last_price_ts": price_ctx.get("last_price_ts"),
                "last_price": price_ctx.get("last_price"),
            },
            signal_payload=stand_down_piv,
            spx_block=spx_block,
            spx_meta=spx_meta,
            issue=issue_text,
        )

    context_mode = str(
        signal_payload.get("trade_context_mode")
        or signal_payload.get("analysis_mode")
        or "db"
    )
    context_source = str(
        signal_payload.get("trade_context_source")
        or signal_payload.get("analysis_source")
        or "model"
    )

    tech_state, pattern_candidates, tech_quality, pattern_quality = _build_technical_package(sym_clean, delivery_module)
    snapshot_block = _build_on_demand_data_snapshot_block(
        symbol=sym_clean,
        price_ctx={
            "mode": price_ctx.get("mode"),
            "source": price_ctx.get("source"),
            "age_minutes": price_ctx.get("age_minutes"),
            "accepted": price_ctx.get("accepted"),
            "last_price_ts": price_ctx.get("last_price_ts"),
            "last_price": price_ctx.get("last_price"),
        },
        signal_payload=signal_payload if isinstance(signal_payload, dict) else None,
        technical_state=tech_state,
    )

    lines: List[str] = [f"🔍 **On-Demand Analyze** — **{symbol_display}**"]

    # Always show Numeric Bias Pack first (after the command header).
    try:
        from delivery.numeric_bias_pack import format_numeric_bias_pack_from_signal_payload

        if isinstance(signal_payload, dict):
            pack = format_numeric_bias_pack_from_signal_payload(
                symbol=symbol_display,
                signal_payload=signal_payload,
                price_context={
                    "mode": price_ctx.get("mode"),
                    "source": price_ctx.get("source"),
                    "age_minutes": price_ctx.get("age_minutes"),
                    "accepted": price_ctx.get("accepted"),
                    "last_price_ts": price_ctx.get("last_price_ts"),
                    "last_price": price_ctx.get("last_price"),
                },
            )
            if pack:
                lines.append(pack)
    except Exception:
        pass

    # In insights mode, show the TNT gate even if it blocks strict execution.
    if analysis_mode_setting == "insights" and isinstance(signal_payload, dict):
        tnt_ctx = signal_payload.get("tnt")
        if isinstance(tnt_ctx, dict):
            tnt_regime = str(tnt_ctx.get("regime") or "UNKNOWN").upper()
            tnt_posture = str(tnt_ctx.get("posture") or "UNKNOWN").upper()
            reasons = tnt_ctx.get("reasons")
            why = "n/a"
            if isinstance(reasons, list) and reasons:
                why = ", ".join(str(x) for x in reasons[:4])
            lines.append(f"🛡️ **TNT Gate**: {tnt_regime} | posture {tnt_posture} | why: {why}")
    if snapshot_block:
        lines.append(snapshot_block)
    if last_price_section:
        lines.append(last_price_section)

    # Tiny summaries (strictly 2 lines max) for Pressure + HTF.
    # These must be best-effort and synchronous (no network/await here).
    try:
        def _pressure_line() -> str:
            getter = getattr(delivery_module, "_get_cached_options_micro", None)
            if getter is None:
                return "🧭 Pressure: n/a"
            entry = getter(sym_clean)
            packet = getattr(entry, "packet", None) if entry is not None else None
            if not isinstance(packet, dict) or packet.get("status") != "ok":
                return "🧭 Pressure: n/a"
            metrics = packet.get("metrics") if isinstance(packet.get("metrics"), dict) else {}
            oi_ratio = metrics.get("call_put_oi_ratio")
            vol_ratio = metrics.get("call_put_vol_ratio")
            call_wall = metrics.get("call_wall") if isinstance(metrics.get("call_wall"), dict) else {}
            put_wall = metrics.get("put_wall") if isinstance(metrics.get("put_wall"), dict) else {}

            def _fmt_ratio(val: object) -> str:
                try:
                    if isinstance(val, (int, float)):
                        return f"{float(val):.2f}"
                except Exception:
                    pass
                return "n/a"

            def _fmt_strike(wall: dict) -> str:
                try:
                    s = wall.get("strike")
                    if isinstance(s, (int, float)):
                        return f"{float(s):.0f}"
                except Exception:
                    pass
                return "n/a"

            return (
                "🧭 Pressure: "
                f"OI C/P **{_fmt_ratio(oi_ratio)}** | "
                f"VOL C/P **{_fmt_ratio(vol_ratio)}** | "
                f"Walls C **{_fmt_strike(call_wall)}** / P **{_fmt_strike(put_wall)}**"
            )

        def _htf_line() -> str:
            import math
            from datetime import date

            bars = []
            try:
                bars = list(getattr(delivery_module, "get_last_n_bars")(sym_clean, tf="1d", n=280, with_volume=True) or [])
            except Exception:
                bars = []
            if len(bars) < 30:
                return "🧠 HTF: n/a"

            closes: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            vols: list[float] = []
            dates: list[date] = []
            for row in bars:
                if not row or len(row) < 5:
                    continue
                ts = str(row[0] or "")
                try:
                    d = date.fromisoformat(ts[:10])
                except Exception:
                    continue
                try:
                    highs.append(float(row[2]))
                    lows.append(float(row[3]))
                    closes.append(float(row[4]))
                    v = float(row[5]) if len(row) >= 6 and row[5] is not None else 0.0
                    vols.append(max(v, 0.0))
                    dates.append(d)
                except Exception:
                    continue

            if len(closes) < 30:
                return "🧠 HTF: n/a"

            # RVOL (last vs prior 20D avg)
            rvol = None
            try:
                if len(vols) >= 22:
                    base = vols[-21:-1]
                    avg = sum(base) / float(len(base)) if base else 0.0
                    if avg > 0:
                        rvol = float(vols[-1]) / avg
            except Exception:
                rvol = None

            # ATR(14) Wilder + ATR%.
            atr = None
            try:
                trs: list[float] = []
                for i in range(1, len(closes)):
                    tr = max(
                        highs[i] - lows[i],
                        abs(highs[i] - closes[i - 1]),
                        abs(lows[i] - closes[i - 1]),
                    )
                    trs.append(float(tr))
                period = 14
                if len(trs) >= period:
                    first = sum(trs[:period]) / float(period)
                    prev = first
                    for tr in trs[period:]:
                        prev = (prev * (period - 1) + float(tr)) / float(period)
                    atr = prev
            except Exception:
                atr = None

            atr_pct = None
            try:
                if atr is not None and closes[-1] > 0:
                    atr_pct = float(atr) / float(closes[-1])
            except Exception:
                atr_pct = None

            # Stoch(14,3) cross on last bar.
            k1 = None
            d1 = None
            k0 = None
            d0 = None
            try:
                lookback = 14
                if len(closes) >= lookback + 3:
                    ks: list[float] = []
                    for i in range(lookback - 1, len(closes)):
                        lo = min(lows[i - lookback + 1 : i + 1])
                        hi = max(highs[i - lookback + 1 : i + 1])
                        denom = (hi - lo)
                        k = 50.0
                        if denom > 0:
                            k = 100.0 * (closes[i] - lo) / denom
                        ks.append(float(k))
                    if len(ks) >= 3:
                        d_series = []
                        for i in range(len(ks)):
                            if i < 2:
                                d_series.append(math.nan)
                            else:
                                d_series.append((ks[i] + ks[i - 1] + ks[i - 2]) / 3.0)
                        k1 = ks[-1]
                        k0 = ks[-2]
                        d1 = d_series[-1]
                        d0 = d_series[-2]
            except Exception:
                pass

            bullish_cross = False
            bearish_cross = False
            try:
                if all(isinstance(x, (int, float)) and math.isfinite(float(x)) for x in (k0, d0, k1, d1)):
                    bullish_cross = bool(float(k0) <= float(d0) and float(k1) > float(d1))
                    bearish_cross = bool(float(k0) >= float(d0) and float(k1) < float(d1))
            except Exception:
                bullish_cross = False
                bearish_cross = False

            weekly_bias = "NEUTRAL"
            try:
                # Weekly closes via ISO week buckets, then 10W SMA.
                weekly: dict[tuple[int, int], float] = {}
                for d, c in zip(dates, closes):
                    iso = d.isocalendar()
                    weekly[(int(iso.year), int(iso.week))] = float(c)
                wk_keys = sorted(weekly.keys())
                wk_closes = [weekly[k] for k in wk_keys]
                if len(wk_closes) >= 10:
                    sma10 = sum(wk_closes[-10:]) / 10.0
                    if wk_closes[-1] > sma10:
                        weekly_bias = "BULL"
                    elif wk_closes[-1] < sma10:
                        weekly_bias = "BEAR"
            except Exception:
                weekly_bias = "NEUTRAL"

            state = "OFF"
            direction = ""
            try:
                rv = float(rvol) if isinstance(rvol, (int, float)) else None
                kk = float(k1) if isinstance(k1, (int, float)) else None
                if rv is not None and kk is not None:
                    if rv >= 1.50 and ((bullish_cross and kk <= 25.0) or (bearish_cross and kk >= 75.0)):
                        state = "ON"
                        direction = "bull" if bullish_cross else "bear" if bearish_cross else ""
                    elif rv >= 1.10 and (kk <= 30.0 or kk >= 70.0):
                        state = "WATCH"
            except Exception:
                pass

            rv_txt = f"{float(rvol):.2f}" if isinstance(rvol, (int, float)) else "n/a"
            atr_txt = f"{float(atr_pct)*100.0:.2f}%" if isinstance(atr_pct, (int, float)) else "n/a"
            st = state if state != "ON" or not direction else f"{state} ({direction})"
            return f"🧠 HTF: **{st}** | Weekly **{weekly_bias}** | RVOL **{rv_txt}** | ATR% **{atr_txt}**"

        lines.append("\n".join([_pressure_line(), _htf_line()]))
    except Exception:
        pass
    if pivots_block:
        lines.append(pivots_block)
    if signal_block:
        lines.append(signal_block)
    if regime_block:
        lines.append(regime_block)
    if futures_block:
        lines.append(futures_block)
    if spx_block:
        lines.append(spx_block)

    text = "\n\n".join([chunk for chunk in lines if chunk and chunk.strip()])

    extras: Dict[str, Any] = {
        "gate": gate_info,
        "signal_payload": {
            "bias": signal_payload.get("bias") if isinstance(signal_payload, dict) else None,
            "bias_confirm": signal_payload.get("bias_confirm") if isinstance(signal_payload, dict) else None,
            "conviction": signal_payload.get("conviction") if isinstance(signal_payload, dict) else None,
            "regime": signal_payload.get("regime") if isinstance(signal_payload, dict) else None,
            "pivot_regime": signal_payload.get("pivot_regime") if isinstance(signal_payload, dict) else None,
            "tf_exec": signal_payload.get("tf_exec") if isinstance(signal_payload, dict) else None,
            "tf_struct": signal_payload.get("tf_struct") if isinstance(signal_payload, dict) else None,
            "tf_ctx": signal_payload.get("tf_ctx") if isinstance(signal_payload, dict) else None,
            "pivot": signal_payload.get("pivot") if isinstance(signal_payload, dict) else None,
            "r1": signal_payload.get("r1") if isinstance(signal_payload, dict) else None,
            "r2": signal_payload.get("r2") if isinstance(signal_payload, dict) else None,
            "s1": signal_payload.get("s1") if isinstance(signal_payload, dict) else None,
            "s2": signal_payload.get("s2") if isinstance(signal_payload, dict) else None,
            "ts": signal_payload.get("ts") if isinstance(signal_payload, dict) else None,
            "session": signal_payload.get("session") if isinstance(signal_payload, dict) else None,
        },
        "last_price_available": bool(last_price_section),
        "futures_section": bool(futures_block),
        "spx_addon": {
            "enabled": bool(spx_block),
            "source": "iqfeed_sqlite" if spx_block else None,
            "metrics": spx_meta,
        },
    }
    extras["technical_state_quality"] = tech_quality
    extras["pattern_candidates_quality"] = pattern_quality
    extras["price_context"] = {
        "mode": price_ctx.get("mode"),
        "source": price_ctx.get("source"),
        "age_minutes": price_ctx.get("age_minutes"),
        "accepted": price_ctx.get("accepted"),
        "last_price_ts": price_ctx.get("last_price_ts"),
        "last_price": price_ctx.get("last_price"),
    }

    return _compose_analyze_render(
        delivery_module,
        text=text,
        symbol=symbol_meta,
        stand_down=False,
        missing=[],
        context_mode=context_mode,
        output_mode=analysis_mode_setting,
        context_source=context_source,
        extras=extras,
        technical_state=tech_state,
        pattern_candidates=pattern_candidates,
        technical_quality=tech_quality,
        pattern_quality=pattern_quality,
        price_quality=price_quality,
        scenario_flags=scenario_flags,
    )
