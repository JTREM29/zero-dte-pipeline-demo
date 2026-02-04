from __future__ import annotations

import os


def _env_str(name: str, default: str) -> str:
    try:
        return str(os.getenv(name, default) or default).strip()
    except Exception:
        return str(default)


def main() -> int:
    from services.redis_env import redis_client, redis_env

    env = redis_env()
    r = redis_client(timeout_s=0.5, decode_responses=True)

    keys = [
        _env_str("TNT_REDIS_QUEUE", "tnt:jobs") or "tnt:jobs",
        _env_str("TNT_REDIS_RESULTS", "tnt:results") or "tnt:results",
        _env_str("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue",
    ]

    print(f"redis={env.host}:{env.port}/{env.db}")
    for k in keys:
        try:
            t = r.type(k)
        except Exception:
            t = "?"
        n = None
        if t == "list":
            try:
                n = int(r.llen(k) or 0)
            except Exception:
                n = None
        elif t == "none":
            n = 0
        else:
            # Not a list; still show type.
            n = None

        if n is None:
            print(f"{k}: type={t}")
        else:
            print(f"{k}: type={t} len={n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
