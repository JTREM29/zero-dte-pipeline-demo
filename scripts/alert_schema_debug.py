from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tnt_alerts.llm_compiler.validate import CompilerValidationError, validate_envelope


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_schema_errors(obj: dict[str, Any]) -> list[str]:
    # Import the module internals intentionally for debugging.
    from tnt_alerts.llm_compiler import validate as v

    errors = sorted(v._ENVELOPE_VALIDATOR.iter_errors(obj), key=lambda e: str(e.path))  # type: ignore[attr-defined]
    out: list[str] = []
    for e in errors[:25]:
        path = ".".join([str(p) for p in list(e.path)]) or "<root>"
        out.append(f"- {path}: {e.message}")
    if len(errors) > 25:
        out.append(f"- ... ({len(errors) - 25} more)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Debug jsonschema validation for alert compiler envelope.")
    ap.add_argument("path", help="Path to JSON file containing an envelope or intent")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"File not found: {p}", file=sys.stderr)
        return 2

    obj = _read_json(p)
    if not isinstance(obj, dict):
        print("Top-level JSON must be an object", file=sys.stderr)
        return 2

    # Allow passing intent-only JSON for convenience.
    if "ok" not in obj and "intent" not in obj:
        obj = {"ok": True, "intent": obj, "warnings": [], "clarify": None}

    try:
        validate_envelope(obj)
        print("OK: envelope validates")
        return 0
    except CompilerValidationError as exc:
        print(f"FAIL: {exc}")
        print("Schema errors:")
        for line in _iter_schema_errors(obj):
            print(line)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
