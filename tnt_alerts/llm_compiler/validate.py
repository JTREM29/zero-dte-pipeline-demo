from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from referencing import Registry
from referencing.jsonschema import DRAFT7

from tnt_alerts.alert_intent import AlertIntent


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"


class CompilerValidationError(ValueError):
    pass


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CompilerValidationError(f"Failed to load schema {path.name}: {type(exc).__name__}: {exc}")


def _build_registry(*, envelope_schema: dict[str, Any], intent_schema: dict[str, Any]) -> tuple[dict[str, Any], Draft7Validator]:
    envelope_uri = (SCHEMAS / "compiler_envelope_v1.schema.json").resolve().as_uri()
    intent_uri = (SCHEMAS / "alert_intent_v1.schema.json").resolve().as_uri()

    envelope_rt = dict(envelope_schema)
    envelope_rt.setdefault("$id", envelope_uri)
    intent_rt = dict(intent_schema)
    intent_rt.setdefault("$id", intent_uri)

    registry = (
        Registry()
        .with_resource(envelope_uri, DRAFT7.create_resource(envelope_rt))
        .with_resource(intent_uri, DRAFT7.create_resource(intent_rt))
    )

    return envelope_rt, Draft7Validator(envelope_rt, registry=registry)


_ENVELOPE_SCHEMA = _load_schema(SCHEMAS / "compiler_envelope_v1.schema.json")
_INTENT_SCHEMA = _load_schema(SCHEMAS / "alert_intent_v1.schema.json")
_ENVELOPE_SCHEMA_RT, _ENVELOPE_VALIDATOR = _build_registry(
    envelope_schema=_ENVELOPE_SCHEMA,
    intent_schema=_INTENT_SCHEMA,
)


def validate_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    errors = sorted(_ENVELOPE_VALIDATOR.iter_errors(envelope), key=lambda e: str(e.path))
    if errors:
        e0 = errors[0]
        path = ".".join([str(p) for p in list(e0.path)])
        raise CompilerValidationError(f"Envelope schema validation failed at {path or '<root>'}: {e0.message}")

    ok = bool(envelope.get("ok"))
    if ok:
        if envelope.get("intent") is None:
            raise CompilerValidationError("Contract violation: ok=true requires intent")
        if envelope.get("clarify") is not None:
            raise CompilerValidationError("Contract violation: ok=true requires clarify=null")
    else:
        if envelope.get("intent") is not None:
            raise CompilerValidationError("Contract violation: ok=false requires intent=null")
        if envelope.get("clarify") is None:
            raise CompilerValidationError("Contract violation: ok=false requires clarify")

    return envelope


def validate_intent(intent: dict[str, Any]) -> AlertIntent:
    # We intentionally validate via Pydantic here; schema-level intent validation is handled
    # indirectly through the envelope $ref.
    try:
        return AlertIntent.model_validate(intent)
    except Exception as exc:  # noqa: BLE001
        raise CompilerValidationError(f"Intent validation failed: {type(exc).__name__}: {exc}")
