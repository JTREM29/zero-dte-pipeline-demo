from __future__ import annotations

from datetime import time as dtime

import pandas as pd

RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)


def filter_rth(bars: pd.DataFrame) -> pd.DataFrame:
    """Filter bars to Regular Trading Hours using the timestamp's clock.

    Assumptions:
      - If ts is tz-aware, this will convert to America/New_York for filtering.
      - If ts is tz-naive, this will use its local-naive clock with no conversion.

    This avoids guessing when the upstream provider returns naive timestamps.
    """

    if bars is None or bars.empty or "ts" not in bars.columns:
        return bars

    df = bars.copy()
    ts = pd.to_datetime(df["ts"], errors="coerce")
    df = df.assign(ts=ts).dropna(subset=["ts"]).reset_index(drop=True)
    if df.empty:
        return df

    try:
        if getattr(df["ts"].dt, "tz", None) is not None:
            df["ts"] = df["ts"].dt.tz_convert("America/New_York")
    except Exception:
        pass

    t = df["ts"].dt.time
    mask = (t >= RTH_START) & (t <= RTH_END)
    return df.loc[mask].reset_index(drop=True)
