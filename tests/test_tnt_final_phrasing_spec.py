import re

import pytest

from delivery.tnt_final_phrasing import (
    build_candidate_reply,
    build_futures_overview,
    build_greeting,
    build_market_context,
    build_market_snapshot_missing,
    build_symbol_context,
)


_BANNED = re.compile(r"\b(signal|call|entry|prediction|guaranteed|will)\b", re.IGNORECASE)


def _assert_spec(text: str) -> None:
    assert isinstance(text, str) and text.strip()
    assert "ctx:" not in text.lower()
    assert not _BANNED.search(text)
    assert "unknown" not in text.lower()
    assert "Triggers:" in text


def test_market_snapshot_missing_template_is_safe() -> None:
    out = build_market_snapshot_missing()
    _assert_spec(out)
    assert out.splitlines()[0].startswith("Context:")
    assert "Confidence:" in out


def test_futures_overview_ok_has_required_lines() -> None:
    mkt = {
        "ts_utc": 1700000000,
        "sources_present": {"futures": True, "news": True},
        "futures": {
            "bias": "BULLISH",
            "regime": "RANGE",
            "hb_age_sec": 10,
            "updated_utc": 1700000000,
            "degraded": False,
        },
    }
    out = build_futures_overview(mkt=mkt)
    _assert_spec(out)
    assert out.splitlines()[0] == "Context: based on current market snapshot…"
    assert "Regime:" in out
    assert "Bottom line:" in out
    assert "Confidence:" in out


def test_futures_overview_stale_has_bottom_line() -> None:
    mkt = {
        "ts_utc": 1700000000,
        "sources_present": {"futures": True, "news": True},
        "futures": {
            "bias": "BULLISH",
            "regime": "RANGE",
            "hb_age_sec": 999,
            "updated_utc": 1699990000,
            "degraded": True,
        },
    }
    out = build_futures_overview(mkt=mkt)
    _assert_spec(out)
    assert "Bottom line:" in out
    assert "Confidence:" in out


def test_market_context_template_is_safe() -> None:
    mkt = {"ts_utc": 1700000000, "sources_present": {"futures": True, "news": False}}
    tnt_state = {
        "posture": {"regime": "RANGE", "bias": "NEUTRAL", "conviction": "LOW"},
        "meta": {"confirm": "UNKNOWN"},
        "permissions": {"no_trade": True},
        "crowding_risk": {"risk": "LOW"},
        "narrative_risk": {"risk": "LOW"},
        "structure_stress": {"risk": "LOW"},
    }
    out = build_market_context(mkt=mkt, tnt_state=tnt_state)
    _assert_spec(out)
    assert out.splitlines()[0] == "Context: based on current market snapshot…"
    assert "Market Context" in out
    assert "Regime:" in out
    assert "Confidence:" in out


def test_greeting_template_is_safe() -> None:
    mkt = {"ts_utc": 1700000000, "sources_present": {"futures": True, "news": False}}
    out = build_greeting(mkt=mkt)
    _assert_spec(out)
    assert out.splitlines()[0].startswith("Context:")
    assert "Bottom line:" in out
    assert "Confidence:" in out


def test_symbol_context_missing_symbol_snapshot_does_not_leak_ctx() -> None:
    mkt = {"ts_utc": 1700000000, "sources_present": {"futures": True, "news": True}}
    out = build_symbol_context(symbol="SPY", mkt=mkt, sym_ctx=None, tnt_state={})
    _assert_spec(out)
    assert "SPY" in out
    assert "snapshot" in out.lower()


def test_candidate_reply_approved_includes_candidate_not_command() -> None:
    sym_ctx = {
        "ts_utc": 1700000000,
        "candidates": {
            "status": "APPROVED",
            "as_of_et": "2026-01-20 09:45 ET",
            "age_s": 12,
            "confidence": "MED",
            "top": {"strategy": "iron_condor", "direction": "NEUTRAL"},
        },
    }
    out = build_candidate_reply(symbol="SPY", sym_ctx=sym_ctx)
    _assert_spec(out)
    assert "this is a candidate, not a command." in out.lower()
    assert "Confidence:" in out


def test_candidate_reply_missing_candidates_is_stale_not_none() -> None:
    out = build_candidate_reply(symbol="SPY", sym_ctx={})
    _assert_spec(out)
    assert "unavailable" in out.lower()
    assert "Confidence:" in out

