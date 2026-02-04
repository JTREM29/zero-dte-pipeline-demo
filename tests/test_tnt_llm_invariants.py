"""Regression tests enforcing the "One Door" LLM policy.

Intent:
- Only `delivery/tnt_llm.py` should directly import/use the OpenAI SDK.
- The wrapper must inject TNT_STATE (authoritative) into instructions.
"""

from __future__ import annotations

import ast
import os
import pathlib

from delivery.tnt_llm import compose_instructions


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _iter_py_files() -> list[pathlib.Path]:
    root = _repo_root()

    def _has_any_windows_attrs(path: pathlib.Path, mask: int) -> bool:
        try:
            st = path.stat()
        except Exception:
            return False
        attrs = int(getattr(st, "st_file_attributes", 0) or 0)
        return bool(attrs & mask)

    # OneDrive / Cloud files: protect against placeholder reads that can block.
    # References:
    # - FILE_ATTRIBUTE_OFFLINE (0x1000)
    # - FILE_ATTRIBUTE_REPARSE_POINT (0x0400) (symlinks/junctions)
    # - FILE_ATTRIBUTE_RECALL_ON_OPEN (0x40000)
    # - FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (0x400000)
    FILE_ATTR_SKIP_MASK = 0x1000 | 0x0400 | 0x40000 | 0x400000

    # Only scan the actual code directories. This avoids crawling OneDrive-synced
    # non-code trees (logs/tmp/artifacts/etc) where placeholder files can hang
    # on open/read.
    # Keep this scan focused on runtime code paths.
    # In OneDrive-backed workspaces, some folders may contain placeholder files
    # that can hang on open/read.
    code_dirs = [
        "delivery",
        "tnt_alerts",
        "cli",
        "src",
        "agents",
        "controller",
        "massive_service",
        "worker",
        "zero_dte_pipeline",
        "bots",
    ]

    bases: list[pathlib.Path] = []
    for d in code_dirs:
        p = (root / d)
        if p.exists() and p.is_dir():
            bases.append(p)

    files: list[pathlib.Path] = []
    # Top-level python scripts.
    files.extend([p for p in root.glob("*.py") if p.is_file()])

    # Walk code directories without following symlinks/junctions.
    for base in bases:
        # Skip entire bases that are cloud placeholders or junctions.
        if _has_any_windows_attrs(base, FILE_ATTR_SKIP_MASK):
            continue

        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dir_path = pathlib.Path(dirpath)

            # Prune virtualenvs/cache folders early.
            dirnames[:] = [
                d
                for d in dirnames
                if d not in {".venv", "venv", "__pycache__", "site-packages"} and not d.startswith(".venv")
            ]

            # Prune directories that are junctions/placeholders.
            pruned: list[str] = []
            for d in dirnames:
                p = dir_path / d
                if _has_any_windows_attrs(p, FILE_ATTR_SKIP_MASK):
                    pruned.append(d)
            if pruned:
                dirnames[:] = [d for d in dirnames if d not in set(pruned)]

            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = dir_path / name
                if _has_any_windows_attrs(path, FILE_ATTR_SKIP_MASK):
                    continue
                files.append(path)

    # Stable ordering for reproducibility.
    return sorted(set(files))


def test_tnt_llm_compose_instructions_includes_state():
    state = {
        "meta": {
            "schema_version": "1.0",
            "generated_at_et": "2025-01-01T09:30:00-05:00",
            "data_freshness_sec": None,
            "data_health": "DOWN",
            "data_integrity": {"ok": False, "reason": "TEST"},
            "source": {"provider": "TEST", "mode": "TEST"},
        },
        "context": {
            "symbol": "SPY",
            "asset_class": "EQUITY",
            "session": "UNKNOWN",
            "timeframes": {"execution": "5m", "structure": "60m", "context": "1D"},
        },
        "price": {"last": None},
        "posture": {"bias": "NEUTRAL", "conviction": "LOW", "regime": "COMPRESSION", "mode": "NORMAL", "rationale_tags": []},
        "levels": {"decision_zones": [], "support": [], "resistance": [], "pivots_rth": {"P": None, "R1": None, "S1": None, "R2": None, "S2": None}},
        "permissions": {"aggression": "PROHIBITED", "momentum_only": False, "no_trade": True, "reasons": ["DATA_HEALTH"]},
        "events": [],
        "chart": {"recommended_template": "BALANCED", "template_params": None},
    }

    instructions, prompt_sha, tnt_sha = compose_instructions(tnt_state=state)

    assert prompt_sha
    assert tnt_sha
    assert "TNT_STATE (authoritative)" in instructions
    assert '"symbol": "SPY"' in instructions


import pytest


@pytest.mark.timeout(240)
def test_one_door_no_openai_outside_wrapper():
    root = _repo_root()
    allowed = {
        (root / "delivery" / "tnt_llm.py").resolve(),
    }

    offenders: list[str] = []

    def _rel(p: pathlib.Path) -> str:
        return str(p.relative_to(root)).replace("\\", "/")

    def _attr_chain(node: ast.AST) -> list[str]:
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return list(reversed(parts))

    for path in _iter_py_files():
        resolved = path.resolve()
        if resolved in allowed:
            continue

        # Fast path: only fully parse files that look suspicious.
        try:
            # Avoid buffering weirdness; we only want a small prefix.
            with path.open("rb", buffering=0) as f:
                head = f.read(200_000)
            head_txt = head.decode("utf-8", errors="replace")
        except Exception:
            continue

        suspicious = (
            "openai" in head_txt
            or "OpenAI" in head_txt
            or "responses.create" in head_txt
            or "chat.completions.create" in head_txt
        )
        if not suspicious:
            continue

        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            # If a file is syntactically invalid, let other tests/lints surface it.
            continue

        hit = False

        for node in ast.walk(tree):
            # Blocklist: `from openai import ...` and `import openai`
            if isinstance(node, ast.ImportFrom) and node.module == "openai":
                hit = True
                break
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "openai" or alias.name.startswith("openai."):
                        hit = True
                        break
                if hit:
                    break

            # Blocklist: `OpenAI(...)` instantiation
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == "OpenAI":
                    hit = True
                    break

                # Blocklist: `.responses.create(...)` or `.chat.completions.create(...)`
                if isinstance(fn, ast.Attribute) and fn.attr == "create":
                    chain = _attr_chain(fn)
                    if chain[-2:] == ["responses", "create"]:
                        hit = True
                        break
                    if chain[-3:] == ["completions", "create",]:
                        # Ensure it's chat.completions.create
                        if "chat" in chain:
                            hit = True
                            break

        if hit:
            offenders.append(_rel(path))

    assert not offenders, f"OpenAI usage found outside delivery/tnt_llm.py: {offenders}"
