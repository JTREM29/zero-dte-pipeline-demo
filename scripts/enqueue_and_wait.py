from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

POLL_SECONDS = 0.25


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_job_id() -> str:
    # Stable + readable.
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3).upper()}"


@dataclass(frozen=True)
class WaitOutcome:
    job_id: str
    status: str
    result: Dict[str, Any]
    heartbeat_text: Optional[str]


def enqueue_and_wait(
    *,
    redis_host: str,
    symbol: str,
    job_type: str,
    payload: Optional[Dict[str, Any]] = None,
    reply_path: Optional[str] = None,
    redis_port: int = 6379,
    redis_db: int = 0,
    queue: str = "tnt:jobs",
    result_key_prefix: str = "tnt:result:",
    heartbeat_key_prefix: str = "tnt:hb:",
    timeout_s: float = 90.0,
    redis_socket_connect_timeout_s: float = 2.0,
    redis_socket_timeout_s: float = 2.0,
) -> WaitOutcome:
    try:
        import redis  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "redis not installed for this interpreter. "
            "Install it into the Python you're using to run this script (e.g. `py -3.12 -m pip install redis` or your venv). "
            f"Import error: {type(exc).__name__}: {exc}"
        )

    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")

    job_id = make_job_id()

    rp = (reply_path or os.getenv("TNT_ARTIFACTS_SHARE", "") or "").strip()

    job = {
        "job_id": job_id,
        "type": str(job_type),
        "symbol": sym,
        "params": payload or {},
        # Backwards compatible: if a shared folder is configured, ask worker to write there.
        # Otherwise, worker will write locally (e.g. TNT_ARTIFACTS_DIR) and can expose via HTTP.
        "reply": ({"mode": "shared_folder", "path": rp} if rp else {"mode": "local"}),
        "created_at": utc_now_iso(),
    }

    try:
        r = redis.Redis(
            host=str(redis_host),
            port=int(redis_port),
            db=int(redis_db),
            decode_responses=True,
            socket_connect_timeout=float(redis_socket_connect_timeout_s),
            socket_timeout=float(redis_socket_timeout_s),
        )
        # Force a real connection early so failures are obvious.
        r.ping()
        r.rpush(str(queue), json.dumps(job, separators=(",", ":"), ensure_ascii=False))
    except Exception as exc:
        raise RuntimeError(
            "Redis enqueue failed. "
            f"host={redis_host!r} port={redis_port} db={redis_db} queue={queue!r}. "
            "Verify Redis is reachable from this machine (PowerShell: "
            f"`Test-NetConnection -ComputerName {redis_host} -Port {redis_port}`). "
            f"Error: {type(exc).__name__}: {exc}"
        )

    result_key = f"{result_key_prefix}{job_id}"
    hb_key = f"{heartbeat_key_prefix}{job_id}"

    deadline = time.time() + float(timeout_s)
    last_hb: Optional[str] = None

    while True:
        raw = r.get(result_key)
        if isinstance(raw, str) and raw.strip():
            try:
                result = json.loads(raw)
            except Exception:
                result = {"raw": raw}
            status = str(result.get("status") or ("succeeded" if bool(result.get("ok")) else "failed"))
            return WaitOutcome(job_id=job_id, status=status, result=result if isinstance(result, dict) else {"result": result}, heartbeat_text=last_hb)

        hb = r.get(hb_key)
        if isinstance(hb, str) and hb.strip() and hb != last_hb:
            last_hb = hb.strip()
            try:
                print(f"{job_id}: heartbeat {last_hb}")
            except Exception:
                pass

        if time.time() > deadline:
            raise TimeoutError(f"{job_id}: timeout after {timeout_s:.1f}s (last heartbeat={last_hb!r})")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    host = os.environ.get("TNT_REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("TNT_REDIS_PORT", "6379"))
    share = os.environ.get("TNT_ARTIFACTS_SHARE", "")

    out = enqueue_and_wait(
        redis_host=host,
        redis_port=port,
        symbol="SPY",
        job_type="render_oi",
        payload={"top": 18, "dpi": 130},
        reply_path=(share or None),
        timeout_s=30.0,
    )
    print(out.job_id, out.status)
    print(json.dumps(out.result, indent=2))
