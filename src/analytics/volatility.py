"""Rolling volatility / return statistics helpers for streaming prices."""
from __future__ import annotations
from collections import deque
from typing import Deque, Optional, List
import math


class RollingVolatility:
    """Maintains rolling window of prices and returns; computes volatility statistics.

    Supports arithmetic or log returns and optional EWMA volatility.

    Parameters
    ----------
    window : int
        Rolling window length for *simple* (sample std) statistics.
    annualize_factor : float | None
        If provided, scales std_return via sqrt(factor) for annualized_vol.
    log_returns : bool
        If True, use log(p_t / p_{t-1}) instead of arithmetic (p_t / p_{t-1} - 1).
    ewma_alpha : float | None
        If provided (0<alpha<=1), maintain an exponentially weighted moving
        variance estimate producing ewma_vol.
    """

    def __init__(self, window: int = 30, annualize_factor: float | None = None,
                 log_returns: bool = False, ewma_alpha: float | None = None):
        if window < 2:
            raise ValueError("window must be >= 2")
        if ewma_alpha is not None and not (0 < ewma_alpha <= 1):
            raise ValueError("ewma_alpha must be in (0,1]")
        self.window = window
        self._prices: Deque[float] = deque(maxlen=window)
        self._returns: Deque[float] = deque(maxlen=window)
        self.annualize_factor = annualize_factor  # e.g. ticks_per_year for scaling
        self.log_returns = log_returns
        self.ewma_alpha = ewma_alpha
        self._ewma_var: float | None = None

    def update(self, price: float) -> None:
        if price <= 0:
            return
        if self._prices and self._prices[-1] > 0:
            prev = self._prices[-1]
            if self.log_returns:
                r = math.log(price / prev)
            else:
                r = (price / prev) - 1.0
            self._returns.append(r)
            # EWMA variance update: var_t = (1-alpha)*var_{t-1} + alpha*r^2
            if self.ewma_alpha is not None:
                if self._ewma_var is None:
                    self._ewma_var = r * r
                else:
                    a = self.ewma_alpha
                    self._ewma_var = (1 - a) * self._ewma_var + a * (r * r)
        self._prices.append(price)

    @property
    def count(self) -> int:
        return len(self._prices)

    @property
    def mean_return(self) -> float:
        if not self._returns:
            return 0.0
        return sum(self._returns) / len(self._returns)

    @property
    def std_return(self) -> float:
        n = len(self._returns)
        if n < 2:
            return 0.0
        mean_r = self.mean_return
        var = sum((r - mean_r) ** 2 for r in self._returns) / (n - 1)
        return math.sqrt(var)

    @property
    def rolling_vol(self) -> float:
        return self.std_return

    @property
    def annualized_vol(self) -> float:
        if not self.annualize_factor or self.std_return == 0:
            return 0.0
        return self.std_return * math.sqrt(self.annualize_factor)

    @property
    def ewma_vol(self) -> float:
        if self._ewma_var is None:
            return 0.0
        return math.sqrt(self._ewma_var)

    @property
    def realized_var(self) -> float:
        """Realized variance (sum of squared returns) over window.

        Not divided by (n-1); this is the raw sum useful for scaling.
        """
        if not self._returns:
            return 0.0
        return sum(r * r for r in self._returns)

    @property
    def realized_vol(self) -> float:
        # sqrt of realized variance (per-window, not annualized)
        rv = self.realized_var
        return math.sqrt(rv) if rv > 0 else 0.0

    @property
    def realized_vol_annualized(self) -> float:
        if not self.annualize_factor:
            return 0.0
        return self.realized_vol * math.sqrt(self.annualize_factor)

    @property
    def parkinson_vol(self) -> float:
        """Approximate Parkinson volatility using adjacent tick high/low pairs.

        For each adjacent pair treat (prev, cur) as a micro-bar; then
        ln(H/L)^2 = (log(price_t / price_{t-1}))^2. Classic formula:
            sigma^2 = (1 / (4 n ln 2)) * Σ log(H_i / L_i)^2
        Here Σ log(H/L)^2 reduces to Σ log_ratio^2 for each return (log returns).
        We use log returns even if arithmetic mode is selected (convert) to preserve scale.
        """
        n = len(self._returns)
        if n < 2:
            return 0.0
        # Use log returns for stability
        logsq_sum = 0.0
        if self.log_returns:
            logsq_sum = sum(r * r for r in self._returns)
        else:
            # convert arithmetic to log approx: log(1+r)
            logsq_sum = sum(math.log(1 + r) ** 2 for r in self._returns if (1 + r) > 0)
        denom = 4 * n * math.log(2)
        if denom <= 0:
            return 0.0
        var = logsq_sum / denom
        return math.sqrt(var) if var > 0 else 0.0

    def returns_list(self) -> List[float]:
        return list(self._returns)

    def snapshot(self) -> dict:
        data = {
            "window": self.window,
            "count": self.count,
            "mean_return": self.mean_return,
            "std_return": self.std_return,
            "rolling_vol": self.rolling_vol,
            "annualized_vol": self.annualized_vol,
            "realized_var": self.realized_var,
            "realized_vol": self.realized_vol,
            "realized_vol_annualized": self.realized_vol_annualized,
            "parkinson_vol": self.parkinson_vol,
        }
        if self.log_returns:
            data["return_mode"] = "log"
        else:
            data["return_mode"] = "arithmetic"
        if self.ewma_alpha is not None:
            data["ewma_vol"] = self.ewma_vol
            data["ewma_alpha"] = self.ewma_alpha
        return data

__all__ = ["RollingVolatility"]