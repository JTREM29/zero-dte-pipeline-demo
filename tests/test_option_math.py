from __future__ import annotations
from src.utils.options_math import black_scholes_price, implied_vol, compute_option_metrics


def test_black_scholes_put_call_parity_monotonic():
    spot = 100
    strike = 100
    t = 1.0
    vol = 0.2
    r = 0.0
    call = black_scholes_price(spot, strike, t, vol, r, call=True)
    put = black_scholes_price(spot, strike, t, vol, r, call=False)
    # ATM call ~ ATM put when r=0
    assert abs(call - put) < 1.0


def test_implied_vol_recovery():
    spot = 4000
    strike = 4050
    t_years = 7/365
    true_vol = 0.25
    r = 0.00
    price = black_scholes_price(spot, strike, t_years, true_vol, r, call=True)
    est = implied_vol(price, spot, strike, t_years, r, call=True, initial_vol=0.2)
    assert est is not None
    assert abs(est - true_vol) < 0.05


def test_compute_option_metrics_greeks():
    spot = 100
    strike = 105
    days = 7
    # Assume a mid price derived from some vol ~0.3
    vol_guess = 0.3
    t_years = days / 365
    mid = black_scholes_price(spot, strike, t_years, vol_guess, 0.0, call=True)
    metrics = compute_option_metrics(spot, strike, days, mid, call=True)
    assert metrics.iv is not None
    assert metrics.delta is not None and -0.1 < metrics.delta < 1.1
    assert metrics.gamma is not None and metrics.gamma > 0
    assert metrics.vega is not None and metrics.vega > 0