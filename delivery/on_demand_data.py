import os
import time
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple

import requests


POLYGON_BASE_URL = (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")


_CACHE: Dict[Tuple[str, str], Tuple[float, Any]] = {}

def cache_get(sym: str, key: str):
    now = time.time()
    cached = _CACHE.get((sym, key))
    if not cached:
        return None
    expires_at, value = cached
    if now > expires_at:
        _CACHE.pop((sym, key), None)
        return None
    return value

def cache_set(sym: str, key: str, val: Any, ttl_sec: int) -> None:
    _CACHE[(sym, key)] = (time.time() + ttl_sec, val)


def _db_path() -> str:
    return os.getenv("DB_PATH", "db/tnt.db")

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
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0

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
    resp = requests.get(url, params={"apiKey": _polygon_key()}, timeout=15)
    resp.raise_for_status()
    return resp.json()

def polygon_aggs_1m(symbol: str, minutes: int = 180) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)
    end_date = now.strftime("%Y-%m-%d")
    start_date = start.strftime("%Y-%m-%d")
    url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{symbol}/range/1/minute/{start_date}/{end_date}"
    resp = requests.get(
        url,
        params={
            "apiKey": _polygon_key(),
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_last_agg_close(symbol: str, api_key: str):
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(minutes=15)).date().isoformat()
    to = now.date().isoformat()

    url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{symbol}/range/1/minute/{frm}/{to}"
    try:
        resp = requests.get(
            url,
            params={
                "adjusted": "true",
                "sort": "desc",
                "limit": 50,
                "apiKey": api_key,
            },
            timeout=10,
        )
    except Exception:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
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

def _ts_to_iso(raw: Optional[float]) -> Optional[str]:
    if raw is None:
        return None
    try:
        value = float(raw)
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
        return float(lt["p"]), "lastTrade"

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
            return (float(bp) + float(ap)) / 2.0, "lastQuote(mid)"
        if bp is not None:
            return float(bp), "lastQuote(bid)"
        if ap is not None:
            return float(ap), "lastQuote(ask)"

    mn = t.get("min") or {}
    if isinstance(mn, dict) and mn.get("c") is not None:
        return float(mn["c"]), "min.c"

    pd = t.get("prevDay") or {}
    if isinstance(pd, dict) and pd.get("c") is not None:
        return float(pd["c"]), "prevDay.c"

    return None, None


def _snapshot_timestamp(snap: Dict[str, Any], source: Optional[str]) -> Optional[str]:
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

    return _ts_to_iso(raw)


def fetch_live_price(symbol: str) -> Tuple[Optional[float], Optional[str], str]:
    cached = cache_get(symbol, "snapshot_price")
    if cached:
        return cached

    try:
        snap = polygon_snapshot(symbol)
    except Exception:
        snap = None

    price = None
    src_key: Optional[str] = None
    ts_iso: Optional[str] = None
    source_label = "polygon-snapshot"

    if snap:
        snap_price, snap_key = _extract_snapshot_price(snap)
        if snap_price is not None:
            price = float(snap_price)
            src_key = snap_key
            ts_iso = _snapshot_timestamp(snap, snap_key)
            source_label = f"polygon-snapshot:{snap_key or 'unknown'}"

    if price is None:
        # Fallback to recent 1m aggregation close when snapshot lacks a usable price.
        try:
            agg = fetch_last_agg_close(symbol, _polygon_key())
        except RuntimeError:
            return None, None, "polygon-api-key"

        if not agg:
            return None, None, "polygon-snapshot"

        price, agg_src, t_raw = agg
        ts_iso = _ts_to_iso(t_raw)
        source_label = f"polygon-{agg_src}"

    payload = (float(price), ts_iso, source_label)
    cache_set(symbol, "snapshot_price", payload, ttl_sec=5)
    cache_set(symbol, "sym_supported", True, ttl_sec=600)
    return payload

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
