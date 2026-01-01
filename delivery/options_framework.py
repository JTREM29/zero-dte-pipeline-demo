"""Deterministic templates for the Options Framework (Coach) section."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

try:  # Python <3.8 backport guard (not expected, but keeps import local)
    from typing import Literal
except ImportError:  # pragma: no cover
    Literal = str  # type: ignore[assignment]

# Templates are grouped by (bias, regime_bucket)
# Each entry holds the four framework bullets and two or three avoid bullets.
_TEMPLATES: Dict[str, Dict[str, Dict[str, Iterable[str]]]] = {
    "BULL": {
        "TREND": {
            "framework": (
                "Directional confirmation favors bullish exposure rather than mean-reversion.",
                "Continuation structure fits best when price holds above the pivot/trigger level.",
                "Expansion setups align better than premium selling when momentum is building.",
                "Defined-risk structures fit best when invalidation is clear and nearby.",
            ),
            "avoid": (
                "Fading strength without a failed reclaim / breakdown",
                "Neutral structures if follow-through is already underway",
                "Holding exposure if price loses the pivot/trigger",
            ),
        },
        "RANGE": {
            "framework": (
                "Directional confirmation is bullish, but range conditions reduce follow-through.",
                "Mean-reversion bias is common inside ranges; prioritize patience and clarity.",
                "Structures that tolerate chop fit better than momentum-dependent setups.",
                "Defined-risk framing matters most when signals are mixed at the pivot.",
            ),
            "avoid": (
                "Momentum-dependent exposure without a clean break + hold",
                "Over-committing when price is oscillating around the pivot",
                "Premium selling if compression is extreme and breakout risk is rising",
            ),
        },
        "COMPRESSION": {
            "framework": (
                "Confirmation is bullish, and compression increases the odds of expansion.",
                "Expansion-focused structures fit better than chop-tolerant approaches.",
                "Let the break + acceptance define direction before leaning in.",
                "Keep risk defined until expansion proves itself with follow-through.",
            ),
            "avoid": (
                "Premium selling into rising expansion risk",
                "Acting on the first spike without acceptance",
                "Staying involved if price snaps back through the pivot",
            ),
        },
    },
    "BEAR": {
        "TREND": {
            "framework": (
                "Directional confirmation favors bearish exposure rather than dip-buying.",
                "Continuation structure fits best when price holds below the pivot/trigger level.",
                "Expansion setups align better than premium selling when downside momentum builds.",
                "Defined-risk structures fit best when invalidation is clear and nearby.",
            ),
            "avoid": (
                "Catching bottoms without a failed breakdown / reclaim",
                "Neutral structures if follow-through is accelerating",
                "Holding exposure if price reclaims the pivot/trigger",
            ),
        },
        "RANGE": {
            "framework": (
                "Confirmation is bearish, but range conditions reduce downside follow-through.",
                "Inside ranges, false breaks are common; require clean rejection for confidence.",
                "Structures that tolerate chop fit better than momentum-dependent exposure.",
                "Keep risk defined until a decisive break + acceptance occurs.",
            ),
            "avoid": (
                "Momentum exposure without a clean break + hold below pivot",
                "Over-committing near the middle of the range",
                "Premium selling if compression is extreme and breakout risk is rising",
            ),
        },
        "COMPRESSION": {
            "framework": (
                "Confirmation is bearish, and compression increases expansion odds.",
                "Expansion-focused structures fit better than range-fade assumptions.",
                "Let rejection and acceptance below the pivot define the setup.",
                "Keep risk defined until expansion proves itself with follow-through.",
            ),
            "avoid": (
                "Premium selling into expansion risk",
                "Acting on the first flush without acceptance",
                "Staying involved if price snaps back above pivot",
            ),
        },
    },
    "NEUTRAL": {
        "RANGE": {
            "framework": (
                "Direction is mixed; treat this as a structure-first environment.",
                "Range conditions favor patience: let the pivot act as the decision line.",
                "Defined-risk and flexibility matter more than directional conviction.",
                "Wait for break + acceptance before adopting directional exposure.",
            ),
            "avoid": (
                "Directional exposure without confirmation",
                "Chasing spikes that return to pivot",
                "Over-trading inside the range midpoint",
            ),
        },
        "COMPRESSION": {
            "framework": (
                "Direction is unresolved, but compression increases expansion probability.",
                "Treat the pivot as the trigger: clarity comes from acceptance.",
                "Defined-risk framing is essential until direction proves itself.",
                "Focus on process: confirmation first, then structure.",
            ),
            "avoid": (
                "Premium selling into potential expansion",
                "Acting on the first move without acceptance",
                "Adding risk while confirmation is mixed",
            ),
        },
        # Default fallback for neutral + trend/other still uses range copy.
    },
}

RegimeBucket = Literal["TREND", "RANGE", "COMPRESSION"]
BiasDir = Literal["BULL", "BEAR", "NEUTRAL"]


def _norm(value: Optional[object]) -> str:
    return str(value or "").upper().strip().replace(" ", "_")


def bucket_regime(regime: Optional[str], trend_score: Optional[float] = None) -> RegimeBucket:
    reg = _norm(regime)

    if any(keyword in reg for keyword in ("COMPRESS", "SQUEEZE", "TIGHT", "COIL", "CONSOLID", "VOL_CONTRACTION", "LOW_VOL", "BREAKOUT_SETUP")):
        return "COMPRESSION"

    if (
        any(keyword in reg for keyword in ("TREND", "MOMENTUM", "IMPULSE", "DIRECTIONAL", "DRIFT"))
        or reg.endswith("_UP")
        or reg.endswith("_DOWN")
        or reg in {"TREND_UP", "TREND_DOWN", "UPTREND", "DOWNTREND"}
    ):
        return "TREND"

    if any(keyword in reg for keyword in ("RANGE", "CHOP", "MEAN_REVERT", "SIDEWAYS", "ROTATION")):
        return "RANGE"
    if reg in {"ABOVE_P", "BELOW_P", "ABOVE_R1", "BELOW_S1", "AT_PIVOT", "NEAR_PIVOT"}:
        return "RANGE"

    if trend_score is not None:
        try:
            score = float(trend_score)
        except (TypeError, ValueError):
            score = None
        else:
            if abs(score) >= 0.60:
                return "TREND"
            if abs(score) <= 0.30:
                return "RANGE"
            return "COMPRESSION"

    return "RANGE"


def bias_dir(bias: Optional[str]) -> BiasDir:
    normed = _norm(bias)
    if "BULL" in normed:
        return "BULL"
    if "BEAR" in normed:
        return "BEAR"
    return "NEUTRAL"


def is_confirmed(bias: Optional[str], confirm: Optional[str]) -> bool:
    bias_bucket = bias_dir(bias)
    confirm_bucket = bias_dir(confirm)
    return bias_bucket in {"BULL", "BEAR"} and bias_bucket == confirm_bucket


def pick_framework_template(
    bias: Optional[str],
    confirm: Optional[str],
    regime: Optional[str],
    trend_score: Optional[float] = None,
) -> Optional[Tuple[BiasDir, RegimeBucket]]:
    if not is_confirmed(bias, confirm):
        return None
    direction = bias_dir(bias)
    regime_bucket = bucket_regime(regime, trend_score)
    return direction, regime_bucket


def build_options_framework(meta: Optional[Dict[str, object]]) -> Optional[str]:
    """Return formatted Options Framework text when eligibility passes."""

    if not isinstance(meta, dict):
        return None

    stand_down = bool(meta.get("stand_down"))
    if stand_down:
        return None

    confirm_raw = meta.get("confirm")
    if isinstance(confirm_raw, bool):
        confirm_value: Optional[str] = meta.get("confirm_dir") if confirm_raw else None
    else:
        confirm_value = confirm_raw  # type: ignore[assignment]
    if not confirm_value:
        confirm_value = meta.get("confirm_direction")

    pivot_present = bool(meta.get("pivot_present"))
    if not pivot_present:
        return None

    tech_state = str(meta.get("technical_state") or "").upper()
    if tech_state and tech_state not in {"OK", "FRESH"}:
        return None

    trend_score = meta.get("trend_score")
    try:
        trend_score_float = float(trend_score) if trend_score is not None else None
    except (TypeError, ValueError):
        trend_score_float = None

    chosen = pick_framework_template(
        meta.get("bias"),
        confirm_value,
        meta.get("regime"),
        trend_score_float,
    )

    if not chosen:
        return None

    direction, regime_bucket = chosen

    template = _TEMPLATES.get(direction, {}).get(regime_bucket)
    if template is None:
        # Fall back to RANGE template if regime-specific copy is missing.
        template = _TEMPLATES.get(direction, {}).get("RANGE")

    if not template:
        return None

    framework_lines: Iterable[str] = template.get("framework", ())
    avoid_lines: Iterable[str] = template.get("avoid", ())

    framework = [str(line).strip() for line in framework_lines if str(line).strip()]
    avoid = [str(line).strip() for line in avoid_lines if str(line).strip()]

    if len(framework) != 4:
        return None

    if len(avoid) < 2:
        return None

    max_lines = 9
    total_lines = 1 + len(framework) + 1 + len(avoid)
    if total_lines > max_lines:
        # Trim avoid bullets down to keep within contract rules.
        avoid = avoid[: max(2, max_lines - (1 + len(framework) + 1))]
        total_lines = 1 + len(framework) + 1 + len(avoid)
        if total_lines > max_lines:
            return None

    lines: List[str] = ["🧠 Options Framework (Coach)"]
    lines.extend(f"• {line}" for line in framework)
    lines.append("Avoid:")
    lines.extend(f"• {line}" for line in avoid)

    return "\n".join(lines)


__all__ = [
    "build_options_framework",
]
