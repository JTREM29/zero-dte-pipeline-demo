from __future__ import annotations

import json
import os
from pathlib import Path


def _is_ci() -> bool:
    v = (os.getenv("CI") or os.getenv("GITHUB_ACTIONS") or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def _is_strict() -> bool:
    v = (os.getenv("MASSIVE_CONTRACT_TESTS_STRICT") or "").strip().lower()
    return _is_ci() or (v in {"1", "true", "yes", "on"})


def test_massive_registry_exists_and_valid_json() -> None:
    path = Path("docs/massive/registry.json")

    if not path.exists():
        if _is_ci() or (os.getenv("MASSIVE_CONTRACT_TESTS_STRICT") or "").strip() in {"1", "true", "yes", "on"}:
            raise AssertionError(
                "Missing docs/massive/registry.json. Run: "
                "python scripts/massive_docs_sync.py ; python scripts/massive_registry_build.py"
            )
        # Local dev convenience: don’t force docs sync to run on every edit.
        return

    obj = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(obj, dict)
    assert isinstance(obj.get("entries"), list)


def test_massive_registry_has_required_endpoints() -> None:
    path = Path("docs/massive/registry.json")

    if not path.exists():
        if _is_ci() or (os.getenv("MASSIVE_CONTRACT_TESTS_STRICT") or "").strip() in {"1", "true", "yes", "on"}:
            raise AssertionError(
                "Missing docs/massive/registry.json. Run: "
                "python scripts/massive_docs_sync.py ; python scripts/massive_registry_build.py"
            )
        return

    obj = json.loads(path.read_text(encoding="utf-8"))
    entries = obj.get("entries")
    assert isinstance(entries, list)

    def _has(method: str, p: str) -> bool:
        for e in entries:
            if not isinstance(e, dict):
                continue
            if str(e.get("method") or "").upper() == method and str(e.get("path") or "") == p:
                return True
        return False

    # These are hard dependencies in this repo today.
    assert _has("GET", "/benzinga/v2/news"), "Missing GET /benzinga/v2/news in registry"
    assert _has("GET", "/benzinga/v1/earnings"), "Missing GET /benzinga/v1/earnings in registry"

    # Second tripwire: options endpoint used for expected-move plumbing.
    # See delivery.discord_bot._fetch_polygon_options_chain_df / _fetch_polygon_atm_straddle_df.
    options_req = ("GET", "/v3/reference/options/contracts")
    have_opt = _has(options_req[0], options_req[1])
    if _is_strict():
        assert have_opt, f"Missing {options_req[0]} {options_req[1]} in registry"
    elif not have_opt:
        print(f"WARN massive registry missing {options_req[0]} {options_req[1]} (non-strict)")


def test_massive_registry_entries_are_not_empty() -> None:
    path = Path("docs/massive/registry.json")

    if not path.exists():
        if _is_ci() or (os.getenv("MASSIVE_CONTRACT_TESTS_STRICT") or "").strip() in {"1", "true", "yes", "on"}:
            raise AssertionError(
                "Missing docs/massive/registry.json. Run: "
                "python scripts/massive_docs_sync.py ; python scripts/massive_registry_build.py"
            )
        return

    obj = json.loads(path.read_text(encoding="utf-8"))
    entries = obj.get("entries")
    assert isinstance(entries, list)

    for e in entries:
        assert isinstance(e, dict)
        method = str(e.get("method") or "").strip().upper()
        p = str(e.get("path") or "").strip()
        assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert p.startswith("/")
