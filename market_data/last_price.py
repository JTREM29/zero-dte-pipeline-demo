from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Callable, Optional, Literal, Tuple

import requests

PriceSource = Literal[
    "polygon_snapshot_lastTrade",
    "db_1m_close",
    "db_1d_close",
    "cached_analyze",
    "none",
]

MarketState = Literal[
    "OPEN",
    "CLOSED_WEEKEND",
    "CLOSED_PRE",
    "CLOSED_AFTER",
    "CLOSED_HOLIDAY",
    "UNKNOWN",
]


@dataclass(frozen=True)
class LastPrice:
    symbol: str
    price: Optional[float]
    asof_et: Optional[datetime]
    source: PriceSource
    market_state: MarketState
    age_minutes: Optional[float]
    ok: bool
    reason: Optional[str] = None

    @property
    def px(self) -> Optional[float]:
        return self.price

    @property
    def stale(self) -> bool:
        return not self.ok


FetchFn = Callable[[str], Tuple[Optional[float], Optional[datetime]]]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_age_minutes(now_utc: datetime, asof: Optional[datetime]) -> Optional[float]:
    if not asof:
        return None
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    return (now_utc - asof.astimezone(timezone.utc)).total_seconds() / 60.0


def detect_market_state(now_et: datetime) -> MarketState:
    # Best-effort holiday detection via Polygon market status.
    # This avoids mislabeling holidays as OPEN based purely on weekday/time.
    key = os.getenv("POLYGON_API_KEY", "").strip()
    base_url = (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")
    if key:
        try:
            cache = getattr(detect_market_state, "_cache", None)
            now_ts = _now_utc().timestamp()
            if cache and now_ts <= cache[0]:
                return cache[1]

            resp = requests.get(
                f"{base_url}/v1/marketstatus/now",
                params={"apiKey": key},
                timeout=2,
            )
            if resp.status_code == 200:
                payload = resp.json() if resp.content else {}
                market = str(payload.get("market") or "").lower()
                if market == "open":
                    state: MarketState = "OPEN"
                else:
                    if now_et.weekday() >= 5:
                        state = "CLOSED_WEEKEND"
                    else:
                        hhmm = now_et.hour * 60 + now_et.minute
                        rth_open = 9 * 60 + 30
                        rth_close = 16 * 60
                        if hhmm < rth_open:
                            state = "CLOSED_PRE"
                        elif hhmm > rth_close:
                            state = "CLOSED_AFTER"
                        else:
                            state = "CLOSED_HOLIDAY"

                # Cache for 60 seconds.
                setattr(detect_market_state, "_cache", (now_ts + 60.0, state))
                return state
        except Exception:
            pass

    if now_et.weekday() >= 5:
        return "CLOSED_WEEKEND"
    hhmm = now_et.hour * 60 + now_et.minute
    rth_open = 9 * 60 + 30
    rth_close = 16 * 60
    if rth_open <= hhmm <= rth_close:
        return "OPEN"
    if hhmm < rth_open:
        return "CLOSED_PRE"
    if hhmm > rth_close:
        return "CLOSED_AFTER"
    return "UNKNOWN"


def resolve_last_price(
    symbol: str,
    *,
    now_et: datetime,
    fetch_polygon_snapshot_last_trade: FetchFn,
    fetch_db_latest_1m_close: FetchFn,
    fetch_db_latest_1d_close: FetchFn,
    fetch_cached_analyze_price: Optional[FetchFn] = None,
    allow_stale_when_closed: bool = True,
    open_max_age_min: int = 10,
    closed_max_age_days: int = 7,
) -> LastPrice:
    sym = symbol.strip().upper()
    state = detect_market_state(now_et)
    now_utc = _now_utc()

    def validate(price: Optional[float], asof_et: Optional[datetime], source: PriceSource) -> LastPrice:
        age_min = _to_age_minutes(now_utc, asof_et)
        if price is None or asof_et is None:
            return LastPrice(sym, price, asof_et, source, state, age_min, False, "missing_last_price")

        if state == "OPEN":
            if age_min is None or age_min > open_max_age_min:
                return LastPrice(
                    sym,
                    price,
                    asof_et,
                    source,
                    state,
                    age_min,
                    False,
                    f"stale_price_open>{open_max_age_min}m",
                )
            return LastPrice(sym, price, asof_et, source, state, age_min, True)

        if not allow_stale_when_closed:
            if age_min is None or age_min > open_max_age_min:
                return LastPrice(sym, price, asof_et, source, state, age_min, False, "closed_disallows_stale")
            return LastPrice(sym, price, asof_et, source, state, age_min, True)

        max_age_min = closed_max_age_days * 24 * 60
        if age_min is None or age_min > max_age_min:
            return LastPrice(
                sym,
                price,
                asof_et,
                source,
                state,
                age_min,
                False,
                f"stale_price_closed>{closed_max_age_days}d",
            )
        return LastPrice(sym, price, asof_et, source, state, age_min, True)

    p, ts = fetch_polygon_snapshot_last_trade(sym)
    lp = validate(p, ts, "polygon_snapshot_lastTrade")
    if lp.ok:
        return lp

    p, ts = fetch_db_latest_1m_close(sym)
    lp2 = validate(p, ts, "db_1m_close")
    if lp2.ok:
        return lp2

    p, ts = fetch_db_latest_1d_close(sym)
    lp3 = validate(p, ts, "db_1d_close")
    if lp3.ok:
        return lp3

    if fetch_cached_analyze_price:
        p, ts = fetch_cached_analyze_price(sym)
        lp4 = validate(p, ts, "cached_analyze")
        if lp4.ok:
            return lp4

    return LastPrice(sym, None, None, "none", state, None, False, "no_price_sources")


def market_state_label(state: MarketState) -> str:
    return {
        "OPEN": "OPEN",
        "CLOSED_WEEKEND": "CLOSED — weekend",
        "CLOSED_PRE": "CLOSED — premarket",
        "CLOSED_AFTER": "CLOSED — after-hours",
        "CLOSED_HOLIDAY": "CLOSED — holiday",
        "UNKNOWN": "CLOSED",
    }.get(state, "CLOSED")


def format_last_price_block(lp: LastPrice) -> str:
    if not lp.price or not lp.asof_et:
        return "💵 **Last Price**\n• n/a"

    asof_str = lp.asof_et.strftime("%Y-%m-%d %H:%M ET")
    src = {
        "polygon_snapshot_lastTrade": "live snapshot",
        "db_1m_close": "last 1m close",
        "db_1d_close": "last daily close",
        "cached_analyze": "cached",
        "none": "n/a",
    }.get(lp.source, lp.source)

    state = market_state_label(lp.market_state)
    return "💵 **Last Price**\n" f"• {lp.symbol}: {lp.price:.2f} ({src} | {asof_str} | {state})"
