from __future__ import annotations

import os
from pathlib import Path


def _repo_root() -> Path:
    try:
        # services/context/ctx_mode.py -> repo root is 2 levels up
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()


def _dotenv_get_value(path: Path, key: str) -> str | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        val = v.strip()
        if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
            val = val[1:-1]
        return val.strip()
    return None


def get_setting(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw and raw.strip():
        return raw.strip()
    root = _repo_root()
    for env_path in (root / ".env.local", root / ".env"):
        val = _dotenv_get_value(env_path, key)
        if val and val.strip():
            return val.strip()
    return default


def ctx_enabled() -> bool:
    return (get_setting("CTX_SNAPSHOT_ENABLED", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


def ctx_strict_mode() -> str:
    """returns: "off" | "shadow" | "on"""
    v = (get_setting("CTX_SNAPSHOT_STRICT", "0") or "0").strip().lower()
    if v in ("1", "true", "yes", "on", "strict"):
        return "on"
    if v in ("shadow", "warn", "telemetry"):
        return "shadow"
    return "off"
