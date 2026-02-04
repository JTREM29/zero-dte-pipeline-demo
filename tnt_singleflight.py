from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from tnt_redis import redis_client, rkey

T = TypeVar("T")


@dataclass(frozen=True)
class SingleFlightCfg:
    lock_ttl_s: int = 60
    wait_timeout_s: int = 25
    poll_ms: int = 250


def singleflight(
    namespace: str,
    work_key: str,
    cfg: SingleFlightCfg,
    compute: Callable[[], T],
    read_result: Callable[[], Optional[T]],
    write_result: Callable[[T], None],
) -> T:
    """
    - Try read_result() first (cache hit)
    - Otherwise acquire a redis lock
      - owner computes and writes result
      - followers poll until result appears or timeout, then best-effort compute
    """
    r = redis_client()
    lock_key = rkey("sf", namespace, work_key, "lock")

    existing = read_result()
    if existing is not None:
        return existing

    got_lock = r.set(lock_key, "1", nx=True, ex=cfg.lock_ttl_s)
    if got_lock:
        result = compute()
        write_result(result)
        return result

    deadline = time.time() + cfg.wait_timeout_s
    while time.time() < deadline:
        existing = read_result()
        if existing is not None:
            return existing
        time.sleep(cfg.poll_ms / 1000.0)

    # Fallback: best-effort compute (rare; prevents dead UX)
    result = compute()
    write_result(result)
    return result
