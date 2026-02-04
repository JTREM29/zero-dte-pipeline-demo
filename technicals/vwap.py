from __future__ import annotations

import pandas as pd


def compute_rth_vwap(bars: pd.DataFrame) -> float | None:
    """Deterministic VWAP over the provided bars.

    Expects columns: ts, high, low, close, volume.
    Uses typical price = (H+L+C)/3.

    Notes:
      - This does not convert timezones.
      - You control which session bars you feed it (e.g., RTH-only).
    """

    if bars is None or bars.empty:
        return None

    cols = {"ts", "high", "low", "close", "volume"}
    if not cols.issubset(set(bars.columns)):
        return None

    df = bars.copy()

    for c in ("high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["ts", "high", "low", "close", "volume"]).reset_index(drop=True)
    if df.empty:
        return None

    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]

    denom = float(df["volume"].sum())
    if denom <= 0:
        return None

    return float(pv.sum() / denom)
