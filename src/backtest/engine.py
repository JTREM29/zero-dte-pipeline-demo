"""Minimal backtest harness skeleton.

This is a placeholder to iterate on strategy evaluation over a sequence of bars.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Protocol, Any

try:
    # Optional import; strategy signal shape (not strictly required for the protocol)
    from src.strategies.simple_intraday_spx import StrategySignal  # type: ignore
except Exception:  # pragma: no cover - soft import
    class StrategySignal:  # type: ignore
        pass


class Bar(Protocol):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Strategy(Protocol):
    """Strategy interface expected by BacktestEngine.

    The strategy should yield zero or more StrategySignal objects for each bar.
    """

    def evaluate(self, market_ctx: dict[str, Any]) -> Iterable[StrategySignal]:  # noqa: D401
        ...  # pragma: no cover


@dataclass
class BacktestResult:
    signals_emitted: int
    bars_processed: int


class BacktestEngine:
    def __init__(self, strategy: Strategy):
        self.strategy = strategy

    def run(self, bars: Iterable[dict[str, Any]]) -> BacktestResult:
        sig_count = 0
        bar_count = 0
        for bar in bars:
            bar_count += 1
            for _sig in self.strategy.evaluate(bar):
                sig_count += 1
        return BacktestResult(signals_emitted=sig_count, bars_processed=bar_count)
