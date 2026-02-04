from __future__ import annotations

import json
import os
from pathlib import Path

import redis


def _repo_root() -> Path:
    try:
        return Path(__file__).resolve().parents[1]
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


def _get_setting(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw and raw.strip():
        return raw.strip()

    root = _repo_root()
    for env_path in (root / ".env.local", root / ".env"):
        val = _dotenv_get_value(env_path, key)
        if val and val.strip():
            return val.strip()
    return default


def main() -> None:
    r = redis.Redis(
        host=_get_setting("TNT_REDIS_HOST", "127.0.0.1"),
        port=int(_get_setting("TNT_REDIS_PORT", "6379")),
        db=int(_get_setting("TNT_REDIS_DB", "0")),
        decode_responses=True,
    )

    for s in ["ES", "NQ", "RTY"]:
        h = r.hgetall(f"fut:{s}:last")
        print(s, h)

    raw = r.get("fut:scores")
    print("scores:", json.loads(raw) if raw else None)
    print("hb:", r.get("fut:hb"))

    raw_status = r.get("fut:status")
    if not raw_status:
        print("status:", None)
        return

    print("status_raw:", raw_status)
    try:
        js = json.loads(raw_status)
    except Exception as exc:
        print("status_parse_error:", repr(exc))
        return

    if not isinstance(js, dict):
        print("status:", js)
        return

    stats = js.get("stats") if isinstance(js.get("stats"), dict) else {}
    print(
        "status:",
        {
            "state": js.get("state"),
            "note": js.get("note"),
            "connected": js.get("connected"),
            "dataset": js.get("dataset"),
            "schema": js.get("schema"),
            "stype_in": js.get("stype_in"),
            "symbols": js.get("symbols"),
            "ts_utc": js.get("ts_utc"),
            "last_error": js.get("last_error"),
            "last_rec_age_s": stats.get("last_rec_age_s"),
            "rec_counts": stats.get("rec_counts"),
        },
    )


if __name__ == "__main__":
    main()
