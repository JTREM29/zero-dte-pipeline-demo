from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LockHandle:
    key: str
    token: str


def acquire_lock(r: Any, key: str, *, ttl_sec: int) -> LockHandle | None:
    """Best-effort distributed lock.

    Uses Redis SET key value NX EX ttl.
    Returns a handle if acquired, else None.
    """

    token = secrets.token_hex(16)
    try:
        ok = r.set(str(key), token, nx=True, ex=int(ttl_sec))
    except TypeError:
        # Some fake redis implementations may not support named params.
        try:
            ok = r.set(str(key), token)
            if hasattr(r, "expire"):
                r.expire(str(key), int(ttl_sec))
        except Exception:
            ok = False
    except Exception:
        ok = False

    if ok:
        return LockHandle(key=str(key), token=str(token))
    return None


def release_lock(r: Any, handle: LockHandle) -> bool:
    """Release a lock if still owned by our token."""

    try:
        cur = r.get(str(handle.key))
        if cur == handle.token:
            r.delete(str(handle.key))
            return True
    except Exception:
        return False
    return False
