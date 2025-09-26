from __future__ import annotations
from src.datafeeds.iqfeed_options import IQFeedOptionsGreeks, skew_signal_from_chain


def test_skew_chain_generation_and_signal():
    og = IQFeedOptionsGreeks()
    chain = og.fetch_chain_greeks("@SPX.X")
    assert chain is not None and len(chain) > 0
    sig = skew_signal_from_chain(chain)
    assert "skew_score" in sig and "put_call_iv_spread" in sig
    # Because puts are simulated richer, expect negative spread (bearish skew)
    assert sig["put_call_iv_spread"] <= 0.0
