from __future__ import annotations

import time
from typing import Any

from services.context.ctx_mode import ctx_enabled, ctx_strict_mode


def record_ctx_miss(r: Any, consumer: str, *, sym: str = "", why: str = "") -> None:
    """Record a ctx snapshot miss (best-effort).

    Spec:
    INCR ctx:miss:{consumer}
      SETEX ctx:miss_last 86400 "{ts}|{mode}|{consumer}|{sym}|{why}"
      SETEX ctx:miss_last:{consumer} 86400 "{ts}|{mode}|{consumer}|{sym}|{why}"

    Only when CTX_SNAPSHOT_ENABLED=1.
    """

    if not ctx_enabled():
        return

    if r is None:
        return

    who = str(consumer or "").strip() or "unknown"
    symbol = str(sym or "").strip().upper()
    reason = str(why or "").strip()
    mode = ctx_strict_mode()
    now_s = int(time.time())
    sample = f"{now_s}|{mode}|{who}|{symbol}|{reason}"[:240]

    try:
        if hasattr(r, "incr"):
            r.incr(f"ctx:miss:{who}")
    except Exception:
        pass

    try:
        if hasattr(r, "setex"):
            r.setex("ctx:miss_last", 86400, sample)
            r.setex(f"ctx:miss_last:{who}", 86400, sample)
        else:
            r.set("ctx:miss_last", sample)
            r.set(f"ctx:miss_last:{who}", sample)
    except Exception:
        pass
