from __future__ import annotations


from delivery.tnt_llm import call_tnt_agent


def _sample_tnt_state(symbol: str = "SPY") -> dict:
    return {
        "meta": {
            "schema_version": "1.0",
            "generated_at_et": "2025-01-01T09:30:00-05:00",
            "data_freshness_sec": None,
            "data_health": "DOWN",
            "data_integrity": {"ok": False, "reason": "TEST"},
            "source": {"provider": "TEST", "mode": "TEST"},
        },
        "context": {
            "symbol": symbol,
            "asset_class": "EQUITY",
            "session": "UNKNOWN",
            "timeframes": {"execution": "5m", "structure": "60m", "context": "1D"},
        },
        "price": {"last": None},
        "posture": {
            "bias": "NEUTRAL",
            "conviction": "LOW",
            "regime": "COMPRESSION",
            "mode": "NORMAL",
            "rationale_tags": [],
        },
        "levels": {
            "decision_zones": [],
            "support": [],
            "resistance": [],
            "pivots_rth": {"P": None, "R1": None, "S1": None, "R2": None, "S2": None},
        },
        "permissions": {
            "aggression": "PROHIBITED",
            "momentum_only": False,
            "no_trade": True,
            "reasons": ["DATA_HEALTH"],
        },
        "events": [],
        "chart": {"recommended_template": "BALANCED", "template_params": None},
    }


def test_tnt_llm_locked_model_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("TNT_LLM_MODEL", "gpt-5.2")

    captured: dict = {}

    class _FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            resp = type("_Resp", (), {})()
            resp.output_text = "OK"
            resp.model = kwargs.get("model")
            return resp

    class _FakeClient:
        def __init__(self):
            self.responses = _FakeResponses()

    monkeypatch.setattr("delivery.tnt_llm._get_openai_client", lambda: _FakeClient())

    result = call_tnt_agent(tnt_state=_sample_tnt_state(), user_text="Say OK", label="test")

    assert result.text == "OK"
    assert captured.get("model") == "gpt-5.2"
    assert result.model == "gpt-5.2"


def test_tnt_llm_locked_model_rejects_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("TNT_LLM_MODEL", "gpt-5.2")

    class _FakeResponses:
        def create(self, **kwargs):
            resp = type("_Resp", (), {})()
            resp.output_text = "OK"
            resp.model = kwargs.get("model")
            return resp

    class _FakeClient:
        def __init__(self):
            self.responses = _FakeResponses()

    monkeypatch.setattr("delivery.tnt_llm._get_openai_client", lambda: _FakeClient())

    try:
        call_tnt_agent(
            tnt_state=_sample_tnt_state(),
            user_text="Say OK",
            label="test",
            model="gpt-5.1-pro",
        )
    except AssertionError as e:
        assert "TNT model drift detected" in str(e)
    else:
        raise AssertionError("Expected model drift assertion")
