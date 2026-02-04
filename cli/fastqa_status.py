"""FAST_QA status builder for Discord ops.

Intentionally dependency-light: avoid importing delivery.discord_bot or worker/render code.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from delivery.fastqa_state import (
    FASTQA_NEVER_CALLS_HEAVY,
    estimate_fastqa_capacity_per_minute,
    fastqa_throttle_rate,
    get_fastqa_last,
    get_fastqa_slo_snapshot,
)


def _env_str(name: str, default: str = "") -> str:
    try:
        return str(os.getenv(name, default) or default).strip()
    except Exception:
        return str(default)


def _env_bool(name: str, default: str = "0") -> bool:
    return _env_str(name, default) == "1"


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env_str(name, str(default)) or str(default))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)) or str(default))
    except Exception:
        return int(default)


def _fmt_age_s(age_s: Optional[int]) -> str:
    if age_s is None:
        return "unknown"
    return f"{int(age_s)}s"


def _utc_iso(ts_utc: Optional[float]) -> str:
    if ts_utc is None:
        return "(none)"
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts_utc)))
    except Exception:
        return "(bad_ts)"


def _safe_json_load(s: Any) -> Optional[dict]:
    if s is None:
        return None
    if isinstance(s, (bytes, bytearray)):
        try:
            s = s.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if not isinstance(s, str):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _live_ctx_market_check(timeout_s: float = 0.25) -> Dict[str, Any]:
    """Best-effort ctx:market read with short timeouts (FAST lane)."""

    from services.redis_env import redis_client

    try:
        r = redis_client(timeout_s=timeout_s, decode_responses=True)
        raw = r.get("ctx:market")
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}",
        }

    mkt = _safe_json_load(raw)
    if not isinstance(mkt, dict):
        return {"ok": False, "error": "missing_or_bad_json"}

    ts_utc = mkt.get("ts_utc")
    try:
        now = int(time.time())
        ts_i = int(float(ts_utc)) if ts_utc is not None else None
        age_s = None if ts_i is None else max(0, now - ts_i)
    except Exception:
        age_s = None

    fut = mkt.get("futures") if isinstance(mkt.get("futures"), dict) else None
    fut_age = None
    fut_ok = False
    fut_degraded = False
    try:
        if isinstance(fut, dict):
            for k in ("hb_age_sec", "hb_age_s", "age_sec", "age_s"):
                if k in fut and fut.get(k) is not None:
                    fut_age = int(float(fut.get(k)))
                    break
            fut_degraded = bool(fut.get("degraded"))
            fut_ok = (not fut_degraded) and (fut_age is None or fut_age <= 120)
    except Exception:
        fut_ok = False

    return {
        "ok": True,
        "ctx_age_s": age_s,
        "futures_ok": bool(fut_ok),
        "futures_age_s": fut_age,
        "futures_degraded": bool(fut_degraded),
    }


def build_fastqa_status_message(*, check: bool = False) -> str:
    # FAST_QA is a behavior contract, not a remote dependency.
    ask_tnt_enabled = _env_bool("TNT_ASK_TNT_FREE_TEXT_ENABLED", "1")
    router_enabled = _env_bool("TNT_CHANNEL_ROUTER_ENABLED", "0")

    max_len = _env_int("TNT_ASK_TNT_FREE_TEXT_MAX_LEN", 0)
    heuristics_enabled = _env_bool("TNT_ASK_TNT_FREE_TEXT_HEURISTICS_ENABLED", "1")

    cooldown_s = _env_float("TNT_ASK_TNT_FREE_TEXT_MIN_INTERVAL_SEC", 20.0)
    dedupe_s = _env_float("TNT_ASK_TNT_FREE_TEXT_DEDUPE_SEC", 90.0)

    debug_fast = _env_bool("TNT_FAST_QA_DEBUG", "0")
    debug_log = _env_bool("TNT_FAST_QA_DEBUG_LOG", "0")
    admin_receipt_dm = bool(debug_fast)  # current behavior: DM receipt only when debug on

    # Router mapping (dependency-light).
    try:
        from delivery.channel_router import channel_purposes, ask_tnt_channel_id, router_enabled as _router_auto

        purposes = channel_purposes()
        ask_tnt_id = ask_tnt_channel_id()
        router_effective = bool(_router_auto())
    except Exception:
        purposes = {}
        ask_tnt_id = 0
        router_effective = bool(router_enabled)

    last = get_fastqa_last() or None

    slo_state, slo_p95_ms, slo_n = get_fastqa_slo_snapshot()
    cap = estimate_fastqa_capacity_per_minute(window_s=60)
    thr = fastqa_throttle_rate(window_s=300)

    lines: list[str] = []
    lines.append(
        "FAST_QA enabled: ON | mode: /ask=always, ask-tnt-free-text="
        + ("on" if ask_tnt_enabled else "off")
    )
    lines.append(
        "Ask-TNT routing: router="
        + ("on" if router_effective else "off")
        + f" | ASK_TNT_CHANNEL_ID={ask_tnt_id or 'unset'}"
    )
    if purposes:
        # keep compact: show ASK_TNT allowlist + count
        ask_ids = [cid for cid, p in purposes.items() if p == "ASK_TNT"]
        lines.append(f"Router allowlist: ASK_TNT={ask_ids or '[]'} | total_mapped={len(purposes)}")
    else:
        lines.append("Router allowlist: (router map empty)")

    lines.append(
        "Anti-spam: cooldown_s="
        + str(int(cooldown_s) if cooldown_s.is_integer() else cooldown_s)
        + " | dedupe_s="
        + str(int(dedupe_s) if dedupe_s.is_integer() else dedupe_s)
        + " | max_len="
        + (str(max_len) if max_len > 0 else "unset")
        + " | question_heuristics="
        + ("on" if heuristics_enabled else "off")
    )

    lines.append(
        "Debug: TNT_FAST_QA_DEBUG="
        + ("1" if debug_fast else "0")
        + " | admin_receipt_dm="
        + ("on" if admin_receipt_dm else "off")
        + " | TNT_FAST_QA_DEBUG_LOG="
        + ("1" if debug_log else "0")
    )

    p95_label = "unknown" if slo_p95_ms is None else f"{int(round(float(slo_p95_ms)))}ms"
    lines.append(f"SLO: state={slo_state} | p95={p95_label} | window_n={slo_n}")

    lines.append(f"Estimated capacity: ~{int(round(cap))} FAST_QA/min (last 60s)")
    if thr is None:
        lines.append("Throttle rate: unknown (last 5m)")
    else:
        lines.append(f"Throttle rate: {int(round(thr * 100.0))}% (last 5m)")

    lines.append(f"Hard assertion: fastqa_never_calls_heavy={FASTQA_NEVER_CALLS_HEAVY}")

    if last is None:
        lines.append("Last receipt: (none yet)")
    else:
        # compact last record
        lines.append(
            "Last receipt: ts="
            + _utc_iso(last.get("ts_utc"))
            + " | user=****"
            + str(last.get("user_id_last4") or "????")
            + " | ch="
            + str(last.get("channel_id") or "?")
            + " | route="
            + str(last.get("mode") or "?")
            + " | decision="
            + str(last.get("decision") or "?")
            + " | latency_ms="
            + (str(int(float(last.get("latency_ms")))) if last.get("latency_ms") is not None else "?")
        )
        lines.append(
            "  why="
            + str(last.get("reason") or "")[:140]
            + " | ctx_ok="
            + str(last.get("ctx_ok"))
            + " ctx_age="
            + _fmt_age_s(last.get("ctx_age_s"))
            + " | fut_ok="
            + str(last.get("futures_ok"))
            + " fut_age="
            + _fmt_age_s(last.get("futures_age_s"))
        )

    if check:
        chk = _live_ctx_market_check()
        if chk.get("ok"):
            lines.append(
                "Live check: ctx_age="
                + _fmt_age_s(chk.get("ctx_age_s"))
                + " | futures_ok="
                + str(chk.get("futures_ok"))
                + " fut_age="
                + _fmt_age_s(chk.get("futures_age_s"))
                + (" degraded=1" if chk.get("futures_degraded") else "")
            )
        else:
            lines.append(f"Live check: unavailable ({chk.get('error')})")

    # Keep message compact.
    return "\n".join(lines)[:1800]
