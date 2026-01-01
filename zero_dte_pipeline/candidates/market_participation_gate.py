from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional

ParticipationRegime = Literal["RISK_ON", "DEFENSIVE", "MIXED", "UNKNOWN"]
ParticipationState = Literal["CONFIRM", "NEUTRAL", "CONFLICT", "UNKNOWN"]
ParticipationImpact = Literal["ALLOW", "CAUTION", "STAND_DOWN", "UNKNOWN"]


@dataclass(frozen=True)
class ParticipationGateResult:
    regime: ParticipationRegime
    state: ParticipationState
    impact: ParticipationImpact
    reason: str
    authority: str = "market_participation_gate"
    data_quality: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime,
            "state": self.state,
            "impact": self.impact,
            "reason": self.reason,
            "authority": self.authority,
            "data_quality": self.data_quality,
        }


def _norm_bias(bias: Optional[str]) -> str:
    if not bias:
        return "UNKNOWN"
    normalized = bias.strip().upper()
    if normalized in {"BULL", "BULLISH", "LONG"}:
        return "BULLISH"
    if normalized in {"BEAR", "BEARISH", "SHORT"}:
        return "BEARISH"
    if normalized == "NEUTRAL":
        return "NEUTRAL"
    return normalized


def apply_participation_gate(
    *,
    bias: Optional[str],
    model_edge: Optional[float],
    participation_regime: Optional[str],
    participation_quality: Optional[str] = None,
) -> ParticipationGateResult:
    """Return the participation gate outcome given bias, regime and data quality."""
    normalized_bias = _norm_bias(bias)
    regime = (participation_regime or "UNKNOWN").strip().upper()
    quality = (participation_quality or "UNKNOWN").strip().upper()

    if regime not in {"RISK_ON", "DEFENSIVE", "MIXED", "UNKNOWN"}:
        regime = "UNKNOWN"

    if quality in {"MISSING", "STALE", "PARTIAL", "UNKNOWN"} and regime == "UNKNOWN":
        return ParticipationGateResult(
            regime="UNKNOWN",
            state="UNKNOWN",
            impact="UNKNOWN",
            reason="Market participation unavailable (missing/stale). No override applied.",
            data_quality=quality,
        )

    if normalized_bias in {"UNKNOWN", "NEUTRAL"}:
        if regime == "MIXED":
            return ParticipationGateResult(
                regime="MIXED",
                state="NEUTRAL",
                impact="CAUTION",
                reason="Mixed participation while bias is NEUTRAL/UNKNOWN; tighten confirmation.",
                data_quality=quality,
            )
        return ParticipationGateResult(
            regime=regime,
            state="NEUTRAL",
            impact="ALLOW",
            reason="No directional bias; participation does not create a conflict gate.",
            data_quality=quality,
        )

    if normalized_bias == "BULLISH":
        if regime == "RISK_ON":
            return ParticipationGateResult(
                regime="RISK_ON",
                state="CONFIRM",
                impact="ALLOW",
                reason="Risk-on leadership confirms bullish bias.",
                data_quality=quality,
            )
        if regime == "DEFENSIVE":
            return ParticipationGateResult(
                regime="DEFENSIVE",
                state="CONFLICT",
                impact="STAND_DOWN",
                reason="Defensive leadership conflicts with bullish bias; override to stand-down.",
                data_quality=quality,
            )
        if regime == "MIXED":
            return ParticipationGateResult(
                regime="MIXED",
                state="NEUTRAL",
                impact="CAUTION",
                reason="Mixed participation; bullish bias requires stronger confirmation.",
                data_quality=quality,
            )

    if normalized_bias == "BEARISH":
        if regime == "DEFENSIVE":
            return ParticipationGateResult(
                regime="DEFENSIVE",
                state="CONFIRM",
                impact="ALLOW",
                reason="Defensive leadership confirms bearish bias.",
                data_quality=quality,
            )
        if regime == "RISK_ON":
            return ParticipationGateResult(
                regime="RISK_ON",
                state="CONFLICT",
                impact="STAND_DOWN",
                reason="Risk-on leadership conflicts with bearish bias; override to stand-down.",
                data_quality=quality,
            )
        if regime == "MIXED":
            return ParticipationGateResult(
                regime="MIXED",
                state="NEUTRAL",
                impact="CAUTION",
                reason="Mixed participation; bearish bias requires stronger confirmation.",
                data_quality=quality,
            )

    return ParticipationGateResult(
        regime="UNKNOWN",
        state="UNKNOWN",
        impact="UNKNOWN",
        reason="Unable to classify participation vs bias; no override applied.",
        data_quality=quality,
    )
