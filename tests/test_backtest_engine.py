"""Tests for backtest engine skeleton."""
from __future__ import annotations
from src.backtest.engine import BacktestEngine
from src.strategies.simple_intraday_spx import SimpleIntradaySPXStrategy


def test_backtest_engine_counts():
    strat = SimpleIntradaySPXStrategy()
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    bars = [
        {"lastPrice": 10.0},
        {"lastPrice": 11.0},
        {"lastPrice": 12.0},
    ]
    result = engine.run(bars)
    assert result.bars_processed == 3
    assert result.signals_emitted == 3  # one signal per bar in current strategy
    # With 3 signals, one open trade may remain incomplete, so wins+losses <= signals//2
    assert result.wins + result.losses <= result.signals_emitted // 2
    # Equity curve shape validations
    assert isinstance(result.equity_curve, list)
    assert all(isinstance(x, (int, float)) for x in result.equity_curve)
    assert result.max_drawdown >= 0
