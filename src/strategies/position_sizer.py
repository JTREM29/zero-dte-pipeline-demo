"""Position sizing utilities for strategy scores.

The function `size_from_score` maps a normalized strategy score in [-1,1] to a
risk fraction (0..max_risk) and applies regime haircuts:

Logic:
  base_size = max_risk * |score| (capped at max_risk)
  if choppy: * 0.6
  if vol_event: * 0.6 (applied after any choppy haircut)

Values are rounded to 3 decimal places for clean downstream serialization.

Rationale:
 - Using absolute score focuses on conviction magnitude; direction handled by
   separate signal layer (enter_long/enter_short).
 - Sequential haircuts compound (e.g. both True => 0.36 of original base).
 - Keeps sizing deterministic and monotonic with respect to |score|.
"""
from __future__ import annotations

def size_from_score(score: float, choppy: bool, vol_event: bool, max_risk: float = 0.60) -> float:
    """Return fractional risk sizing given a composite score and regime flags.

    Parameters
    ----------
    score : float
        Normalized strategy conviction in [-1, 1]. Values outside are clipped.
    choppy : bool
        If True, reduces size to reflect lower edge in sideways conditions.
    vol_event : bool
        If True, reduces size for elevated realized volatility regime.
    max_risk : float
        Maximum fractional risk allocation (e.g., 0.60 => 60%). Must be >0.

    Returns
    -------
    float
        Position size fraction in [0, max_risk] after haircuts.
    """
    if max_risk <= 0:
        raise ValueError("max_risk must be > 0")
    # Clamp score to safe bounds
    s = max(-1.0, min(1.0, float(score)))
    base = max_risk * abs(s)
    # Haircuts (multiplicative)
    if choppy:
        base *= 0.6
    if vol_event:
        base *= 0.6
    return round(min(base, max_risk), 3)

__all__ = ["size_from_score"]