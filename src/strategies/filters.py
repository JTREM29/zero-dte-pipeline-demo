from __future__ import annotations
from dataclasses import dataclass
import calendar
import numpy as np
from typing import Dict, Any
from ..utils.indicators import choppiness_index, rolling_vol_zscore

@dataclass
class SeasonalityFilter:
    """Month-based seasonality weighting scaffold.

    Provide a mapping of month->weight (rough expected drift or bias) to influence
    strategy sizing or directional conviction. Defaults are intentionally mild.
    """
    month_weights: Dict[int, float] | None = None

    def __post_init__(self):  # noqa: D401
        if self.month_weights is None:
            # Conservative defaults; negative bias Sept, modest positive Nov/Dec.
            self.month_weights = {
                1: 0.00, 2: 0.00, 3: 0.00, 4: 0.05, 5: -0.02, 6: 0.00,
                7: 0.02, 8: -0.02, 9: -0.05, 10: 0.00, 11: 0.04, 12: 0.04,
            }

    def score(self, month: int) -> float:
        mw = self.month_weights or {}
        return float(mw.get(month, 0.0))

    def label(self, month: int) -> str:
        return calendar.month_abbr[month]


@dataclass
class RegimeFilters:
    """Lightweight market regime signals combining choppiness and realized vol regime.

    choppy: True if choppiness index exceeds threshold.
    vol_event: True if realized volatility z-score exceeds vol_z_hi.
    """
    chop_threshold: float = 61.8  # > ~61 often considered 'choppy'
    vol_z_hi: float = 1.0         # treat vol spike if zscore above this

    def evaluate(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> Dict[str, Any]:
        chop = choppiness_index(high, low, close, period=14)
        rv, z = rolling_vol_zscore(close, lookback_vol=20, lookback_z=252)
        return {
            "choppy": bool(chop >= self.chop_threshold) if not np.isnan(chop) else False,
            "vol_event": bool(z >= self.vol_z_hi) if not np.isnan(z) else False,
            "chop": chop,
            "realized_vol": rv,
            "vol_z": z,
        }
