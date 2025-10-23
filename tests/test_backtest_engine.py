"""Tests for backtest engine skeleton."""
from __future__ import annotations
from src.backtest.engine import BacktestEngine, BacktestResult
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


def test_size_scaled_trade_and_costs():
    # Strategy that sets size=0.5, enters at 100, exits at 110, then resets size to 1.0
    class Sig:
        def __init__(self, name: str, value: float, strength: float | None = None):
            self.name = name
            self.value = value
            if strength is not None:
                self.strength = strength

    class FixedStrategy:
        def __init__(self):
            self._emitted = False

        def evaluate(self, ctx: dict):
            price = ctx.get("close") or ctx.get("lastPrice", 0.0)
            if not self._emitted:
                self._emitted = True
                yield Sig("size_update", 0.5)
                yield Sig("enter_long", price)
            else:
                yield Sig("exit_long", price)
                yield Sig("size_update", 1.0)

    strat = FixedStrategy()
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    bars = [
        {"close": 100.0},
        {"close": 110.0},
    ]
    res: BacktestResult = engine.run(bars, commission=2.0, slippage_bps=10.0)
    assert res.trade_count == 1
    # Gross PnL should be (110-100)*0.5 = 5.0
    assert abs(res.gross_pnl - 5.0) < 1e-6
    # Commission scales by size: 2.0 * 0.5 = 1.0
    assert abs(res.commission_paid - 1.0) < 1e-6
    # Slippage per side bps of price sum scaled by size: ((100+110)*(10/10000))*0.5
    expected_slip = ((100 + 110) * (10 / 10000.0)) * 0.5
    assert abs(res.slippage_paid - expected_slip) < 1e-6
    # Net = gross - commission - slippage
    expected_net = 5.0 - 1.0 - expected_slip
    assert abs(res.total_pnl - expected_net) < 1e-6
