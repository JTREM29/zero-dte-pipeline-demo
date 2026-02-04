from __future__ import annotations

"""Small, dependency-light Redis env resolver.

Shared by bot + writers to avoid "same keys, different Redis" surprises.

Env surface:
- TNT_REDIS_HOST (default 127.0.0.1)
- TNT_REDIS_PORT (default 6379)
- TNT_REDIS_DB   (default 0)

This module intentionally does NOT import heavy runtime modules.
"""

import os
from dataclasses import dataclass
from typing import Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def _env_str(name: str, default: str) -> str:
    try:
        return str(os.getenv(name, default) or default).strip()
    except Exception:
        return str(default)


@dataclass(frozen=True)
class RedisEnv:
    host: str
    port: int
    db: int


def redis_env() -> RedisEnv:
    host = _env_str("TNT_REDIS_HOST", "127.0.0.1") or "127.0.0.1"
    port = _env_int("TNT_REDIS_PORT", 6379)
    db = _env_int("TNT_REDIS_DB", 0)
    return RedisEnv(host=host, port=port, db=db)


def redis_client(*, timeout_s: Optional[float] = None, decode_responses: bool = True):
    """Create a redis client from env, optionally with short timeouts.

    When timeout_s is provided, it's used for both connect + socket timeouts.
    """

    try:
        import redis  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("redis package is required") from exc

    env = redis_env()
    kwargs = {
        "host": env.host,
        "port": int(env.port),
        "db": int(env.db),
        "decode_responses": bool(decode_responses),
    }
    if timeout_s is not None:
        try:
            t = float(timeout_s)
        except Exception:
            t = None
        if t is not None and t > 0:
            kwargs["socket_connect_timeout"] = t
            kwargs["socket_timeout"] = t

    return redis.Redis(**kwargs)
