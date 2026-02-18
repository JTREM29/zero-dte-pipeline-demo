"""SPX snapshot helper sourced from local IQFeed SQLite.

The loader is intentionally isolated so the on-demand flow can opt-in without
risking regressions for core SPY/QQQ surfaces. All sqlite access is guarded by
bounds checks and simple sanity filters to avoid relaying bogus prints.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Iterable, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 fallback
    ZoneInfo = None  # type: ignore[assignment]

_ET = ZoneInfo("America/New_York") if ZoneInfo else None

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "iqfeed_service" / "market_iqfeed.db"

_SPX_ALIAS = os.getenv("IQFEED_SPX_ALIAS", "SPX")
_SPX_FEED_SYMBOL = os.getenv("IQFEED_SPX_SYMBOL", "SPX.XO").upper() or "SPX.XO"
_SPX_RTH_STALE_SEC = float(os.getenv("IQFEED_SPX_RTH_STALE_SEC", "300"))
_SPX_EXTENDED_STALE_SEC = float(os.getenv("IQFEED_SPX_EXTENDED_STALE_SEC", "1800"))
_SPX_MAX_LOOKBACK_SEC = float(os.getenv("IQFEED_SPX_MAX_LOOKBACK_SEC", "604800"))
_SPX_MIN_ROWS = int(os.getenv("IQFEED_SPX_SAMPLE_ROWS", "6"))
_SPX_MAX_JUMP = float(os.getenv("IQFEED_SPX_MAX_JUMP", "150"))


@dataclass(slots=True)
class SpxQuoteSnapshot:
    last: Optional[float]
    ts_utc: Optional[datetime]
    bid: Optional[float]
    ask: Optional[float]
    sample_count: int
    trend: str
    tape_bias: str
    vwap: Optional[float]
    rsi: Optional[float]
    support: Optional[float]
    resistance: Optional[float]
    status: str
    freshness_sec: Optional[float]
    message: Optional[str]

    @property
    def ts_et(self) -> datetime:
        if _ET:
            return self.ts_utc.astimezone(_ET)
        return self.ts_utc.astimezone(timezone.utc)


def _resolve_db_path() -> Path:
    override = os.getenv("IQFEED_SQLITE_PATH")
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists():
            return candidate
    return _DEFAULT_DB_PATH


def _safe_float(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _trend_from_series(series: Iterable[float]) -> str:
    seq = [x for x in series if x is not None]
    if len(seq) < 2:
        return "flat"
    newest = seq[-1]
    earliest = seq[0]
    delta = newest - earliest
    if abs(delta) < 0.5:
        return "flat"
    return "up" if delta > 0 else "down"


def _tape_bias(last_px: Optional[float], mids: Iterable[float]) -> str:
    seq = [x for x in mids if x is not None]
    if not seq:
        return "n/a"
    if last_px is None:
        return "n/a"
    avg_mid = fmean(seq)
    gap = last_px - avg_mid
    if abs(gap) < 0.5:
        return "balanced"
    return "firm" if gap > 0 else "soft"


def _vwap_from_prices(prices: Sequence[float]) -> Optional[float]:
    if len(prices) < 2:
        return None
    try:
        return fmean(prices)
    except Exception:  # noqa: BLE001
        return None


def _rsi_from_prices(prices: Sequence[float]) -> Optional[float]:
    if len(prices) < 3:
        return None
    period = min(14, len(prices) - 1)
    if period <= 0:
        return None
    window = prices[-(period + 1) :]
    gains = 0.0
    losses = 0.0
    for prev, curr in zip(window, window[1:]):
        delta = curr - prev
        if delta > 0:
            gains += delta
        else:
            losses += abs(delta)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return max(0.0, min(100.0, rsi))


def _support_resistance(prices: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    if len(prices) < 3:
        return None, None
    history = prices[:-1]
    if not history:
        return None, None
    try:
        return min(history), max(history)
    except Exception:  # noqa: BLE001
        return None, None


def load_spx_snapshot(
    now_utc: Optional[datetime] = None,
    *,
    session: Optional[str] = None,
) -> Optional[SpxQuoteSnapshot]:
    db_path = _resolve_db_path()
    if not db_path.exists():
        return None

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts, symbol, last, bid, ask
            FROM iq_quote
            WHERE symbol IN (?, ?)
            ORDER BY ts DESC
            LIMIT ?
            """,
            (_SPX_ALIAS.upper(), _SPX_FEED_SYMBOL.upper(), max(_SPX_MIN_ROWS, 3)),
        )
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()

    if not rows:
        return None

    last_row = rows[0]
    ts_raw = last_row["ts"]
    last_px = _safe_float(last_row["last"])
    bid_px = _safe_float(last_row["bid"])
    ask_px = _safe_float(last_row["ask"])

    ts_utc: Optional[datetime]
    try:
        ts_utc = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        else:
            ts_utc = ts_utc.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        ts_utc = None

    now = now_utc or datetime.now(timezone.utc)
    age_sec: Optional[float] = None
    if ts_utc is not None:
        age_sec = (now - ts_utc).total_seconds()
        if age_sec < 0:
            age_sec = 0

    if age_sec is not None and age_sec > _SPX_MAX_LOOKBACK_SEC:
        return None

    prices_series = [_safe_float(r["last"]) for r in rows]
    valid_prices_desc = [p for p in prices_series if p is not None]
    mids = []
    for r in rows:
        bid = _safe_float(r["bid"])
        ask = _safe_float(r["ask"])
        if bid is not None and ask is not None:
            mids.append((bid + ask) / 2.0)
    if not mids and bid_px is not None and ask_px is not None:
        mids.append((bid_px + ask_px) / 2.0)

    if valid_prices_desc:
        newest = valid_prices_desc[0]
        oldest = valid_prices_desc[-1]
        if abs(newest - oldest) > _SPX_MAX_JUMP:
            return None

    trend = _trend_from_series(reversed(prices_series))  # oldest first for trend
    tape = _tape_bias(last_px, mids)

    prices_chrono = list(reversed(valid_prices_desc))
    vwap = _vwap_from_prices(valid_prices_desc)
    rsi = _rsi_from_prices(prices_chrono)
    support, resistance = _support_resistance(prices_chrono)

    session_norm = (session or "").strip().upper() or "UNKNOWN"
    limit: Optional[float]
    if session_norm == "RTH":
        limit = _SPX_RTH_STALE_SEC
    elif session_norm in {"PRE", "AH"}:
        limit = _SPX_EXTENDED_STALE_SEC
    elif session_norm in {"WEEKEND", "CLOSED"}:
        limit = None
    else:
        limit = _SPX_EXTENDED_STALE_SEC

    status = "FRESH"
    message: Optional[str] = None
    if limit is None:
        status = "CLOSED"
    elif age_sec is not None and limit is not None and age_sec > limit:
        status = "STALE"

    if status == "CLOSED":
        if last_px is not None:
            message = "Market closed — showing last settled close + prior pivots"
        else:
            message = "Market closed — SPX live tape unavailable"
    elif status == "STALE" and age_sec is not None:
        mins = max(age_sec / 60.0, 0.1)
        message = f"Data stale ({mins:.0f}m old)"

    if last_px is None and status != "CLOSED":
        message = message or "SPX quote unavailable"

    return SpxQuoteSnapshot(
        last=last_px,
        ts_utc=ts_utc,
        bid=bid_px,
        ask=ask_px,
        sample_count=len(rows),
        trend=trend,
        tape_bias=tape,
        vwap=vwap,
        rsi=rsi,
        support=support,
        resistance=resistance,
        status=status,
        freshness_sec=age_sec,
        message=message,
    )
```},