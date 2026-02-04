import pandas as pd

from technicals.divergence import divergence_trade_plan


class DummyRes:
    def __init__(self, kind: str, timeframe: str = "5m", rsi_period: int = 14):
        self.kind = kind
        self.timeframe = timeframe
        self.rsi_period = rsi_period
        self.strength = 0.8

        class P:
            def __init__(self, value, ts):
                self.value = value
                self.ts = ts

        self.price_pivots = (P(100, "t1"), P(101, "t2"))
        self.rsi_pivots = (P(60, "t1"), P(50, "t2"))
        self.summary = "ok"


def test_vwap_line_added():
    df = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-21 10:00", periods=5, freq="min"),
            "open": [100, 100, 100, 100, 100],
            "high": [100, 100, 100, 100, 100],
            "low": [100, 100, 100, 100, 100],
            "close": [100, 100, 100, 100, 100],
            "volume": [10, 10, 10, 10, 10],
        }
    )
    res = DummyRes("bearish_rsi_divergence")
    text = divergence_trade_plan(res, symbol="SPY", bars_df=df)
    assert "VWAP (RTH):" in text
