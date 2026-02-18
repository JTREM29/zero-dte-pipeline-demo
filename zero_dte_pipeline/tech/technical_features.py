"""Deterministic indicator extraction across timeframes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .schemas import TFState, TechnicalSymbolState, TrendOut

from analysis.indicators import macd as _macd
from analysis.indicators import rsi as _rsi

# Bars input expectation: DataFrame with columns ["ts", "o", "h", "l", "c", "v"].

DEFAULT_SMA = (20, 50, 200)
DEFAULT_EMA = (9, 20, 50)
DEFAULT_ATR = (14,)


@dataclass(frozen=True)
class TrendCfg:
    lookback: int
    eps_mult: float = 0.05  # epsilon threshold scaled by ATR per bar


TREND_CFG_BY_TF: dict[str, TrendCfg] = {
    "1D": TrendCfg(lookback=20),
    "60m": TrendCfg(lookback=40),
    "5m": TrendCfg(lookback=60),
}


def _sma(series: pd.Series, n: int) -> float | None:
    if len(series) < n:
        return None
    return float(series.rolling(n).mean().iloc[-1])


def _ema(series: pd.Series, n: int) -> float | None:
    if len(series) < n:
        return None
    return float(series.ewm(span=n, adjust=False).mean().iloc[-1])


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["c"].shift(1)
    tr = pd.concat(
        [
            df["h"] - df["l"],
            (df["h"] - prev_close).abs(),
            (df["l"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def _atr(df: pd.DataFrame, n: int) -> float | None:
    if len(df) < n + 1:
        return None
    tr = _true_range(df)
    return float(tr.rolling(n).mean().iloc[-1])


def _vwap_intraday(df: pd.DataFrame) -> float | None:
    if df.empty:
        return None
    vol = df["v"].astype(float)
    total_vol = vol.sum()
    if total_vol <= 0:
        return None
    tp = (df["h"] + df["l"] + df["c"]) / 3.0
    return float((tp * vol).sum() / total_vol)


def _trend_strength_regression(df: pd.DataFrame, tf: str) -> TrendOut:
    cfg = TREND_CFG_BY_TF.get(tf, TrendCfg(lookback=20))
    n = cfg.lookback
    closes = df["c"].astype(float)
    if len(closes) < n:
        return {"direction": "NEUTRAL", "strength": 0.0}

    window = closes.iloc[-n:]
    x = pd.Series(range(n), dtype=float)
    y = window.reset_index(drop=True)
    x_mean = x.mean()
    y_mean = y.mean()
    denom = ((x - x_mean) ** 2).sum()
    beta = float(((x - x_mean) * (y - y_mean)).sum() / denom) if denom else 0.0

    atr14 = _atr(df, 14)
    if atr14 is None or atr14 <= 0:
        strength = min(1.0, abs(beta))  # fallback normalization
        eps = 1e-9
    else:
        atr_per_bar = atr14 / n
        strength = max(0.0, min(1.0, abs(beta) / (atr_per_bar + 1e-9)))
        eps = cfg.eps_mult * atr_per_bar

    if beta > eps:
        direction = "UP"
    elif beta < -eps:
        direction = "DOWN"
    else:
        direction = "FLAT"

    return {"direction": direction, "strength": float(strength)}


def _notes_for_tf(
    df: pd.DataFrame,
    sma_map: dict[str, float],
    vwap: float | None,
) -> list[str]:
    notes: list[str] = []
    last = float(df["c"].iloc[-1])

    s20 = sma_map.get("20")
    s50 = sma_map.get("50")
    s200 = sma_map.get("200")
    if s20 and s50 and s200:
        if last > s20 > s50 > s200:
            notes.append("Above 20/50/200 SMA (bull stack)")
        elif last < s20 < s50 < s200:
            notes.append("Below 20/50/200 SMA (bear stack)")

    if vwap is not None:
        if last > vwap:
            notes.append("Above session VWAP")
        elif last < vwap:
            notes.append("Below session VWAP")

    atr14 = _atr(df, 14)
    if atr14 is not None:
        recent = df.iloc[-min(20, len(df)) :]
        range_span = float(recent["h"].max() - recent["l"].min())
        if range_span <= 0.9 * atr14:
            notes.append("Compression / tight range")

    return notes


def compute_timeframe_state(df: pd.DataFrame, tf: str, include_vwap: bool) -> TFState:
    state: TFState = {}
    df = df.sort_values("ts").reset_index(drop=True)
    closes = df["c"].astype(float)

    sma_map: dict[str, float] = {}
    for n in DEFAULT_SMA:
        value = _sma(closes, n)
        if value is not None:
            sma_map[str(n)] = value

    ema_map: dict[str, float] = {}
    for n in DEFAULT_EMA:
        value = _ema(closes, n)
        if value is not None:
            ema_map[str(n)] = value

    atr_map: dict[str, float] = {}
    for n in DEFAULT_ATR:
        value = _atr(df, n)
        if value is not None:
            atr_map[str(n)] = value

    vwap = _vwap_intraday(df) if include_vwap else None

    # Momentum indicators (used by coach + internal gating)
    closes = [float(x) for x in df["c"].tolist() if x is not None]
    rsi_14 = _rsi(closes, 14) if len(closes) >= 15 else None
    macd_val, macd_sig, macd_hist = (None, None, None)
    if len(closes) >= 35:
        macd_val, macd_sig, macd_hist = _macd(closes, 12, 26, 9)
    last_close = closes[-1] if closes else None
    macd_hist_pct = None
    if macd_hist is not None and last_close:
        try:
            macd_hist_pct = abs(float(macd_hist)) / float(last_close) * 100.0
        except Exception:  # noqa: BLE001
            macd_hist_pct = None

    state["trend"] = _trend_strength_regression(df, tf)
    if sma_map:
        state["sma"] = sma_map
    if ema_map:
        state["ema"] = ema_map
    if atr_map:
        state["atr"] = atr_map
    state["vwap"] = vwap
    state["rsi_14"] = rsi_14
    state["macd"] = float(macd_val) if macd_val is not None else None
    state["macd_signal"] = float(macd_sig) if macd_sig is not None else None
    state["macd_hist"] = float(macd_hist) if macd_hist is not None else None
    state["macd_hist_pct"] = float(macd_hist_pct) if macd_hist_pct is not None else None
    state["notes"] = _notes_for_tf(df, sma_map, vwap)
    return state


def build_technical_state(
    symbol: str,
    bars_by_tf: dict[str, pd.DataFrame],
    levels: dict[str, float] | None = None,
    custom_levels: Iterable[dict[str, float | str]] | None = None,
) -> TechnicalSymbolState:
    state: TechnicalSymbolState = {"timeframes": {}, "levels": {}}

    for tf, df in bars_by_tf.items():
        include_vwap = tf in {"5m", "1m", "15m", "60m"}
        state["timeframes"][tf] = compute_timeframe_state(df, tf, include_vwap)

    if levels:
        state["levels"].update(levels)
    if custom_levels:
        state["levels"].setdefault("custom", [])
        state["levels"]["custom"].extend(custom_levels)

    return state
