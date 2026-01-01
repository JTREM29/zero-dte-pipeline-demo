"""Tests for the OpenAI client compatibility wrapper."""
from __future__ import annotations

from zero_dte_pipeline.openai_client import OpenAIClient


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


def test_pro_model_available(monkeypatch):
    """Ensure the wrapper forwards model selection and returns text."""

    # Provide deterministic configuration.
    monkeypatch.setenv("OPENAI_MODEL_NAME", "gpt-5.1-pro")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    captured: dict = {}

    def _fake_call_tnt_agent(**kwargs):
        captured.update(kwargs)
        return type(
            "_Res",
            (),
            {"text": "OK", "model": kwargs.get("model"), "prompt_sha256": "p", "tnt_state_sha256": "s"},
        )()

    monkeypatch.setattr("zero_dte_pipeline.openai_client.call_tnt_agent", _fake_call_tnt_agent)

    wrapper = OpenAIClient()
    result = wrapper.generate("test system", "Say OK.", tnt_state=_sample_tnt_state())

    assert result == "OK"
    assert captured
    assert captured["model"] == "gpt-5.1-pro"
