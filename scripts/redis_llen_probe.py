from __future__ import annotations

import os
import time
from datetime import datetime, timezone


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw) if raw is not None else int(default)
    except Exception:
        return int(default)


def _redis_client():
    import redis  # type: ignore

    host = (os.getenv("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1").strip()
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _snap(r, tag: str) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _size(key: str) -> tuple[str, int]:
        t = str(r.type(key) or "none")
        try:
            if t == "list":
                return (t, int(r.llen(key)))
            if t == "stream":
                return (t, int(r.xlen(key)))
            if t == "zset":
                return (t, int(r.zcard(key)))
            if t == "set":
                return (t, int(r.scard(key)))
            if t == "hash":
                return (t, int(r.hlen(key)))
        except Exception:
            pass
        return (t, 0)

    jobs_key = (os.getenv("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs").strip()
    discord_queue_key = (os.getenv("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue").strip()
    results_key = (os.getenv("TNT_REDIS_RESULTS", "tnt:results") or "tnt:results").strip()

    jobs_t, jobs_n = _size(jobs_key)
    dq_t, dq_n = _size(discord_queue_key)
    res_t, res_n = _size(results_key)
    print(
        f"{tag} utc={now} "
        f"{jobs_key} ({jobs_t})={jobs_n} "
        f"{discord_queue_key} ({dq_t})={dq_n} "
        f"{results_key} ({res_t})={res_n}"
    )


def main() -> int:
    r = _redis_client()
    _snap(r, "A")
    time.sleep(10)
    _snap(r, "B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
