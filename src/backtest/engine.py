"""Minimal backtest harness skeleton.

This is a placeholder to iterate on strategy evaluation over a sequence of bars.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Any, List, Optional

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
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    max_drawdown: float = 0.0
    equity_curve: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total) if total else 0.0

    @property
    def total_return(self) -> float:
        if not self.equity_curve:
            return 0.0
        start = self.equity_curve[0]
        end = self.equity_curve[-1]
        if start == 0:
            return 0.0
        return (end - start) / start


class BacktestEngine:
    def __init__(self, strategy: Strategy):
        self.strategy = strategy

    def run(self, bars: Iterable[dict[str, Any]], compute_metrics: bool = True) -> BacktestResult:
        sig_count = 0
        bar_count = 0
        # Simple alternating position model: every 2 signals is a round trip trade.
        open_price: Optional[float] = None
        pnl = 0.0
        wins = 0
        losses = 0
        equity: List[float] = []

        for bar in bars:
            bar_count += 1
            for sig in self.strategy.evaluate(bar):
                sig_count += 1
                if compute_metrics:
                    price = float(getattr(sig, "value", bar.get("close") or bar.get("lastPrice", 0.0)))
                    if open_price is None:
                        # open position
                        open_price = price
                        if not equity:
                            equity.append(0.0)  # starting equity base 0 for PnL curve
                    else:
                        trade_pnl = price - open_price
                        pnl += trade_pnl
                        if trade_pnl >= 0:
                            wins += 1
                        else:
                            losses += 1
                        equity.append(pnl)
                        open_price = None

        max_dd = 0.0
        if equity:
            peak = equity[0]
            for val in equity:
                if val > peak:
                    peak = val
                drawdown = peak - val
                if drawdown > max_dd:
                    max_dd = drawdown

        return BacktestResult(
            signals_emitted=sig_count,
            bars_processed=bar_count,
            total_pnl=pnl,
            wins=wins,
            losses=losses,
            max_drawdown=max_dd,
            equity_curve=equity,
        )
