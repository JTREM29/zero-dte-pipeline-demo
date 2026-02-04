from __future__ import annotations

import os
from typing import Any

from .calendar_keys import earnings_qcount_24h_zset_key


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def bump_earnings_query_count(r: Any, symbol: str, *, ttl_sec: int | None = None) -> None:
    """Increment rolling query count for a symbol.

    This is intentionally best-effort. If Redis errors, we silently ignore.

    TTL behavior: we apply a single key TTL that is refreshed on every bump.
    That approximates a rolling 24h window while staying very cheap.
    """

    sym = str(symbol or "").strip().upper()
    if not sym:
        return

    if ttl_sec is None:
        ttl_sec = _env_int("EARNINGS_QCOUNT_TTL_SEC", 24 * 3600)
    ttl = max(60, int(ttl_sec))

    if r is None or not hasattr(r, "zincrby"):
        return

    key = earnings_qcount_24h_zset_key()
    try:
        r.zincrby(key, 1.0, sym)
        # Keep TTL bounded.
        if hasattr(r, "expire"):
            r.expire(key, ttl)
    except Exception:
        return


def top_earnings_queries(
    r: Any,
    *,
    limit: int = 25,
    min_count: int = 1,
) -> list[tuple[str, int]]:
    """Return (symbol, count) tuples in descending count order."""

    n = max(1, min(250, int(limit)))
    minc = max(1, int(min_count))

    if r is None or not hasattr(r, "zrevrange"):
        return []

    key = earnings_qcount_24h_zset_key()
    try:
        raw = r.zrevrange(key, 0, n - 1, withscores=True)
    except Exception:
        return []

    out: list[tuple[str, int]] = []
    for row in raw or []:
        try:
            member, score = row
            sym = member.decode("utf-8", errors="replace") if isinstance(member, (bytes, bytearray)) else str(member)
            sym = sym.strip().upper()
            if not sym:
                continue
            c = int(float(score))
            if c < minc:
                continue
            out.append((sym, c))
        except Exception:
            continue

    return out
