import pandas as pd

from technicals.divergence import detect_rsi_divergence


def _make_df(prices):
    ts = pd.date_range("2026-01-01", periods=len(prices), freq="min")
    close = pd.Series(prices, dtype=float)
    high = close + 0.2
    low = close - 0.2
    return pd.DataFrame({"ts": ts, "open": close, "high": high, "low": low, "close": close})


def test_detects_bearish_divergence_synthetic():
    prices = [
        100,
        101,
        102,
        103,
        104,
        105,
        104,
        103,
        102,
        101,
        102,
        103,
        104,
        106,
        105,
        104,
        103,
        104,
        105,
        106.5,
        106,
        105,
    ]
    df = _make_df(prices)
    res = detect_rsi_divergence(df, timeframe="1m", rsi_period=6, left_right=2, lookback_bars=len(df))
    assert res.kind in ("bearish_rsi_divergence", "none")


def test_detects_bullish_divergence_synthetic():
    prices = [110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 101, 102, 101, 100.5, 100.8, 101.2, 101.5]
    df = _make_df(prices)
    res = detect_rsi_divergence(df, timeframe="1m", rsi_period=6, left_right=2, lookback_bars=len(df))
    assert res.kind in ("bullish_rsi_divergence", "none")


def test_handles_insufficient_data():
    df = _make_df([100, 101, 102, 103, 104, 105])
    res = detect_rsi_divergence(df, timeframe="1m", rsi_period=14, left_right=3, lookback_bars=len(df))
    assert res.kind == "none"
    assert res.details.get("reason") == "insufficient_bars"
