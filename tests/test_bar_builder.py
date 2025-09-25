"""Tests for TimeBarAggregator bar building."""
from __future__ import annotations
import time
from src.aggregation.bar_builder import TimeBarAggregator


def test_bar_builder_basic_sequence():
    agg = TimeBarAggregator(interval_sec=0.5)
    ticks = [
        {"symbol": "SPY", "last_trade": 100.0, "last_trade_size": 10},
        {"symbol": "SPY", "last_trade": 101.0, "last_trade_size": 5},
    ]
    out = []
    for t in ticks:
        out.extend(agg.add_tick(t))
    # No bar yet until interval rollover
    assert out == []
    time.sleep(0.6)
    # Add another tick causing previous bucket to finalize
    out.extend(agg.add_tick({"symbol": "SPY", "last_trade": 102.0, "last_trade_size": 1}))
    assert len(out) == 1
    bar = out[0]
    assert bar.open == 100.0
    assert bar.high == 101.0
    assert bar.low == 100.0
    assert bar.close == 101.0
    assert bar.volume == 15.0


def test_bar_builder_flush():
    agg = TimeBarAggregator(interval_sec=10)
    agg.add_tick({"symbol": "QQQ", "last_trade": 300.0, "last_trade_size": 2})
    bars = agg.flush()
    assert len(bars) == 1
    b = bars[0]
    assert b.symbol == "QQQ"
    assert b.open == b.close == 300.0
