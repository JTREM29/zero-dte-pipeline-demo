from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(x: Any) -> str:
    return str(x).strip() if x is not None else ""


def _safe_bool(x: Any) -> bool:
    return bool(x)


def normalize_session(value: Any) -> str:
    s = _safe_str(value).upper()
    if s in {"BMO", "AMC", "DURING", "UNKNOWN"}:
        return s
    return "UNKNOWN"


def normalize_source(value: Any) -> str:
    s = _safe_str(value).lower()
    if not s:
        return "cache"
    # Keep source string flexible but normalized.
    return s


def _normalize_blackout(value: Any, *, default_pre: int = 45, default_post: int = 30) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"pre_min": int(default_pre), "post_min": int(default_post)}
    try:
        pre = int(value.get("pre_min", default_pre))
    except Exception:
        pre = int(default_pre)
    try:
        post = int(value.get("post_min", default_post))
    except Exception:
        post = int(default_post)
    return {"pre_min": max(0, pre), "post_min": max(0, post)}


def _normalize_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for it in value:
        if not isinstance(it, dict):
            continue
        # Keep shape flexible; embed layer will be defensive.
        out.append(dict(it))
        if len(out) >= 4:
            break
    return out


def normalize_earnings_blob(symbol: str, blob: dict[str, Any]) -> dict[str, Any]:
    """Normalize canonical `cal:earnings:{SYM}` JSON blob.

    Backward compatible: if caller provides only {ts_utc, confirmed}, we fill the rest.
    """

    sym = _safe_str(symbol).upper()
    if not sym:
        return {}

    src = dict(blob) if isinstance(blob, dict) else {}

    ts_utc = _safe_str(src.get("ts_utc"))
    confirmed = _safe_bool(src.get("confirmed", False))

    out: dict[str, Any] = {
        "symbol": sym,
        "ts_utc": ts_utc,
        "session": normalize_session(src.get("session")),
        "confirmed": confirmed,
        "source": normalize_source(src.get("source")),
        "refreshed_utc": _safe_str(src.get("refreshed_utc")) or _now_utc_iso(),
        "blackout": _normalize_blackout(src.get("blackout")),
        # Enrichment payloads (best-effort).
        "estimates": src.get("estimates") if isinstance(src.get("estimates"), dict) else {},
        "history": _normalize_history(src.get("history")),
        "expected_move_pct": src.get("expected_move_pct"),
        "iv_state": src.get("iv_state") if isinstance(src.get("iv_state"), dict) else {},
        "liquidity_risk": _safe_str(src.get("liquidity_risk")).upper() or "MED",
        "notes": list(src.get("notes") or []) if isinstance(src.get("notes"), list) else [],
    }

    # Allow additional free-form keys to survive (future-proofing).
    for k, v in src.items():
        if k not in out:
            out[k] = v

    return out
