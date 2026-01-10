from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from .templates import LockState


@dataclass(frozen=True)
class DecisionLock:
    state: LockState
    reason: str
    ts: float


# Keyed by (guild_id, channel_id)
_locks: dict[tuple[int, int], DecisionLock] = {}


def _log_enabled() -> bool:
    return (os.getenv("TNT_DECISION_LOCK_LOG", "1") or "1") == "1"


def _ttl_sec() -> float:
    raw = (os.getenv("TNT_DECISION_LOCK_TTL_SEC", "900") or "").strip()
    try:
        v = float(raw)
    except Exception:
        v = 900.0
    return max(1.0, v)


def set_lock(
    *,
    guild_id: Optional[int],
    channel_id: Optional[int],
    state: LockState,
    reason: str,
    now_ts: Optional[float] = None,
) -> None:
    if guild_id is None or channel_id is None:
        return
    ts = float(time.time() if now_ts is None else now_ts)
    key = (int(guild_id), int(channel_id))
    prev = _locks.get(key)
    _locks[key] = DecisionLock(state=state, reason=str(reason or ""), ts=ts)

    if not _log_enabled():
        return

    try:
        if prev is None:
            print(f"decision_lock_set level={state.value} guild={key[0]} channel={key[1]}")
        else:
            age = max(0, int(round(ts - float(prev.ts))))
            if prev.state == state:
                print(
                    f"decision_lock_refreshed level={state.value} guild={key[0]} channel={key[1]} age={age}s"
                )
            else:
                print(
                    f"decision_lock_overwritten prev={prev.state.value} new={state.value} guild={key[0]} channel={key[1]} age={age}s"
                )
    except Exception:
        return


def get_lock(
    *,
    guild_id: Optional[int],
    channel_id: Optional[int],
    now_ts: Optional[float] = None,
) -> Optional[DecisionLock]:
    if guild_id is None or channel_id is None:
        return None

    key = (int(guild_id), int(channel_id))
    lock = _locks.get(key)
    if lock is None:
        return None

    ts = float(time.time() if now_ts is None else now_ts)
    if (ts - float(lock.ts)) > _ttl_sec():
        if _log_enabled():
            try:
                age = max(0, int(round(ts - float(lock.ts))))
                print(
                    f"decision_lock_expired level={lock.state.value} guild={key[0]} channel={key[1]} age={age}s"
                )
            except Exception:
                pass
        try:
            _locks.pop(key, None)
        except Exception:
            pass
        return None

    return lock


def _reset_state_for_tests() -> None:
    _locks.clear()
