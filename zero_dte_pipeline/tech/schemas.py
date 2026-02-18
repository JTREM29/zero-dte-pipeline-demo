"""Typed schema definitions for technical and pattern outputs."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

TrendDir = Literal["UP", "DOWN", "FLAT", "NEUTRAL"]
Posture = Literal[
    "ACCEPTANCE_ABOVE_VWAP",
    "ACCEPTANCE_BELOW_VWAP",
    "REJECTION_AT_VWAP",
    "BALANCED",
    "UNKNOWN",
]


class TrendOut(TypedDict):
    direction: TrendDir
    strength: float  # range 0..1


class TFState(TypedDict, total=False):
    trend: TrendOut
    sma: dict[str, float]
    ema: dict[str, float]
    atr: dict[str, float]
    vwap: float | None
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    macd_hist_pct: float | None
    notes: list[str]


class LevelsOut(TypedDict, total=False):
    pivot: float
    r1: float
    s1: float
    r2: float
    s2: float
    custom: list[dict[str, float | str]]


class TechnicalSymbolState(TypedDict, total=False):
    timeframes: dict[str, TFState]
    levels: LevelsOut


class PatternCandidate(TypedDict, total=False):
    name: str
    confidence: float
    lookback_bars: int
    invalidation: float
    breakout: NotRequired[float]
    neckline: NotRequired[float]
    evidence: NotRequired[list[str]]


class PatternOut(TypedDict, total=False):
    notes: list[str]
    by_tf: dict[str, list[PatternCandidate]]
