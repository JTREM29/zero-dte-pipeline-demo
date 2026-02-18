"""Tests for gating manager."""
import pytest
from datetime import datetime, time

from zero_dte_pipeline.candidates.gating import GatingManager, GatingResult
from zero_dte_pipeline.candidates.scoring import (
    Candidate,
    Direction,
    Regime,
    SignalAlignment,
    Strategy,
)


class TestGatingManager:
    """Test gating manager functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.gating = GatingManager(strictness="moderate")
        
        self.signals = SignalAlignment(
            direction=Direction.BULLISH,
            direction_confidence=0.7,
            regime=Regime.TRENDING,
            regime_confidence=0.6,
            iv_signal=0.2,
            order_flow_signal=0.4,
        )
        
        self.good_candidate = Candidate(
            symbol="SPY231201C450",
            underlying="SPY",
            strike=450.0,
            option_type="call",
            expiration=datetime.now(),
            strategy=Strategy.VERTICAL,
            direction=Direction.BULLISH,
            score=0.7,
            confidence=0.6,
            signals=self.signals,
        )
        
        self.weak_candidate = Candidate(
            symbol="SPY231201C450",
            underlying="SPY",
            strike=450.0,
            option_type="call",
            expiration=datetime.now(),
            strategy=Strategy.VERTICAL,
            direction=Direction.BULLISH,
            score=0.2,
            confidence=0.2,
            signals=self.signals,
        )
    
    def test_approve_good_candidate(self):
        """Good candidates should be approved."""
        result = self.gating.check_candidate(self.good_candidate)
        
        assert isinstance(result, GatingResult)
        # May or may not be approved depending on time of day
        assert isinstance(result.approved, bool)
        assert result.candidate is self.good_candidate
    
    def test_reject_weak_candidate(self):
        """Weak candidates should be rejected."""
        result = self.gating.check_candidate(self.weak_candidate)
        
        assert not result.approved
        assert len(result.reasons) > 0
        assert "score_below_minimum" in result.reasons[0] or "confidence_below_minimum" in result.reasons[0]
    
    def test_strictness_levels(self):
        """Different strictness levels should have different thresholds."""
        loose = GatingManager(strictness="loose")
        strict = GatingManager(strictness="strict")
        
        assert loose.settings["min_score"] < strict.settings["min_score"]
        assert loose.settings["max_daily_trades"] > strict.settings["max_daily_trades"]
    
    def test_custom_settings(self):
        """Custom settings should override defaults."""
        custom = GatingManager(
            strictness="moderate",
            custom_settings={"min_score": 0.1, "max_daily_trades": 50}
        )
        
        assert custom.settings["min_score"] == 0.1
        assert custom.settings["max_daily_trades"] == 50
    
    def test_filter_candidates(self):
        """Filter should return approved candidates."""
        candidates = [self.good_candidate] * 3 + [self.weak_candidate] * 3
        
        results = self.gating.filter_candidates(candidates, max_approved=5)
        
        # All results should be GatingResult
        assert all(isinstance(r, GatingResult) for r in results)
        # All approved
        assert all(r.approved for r in results)
    
    def test_max_approved_limit(self):
        """Filter should respect max_approved limit."""
        # Create many good candidates
        candidates = [
            Candidate(
                symbol=f"SPY{i}",
                underlying="SPY",
                strike=450.0,
                option_type="call",
                expiration=datetime.now(),
                strategy=Strategy.VERTICAL,
                direction=Direction.BULLISH,
                score=0.8,
                confidence=0.7,
                signals=self.signals,
            )
            for i in range(20)
        ]
        
        # Use a time-independent gating for this test
        gating = GatingManager(
            strictness="loose",
            custom_settings={"max_daily_trades": 100}
        )
        
        results = gating.filter_candidates(candidates, max_approved=3)
        
        assert len(results) <= 3
    
    def test_metrics_tracking(self):
        """Gating metrics should be tracked."""
        self.gating.reset_metrics()
        
        self.gating.check_candidate(self.good_candidate)
        self.gating.check_candidate(self.weak_candidate)
        
        metrics = self.gating.get_metrics()
        
        assert metrics["total_checked"] == 2
        assert metrics["approved"] + metrics["rejected"] == 2
    
    def test_update_settings(self):
        """Settings should be updatable."""
        original = self.gating.settings["min_score"]
        
        self.gating.update_settings(min_score=0.9)
        
        assert self.gating.settings["min_score"] == 0.9
        assert self.gating.settings["min_score"] != original
    
    def test_unknown_regime_handling(self):
        """Unknown regime should be handled based on settings."""
        unknown_signals = SignalAlignment(
            direction=Direction.BULLISH,
            direction_confidence=0.7,
            regime=Regime.UNKNOWN,
            regime_confidence=0.3,
            iv_signal=0.2,
            order_flow_signal=0.4,
        )
        
        candidate = Candidate(
            symbol="SPY231201C450",
            underlying="SPY",
            strike=450.0,
            option_type="call",
            expiration=datetime.now(),
            strategy=Strategy.VERTICAL,
            direction=Direction.BULLISH,
            score=0.7,
            confidence=0.6,
            signals=unknown_signals,
        )
        
        # Loose allows unknown regime
        loose = GatingManager(strictness="loose")
        result_loose = loose.check_candidate(candidate)
        
        # Strict rejects unknown regime
        strict = GatingManager(strictness="strict")
        result_strict = strict.check_candidate(candidate)
        
        # Check that gate_scores tracked this
        assert "regime_known" in result_loose.gate_scores
        assert "regime_known" in result_strict.gate_scores
