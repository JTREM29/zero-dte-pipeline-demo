from typing import Optional

from delivery.options_framework import build_options_framework
from zero_dte_pipeline.tech.contracts import validate_options_framework


def _assert_valid_section(text: Optional[str]) -> str:
    assert text is not None
    lines = text.splitlines()
    assert lines[0] == "🧠 Options Framework (Coach)"
    assert 6 <= len(lines) <= 9
    assert not validate_options_framework(text)
    return text


def test_bullish_trend_section_is_contract_safe() -> None:
    text = build_options_framework(
        {
            "bias": "BULL",
                "confirm": "BULLISH",
            "regime": "TREND_UP",
            "pivot_present": True,
            "stand_down": False,
            "technical_state": "FRESH",
        }
    )
    checked = _assert_valid_section(text)
    assert "Directional confirmation favors bullish exposure" in checked


def test_bearish_compression_section_is_contract_safe() -> None:
    text = build_options_framework(
        {
            "bias": "BEAR",
                "confirm": "BEARISH",
            "regime": "compression",
            "pivot_present": True,
            "stand_down": False,
            "technical_state": "OK",
        }
    )
    checked = _assert_valid_section(text)
    assert "compression increases expansion odds" in checked


def test_neutral_range_section_is_contract_safe() -> None:
    meta = {
        "bias": "NEUTRAL",
        "confirm": "NEUTRAL",
        "regime": "range",
        "pivot_present": True,
        "stand_down": False,
        "technical_state": "FRESH",
    }
    assert build_options_framework(meta) is None


def test_section_omitted_when_guard_conditions_fail() -> None:
    meta = {
        "bias": "BULL",
            "confirm": "BULLISH",
        "regime": "TREND",
        "pivot_present": True,
        "stand_down": True,
        "technical_state": "FRESH",
    }
    assert build_options_framework(meta) is None

    meta = {
        "bias": "BULL",
            "confirm": "NEUTRAL",
        "regime": "TREND",
        "pivot_present": True,
        "stand_down": False,
        "technical_state": "FRESH",
    }
    assert build_options_framework(meta) is None

    meta = {
        "bias": "BULL",
            "confirm": "BULLISH",
        "regime": "TREND",
        "pivot_present": False,
        "stand_down": False,
        "technical_state": "FRESH",
    }
    assert build_options_framework(meta) is None

    meta = {
        "bias": "BULL",
            "confirm": "BULLISH",
        "regime": "TREND",
        "pivot_present": True,
        "stand_down": False,
        "technical_state": "STALE",
    }
    assert build_options_framework(meta) is None

    def test_section_omitted_when_confirmation_mismatches() -> None:
        meta = {
            "bias": "BULL",
            "confirm": "BEARISH",
            "regime": "TREND",
            "pivot_present": True,
            "stand_down": False,
            "technical_state": "FRESH",
        }
        assert build_options_framework(meta) is None
