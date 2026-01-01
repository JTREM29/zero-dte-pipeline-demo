from __future__ import annotations

import json
import re

from delivery.state_builder import build_tnt_state


def test_state_builder_includes_coverage_and_safe_buckets() -> None:
    state = build_tnt_state(
        ["SPY"],
        mode="REALTIME",
        signal_override={"prob_up": 0.52, "edge": 0.02, "regime": "TREND"},
        options_focus_lines=[
            "• Gamma: NORMAL",
            "• Theta: HIGH",
            "• Vol Regime: ELEVATED",
        ],
    )

    assert state["meta"]["schema_version"] == "1.0"
    assert "coverage" in state["meta"]

    cov = state["meta"]["coverage"]
    assert isinstance(cov, dict)
    for key in ("price", "pivots", "signal", "options_environment", "indices_advanced"):
        assert key in cov
        assert isinstance(cov[key], bool)

    assert "indices" in state
    assert state["indices"]["vix_trend"] in {"UP", "DOWN", "FLAT", "UNKNOWN"}
    assert state["indices"]["spx_context"] in {
        "ABOVE_R1",
        "ABOVE_P",
        "NEAR_P",
        "BELOW_P",
        "BELOW_S1",
        "UNKNOWN",
    }

    opt = state["options_environment"]
    assert opt["gamma"] in {"LOW", "NORMAL", "ELEVATED", "HIGH", "EXTREME", "UNKNOWN"}
    assert opt["theta"] in {"LOW", "NORMAL", "ELEVATED", "HIGH", "EXTREME", "UNKNOWN"}
    assert opt["vol_regime"] in {"LOW", "NORMAL", "ELEVATED", "HIGH", "EXTREME", "UNKNOWN"}

    # New: compact per-symbol data freshness hints.
    assert "data_freshness" in state
    df = state["data_freshness"]
    assert isinstance(df, dict)
    for key in ("primary", "spx", "vix"):
        assert key in df
        assert isinstance(df[key], dict)
        assert "symbol" in df[key]
        assert "source" in df[key]
        assert isinstance(df[key]["source"], dict)


def test_state_builder_contains_no_option_contract_language() -> None:
    state = build_tnt_state(
        ["SPY"],
        mode="REALTIME",
        signal_override={"prob_up": 0.52, "edge": 0.02, "regime": "TREND"},
        options_focus_lines=[
            "• Gamma: NORMAL",
            "• Theta: NORMAL",
            "• Vol Regime: NORMAL",
        ],
    )

    blob = json.dumps(state, ensure_ascii=False).lower()

    # Common contract patterns: strikes like 4800c/4800p, explicit call/put, strike/expiry.
    forbidden_substrings = [" call", " put", "strike", "expiry", "expiration", "0dte", "dte"]
    for s in forbidden_substrings:
        assert s not in blob

    assert re.search(r"\b\d{3,6}[cp]\b", blob) is None


def test_state_builder_integrity_conflict_forces_no_trade() -> None:
    class FakeAdapter:
        @staticmethod
        def get_options_environment(*, options_focus_lines=None, now=None):
            return {"asof_et": "x", "gamma": "NORMAL", "theta": "NORMAL", "vol_regime": "NORMAL", "source": {"provider": "t"}}

        @staticmethod
        def get_index_snapshot(symbol: str, *, now=None):
            return {"symbol": symbol, "last": 20.0, "ts_iso": "2025-12-24T14:41:12+00:00", "staleness_s": 0.0, "source": {"provider": "t"}}

        @staticmethod
        def get_equity_snapshot(symbol: str, *, now=None):
            # Price and pivots wildly disagree in scale.
            return {"symbol": symbol, "last": 679.0, "ts_iso": "2025-12-24T14:41:12+00:00", "staleness_s": 0.0, "source": {"provider": "t", "detail": "x"}}

        @staticmethod
        def get_pivots(symbol: str):
            return {"ts": "2025-12-24T14:00:00+00:00", "piv": {"P": 4832.0, "R1": 4845.0, "S1": 4818.0, "R2": None, "S2": None}}

    state = build_tnt_state(
        ["SPY"],
        mode="REALTIME",
        adapter=FakeAdapter,
        signal_override={"prob_up": 0.52, "edge": 0.02, "regime": "TREND"},
    )

    assert state["meta"]["data_health"] == "DEGRADED"
    assert state["meta"]["data_integrity"]["ok"] is False
    assert state["permissions"]["no_trade"] is True
    assert state["permissions"]["aggression"] == "PROHIBITED"
