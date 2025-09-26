from src.strategies.position_sizer import size_from_score


def test_size_linear_and_cap():
    assert size_from_score(0.0, False, False) == 0.0
    assert size_from_score(1.0, False, False) == 0.6  # max_risk default
    assert size_from_score(-1.0, False, False) == 0.6
    # mid score
    assert size_from_score(0.5, False, False) == 0.3
    # out-of-range score is clipped
    assert size_from_score(5.0, False, False) == 0.6


def test_size_haircuts():
    # base for |score|=1 is 0.6
    assert size_from_score(1.0, True, False) == round(0.6 * 0.6, 3)
    assert size_from_score(1.0, False, True) == round(0.6 * 0.6, 3)
    # both haircuts compound
    assert size_from_score(1.0, True, True) == round(0.6 * 0.6 * 0.6, 3)


def test_custom_max_risk():
    assert size_from_score(1.0, False, False, max_risk=0.25) == 0.25
    assert size_from_score(0.4, False, False, max_risk=0.25) == round(0.25 * 0.4, 3)


def test_invalid_max_risk():
    import pytest
    with pytest.raises(ValueError):
        size_from_score(0.5, False, False, max_risk=0.0)