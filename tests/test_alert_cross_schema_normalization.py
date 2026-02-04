from __future__ import annotations

from tnt_alerts.llm_compiler.validate import validate_envelope


def _base_env(intent: dict) -> dict:
    return {
        "ok": True,
        "intent": intent,
        "warnings": [],
        "clarify": None,
    }


def test_cross_rhs_bare_number_coerces_to_break_number_level() -> None:
    env = _base_env(
        {
            "version": "1.0",
            "source": {
                "user_id": "1",
                "channel_id": "1",
                "request_text": "spy crosses above 689",
                "created_at_utc": "2026-01-22T14:55:17.538936+00:00",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "direction": "AUTO",
            "condition": {
                "type": "cross",
                "op": "crosses_above",
                "left": {"type": "price", "field": "last"},
                # Key variant: LLM emits a bare number instead of a level/series object.
                "right": 689.0,
                "timeframe": "5m",
                "confirm": "close",
            },
            "gates": {"cooldown": {"seconds": 300}},
            "lifecycle": {"start": "now", "expires": {"type": "relative", "hours": 8}},
            "actions": [{"type": "discord_notify", "style": "compact"}],
            "tags": {},
        }
    )

    out = validate_envelope(env)
    cond = out["intent"]["condition"]

    assert cond["type"] == "break"
    assert cond["op"] == "breaks_above"
    assert cond["level"]["type"] == "number"
    assert float(cond["level"]["value"]) == 689.0


def test_cross_rhs_level_wrapper_kind_number_normalizes_to_break_number_level() -> None:
    env = _base_env(
        {
            "version": "1.0",
            "source": {
                "user_id": "1",
                "channel_id": "1",
                "request_text": "spy crosses above 689",
                "created_at_utc": "2026-01-22T14:55:17.538936+00:00",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "direction": "AUTO",
            "condition": {
                "type": "cross",
                "op": "crosses_above",
                "left": {"type": "price", "field": "last"},
                "right": {"type": "level", "kind": "number", "value": "689"},
                "timeframe": "5m",
                "confirm": "close",
            },
            "gates": {"cooldown": {"seconds": 300}},
            "lifecycle": {"start": "now", "expires": {"type": "relative", "hours": 8}},
            "actions": [{"type": "discord_notify", "style": "compact"}],
            "tags": {},
        }
    )

    out = validate_envelope(env)
    cond = out["intent"]["condition"]

    assert cond["type"] == "break"
    assert cond["op"] == "breaks_above"
    assert cond["level"]["type"] == "number"
    assert float(cond["level"]["value"]) == 689.0


def test_break_op_crosses_above_is_mapped_to_breaks_above_and_defaults_tf_confirm() -> None:
    env = _base_env(
        {
            "version": "1.0",
            "source": {
                "user_id": "1",
                "channel_id": "1",
                "request_text": "spy crosses above 689",
                "created_at_utc": "2026-01-22T14:55:17.538936+00:00",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "direction": "AUTO",
            "condition": {
                "type": "break",
                "op": "crosses_above",
                "level": 689,
                # omit timeframe/confirm to ensure we default to schema-safe values
            },
            "gates": {"cooldown": {"seconds": 300}},
            "lifecycle": {"start": "now", "expires": {"type": "relative", "hours": 8}},
            "actions": [{"type": "discord_notify", "style": "compact"}],
            "tags": {},
        }
    )

    out = validate_envelope(env)
    cond = out["intent"]["condition"]
    assert cond["type"] == "break"
    assert cond["op"] == "breaks_above"
    assert cond["timeframe"] == "5m"
    assert cond["confirm"] == "close"
