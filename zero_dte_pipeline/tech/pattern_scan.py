"""Conservative pattern detectors for common continuation and reversal setups."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .schemas import PatternCandidate, PatternOut
from .technical_features import _atr, _trend_strength_regression


@dataclass(frozen=True)
class PivotCfg:
    k: int = 2


def pivot_highs(df: pd.DataFrame, k: int = 2) -> list[int]:
    highs = df["h"].astype(float).reset_index(drop=True)
    idxs: list[int] = []
    for i in range(k, len(highs) - k):
        window = highs.iloc[i - k : i + k + 1]
        if highs.iloc[i] == window.max() and (window == highs.iloc[i]).sum() == 1:
            idxs.append(i)
    return idxs


def pivot_lows(df: pd.DataFrame, k: int = 2) -> list[int]:
    lows = df["l"].astype(float).reset_index(drop=True)
    idxs: list[int] = []
    for i in range(k, len(lows) - k):
        window = lows.iloc[i - k : i + k + 1]
        if lows.iloc[i] == window.min() and (window == lows.iloc[i]).sum() == 1:
            idxs.append(i)
    return idxs


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def detect_bull_flag(df: pd.DataFrame, tf: str) -> list[PatternCandidate]:
    df = df.sort_values("ts").reset_index(drop=True)
    if len(df) < 40:
        return []

    closes = df["c"].astype(float)
    atr14 = _atr(df, 14) or 0.0
    trend = _trend_strength_regression(df, tf)
    if trend["direction"] != "UP" or trend["strength"] < 0.55:
        return []

    candidates: list[PatternCandidate] = []

    for pole in (6, 8, 10, 12):
        for flag_len in (6, 8, 10, 12):
            end_idx = len(df) - 1
            pole_start = end_idx - (pole + flag_len)
            flag_start = end_idx - flag_len
            if pole_start < 0:
                continue

            pole_return = (closes.iloc[flag_start] - closes.iloc[pole_start]) / max(
                1e-9, closes.iloc[pole_start]
            )
            pole_threshold = 0.03 if tf == "1D" else 0.015
            if pole_return < pole_threshold:
                continue

            flag = df.iloc[flag_start : end_idx + 1]
            flag_range = float(flag["h"].max() - flag["l"].min())
            if atr14 > 0 and flag_range > 1.2 * atr14:
                continue

            flag_closes = flag["c"].astype(float)
            drift = (flag_closes.iloc[-1] - flag_closes.iloc[0]) / max(
                1e-9, abs(flag_closes.iloc[0])
            )
            if drift > 0.01:
                continue

            breakout = float(flag["h"].max())
            invalidation = float(flag["l"].min())

            confidence = 0.40
            big_pole = 0.05 if tf == "1D" else 0.025
            if pole_return >= big_pole:
                confidence += 0.20
            if atr14 > 0 and flag_range <= 0.9 * atr14:
                confidence += 0.20
            if 7 <= flag_len <= 12:
                confidence += 0.10
            if trend["strength"] >= 0.65:
                confidence += 0.10

            confidence = _clamp(confidence)
            candidates.append(
                {
                    "name": "bull_flag",
                    "confidence": confidence,
                    "lookback_bars": pole + flag_len,
                    "invalidation": invalidation,
                    "breakout": breakout,
                    "evidence": [
                        f"pole_return={pole_return:.3f}",
                        f"flag_range={flag_range:.2f}",
                        f"trend_strength={trend['strength']:.2f}",
                    ],
                }
            )

    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:1]


def detect_bear_flag(df: pd.DataFrame, tf: str) -> list[PatternCandidate]:
    df = df.sort_values("ts").reset_index(drop=True)
    if len(df) < 40:
        return []

    closes = df["c"].astype(float)
    atr14 = _atr(df, 14) or 0.0
    trend = _trend_strength_regression(df, tf)
    if trend["direction"] != "DOWN" or trend["strength"] < 0.55:
        return []

    candidates: list[PatternCandidate] = []

    for pole in (6, 8, 10, 12):
        for flag_len in (6, 8, 10, 12):
            end_idx = len(df) - 1
            pole_start = end_idx - (pole + flag_len)
            flag_start = end_idx - flag_len
            if pole_start < 0:
                continue

            pole_return = (closes.iloc[flag_start] - closes.iloc[pole_start]) / max(
                1e-9, closes.iloc[pole_start]
            )
            pole_threshold = -0.03 if tf == "1D" else -0.015
            if pole_return > pole_threshold:
                continue

            flag = df.iloc[flag_start : end_idx + 1]
            flag_range = float(flag["h"].max() - flag["l"].min())
            if atr14 > 0 and flag_range > 1.2 * atr14:
                continue

            flag_closes = flag["c"].astype(float)
            drift = (flag_closes.iloc[-1] - flag_closes.iloc[0]) / max(
                1e-9, abs(flag_closes.iloc[0])
            )
            if drift < -0.01:
                continue

            breakdown = float(flag["l"].min())
            invalidation = float(flag["h"].max())

            confidence = 0.40
            big_pole = -0.05 if tf == "1D" else -0.025
            if pole_return <= big_pole:
                confidence += 0.20
            if atr14 > 0 and flag_range <= 0.9 * atr14:
                confidence += 0.20
            if 7 <= flag_len <= 12:
                confidence += 0.10
            if trend["strength"] >= 0.65:
                confidence += 0.10

            confidence = _clamp(confidence)
            candidates.append(
                {
                    "name": "bear_flag",
                    "confidence": confidence,
                    "lookback_bars": pole + flag_len,
                    "invalidation": invalidation,
                    "breakout": breakdown,
                    "evidence": [
                        f"pole_return={pole_return:.3f}",
                        f"flag_range={flag_range:.2f}",
                        f"trend_strength={trend['strength']:.2f}",
                    ],
                }
            )

    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:1]


def detect_head_and_shoulders(df: pd.DataFrame, tf: str) -> list[PatternCandidate]:
    del tf  # tf unused but kept for signature consistency
    df = df.sort_values("ts").reset_index(drop=True)
    if len(df) < 80:
        return []

    ph = pivot_highs(df, k=3)
    pl = pivot_lows(df, k=3)
    if len(ph) < 3 or len(pl) < 2:
        return []

    highs = df["h"].astype(float).reset_index(drop=True)
    lows = df["l"].astype(float).reset_index(drop=True)

    candidates: list[PatternCandidate] = []

    ph_recent = ph[-10:]
    for i in range(len(ph_recent) - 2):
        s1 = ph_recent[i]
        head = ph_recent[i + 1]
        s2 = ph_recent[i + 2]
        if not (s1 < head < s2):
            continue

        s1_val = highs.iloc[s1]
        head_val = highs.iloc[head]
        s2_val = highs.iloc[s2]

        if head_val < s1_val * 1.02 or head_val < s2_val * 1.02:
            continue

        if abs(s1_val - s2_val) / max(1e-9, s1_val) > 0.03:
            continue

        seg1 = lows.iloc[s1:head + 1]
        seg2 = lows.iloc[head:s2 + 1]
        if seg1.empty or seg2.empty:
            continue

        l1 = float(seg1.min())
        l2 = float(seg2.min())
        neckline = (l1 + l2) / 2.0
        invalidation = float(s2_val)

        confidence = 0.25
        symmetry = 1.0 - min(1.0, abs(s1_val - s2_val) / max(1e-9, 0.03 * s1_val))
        neckline_flat = 1.0 - min(1.0, abs(l1 - l2) / max(1e-9, 0.02 * neckline))
        prominence = (head_val - max(s1_val, s2_val)) / max(1e-9, 0.05 * head_val)
        confidence += 0.25 * _clamp(symmetry)
        confidence += 0.25 * _clamp(neckline_flat)
        confidence += 0.15 * _clamp(prominence)
        confidence = _clamp(confidence)

        candidates.append(
            {
                "name": "head_and_shoulders",
                "confidence": confidence,
                "lookback_bars": int(s2 - s1),
                "invalidation": invalidation,
                "neckline": float(neckline),
                "evidence": [
                    f"S1={s1_val:.2f} H={head_val:.2f} S2={s2_val:.2f}",
                    f"neckline≈{neckline:.2f}",
                ],
            }
        )

    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:1]


def detect_inverse_head_and_shoulders(df: pd.DataFrame, tf: str) -> list[PatternCandidate]:
    del tf
    df = df.sort_values("ts").reset_index(drop=True)
    if len(df) < 80:
        return []

    pl = pivot_lows(df, k=3)
    if len(pl) < 3:
        return []

    highs = df["h"].astype(float).reset_index(drop=True)
    lows = df["l"].astype(float).reset_index(drop=True)

    candidates: list[PatternCandidate] = []
    pl_recent = pl[-10:]

    for i in range(len(pl_recent) - 2):
        s1 = pl_recent[i]
        head = pl_recent[i + 1]
        s2 = pl_recent[i + 2]
        if not (s1 < head < s2):
            continue

        s1_val = lows.iloc[s1]
        head_val = lows.iloc[head]
        s2_val = lows.iloc[s2]

        if head_val > s1_val * 0.98 or head_val > s2_val * 0.98:
            continue

        if abs(s1_val - s2_val) / max(1e-9, s1_val) > 0.03:
            continue

        seg1 = highs.iloc[s1:head + 1]
        seg2 = highs.iloc[head:s2 + 1]
        if seg1.empty or seg2.empty:
            continue

        h1 = float(seg1.max())
        h2 = float(seg2.max())
        neckline = (h1 + h2) / 2.0
        invalidation = float(s2_val)

        confidence = 0.25
        symmetry = 1.0 - min(1.0, abs(s1_val - s2_val) / max(1e-9, 0.03 * s1_val))
        neckline_flat = 1.0 - min(1.0, abs(h1 - h2) / max(1e-9, 0.02 * neckline))
        prominence = (min(s1_val, s2_val) - head_val) / max(
            1e-9, 0.05 * abs(head_val)
        )
        confidence += 0.25 * _clamp(symmetry)
        confidence += 0.25 * _clamp(neckline_flat)
        confidence += 0.15 * _clamp(prominence)
        confidence = _clamp(confidence)

        candidates.append(
            {
                "name": "inverse_head_and_shoulders",
                "confidence": confidence,
                "lookback_bars": int(s2 - s1),
                "invalidation": invalidation,
                "neckline": float(neckline),
                "evidence": [
                    f"S1={s1_val:.2f} H={head_val:.2f} S2={s2_val:.2f}",
                    f"neckline≈{neckline:.2f}",
                ],
            }
        )

    candidates.sort(key=lambda x: x["confidence"], reverse=True)
    return candidates[:1]


def scan_patterns(df: pd.DataFrame, tf: str) -> list[PatternCandidate]:
    out: list[PatternCandidate] = []
    out.extend(detect_bull_flag(df, tf))
    out.extend(detect_bear_flag(df, tf))
    out.extend(detect_head_and_shoulders(df, tf))
    out.extend(detect_inverse_head_and_shoulders(df, tf))
    out.sort(key=lambda x: x["confidence"], reverse=True)
    return out[:2]


def build_pattern_candidates(
    symbol: str,
    bars_by_tf: dict[str, pd.DataFrame],
    min_confidence: float = 0.60,
) -> PatternOut:
    result: PatternOut = {
        "notes": [
            f"Only mention patterns with confidence >= {min_confidence:.2f}",
            "Patterns are context; always cite invalidation.",
        ],
        "by_tf": {},
    }

    for tf, df in bars_by_tf.items():
        candidates = [
            c for c in scan_patterns(df, tf) if float(c.get("confidence", 0.0)) >= min_confidence
        ]
        result["by_tf"][tf] = candidates

    return result
