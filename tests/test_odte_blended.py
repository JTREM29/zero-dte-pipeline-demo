import math
from src.strategies.registry import get_strategy


def test_odte_blended_warm_and_signals():
    Strat = get_strategy("odte_blended")
    strat = Strat(rsi_period=5, bb_period=10, entry_threshold=0.3, exit_threshold=0.15)

    # Feed ascending then descending prices to trigger both directions
    prices = [100 + i * 0.4 for i in range(25)] + [110 - i * 0.5 for i in range(25)]
    got_score = False
    signals = []
    for p in prices:
        ctx = {"lastPrice": p, "lastSize": 10, "regime": "trending", "seasonality_bias": 0.0}
        for sig in strat.evaluate(ctx):
            if sig.name == "odte_score":
                got_score = True
                # Strength alignment
                assert 0 <= sig.strength <= 1
            else:
                signals.append(sig.name)

    assert got_score, "Should emit odte_score"
    # Check composite signals appear (at least one enter)
    assert any(s in ("enter_long", "enter_short") for s in signals)
    # Score bounding indirectly via strength check already ensures [-1,1]

def test_odte_blended_skew_component():
    Strat = get_strategy("odte_blended")
    strat = Strat(rsi_period=3, bb_period=5)
    # Provide extreme skew to test clipping
    for p in range(50, 70):
        ctx = {"lastPrice": float(p), "lastSize": 5, "skew_norm": 5.0}
        list(strat.evaluate(ctx))
    score = strat._state.last_score
    assert -1.0 <= score <= 1.0