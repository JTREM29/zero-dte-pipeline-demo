from __future__ import annotations

import pandas as pd


class BarsProvider:
    """Abstract bars provider.

    Implement using whatever bars source you already have (cache, REST, etc.).

    Must return a DataFrame with columns:
      - ts (datetime-like) OR index as DatetimeIndex
      - high, low, close
    """

    def get_bars(self, symbol: str, timeframe: str, lookback_bars: int) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError
