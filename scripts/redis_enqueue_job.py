from __future__ import annotations

import argparse
import json
import os
import sys
import uuid


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Enqueue a TNT render job into Redis (Controller-side).")
    p.add_argument("--redis-host", default=os.getenv("TNT_REDIS_HOST", "127.0.0.1"))
    p.add_argument("--redis-port", type=int, default=_env_int("TNT_REDIS_PORT", 6379))
    p.add_argument("--redis-db", type=int, default=_env_int("TNT_REDIS_DB", 0))
    p.add_argument("--queue", default=os.getenv("TNT_REDIS_QUEUE", "tnt:jobs"))

    p.add_argument("--type", default="render_oi", choices=["render_oi"], help="Job type")
    p.add_argument("--symbol", required=True)
    p.add_argument("--expiry", default=os.getenv("TNT_OI_EXPIRY", ""), help="YYYY-MM-DD (optional; defaults to today ET)")
    p.add_argument("--top", type=int, default=_env_int("TNT_OI_TOP", 18))
    p.add_argument("--dpi", type=int, default=_env_int("TNT_OI_DPI", 130))
    p.add_argument(
        "--reply-path",
        default=os.getenv("TNT_ARTIFACTS_SHARE", ""),
        help=r"UNC folder path like \\CONTROLLER\tnt_artifacts\\",
    )

    args = p.parse_args(argv)

    if not args.reply_path:
        print("[FATAL] Missing --reply-path (or TNT_ARTIFACTS_SHARE)")
        return 2

    job = {
        "job_id": str(uuid.uuid4()),
        "type": str(args.type),
        "symbol": str(args.symbol).strip().upper(),
        "params": {"expiry": (str(args.expiry).strip() or None), "top": int(args.top), "dpi": int(args.dpi)},
        "reply": {"mode": "shared_folder", "path": str(args.reply_path)},
    }

    try:
        import redis  # type: ignore
    except Exception as exc:
        print(f"[FATAL] redis not installed: {type(exc).__name__}: {exc}")
        return 2

    r = redis.Redis(host=args.redis_host, port=int(args.redis_port), db=int(args.redis_db), decode_responses=True)
    payload = json.dumps(job, separators=(",", ":"), ensure_ascii=False)

    try:
        r.rpush(str(args.queue), payload)
    except Exception as exc:
        print(f"[FATAL] Failed to enqueue job: {type(exc).__name__}: {exc}")
        return 1

    print(f"[OK] Enqueued {job['type']} job_id={job['job_id']} symbol={job['symbol']} -> {args.queue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
