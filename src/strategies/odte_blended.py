"""Blended multi-factor intraday 0DTE scoring strategy.

Registered name: odte_blended

Combines technical + contextual factors into a composite normalized score:
  - RSI (mean reversion or momentum mode)
  - Bollinger band z-position
  - VWAP deviation
  - Optional option skew factor (expected in market_ctx['skew_norm'] in [-1,1])
    - Optional greeks factor (expected in market_ctx['greeks_norm'] in [-1,1])
  - Regime filters (choppiness / volatility) passed via market_ctx
  - Seasonality bias (market_ctx['seasonality_bias'] additive or multiplicative)

Emits StrategySignal objects (re-uses dataclass from simple_intraday_spx) for:
  - odte_score (continuous composite)
  - enter_long / exit_long / enter_short / exit_short (threshold + hysteresis)

Warm-up: waits until min_period = max(rsi_period+1, bb_period) samples before
producing directional signals; still emits a warming score if partial data.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Deque, Iterable, Optional
from collections import deque
import math
import numpy as np

from .registry import register_strategy
from .simple_intraday_spx import StrategySignal  # reuse structure
from ..utils.indicators import rsi as rsi_simple, bollinger, VWAPStream


@dataclass
class _State:
    prices: Deque[float]
    vwap: VWAPStream
    position: int = 0  # -1 short, 0 flat, 1 long
    last_score: Optional[float] = None


@register_strategy("odte_blended")
class ODTEBlendedStrategy:
    """Composite intraday scoring.

    Parameters
    ----------
    rsi_period : int
        Lookback for RSI.
    bb_period : int
        Lookback for Bollinger mid/std.
    bb_mult : float
        Std deviation multiplier.
    vwap_beta : float
        Scaling denominator for VWAP deviation normalization.
    mode : str
        'meanrev' or 'momentum' interpretation of RSI component.
    skew_weight, greeks_weight, rsi_weight, bb_weight, vwap_weight : float
        Component weights (will be re-normalized if sum != 1).
    entry_threshold : float
        Score >= threshold => long; <= -threshold => short.
    exit_threshold : float
        Position exit threshold (hysteresis) must cross back inside +/- exit.
    clip_score : bool
        If True apply tanh to composite to bound [-1,1].
    """

    def __init__(
        self,
        rsi_period: int = 14,
        bb_period: int = 20,
        bb_mult: float = 2.0,
        vwap_beta: float = 0.003,
        mode: str = "meanrev",
        rsi_weight: float = 0.25,
        bb_weight: float = 0.25,
        vwap_weight: float = 0.25,
        skew_weight: float = 0.0,
        greeks_weight: float = 0.0,
        entry_threshold: float = 0.6,
        exit_threshold: float = 0.25,
        clip_score: bool = True,
    ):
        if rsi_period <= 1:
            raise ValueError("rsi_period must be >1")
        if bb_period <= 1:
            raise ValueError("bb_period must be >1")
        if entry_threshold <= 0 or exit_threshold < 0 or exit_threshold >= entry_threshold:
            raise ValueError("Require 0 < exit_threshold < entry_threshold")
        self.rsi_period = rsi_period
        self.bb_period = bb_period
        self.bb_mult = bb_mult
        self.vwap_beta = vwap_beta
        self.mode = mode.lower()
        if self.mode not in ("meanrev", "momentum"):
            raise ValueError("mode must be 'meanrev' or 'momentum'")
        weights = np.array([rsi_weight, bb_weight, vwap_weight, skew_weight, greeks_weight], dtype=float)
        if (weights < 0).any():
            raise ValueError("Weights must be non-negative")
        s = weights.sum()
        if s > 0:
            self.weights = weights / s
        else:
            # Default back to equal weights for first 4, zero for greeks if nothing provided
            self.weights = np.array([0.25, 0.25, 0.25, 0.25, 0.0])
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.clip_score = clip_score
        self._state = _State(prices=deque(maxlen=max(bb_period, rsi_period) * 4), vwap=VWAPStream())

    # --- Helper component scorers -------------------------------------------------
    def _rsi_component(self, prices: np.ndarray) -> float:
        val = rsi_simple(prices, period=self.rsi_period)
        if math.isnan(val):
            return 0.0
        # Map RSI -> [-1,1]
        mid = 50.0
        if self.mode == "meanrev":
            comp = (mid - val) / 50.0  # Overbought (>50) -> negative, oversold (<50) -> positive
        else:  # momentum
            comp = (val - mid) / 50.0
        return max(-1.0, min(1.0, comp))

    def _bb_component(self, prices: np.ndarray) -> float:
        mid, upper, lower = bollinger(prices, period=self.bb_period, num_std=self.bb_mult)
        if math.isnan(mid) or math.isnan(upper) or upper == mid:
            return 0.0
        price = prices[-1]
        std = (upper - mid) / max(self.bb_mult, 1e-9)
        if std <= 1e-12:
            return 0.0
        z = (price - mid) / (self.bb_mult * std)
        return float(max(-1.0, min(1.0, z)))

    def _vwap_component(self, price: float, vwap_val: float) -> float:
        if vwap_val is None or vwap_val == 0:
            return 0.0
        dev = (price - vwap_val) / (vwap_val * self.vwap_beta)
        return float(max(-1.0, min(1.0, dev)))

    # --- Core evaluation ----------------------------------------------------------
    def evaluate(self, market_ctx: dict[str, Any]) -> Iterable[StrategySignal]:
        price = float(market_ctx.get("lastPrice", 0.0))
        volume = float(market_ctx.get("lastSize", 0.0))
        self._state.prices.append(price)
        vwap_val = self._state.vwap.update(price, volume)
        prices_np = np.fromiter(self._state.prices, dtype=float)

        rsi_c = self._rsi_component(prices_np)
        bb_c = self._bb_component(prices_np)
        vwap_c = self._vwap_component(price, vwap_val)
        skew_c = float(market_ctx.get("skew_norm", 0.0))
        skew_c = max(-1.0, min(1.0, skew_c))

        greeks_c = float(market_ctx.get("greeks_norm", 0.0))
        greeks_c = max(-1.0, min(1.0, greeks_c))

        components = np.array([rsi_c, bb_c, vwap_c, skew_c, greeks_c])
        raw_score = float(np.dot(self.weights, components))

        # Regime / seasonality adjustments
        # seasonality_bias: additive mild tilt; regime 'choppy' may attenuate
        season_bias = float(market_ctx.get("seasonality_bias", 0.0))
        if season_bias:
            raw_score += season_bias
        regime = market_ctx.get("regime")
        if regime == "choppy":
            raw_score *= 0.6  # attenuate in chop
        elif regime == "trending":
            raw_score *= 1.1  # modest boost

        score = math.tanh(raw_score) if self.clip_score else raw_score
        # Bound for safety
        score = max(-1.0, min(1.0, score))
        self._state.last_score = score

        meta = {
            "rsi_component": rsi_c,
            "bb_component": bb_c,
            "vwap_component": vwap_c,
            "skew_component": skew_c,
            "greeks_component": greeks_c,
            "weights": self.weights.tolist(),
            "raw_score": raw_score,
            "price": price,
            "vwap": vwap_val,
            "regime": regime,
            "seasonality_bias": season_bias,
        }
        yield StrategySignal(name="odte_score", value=price, metadata=meta, strength=abs(score))

        # Warm-up gating
        if len(self._state.prices) < max(self.rsi_period + 1, self.bb_period):
            return

        # Directional / position management
        pos = self._state.position
        if pos == 0:
            if score >= self.entry_threshold:
                self._state.position = 1
                yield StrategySignal(
                    name="enter_long",
                    value=price,
                    metadata={"score": score, **meta},
                    strength=abs(score),
                )
            elif score <= -self.entry_threshold:
                self._state.position = -1
                yield StrategySignal(
                    name="enter_short",
                    value=price,
                    metadata={"score": score, **meta},
                    strength=abs(score),
                )
        elif pos == 1:
            if score <= self.exit_threshold:
                self._state.position = 0
                yield StrategySignal(
                    name="exit_long",
                    value=price,
                    metadata={"score": score, **meta},
                    strength=abs(score),
                )
        elif pos == -1:
            if score >= -self.exit_threshold:
                self._state.position = 0
                yield StrategySignal(
                    name="exit_short",
                    value=price,
                    metadata={"score": score, **meta},
                    strength=abs(score),
                )

    # Optional price-only adapter for symmetry with odte_direction
    def on_price(self, price: float) -> float:  # pragma: no cover
        # Minimal adapter: just push through evaluate with synthetic ctx
        ctx = {"lastPrice": price, "lastSize": 1}
        list(self.evaluate(ctx))
        return self._state.last_score if self._state.last_score is not None else 0.0

__all__ = ["ODTEBlendedStrategy"]