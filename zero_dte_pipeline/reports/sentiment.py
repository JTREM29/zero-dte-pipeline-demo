"""Utilities for deriving agent-style sentiment summaries from morning reports."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Sequence

from .morning_report import MorningReportResult
from zero_dte_pipeline.candidates.scoring import Direction


def _normalize_direction(direction: Any) -> str:
    if isinstance(direction, Direction):
        return direction.value
    if isinstance(direction, str):
        return direction.lower()
    return "unknown"


def describe_volatility(percentile: Optional[float]) -> str:
    """Return a short volatility description for the given percentile."""
    if percentile is None:
        return "volatility read is unclear"
    if percentile < 35:
        return "volatility remains subdued"
    if percentile < 65:
        return "volatility sits near the mid-range"
    return "volatility is elevated"


def _bias_from_counts(counts: Mapping[str, int]) -> str:
    bullish = counts.get("bullish", 0)
    bearish = counts.get("bearish", 0)
    neutral = counts.get("neutral", 0)
    if not counts:
        return "neutral"
    if bullish >= max(bearish, neutral):
        return "bullish"
    if bearish >= max(bullish, neutral):
        return "bearish"
    return "neutral"


def _avg(values: Iterable[Optional[float]]) -> float:
    numbers = [v for v in values if v is not None]
    if not numbers:
        return 0.0
    return float(sum(numbers) / len(numbers))


def build_agent_sentiments(
    report_result: MorningReportResult | Mapping[str, Any],
    symbols: Sequence[str],
) -> List[MutableMapping[str, Any]]:
    """Generate agent-style sentiment summaries for the requested symbols."""
    sentiments: List[MutableMapping[str, Any]] = []

    if hasattr(report_result, "market_profile"):
        profile = getattr(report_result, "market_profile")
        summary = getattr(report_result, "summary", {})
        approved = getattr(report_result, "approved_candidates", []) or []
        generated = getattr(report_result, "candidates", []) or []
    else:
        payload = report_result  # type: ignore[assignment]
        profile = (payload or {}).get("market_profile")
        summary = (payload or {}).get("summary", {})
        approved = (payload or {}).get("approved_candidates", []) or []
        generated = (payload or {}).get("candidates", []) or []

    fallback_bias = "neutral"
    fallback_confidence = 0.0
    volatility_note = "volatility read is unclear"
    if profile:
        fallback_bias = getattr(profile, "direction_bias", Direction.NEUTRAL).value
        fallback_confidence = getattr(profile, "confidence", 0.0)
        percentile = getattr(profile, "volatility_percentile", None)
        volatility_note = describe_volatility(percentile)
    else:
        percentile = summary.get("volatility_percentile")
        volatility_note = describe_volatility(percentile)

    seen = set()
    ordered_symbols = [sym.upper() for sym in symbols if sym]
    for sym in ordered_symbols:
        if sym in seen:
            continue
        seen.add(sym)

        by_symbol = [
            c for c in approved if getattr(c, "underlying", "").upper() == sym
        ]
        source = "approved"
        if not by_symbol:
            by_symbol = [
                c for c in generated if getattr(c, "underlying", "").upper() == sym
            ]
            source = "generated"

        counts = Counter(
            _normalize_direction(getattr(c, "direction", "unknown")) for c in by_symbol
        )
        bias = _bias_from_counts(counts)

        avg_score = _avg(getattr(c, "score", None) for c in by_symbol)
        avg_confidence = _avg(getattr(c, "confidence", None) for c in by_symbol)
        if not by_symbol:
            avg_confidence = fallback_confidence

        if avg_score >= 0.45:
            conviction = "high conviction"
        elif avg_score >= 0.25:
            conviction = "moderate conviction"
        else:
            conviction = "low conviction"

        if not by_symbol:
            summary_text = (
                f"{sym}: No live structures; defaulting to a {fallback_bias} posture while {volatility_note}."
            )
        else:
            summary_text = (
                f"{sym}: {bias.capitalize()} tilt with {len(by_symbol)} {source} structures "
                f"({counts.get('bullish', 0)} bullish vs {counts.get('bearish', 0)} bearish); "
                f"{conviction} (avg score {avg_score:.2f})."
            )

        summary_text += f" Risk view: {volatility_note}."

        sentiments.append(
            {
                "symbol": sym,
                "bias": bias,
                "avg_score": round(avg_score, 3),
                "avg_confidence": round(avg_confidence, 3),
                "structures": len(by_symbol),
                "source": source,
                "summary": summary_text,
            }
        )

    return sentiments
