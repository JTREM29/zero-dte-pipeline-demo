"""Replay the most recent alert_trigger result as a fresh job.

Purpose: give a deterministic, one-command end-to-end test:
- Push a synthetic alert_trigger job into TNT_REDIS_QUEUE
- Worker consumes it
- Worker enqueues it into TNT_ALERTS_DISCORD_QUEUE
- cli.discord_bot delivery loop posts to Discord

Safety: defaults to dry-run; requires --send to actually enqueue.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

import redis


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _redis_client() -> redis.Redis:
    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _load_last_result(r: redis.Redis, results_key: str) -> dict | None:
    raw = r.lindex(results_key, -1)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true", help="Actually enqueue (otherwise dry-run)")
    p.add_argument("--queue", default=(os.getenv("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs").strip())
    p.add_argument("--results", default=(os.getenv("TNT_REDIS_RESULTS", "tnt:results") or "tnt:results").strip())
    p.add_argument("--tf", default="1m", help="Fallback timeframe if missing")
    args = p.parse_args(argv)

    r = _redis_client()

    last = _load_last_result(r, args.results)
    if not last:
        print(f"[FATAL] No results in {args.results}")
        return 2

    if str(last.get("type") or "") != "alert_trigger":
        print(f"[FATAL] Last result is not alert_trigger: type={last.get('type')}")
        return 2

    if not bool(last.get("ok")):
        print(f"[FATAL] Last result is not ok: ok={last.get('ok')} error={last.get('error')}")
        return 2

    symbol = str(last.get("symbol") or "").strip().upper() or "SPY"
    meta = last.get("meta") if isinstance(last.get("meta"), dict) else {}

    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    job_id = f"alert_trigger:REPLAY:{meta.get('alert_id') or 'UNKNOWN'}:{symbol}:{now_utc}"

    job = {
        "type": "alert_trigger",
        "job_type": "alert_trigger",
        "job_id": job_id,
        "alert_id": meta.get("alert_id"),
        "symbol": symbol,
        "tf": str(meta.get("tf") or args.tf),
        "ts_utc": str((meta.get("event") or {}).get("ts_utc") if isinstance(meta.get("event"), dict) else "") or now_utc,
        "intent": meta.get("intent"),
        "event": meta.get("event"),
        "actions": meta.get("actions"),
        "replay": {"source": "tnt:results", "results_key": args.results, "ts_utc": now_utc},
    }

    payload = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
    if not args.send:
        print(f"[DRYRUN] Would RPUSH -> {args.queue} job_id={job_id} symbol={symbol}")
        return 0

    r.rpush(args.queue, payload)
    time.sleep(0.05)
    try:
        n = int(r.llen(args.queue) or 0)
    except Exception:
        n = -1

    print(f"[OK] Enqueued replay job -> {args.queue} llen={n} job_id={job_id} symbol={symbol}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
