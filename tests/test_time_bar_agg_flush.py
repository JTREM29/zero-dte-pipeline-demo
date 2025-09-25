"""Additional test ensuring rollover logic emits bar when interval passes."""
from __future__ import annotations
import time
from src.aggregation.bar_builder import TimeBarAggregator


def test_rollover_emits():
    agg = TimeBarAggregator(interval_sec=0.25)
    agg.add_tick({"symbol": "ES", "last_trade": 5000.0, "last_trade_size": 1})
    time.sleep(0.3)
    # trigger new bucket
    bars = agg.add_tick({"symbol": "ES", "last_trade": 5000.5, "last_trade_size": 2})
    assert len(bars) == 1
    b = bars[0]
    assert b.high >= b.low
    assert b.trades >= 1
