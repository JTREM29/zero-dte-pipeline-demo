from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
        return val.strip() or None
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


def _redis() -> redis.Redis:
    host = (_get_setting("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = int((_get_setting("TNT_REDIS_PORT", "6379") or "6379").strip())
    rdb = int((_get_setting("TNT_REDIS_DB", "0") or "0").strip())
    return redis.Redis(host=host, port=port, db=rdb, decode_responses=True)


def _now() -> int:
    return int(time.time())


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


def _read_scores_updated_utc(raw_scores: str | None) -> int | None:
    if not raw_scores:
        return None
    try:
        js = json.loads(raw_scores)
    except Exception:
        return None
    if not isinstance(js, dict):
        return None
    return _safe_int(js.get("updated_utc"))


def _read_fut_last_ts_utc(r: redis.Redis, sym: str) -> int | None:
    try:
        h = r.hgetall(f"fut:{sym}:last")
    except Exception:
        return None
    if not h:
        return None
    return _safe_int(h.get("ts_utc"))


def _read_lock_pid() -> int | None:
    # Preferred: explicit PID breadcrumb written by the ingest worker.
    pid_path = Path("logs") / "futures_ingest.pid"
    try:
        if pid_path.exists():
            pid = _safe_int(pid_path.read_text(encoding="utf-8", errors="ignore"))
            if pid and pid > 0:
                return int(pid)
    except Exception:
        pass

    # Fallback: singleton lock content (may be unreadable while locked on Windows).
    lock_path = Path("logs") / "tnt_futures_ingest.lock"
    try:
        if not lock_path.exists():
            return None
        text = lock_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    m = re.search(r"\bpid\s*=\s*(\d+)\b", text)
    if not m:
        return None
    pid = _safe_int(m.group(1))
    return int(pid) if pid and pid > 0 else None


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
        # Treat permission as "probably alive"; this is an ops breadcrumb only.
        return True
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Ops: Databento futures ingest health (CONNECTED=1/0).")
    ap.add_argument(
        "--hb-max-age",
        type=int,
        default=int((_get_setting("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", "30") or "30").strip()),
    )
    ap.add_argument(
        "--last-ts-max-age",
        type=int,
        default=int((_get_setting("TNT_FUTURES_INGEST_LAST_TS_MAX_AGE_SEC", "30") or "30").strip()),
        help="Max age (sec) for fut:last_ts advancing",
    )
    ap.add_argument(
        "--scores-max-age",
        type=int,
        default=int((_get_setting("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120") or "120").strip()),
    )
    ap.add_argument(
        "--symbols",
        type=str,
        default=_get_setting("DATABENTO_SYMBOLS", "ES,NQ,RTY"),
        help="Comma-separated futures roots to check fut:{SYM}:last for flow",
    )
    args = ap.parse_args()

    now = _now()
    r = _redis()

    try:
        lock_pid = _read_lock_pid()
        pid_alive = _pid_alive(lock_pid)

        # Keys written by scripts/futures_ingest_databento.py
        hb_ts = _safe_int(r.get("fut:hb"))
        hb_msg = (r.get("fut:hb_msg") or "").strip() or None
        last_ts = _safe_int(r.get("fut:last_ts"))
        raw_scores = r.get("fut:scores")
        raw_status = r.get("fut:status")

        hb_age = _age_s(hb_ts, now)
        last_age = _age_s(last_ts, now)
        scores_updated = _read_scores_updated_utc(raw_scores)
        scores_age = _age_s(scores_updated, now)

        status_note: str | None = None
        status_connected: bool | None = None
        status_last_rec_age_s: float | None = None
        if raw_status:
            try:
                js = json.loads(raw_status)
                if isinstance(js, dict):
                    status_note = str(js.get("note") or "").strip() or None
                    if "connected" in js:
                        v = js.get("connected")
                        status_connected = bool(v) if v is not None else None
                    st_stats = js.get("stats")
                    if isinstance(st_stats, dict) and "last_rec_age_s" in st_stats:
                        try:
                            status_last_rec_age_s = float(st_stats.get("last_rec_age_s"))
                        except Exception:
                            status_last_rec_age_s = None
            except Exception:
                pass

        # Flow check: at least one symbol has a fresh fut:{SYM}:last ts_utc.
        roots = [s.strip().upper() for s in str(args.symbols or "").split(",") if s.strip()]
        freshest_sym: tuple[str, int] | None = None
        for sym in roots:
            ts_utc = _read_fut_last_ts_utc(r, sym)
            age = _age_s(ts_utc, now)
            if age is not None:
                if freshest_sym is None or age < freshest_sym[1]:
                    freshest_sym = (sym, int(age))

        reasons: list[str] = []

        # PID is a helpful breadcrumb, but not required to prove flow.
        if pid_alive is False:
            reasons.append("pid_dead")

        if hb_age is None:
            reasons.append("hb_missing")
        elif hb_age > max(1, int(args.hb_max_age)):
            reasons.append(f"hb_stale_{hb_age}s")

        if last_age is None:
            reasons.append("last_ts_missing")
        elif last_age > max(1, int(args.last_ts_max_age)):
            reasons.append(f"last_ts_stale_{last_age}s")

        if raw_scores is None:
            reasons.append("scores_missing")
        elif scores_age is None:
            reasons.append("scores_bad")
        elif scores_age > max(1, int(args.scores_max_age)):
            reasons.append(f"scores_stale_{scores_age}s")

        if freshest_sym is None:
            reasons.append("no_symbol_ticks")
        else:
            sym, age = freshest_sym
            # Intentionally aligned with last_ts_max_age.
            if age > max(1, int(args.last_ts_max_age)):
                reasons.append(f"sym:{sym}_stale_{age}s")

        # If fut:status explicitly says we're awaiting prints, call it out.
        if status_note and status_note.lower() in {"awaiting_prints", "boot"}:
            reasons.append(f"status_note={status_note}")
        if status_connected is False:
            reasons.append("status_connected=0")
        if status_last_rec_age_s is not None and status_last_rec_age_s > float(max(1, int(args.last_ts_max_age))):
            reasons.append(f"last_rec_age_s={status_last_rec_age_s:.1f}")

        connected = 1 if len(reasons) == 0 else 0

        parts: list[str] = []
        parts.append(f"CONNECTED={connected}")
        parts.append(f"pid={lock_pid if lock_pid is not None else '—'}")
        parts.append("pid_alive=" + ("1" if pid_alive is True else ("0" if pid_alive is False else "—")))
        parts.append(f"hb_age={hb_age if hb_age is not None else '—'}s")
        parts.append(f"last_ts_age={last_age if last_age is not None else '—'}s")
        parts.append(f"scores_age={scores_age if scores_age is not None else '—'}s")

        if freshest_sym is not None:
            parts.append(f"freshest={freshest_sym[0]}:{freshest_sym[1]}s")
        else:
            parts.append("freshest=—")

        if hb_msg:
            parts.append(f"hb_msg={hb_msg}")
        if status_note:
            parts.append(f"note={status_note}")
        if status_connected is not None:
            parts.append(f"connected={int(bool(status_connected))}")
        if status_last_rec_age_s is not None:
            parts.append(f"last_rec_age={status_last_rec_age_s:.1f}s")
        if reasons:
            parts.append("reason=" + "+".join(reasons))

        print(" ".join(parts))
        return 0 if connected == 1 else 2
    finally:
        try:
            r.close()
        except Exception:
            pass
        reasons.append("pid_dead")

    if hb_age is None:
        reasons.append("hb_missing")
    elif hb_age > max(1, int(args.hb_max_age)):
        reasons.append(f"hb_stale_{hb_age}s")

    if last_age is None:
        reasons.append("last_ts_missing")
    elif last_age > max(1, int(args.last_ts_max_age)):
        reasons.append(f"last_ts_stale_{last_age}s")

    if raw_scores is None:
        reasons.append("scores_missing")
    elif scores_age is None:
        reasons.append("scores_bad")
    elif scores_age > max(1, int(args.scores_max_age)):
        reasons.append(f"scores_stale_{scores_age}s")

    if freshest_sym is None:
        reasons.append("no_symbol_ticks")
    else:
        sym, age = freshest_sym
        # This is intentionally aligned with last_ts_max_age.
        if age > max(1, int(args.last_ts_max_age)):
            reasons.append(f"sym:{sym}_stale_{age}s")

    # If fut:status explicitly says we're awaiting prints, call it out.
    if status_note and status_note.lower() in {"awaiting_prints", "boot"}:
        reasons.append(f"status_note={status_note}")
    if status_connected is False:
        reasons.append("status_connected=0")
    if status_last_rec_age_s is not None and status_last_rec_age_s > float(max(1, int(args.last_ts_max_age))):
        reasons.append(f"last_rec_age_s={status_last_rec_age_s:.1f}")

    connected = 1 if len(reasons) == 0 else 0

    # Single-line ops output.
    parts: list[str] = []
    parts.append(f"CONNECTED={connected}")
    parts.append(f"pid={lock_pid if lock_pid is not None else '—'}")
    parts.append(
        "pid_alive="
        + ("1" if pid_alive is True else ("0" if pid_alive is False else "—"))
    )
    parts.append(f"hb_age={hb_age if hb_age is not None else '—'}s")
    parts.append(f"last_ts_age={last_age if last_age is not None else '—'}s")
    parts.append(f"scores_age={scores_age if scores_age is not None else '—'}s")
    if freshest_sym is not None:
        parts.append(f"freshest={freshest_sym[0]}:{freshest_sym[1]}s")
    else:
        parts.append("freshest=—")
    if hb_msg:
        parts.append(f"hb_msg={hb_msg}")
    if status_note:
        parts.append(f"note={status_note}")
    if status_connected is not None:
        parts.append(f"connected={int(bool(status_connected))}")
    if status_last_rec_age_s is not None:
        parts.append(f"last_rec_age={status_last_rec_age_s:.1f}s")
    if reasons:
        parts.append("reason=" + "+".join(reasons))

    print(" ".join(parts))

    return 0 if connected == 1 else 2


if __name__ == "__main__":
    raise SystemExit(main())
