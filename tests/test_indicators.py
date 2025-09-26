import numpy as np
from src.utils.indicators import rsi, VWAPStream, bollinger, atr, choppiness_index, rolling_vol_zscore


def test_rsi_basic():
    prices = np.linspace(100, 120, 40)  # monotonically rising -> RSI near 100
    val = rsi(prices, period=14)
    assert 90 <= val <= 100


def test_vwap_stream():
    v = VWAPStream()
    v.update(100, 10)
    v.update(102, 5)
    v.update(101, 5)
    assert v.value is not None
    expected = (100*10 + 102*5 + 101*5)/(10+5+5)
    assert abs(v.value - expected) < 1e-9


def test_bollinger_insufficient():
    arr = np.array([1.0, 2.0, 3.0])
    mid, up, low = bollinger(arr, period=5)
    assert mid == up == low


def test_bollinger_full():
    arr = np.array([1,2,3,4,5,6,7,8,9,10], dtype=float)
    mid, up, low = bollinger(arr, period=5)
    assert mid == np.mean(arr[-5:])
    assert up > mid and low < mid


def test_atr_nan():
    h = np.array([10,11,12], dtype=float)
    l = np.array([9,10,11], dtype=float)
    c = np.array([9.5,10.5,11.5], dtype=float)
    assert np.isnan(atr(h,l,c,period=5))


def test_atr_basic():
    h = np.array([10,11,12,13,14,15], dtype=float)
    l = np.array([9,10,11,12,13,14], dtype=float)
    c = np.array([9.5,10.5,11.5,12.5,13.5,14.5], dtype=float)
    val = atr(h,l,c,period=3)
    assert val > 0


def test_choppiness_index_bounds():
    h = np.linspace(10, 20, 40)
    l = h - 1
    c = h - 0.5
    val = choppiness_index(h,l,c,period=14)
    assert 0 <= val <= 100


def test_rolling_vol_zscore():
    # create mild random walk
    rng = np.random.default_rng(0)
    prices = 100 + np.cumsum(rng.normal(0, 1, 400))
    rv, z = rolling_vol_zscore(prices, lookback_vol=20, lookback_z=100)
    assert rv > 0 and not np.isnan(z)
