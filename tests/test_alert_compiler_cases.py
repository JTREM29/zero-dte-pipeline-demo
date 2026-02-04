from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tnt_alerts.dsl_envelope import compile_text_to_dsl_envelope


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "tests" / "alert_compiler_cases.json"


def _deep_contains(actual: Any, expected_subset: Any, *, path: str = "$") -> None:
    """Assert that actual recursively contains expected_subset.

    - dict: expected keys must exist in actual and recursively deep-contain
    - list: expected must be a prefix subset by position (stable for our contract cases)
    - scalars: equality
    """

    if expected_subset is None:
        assert actual is None, f"{path}: expected None, got {type(actual).__name__}"
        return

    if isinstance(expected_subset, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual).__name__}"
        for k, v in expected_subset.items():
            assert k in actual, f"{path}: missing key '{k}'"
            _deep_contains(actual[k], v, path=f"{path}.{k}")
        return

    if isinstance(expected_subset, list):
        assert isinstance(actual, list), f"{path}: expected list, got {type(actual).__name__}"
        assert len(actual) >= len(expected_subset), f"{path}: list shorter than expected subset"
        for i, v in enumerate(expected_subset):
            _deep_contains(actual[i], v, path=f"{path}[{i}]")
        return

    assert actual == expected_subset, f"{path}: expected {expected_subset!r}, got {actual!r}"


@pytest.mark.parametrize("case", json.loads(CASES_PATH.read_text(encoding="utf-8")))
def test_alert_compiler_cases(case: dict[str, Any]) -> None:
    text = str(case["text"])
    expected = case["expected"]

    env = compile_text_to_dsl_envelope(text)

    assert bool(env.get("ok")) == bool(expected.get("ok")), f"{case.get('id')}: ok mismatch"

    assert env.get("dsl") == expected.get("dsl"), f"{case.get('id')}: dsl mismatch"

    actual_codes = sorted(
        [w.get("code") for w in (env.get("warnings") or []) if isinstance(w, dict) and w.get("code")]
    )
    expected_codes = sorted([str(x) for x in (expected.get("warning_codes") or [])])
    assert actual_codes == expected_codes, f"{case.get('id')}: warning codes mismatch"

    assert env.get("clarify") == expected.get("clarify"), f"{case.get('id')}: clarify mismatch"

    _deep_contains(env.get("intent_partial"), expected.get("intent_partial"), path="$intent_partial")
