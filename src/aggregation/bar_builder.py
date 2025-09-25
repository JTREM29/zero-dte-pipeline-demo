"""Bar aggregation utilities for Level1 ticks.

Currently provides a simple time bucket aggregator (e.g., 1s or 5s bars) based on
wall-clock arrival time. For production-grade aggregation you may wish to use the
exchange timestamp from the feed once exposed.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple
import time
from math import isnan


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
    def __init__(self, interval_sec: float = 1.0):
        self.interval = interval_sec
        self._buckets: Dict[Tuple[str, int], Dict[str, Any]] = {}

    def _bucket_key(self, symbol: str, ts: float) -> Tuple[str, int]:
        bucket_index = int(ts // self.interval)
        return symbol, bucket_index

    def add_tick(self, tick: Dict[str, Any]) -> List[Bar]:
        """Add a tick (parsed Level1) and return any completed bars."""
        symbol_val = tick.get("symbol")
        if not isinstance(symbol_val, str):
            return []
        symbol = symbol_val
        price = tick.get("last_trade") or tick.get("last") or tick.get("bid") or tick.get("ask")
        if price is None or not isinstance(price, (int, float)) or (isinstance(price, float) and isnan(price)):
            return []
        ts = time.time()
        key = self._bucket_key(symbol, ts)
        bucket = self._buckets.get(key)
        completed: List[Bar] = []
        if bucket is None:
            # finalize any previous bucket for this symbol
            to_del = []
            for (sym, idx), data in self._buckets.items():
                if sym == symbol and idx < key[1]:
                    completed.append(self._finalize_bucket(sym, idx, data))
                    to_del.append((sym, idx))
            for k in to_del:
                self._buckets.pop(k, None)
            bucket = {
                "symbol": symbol,
                "start_ts": ts - (ts % self.interval),
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
        return completed

    def flush(self) -> List[Bar]:
        completed: List[Bar] = []
        for (sym, idx), data in list(self._buckets.items()):
            completed.append(self._finalize_bucket(sym, idx, data))
            self._buckets.pop((sym, idx), None)
        return completed

    def _finalize_bucket(self, symbol: str, idx: int, data: Dict[str, Any]) -> Bar:
        start = data["start_ts"]
        end = start + self.interval
        return Bar(
            symbol=symbol,
            start_ts=start,
            end_ts=end,
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            volume=data["volume"],
            trades=data["trades"],
        )


__all__ = ["TimeBarAggregator", "Bar"]