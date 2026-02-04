"""Debug helper: run a single alert_trigger job through worker code path.

This executes massive_service.redis_worker._alert_trigger_job() directly and prints:
- JobResult summary
- LLEN/type of TNT_ALERTS_DISCORD_QUEUE

Use this to validate end-to-end wiring without relying on schedulers.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import redis

# Ensure repo root is importable when running as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from massive_service.redis_worker import _alert_trigger_job


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def main() -> int:
    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)

    results_key = (os.getenv("TNT_REDIS_RESULTS", "tnt:results") or "tnt:results").strip()
    dq_key = (os.getenv("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue").strip()

    r = redis.Redis(host=host, port=port, db=db, decode_responses=True)

    raw = r.lindex(results_key, -1)
    if not raw:
        print(f"[FATAL] no results in {results_key}")
        return 2

    last = json.loads(raw)
    if not isinstance(last, dict):
        print("[FATAL] last result not dict")
        return 2

    meta = last.get("meta") if isinstance(last.get("meta"), dict) else {}
    symbol = str(last.get("symbol") or "SPY").strip().upper() or "SPY"
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    job = {
        "type": "alert_trigger",
        "job_type": "alert_trigger",
        "job_id": f"alert_trigger:DEBUG:{meta.get('alert_id') or 'UNKNOWN'}:{symbol}:{now_utc}",
        "alert_id": meta.get("alert_id"),
        "symbol": symbol,
        "tf": "1m",
        "ts_utc": now_utc,
        "intent": meta.get("intent"),
        "event": meta.get("event"),
        "actions": meta.get("actions"),
    }

    res = _alert_trigger_job(job, r=r)
    print(f"[RES] ok={res.ok} type={res.type} symbol={res.symbol} job_id={res.job_id} out={res.out_path} err={res.error}")

    try:
        t = str(r.type(dq_key) or "none")
        n = int(r.llen(dq_key) or 0) if t == "list" else 0
        print(f"[DQ] key={dq_key} type={t} len={n}")
    except Exception as exc:
        print(f"[DQ][WARN] {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
