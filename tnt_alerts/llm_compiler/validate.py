from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft7Validator
from referencing import Registry
from referencing.jsonschema import DRAFT7

from tnt_alerts.alert_intent import AlertIntent


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"


class CompilerValidationError(ValueError):
    pass


def _format_jsonschema_context(e: object) -> str:
    """Format jsonschema.ValidationError (or iter_errors error) context.

    `jsonschema` uses `.context` to attach sub-errors for combinators like
    oneOf/anyOf. Surfacing those details is the fastest way to locate the
    true offending field.
    """

    lines: list[str] = []
    ctx = getattr(e, "context", None)
    if not isinstance(ctx, list) or not ctx:
        return ""

    for i, sub in enumerate(ctx[:12]):
        try:
            path = ".".join([str(p) for p in list(getattr(sub, "absolute_path", []) or [])])
        except Exception:
            path = ""
        try:
            msg = str(getattr(sub, "message", "") or "")
        except Exception:
            msg = ""
        if not msg:
            try:
                msg = str(sub)
            except Exception:
                msg = ""
        lines.append(f"  [{i}] path={path or '<root>'} msg={msg}")

    return "\n".join(lines)


def _as_float(v: object) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _looks_like_level(node: object) -> bool:
    # Some LLM variants emit a bare numeric/string for a level.
    if isinstance(node, (int, float, str)):
        return _as_float(node) is not None
    if not isinstance(node, Mapping):
        return False
    t = str(node.get("type") or "").strip().lower()
    if t in {"number", "pivot", "ref", "level", "level_ref"}:
        return True
    # Heuristic: if it carries a numeric value, treat as potential level.
    if "value" in node and _as_float(node.get("value")) is not None:
        return True
    return False


def _normalize_level(node: object) -> dict[str, Any] | None:
    """Coerce a variety of level shapes into the canonical schema Level.

    Canonical shapes (AlertIntent schema):
      - {"type":"number","value":float}
      - {"type":"pivot","pivot":"P"|"R1"|...}
      - {"type":"ref","ref":"y_high"|"or_high"|..., "params":{...}}
    """

    # Some LLM variants emit a bare numeric/string for a level.
    if isinstance(node, (int, float, str)):
        v = _as_float(node)
        if v is None:
            return None
        return {"type": "number", "value": float(v)}

    if not isinstance(node, Mapping):
        return None
    d = dict(node)
    t_raw = str(d.get("type") or "").strip()
    t = t_raw.lower()

    # Legacy/LLM variants
    if t == "level":
        kind = str(d.get("kind") or "").strip().lower()
        if kind in {"number", "pivot", "ref"}:
            t = kind
        else:
            # If kind is missing but it has value, treat as number.
            if _as_float(d.get("value")) is not None:
                t = "number"

    if t == "level_ref":
        # Some older code used {type:"level_ref", name:"y_high"}.
        t = "ref"

    if t == "number":
        v = _as_float(d.get("value"))
        if v is None:
            return None
        return {"type": "number", "value": float(v)}

    if t == "pivot":
        piv = d.get("pivot")
        if piv is None:
            piv = d.get("name")
        piv_s = str(piv or "").strip().upper()
        if not piv_s:
            return None
        return {"type": "pivot", "pivot": piv_s}

    if t == "ref":
        ref = d.get("ref")
        if ref is None:
            ref = d.get("name")
        ref_s = str(ref or "").strip().lower()
        if not ref_s:
            return None
        params = d.get("params")
        if not isinstance(params, dict):
            params = {}
        # Carry over minutes if present at top-level (legacy).
        if "minutes" in d and "minutes" not in params:
            try:
                params = dict(params)
                params["minutes"] = int(d.get("minutes"))
            except Exception:
                pass
        return {"type": "ref", "ref": ref_s, "params": params}

    return None


def _normalize_condition(cond: object) -> dict[str, Any] | None:
    if not isinstance(cond, Mapping):
        return None
    d: dict[str, Any] = dict(cond)
    ctype = str(d.get("type") or "").strip().lower()

    # Schema requires timeframe+confirm on all condition variants.
    allowed_timeframes = {"1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "1D"}
    allowed_confirms = {"close", "intrabar"}

    def _ensure_tf_confirm(node: dict[str, Any]) -> dict[str, Any]:
        tf = node.get("timeframe")
        if not isinstance(tf, str) or tf not in allowed_timeframes:
            node["timeframe"] = "5m"
        cf = node.get("confirm")
        if not isinstance(cf, str) or cf not in allowed_confirms:
            node["confirm"] = "close"
        return node

    # touch: normalize right Level
    if ctype == "touch":
        right = d.get("right")
        if _looks_like_level(right):
            nl = _normalize_level(right)
            if nl is not None:
                d["right"] = nl
        return d

    # break: some LLMs emit right instead of level
    if ctype == "break":
        # Naming drift: users say "crosses above" but schema break uses breaks_above/below.
        op = str(d.get("op") or "").strip().lower()
        if op == "crosses_above":
            d["op"] = "breaks_above"
        elif op == "crosses_below":
            d["op"] = "breaks_below"
        level = d.get("level")
        if level is None and d.get("right") is not None:
            level = d.get("right")
            d.pop("right", None)
        if _looks_like_level(level):
            nl = _normalize_level(level)
            if nl is not None:
                d["level"] = nl
        return _ensure_tf_confirm(d)

    # cross: occasionally emitted with a numeric level on the right.
    # Coerce into break condition, which is the intended semantics for crossing a level.
    if ctype == "cross":
        right = d.get("right")
        if _looks_like_level(right):
            nl = _normalize_level(right)
            if nl is not None:
                op = str(d.get("op") or "").strip().lower()
                if op == "crosses_below":
                    bop = "breaks_below"
                else:
                    bop = "breaks_above"
                out: dict[str, Any] = {
                    "type": "break",
                    "op": bop,
                    "level": nl,
                    "timeframe": d.get("timeframe"),
                    "confirm": d.get("confirm"),
                }
                return _ensure_tf_confirm(out)
        return _ensure_tf_confirm(d)

    return _ensure_tf_confirm(d)


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
    # Normalize into a fresh dict so callers can't accidentally validate one object
    # while continuing to use a different/stale reference.
    if not isinstance(envelope, Mapping):
        raise CompilerValidationError(f"Envelope must be an object/dict, got {type(envelope).__name__}")

    env: dict[str, Any] = dict(envelope)

    # Some LLM outputs emit warnings as strings. The schema requires warnings to be
    # objects ({code, note}). Normalize here so the *authoritative* schema gate
    # cannot fail on warnings shape, regardless of call path.
    try:
        w = env.get("warnings")
        out: list[dict[str, Any]] = []
        if isinstance(w, list):
            for item in w:
                if isinstance(item, str):
                    s = item.strip()
                    if s:
                        out.append({"code": "WARN", "note": s})
                elif isinstance(item, dict):
                    code = item.get("code") or "WARN"
                    note = item.get("note")
                    if note is None:
                        note = item.get("message")
                    out.append({"code": str(code), "note": note})
                else:
                    out.append({"code": "WARN", "note": str(item)})
        env["warnings"] = out
    except Exception:
        # If warning normalization fails, keep schema strict and let validation surface.
        pass

    # Normalize legacy/variant condition/level shapes emitted by the LLM so schema
    # validation is deterministic and stable.
    try:
        intent = env.get("intent")
        if isinstance(intent, dict):
            cond = intent.get("condition")
            nc = _normalize_condition(cond)
            if nc is not None:
                intent2 = dict(intent)
                intent2["condition"] = nc
                env["intent"] = intent2
    except Exception:
        # Keep best-effort: schema validation will surface if still invalid.
        pass

    errors = sorted(_ENVELOPE_VALIDATOR.iter_errors(env), key=lambda e: str(e.path))
    if errors:
        e0 = errors[0]
        path = ".".join([str(p) for p in list(e0.path)])
        details = _format_jsonschema_context(e0)
        suffix = f"\n{details}" if details else ""
        raise CompilerValidationError(f"Envelope schema validation failed at {path or '<root>'}: {e0.message}{suffix}")

    ok = bool(env.get("ok"))
    if ok:
        if env.get("intent") is None:
            raise CompilerValidationError("Contract violation: ok=true requires intent")
        if env.get("clarify") is not None:
            raise CompilerValidationError("Contract violation: ok=true requires clarify=null")
    else:
        if env.get("intent") is not None:
            raise CompilerValidationError("Contract violation: ok=false requires intent=null")
        if env.get("clarify") is None:
            raise CompilerValidationError("Contract violation: ok=false requires clarify")

    return env


def validate_intent(intent: dict[str, Any]) -> AlertIntent:
    # We intentionally validate via Pydantic here; schema-level intent validation is handled
    # indirectly through the envelope $ref.

    # Normalize market-hours semantics:
    # If a time_window_et is provided, treat it as CUSTOM (even if session
    # was left as RTH by an LLM or older code). This prevents "Rule shows
    # 20:00-23:59 ET" while runtime gating still enforces RTH.
    try:
        if isinstance(intent, dict):
            gates = intent.get("gates")
            if isinstance(gates, dict):
                mh = gates.get("market_hours")
                if isinstance(mh, dict):
                    win = mh.get("time_window_et")
                    if isinstance(win, (list, tuple)) and len(win) == 2:
                        mh2 = dict(mh)
                        mh2["time_window_et"] = [str(win[0]), str(win[1])]
                        mh2["session"] = "CUSTOM"
                        gates2 = dict(gates)
                        gates2["market_hours"] = mh2
                        intent = dict(intent)
                        intent["gates"] = gates2
                # RTH-default: if market_hours is omitted entirely, enforce RTH.
                if isinstance(gates, dict) and "market_hours" not in gates:
                    gates2 = dict(gates)
                    gates2["market_hours"] = {"session": "RTH", "time_window_et": None}
                    intent = dict(intent)
                    intent["gates"] = gates2
            elif gates is None:
                # If gates is missing/null, create it with RTH market-hours default.
                intent = dict(intent)
                intent["gates"] = {"market_hours": {"session": "RTH", "time_window_et": None}}
    except Exception:
        pass

    try:
        return AlertIntent.model_validate(intent)
    except Exception as exc:  # noqa: BLE001
        raise CompilerValidationError(f"Intent validation failed: {type(exc).__name__}: {exc}")
