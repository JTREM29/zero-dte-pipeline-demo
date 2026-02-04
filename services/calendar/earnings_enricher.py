from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _safe_float(x: Any) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def _hours_to_event(ts_utc_iso: str) -> float | None:
    if not ts_utc_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_utc_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    except Exception:
        return None

    now = datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 3600.0


def _infer_crush_risk(*, iv_rank: float | None, front_iv: float | None) -> str:
    """Best-effort IV crush risk bucket.

    Heuristic: if IV rank is known, it dominates; else fall back to front_iv.
    """

    if iv_rank is not None:
        if iv_rank >= 0.8:
            return "HIGH"
        if iv_rank >= 0.55:
            return "MED"
        return "LOW"

    if front_iv is not None:
        if front_iv >= 0.70:
            return "HIGH"
        if front_iv >= 0.45:
            return "MED"
        return "LOW"

    return "MED"


def _proximity_score(*, hours_to_event: float | None, crush_risk: str, expected_move_pct: float | None) -> float | None:
    """0-100: how dangerous it is to trade today.

    Combines time-to-event (dominant) with IV crush risk and expected move.
    """

    if hours_to_event is None:
        return None

    # Time component: within 6h is max-danger.
    t = max(0.0, min(1.0, (72.0 - float(hours_to_event)) / 72.0))

    risk_bonus = {"LOW": 0.05, "MED": 0.15, "HIGH": 0.30}.get(str(crush_risk or "").upper(), 0.15)

    em = expected_move_pct or 0.0
    em_bonus = max(0.0, min(0.25, float(em) / 20.0))

    score = (t + risk_bonus + em_bonus) * 100.0
    return max(0.0, min(100.0, score))


def enrich_earnings_blob(blob: dict[str, Any]) -> dict[str, Any]:
    """Best-effort enrichment for the canonical `cal:earnings:{SYM}` blob.

    This function is intentionally defensive: it never raises.

    Populates/derives where possible:
    - iv_state.crush_risk
    - expected_move_pct (only if iv_state.front_iv is present; otherwise left null)
    - earnings_proximity_score
    """

    if not isinstance(blob, dict):
        return blob

    ts_utc = str(blob.get("ts_utc") or "")

    iv_state = blob.get("iv_state")
    if not isinstance(iv_state, dict):
        iv_state = {}

    iv_rank = _safe_float(iv_state.get("iv_rank"))
    front_iv = _safe_float(iv_state.get("front_iv"))

    crush_risk = str(iv_state.get("crush_risk") or "").strip().upper()
    if crush_risk not in {"LOW", "MED", "HIGH"}:
        crush_risk = _infer_crush_risk(iv_rank=iv_rank, front_iv=front_iv)
    iv_state["crush_risk"] = crush_risk

    expected_move_pct = _safe_float(blob.get("expected_move_pct"))
    if expected_move_pct is None and front_iv is not None:
        # Very rough proxy: use 1 trading day sigma from IV (annualized) => IV/sqrt(252).
        try:
            expected_move_pct = float(front_iv) / math.sqrt(252.0) * 100.0
        except Exception:
            expected_move_pct = None

    blob["iv_state"] = iv_state
    blob["expected_move_pct"] = expected_move_pct

    h = _hours_to_event(ts_utc)
    prox = _proximity_score(hours_to_event=h, crush_risk=crush_risk, expected_move_pct=expected_move_pct)
    blob["earnings_proximity_score"] = prox

    return blob
