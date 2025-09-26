"""Option math utilities (Black-Scholes and implied volatility placeholder).

We keep dependencies minimal (no external quant libraries). IV solver here is a
simple Newton-Raphson with fallback bisection for robustness; good enough for
research scaffolding and unit tests but not production-grade greeks surface fitting.
"""
from __future__ import annotations
from math import log, sqrt, exp, erf
from dataclasses import dataclass
from typing import Optional


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    from math import exp, pi
    return (1.0 / sqrt(2 * pi)) * exp(-0.5 * x * x)


def black_scholes_price(spot: float, strike: float, t: float, vol: float, rate: float, call: bool = True) -> float:
    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if call else (strike - spot))
    d1 = (log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * sqrt(t))
    d2 = d1 - vol * sqrt(t)
    if call:
        return spot * _norm_cdf(d1) - strike * exp(-rate * t) * _norm_cdf(d2)
    else:
        return strike * exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float, rate: float, call: bool = True, 
                initial_vol: float = 0.2) -> Optional[float]:
    """Solve implied volatility using Newton-Raphson + bounded fallback.

    Returns None if it fails to converge in bounds (0.0001, 5.0).
    """
    if price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return None
    vol = max(1e-4, min(initial_vol, 5.0))
    for _ in range(10):
        d1 = (log(spot/strike) + (rate + 0.5*vol*vol)*t) / (vol*sqrt(t))
        d2 = d1 - vol*sqrt(t)
        theo = black_scholes_price(spot, strike, t, vol, rate, call)
        vega = spot * _norm_pdf(d1) * sqrt(t)
        diff = theo - price
        if abs(diff) < 1e-6:
            return vol
        if vega < 1e-8:
            break
        vol -= diff / vega
        if vol <= 0 or vol > 5:
            break
    # Fallback bisection
    lo, hi = 1e-4, 5.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        theo = black_scholes_price(spot, strike, t, mid, rate, call)
        if abs(theo - price) < 1e-5:
            return mid
        if theo > price:
            hi = mid
        else:
            lo = mid
    return None


@dataclass
class OptionMetrics:
    price: float
    iv: Optional[float]
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None  # per year units (convert externally if needed)
    vega: Optional[float] = None   # per 1 volatility (not percent)


def compute_option_metrics(spot: float, strike: float, t_days: float, mid_price: float, call: bool = True, rate: float = 0.0) -> OptionMetrics:
    t_years = max(0.0, t_days / 365.0)
    iv = implied_vol(mid_price, spot, strike, t_years, rate, call=call, initial_vol=0.2)
    if iv is None or t_years <= 0 or spot <= 0 or strike <= 0:
        return OptionMetrics(price=mid_price, iv=iv)
    d1 = (log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / (iv * sqrt(t_years))
    d2 = d1 - iv * sqrt(t_years)
    pdf = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)
    if call:
        delta = cdf_d1
        theta = -(spot * pdf * iv / (2 * sqrt(t_years))) - rate * strike * exp(-rate * t_years) * cdf_d2
    else:
        delta = cdf_d1 - 1
        theta = -(spot * pdf * iv / (2 * sqrt(t_years))) + rate * strike * exp(-rate * t_years) * _norm_cdf(-d2)
    gamma = pdf / (spot * iv * sqrt(t_years))
    vega = spot * pdf * sqrt(t_years)
    return OptionMetrics(price=mid_price, iv=iv, delta=delta, gamma=gamma, theta=theta, vega=vega)


__all__ = [
    "black_scholes_price",
    "implied_vol",
    "compute_option_metrics",
    "OptionMetrics",
]