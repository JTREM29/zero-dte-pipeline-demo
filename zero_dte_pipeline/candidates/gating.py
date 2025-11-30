"""Gating manager for candidate approval.

Controls which candidates are approved for trading based on
risk management rules and signal alignment.
"""
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, List, Optional

from zero_dte_pipeline.candidates.scoring import Candidate, Direction, Regime
from zero_dte_pipeline.config import config
from zero_dte_pipeline.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GatingResult:
    """Result of gating check."""
    approved: bool
    candidate: Candidate
    reasons: List[str]
    gate_scores: Dict[str, float]


class GatingManager:
    """Manages gating rules for candidate approval.
    
    Implements configurable gating with:
    - Adjustable strictness levels
    - Time-based rules
    - Risk-based limits
    - Metrics tracking
    """
    
    # Strictness presets
    STRICTNESS_PRESETS = {
        "loose": {
            "min_score": 0.2,
            "min_confidence": 0.25,
            "max_daily_trades": 20,
            "max_concurrent_positions": 10,
            "allow_unknown_regime": True,
            "allow_direction_mismatch": True,
        },
        "moderate": {
            "min_score": 0.35,
            "min_confidence": 0.35,
            "max_daily_trades": 10,
            "max_concurrent_positions": 5,
            "allow_unknown_regime": True,
            "allow_direction_mismatch": False,
        },
        "strict": {
            "min_score": 0.5,
            "min_confidence": 0.5,
            "max_daily_trades": 5,
            "max_concurrent_positions": 3,
            "allow_unknown_regime": False,
            "allow_direction_mismatch": False,
        },
    }
    
    def __init__(
        self,
        strictness: str = "moderate",
        custom_settings: Optional[Dict[str, Any]] = None,
    ):
        """Initialize the gating manager.
        
        Args:
            strictness: Strictness level (loose, moderate, strict)
            custom_settings: Optional custom settings to override defaults
        """
        # Get strictness from config or use provided
        self.strictness = strictness or config.gating_strictness
        
        # Load preset settings
        self.settings = self.STRICTNESS_PRESETS.get(
            self.strictness,
            self.STRICTNESS_PRESETS["moderate"]
        ).copy()
        
        # Apply custom overrides
        if custom_settings:
            self.settings.update(custom_settings)
        
        # State tracking
        self._daily_trade_count = 0
        self._concurrent_positions = 0
        self._last_reset_date: Optional[datetime] = None
        
        # Metrics
        self.metrics = {
            "total_checked": 0,
            "approved": 0,
            "rejected": 0,
            "rejection_reasons": {},
            "gate_failure_count": {},
        }
    
    def check_candidate(
        self,
        candidate: Candidate,
        current_positions: int = 0,
    ) -> GatingResult:
        """Check if a candidate passes gating rules.
        
        Args:
            candidate: The candidate to check
            current_positions: Current number of open positions
            
        Returns:
            GatingResult with approval status and reasons.
        """
        self._check_daily_reset()
        self.metrics["total_checked"] += 1
        
        reasons = []
        gate_scores = {}
        
        # Gate 1: Minimum score
        gate_scores["score"] = candidate.score
        if candidate.score < self.settings["min_score"]:
            reasons.append(f"score_below_minimum_{candidate.score:.2f}")
            self._record_gate_failure("score")
        
        # Gate 2: Minimum confidence
        gate_scores["confidence"] = candidate.confidence
        if candidate.confidence < self.settings["min_confidence"]:
            reasons.append(f"confidence_below_minimum_{candidate.confidence:.2f}")
            self._record_gate_failure("confidence")
        
        # Gate 3: Daily trade limit
        gate_scores["daily_trades"] = self._daily_trade_count
        if self._daily_trade_count >= self.settings["max_daily_trades"]:
            reasons.append("daily_trade_limit_reached")
            self._record_gate_failure("daily_trades")
        
        # Gate 4: Concurrent position limit
        gate_scores["concurrent_positions"] = current_positions
        if current_positions >= self.settings["max_concurrent_positions"]:
            reasons.append("concurrent_position_limit_reached")
            self._record_gate_failure("concurrent_positions")
        
        # Gate 5: Unknown regime check
        if candidate.signals and candidate.signals.regime == Regime.UNKNOWN:
            gate_scores["regime_known"] = 0.0
            if not self.settings["allow_unknown_regime"]:
                reasons.append("regime_unknown")
                self._record_gate_failure("regime_unknown")
        else:
            gate_scores["regime_known"] = 1.0
        
        # Gate 6: Direction mismatch check
        if candidate.signals and "direction_mismatch" in candidate.rejection_reasons:
            gate_scores["direction_aligned"] = 0.0
            if not self.settings["allow_direction_mismatch"]:
                reasons.append("direction_mismatch")
                self._record_gate_failure("direction_mismatch")
        else:
            gate_scores["direction_aligned"] = 1.0
        
        # Gate 7: Time-based restrictions
        time_gate = self._check_time_gate()
        gate_scores["time_gate"] = 1.0 if time_gate else 0.0
        if not time_gate:
            reasons.append("outside_trading_hours")
            self._record_gate_failure("time_gate")
        
        # Gate 8: Pre-existing rejection reasons
        for reason in candidate.rejection_reasons:
            if reason not in [r.split("_")[0] for r in reasons]:
                reasons.append(f"scoring_rejected_{reason}")
        
        # Determine approval
        approved = len(reasons) == 0
        
        # Update metrics
        if approved:
            self.metrics["approved"] += 1
            self._daily_trade_count += 1
        else:
            self.metrics["rejected"] += 1
            for reason in reasons:
                self.metrics["rejection_reasons"][reason] = (
                    self.metrics["rejection_reasons"].get(reason, 0) + 1
                )
        
        return GatingResult(
            approved=approved,
            candidate=candidate,
            reasons=reasons,
            gate_scores=gate_scores,
        )
    
    def _check_time_gate(self) -> bool:
        """Check if current time is within trading hours.
        
        Returns:
            True if within trading hours.
        """
        now = datetime.now().time()
        
        # Market hours: 9:30 AM - 4:00 PM ET
        market_open = time(9, 30)
        market_close = time(16, 0)
        
        # Add buffer for 0DTE (stop trading 30 min before close)
        cutoff = time(15, 30)
        
        return market_open <= now <= cutoff
    
    def _check_daily_reset(self) -> None:
        """Reset daily counters if it's a new day."""
        today = datetime.now().date()
        
        if self._last_reset_date != today:
            self._daily_trade_count = 0
            self._last_reset_date = today
    
    def _record_gate_failure(self, gate_name: str) -> None:
        """Record a gate failure in metrics."""
        self.metrics["gate_failure_count"][gate_name] = (
            self.metrics["gate_failure_count"].get(gate_name, 0) + 1
        )
    
    def filter_candidates(
        self,
        candidates: List[Candidate],
        current_positions: int = 0,
        max_approved: Optional[int] = None,
    ) -> List[GatingResult]:
        """Filter a list of candidates through gating.
        
        Args:
            candidates: List of candidates to filter
            current_positions: Current open positions
            max_approved: Maximum number to approve (None for unlimited)
            
        Returns:
            List of GatingResults for approved candidates.
        """
        approved = []
        
        for candidate in candidates:
            result = self.check_candidate(candidate, current_positions + len(approved))
            
            if result.approved:
                approved.append(result)
                
                if max_approved and len(approved) >= max_approved:
                    break
        
        logger.info(
            f"Gating: {len(approved)}/{len(candidates)} candidates approved"
        )
        
        return approved
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get gating metrics."""
        metrics = self.metrics.copy()
        metrics["settings"] = self.settings.copy()
        metrics["daily_trade_count"] = self._daily_trade_count
        metrics["approval_rate"] = (
            self.metrics["approved"] / self.metrics["total_checked"]
            if self.metrics["total_checked"] > 0 else 0
        )
        return metrics
    
    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        self.metrics = {
            "total_checked": 0,
            "approved": 0,
            "rejected": 0,
            "rejection_reasons": {},
            "gate_failure_count": {},
        }
    
    def update_settings(self, **kwargs) -> None:
        """Update gating settings.
        
        Args:
            **kwargs: Settings to update
        """
        for key, value in kwargs.items():
            if key in self.settings:
                self.settings[key] = value
                logger.info(f"Updated gating setting: {key} = {value}")
