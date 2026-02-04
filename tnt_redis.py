from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import redis


@dataclass(frozen=True)
class RedisCfg:
    host: str
    port: int
    db: int
    password: Optional[str]
    prefix: str


def get_cfg() -> RedisCfg:
    return RedisCfg(
        host=os.getenv("TNT_REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("TNT_REDIS_PORT", "6379")),
        db=int(os.getenv("TNT_REDIS_DB", "0")),
        password=os.getenv("TNT_REDIS_PASSWORD") or None,
        prefix=os.getenv("TNT_REDIS_PREFIX", "tnt"),
    )


def rkey(*parts: str) -> str:
    cfg = get_cfg()
    safe = [p.replace(" ", "_") for p in parts]
    return f"{cfg.prefix}:" + ":".join(safe)


_client: Optional[redis.Redis] = None


def redis_client() -> redis.Redis:
    global _client
    if _client is not None:
        return _client

    cfg = get_cfg()
    _client = redis.Redis(
        host=cfg.host,
        port=cfg.port,
        db=cfg.db,
        password=cfg.password,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
        retry_on_timeout=True,
        health_check_interval=30,
    )
    _client.ping()  # fail-fast if misconfigured
    return _client


def now_ms() -> int:
    return int(time.time() * 1000)
