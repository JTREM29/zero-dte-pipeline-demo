"""Bar aggregation utilities for Level1 ticks.

Features:
- Time-bucket aggregation (e.g., 1s or 5s bars) with exchange/arrival timestamp.
- Watermark-based finalization to tolerate late/out-of-order ticks.
- Gap handling with optional continuous empty-bar emission.
- Simple outlier handling (winsorize or clip) on tick-to-tick returns.
- Look-ahead-safe emission: only finalized bars are emitted by default.

Defaults preserve previous behavior (no watermark delay), so existing tests and
callers continue to work. Use ``finalize_until(now_ts)`` for explicit control
when backfilling or processing historical tick streams.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional, Callable
import time
from math import isnan
from collections import deque, defaultdict
import numpy as np


@dataclass
class Bar:
    symbol: str
    start_ts: float
    end_ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int


class TimeBarAggregator:
    def __init__(
        self,
        interval_sec: float = 1.0,
        *,
        watermark_delay: float = 0.0,
    max_lag_sec: Optional[float] = None,
    enforce_continuous: bool = False,
        outlier_mode: str = "winsorize",  # one of {"none","winsorize","clip"}
        outlier_window: int = 50,
        outlier_threshold: float = 3.0,
        ts_field: str = "epoch",
        fallback_to_arrival: bool = True,
        price_source_order: Optional[List[str]] = None,
        on_bar_finalized: Optional[Callable[[Bar], None]] = None,
        on_bar_updated: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """Create a time-bar aggregator.

        Args:
            interval_sec: Bar interval in seconds.
            watermark_delay: Delay before finalizing a bucket to allow late ticks. Default 0.0 (immediate on rollover).
            max_lag_sec: Max age (seconds) for accepting out-of-order ticks into already-finalized buckets. Defaults to 2×interval.
            enforce_continuous: Emit empty bars for gaps.
            outlier_mode: Tick outlier handling: none | winsorize | clip.
            outlier_window: Window size for return statistics.
            outlier_threshold: Z-like threshold for clipping/winsorizing.
            ts_field: Tick timestamp field name (epoch seconds). If absent and fallback enabled, arrival time is used.
            fallback_to_arrival: Use arrival time when ts_field missing.
            price_source_order: Priority order to pick price from tick fields. Defaults to [last_trade, last, mid, bid, ask].
            on_bar_finalized: Optional callback when a bar is emitted.
            on_bar_updated: Optional callback when an in-flight bucket is updated.
        """
        self.interval = float(interval_sec)
        self.watermark_delay = float(watermark_delay)
        self.max_lag_sec = float(max_lag_sec) if max_lag_sec is not None else 2.0 * self.interval
        self.enforce_continuous = bool(enforce_continuous)
        self.outlier_mode = outlier_mode
        self.outlier_window = int(outlier_window)
        self.outlier_threshold = float(outlier_threshold)
        self.ts_field = ts_field
        self.fallback_to_arrival = bool(fallback_to_arrival)
        self.price_source_order = (
            price_source_order
            if price_source_order is not None
            else ["last_trade", "last", "mid", "bid", "ask"]
        )
        self.on_bar_finalized = on_bar_finalized
        self.on_bar_updated = on_bar_updated

        # Active buckets keyed by (symbol, bucket_index)
        self._buckets: Dict[Tuple[str, int], Dict[str, Any]] = {}
        # Stats and per-symbol state
        self._stats = defaultdict(int)  # gaps_filled, late_dropped, outliers_clipped
        self._last_close: Dict[str, Optional[float]] = defaultdict(lambda: None)
        self._finalized_idx: Dict[str, int] = defaultdict(lambda: -1)
        self._sym_state: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"last_price": None, "returns": deque(maxlen=self.outlier_window)}
        )

    def _bucket_key(self, symbol: str, ts: float) -> Tuple[str, int]:
        bucket_index = int(ts // self.interval)
        return symbol, bucket_index

    def _pick_price(self, tick: Dict[str, Any]) -> Optional[float]:
        # Derive mid if possible
        if "mid" not in tick:
            bid = tick.get("bid")
            ask = tick.get("ask")
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                tick = {**tick, "mid": (bid + ask) / 2.0}
        for key in self.price_source_order:
            val = tick.get(key)
            if isinstance(val, (int, float)) and not (isinstance(val, float) and isnan(val)):
                return float(val)
        return None

    def _get_ts(self, tick: Dict[str, Any]) -> Optional[float]:
        ts = tick.get(self.ts_field)
        if isinstance(ts, (int, float)) and not (isinstance(ts, float) and isnan(ts)):
            return float(ts)
        if self.fallback_to_arrival:
            return float(time.time())
        return None

    def _maybe_filter_outlier(self, symbol: str, price: float) -> float:
        st = self._sym_state[symbol]
        last_p = st["last_price"]
        if last_p is None or last_p <= 0.0:
            st["last_price"] = price
            return price
        r = (price - last_p) / last_p
        rets = st["returns"]
        if self.outlier_mode == "none" or len(rets) < max(5, min(10, self.outlier_window // 2)):
            # Not enough history for robust stats
            rets.append(r)
            st["last_price"] = price
            return price
        r_adj = r
        if self.outlier_mode == "winsorize":
            median = float(np.median(rets))
            mad = float(np.median(np.abs(np.array(rets) - median)))
            scaled = 1.4826 * (mad if mad > 1e-12 else np.std(rets) + 1e-12)
            z = (r - median) / scaled
            if abs(z) > self.outlier_threshold:
                r_adj = median + np.sign(z) * self.outlier_threshold * scaled
        elif self.outlier_mode == "clip":
            mu = float(np.mean(rets))
            sd = float(np.std(rets) + 1e-12)
            z = (r - mu) / sd
            if abs(z) > self.outlier_threshold:
                r_adj = mu + np.sign(z) * self.outlier_threshold * sd
        # Apply adjustment if changed
        if r_adj != r:
            self._stats["outliers_clipped"] += 1
            price = last_p * (1.0 + r_adj)
        rets.append(r_adj)
        st["last_price"] = price
        return price

    def add_tick(self, tick: Dict[str, Any]) -> List[Bar]:
        """Add a tick (parsed Level1) and return any newly finalized bars.

        Notes:
            - Bars are finalized immediately when we observe a higher bucket index for
              the symbol (legacy behavior) and additionally when watermark allows via
              explicit ``finalize_until`` calls.
        """
        symbol_val = tick.get("symbol")
        if not isinstance(symbol_val, str):
            return []
        symbol = symbol_val

        ts = self._get_ts(tick)
        if ts is None:
            return []
        price_raw = self._pick_price(tick)
        if price_raw is None:
            return []
        price = self._maybe_filter_outlier(symbol, float(price_raw))

        key_sym, key_idx = self._bucket_key(symbol, ts)
        # Late tick into already finalized bucket? drop if too old
        if key_idx <= self._finalized_idx[symbol]:
            # Still accept if within max_lag_sec and the bucket exists open (very edge), otherwise drop
            start_time = key_idx * self.interval
            if (ts < start_time + self.interval + self.max_lag_sec) and ((symbol, key_idx) in self._buckets):
                pass
            else:
                self._stats["late_dropped"] += 1
                return []

        completed: List[Bar] = []

        # Only finalize older buckets once we have at least one open bucket for this symbol
        # to avoid walking billions of historical indices on first tick.
        if any(sym == symbol for (sym, _) in self._buckets.keys()):
            # Establish an anchor for finalized index on first finalize to avoid scanning from 0
            if self._finalized_idx[symbol] < 0:
                sym_idxs = [idx for (sym, idx) in self._buckets.keys() if sym == symbol]
                if sym_idxs:
                    self._finalized_idx[symbol] = min(sym_idxs) - 1
                else:
                    self._finalized_idx[symbol] = key_idx - 1
            more = self._finalize_symbol_until(symbol, key_idx)
            if more:
                completed.extend(more)

        # Create or update the active bucket
        key = (symbol, key_idx)
        bucket = self._buckets.get(key)
        if bucket is None:
            # Fresh bucket
            start_ts = key_idx * self.interval
            bucket = {
                "symbol": symbol,
                "start_ts": start_ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": float(tick.get("last_trade_size") or 0.0),
                "trades": 1,
            }
            self._buckets[key] = bucket
        else:
            bucket["close"] = price
            bucket["high"] = max(bucket["high"], price)
            bucket["low"] = min(bucket["low"], price)
            bucket["volume"] += float(tick.get("last_trade_size") or 0.0)
            bucket["trades"] += 1

        if self.on_bar_updated is not None:
            try:
                self.on_bar_updated(bucket)
            except Exception:
                pass
        return completed

    def flush(self) -> List[Bar]:
        """Finalize and emit all open buckets immediately (ignores watermark)."""
        completed: List[Bar] = []
        # Group by symbol to fill gaps in order
        by_sym: Dict[str, List[int]] = defaultdict(list)
        for (sym, idx) in self._buckets.keys():
            by_sym[sym].append(idx)
        for sym, idxs in by_sym.items():
            for idx in sorted(idxs):
                completed.extend(self._finalize_symbol_until(sym, idx))
                # finalize the target idx itself
                data = self._buckets.pop((sym, idx), None)
                if data is not None:
                    completed.append(self._finalize_bucket(sym, idx, data))
        return completed

    def finalize_until(self, now_ts: float) -> List[Bar]:
        """Finalize all bars whose end time is before the watermark.

        The watermark is ``now_ts - watermark_delay``.
        """
        if now_ts is None:
            return []
        watermark = now_ts - self.watermark_delay
        watermark_idx = int(watermark // self.interval)
        completed: List[Bar] = []
        # finalize any bucket whose index < watermark_idx
        by_sym: Dict[str, List[int]] = defaultdict(list)
        for (sym, idx) in list(self._buckets.keys()):
            if idx < watermark_idx:
                by_sym[sym].append(idx)
        for sym, idxs in by_sym.items():
            for idx in sorted(idxs):
                completed.extend(self._finalize_symbol_until(sym, idx))
                data = self._buckets.pop((sym, idx), None)
                if data is not None:
                    completed.append(self._finalize_bucket(sym, idx, data))
        return completed

    def _finalize_symbol_until(self, symbol: str, target_idx_exclusive: int) -> List[Bar]:
        """Finalize all bars for symbol with index < target_idx_exclusive, in order.

        Fills gaps with empty bars if configured.
        """
        completed: List[Bar] = []
        # Next index to finalize
        next_idx = self._finalized_idx[symbol] + 1
        while next_idx < target_idx_exclusive:
            key = (symbol, next_idx)
            data = self._buckets.pop(key, None)
            if data is None:
                last_val = self._last_close.get(symbol)
                if self.enforce_continuous and last_val is not None:
                    # create empty gap bar
                    start = next_idx * self.interval
                    last_close = float(last_val)
                    gap_bar = Bar(
                        symbol=symbol,
                        start_ts=start,
                        end_ts=start + self.interval,
                        open=last_close,
                        high=last_close,
                        low=last_close,
                        close=last_close,
                        volume=0.0,
                        trades=0,
                    )
                    completed.append(gap_bar)
                    self._last_close[symbol] = gap_bar.close
                    self._finalized_idx[symbol] = next_idx
                    self._stats["gaps_filled"] += 1
                    if self.on_bar_finalized is not None:
                        try:
                            self.on_bar_finalized(gap_bar)
                        except Exception:
                            pass
                else:
                    # Nothing to finalize, just advance pointer to avoid infinite loop
                    self._finalized_idx[symbol] = next_idx
                next_idx += 1
                continue
            # finalize real bar
            bar = self._finalize_bucket(symbol, next_idx, data)
            completed.append(bar)
            self._last_close[symbol] = bar.close
            self._finalized_idx[symbol] = next_idx
            if self.on_bar_finalized is not None:
                try:
                    self.on_bar_finalized(bar)
                except Exception:
                    pass
            next_idx += 1
        return completed

    def _finalize_bucket(self, symbol: str, idx: int, data: Dict[str, Any]) -> Bar:
        start = data["start_ts"]
        end = start + self.interval
        return Bar(
            symbol=symbol,
            start_ts=start,
            end_ts=end,
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=float(data["volume"]),
            trades=int(data["trades"]),
        )

    def get_open_buckets(self) -> List[Dict[str, Any]]:
        """Return a shallow snapshot of open buckets."""
        return [
            {
                **data,
                "end_ts": data["start_ts"] + self.interval,
                "symbol": sym,
                "bucket_index": idx,
            }
            for (sym, idx), data in self._buckets.items()
        ]

    def stats(self) -> Dict[str, int]:
        return dict(self._stats)


__all__ = ["TimeBarAggregator", "Bar"]