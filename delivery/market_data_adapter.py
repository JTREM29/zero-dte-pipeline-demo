"""Market Data Adapter

Normalized, vendor-agnostic access layer for building TNT_STATE.

Design goals:
- Keep vendor APIs behind a stable interface.
- Return small, contract-safe, JSON-serializable dictionaries.
- Never return option chain/contract identifiers (strikes/expiries/symbols).

This adapter is intentionally deterministic and defensive; callers should
expect partial coverage and check returned coverage flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from zoneinfo import ZoneInfo

from delivery.on_demand_data import db_last_tick, fetch_live_price

ET = ZoneInfo("America/New_York")


def _now_et(now: Optional[datetime] = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(ET)


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        raw = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None


def _age_seconds(now_et: datetime, asof_iso: Optional[str]) -> Optional[float]:
    dt = _parse_iso(asof_iso or "")
    if not dt:
        return None
    now_utc = now_et.astimezone(timezone.utc)
    return max((now_utc - dt.astimezone(timezone.utc)).total_seconds(), 0.0)


def _coerce_float(val: object) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:  # noqa: BLE001
        return None


def _coerce_str(val: object) -> Optional[str]:
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _norm_bucket(val: Optional[str], *, allow: Iterable[str], default: str = "UNKNOWN") -> str:
    raw = (_coerce_str(val) or "").upper().replace(" ", "_")
    allowed = {x.upper() for x in allow}
    if raw in allowed:
        return raw

    # Conservative fallback: unknown -> UNKNOWN.
    return default


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    last: Optional[float]
    ts_iso: Optional[str]
    source: str
    staleness_s: Optional[float]


def get_equity_snapshot(symbol: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return a normalized equity snapshot.

    Source priority is handled by `fetch_live_price` (Polygon snapshot first, then
    1m aggregates fallback).
    """

    sym = (symbol or "").strip().upper() or "SPY"
    now_et = _now_et(now)

    try:
        price, ts_iso, src = fetch_live_price(sym)
    except Exception:  # noqa: BLE001
        price, ts_iso, src = None, None, "error"

    last = _coerce_float(price)
    staleness_s = _age_seconds(now_et, ts_iso)

    return {
        "symbol": sym,
        "last": last,
        "ts_iso": ts_iso,
        "staleness_s": staleness_s,
        "source": {"provider": "polygon", "detail": _coerce_str(src) or "unknown"},
    }


def get_index_snapshot(symbol: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return a normalized index snapshot.

    Indices may be sourced from the same snapshot pipeline as equities.
    """

    sym = (symbol or "").strip().upper()
    now_et = _now_et(now)

    # Prefer the dedicated Polygon indices endpoint for known index tickers.
    # Fallback to the equity snapshot pipeline if unavailable.
    index_ticker = None
    if sym in {"VIX", "I:VIX"}:
        index_ticker = "I:VIX"
    elif sym in {"SPX", "I:SPX"}:
        index_ticker = "I:SPX"

    if index_ticker:
        # If a WS collector is running (market=indices) prefer its low-latency tick cache.
        try:
            ws_price, ws_ts, ws_src = db_last_tick(index_ticker, max_age_sec=5.0)
        except Exception:  # noqa: BLE001
            ws_price, ws_ts, ws_src = None, None, None

        if ws_price is not None and ws_ts:
            staleness_s = _age_seconds(now_et, ws_ts)
            return {
                "symbol": sym,
                "last": _coerce_float(ws_price),
                "prev_close": None,
                "ts_iso": ws_ts,
                "staleness_s": staleness_s,
                "source": {"provider": "polygon", "detail": f"ws-cache:{_coerce_str(ws_src) or 'last_ticks'}"},
            }

        # REST fallback: rely on the shared `fetch_live_price` pipeline for indices
        # (which supports index tickers via aggregates when snapshots aren't available).
        try:
            price, ts_iso, src = fetch_live_price(index_ticker)
        except Exception:  # noqa: BLE001
            price, ts_iso, src = None, None, "error"

        staleness_s = _age_seconds(now_et, ts_iso)
        return {
            "symbol": sym,
            "last": _coerce_float(price),
            "prev_close": None,
            "ts_iso": ts_iso,
            "staleness_s": staleness_s,
            "source": {"provider": "polygon", "detail": _coerce_str(src) or "unknown"},
        }

    # Conservative fallback.
    return get_equity_snapshot(sym, now=now)


def get_pivots(symbol: str) -> Optional[Dict[str, Any]]:
    """Return latest RTH pivots for symbol (DB-backed)."""

    try:
        from delivery.discord_bot_head import get_latest_daily_pivots  # local import to keep adapter lightweight

        piv = get_latest_daily_pivots((symbol or "").strip().upper())
        if not isinstance(piv, dict):
            return None
        if not isinstance(piv.get("piv"), dict):
            return None
        return piv
    except Exception:  # noqa: BLE001
        return None


def get_ohlc(
    symbol: str,
    *,
    tf: str = "1m",
    limit: int = 120,
) -> Dict[str, Any]:
    """Return normalized OHLC bars.

    Primary source is the local SQLite `prices` table (via discord_bot_head).
    This is intended for internal computations; TNT_STATE should usually not
    include full bar payloads.
    """

    sym = (symbol or "").strip().upper() or "SPY"
    tf_clean = (tf or "1m").strip()
    n = int(limit) if isinstance(limit, int) and limit > 0 else 120

    rows: List[Tuple[Any, Any, Any, Any, Any]] = []
    try:
        from delivery.discord_bot_head import get_last_n_bars  # local import

        rows = get_last_n_bars(sym, tf=tf_clean, n=n)
    except Exception:  # noqa: BLE001
        rows = []

    bars: List[Dict[str, Any]] = []
    for ts, o, h, l, c in rows:
        bars.append(
            {
                "ts": str(ts),
                "o": _coerce_float(o),
                "h": _coerce_float(h),
                "l": _coerce_float(l),
                "c": _coerce_float(c),
                "v": None,
            }
        )

    return {
        "symbol": sym,
        "tf": tf_clean,
        "bars": bars,
        "source": {"provider": "sqlite", "detail": "prices"},
    }


_OPTIONS_BUCKETS = ("LOW", "NORMAL", "ELEVATED", "HIGH", "EXTREME", "UNKNOWN")


def get_options_environment(
    *,
    options_focus_lines: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return contract-safe options environment buckets.

    This adapter intentionally does NOT fetch option chains.

    Inputs can be a small list of bullet lines such as:
    - "• Gamma: NORMAL"
    - "• Theta: HIGH"
    - "• Vol Regime: ELEVATED"

    Output is strictly bucketed and does not contain strikes/expiries/option tickers.
    """

    now_et = _now_et(now)

    lines = [str(x) for x in (options_focus_lines or []) if str(x or "").strip()]

    def _extract_value(prefixes: Tuple[str, ...]) -> Optional[str]:
        for raw in lines:
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

    gamma = _norm_bucket(
        _extract_value(("gamma", "gamma:")),
        allow=_OPTIONS_BUCKETS,
        default="UNKNOWN",
    )
    theta = _norm_bucket(
        _extract_value(("theta", "theta:")),
        allow=_OPTIONS_BUCKETS,
        default="UNKNOWN",
    )
    vol_regime = _norm_bucket(
        _extract_value(("vol regime", "vol_regime", "vol", "vol:")),
        allow=_OPTIONS_BUCKETS,
        default="UNKNOWN",
    )

    return {
        "asof_et": now_et.isoformat(),
        "gamma": gamma,
        "theta": theta,
        "vol_regime": vol_regime,
        "source": {"provider": "adapter", "detail": "bucketed"},
    }


def get_event_risk(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Optional: return macro/event risk in a normalized form.

    This keeps the adapter contract stable; event ingestion can be wired later.
    """

    now_et = _now_et(now)
    return {
        "asof_et": now_et.isoformat(),
        "events": [],
        "source": {"provider": "adapter", "detail": "none"},
    }
