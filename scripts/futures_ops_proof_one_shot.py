from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import redis

from services.futures.futures_store import FuturesStore
from services.observability.futures_ingest_health import classify_futures_ingest


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    s = str(raw).strip()
    if not s:
        return int(default)
    try:
        return int(float(s))
    except Exception:
        return int(default)


def _redis_client() -> redis.Redis:
    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    return redis.Redis(host=host, port=int(port), db=int(db), decode_responses=True)


def _read_pid_breadcrumb() -> int | None:
    pid_path = Path("logs") / "futures_ingest.pid"
    try:
        if pid_path.exists():
            txt = pid_path.read_text(encoding="utf-8", errors="ignore").strip()
            if txt:
                pid = int(float(txt))
                return pid if pid > 0 else None
    except Exception:
        return None
    return None


def _pid_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None

    if sys.platform.startswith("win"):
        try:
            p = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            out = (p.stdout or "") + "\n" + (p.stderr or "")
            return re.search(rf"\b{pid}\b", out) is not None
        except Exception:
            return None

    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _safe_int(x: Any) -> int | None:
    if x is None:
        return None
    if isinstance(x, (bytes, bytearray)):
        try:
            x = x.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return int(x)
    if isinstance(x, float):
        return int(x)
    try:
        return int(float(str(x).strip()))
    except Exception:
        return None


def _age_s(ts: int | None, now: int) -> int | None:
    if ts is None:
        return None
    return max(0, int(now - int(ts)))


def _tail_text(path: Path, lines: int = 25) -> str | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    parts = text.splitlines()[-max(1, int(lines)) :]
    return "\n".join(parts).strip() or None


def main() -> int:
    now = int(time.time())

    r = _redis_client()
    store = FuturesStore(r=r)

    # 1) Redis alive
    redis_ok = False
    redis_err = None
    try:
        redis_ok = bool(r.ping())
    except Exception as exc:
        redis_ok = False
        redis_err = f"{type(exc).__name__}: {exc}"[:200]

    # 2) Worker PID breadcrumb
    pid = _read_pid_breadcrumb()
    pid_alive = _pid_alive(pid)

    # 3) fut:* freshness
    hb_ts, hb_msg = store.get_heartbeat()
    hb_age = _age_s(int(hb_ts) if hb_ts is not None else None, now)

    raw_scores = None
    try:
        raw_scores = r.get("fut:scores")
    except Exception:
        raw_scores = None
    scores_present = bool(raw_scores)

    scores_updated = None
    if raw_scores:
        try:
            js = json.loads(raw_scores)
            if isinstance(js, dict):
                scores_updated = _safe_int(js.get("updated_utc"))
        except Exception:
            scores_updated = None
    scores_age = _age_s(scores_updated, now) if scores_present else None

    last_ts = None
    try:
        last_ts = _safe_int(r.get("fut:last_ts"))
    except Exception:
        last_ts = None
    last_age = _age_s(last_ts, now)

    # 4) fut:status content
    status = store.get_status() or {}
    note = status.get("note") if isinstance(status, dict) else None
    connected = status.get("connected") if isinstance(status, dict) else None

    stats = status.get("stats") if isinstance(status, dict) and isinstance(status.get("stats"), dict) else {}
    last_rec_age_s = stats.get("last_rec_age_s") if isinstance(stats, dict) else None
    rec_counts = stats.get("rec_counts") if isinstance(stats, dict) else None

    # 5) Classification (same single source of truth used by bot/canaries)
    hb_max = _env_int("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", 90)
    scores_max = _env_int("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", 120)

    state, reason = classify_futures_ingest(
        hb_age_s=hb_age,
        scores_age_s=scores_age,
        scores_present=scores_present,
        note=str(note) if note is not None else None,
        hb_msg=str(hb_msg) if hb_msg is not None else None,
        hb_max_age=hb_max,
        scores_max_age=scores_max,
    )

    # Legacy /status line mirror (cli/discord_bot.py)
    legacy_line = "—"
    if hb_age is not None:
        if hb_age > hb_max:
            legacy_line = f"DOWN (hb {hb_age}s stale)"
        elif (not scores_present) or (scores_age is None):
            legacy_line = f"DEGRADED (hb {hb_age}s, scores missing)"
        elif scores_age > scores_max:
            legacy_line = f"DEGRADED (hb {hb_age}s, scores {scores_age}s stale)"
        else:
            legacy_line = f"OK (hb {hb_age}s, scores {scores_age}s)"

    # Output: short proof block, easy to paste.
    print(f"REDIS_OK={int(redis_ok)}" + (f" err={redis_err}" if (not redis_ok and redis_err) else ""))
    print(
        " ".join(
            [
                f"WORKER_PID={pid if pid is not None else '—'}",
                "pid_alive=" + ("1" if pid_alive is True else ("0" if pid_alive is False else "—")),
            ]
        )
    )
    print(
        " ".join(
            [
                f"FRESH hb_age={hb_age if hb_age is not None else '—'}s",
                f"last_ts_age={last_age if last_age is not None else '—'}s",
                f"scores_age={scores_age if scores_age is not None else '—'}s",
                f"hb_msg={hb_msg or '—'}",
            ]
        )
    )
    print(
        " ".join(
            [
                f"STATUS note={note or '—'}",
                f"connected={connected if connected is not None else '—'}",
                f"last_rec_age_s={last_rec_age_s if last_rec_age_s is not None else '—'}",
                f"rec_counts={rec_counts if rec_counts is not None else '—'}",
            ]
        )
    )
    print(f"CLASSIFY={state}" + (f" reason={reason}" if reason else "") + f" | status_line={legacy_line}")

    # Optional: last ingest stdout tail
    tail = _tail_text(Path("logs") / "futures_ingest_stdout.log", lines=12)
    if tail:
        print("LOG_TAIL:")
        print(tail)

    try:
        r.close()
    except Exception:
        pass

    return 0 if state == "OK" and redis_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
