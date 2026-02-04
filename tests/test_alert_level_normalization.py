import pytest

from tnt_alerts.llm_compiler.validate import validate_envelope


def test_crosses_above_numeric_level_triggers_on_replay() -> None:
    from datetime import datetime, timezone

    from tnt_alerts.eval_engine import Bar, GateContext, MarketDataSnapshot, evaluate_alert
    from tnt_alerts.llm_compiler.validate import validate_intent

    env = _base_env(
        {
            "version": "1.0",
            "source": {
                "user_id": "1",
                "channel_id": "1",
                "request_text": "SPY crosses above 389",
                "created_at_utc": "2026-01-22T00:00:00Z",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "direction": "AUTO",
            # Intentionally use the problematic shape: cross + numeric right
            "condition": {
                "type": "cross",
                "op": "crosses_above",
                "timeframe": "5m",
                "confirm": "close",
                "left": {"type": "price", "field": "last"},
                "right": {"type": "level", "kind": "number", "value": 389.0},
            },
            "gates": {},
            "lifecycle": {"start": "now", "expires": {"type": "relative", "hours": 8}},
            "actions": [{"type": "discord_notify", "style": "compact"}],
            "tags": {},
        }
    )

    out = validate_envelope(env)
    intent = validate_intent(out["intent"])

    now = datetime(2026, 1, 22, 15, 30, tzinfo=timezone.utc)
    bars = [
        Bar(ts_utc=now, o=388.7, h=389.0, l=388.6, c=388.9, v=1_000_000),
        Bar(ts_utc=now, o=388.9, h=389.3, l=388.8, c=389.1, v=1_200_000),
    ]
    snap = MarketDataSnapshot(
        symbol="SPY",
        timeframe="5m",
        bars=bars,
        last_price=389.1,
        price_ts_utc=now,
    )

    gctx = GateContext(now_utc=now)
    state: dict = {}
    event = evaluate_alert(intent, snap, context={}, gctx=gctx, state=state)
    assert event["decision"] == "TRIGGERED"


def _base_env(intent: dict) -> dict:
    return {
        "ok": True,
        "warnings": [],
        "clarify": None,
        "intent": intent,
    }


def test_validate_envelope_normalizes_level_kind_number_to_number() -> None:
    env = _base_env(
        {
            "version": "1.0",
            "source": {
                "user_id": "1",
                "channel_id": "1",
                "request_text": "SPY crosses above 389",
                "created_at_utc": "2026-01-22T00:00:00Z",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "direction": "AUTO",
            "condition": {
                "type": "touch",
                "timeframe": "5m",
                "confirm": "intrabar",
                "left": {"type": "price", "field": "last"},
                "right": {"type": "level", "kind": "number", "value": 389.0},
            },
            "gates": {},
            "lifecycle": {"start": "now", "expires": {"type": "relative", "hours": 8}},
            "actions": [{"type": "discord_notify", "style": "compact"}],
            "tags": {},
        }
    )

    out = validate_envelope(env)
    assert out["intent"]["condition"]["right"]["type"] == "number"
    assert float(out["intent"]["condition"]["right"]["value"]) == 389.0


def test_validate_envelope_coerces_cross_with_level_right_into_break() -> None:
    env = _base_env(
        {
            "version": "1.0",
            "source": {
                "user_id": "1",
                "channel_id": "1",
                "request_text": "SPY crosses above 389",
                "created_at_utc": "2026-01-22T00:00:00Z",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "direction": "AUTO",
            "condition": {
                "type": "cross",
                "op": "crosses_above",
                "timeframe": "5m",
                "confirm": "close",
                "left": {"type": "price", "field": "last"},
                "right": {"type": "number", "value": 389.0},
            },
            "gates": {},
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
    assert float(cond["level"]["value"]) == 389.0


def test_validate_envelope_rejects_unfixable_level_shapes() -> None:
    env = _base_env(
        {
            "version": "1.0",
            "source": {
                "user_id": "1",
                "channel_id": "1",
                "request_text": "SPY crosses above ???",
                "created_at_utc": "2026-01-22T00:00:00Z",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "direction": "AUTO",
            "condition": {
                "type": "touch",
                "timeframe": "5m",
                "confirm": "intrabar",
                "left": {"type": "price", "field": "last"},
                "right": {"type": "level", "kind": "number", "value": "nope"},
            },
            "gates": {},
            "lifecycle": {"start": "now", "expires": {"type": "relative", "hours": 8}},
            "actions": [{"type": "discord_notify", "style": "compact"}],
            "tags": {},
        }
    )

    with pytest.raises(Exception):
        validate_envelope(env)
