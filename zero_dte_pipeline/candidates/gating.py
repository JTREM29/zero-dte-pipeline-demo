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
            "min_score": 0.15,
            "quality_threshold": None,
            "min_confidence": 0.2,
            "max_daily_trades": 20,
            "max_concurrent_positions": 10,
            "allow_unknown_regime": True,
            "allow_direction_mismatch": True,
            "max_total": None,
            "max_per_side": None,
            "max_per_underlying": None,
            "credit_min": None,
            "credit_max": None,
            "distance_from_spot_min_pct": None,
            "distance_from_spot_max_pct": None,
        },
        "moderate": {
            "min_score": 0.28,
            "quality_threshold": None,
            "min_confidence": 0.28,
            "max_daily_trades": 12,
            "max_concurrent_positions": 6,
            "allow_unknown_regime": True,
            "allow_direction_mismatch": False,
            "max_total": None,
            "max_per_side": None,
            "max_per_underlying": None,
            "credit_min": None,
            "credit_max": None,
            "distance_from_spot_min_pct": None,
            "distance_from_spot_max_pct": None,
        },
        "strict": {
            "min_score": 0.45,
            "quality_threshold": None,
            "min_confidence": 0.4,
            "max_daily_trades": 5,
            "max_concurrent_positions": 3,
            "allow_unknown_regime": False,
            "allow_direction_mismatch": False,
            "max_total": None,
            "max_per_side": None,
            "max_per_underlying": None,
            "credit_min": None,
            "credit_max": None,
            "distance_from_spot_min_pct": None,
            "distance_from_spot_max_pct": None,
        },
    }
    
    def __init__(
        self,
        strictness: Optional[str] = None,
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
            self._apply_custom_settings(custom_settings)
        quality_override = self.settings.get("quality_threshold")
        if isinstance(quality_override, (int, float)):
            self.settings["min_score"] = float(quality_override)
        
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
        
        # Gate 8: Credit band (if configured)
        credit_value = self._extract_credit(candidate)
        gate_scores["credit"] = credit_value if credit_value is not None else 0.0
        if credit_value is not None:
            min_credit = self.settings.get("credit_min")
            max_credit = self.settings.get("credit_max")
            if min_credit is not None and credit_value < min_credit:
                reasons.append(f"credit_below_min_{credit_value:.2f}")
                self._record_gate_failure("credit")
            if max_credit is not None and credit_value > max_credit:
                reasons.append(f"credit_above_max_{credit_value:.2f}")
                self._record_gate_failure("credit")
        
        # Gate 9: Distance from spot constraints
        distance_pct = self._compute_distance_pct(candidate)
        gate_scores["distance_pct"] = distance_pct if distance_pct is not None else 0.0
        if distance_pct is not None:
            min_dist = self.settings.get("distance_from_spot_min_pct")
            max_dist = self.settings.get("distance_from_spot_max_pct")
            if min_dist is not None and distance_pct < min_dist:
                reasons.append(f"distance_below_min_{distance_pct:.4f}")
                self._record_gate_failure("distance")
            if max_dist is not None and distance_pct > max_dist:
                reasons.append(f"distance_above_max_{distance_pct:.4f}")
                self._record_gate_failure("distance")
        
        # Gate 10: Pre-existing rejection reasons
        for reason in candidate.rejection_reasons:
            if (
                reason == "direction_mismatch"
                and self.settings.get("allow_direction_mismatch", False)
            ):
                continue  # honor preset override for direction
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
        approved: List[GatingResult] = []
        side_counts: Dict[str, int] = {}
        underlying_counts: Dict[str, int] = {}
        max_total = self.settings.get("max_total")
        max_per_side = self.settings.get("max_per_side")
        max_per_underlying = self.settings.get("max_per_underlying")
        
        for candidate in candidates:
            if max_total and len(approved) >= int(max_total):
                break
            result = self.check_candidate(candidate, current_positions + len(approved))
            
            if result.approved:
                side_key = self._normalize_side(candidate.option_type)
                underlying_key = candidate.underlying
                if (
                    max_per_side is not None and side_key and
                    side_counts.get(side_key, 0) >= int(max_per_side)
                ):
                    self._revoke_approval(result, "per_side_limit", gate_name="per_side_quota")
                    continue
                if (
                    max_per_underlying is not None and underlying_key and
                    underlying_counts.get(underlying_key, 0) >= int(max_per_underlying)
                ):
                    self._revoke_approval(result, "per_underlying_limit", gate_name="per_underlying_quota")
                    continue
                approved.append(result)
                if side_key:
                    side_counts[side_key] = side_counts.get(side_key, 0) + 1
                if underlying_key:
                    underlying_counts[underlying_key] = (
                        underlying_counts.get(underlying_key, 0) + 1
                    )
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
            if key == "quality_threshold" and isinstance(value, (int, float)):
                self.settings["quality_threshold"] = float(value)
                self.settings["min_score"] = float(value)
                logger.info("Updated gating setting: quality_threshold = %s", value)
                continue
            if key == "credit" and isinstance(value, dict):
                if isinstance(value.get("min"), (int, float)):
                    self.settings["credit_min"] = float(value["min"])
                if isinstance(value.get("max"), (int, float)):
                    self.settings["credit_max"] = float(value["max"])
                logger.info("Updated gating setting: credit band = %s", value)
                continue
            if key == "distance_from_spot" and isinstance(value, dict):
                if isinstance(value.get("min_pct"), (int, float)):
                    self.settings["distance_from_spot_min_pct"] = float(value["min_pct"])
                if isinstance(value.get("max_pct"), (int, float)):
                    self.settings["distance_from_spot_max_pct"] = float(value["max_pct"])
                logger.info("Updated gating setting: distance_from_spot = %s", value)
                continue
            if key in self.settings:
                self.settings[key] = value
                logger.info(f"Updated gating setting: {key} = {value}")

    def _apply_custom_settings(self, overrides: Dict[str, Any]) -> None:
        """Apply overrides including structured aliases."""
        for key, value in overrides.items():
            if key in self.settings:
                self.settings[key] = value
        if isinstance(overrides.get("quality_threshold"), (int, float)):
            self.settings["quality_threshold"] = float(overrides["quality_threshold"])
        credit_cfg = overrides.get("credit")
        if isinstance(credit_cfg, dict):
            if isinstance(credit_cfg.get("min"), (int, float)):
                self.settings["credit_min"] = float(credit_cfg["min"])
            if isinstance(credit_cfg.get("max"), (int, float)):
                self.settings["credit_max"] = float(credit_cfg["max"])
        distance_cfg = overrides.get("distance_from_spot")
        if isinstance(distance_cfg, dict):
            if isinstance(distance_cfg.get("min_pct"), (int, float)):
                self.settings["distance_from_spot_min_pct"] = float(distance_cfg["min_pct"])
            if isinstance(distance_cfg.get("max_pct"), (int, float)):
                self.settings["distance_from_spot_max_pct"] = float(distance_cfg["max_pct"])
        for alias_key in ("max_total", "max_per_side", "max_per_underlying"):
            if isinstance(overrides.get(alias_key), (int, float)):
                self.settings[alias_key] = int(overrides[alias_key])

    def _extract_credit(self, candidate: Candidate) -> Optional[float]:
        """Derive trade credit/debit for gating comparisons."""
        metadata = candidate.metadata or {}
        for key in ("credit", "net_credit", "mid_price"):
            value = metadata.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        if isinstance(candidate.entry_price, (int, float)):
            return float(candidate.entry_price)
        bid = metadata.get("bid")
        ask = metadata.get("ask")
        if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and ask:
            return float((bid + ask) / 2)
        return None

    def _compute_distance_pct(self, candidate: Candidate) -> Optional[float]:
        """Compute absolute distance between strike and spot as a percent."""
        metadata = candidate.metadata or {}
        spot = metadata.get("underlying_price") or metadata.get("spot") or metadata.get("underlying_spot")
        strike = candidate.strike
        if not isinstance(spot, (int, float)) or not isinstance(strike, (int, float)):
            return None
        if spot <= 0:
            return None
        return abs(float(strike) - float(spot)) / float(spot)

    def _normalize_side(self, option_type: Optional[str]) -> Optional[str]:
        if not option_type:
            return None
        option = option_type.lower()
        if "call" in option:
            return "call"
        if "put" in option:
            return "put"
        return option

    def _revoke_approval(self, result: GatingResult, reason: str, gate_name: Optional[str] = None) -> None:
        """Convert an approved result into a rejection due to quota limits."""
        if not result.approved:
            return
        result.approved = False
        result.reasons.append(reason)
        self.metrics["approved"] = max(0, self.metrics["approved"] - 1)
        self.metrics["rejected"] += 1
        self.metrics["rejection_reasons"][reason] = self.metrics["rejection_reasons"].get(reason, 0) + 1
        self._daily_trade_count = max(0, self._daily_trade_count - 1)
        self._record_gate_failure(gate_name or reason)
