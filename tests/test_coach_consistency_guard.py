from delivery.coach_consistency import CoachTokens, apply_consistency_guard, normalize_coach_output


def test_normalize_requires_envelope():
    txt, ok = normalize_coach_output("hello")
    assert ok is False
    assert txt == ""


def test_guard_blocks_trade_when_no_trade():
    raw = "\n".join(
        [
            "A) State",
            "Regime=TREND | Bias=BEARISH | Confirm=DOWN | Confidence=0.8 | Eligibility=NO_TRADE | DataHealth=OK",
            "",
            "B) Why",
            "- test",
            "",
            "C) Action",
            "- Enter calls on the next dip.",
            "",
            "D) Risk + invalidation",
            "- n/a",
        ]
    )
    tokens = CoachTokens(symbol="SPY", bias="BEARISH", regime="TREND", eligibility="NO_TRADE", data_health="OK")
    out = apply_consistency_guard(raw, tokens=tokens)
    assert "DO NOTHING" in out


def test_guard_reframes_opposite_bias_as_countertrend():
    raw = "\n".join(
        [
            "A) State",
            "Regime=TREND | Bias=BEARISH | Confirm=DOWN | Confidence=0.8 | Eligibility=ELIGIBLE | DataHealth=OK",
            "",
            "B) Why",
            "- test",
            "",
            "C) Action",
            "- Bullish breakout above R1.",
            "",
            "D) Risk + invalidation",
            "- n/a",
        ]
    )
    tokens = CoachTokens(symbol="SPY", bias="BEARISH", regime="TREND", eligibility="ELIGIBLE", data_health="OK")
    out = apply_consistency_guard(raw, tokens=tokens)
    assert "Countertrend" in out
