"""Tests for backtest engine skeleton."""
from __future__ import annotations
from src.backtest.engine import BacktestEngine
from src.strategies.simple_intraday_spx import SimpleIntradaySPXStrategy


def test_backtest_engine_counts():
    strat = SimpleIntradaySPXStrategy()
    engine = BacktestEngine(strat)
    bars = [
        {"lastPrice": 10.0},
        {"lastPrice": 11.0},
        {"lastPrice": 12.0},
    ]
    result = engine.run(bars)
    assert result.bars_processed == 3
    assert result.signals_emitted == 3  # one signal per bar in current strategy
