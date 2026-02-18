from __future__ import annotations

from delivery.tnt_chart_contract import build_chart_spec_from_state, validate_chart_spec


def _base_state() -> dict:
    return {
        "meta": {"schema_version": "1.0", "generated_at_et": "2025-01-01T09:35:00-05:00", "data_health": "OK"},
        "context": {"symbol": "SPY", "timeframes": {"execution": "5m"}},
        "posture": {"bias": "NEUTRAL", "regime": "TREND", "conviction": "LOW"},
        "permissions": {"no_trade": False, "aggression": "REDUCED"},
        "levels": {"pivots_rth": {"P": 500.0, "R1": 501.0, "S1": 499.0}, "decision_zones": []},
        "events": [],
    }


def _valid_decision_zone_spec() -> dict:
    return {
        "contract_version": "1.0",
        "template": "DECISION_ZONE",
        "symbol": "SPY",
        "timeframe": "5m",
        "timestamp_et": "2025-01-01T09:35:00-05:00",
        "posture": {"bias": "NEUTRAL", "regime": "TREND", "conviction": "LOW"},
        "permissions": {"aggression": "REDUCED"},
        "footer": "TNT charts are posture & behavior only. No execution guidance.",
        "template_params": {
            "lines": [{"label": "Pivot", "price": 500.0}],
            "zones": [{"label": "Support zone", "low": 499.0, "high": 500.0}],
            "gates": {
                "acceptance_up": {"price": 501.0},
                "breakdown_down": {"price": 499.0},
            },
            "labels": [
                "Bullish posture permitted only above acceptance",
                "Bearish posture permitted only below breakdown",
            ],
        },
    }


def test_template_selection_no_trade() -> None:
    s = _base_state()
    s["permissions"]["no_trade"] = True
    r = build_chart_spec_from_state(s)
    assert r.template == "NO_TRADE_COMPRESSION"
    assert r.spec is not None


def test_template_selection_event_overlay_pre() -> None:
    s = _base_state()
    s["events"] = [
        {
            "name": "CPI",
            "risk": "HIGH",
            "phase": "PRE",
            "window_et": {"start": "2025-01-01T08:30:00-05:00", "end": "2025-01-01T08:30:00-05:00"},
        }
    ]
    r = build_chart_spec_from_state(s)
    assert r.template == "EVENT_RISK_OVERLAY"
    assert r.spec is not None


def test_validate_rejects_forbidden_words_in_labels() -> None:
    bad_spec = _valid_decision_zone_spec()
    bad_spec["template_params"]["lines"] = [{"label": "buy", "price": 500.0}]
    ok, msg = validate_chart_spec(bad_spec)
    assert ok is False


def test_validate_rejects_unknown_label_not_in_vocabulary() -> None:
    bad_spec = _valid_decision_zone_spec()
    bad_spec["template_params"]["lines"] = [{"label": "My Custom Label", "price": 500.0}]
    ok, msg = validate_chart_spec(bad_spec)
    assert ok is False
    assert "label" in msg.lower()


def test_validate_rejects_indicator_terms_like_vwap() -> None:
    bad_spec = _valid_decision_zone_spec()
    bad_spec["template_params"]["labels"] = ["VWAP"]
    ok, msg = validate_chart_spec(bad_spec)
    assert ok is False
    assert "forbidden" in msg.lower() or "label" in msg.lower()


def test_validate_rejects_too_many_lines() -> None:
    bad_spec = _valid_decision_zone_spec()
    bad_spec["template_params"]["lines"] = [
        {"label": "Pivot", "price": 500.0},
        {"label": "Pivot", "price": 500.0},
        {"label": "Pivot", "price": 500.0},
        {"label": "Pivot", "price": 500.0},
        {"label": "Pivot", "price": 500.0},
        {"label": "Pivot", "price": 500.0},
    ]
    ok, msg = validate_chart_spec(bad_spec)
    assert ok is False
    assert "lines" in msg.lower()
