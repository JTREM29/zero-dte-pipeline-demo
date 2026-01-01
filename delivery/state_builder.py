"""Canonical TNT_STATE builder (v1.0).

This module builds the single JSON object that is injected into the LLM context.
The agent should only see:
- `tnt_system_prompt.txt`
- `TNT_STATE` JSON (v1.0)
- user question (if on-demand)

It intentionally keeps vendor-specific payloads out of the model context.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence

from zoneinfo import ZoneInfo

from delivery import market_data_adapter as mda
from delivery.tnt_state import build_tnt_state_from_packet

ET = ZoneInfo("America/New_York")


def _now_et(now: Optional[datetime] = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(ET)


def _coerce_float(val: object) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:  # noqa: BLE001
        return None


def _coerce_str(val: object) -> Optional[str]:
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _bias_from_prob(prob_up: Optional[float]) -> str:
    if prob_up is None:
        return "NEUTRAL"
    if prob_up >= 0.53:
        return "BULLISH"
    if prob_up <= 0.47:
        return "BEARISH"
    return "NEUTRAL"


def _conviction_from_edge(edge: Optional[float]) -> str:
    if edge is None:
        return "LOW"
    try:
        e = float(edge)
    except Exception:  # noqa: BLE001
        return "LOW"
    if e >= 0.08:
        return "HIGH"
    if e >= 0.05:
        return "MEDIUM"
    return "LOW"


_ALLOWED_VIX_TREND = {"UP", "DOWN", "FLAT", "UNKNOWN"}


def _bucket_trend(last: Optional[float], prev: Optional[float], *, flat_bps: float = 10.0) -> str:
    """Bucket trend based on basis points move between two levels."""

    if last is None or prev is None or last <= 0 or prev <= 0:
        return "UNKNOWN"

    rel_bps = ((last - prev) / prev) * 10_000.0
    if abs(rel_bps) <= flat_bps:
        return "FLAT"
    return "UP" if rel_bps > 0 else "DOWN"


_ALLOWED_SPX_CONTEXT = {
    "ABOVE_R1",
    "ABOVE_P",
    "NEAR_P",
    "BELOW_P",
    "BELOW_S1",
    "UNKNOWN",
}


def _bucket_spx_context(last: Optional[float], pivots: Optional[Mapping[str, Any]]) -> str:
    if last is None or not isinstance(pivots, Mapping):
        return "UNKNOWN"

    try:
        P = _coerce_float(pivots.get("P"))
        R1 = _coerce_float(pivots.get("R1"))
        S1 = _coerce_float(pivots.get("S1"))
    except Exception:  # noqa: BLE001
        return "UNKNOWN"

    if P is None:
        return "UNKNOWN"

    # Treat a tiny band around pivot as "near".
    if abs(last - P) <= max(0.001 * P, 0.5):
        return "NEAR_P"

    if R1 is not None and last >= R1:
        return "ABOVE_R1"
    if S1 is not None and last <= S1:
        return "BELOW_S1"
    if last > P:
        return "ABOVE_P"
    return "BELOW_P"


def build_tnt_state(
    symbols: Sequence[str],
    *,
    mode: str = "REALTIME",
    now: Optional[datetime] = None,
    adapter: Any = None,
    signal_override: Optional[Mapping[str, Any]] = None,
    options_focus_lines: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build TNT_STATE (v1.0) for the requested symbols.

    - `symbols`: ordered list; first symbol is the primary.
    - `mode`: a label for `meta.source.mode` (e.g., REALTIME, ON_DEMAND).

    `signal_override` is a test hook. Expected keys:
    - prob_up (float)
    - edge (float)
    - regime (str)

    `options_focus_lines` allows passing already-sanitized bucket lines.
    """

    now_et = _now_et(now)
    sym_list = [str(s).upper() for s in (symbols or []) if str(s or "").strip()]
    primary = sym_list[0] if sym_list else "SPY"

    a = adapter or mda

    coverage: Dict[str, bool] = {
        "price": False,
        "pivots": False,
        "signal": False,
        "options_environment": False,
        "indices_advanced": False,
    }

    # 1) Options environment (contract-safe buckets only)
    options_env = a.get_options_environment(options_focus_lines=options_focus_lines, now=now_et)
    coverage["options_environment"] = bool(options_env)

    # 2) Indices advanced crosschecks (bucketed)
    vix_snap = a.get_index_snapshot("VIX", now=now_et)
    vix_last = _coerce_float(vix_snap.get("last"))
    # We do not guarantee prev-close availability in v1; use a conservative placeholder.
    # Callers can wire a richer provider later.
    vix_trend = "UNKNOWN"
    if isinstance(vix_snap, Mapping):
        # If a provider adds prev_close later, we will bucket it.
        prev_close = _coerce_float(vix_snap.get("prev_close"))
        vix_trend = _bucket_trend(vix_last, prev_close)

    spx_snap = a.get_index_snapshot("SPX", now=now_et)
    spx_last = _coerce_float(spx_snap.get("last"))
    spx_piv = a.get_pivots("SPX")
    spx_piv_vals = spx_piv.get("piv") if isinstance(spx_piv, Mapping) else None
    spx_context = _bucket_spx_context(spx_last, spx_piv_vals if isinstance(spx_piv_vals, Mapping) else None)

    if vix_trend not in _ALLOWED_VIX_TREND:
        vix_trend = "UNKNOWN"
    if spx_context not in _ALLOWED_SPX_CONTEXT:
        spx_context = "UNKNOWN"

    indices = {
        "vix_trend": vix_trend,
        "spx_context": spx_context,
        "vix_last": vix_last,
        "spx_last": spx_last,
        "asof_et": now_et.isoformat(),
        "source": {"provider": "adapter", "detail": "bucketed"},
    }
    coverage["indices_advanced"] = True

    # 3) Equity snapshot + pivots normalization
    snap = a.get_equity_snapshot(primary, now=now_et)
    last = _coerce_float(snap.get("last"))
    ts_iso = _coerce_str(snap.get("ts_iso"))
    src = (snap.get("source") or {}) if isinstance(snap.get("source"), Mapping) else {}
    src_detail = _coerce_str(src.get("detail")) or "unknown"

    piv = a.get_pivots(primary)
    piv_vals = piv.get("piv") if isinstance(piv, Mapping) else None

    P = _coerce_float(piv_vals.get("P")) if isinstance(piv_vals, Mapping) else None
    r1 = _coerce_float(piv_vals.get("R1")) if isinstance(piv_vals, Mapping) else None
    r2 = _coerce_float(piv_vals.get("R2")) if isinstance(piv_vals, Mapping) else None
    s1 = _coerce_float(piv_vals.get("S1")) if isinstance(piv_vals, Mapping) else None
    s2 = _coerce_float(piv_vals.get("S2")) if isinstance(piv_vals, Mapping) else None

    coverage["price"] = last is not None
    coverage["pivots"] = P is not None

    # Signal (optional): use DB if available, unless test override provided.
    prob_up: Optional[float] = None
    edge: Optional[float] = None
    regime: Optional[str] = None

    if signal_override is not None:
        prob_up = _coerce_float(signal_override.get("prob_up"))
        edge = _coerce_float(signal_override.get("edge"))
        regime = _coerce_str(signal_override.get("regime"))
        coverage["signal"] = bool(prob_up is not None or edge is not None or regime)
    else:
        try:
            from delivery.discord_bot_head import get_latest_signal

            sig = get_latest_signal(primary)
            if sig:
                _ts, prob_up_val, _prob_dn, _model, edge_val, meta = sig
                prob_up = _coerce_float(prob_up_val)
                edge = _coerce_float(edge_val)
                if isinstance(meta, Mapping):
                    regime = _coerce_str(meta.get("regime")) or _coerce_str(meta.get("pivot_regime"))
                coverage["signal"] = True
        except Exception:  # noqa: BLE001
            pass

    bias = _bias_from_prob(prob_up)
    conviction = _conviction_from_edge(edge)

    # Use a conservative posture regime; prefer signal regime when it maps cleanly.
    # Unknown values will map to COMPRESSION inside `delivery.tnt_state`.
    regime_value = regime or "TREND"

    # Compute staleness for TNT_STATE.
    staleness_s = None
    if ts_iso:
        try:
            dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            staleness_s = max((now_et.astimezone(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds(), 0.0)
        except Exception:  # noqa: BLE001
            staleness_s = None

    packet: Dict[str, Any] = {
        "symbol": primary,
        "current_price": last,
        "current_price_source": f"adapter:{src_detail}",
        "current_price_ts": ts_iso,
        "pivot": P,
        "r_levels": {"R1": r1, "R2": r2},
        "s_levels": {"S1": s1, "S2": s2},
        "bias": bias,
        "conviction": conviction,
        "regime": regime_value,
        "notes": {
            "session": "RTH" if 9 <= now_et.hour <= 16 else "UNKNOWN",
            "data_staleness_s": staleness_s,
        },
        "trade_context_mode": mode,
    }

    result = build_tnt_state_from_packet(
        packet,
        now=now_et,
        source_provider="market_data_adapter",
        source_mode=mode,
    )

    state = result.state

    # Extend TNT_STATE with additional safe context.
    meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
    meta["coverage"] = coverage
    state["meta"] = meta

    state["indices"] = indices
    state["options_environment"] = options_env

    # Additional compact freshness signals (bounded and deterministic).
    def _fresh_entry(sym: str, snap: Mapping[str, Any], *, key: str) -> Dict[str, Any]:
        src = snap.get("source") if isinstance(snap.get("source"), Mapping) else {}
        return {
            "key": key,
            "symbol": sym,
            "ts_iso": _coerce_str(snap.get("ts_iso")),
            "staleness_s": _coerce_float(snap.get("staleness_s")),
            "source": {
                "provider": _coerce_str(src.get("provider")) or "unknown",
                "detail": _coerce_str(src.get("detail")) or "unknown",
            },
        }

    freshness: Dict[str, Any] = {
        "asof_et": now_et.isoformat(),
        "primary": _fresh_entry(primary, snap if isinstance(snap, Mapping) else {}, key="primary"),
        "spx": _fresh_entry("SPX", spx_snap if isinstance(spx_snap, Mapping) else {}, key="spx"),
        "vix": _fresh_entry("VIX", vix_snap if isinstance(vix_snap, Mapping) else {}, key="vix"),
    }
    state["data_freshness"] = freshness

    return state
