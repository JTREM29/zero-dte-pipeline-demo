"""Minimal backtest harness skeleton.

This is a placeholder to iterate on strategy evaluation over a sequence of bars.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Any, List, Optional, Sequence, Dict, Deque, Tuple
from collections import deque
import random
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
    # Slippage-adjusted metrics
    pnl_after_slippage: float = 0.0  # gross - slippage_paid
    slippage_share_of_gross: float = 0.0  # slippage_paid / |gross_pnl| (if gross != 0)
    # Realism counters
    dropped_bars: int = 0
    stale_bars: int = 0
    executed_orders: int = 0
    partial_fills: int = 0

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
        # Trading model: explicit enter_/exit_ signals open/close positions; if unspecified, fall back to alternating.
        open_price: Optional[float] = None
        open_size: Optional[float] = None  # fractional position size applied to PnL and costs
        current_size: float = 1.0  # updated by size_update signals
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
                    # Read signal name if available
                    sig_name = getattr(sig, "name", None)
                    # Handle dynamic sizing without toggling positions
                    if sig_name == "size_update":
                        try:
                            val = float(getattr(sig, "value", current_size))
                            if not (val == val):  # NaN check
                                pass
                            else:
                                current_size = max(0.0, min(1.0, val))
                        except Exception:
                            pass
                        # continue to next signal without affecting position state
                        continue
                    # Collect strength if present
                    str_val = getattr(sig, "strength", None)
                    if isinstance(str_val, (int, float)):
                        strengths.append(float(str_val))

                    # Determine open/close behavior. Prefer explicit enter_/exit_ prefixes if present.
                    is_enter = isinstance(sig_name, str) and sig_name.startswith("enter_")
                    is_exit = isinstance(sig_name, str) and sig_name.startswith("exit_")
                    if open_price is None:
                        if is_exit:
                            # Ignore stray exit without an open
                            continue
                        # open position
                        open_price = price
                        open_size = current_size
                        entry_price = price
                        entry_bar_index = bar_count
                        cur_min_unrealized = 0.0  # flat at open
                        cur_max_unrealized = 0.0
                        if not equity:
                            equity.append(0.0)  # starting equity base 0 for PnL curve
                    else:
                        if is_enter and not is_exit:
                            # Ignore redundant enter while already open
                            continue
                        # update unrealized extremes prior to closing using current price
                        size = open_size if (open_size is not None) else 1.0
                        trade_pnl_gross = (price - open_price) * size
                        # Costs (scale by size)
                        trade_commission = (commission * size) if commission > 0 else 0.0
                        trade_slippage = 0.0
                        if slippage_bps > 0:
                            # slippage per side in bps of price; cost reduces pnl
                            trade_slippage = ((open_price + price) * (slippage_bps / 10000.0)) * size
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
                            "size": size,
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
                        open_size = None
                        entry_price = None
                        entry_bar_index = None
                        cur_min_unrealized = None
                        cur_max_unrealized = None
                elif open_price is not None and compute_metrics:
                    # update unrealized pnl extremes intra-trade (no signal produced yet)
                    # use bar close as current price context
                    cur_price_ctx = float(bar.get("close") or bar.get("lastPrice", 0.0))
                    size = open_size if (open_size is not None) else 1.0
                    unreal = (cur_price_ctx - open_price) * size
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
            pnl_after_slippage=(gross_pnl_total - slippage_paid_total),
            slippage_share_of_gross=(slippage_paid_total / abs(gross_pnl_total)) if abs(gross_pnl_total) > 0 else 0.0,
        )


@dataclass
class RealismConfig:
    """Parameters to replicate live-like issues in backtests.

    - feed_latency_secs: bars are only visible to the strategy after this latency (plus optional jitter)
    - order_delay_secs: orders execute after this delay (plus optional jitter)
    - latency_jitter_secs: uniform jitter in [-jitter, +jitter] applied to both feed and order delays
    - drop_bar_prob: probability a bar is missing (skipped) from both feed and execution timelines
    - stale_bar_prob: probability the observed bar is a stale repeat of the last seen bar (strategy sees old data)
    - partial_fill_min/max: fraction of desired size actually filled on entry (uniform draw)
    - seed: RNG seed for reproducibility
    """
    feed_latency_secs: float = 0.0
    order_delay_secs: float = 0.0
    latency_jitter_secs: float = 0.0
    drop_bar_prob: float = 0.0
    stale_bar_prob: float = 0.0
    partial_fill_min: float = 1.0
    partial_fill_max: float = 1.0
    seed: Optional[int] = None


def _ts_of(bar: dict[str, Any]) -> float:
    for k in ("timestamp", "end_ts", "ts"):
        v = bar.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                continue
    return 0.0


def _bar_price(bar: dict[str, Any]) -> float:
    for k in ("close", "lastPrice", "price", "value"):
        v = bar.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                continue
    return 0.0


def _run_with_realism(
    self,
    bars: Iterable[dict[str, Any]],
    realism: RealismConfig,
    *,
    compute_metrics: bool = True,
    collect_signals: bool = False,
    annualize_factor: float | None = None,
    quantiles: Optional[Sequence[float]] = None,
    commission: float = 0.0,
    slippage_bps: float = 0.0,
) -> BacktestResult:
    # Prepare RNG
    rng = random.Random(realism.seed)
    feed_buf: Deque[dict[str, Any]] = deque()
    observed_last: Optional[dict[str, Any]] = None
    # Convert bars to a list and sort by timestamp
    bar_list = list(bars)
    bar_list.sort(key=_ts_of)

    # State
    sig_count = 0
    bar_count = 0
    dropped_bars = 0
    stale_bars = 0
    executed_orders = 0
    partial_fills = 0
    gross_pnl_total = 0.0
    net_pnl_total = 0.0
    commission_paid_total = 0.0
    slippage_paid_total = 0.0
    equity: List[float] = []
    collected: List[Any] = []
    closed_trade_pnls: List[float] = []
    trades: List[dict[str, Any]] = []
    wins = 0
    losses = 0
    strengths: List[float] = []

    # Position state
    open_price: Optional[float] = None
    open_size: Optional[float] = None
    entry_price: Optional[float] = None
    entry_bar_index: Optional[int] = None
    cur_min_unrealized: Optional[float] = None
    cur_max_unrealized: Optional[float] = None
    current_size: float = 1.0

    # Scheduled orders: (exec_ts, type: 'enter'|'exit', size)
    scheduled: List[Tuple[float, str, float]] = []

    # Main loop over actual bars
    for i, true_bar in enumerate(bar_list):
        # Randomly drop bars
        if realism.drop_bar_prob > 0 and rng.random() < realism.drop_bar_prob:
            dropped_bars += 1
            continue
        bar_count += 1
        true_ts = _ts_of(true_bar)
        feed_buf.append(true_bar)

        # Release observed bars according to feed latency + jitter
        delay = realism.feed_latency_secs
        if realism.latency_jitter_secs > 0:
            delay += rng.uniform(-realism.latency_jitter_secs, realism.latency_jitter_secs)
            if delay < 0:
                delay = 0.0
        release_cutoff = true_ts - delay
        # Process all bars that became visible now
        while feed_buf and _ts_of(feed_buf[0]) <= release_cutoff:
            obs = feed_buf.popleft()
            observed = obs
            if realism.stale_bar_prob > 0 and rng.random() < realism.stale_bar_prob and observed_last is not None:
                observed = dict(observed_last)  # stale repeat
                stale_bars += 1
            observed_last = observed
            # Evaluate strategy on the observed bar, schedule orders with order delay
            for sig in self.strategy.evaluate(observed):
                sig_count += 1
                if collect_signals:
                    collected.append(sig)
                # Strength capture
                str_val = getattr(sig, "strength", None)
                if isinstance(str_val, (int, float)):
                    strengths.append(float(str_val))
                # Sizing updates don't create orders
                sig_name = getattr(sig, "name", None)
                if sig_name == "size_update":
                    try:
                        val = float(getattr(sig, "value", current_size))
                        if val == val:
                            current_size = max(0.0, min(1.0, val))
                    except Exception:
                        pass
                    continue
                # Determine enter/exit intent
                is_enter = isinstance(sig_name, str) and sig_name.startswith("enter_")
                is_exit = isinstance(sig_name, str) and sig_name.startswith("exit_")
                intent: Optional[str] = None
                if open_price is None and not is_exit:
                    intent = "enter"
                elif open_price is not None and not is_enter:
                    intent = "exit"
                else:
                    # Ignore contradictory signals
                    intent = None
                if intent:
                    # schedule with delay + jitter
                    od = realism.order_delay_secs
                    if realism.latency_jitter_secs > 0:
                        od += rng.uniform(-realism.latency_jitter_secs, realism.latency_jitter_secs)
                        if od < 0:
                            od = 0.0
                    exec_ts = _ts_of(observed) + od
                    # size to attempt
                    size_attempt = current_size
                    scheduled.append((exec_ts, intent, size_attempt))

        # Execute any scheduled orders due by now
        if scheduled:
            scheduled.sort(key=lambda x: x[0])
            due: List[Tuple[float, str, float]] = []
            while scheduled and scheduled[0][0] <= true_ts:
                due.append(scheduled.pop(0))
            # Execute in order
            for exec_ts, intent, size_attempt in due:
                executed_orders += 1
                # Partial fill
                size = size_attempt
                if realism.partial_fill_max < 1.0 or realism.partial_fill_min < 1.0:
                    lo = max(0.0, min(1.0, realism.partial_fill_min))
                    hi = max(lo, min(1.0, realism.partial_fill_max))
                    fill = rng.uniform(lo, hi)
                    size = size_attempt * fill
                    partial_fills += 1 if fill < 0.999 else 0
                price = _bar_price(true_bar)
                if intent == "enter" and open_price is None:
                    open_price = price
                    open_size = size
                    entry_price = price
                    entry_bar_index = bar_count
                    cur_min_unrealized = 0.0
                    cur_max_unrealized = 0.0
                    if not equity:
                        equity.append(0.0)
                elif intent == "exit" and open_price is not None:
                    size_eff = open_size if (open_size is not None) else 1.0
                    trade_pnl_gross = (price - open_price) * size_eff
                    trade_commission = (commission * size_eff) if commission > 0 else 0.0
                    trade_slippage = 0.0
                    if slippage_bps > 0:
                        trade_slippage = ((open_price + price) * (slippage_bps / 10000.0)) * size_eff
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
                    trades.append({
                        "entry_price": entry_price,
                        "exit_price": price,
                        "size": size_eff,
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
                    open_size = None
                    entry_price = None
                    entry_bar_index = None
                    cur_min_unrealized = None
                    cur_max_unrealized = None

        # Update unrealized extremes
        if open_price is not None and compute_metrics:
            cur_price_ctx = _bar_price(true_bar)
            size_eff = open_size if (open_size is not None) else 1.0
            unreal = (cur_price_ctx - open_price) * size_eff
            if cur_min_unrealized is None or unreal < cur_min_unrealized:
                cur_min_unrealized = unreal
            if cur_max_unrealized is None or unreal > cur_max_unrealized:
                cur_max_unrealized = unreal

    # Compute summary metrics largely as in run()
    max_dd = 0.0
    if equity:
        peak = equity[0]
        for val in equity:
            if val > peak:
                peak = val
            drawdown = peak - val
            if drawdown > max_dd:
                max_dd = drawdown

    sharpe = 0.0
    sortino = 0.0
    if len(equity) > 1:
        rets = [equity[i] - equity[i-1] for i in range(1, len(equity))]
        if rets:
            mean_ret = sum(rets) / len(rets)
            std_ret = (sum((r - mean_ret)**2 for r in rets) / len(rets)) ** 0.5 if len(rets) > 1 else 0.0
            if std_ret > 0:
                sharpe = mean_ret / std_ret
            neg = [r for r in rets if r < 0]
            if neg:
                downside_std = (sum(r*r for r in neg) / len(neg)) ** 0.5
                if downside_std > 0:
                    sortino = mean_ret / downside_std

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

    mae_vals = [t["mae"] for t in trades if t and t.get("mae") is not None]
    mfe_vals = [t["mfe"] for t in trades if t and t.get("mfe") is not None]
    avg_mae = sum(mae_vals) / len(mae_vals) if mae_vals else 0.0
    avg_mfe = sum(mfe_vals) / len(mfe_vals) if mfe_vals else 0.0
    calmar = (net_pnl_total / max_dd) if max_dd > 0 else 0.0

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

    pnl_quantiles: Dict[str, float] = {}
    if quantiles:
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
        pnl_after_slippage=(gross_pnl_total - slippage_paid_total),
        slippage_share_of_gross=(slippage_paid_total / abs(gross_pnl_total)) if abs(gross_pnl_total) > 0 else 0.0,
        dropped_bars=dropped_bars,
        stale_bars=stale_bars,
        executed_orders=executed_orders,
        partial_fills=partial_fills,
    )

# Attach as method
BacktestEngine.run_with_realism = _run_with_realism  # type: ignore[attr-defined]
