"""Quick status for alerts Discord delivery.

Prints queue type/len and last_posted markers written by cli.discord_bot.
"""

from __future__ import annotations

import os

import redis


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

    dq_key = (os.getenv("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue").strip()

    r = redis.Redis(host=host, port=port, db=db, decode_responses=True)

    t = str(r.type(dq_key) or "none")
    n = int(r.llen(dq_key) or 0) if t == "list" else 0

    last_job = str(r.get("tnt:alerts:discord:last_posted_job_id") or "")
    last_utc = str(r.get("tnt:alerts:discord:last_posted_utc") or "")
    last_ch = str(r.get("tnt:alerts:discord:last_posted_channel_id") or "")
    last_src = str(r.get("tnt:alerts:discord:last_posted_channel_source") or "")

    print(f"queue={dq_key} type={t} len={n}")
    print(f"last_posted_job_id={last_job}")
    print(f"last_posted_utc={last_utc}")
    print(f"last_posted_channel_id={last_ch}")
    print(f"last_posted_channel_source={last_src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
