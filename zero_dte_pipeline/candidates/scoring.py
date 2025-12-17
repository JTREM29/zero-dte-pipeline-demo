"""Candidate scoring logic for 0DTE options.

Provides scoring based on:
- Direction alignment
- Regime detection
- Confidence levels
- IV signals
- Order flow
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from zero_dte_pipeline.utils.logging import get_logger

logger = get_logger(__name__)


class Direction(Enum):
    """Market direction."""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class Regime(Enum):
    """Market regime."""
    LOW_VOL = "low_volatility"
    HIGH_VOL = "high_volatility"
    TRENDING = "trending"
    RANGING = "ranging"
    UNKNOWN = "unknown"


class Strategy(Enum):
    """Trading strategy type."""
    SHORT_PREMIUM = "short_premium"
    TREND_FOLLOWING = "trend_following"
    VERTICAL = "vertical"
    BUTTERFLY = "butterfly"
    IRON_CONDOR = "iron_condor"


@dataclass
class SignalAlignment:
    """Signal alignment data."""
    direction: Direction
    direction_confidence: float
    regime: Regime
    regime_confidence: float
    iv_signal: float  # -1 to 1 (negative = IV low, positive = IV high)
    order_flow_signal: float  # -1 to 1 (negative = bearish, positive = bullish)
    
    def calculate_alignment_score(self) -> float:
        """Calculate overall signal alignment score.
        
        Returns:
            Score from 0 to 1, where 1 is perfect alignment.
        """
        # Weight each component
        weights = {
            "direction": 0.30,
            "regime": 0.20,
            "iv": 0.25,
            "order_flow": 0.25,
        }
        
        # Direction score
        direction_score = self.direction_confidence if self.direction != Direction.UNKNOWN else 0
        
        # Regime score
        regime_score = self.regime_confidence if self.regime != Regime.UNKNOWN else 0.5
        
        # IV signal score (depends on strategy)
        iv_score = abs(self.iv_signal)  # Strong signal either way is good
        
        # Order flow score
        of_score = abs(self.order_flow_signal)
        
        # Calculate weighted score
        total = (
            weights["direction"] * direction_score +
            weights["regime"] * regime_score +
            weights["iv"] * iv_score +
            weights["order_flow"] * of_score
        )
        
        return min(1.0, max(0.0, total))


@dataclass
class Candidate:
    """Trading candidate."""
    symbol: str
    underlying: str
    strike: float
    option_type: str  # 'call' or 'put'
    expiration: datetime
    strategy: Strategy
    direction: Direction
    entry_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    score: float = 0.0
    confidence: float = 0.0
    signals: Optional[SignalAlignment] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    rejection_reasons: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "symbol": self.symbol,
            "underlying": self.underlying,
            "strike": self.strike,
            "option_type": self.option_type,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "strategy": self.strategy.value if self.strategy else None,
            "direction": self.direction.value if self.direction else None,
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "stop_price": self.stop_price,
            "score": self.score,
            "confidence": self.confidence,
            "rejection_reasons": self.rejection_reasons,
            "metadata": self.metadata,
        }


class CandidateScorer:
    """Scores trading candidates based on multiple factors.
    
    Improved scoring logic that:
    - Aligns direction, regime, confidence, IV, and order flow
    - Reduces hyper-strict gating
    - Supports SPY, QQQ, IWM in addition to SPX
    """
    
    # Supported underlying symbols
    SUPPORTED_UNDERLYINGS = ["SPX", "SPY", "QQQ", "IWM"]
    
    def __init__(
        self,
        min_confidence: float = 0.25,  # Allow slightly lower confidence before rejection
        iv_threshold: float = 0.2,
        direction_weight: float = 0.30,
        regime_weight: float = 0.20,
        iv_weight: float = 0.25,
        order_flow_weight: float = 0.25,
    ):
        self.min_confidence = min_confidence
        self.iv_threshold = iv_threshold
        self.weights = {
            "direction": direction_weight,
            "regime": regime_weight,
            "iv": iv_weight,
            "order_flow": order_flow_weight,
        }
        
        # Metrics tracking
        self.metrics = {
            "total_scored": 0,
            "passed": 0,
            "rejected": 0,
            "rejection_reasons": {},
            "regime_unknown_count": 0,
            "direction_override_count": 0,
        }
    
    def score_candidate(
        self,
        candidate: Candidate,
        signals: SignalAlignment,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> Candidate:
        """Score a single candidate.
        
        Args:
            candidate: The candidate to score
            signals: Signal alignment data
            market_data: Optional market data for additional scoring
            
        Returns:
            Scored candidate with updated score and confidence.
        """
        self.metrics["total_scored"] += 1
        candidate.signals = signals
        candidate.rejection_reasons = []
        
        # Track regime unknown
        if signals.regime == Regime.UNKNOWN:
            self.metrics["regime_unknown_count"] += 1
        
        # Calculate base score from signal alignment
        alignment_score = signals.calculate_alignment_score()
        
        # Direction alignment bonus/penalty
        direction_multiplier = 1.0
        if candidate.direction != Direction.UNKNOWN and signals.direction != Direction.UNKNOWN:
            if candidate.direction == signals.direction:
                direction_multiplier = 1.2  # 20% bonus
            else:
                direction_multiplier = 0.7  # 30% penalty
                candidate.rejection_reasons.append("direction_mismatch")
        
        # IV-based adjustments
        iv_multiplier = 1.0
        if signals.iv_signal > self.iv_threshold:
            # High IV - favor short premium
            if candidate.strategy == Strategy.SHORT_PREMIUM:
                iv_multiplier = 1.15
            elif candidate.strategy == Strategy.TREND_FOLLOWING:
                iv_multiplier = 0.9
        elif signals.iv_signal < -self.iv_threshold:
            # Low IV - favor trend following
            if candidate.strategy == Strategy.TREND_FOLLOWING:
                iv_multiplier = 1.15
            elif candidate.strategy == Strategy.SHORT_PREMIUM:
                iv_multiplier = 0.9
        
        # Order flow alignment
        of_multiplier = 1.0
        if candidate.direction == Direction.BULLISH and signals.order_flow_signal > 0.2:
            of_multiplier = 1.1
        elif candidate.direction == Direction.BEARISH and signals.order_flow_signal < -0.2:
            of_multiplier = 1.1
        elif (
            (candidate.direction == Direction.BULLISH and signals.order_flow_signal < -0.3) or
            (candidate.direction == Direction.BEARISH and signals.order_flow_signal > 0.3)
        ):
            of_multiplier = 0.8
            candidate.rejection_reasons.append("order_flow_contrary")
        
        # Regime-strategy alignment
        regime_multiplier = 1.0
        if signals.regime == Regime.HIGH_VOL and candidate.strategy in [
            Strategy.SHORT_PREMIUM, Strategy.IRON_CONDOR
        ]:
            regime_multiplier = 1.15
        elif signals.regime == Regime.LOW_VOL and candidate.strategy == Strategy.BUTTERFLY:
            regime_multiplier = 1.1
        elif signals.regime == Regime.TRENDING and candidate.strategy == Strategy.TREND_FOLLOWING:
            regime_multiplier = 1.2
        
        # Calculate final score
        base_score = alignment_score
        final_score = base_score * direction_multiplier * iv_multiplier * of_multiplier * regime_multiplier
        
        # Normalize to 0-1 range
        candidate.score = min(1.0, max(0.0, final_score))
        
        # Calculate confidence
        candidate.confidence = (
            signals.direction_confidence * self.weights["direction"] +
            signals.regime_confidence * self.weights["regime"] +
            (1.0 - abs(1.0 - abs(signals.iv_signal))) * self.weights["iv"] +
            abs(signals.order_flow_signal) * self.weights["order_flow"]
        )
        
        # Check minimum confidence
        if candidate.confidence < self.min_confidence:
            candidate.rejection_reasons.append(f"low_confidence_{candidate.confidence:.2f}")
        
        # Update metrics
        if candidate.rejection_reasons:
            self.metrics["rejected"] += 1
            for reason in candidate.rejection_reasons:
                self.metrics["rejection_reasons"][reason] = (
                    self.metrics["rejection_reasons"].get(reason, 0) + 1
                )
        else:
            self.metrics["passed"] += 1
        
        return candidate
    
    def score_candidates(
        self,
        candidates: List[Candidate],
        signals: SignalAlignment,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> List[Candidate]:
        """Score multiple candidates.
        
        Args:
            candidates: List of candidates to score
            signals: Signal alignment data
            market_data: Optional market data
            
        Returns:
            List of scored candidates, sorted by score descending.
        """
        scored = [
            self.score_candidate(c, signals, market_data)
            for c in candidates
        ]
        
        # Sort by score descending
        scored.sort(key=lambda c: c.score, reverse=True)
        
        return scored
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get scoring metrics for observability."""
        return {
            **self.metrics,
            "pass_rate": (
                self.metrics["passed"] / self.metrics["total_scored"]
                if self.metrics["total_scored"] > 0 else 0
            ),
        }
    
    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        self.metrics = {
            "total_scored": 0,
            "passed": 0,
            "rejected": 0,
            "rejection_reasons": {},
            "regime_unknown_count": 0,
            "direction_override_count": 0,
        }


def delta_bucket_bias(
    options_data: pd.DataFrame,
    target_delta: float = 0.25,
) -> tuple:
    """Calculate delta bucket bias from options data.
    
    Args:
        options_data: DataFrame with columns: strike, delta, iv, type
        target_delta: Target delta for comparison
        
    Returns:
        Tuple of (bias, skew) where bias is 'bullish' or 'bearish'
    """
    calls = options_data[options_data["type"] == "call"]
    puts = options_data[options_data["type"] == "put"]
    
    if calls.empty or puts.empty:
        return "neutral", 0.0
    
    # Find options near target delta
    call_near = calls.iloc[(calls["delta"] - target_delta).abs().argsort()[:1]]
    put_near = puts.iloc[(puts["delta"] + target_delta).abs().argsort()[:1]]
    
    if call_near.empty or put_near.empty:
        return "neutral", 0.0
    
    skew = call_near["iv"].mean() - put_near["iv"].mean()
    bias = "bullish" if skew < 0 else "bearish"
    
    return bias, skew
