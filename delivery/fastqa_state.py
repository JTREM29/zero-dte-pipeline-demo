"""FAST_QA in-memory state (operator visibility).

This module is intentionally dependency-light.
It must remain safe to import from both cli and delivery without pulling in heavy paths.
"""

from __future__ import annotations

from collections import deque
import threading
import time
from typing import Any, Deque, Dict, Optional, Tuple

# Hard invariant: FAST_QA must never touch heavy paths.
FASTQA_NEVER_CALLS_HEAVY: bool = True

_LOCK = threading.Lock()

# Last observed FAST_QA handling record (best-effort; in-memory only).
FAST_QA_LAST: Optional[Dict[str, Any]] = None

# Rolling SLO window (in-memory only).
_FAST_QA_WINDOW: Deque[Dict[str, Any]] = deque(maxlen=200)

_SLO_STATE: str = "UNKNOWN"  # OK/WARN/BAD/UNKNOWN
_SLO_P95_MS: float | None = None
_PENDING_CANARY: Optional[Dict[str, Any]] = None


def format_fastqa_throttle_message(wait_s: int) -> str:
    """User-facing instant throttle reply (FAST lane). Never mentions queue/waiting."""

    try:
        w = int(max(1, min(60, int(wait_s))))
    except Exception:
        w = 5
    # Keep it short; reduce frustration loops.
    return f"⚡ High traffic — try again in ~{w}s. Tip: one question at a time."


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    # percentile rank (ceil(0.95*n)-1)
    try:
        n = len(v)
        idx = int((0.95 * n) - 1)
        if idx < 0:
            idx = 0
        if idx >= n:
            idx = n - 1
        return float(v[idx])
    except Exception:
        return None


def _classify_slo(*, p95_ms: float | None, queued_detected: bool) -> str:
    if queued_detected:
        return "BAD"
    if p95_ms is None:
        return "UNKNOWN"
    if p95_ms <= 400.0:
        return "OK"
    if p95_ms <= 800.0:
        return "WARN"
    return "BAD"


def record_fastqa_event(
    *,
    ts_utc: float | None = None,
    decision: str,
    latency_ms: float | None = None,
    output_text: str | None = None,
) -> None:
    """Record a FAST_QA event for SLO tracking.

    This is safe to call from the FAST lane: O(window log window) worst-case.
    Canary notifications are only generated on state transitions.
    """

    now_ts = float(ts_utc) if ts_utc is not None else _now_utc_ts()
    out = str(output_text or "")
    queued = "queued" in out.lower() if out else False

    item: Dict[str, Any] = {
        "ts_utc": now_ts,
        "decision": str(decision or "").strip() or "UNKNOWN",
        "latency_ms": float(latency_ms) if latency_ms is not None else None,
        "queued": bool(queued),
        "throttled": str(decision).upper().startswith("THROTTLED"),
    }

    with _LOCK:
        _FAST_QA_WINDOW.append(item)

        latencies: list[float] = []
        any_queued = False
        for e in _FAST_QA_WINDOW:
            if bool(e.get("queued")):
                any_queued = True
            lm = e.get("latency_ms")
            if lm is None:
                continue
            try:
                latencies.append(float(lm))
            except Exception:
                continue

        p95_ms = _p95(latencies)
        state = _classify_slo(p95_ms=p95_ms, queued_detected=any_queued)

        global _SLO_STATE, _SLO_P95_MS, _PENDING_CANARY
        prev = _SLO_STATE
        _SLO_STATE = state
        _SLO_P95_MS = p95_ms

        # Canary: post only when crossing thresholds.
        if prev != state and state in {"OK", "WARN", "BAD"}:
            # Keep canary compact and actionable.
            p95_label = "unknown" if p95_ms is None else f"{int(round(p95_ms))}ms"
            reason = "queued_substring_detected" if any_queued else "p95_latency"
            _PENDING_CANARY = {
                "ts_utc": now_ts,
                "state": state,
                "prev_state": prev,
                "p95_ms": p95_ms,
                "reason": reason,
                "window_n": len(_FAST_QA_WINDOW),
            }


def take_pending_fastqa_canary() -> Optional[Dict[str, Any]]:
    """Return and clear any pending canary notice (state transition)."""

    with _LOCK:
        global _PENDING_CANARY
        if _PENDING_CANARY is None:
            return None
        msg = dict(_PENDING_CANARY)
        _PENDING_CANARY = None
        return msg


def get_fastqa_slo_snapshot() -> Tuple[str, float | None, int]:
    """Return (state, p95_ms, window_n)."""

    with _LOCK:
        return _SLO_STATE, _SLO_P95_MS, len(_FAST_QA_WINDOW)


def estimate_fastqa_capacity_per_minute(*, window_s: int = 60) -> float:
    """Estimate current FAST_QA throughput as events per minute over last window_s."""

    try:
        w = int(max(10, min(300, int(window_s))))
    except Exception:
        w = 60

    now_ts = _now_utc_ts()
    with _LOCK:
        n = 0
        for e in _FAST_QA_WINDOW:
            try:
                if (now_ts - float(e.get("ts_utc") or 0.0)) <= float(w):
                    # Count handled events (exclude ignored noise if it ever leaks here).
                    n += 1
            except Exception:
                continue
    return (float(n) * 60.0) / float(w)


def fastqa_throttle_rate(*, window_s: int = 300) -> float | None:
    """Return throttled/total over the last window_s seconds."""

    try:
        w = int(max(60, min(1800, int(window_s))))
    except Exception:
        w = 300

    now_ts = _now_utc_ts()
    total = 0
    throttled = 0
    with _LOCK:
        for e in _FAST_QA_WINDOW:
            try:
                if (now_ts - float(e.get("ts_utc") or 0.0)) > float(w):
                    continue
                total += 1
                if bool(e.get("throttled")):
                    throttled += 1
            except Exception:
                continue

    if total <= 0:
        return None
    return float(throttled) / float(total)


def _now_utc_ts() -> float:
    try:
        return time.time()
    except Exception:
        return 0.0


def update_fastqa_last(
    *,
    ts_utc: float | None = None,
    channel_id: int | None = None,
    user_id: int | None = None,
    mode: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
    latency_ms: float | None = None,
    ctx_ok: bool | None = None,
    ctx_age_s: int | None = None,
    futures_ok: bool | None = None,
    futures_age_s: int | None = None,
) -> None:
    """Update global FAST_QA_LAST with a compact, privacy-safe record."""

    safe_user_last4: str | None = None
    try:
        if user_id is not None:
            safe_user_last4 = str(int(user_id))[-4:]
    except Exception:
        safe_user_last4 = None

    record: Dict[str, Any] = {
        "ts_utc": float(ts_utc) if ts_utc is not None else _now_utc_ts(),
        "channel_id": int(channel_id) if channel_id is not None else None,
        "user_id_last4": safe_user_last4,
        "mode": (mode or "").strip() or None,
        "decision": (decision or "").strip() or None,
        "reason": (reason or "").strip() or None,
        "latency_ms": float(latency_ms) if latency_ms is not None else None,
        "ctx_ok": bool(ctx_ok) if ctx_ok is not None else None,
        "ctx_age_s": int(ctx_age_s) if ctx_age_s is not None else None,
        "futures_ok": bool(futures_ok) if futures_ok is not None else None,
        "futures_age_s": int(futures_age_s) if futures_age_s is not None else None,
    }

    with _LOCK:
        global FAST_QA_LAST
        FAST_QA_LAST = record


def get_fastqa_last() -> Optional[Dict[str, Any]]:
    with _LOCK:
        if FAST_QA_LAST is None:
            return None
        # shallow copy to avoid accidental mutation by callers
        return dict(FAST_QA_LAST)


def _reset_fastqa_state_for_tests() -> None:  # pragma: no cover
    """Reset in-memory FAST_QA state (tests only)."""

    with _LOCK:
        global FAST_QA_LAST, _SLO_STATE, _SLO_P95_MS, _PENDING_CANARY
        FAST_QA_LAST = None
        _FAST_QA_WINDOW.clear()
        _SLO_STATE = "UNKNOWN"
        _SLO_P95_MS = None
        _PENDING_CANARY = None
