"""TNT state object (v1.0)

A small deterministic JSON snapshot intended to be injected into LLM context as
`TNT_STATE (authoritative)`.

Design goals:
- Small (fast, reliable)
- Deterministic (same inputs -> same answers)
- Fail-safe (freshness + data health)

This module is intentionally independent of discord.py so it can be unit-tested.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, MutableMapping, Optional, Sequence
from zoneinfo import ZoneInfo

from delivery.tnt_chart_contract import build_chart_spec_from_state

ET = ZoneInfo("America/New_York")

SchemaVersion = Literal["1.0"]
DataHealth = Literal["OK", "STALE", "DEGRADED", "DOWN"]
Bias = Literal["BULLISH", "BEARISH", "NEUTRAL"]
Conviction = Literal["LOW", "MEDIUM", "HIGH"]
Regime = Literal["TREND", "COMPRESSION", "MEAN_REVERT", "MOMENTUM", "VOLATILE"]
Aggression = Literal["PERMITTED", "REDUCED", "PROHIBITED"]

TNT_STATE_SCHEMA_VERSION: SchemaVersion = "1.0"


@dataclass(frozen=True)
class TNTStateBuildResult:
    state: Dict[str, Any]
    data_health: DataHealth


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


def _normalize_bias(raw: object) -> Bias:
    s = (_coerce_str(raw) or "NEUTRAL").upper()
    if s in {"BULL", "BULLISH"}:
        return "BULLISH"
    if s in {"BEAR", "BEARISH"}:
        return "BEARISH"
    return "NEUTRAL"


def _normalize_conviction(raw: object) -> Conviction:
    s = (_coerce_str(raw) or "LOW").upper()
    if s in {"HIGH"}:
        return "HIGH"
    if s in {"MED", "MEDIUM"}:
        return "MEDIUM"
    return "LOW"


def _normalize_regime(raw: object) -> Regime:
    s = (_coerce_str(raw) or "").upper()
    allowed = {"TREND", "COMPRESSION", "MEAN_REVERT", "MOMENTUM", "VOLATILE"}
    if s in allowed:
        return s  # type: ignore[return-value]

    # Conservative mapping: unknown -> COMPRESSION (forces restraint).
    if any(token in s for token in ("CHOP", "RANGE", "COMP")):
        return "COMPRESSION"
    if "MEAN" in s:
        return "MEAN_REVERT"
    if "MOM" in s:
        return "MOMENTUM"
    if "VOL" in s:
        return "VOLATILE"
    if "TREND" in s:
        return "TREND"
    return "COMPRESSION"


def _data_health_from_staleness(
    staleness_s: Optional[float],
    *,
    ok_threshold_s: float,
    has_price: bool,
) -> DataHealth:
    if not has_price:
        return "DOWN"
    if staleness_s is None:
        return "DEGRADED"
    if staleness_s <= ok_threshold_s:
        return "OK"
    return "STALE"


def _price_vs_pivot_plausible(symbol: str, last_price: Optional[float], pivot: Optional[float]) -> tuple[bool, Optional[str]]:
    """Return (ok, reason) for a simple price/levels integrity check.

    v1.0 contract requirement:
    - If last price and key level pivot clearly disagree in scale, treat data as DEGRADED.
    """

    if last_price is None or pivot is None:
        return True, None

    try:
        lp = float(last_price)
        pv = float(pivot)
    except Exception:  # noqa: BLE001
        return False, "invalid numeric inputs"

    if lp <= 0 or pv <= 0:
        return False, "non-positive price/level"

    rel = abs(lp - pv) / max(lp, pv)
    # Daily pivots should not be ~20%+ away from last price for the same instrument.
    # Also require a meaningful absolute diff to avoid noise for very small prices.
    if rel > 0.20 and abs(lp - pv) > 10.0:
        sym_u = (symbol or "").upper()
        return False, f"price/levels mismatch for {sym_u}"

    return True, None


def _permissions_from_state(
    *,
    data_health: DataHealth,
    regime: Regime,
    conviction: Conviction,
    gate: Optional[Mapping[str, Any]] = None,
) -> tuple[Aggression, bool, bool, list[str]]:
    reasons: list[str] = []

    if data_health in {"STALE", "DEGRADED", "DOWN"}:
        reasons.append(f"DATA_{data_health}")
        return "PROHIBITED", True, True, reasons

    gate_mode = None
    if isinstance(gate, Mapping):
        gate_mode = _coerce_str(gate.get("mode") or gate.get("impact"))
    if gate_mode:
        gate_mode_u = gate_mode.upper()
        reasons.append(f"GATE_{gate_mode_u}")
        if gate_mode_u in {"HARD", "STAND_DOWN"}:
            return "PROHIBITED", True, True, reasons
        if gate_mode_u in {"SOFT", "CAUTION"}:
            # Gate is authoritative: reduce activity.
            return "REDUCED", True, False, reasons

    if regime == "COMPRESSION":
        reasons.append("COMPRESSION")
        return "PROHIBITED", True, False, reasons

    if conviction == "LOW":
        reasons.append("LOW_CONVICTION")
        return "REDUCED", True, False, reasons

    if regime in {"VOLATILE"}:
        reasons.append("VOLATILE")
        return "REDUCED", True, False, reasons

    return "PERMITTED", False, False, reasons


def _decision_zones_from_pivots(
    *,
    pivot: Optional[float],
    r1: Optional[float],
    s1: Optional[float],
) -> list[dict[str, Any]]:
    zones: list[dict[str, Any]] = []
    up = r1 if r1 is not None else pivot
    dn = s1 if s1 is not None else pivot

    if up is not None:
        zones.append(
            {
                "name": "ACCEPTANCE_UP",
                "type": "trigger",
                "price": float(up),
                "meaning": "Bullish posture permitted only on acceptance above",
            }
        )
    if dn is not None:
        zones.append(
            {
                "name": "BREAKDOWN",
                "type": "trigger",
                "price": float(dn),
                "meaning": "Bearish posture permitted on breakdown below",
            }
        )

    return zones


def _events_from_macro_risk(macro_risk: object) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    items: list[Mapping[str, Any]] = []
    if isinstance(macro_risk, Mapping):
        items = [macro_risk]
    elif isinstance(macro_risk, Sequence) and not isinstance(macro_risk, (str, bytes)):
        items = [x for x in macro_risk if isinstance(x, Mapping)]  # type: ignore[list-item]

    for item in items:
        title = _coerce_str(item.get("title") or item.get("name")) or "EVENT"
        time_et = _coerce_str(item.get("time_et") or item.get("time"))
        delta_min = None
        try:
            delta_min = float(item.get("delta_min")) if item.get("delta_min") is not None else None
        except Exception:  # noqa: BLE001
            delta_min = None

        phase = "PRE"
        if isinstance(delta_min, (int, float)):
            if -10.0 <= delta_min <= 10.0:
                phase = "WINDOW"
            elif delta_min < -10.0:
                phase = "POST"

        events.append(
            {
                "name": title,
                "risk": "HIGH",
                "phase": phase,
                "window_et": {
                    "start": time_et,
                    "end": time_et,
                },
            }
        )

    return events


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except Exception:  # noqa: BLE001
        return lo


def _market_session_from_et(dt_et: datetime) -> str:
    """Local market session classifier to keep this module discord-free."""

    try:
        if dt_et.tzinfo is None:
            dt_et = dt_et.replace(tzinfo=ET)
        dt_et = dt_et.astimezone(ET)
    except Exception:  # noqa: BLE001
        return "UNKNOWN"

    if dt_et.weekday() >= 5:
        return "WEEKEND"

    t = dt_et.time()
    # Mirror delivery.discord_bot defaults (RTH_START 09:31, RTH_CLOSE 16:00, PM_OPEN 04:00, AH_CLOSE 20:00)
    if (t.hour, t.minute) >= (9, 31) and (t.hour, t.minute) < (16, 0):
        return "RTH"
    if (t.hour, t.minute) >= (4, 0) and (t.hour, t.minute) < (9, 31):
        return "PRE"
    if (t.hour, t.minute) >= (16, 0) and (t.hour, t.minute) < (20, 0):
        return "AH"
    return "CLOSED"


def _minutes_to_rth_close(dt_et: datetime) -> Optional[int]:
    try:
        if dt_et.tzinfo is None:
            dt_et = dt_et.replace(tzinfo=ET)
        dt_et = dt_et.astimezone(ET)
        if _market_session_from_et(dt_et) != "RTH":
            return None
        close = dt_et.replace(hour=16, minute=0, second=0, microsecond=0)
        delta = (close - dt_et).total_seconds() / 60.0
        return int(delta)
    except Exception:  # noqa: BLE001
        return None


def _tvds_label(score: float) -> str:
    if score >= 60.0:
        return "RED"
    if score >= 25.0:
        return "YELLOW"
    return "GREEN"


def _compute_tvds(state: Mapping[str, Any], *, freshness_ok_sec: float = 180.0) -> Dict[str, Any]:
    """Trade Viability Decay Score (TVDS): deterministic 0..100 degradation score.

    Higher means lower viability / more decay.
    """

    meta = state.get("meta") if isinstance(state.get("meta"), Mapping) else {}
    posture = state.get("posture") if isinstance(state.get("posture"), Mapping) else {}
    perms = state.get("permissions") if isinstance(state.get("permissions"), Mapping) else {}

    data_health = str(meta.get("data_health") or "UNKNOWN").upper()
    freshness = meta.get("data_freshness_sec")
    integrity = meta.get("data_integrity") if isinstance(meta.get("data_integrity"), Mapping) else {}
    integrity_ok = bool(integrity.get("ok", True))

    conviction = str(posture.get("conviction") or "LOW").upper()
    regime = str(posture.get("regime") or "COMPRESSION").upper()
    no_trade = bool(perms.get("no_trade"))

    generated_at_raw = meta.get("generated_at_et")
    generated_at = None
    if isinstance(generated_at_raw, str) and generated_at_raw:
        try:
            generated_at = datetime.fromisoformat(generated_at_raw.replace("Z", "+00:00"))
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
            generated_at = generated_at.astimezone(ET)
        except Exception:  # noqa: BLE001
            generated_at = None
    if generated_at is None:
        generated_at = _now_et()

    session = _market_session_from_et(generated_at)
    mins_to_close = _minutes_to_rth_close(generated_at)

    score = 0.0
    factors: Dict[str, Any] = {}

    # Health is the dominant decay component.
    health_pts = 0.0
    if data_health == "OK":
        health_pts = 0.0
    elif data_health == "DEGRADED":
        health_pts = 40.0
    elif data_health == "STALE":
        health_pts = 60.0
    elif data_health == "DOWN":
        health_pts = 90.0
    else:
        health_pts = 50.0
    score += health_pts
    factors["data_health_pts"] = health_pts

    # Freshness within OK still decays a bit as it ages.
    fresh_pts = 0.0
    if isinstance(freshness, (int, float)) and freshness_ok_sec > 0:
        try:
            ratio = float(freshness) / float(freshness_ok_sec)
            fresh_pts = _clamp(ratio * 20.0, 0.0, 20.0) if data_health == "OK" else 0.0
        except Exception:  # noqa: BLE001
            fresh_pts = 0.0
    score += fresh_pts
    factors["freshness_pts"] = fresh_pts

    session_pts = 0.0
    if session in {"PRE", "AH"}:
        session_pts = 15.0
    elif session in {"CLOSED", "WEEKEND"}:
        session_pts = 25.0
    score += session_pts
    factors["session_pts"] = session_pts
    factors["session"] = session

    close_pts = 0.0
    if isinstance(mins_to_close, int):
        if mins_to_close < 30:
            close_pts = 25.0
        elif mins_to_close < 60:
            close_pts = 15.0
        elif mins_to_close < 120:
            close_pts = 8.0
    score += close_pts
    factors["mins_to_close"] = mins_to_close
    factors["close_pts"] = close_pts

    conv_pts = 0.0
    if conviction == "LOW":
        conv_pts = 15.0
    elif conviction == "MEDIUM":
        conv_pts = 5.0
    score += conv_pts
    factors["conviction_pts"] = conv_pts

    regime_pts = 0.0
    if regime == "COMPRESSION":
        regime_pts = 10.0
    elif regime == "VOLATILE":
        regime_pts = 10.0
    score += regime_pts
    factors["regime_pts"] = regime_pts

    integrity_pts = 0.0
    if not integrity_ok:
        integrity_pts = 15.0
    score += integrity_pts
    factors["integrity_pts"] = integrity_pts

    # Hard floor: if no_trade is set, viability is effectively dead.
    if no_trade:
        score = max(score, 85.0)
        factors["no_trade_floor"] = 85.0

    score = _clamp(score, 0.0, 100.0)
    return {
        "score": round(score, 1),
        "label": _tvds_label(score),
        "factors": factors,
    }


def _compute_structure_stress(state: Mapping[str, Any]) -> Dict[str, Any]:
    price = state.get("price") if isinstance(state.get("price"), Mapping) else {}
    levels = state.get("levels") if isinstance(state.get("levels"), Mapping) else {}
    piv = levels.get("pivots_rth") if isinstance(levels.get("pivots_rth"), Mapping) else {}

    last = _coerce_float(price.get("last"))
    p = _coerce_float(piv.get("P"))
    r1 = _coerce_float(piv.get("R1"))
    s1 = _coerce_float(piv.get("S1"))

    if last is None or last <= 0 or p is None:
        return {"status": "unavailable"}

    def _dist_pct(level: Optional[float]) -> Optional[float]:
        if level is None:
            return None
        try:
            return round(abs(float(last) - float(level)) / float(last) * 100.0, 3)
        except Exception:  # noqa: BLE001
            return None

    d_p = _dist_pct(p)
    d_r1 = _dist_pct(r1)
    d_s1 = _dist_pct(s1)

    nearest = None
    nearest_dist = None
    for name, dist in (("P", d_p), ("R1", d_r1), ("S1", d_s1)):
        if dist is None:
            continue
        if nearest_dist is None or dist < nearest_dist:
            nearest_dist = dist
            nearest = name

    risk = "LOW"
    tags: list[str] = []
    if nearest_dist is not None:
        if nearest == "P" and nearest_dist <= 0.30:
            risk = "HIGH"
            tags.append("PIVOT_CHOP_RISK")
        elif nearest in {"R1", "S1"} and nearest_dist <= 0.50:
            risk = "HIGH"
            tags.append("BREAKOUT_BREAKDOWN_RISK")
        elif nearest_dist <= 0.75:
            risk = "MEDIUM"

    scenarios: list[dict[str, Any]] = []
    scenarios.append(
        {
            "name": "pivot_flip",
            "description": "Most intraday damage happens on pivot flips (accept/reject).",
            "risk": risk,
        }
    )
    if r1 is not None:
        scenarios.append(
            {
                "name": "range_break_up",
                "level": r1,
                "description": "If R1 breaks and fails, reversal risk increases.",
                "risk": "HIGH" if d_r1 is not None and d_r1 <= 0.50 else "MEDIUM",
            }
        )
    if s1 is not None:
        scenarios.append(
            {
                "name": "range_break_down",
                "level": s1,
                "description": "If S1 breaks and snaps back, trap risk increases.",
                "risk": "HIGH" if d_s1 is not None and d_s1 <= 0.50 else "MEDIUM",
            }
        )

    return {
        "status": "ok",
        "nearest_level": nearest,
        "nearest_dist_pct": nearest_dist,
        "dist_to_p_pct": d_p,
        "dist_to_r1_pct": d_r1,
        "dist_to_s1_pct": d_s1,
        "risk": risk,
        "tags": tags,
        "scenarios": scenarios,
    }


def _compute_narrative_risk(state: Mapping[str, Any]) -> Dict[str, Any]:
    events = state.get("events")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        return {"status": "none", "risk": "LOW"}

    high = 0
    window = 0
    names: list[str] = []
    for item in events:
        if not isinstance(item, Mapping):
            continue
        names.append(str(item.get("name") or "EVENT").strip())
        if str(item.get("risk") or "").upper() == "HIGH":
            high += 1
        if str(item.get("phase") or "").upper() == "WINDOW":
            window += 1

    if not names:
        return {"status": "none", "risk": "LOW"}

    # Conservative: any scheduled high-impact event elevates narrative risk.
    risk = "ELEVATED" if high > 0 else "LOW"
    if window > 0:
        risk = "HIGH"
    return {
        "status": "ok",
        "risk": risk,
        "events": names[:5],
    }


def _compute_crowding_risk(state: Mapping[str, Any]) -> Dict[str, Any]:
    chain = state.get("options_chain")
    if not isinstance(chain, Mapping):
        return {"status": "none", "risk": "LOW"}
    metrics = chain.get("metrics") if isinstance(chain.get("metrics"), Mapping) else {}

    underlying_price = _coerce_float(chain.get("underlying_price"))
    ratio = _coerce_float(metrics.get("call_put_oi_ratio"))
    call_wall = metrics.get("call_wall") if isinstance(metrics.get("call_wall"), Mapping) else {}
    put_wall = metrics.get("put_wall") if isinstance(metrics.get("put_wall"), Mapping) else {}
    gamma_peak = metrics.get("gamma_peak") if isinstance(metrics.get("gamma_peak"), Mapping) else {}

    score = 0.0
    tags: list[str] = []

    if isinstance(ratio, (int, float)):
        if ratio >= 1.8:
            score += 25.0
            tags.append("ONE_SIDE_OI_CALLS")
        elif ratio <= 0.55:
            score += 25.0
            tags.append("ONE_SIDE_OI_PUTS")

    def _near(price: Optional[float], level: Optional[float], pct: float) -> bool:
        if price is None or level is None or price <= 0:
            return False
        try:
            return abs(float(price) - float(level)) / float(price) <= float(pct)
        except Exception:  # noqa: BLE001
            return False

    if underlying_price is not None:
        cw_strike = _coerce_float(call_wall.get("strike"))
        pw_strike = _coerce_float(put_wall.get("strike"))
        gp_strike = _coerce_float(gamma_peak.get("strike"))

        if _near(underlying_price, cw_strike, 0.006) or _near(underlying_price, pw_strike, 0.006):
            score += 30.0
            tags.append("PIN_RISK")

        if _near(underlying_price, gp_strike, 0.010):
            score += 20.0
            tags.append("GAMMA_PEAK_NEAR")

    score = _clamp(score, 0.0, 100.0)
    if score >= 60.0:
        risk = "HIGH"
    elif score >= 30.0:
        risk = "ELEVATED"
    else:
        risk = "LOW"

    return {
        "status": "ok",
        "risk": risk,
        "score": round(score, 1),
        "tags": tags,
    }


def _compute_do_nothing_alert(state: Mapping[str, Any]) -> Dict[str, Any]:
    perms = state.get("permissions") if isinstance(state.get("permissions"), Mapping) else {}
    no_trade = bool(perms.get("no_trade"))

    tvds = state.get("tvds") if isinstance(state.get("tvds"), Mapping) else {}
    structure = state.get("structure_stress") if isinstance(state.get("structure_stress"), Mapping) else {}
    crowding = state.get("crowding_risk") if isinstance(state.get("crowding_risk"), Mapping) else {}
    narrative = state.get("narrative_risk") if isinstance(state.get("narrative_risk"), Mapping) else {}

    score = _coerce_float(tvds.get("score"))
    structure_risk = str(structure.get("risk") or "LOW").upper()
    crowding_risk = str(crowding.get("risk") or "LOW").upper()
    narrative_risk = str(narrative.get("risk") or "LOW").upper()

    reasons: list[str] = []
    if no_trade:
        reasons.append("PERMISSIONS_NO_TRADE")
    if isinstance(score, (int, float)) and float(score) >= 60.0:
        reasons.append("TVDS_RED")
    if structure_risk == "HIGH":
        reasons.append("STRUCTURE_HIGH")
    if crowding_risk in {"ELEVATED", "HIGH"}:
        reasons.append("CROWDING")
    if narrative_risk in {"ELEVATED", "HIGH"}:
        reasons.append("NARRATIVE")

    enabled = bool(reasons)
    return {
        "enabled": enabled,
        "reasons": reasons,
    }


def enrich_tnt_state(state: MutableMapping[str, Any], *, freshness_ok_sec: float = 180.0) -> None:
    """Add deterministic edge/risk fields to TNT_STATE in-place."""

    try:
        state["tvds"] = _compute_tvds(state, freshness_ok_sec=freshness_ok_sec)
        state["structure_stress"] = _compute_structure_stress(state)
        state["narrative_risk"] = _compute_narrative_risk(state)
        state["crowding_risk"] = _compute_crowding_risk(state)
        state["do_nothing_alert"] = _compute_do_nothing_alert(state)
    except Exception:  # noqa: BLE001
        # Enrichment is optional; never break the contract.
        return


def build_tnt_state_from_packet(
    packet: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    source_provider: Optional[str] = None,
    source_mode: Optional[str] = None,
    freshness_ok_sec: float = 180.0,
) -> TNTStateBuildResult:
    """Build TNT_STATE from the LLM packet used in trade-context rendering."""

    sym = (_coerce_str(packet.get("symbol")) or "SPY").upper()
    session = _coerce_str(
        (packet.get("notes") or {}).get("session") if isinstance(packet.get("notes"), Mapping) else packet.get("session")
    ) or "UNKNOWN"

    price_last = _coerce_float(packet.get("current_price"))
    staleness_s = None
    notes = packet.get("notes")
    if isinstance(notes, Mapping):
        try:
            staleness_s = float(notes.get("data_staleness_s")) if notes.get("data_staleness_s") is not None else None
        except Exception:  # noqa: BLE001
            staleness_s = None

    piv = _coerce_float(packet.get("pivot"))
    r_levels = packet.get("r_levels") if isinstance(packet.get("r_levels"), Mapping) else {}
    s_levels = packet.get("s_levels") if isinstance(packet.get("s_levels"), Mapping) else {}
    r1 = _coerce_float(r_levels.get("R1"))
    r2 = _coerce_float(r_levels.get("R2"))
    s1 = _coerce_float(s_levels.get("S1"))
    s2 = _coerce_float(s_levels.get("S2"))

    data_health = _data_health_from_staleness(staleness_s, ok_threshold_s=freshness_ok_sec, has_price=price_last is not None)
    ok_integrity, _integrity_reason = _price_vs_pivot_plausible(sym, price_last, piv)
    if not ok_integrity:
        data_health = "DEGRADED"

    bias = _normalize_bias(packet.get("bias"))
    conviction = _normalize_conviction(packet.get("conviction"))
    regime = _normalize_regime(packet.get("regime"))

    gate = None
    if isinstance(packet.get("gate"), Mapping):
        gate = packet.get("gate")  # type: ignore[assignment]

    aggression, momentum_only, no_trade, reasons = _permissions_from_state(
        data_health=data_health,
        regime=regime,
        conviction=conviction,
        gate=gate,
    )
    if not ok_integrity and "DATA_INTEGRITY_CONFLICT" not in reasons:
        reasons.append("DATA_INTEGRITY_CONFLICT")

    generated_at = _now_et(now)

    state: Dict[str, Any] = {
        "meta": {
            "schema_version": TNT_STATE_SCHEMA_VERSION,
            "generated_at_et": generated_at.isoformat(),
            "data_freshness_sec": int(staleness_s) if isinstance(staleness_s, (int, float)) else None,
            "data_health": data_health,
            "data_integrity": {
                "ok": bool(ok_integrity),
                "reason": _integrity_reason,
            },
            "source": {
                "provider": source_provider or _coerce_str(packet.get("current_price_source")) or "UNKNOWN",
                "mode": source_mode or _coerce_str(packet.get("trade_context_mode")) or "UNKNOWN",
            },
        },
        "context": {
            "symbol": sym,
            "asset_class": "EQUITY",
            "session": session,
            "timeframes": {
                "execution": "5m",
                "structure": "60m",
                "context": "1D",
            },
        },
        "price": {
            "last": price_last,
        },
        "posture": {
            "bias": bias,
            "conviction": conviction,
            "regime": regime,
            "mode": "NORMAL",
            "rationale_tags": [],
        },
        "levels": {
            "decision_zones": _decision_zones_from_pivots(pivot=piv, r1=r1, s1=s1),
            "support": [s1] if s1 is not None else [],
            "resistance": [r1] if r1 is not None else [],
            "pivots_rth": {"P": piv, "R1": r1, "S1": s1, "R2": r2, "S2": s2},
        },
        "permissions": {
            "aggression": aggression,
            "momentum_only": momentum_only,
            "no_trade": no_trade,
            "reasons": reasons,
        },
        "events": _events_from_macro_risk(packet.get("macro_risk")) if packet.get("macro_risk") is not None else [],
    }

    chart = build_chart_spec_from_state(state)
    state["chart"] = {
        "recommended_template": chart.template,
        "template_params": (chart.spec or {}).get("template_params") if chart.spec else None,
    }

    # Enforce stand-down if the contract says data is unhealthy.
    if data_health in {"STALE", "DEGRADED", "DOWN"}:
        state["permissions"]["aggression"] = "PROHIBITED"  # type: ignore[index]
        state["permissions"]["no_trade"] = True  # type: ignore[index]
        if "DATA_HEALTH" not in state["permissions"]["reasons"]:  # type: ignore[index]
            state["permissions"]["reasons"].append("DATA_HEALTH")  # type: ignore[index]

    enrich_tnt_state(state, freshness_ok_sec=freshness_ok_sec)

    return TNTStateBuildResult(state=state, data_health=data_health)


def build_tnt_state_from_analysis_payload(
    analysis_payload: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    freshness_ok_sec: float = 180.0,
) -> TNTStateBuildResult:
    """Build TNT_STATE from an on-demand analyze agent payload.

    This expects a `meta.price_context` and (optionally) `meta.signal_payload`.
    """

    meta = analysis_payload.get("meta") if isinstance(analysis_payload.get("meta"), Mapping) else {}
    price_ctx = meta.get("price_context") if isinstance(meta.get("price_context"), Mapping) else {}
    signal = meta.get("signal_payload") if isinstance(meta.get("signal_payload"), Mapping) else {}
    gate = meta.get("gate") if isinstance(meta.get("gate"), Mapping) else None

    sym = (_coerce_str(analysis_payload.get("symbol")) or _coerce_str(meta.get("display_symbol")) or "SPY").upper()
    session = _coerce_str(signal.get("session")) or _coerce_str(meta.get("session")) or "UNKNOWN"

    # Optional deterministic technical package (RSI/MACD/VWAP/trend).
    technicals: Dict[str, Any] = {}
    try:
        tech_root = analysis_payload.get("technical_state")
        if isinstance(tech_root, Mapping):
            sym_state = tech_root.get(sym)
            if isinstance(sym_state, Mapping):
                tf_map = sym_state.get("timeframes") if isinstance(sym_state.get("timeframes"), Mapping) else {}
                # Prefer the execution timeframe from the signal packet.
                preferred_tf = _coerce_str(signal.get("tf_exec")) or "5m"
                tf_state = tf_map.get(preferred_tf)
                # Fallback: pick a common intraday tf if missing.
                if not isinstance(tf_state, Mapping):
                    for candidate in ("5m", "1m", "60m", "1D"):
                        tf_state = tf_map.get(candidate)
                        if isinstance(tf_state, Mapping):
                            preferred_tf = candidate
                            break

                if isinstance(tf_state, Mapping):
                    trend = tf_state.get("trend") if isinstance(tf_state.get("trend"), Mapping) else None
                    technicals = {
                        "tf": preferred_tf,
                        "rsi_14": _coerce_float(tf_state.get("rsi_14")),
                        "macd": _coerce_float(tf_state.get("macd")),
                        "macd_signal": _coerce_float(tf_state.get("macd_signal")),
                        "macd_hist": _coerce_float(tf_state.get("macd_hist")),
                        "macd_hist_pct": _coerce_float(tf_state.get("macd_hist_pct")),
                        "vwap": _coerce_float(tf_state.get("vwap")),
                        "trend": {
                            "direction": _coerce_str(trend.get("direction")) if isinstance(trend, Mapping) else None,
                            "strength": _coerce_float(trend.get("strength")) if isinstance(trend, Mapping) else None,
                        }
                        if isinstance(trend, Mapping)
                        else None,
                    }
    except Exception:  # noqa: BLE001
        technicals = {}

    price_last = _coerce_float(price_ctx.get("last_price"))
    ts_raw = _coerce_str(price_ctx.get("last_price_ts"))
    staleness_s: Optional[float] = None
    if ts_raw:
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            staleness_s = max((datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds(), 0.0)
        except Exception:  # noqa: BLE001
            staleness_s = None

    piv = _coerce_float(signal.get("pivot"))
    r1 = _coerce_float(signal.get("r1"))
    r2 = _coerce_float(signal.get("r2"))
    s1 = _coerce_float(signal.get("s1"))
    s2 = _coerce_float(signal.get("s2"))

    data_health = _data_health_from_staleness(staleness_s, ok_threshold_s=freshness_ok_sec, has_price=price_last is not None)
    ok_integrity, _integrity_reason = _price_vs_pivot_plausible(sym, price_last, piv)
    if not ok_integrity:
        data_health = "DEGRADED"

    bias = _normalize_bias(signal.get("bias"))
    conviction = _normalize_conviction(signal.get("conviction"))
    regime = _normalize_regime(signal.get("pivot_regime") or signal.get("regime"))

    aggression, momentum_only, no_trade, reasons = _permissions_from_state(
        data_health=data_health,
        regime=regime,
        conviction=conviction,
        gate=gate if isinstance(gate, Mapping) else None,
    )
    if not ok_integrity and "DATA_INTEGRITY_CONFLICT" not in reasons:
        reasons.append("DATA_INTEGRITY_CONFLICT")

    generated_at = _now_et(now)

    state: Dict[str, Any] = {
        "meta": {
            "schema_version": TNT_STATE_SCHEMA_VERSION,
            "generated_at_et": generated_at.isoformat(),
            "data_freshness_sec": int(staleness_s) if isinstance(staleness_s, (int, float)) else None,
            "data_health": data_health,
            "data_integrity": {
                "ok": bool(ok_integrity),
                "reason": _integrity_reason,
            },
            "source": {
                "provider": _coerce_str(price_ctx.get("source")) or "UNKNOWN",
                "mode": _coerce_str(price_ctx.get("mode")) or "UNKNOWN",
            },
        },
        "context": {
            "symbol": sym,
            "asset_class": "EQUITY",
            "session": session,
            "timeframes": {
                "execution": _coerce_str(signal.get("tf_exec")) or "5m",
                "structure": _coerce_str(signal.get("tf_struct")) or "60m",
                "context": _coerce_str(signal.get("tf_ctx")) or "1D",
            },
        },
        "price": {
            "last": price_last,
        },
        "posture": {
            "bias": bias,
            "conviction": conviction,
            "regime": regime,
            "mode": "NORMAL",
            "rationale_tags": [],
        },
        "levels": {
            "decision_zones": _decision_zones_from_pivots(pivot=piv, r1=r1, s1=s1),
            "support": [s1] if s1 is not None else [],
            "resistance": [r1] if r1 is not None else [],
            "pivots_rth": {"P": piv, "R1": r1, "S1": s1, "R2": r2, "S2": s2},
        },
        "permissions": {
            "aggression": aggression,
            "momentum_only": momentum_only,
            "no_trade": no_trade,
            "reasons": reasons,
        },
        "events": _events_from_macro_risk(meta.get("macro_risk")) if meta.get("macro_risk") is not None else [],
    }

    if technicals:
        state["technicals"] = technicals

    chart = build_chart_spec_from_state(state)
    state["chart"] = {
        "recommended_template": chart.template,
        "template_params": (chart.spec or {}).get("template_params") if chart.spec else None,
    }

    if data_health in {"STALE", "DEGRADED", "DOWN"}:
        state["permissions"]["aggression"] = "PROHIBITED"  # type: ignore[index]
        state["permissions"]["no_trade"] = True  # type: ignore[index]
        if "DATA_HEALTH" not in state["permissions"]["reasons"]:  # type: ignore[index]
            state["permissions"]["reasons"].append("DATA_HEALTH")  # type: ignore[index]

    enrich_tnt_state(state, freshness_ok_sec=freshness_ok_sec)

    return TNTStateBuildResult(state=state, data_health=data_health)


def validate_tnt_state(state: Mapping[str, Any]) -> tuple[bool, str]:
    """Lightweight validation (boundary test helper)."""

    if not isinstance(state, Mapping):
        return False, "state not mapping"

    meta = state.get("meta")
    if not isinstance(meta, Mapping):
        return False, "missing meta"
    if str(meta.get("schema_version")) != TNT_STATE_SCHEMA_VERSION:
        return False, "schema_version mismatch"

    data_health = meta.get("data_health")
    if data_health not in {"OK", "STALE", "DEGRADED", "DOWN"}:
        return False, "invalid data_health"

    context = state.get("context")
    if not isinstance(context, Mapping):
        return False, "missing context"
    if not _coerce_str(context.get("symbol")):
        return False, "missing symbol"

    price = state.get("price")
    if not isinstance(price, Mapping):
        return False, "missing price"
    if price.get("last") is None:
        # Allowed, but only if we're in DOWN/DEGRADED mode.
        if data_health == "OK":
            return False, "price.last missing with OK health"

    posture = state.get("posture")
    if not isinstance(posture, Mapping):
        return False, "missing posture"
    if posture.get("bias") not in {"BULLISH", "BEARISH", "NEUTRAL"}:
        return False, "invalid posture.bias"
    if posture.get("conviction") not in {"LOW", "MEDIUM", "HIGH"}:
        return False, "invalid posture.conviction"
    if posture.get("regime") not in {"TREND", "COMPRESSION", "MEAN_REVERT", "MOMENTUM", "VOLATILE"}:
        return False, "invalid posture.regime"

    levels = state.get("levels")
    if not isinstance(levels, Mapping):
        return False, "missing levels"
    dz = levels.get("decision_zones")
    if not isinstance(dz, Sequence):
        return False, "missing decision_zones"

    perms = state.get("permissions")
    if not isinstance(perms, Mapping):
        return False, "missing permissions"
    if perms.get("aggression") not in {"PERMITTED", "REDUCED", "PROHIBITED"}:
        return False, "invalid permissions.aggression"
    if not isinstance(perms.get("momentum_only"), bool):
        return False, "invalid permissions.momentum_only"
    if not isinstance(perms.get("no_trade"), bool):
        return False, "invalid permissions.no_trade"
    reasons = perms.get("reasons")
    if not isinstance(reasons, Sequence):
        return False, "invalid permissions.reasons"

    return True, "OK"


def format_tnt_state_block(state: Mapping[str, Any]) -> str:
    """Return the canonical wrapper string for prompt injection."""

    payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
    return f"TNT_STATE (authoritative). Use only these facts. If stale, stand down.\n{payload}"
