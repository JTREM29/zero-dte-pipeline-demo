from delivery.tnt_state import build_tnt_state_from_analysis_payload


def _payload(*, health_ok: bool = True):
    # Minimal analysis payload stub with required meta.price_context and meta.signal_payload
    return {
        "symbol": "SPY",
        "meta": {
            "price_context": {
                "last_price": 100.0,
                "last_price_ts": "2025-12-26T15:59:00+00:00" if health_ok else "2025-12-26T12:00:00+00:00",
                "source": "test",
                "mode": "test",
            },
            "signal_payload": {
                "session": "RTH",
                "tf_exec": "5m",
                "tf_struct": "60m",
                "tf_ctx": "1D",
                "pivot": 99.0,
                "r1": 101.0,
                "s1": 98.0,
                "r2": 102.0,
                "s2": 97.0,
                "bias": "NEUTRAL",
                "conviction": "LOW",
                "pivot_regime": "COMPRESSION",
            },
            "macro_risk": [{"title": "FOMC", "time_et": "14:00", "delta_min": 0}],
        },
    }


def test_enrichment_fields_present():
    state = build_tnt_state_from_analysis_payload(_payload()).state
    assert "tvds" in state
    assert "structure_stress" in state
    assert "narrative_risk" in state
    assert "crowding_risk" in state
    assert "do_nothing_alert" in state


def test_tvds_is_bounded_and_labeled():
    state = build_tnt_state_from_analysis_payload(_payload()).state
    tvds = state["tvds"]
    assert 0.0 <= float(tvds["score"]) <= 100.0
    assert tvds["label"] in {"GREEN", "YELLOW", "RED"}


def test_do_nothing_alert_triggers_on_low_conviction_compression_and_event_window():
    state = build_tnt_state_from_analysis_payload(_payload()).state
    alert = state["do_nothing_alert"]
    assert isinstance(alert.get("enabled"), bool)
    assert alert["enabled"] is True
    assert "STRUCTURE_HIGH" in alert.get("reasons", []) or "NARRATIVE" in alert.get("reasons", []) or "TVDS_RED" in alert.get("reasons", [])
