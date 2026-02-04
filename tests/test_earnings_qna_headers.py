from services.calendar.earnings_qna import earnings_reaction_answer_header, earnings_risk_answer_header


def test_reaction_header_continuation_language() -> None:
    payload = {
        "history": [
            {"tag": "gap, continuation"},
            {"tag": "gap, continuation"},
            {"tag": "gap, fade"},
            {"tag": "gap, chop"},
        ]
    }
    h = earnings_reaction_answer_header("TSLA", payload)
    assert "often gaps and continues" in h


def test_reaction_header_mixed_language() -> None:
    payload = {
        "history": [
            {"tag": "gap, chop"},
            {"tag": "inside range"},
            {"tag": "gap, continuation"},
            {"tag": "gap, fade"},
        ]
    }
    h = earnings_reaction_answer_header("META", payload)
    assert "choppy/mixed" in h


def test_risk_header_high_risk_threshold() -> None:
    payload = {"expected_move_pct": 7.2, "iv_state": {"crush_risk": "HIGH"}}
    h = earnings_risk_answer_header("META", payload)
    assert "high" in h.lower()
    assert "iv crush risk" in h.lower()


def test_risk_header_elevated_threshold() -> None:
    payload = {"expected_move_pct": 5.2, "iv_state": {"crush_risk": "MED"}}
    h = earnings_risk_answer_header("AAPL", payload)
    assert "moderate" in h.lower()


def test_risk_header_missing_expected_move() -> None:
    payload = {"iv_state": {"crush_risk": "LOW"}}
    h = earnings_risk_answer_header("MSFT", payload)
    assert "risk is unclear" in h.lower()
