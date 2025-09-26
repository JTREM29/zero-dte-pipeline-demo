from __future__ import annotations
"""Lightweight technical indicator helpers.

These are intentionally minimal, dependency-light (NumPy only) utilities that can
feed into streaming analytics or backtests without pulling in a heavy TA library.

Functions:
- rsi: Simple (non-Wilder smoothed) RSI over closing prices.
- VWAPStream: Incremental VWAP calculator for streaming ticks.
- bollinger: Bollinger band tuple (mid, upper, lower).
- atr: Average True Range (simple average of TR over period).
- choppiness_index: CHOP oscillator measuring trendiness vs. choppiness.
- rolling_vol_zscore: Realized vol over short window and z-score vs. historical vol windows.

All functions return float or tuples of floats; NaN returned when insufficient data.
"""
from dataclasses import dataclass
from typing import Optional, Tuple
import math
import numpy as np


def rsi(close: np.ndarray, period: int = 14) -> float:
    """Compute a simple RSI (no Wilder smoothing) over the last `period` closes.

    Returns NaN if insufficient samples. If there are no losses the RSI is 100.
    """
    if len(close) < period + 1:
        return float("nan")
    # Use only last period+1 points to derive period diffs
    diff = np.diff(close[-(period + 1):])
    gains = np.clip(diff, 0, None)
    losses = -np.clip(diff, None, 0)
    avg_gain = gains.mean() if gains.size else 0.0
    avg_loss = losses.mean() if losses.size else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / max(avg_loss, 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))


def rsi_wilder(close: np.ndarray, period: int = 14) -> float:
    """Wilder-smoothed RSI (single output for current window).

    Uses exponential smoothing approach: prev_avg_gain*(period-1)+current_gain) / period.
    For simplicity we approximate by computing initial averages over first period then
    iteratively smoothing remaining portion (if any). Returns NaN if insufficient data.
    """
    if len(close) < period + 1:
        return float('nan')
    diffs = np.diff(close)
    gains = np.clip(diffs, 0, None)
    losses = -np.clip(diffs, None, 0)
    # Seed averages
    seed_g = gains[:period].mean()
    seed_l = losses[:period].mean()
    avg_g = seed_g
    avg_l = seed_l
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / max(avg_l, 1e-12)
    return 100 - (100 / (1 + rs))


@dataclass
class VWAPStream:
    """Streaming VWAP accumulator.

    Call update(price, volume) for each tick. Volume <= 0 is ignored.
    """
    sum_pv: float = 0.0
    sum_v: float = 0.0
    value: Optional[float] = None

    def update(self, price: float, volume: float) -> float:  # noqa: D401
        self.sum_pv += price * max(volume, 0.0)
        self.sum_v += max(volume, 0.0)
        self.value = (self.sum_pv / self.sum_v) if self.sum_v > 0 else price
        return self.value


def bollinger(close: np.ndarray, period: int = 20, num_std: float = 2.0) -> Tuple[float, float, float]:
    """Return (mid, upper, lower) Bollinger bands.

    If insufficient data, returns all NaN midpoint (using nanmean for partial window).
    """
    if len(close) < period:
        if len(close) == 0:
            return float("nan"), float("nan"), float("nan")
        m = float(np.nanmean(close))
        return m, m, m
    window = close[-period:]
    m = float(np.mean(window))
    s = float(np.std(window, ddof=1)) if period > 1 else 0.0
    return m, m + num_std * s, m - num_std * s


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Average True Range using simple (not Wilder) mean of TR values.

    Requires at least period+1 closes. Returns NaN if insufficient.
    """
    n = len(close)
    if n < period + 1:
        return float("nan")
    # Build True Range list over last `period` bars
    tr = []
    # Index i iterates over the TR contributing bars; uses i and i+1 referencing prior close
    for i in range(n - period - 1, n - 1):
        h = high[i + 1]
        l = low[i + 1]
        pc = close[i]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(tr))


def choppiness_index(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    """Compute the Choppiness Index (0..100).

    100 * log10(sum(TR) / (max(high)-min(low))) / log10(period)
    Clamped to [0, 100]. Returns NaN if insufficient data.
    """
    if len(close) < period + 1:
        return float("nan")
    tr_sum = 0.0
    highest = -1e308
    lowest = 1e308
    # Determine extrema over last `period` bars
    for i in range(-period, 0):
        h = high[i]
        l = low[i]
        highest = max(highest, h)
        lowest = min(lowest, l)
    # Sum TR across last period bars
    for i in range(-period - 1, -1):
        h = high[i + 1]
        l = low[i + 1]
        pc = close[i]
        tr_sum += max(h - l, abs(h - pc), abs(l - pc))
    denom = max(highest - lowest, 1e-9)
    chop = 100.0 * math.log10(tr_sum / denom) / math.log10(period)
    return float(max(0.0, min(100.0, chop)))


def rolling_vol_zscore(close: np.ndarray, lookback_vol: int = 20, lookback_z: int = 252) -> Tuple[float, float]:
    """Return (realized_vol_annualized, zscore) using close-to-close returns.

    realized_vol = stdev(rets[-lookback_vol:]) * sqrt(252)
    z = (realized_vol - mean(hist_vol_windows)) / std(hist_vol_windows)

    Returns (nan, nan) if insufficient data.
    """
    needed = max(lookback_vol + 1, lookback_z + 1)
    if len(close) < needed:
        return float("nan"), float("nan")
    rets = np.diff(close) / close[:-1]
    rv = float(np.std(rets[-lookback_vol:], ddof=1) * math.sqrt(252))
    # Build historical realized vol series for z-score
    upper = min(len(rets), lookback_z)
    hist_vol = [
        np.std(rets[i - lookback_vol:i], ddof=1) * math.sqrt(252)
        for i in range(lookback_vol, upper + 1)
    ]
    hv = np.array(hist_vol)
    mean_hv = float(np.mean(hv))
    std_hv = float(np.std(hv, ddof=1)) if len(hv) > 1 else 0.0
    z = (rv - mean_hv) / max(std_hv, 1e-9)
    return rv, float(z)


def garman_klass_vol(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 20) -> float:
    """Garman-Klass volatility estimator (annualized) over last `period` bars.

    σ_GK^2 = 0.5*(ln(H/L))^2 - (2ln2 -1)*(ln(C/O))^2 ; annualize via sqrt(252)
    Returns NaN if insufficient data.
    """
    if len(close) < period:
        return float('nan')
    import numpy as _np
    window = slice(-period, None)
    hl = np.log(high[window] / low[window])
    co = np.log(close[window] / open_[window])
    var = 0.5 * (hl ** 2) - (2 * np.log(2) - 1) * (co ** 2)
    est = float(np.sqrt(max(var.mean(), 0.0)) * math.sqrt(252))
    return est


def rogers_satchell_vol(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 20) -> float:
    """Rogers-Satchell volatility estimator (annualized) over last `period` bars.

    RS = mean( ln(H/O)*(ln(H/O) - ln(C/O)) + ln(L/O)*(ln(L/O) - ln(C/O)) )
    Annualize via sqrt(252). Returns NaN if insufficient data.
    """
    if len(close) < period:
        return float('nan')
    w = slice(-period, None)
    log_h_o = np.log(high[w] / open_[w])
    log_l_o = np.log(low[w] / open_[w])
    log_c_o = np.log(close[w] / open_[w])
    rs_terms = log_h_o * (log_h_o - log_c_o) + log_l_o * (log_l_o - log_c_o)
    var = float(rs_terms.mean())
    return float(math.sqrt(max(var, 0.0)) * math.sqrt(252))
