from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _load_entrypoints() -> list[Path]:
    cfg_path = ROOT / "config" / "entrypoints.json"
    if not cfg_path.exists():
        # Backward-compatible default.
        return [
            ROOT / "delivery" / "discord_bot.py",
            ROOT / "cli" / "discord_bot.py",
        ]

    raw = cfg_path.read_text(encoding="utf-8", errors="ignore")
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"Invalid JSON in config/entrypoints.json: {exc}")

    items = data.get("discord_entrypoints") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise AssertionError("config/entrypoints.json must contain non-empty 'discord_entrypoints' list")

    out: list[Path] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise AssertionError("config/entrypoints.json 'discord_entrypoints' must be a list of non-empty strings")
        out.append(ROOT / item.strip().replace("\\", "/"))
    return out

# Only scan execution paths that can actually send chart PNGs.
ENTRYPOINTS = _load_entrypoints()

# Single source of truth for attachment construction.
HELPER_MODULES = [
    ROOT / "services" / "discord_files.py",
]

DISCORD_FILE_RE = re.compile(r"\bdiscord\.File\s*\(")
IMPORT_HELPER_RE = re.compile(r"\bservices\.discord_files\b")
DEF_HELPER_RE = re.compile(
    r"^def\s+(file_from_png_bytes|files_from_name_bytes)\b",
    re.M,
)


def _read(p: Path) -> str:
    # Keep the test robust: ignore decode issues and avoid hard failures.
    return p.read_text(encoding="utf-8", errors="ignore")


def test_discord_file_callsites_are_centralized() -> None:
    """Static guardrail: bot entrypoints must not construct discord.File(...) directly."""

    offenders: list[str] = []
    missing_imports: list[str] = []

    for p in ENTRYPOINTS:
        if not p.exists():
            raise AssertionError(
                "Configured discord entrypoint does not exist (edit config/entrypoints.json to match production runbook):\n"
                + str(p.relative_to(ROOT))
            )
        txt = _read(p)
        if DISCORD_FILE_RE.search(txt):
            offenders.append(str(p.relative_to(ROOT)))
        if not IMPORT_HELPER_RE.search(txt):
            missing_imports.append(str(p.relative_to(ROOT)))

    assert not offenders, (
        "discord.File(...) found in bot entrypoint(s); attachment construction must be centralized in services/discord_files.py.\n"
        + "\n".join(offenders)
    )

    assert not missing_imports, (
        "Bot entrypoint(s) missing a services.discord_files import; require an explicit import to prevent shadow helper copies.\n"
        + "\n".join(missing_imports)
    )


def test_discord_file_helpers_are_single_source_of_truth() -> None:
    """Static guardrail: helper function defs must exist in one module only.

    This is intentionally O(#entrypoints) rather than scanning the entire repo.
    """

    wanted = {"file_from_png_bytes", "files_from_name_bytes"}

    modules_with_defs: dict[str, set[str]] = {}
    for p in ENTRYPOINTS + HELPER_MODULES:
        if not p.exists():
            continue
        txt = _read(p)
        hits = {m.group(1) for m in DEF_HELPER_RE.finditer(txt)}
        if hits:
            modules_with_defs[str(p.relative_to(ROOT))] = set(hits)

    assert modules_with_defs, "Expected helper defs to exist in services/discord_files.py"
    assert len(modules_with_defs) == 1, (
        "Helper definitions must exist in exactly one module.\nFound in:\n"
        + "\n".join(f"- {mod}: {sorted(fns)}" for mod, fns in modules_with_defs.items())
    )

    only_mod, fns = next(iter(modules_with_defs.items()))
    assert only_mod == str((ROOT / "services" / "discord_files.py").relative_to(ROOT))
    assert wanted.issubset(fns)
