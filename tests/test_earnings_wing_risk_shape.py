from __future__ import annotations

from services.calendar.earnings_options import classify_wing_risk_shape, compute_wing_skew_score


def test_wing_risk_shape_balanced() -> None:
    score = compute_wing_skew_score(atm_straddle=40.0, up_cost=40.0, down_cost=40.0)
    assert score == 0.0
    assert classify_wing_risk_shape(score=score) == "BALANCED"


def test_wing_risk_shape_upside_skew() -> None:
    # (up - down) / atm = (46 - 40)/40 = +0.15
    score = compute_wing_skew_score(atm_straddle=40.0, up_cost=46.0, down_cost=40.0)
    assert score is not None and score > 0
    assert classify_wing_risk_shape(score=score) == "UPSIDE_SKEW"


def test_wing_risk_shape_downside_skew() -> None:
    # (up - down) / atm = (34 - 40)/40 = -0.15
    score = compute_wing_skew_score(atm_straddle=40.0, up_cost=34.0, down_cost=40.0)
    assert score is not None and     score < 0
    assert classify_wing_risk_shape(score=score) == "DOWNSIDE_SKEW"
