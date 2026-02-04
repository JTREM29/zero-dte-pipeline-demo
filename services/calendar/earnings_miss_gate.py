from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def should_show_retrieving_once(r, symbol: str, *, ttl_sec: int | None = None) -> bool:
    """Return True only on the first "missing earnings payload" hit per symbol.

    Uses Redis best-effort to prevent repeating the "I'm retrieving…" message
    indefinitely for symbols where earnings data is genuinely missing/sparse.

    If Redis is unavailable or errors, falls back to returning True.
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return True

    if ttl_sec is None:
        ttl_sec = _env_int("TNT_EARNINGS_FIRST_MISS_TTL_SEC", 6 * 3600)
    ttl_sec = max(60, int(ttl_sec))

    if r is None or not hasattr(r, "get") or not hasattr(r, "setex"):
        return True

    key = f"tnt:earn:first_miss:{sym}"
    try:
        existing = r.get(key)
        if existing is not None:
            return False
    except Exception:
        return True

    try:
        r.setex(key, ttl_sec, "1")
    except Exception:
        return True

    return True
