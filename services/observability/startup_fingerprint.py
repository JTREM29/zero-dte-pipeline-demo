from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _repo_root(start: Path | None = None) -> Path:
    here = start or Path.cwd()
    for p in [here, *here.parents]:
        try:
            if (p / ".git").exists():
                return p
            if (p / "setup.py").exists():
                return p
        except Exception:
            continue
    return here


def _git_sha(repo_root: Path) -> str:
    for k in ("GIT_SHA", "GITHUB_SHA"):
        v = (os.getenv(k) or "").strip()
        if v:
            return v[:12]
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=1.0,
            check=True,
        )
        return (r.stdout or "").strip()[:12] or "unknown"
    except Exception:
        return "unknown"


def collect_startup_fingerprint(*, entrypoint: str) -> dict[str, Any]:
    repo = _repo_root(Path(__file__).resolve())
    now = time.time()
    fp: dict[str, Any] = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "entrypoint": str(entrypoint),
        "host": platform.node() or "unknown",
        "pid": int(os.getpid()),
        "cwd": str(Path.cwd()),
        "repo": str(repo),
        "git": _git_sha(repo),
        "python": sys.version.split()[0],
        "exe": sys.executable,
    }
    return fp


def fingerprint_line(fp: dict[str, Any]) -> str:
    entry = str(fp.get("entrypoint") or "unknown")
    git = str(fp.get("git") or "unknown")
    py = str(fp.get("python") or "unknown")
    host = str(fp.get("host") or "unknown")
    repo = str(fp.get("repo") or "unknown")
    return f"entrypoint={entry} | git={git} | py={py} | host={host} | repo={repo}"


def fingerprint_multiline(fp: dict[str, Any]) -> str:
    keys = [
        "ts_utc",
        "entrypoint",
        "git",
        "python",
        "exe",
        "host",
        "pid",
        "repo",
        "cwd",
    ]
    return "\n".join(f"{k}={fp.get(k)}" for k in keys)


def _state_path(repo_root: Path) -> Path:
    raw = (os.getenv("TNT_STARTUP_FINGERPRINT_STATE_PATH") or "cache/startup_fingerprint.json").strip()
    if not raw:
        raw = "cache/startup_fingerprint.json"
    return (repo_root / raw).resolve()


def _fp_hash(fp: dict[str, Any]) -> str:
    core = {
        "entrypoint": fp.get("entrypoint"),
        "git": fp.get("git"),
        "python": fp.get("python"),
        "exe": fp.get("exe"),
        "repo": fp.get("repo"),
    }
    blob = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def should_emit_startup_fingerprint(fp: dict[str, Any], *, cooldown_sec: int = 300) -> bool:
    repo = Path(str(fp.get("repo") or ""))
    if not repo:
        repo = _repo_root(Path.cwd())

    path = _state_path(repo)
    now = time.time()
    h = _fp_hash(fp)

    try:
        if path.exists():
            prev = json.loads(path.read_text(encoding="utf-8", errors="ignore") or "{}")
        else:
            prev = {}
    except Exception:
        prev = {}

    try:
        prev_hash = str(prev.get("hash") or "")
        prev_ts = float(prev.get("ts") or 0.0)
    except Exception:
        prev_hash, prev_ts = "", 0.0

    if prev_hash != h:
        return True
    if cooldown_sec > 0 and (now - prev_ts) >= float(cooldown_sec):
        # Same fingerprint but last emit was a while ago (rare restarts / long-running).
        return True
    return False


def record_startup_fingerprint_emitted(fp: dict[str, Any]) -> None:
    repo = Path(str(fp.get("repo") or ""))
    if not repo:
        repo = _repo_root(Path.cwd())

    path = _state_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.time(),
            "hash": _fp_hash(fp),
            "entrypoint": fp.get("entrypoint"),
            "git": fp.get("git"),
            "python": fp.get("python"),
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        return
