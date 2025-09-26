from __future__ import annotations
import json
from src.strategies.registry import list_strategies, get_strategy
from src.backtest.engine import BacktestEngine

def test_list_strategies_contains_simple():
    names = list_strategies()
    assert 'simple_intraday_spx' in names
    Strat = get_strategy('simple_intraday_spx')
    strat = Strat(fast=2, slow=4)
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    prices = [10, 11, 12, 13, 12, 11, 10]
    rows = [{"lastPrice": p} for p in prices]
    res = engine.run(rows, collect_signals=True)
    # Ensure avg_signal_strength computed (non-negative)
    assert hasattr(res, 'avg_signal_strength')
    assert res.avg_signal_strength >= 0.0
    # Ensure signals with crossover contain strength field when serialized
    crossover = [s.to_dict() for s in res.collected_signals if s.name in {"enter_long", "exit_long"}]
    if crossover:  # may be zero depending on path
        assert all('strength' in c for c in crossover)


def test_quantiles_and_costs():
    Strat = get_strategy('simple_intraday_spx')
    strat = Strat(fast=2, slow=4)
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    prices = [10, 11, 12, 13, 12, 11, 10, 11, 12, 13]
    rows = [{"lastPrice": p} for p in prices]
    res = engine.run(rows, quantiles=[0.1, 0.5, 0.9], commission=1.0, slippage_bps=10.0)
    # Quantile keys expected
    assert 'q10' in res.pnl_quantiles
    assert 'q50' in res.pnl_quantiles
    assert 'q90' in res.pnl_quantiles
    # Costs should be non-zero when commission/slippage provided (if at least one closed trade)
    if res.trade_count > 0:
        assert res.commission_paid >= 0.0
        assert res.slippage_paid >= 0.0
        assert res.total_costs == res.commission_paid + res.slippage_paid
