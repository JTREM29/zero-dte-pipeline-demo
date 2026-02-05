from __future__ import annotations

import os
import time
from typing import Any


XL_SYMBOLS = {"SPY", "QQQ", "IWM"}


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v.strip() if v else default


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def pick_worker_url(symbol: str, payload: dict[str, Any] | None = None) -> str:
    """Pick a worker base URL based on symbol + request breadth.

    Routing rules:
      1) SPY/QQQ/IWM -> XL worker
      2) Any request that asks for broader fanout -> XL worker
      3) Otherwise -> round-robin between XL and worker_2 (if configured)
      4) If worker_2 missing -> XL worker

    Env:
      - TNT_WORKER_URL_XL (preferred)
      - TNT_WORKER_URL_2 (optional overflow)
      - TNT_WORKER_URL (legacy fallback)

    Notes:
      - This function is controller-side only; it does not probe worker health.
      - Returned URL is not guaranteed normalized; callers may strip trailing '/'.
    """

    payload = payload or {}
    sym = (symbol or "").upper().strip()

    # Preferred: XL URL (fallback to existing TNT_WORKER_URL).
    xl = _env("TNT_WORKER_URL_XL") or _env("TNT_WORKER_URL")
    w2 = _env("TNT_WORKER_URL_2")

    # If nothing is configured, return empty and let callers error/fallback.
    if not xl:
        return _env("TNT_WORKER_URL")

    # Heuristic: "broader fanout requested" (only if payload includes these keys).
    # This is intentionally permissive so future payload expansions auto-route.
    req_exp = _as_int(payload.get("max_expiries"), 0)
    req_win = _as_int(payload.get("strike_window"), 0)
    req_top = _as_int(payload.get("top_n"), 0)

    # Alternative knobs used elsewhere in the codebase.
    req_max_contracts = _as_int(payload.get("max_contracts"), 0)
    req_window_pct = _as_int(payload.get("window_pct"), 0)
    req_window_abs = _as_float(payload.get("window_abs"), 0.0)

    breadth_requested = (
        (req_exp >= 9)
        or (req_win >= 16)
        or (req_top >= 36)
        or (req_max_contracts >= 220)
        or (req_window_pct >= 16)
        or (req_window_abs >= 20.0)
    )

    if sym in XL_SYMBOLS or breadth_requested:
        return xl

    if not w2:
        return xl

    # Simple stable round robin (no shared state required).
    return w2 if (int(time.time()) % 2 == 0) else xl
