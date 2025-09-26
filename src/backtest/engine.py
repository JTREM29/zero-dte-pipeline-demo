"""Minimal backtest harness skeleton.

This is a placeholder to iterate on strategy evaluation over a sequence of bars.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Any, List, Optional, Sequence, Dict
import math

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
    # PnL fields
    total_pnl: float = 0.0  # net pnl after costs (backward compatibility)
    gross_pnl: float = 0.0  # before commission/slippage
    commission_paid: float = 0.0
    slippage_paid: float = 0.0
    total_costs: float = 0.0
    wins: int = 0
    losses: int = 0
    max_drawdown: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    collected_signals: List[Any] = field(default_factory=list)
    sharpe: float = 0.0
    sortino: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0  # absolute value (positive)
    profit_factor: float = 0.0
    expectancy: float = 0.0
    trade_count: int = 0
    open_trade: bool = False
    trades: List[dict[str, Any]] = field(default_factory=list)
    sharpe_annualized: float = 0.0
    sortino_annualized: float = 0.0
    avg_mae: float = 0.0  # average max adverse excursion per closed trade
    avg_mfe: float = 0.0  # average max favorable excursion per closed trade
    calmar: float = 0.0  # total_pnl / max_drawdown (simplified)
    # Distribution metrics
    pnl_p25: float = 0.0
    pnl_p50: float = 0.0
    pnl_p75: float = 0.0
    pnl_p95: float = 0.0
    mae_p50: float = 0.0
    mfe_p50: float = 0.0
    pnl_quantiles: Dict[str, float] = field(default_factory=dict)  # dynamic quantiles if requested
    avg_signal_strength: float = 0.0  # average of non-null signal strengths

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        if total == 0:
            return 0.0
        return self.wins / total

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

    def run(
        self,
        bars: Iterable[dict[str, Any]],
        compute_metrics: bool = True,
        collect_signals: bool = False,
        annualize_factor: float | None = None,
        quantiles: Optional[Sequence[float]] = None,
        commission: float = 0.0,  # flat per round trip
        slippage_bps: float = 0.0,  # per side, applied on entry+exit (so *2 sides)
    ) -> BacktestResult:
        sig_count = 0
        bar_count = 0
        # Simple alternating position model: every 2 signals is a round trip trade.
        open_price: Optional[float] = None
        gross_pnl_total = 0.0
        net_pnl_total = 0.0
        commission_paid_total = 0.0
        slippage_paid_total = 0.0
        wins = 0
        losses = 0
        equity: List[float] = []
        collected: List[Any] = []
        closed_trade_pnls: List[float] = []
        trades: List[dict[str, Any]] = []
        entry_price: Optional[float] = None
        entry_bar_index: Optional[int] = None
        cur_min_unrealized: Optional[float] = None
        cur_max_unrealized: Optional[float] = None

        strengths: List[float] = []
        for bar in bars:
            bar_count += 1
            for sig in self.strategy.evaluate(bar):
                sig_count += 1
                if collect_signals:
                    collected.append(sig)
                if compute_metrics:
                    price = float(getattr(sig, "value", bar.get("close") or bar.get("lastPrice", 0.0)))
                    # Collect strength if present
                    str_val = getattr(sig, "strength", None)
                    if isinstance(str_val, (int, float)):
                        strengths.append(float(str_val))
                    if open_price is None:
                        # open position
                        open_price = price
                        entry_price = price
                        entry_bar_index = bar_count
                        cur_min_unrealized = 0.0  # flat at open
                        cur_max_unrealized = 0.0
                        if not equity:
                            equity.append(0.0)  # starting equity base 0 for PnL curve
                    else:
                        # update unrealized extremes prior to closing using current price
                        trade_pnl_gross = price - open_price
                        # Costs
                        trade_commission = commission if commission > 0 else 0.0
                        trade_slippage = 0.0
                        if slippage_bps > 0:
                            # slippage per side in bps of price; cost reduces pnl
                            trade_slippage = ((open_price + price) * (slippage_bps / 10000.0))
                        trade_costs = trade_commission + trade_slippage
                        trade_pnl_net = trade_pnl_gross - trade_costs

                        gross_pnl_total += trade_pnl_gross
                        net_pnl_total += trade_pnl_net
                        commission_paid_total += trade_commission
                        slippage_paid_total += trade_slippage
                        if trade_pnl_net >= 0:
                            wins += 1
                        else:
                            losses += 1
                        equity.append(net_pnl_total)
                        closed_trade_pnls.append(trade_pnl_net)
                        # finalize trade with recorded MAE/MFE
                        trades.append({
                            "entry_price": entry_price,
                            "exit_price": price,
                            "gross_pnl": trade_pnl_gross,
                            "net_pnl": trade_pnl_net,
                            "commission": trade_commission,
                            "slippage": trade_slippage,
                            "entry_bar_index": entry_bar_index,
                            "exit_bar_index": bar_count,
                            "bars_held": (bar_count - (entry_bar_index or bar_count)),
                            "mae": cur_min_unrealized,
                            "mfe": cur_max_unrealized,
                        })
                        # reset
                        open_price = None
                        entry_price = None
                        entry_bar_index = None
                        cur_min_unrealized = None
                        cur_max_unrealized = None
                elif open_price is not None and compute_metrics:
                    # update unrealized pnl extremes intra-trade (no signal produced yet)
                    unreal = price - open_price
                    if cur_min_unrealized is None or unreal < cur_min_unrealized:
                        cur_min_unrealized = unreal
                    if cur_max_unrealized is None or unreal > cur_max_unrealized:
                        cur_max_unrealized = unreal

        max_dd = 0.0
        if equity:
            peak = equity[0]
            for val in equity:
                if val > peak:
                    peak = val
                drawdown = peak - val
                if drawdown > max_dd:
                    max_dd = drawdown

        # Compute Sharpe / Sortino (simple, based on incremental equity differences)
        sharpe = 0.0
        sortino = 0.0
        if len(equity) > 1:
            returns = [equity[i] - equity[i-1] for i in range(1, len(equity))]
            if returns:
                mean_ret = sum(returns) / len(returns)
                std_ret = math.sqrt(sum((r - mean_ret)**2 for r in returns) / len(returns)) if len(returns) > 1 else 0.0
                if std_ret > 0:
                    sharpe = mean_ret / std_ret
                neg = [r for r in returns if r < 0]
                if neg:
                    downside_std = math.sqrt(sum(r*r for r in neg) / len(neg))
                    if downside_std > 0:
                        sortino = mean_ret / downside_std

        # Trade stats
        pos_pnls = [p for p in closed_trade_pnls if p > 0]
        neg_pnls = [p for p in closed_trade_pnls if p < 0]
        avg_win = (sum(pos_pnls) / len(pos_pnls)) if pos_pnls else 0.0
        avg_loss = (abs(sum(neg_pnls)) / len(neg_pnls)) if neg_pnls else 0.0
        profit_factor = (sum(pos_pnls) / abs(sum(neg_pnls))) if neg_pnls else (1.0 if pos_pnls else 0.0)
        win_rate = (wins / (wins + losses)) if (wins + losses) else 0.0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
        open_trade = open_price is not None

        sharpe_ann = 0.0
        sortino_ann = 0.0
        if annualize_factor and annualize_factor > 1:
            sharpe_ann = sharpe * (annualize_factor ** 0.5)
            sortino_ann = sortino * (annualize_factor ** 0.5)

        # MAE/MFE aggregates
        mae_vals = [t["mae"] for t in trades if t and t.get("mae") is not None]
        mfe_vals = [t["mfe"] for t in trades if t and t.get("mfe") is not None]
        avg_mae = sum(mae_vals) / len(mae_vals) if mae_vals else 0.0
        avg_mfe = sum(mfe_vals) / len(mfe_vals) if mfe_vals else 0.0

        calmar = (net_pnl_total / max_dd) if max_dd > 0 else 0.0

        # Percentiles helper
        def _pct(arr: List[float], q: float) -> float:
            if not arr:
                return 0.0
            if q <= 0:
                return arr[0]
            if q >= 1:
                return arr[-1]
            pos = q * (len(arr) - 1)
            lo = int(pos)
            hi = min(lo + 1, len(arr) - 1)
            if lo == hi:
                return arr[lo]
            frac = pos - lo
            return arr[lo] + (arr[hi] - arr[lo]) * frac

        closed_sorted = sorted(closed_trade_pnls)
        pnl_p25 = _pct(closed_sorted, 0.25)
        pnl_p50 = _pct(closed_sorted, 0.50)
        pnl_p75 = _pct(closed_sorted, 0.75)
        pnl_p95 = _pct(closed_sorted, 0.95)
        mae_sorted = sorted(mae_vals)
        mfe_sorted = sorted(mfe_vals)
        mae_p50 = _pct(mae_sorted, 0.50) if mae_sorted else 0.0
        mfe_p50 = _pct(mfe_sorted, 0.50) if mfe_sorted else 0.0

        # Dynamic quantiles
        pnl_quantiles: Dict[str, float] = {}
        if quantiles:
            # sanitize and compute
            uniq = sorted({q for q in quantiles if 0.0 <= q <= 1.0})
            for q in uniq:
                key = f"q{int(q*100):02d}" if q > 0 else "q00"
                pnl_quantiles[key] = _pct(closed_sorted, q)

        avg_strength = (sum(strengths) / len(strengths)) if strengths else 0.0
        return BacktestResult(
            signals_emitted=sig_count,
            bars_processed=bar_count,
            total_pnl=net_pnl_total,
            gross_pnl=gross_pnl_total,
            commission_paid=commission_paid_total,
            slippage_paid=slippage_paid_total,
            total_costs=(commission_paid_total + slippage_paid_total),
            wins=wins,
            losses=losses,
            max_drawdown=max_dd,
            equity_curve=equity,
            collected_signals=collected,
            sharpe=sharpe,
            sortino=sortino,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            expectancy=expectancy,
            trade_count=wins + losses,
            open_trade=open_trade,
            trades=trades,
            sharpe_annualized=sharpe_ann,
            sortino_annualized=sortino_ann,
            avg_mae=avg_mae,
            avg_mfe=avg_mfe,
            calmar=calmar,
            pnl_p25=pnl_p25,
            pnl_p50=pnl_p50,
            pnl_p75=pnl_p75,
            pnl_p95=pnl_p95,
            mae_p50=mae_p50,
            mfe_p50=mfe_p50,
            pnl_quantiles=pnl_quantiles,
            avg_signal_strength=avg_strength,
        )
