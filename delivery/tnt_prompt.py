"""Utilities for loading TNT's system prompt from disk.

This is intentionally separated from the Discord bot module so it can be
unit-tested without importing discord.py.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TNTSystemPromptInfo:
    path: Path
    sha256: str
    chars: int
    head: str
    text: str


_DEFAULT_PROMPT_PATH = (Path(__file__).resolve().parent.parent / "tnt_system_prompt.txt")

# Cache keyed by resolved path. Tracks file stat so edits are picked up.
_PROMPT_CACHE: dict[str, dict[str, object]] = {}


def _resolve_prompt_path(path: Optional[os.PathLike[str] | str] = None) -> Path:
    env_path = (os.getenv("TNT_SYSTEM_PROMPT_PATH") or "").strip()
    chosen = Path(env_path) if env_path else (Path(path) if path else _DEFAULT_PROMPT_PATH)
    return chosen.expanduser().resolve()


def _fingerprint(text: str) -> tuple[str, int, str]:
    raw = text or ""
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    head = raw.strip().replace("\r", "").replace("\n", " ")[:60]
    return sha, len(raw), head


def load_tnt_system_prompt(
    path: Optional[os.PathLike[str] | str] = None,
    *,
    log: bool = False,
) -> TNTSystemPromptInfo:
    """Load TNT system prompt from disk and return content + fingerprint.

    - Always reads from `tnt_system_prompt.txt` by default.
    - Supports override via `TNT_SYSTEM_PROMPT_PATH`.
    - Caches based on file mtime/size so edits are detected.
    """

    resolved = _resolve_prompt_path(path)

    stat = resolved.stat()  # raises if missing
    cache_key = str(resolved)
    cached = _PROMPT_CACHE.get(cache_key)

    if cached and cached.get("mtime") == stat.st_mtime and cached.get("size") == stat.st_size:
        info = cached["info"]
        assert isinstance(info, TNTSystemPromptInfo)
    else:
        text = resolved.read_text(encoding="utf-8")
        sha, chars, head = _fingerprint(text)
        info = TNTSystemPromptInfo(path=resolved, sha256=sha, chars=chars, head=head, text=text)
        _PROMPT_CACHE[cache_key] = {"mtime": stat.st_mtime, "size": stat.st_size, "info": info, "logged": False}

    if log:
        entry = _PROMPT_CACHE.get(cache_key) or {}
        if not bool(entry.get("logged")):
            print(
                f"[TNT][PROMPT] Loaded {info.path} (sha256={info.sha256}, chars={info.chars}, head={info.head!r})"
            )
            entry["logged"] = True
            _PROMPT_CACHE[cache_key] = entry

    return info
