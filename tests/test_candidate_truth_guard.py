from delivery.coach_consistency import apply_candidate_truth_guard


def _envelope(action: str) -> str:
    return "\n".join(
        [
            "A) State",
            "Regime=RISK_ON | Bias=BULLISH | Confirm=YES | Confidence=0.8 | Eligibility=ELIGIBLE | DataHealth=OK | age=10s",
            "",
            "B) Why",
            "- test",
            "",
            "C) Action",
            action,
            "",
            "D) Risk + invalidation",
            "- test",
        ]
    )


def test_candidate_truth_guard_none_rewrites_action() -> None:
    txt = _envelope("- Enter calls here")
    cand = {
        "status": "NONE",
        "why": "REASON_TOKEN",
        "eligibility": "NO_TRADE",
        "confidence": "LOW",
        "approved": [],
        "top": None,
    }
    out = apply_candidate_truth_guard(txt, candidates=cand, question="what structure should I trade?")
    assert "DO NOTHING" in out
    assert "There is no AI trade candidate right now because conditions do not offer a statistical edge" in out


def test_candidate_truth_guard_approved_injects_top() -> None:
    txt = _envelope("- Enter calls here")
    cand = {
        "status": "APPROVED",
        "why": "OK",
        "eligibility": "ELIGIBLE",
        "confidence": "HIGH",
        "approved": [{"strategy": "call_spread", "direction": "BULLISH", "score": 0.9}],
        "top": {"strategy": "call_spread", "direction": "BULLISH", "score": 0.9},
    }
    out = apply_candidate_truth_guard(txt, candidates=cand, question="recommend a 7-14 DTE structure")
    assert "AI Option Structure Candidate" in out
    assert "candidate, not a command" in out.lower()
    assert "call_spread" in out
