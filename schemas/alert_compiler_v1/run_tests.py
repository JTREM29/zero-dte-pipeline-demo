import json
from pathlib import Path

from jsonschema import Draft7Validator
from referencing import Registry
from referencing.jsonschema import DRAFT7


ROOT = Path(__file__).resolve().parent

with open(ROOT / "compiler_envelope_v1.schema.json", encoding="utf-8") as f:
    envelope_schema = json.load(f)

with open(ROOT / "alert_intent_v1.schema.json", encoding="utf-8") as f:
    intent_schema = json.load(f)

with open(ROOT / "compiler_test_cases.v1.json", encoding="utf-8") as f:
    tests = json.load(f)["cases"]


def run() -> None:
    # 1) Ensure fixtures load cleanly
    for t in tests:
        print(f"✓ Loaded test: {t['name']}")

    # 2) Validate example envelopes (schema-level)
    # Resolve local refs like "alert_intent_v1.schema.json".
    # jsonschema's old RefResolver is deprecated; use referencing.Registry.
    envelope_uri = (ROOT / "compiler_envelope_v1.schema.json").resolve().as_uri()
    intent_uri = (ROOT / "alert_intent_v1.schema.json").resolve().as_uri()

    # The provided schemas don't include $id, but relative refs need a base URI.
    # Add $id at runtime (do not mutate the on-disk JSON files).
    envelope_schema_rt = dict(envelope_schema)
    envelope_schema_rt.setdefault("$id", envelope_uri)
    intent_schema_rt = dict(intent_schema)
    intent_schema_rt.setdefault("$id", intent_uri)

    registry = (
        Registry()
        .with_resource(envelope_uri, DRAFT7.create_resource(envelope_schema_rt))
        .with_resource(intent_uri, DRAFT7.create_resource(intent_schema_rt))
    )

    validator = Draft7Validator(envelope_schema_rt, registry=registry)

    ok_envelope = {
        "ok": True,
        "intent": {
            "version": "1.0",
            "source": {
                "user_id": "discord:123",
                "channel_id": "discord:456",
                "request_text": "Notify me when SPY crosses above VWAP on 5m",
                "created_at_utc": "2026-01-16T00:00:00Z",
            },
            "targets": {"type": "symbols", "symbols": ["SPY"], "watchlist": None, "max_symbols": 20},
            "condition": {
                "type": "cross",
                "op": "crosses_above",
                "left": {"type": "price", "field": "last"},
                "right": {"type": "indicator", "name": "vwap", "params": {}},
                "timeframe": "5m",
                "confirm": "close",
            },
            "lifecycle": {"start": "now", "expires": {"type": "relative", "days": 3}},
            "actions": [{"type": "discord_notify", "style": "compact"}],
            "tags": {},
        },
        "warnings": [],
        "clarify": None,
    }

    clarify_envelope = {
        "ok": False,
        "intent": None,
        "warnings": [{"code": "PARSE_AMBIGUOUS", "note": "Direction not specified"}],
        "clarify": {
            "question": "Do you mean breaks ABOVE VWAP or BELOW VWAP?",
            "choices": ["ABOVE", "BELOW"],
            "default": "ABOVE",
        },
    }

    # Validate using the schema. Any errors will raise.
    validator.validate(ok_envelope)
    print("✓ Schema validated example: ok=true")

    validator.validate(clarify_envelope)
    print("✓ Schema validated example: ok=false")

    # 3) Contract invariants (beyond schema)
    if ok_envelope["ok"] is True and ok_envelope["intent"] is None:
        raise AssertionError("Contract violation: ok=true requires intent")
    if ok_envelope["ok"] is True and ok_envelope["clarify"] is not None:
        raise AssertionError("Contract violation: ok=true requires clarify=null")
    if clarify_envelope["ok"] is False and clarify_envelope["intent"] is not None:
        raise AssertionError("Contract violation: ok=false requires intent=null")
    if clarify_envelope["ok"] is False and clarify_envelope["clarify"] is None:
        raise AssertionError("Contract violation: ok=false requires clarify")
    print("✓ Contract invariants OK")

    print("\nSchema + test fixtures + example envelopes validated successfully.")


if __name__ == "__main__":
    run()
