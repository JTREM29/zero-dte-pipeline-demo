"""Helper to assemble deterministic agent-facing technical payloads."""

from __future__ import annotations

import pandas as pd

from .pattern_scan import build_pattern_candidates
from .technical_features import build_technical_state


def build_agent_tech_package(
    symbol: str,
    bars_by_tf: dict[str, pd.DataFrame],
    levels: dict[str, float] | None = None,
    custom_levels: list[dict[str, float | str]] | None = None,
) -> dict:
    tech_state = build_technical_state(
        symbol=symbol,
        bars_by_tf=bars_by_tf,
        levels=levels,
        custom_levels=custom_levels,
    )
    patterns = build_pattern_candidates(
        symbol=symbol,
        bars_by_tf=bars_by_tf,
        min_confidence=0.60,
    )

    return {
        "technical_state": {symbol: tech_state},
        "pattern_candidates": {symbol: patterns},
    }
