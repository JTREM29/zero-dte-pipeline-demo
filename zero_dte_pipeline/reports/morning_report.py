"""Morning report generation.

Generates pre-market analysis reports with timeout protection
and safe fallback behavior.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from zero_dte_pipeline.candidates.generator import CandidateGenerator
from zero_dte_pipeline.candidates.gating import GatingManager
from zero_dte_pipeline.candidates.scoring import (
    Candidate,
    CandidateScorer,
    Direction,
    Regime,
    SignalAlignment,
)
from zero_dte_pipeline.config import config
from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
from zero_dte_pipeline.profiler.market_profiler import MarketProfiler, MarketProfile
from zero_dte_pipeline.utils.logging import get_logger
from zero_dte_pipeline.utils.timeout import safe_gather, with_timeout

logger = get_logger(__name__)


@dataclass
class ReportSection:
    """A section of the morning report."""
    title: str
    status: str  # 'success', 'partial', 'error', 'timeout'
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    duration_ms: float = 0


@dataclass
class MorningReportResult:
    """Complete morning report."""
    timestamp: datetime
    status: str  # 'success', 'partial', 'error'
    sections: List[ReportSection]
    market_profile: Optional[MarketProfile]
    candidates: List[Candidate]
    approved_candidates: List[Candidate]
    summary: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "status": self.status,
            "sections": [
                {
                    "title": s.title,
                    "status": s.status,
                    "data": s.data,
                    "error": s.error,
                    "duration_ms": s.duration_ms,
                }
                for s in self.sections
            ],
            "market_profile": self.market_profile.to_dict() if self.market_profile else None,
            "candidates_count": len(self.candidates),
            "approved_count": len(self.approved_candidates),
            "approved_candidates": [c.to_dict() for c in self.approved_candidates],
            "summary": self.summary,
        }


class MorningReport:
    """Generates morning analysis reports.
    
    Features:
    - Timeout protection on all external calls
    - Safe fallback when data is missing
    - Parallel data fetching where possible
    """
    
    DEFAULT_TIMEOUT = config.default_timeout  # seconds per operation
    TOTAL_TIMEOUT = config.get_int("MORNING_REPORT_TOTAL_TIMEOUT_SECONDS", 180)
    
    def __init__(
        self,
        data_connector: UnifiedDataConnector,
        timeout: int = DEFAULT_TIMEOUT,
        total_timeout: int = TOTAL_TIMEOUT,
        underlyings: Optional[List[str]] = None,
        primary_expiration: Optional[datetime] = None,
    ):
        self.data_connector = data_connector
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.underlyings = underlyings
        self.primary_expiration = primary_expiration
        
        # Initialize components
        self.profiler = MarketProfiler(data_connector)
        self.generator = CandidateGenerator(data_connector)
        self.scorer = CandidateScorer()
        gating_overrides = config.gating_overrides()
        self.gating = GatingManager(
            custom_settings=gating_overrides if gating_overrides else None
        )
    
    async def generate(self) -> MorningReportResult:
        """Generate the morning report.
        
        Uses timeout protection and fallback behavior to prevent stalling.
        
        Returns:
            MorningReportResult with analysis data.
        """
        start_time = datetime.now()
        sections: List[ReportSection] = []
        
        try:
            # Wrap entire generation in timeout
            result = await asyncio.wait_for(
                self._generate_internal(sections),
                timeout=self.total_timeout
            )
            return result
            
        except asyncio.TimeoutError:
            logger.error(f"Morning report generation timed out after {self.total_timeout}s")
            
            # Return partial result
            return MorningReportResult(
                timestamp=start_time,
                status="timeout",
                sections=sections,
                market_profile=self.profiler.last_profile,
                candidates=[],
                approved_candidates=[],
                summary={
                    "error": f"Report generation timed out after {self.total_timeout}s",
                    "duration_ms": (datetime.now() - start_time).total_seconds() * 1000,
                },
            )
        except Exception as e:
            logger.error(f"Morning report generation failed: {e}")
            
            return MorningReportResult(
                timestamp=start_time,
                status="error",
                sections=sections,
                market_profile=None,
                candidates=[],
                approved_candidates=[],
                summary={
                    "error": str(e),
                    "duration_ms": (datetime.now() - start_time).total_seconds() * 1000,
                },
            )
    
    async def _generate_internal(
        self,
        sections: List[ReportSection],
    ) -> MorningReportResult:
        """Internal report generation with section tracking.
        
        Args:
            sections: List to append sections to (for partial results)
            
        Returns:
            Complete MorningReportResult.
        """
        start_time = datetime.now()
        
        # Section 1: Data connectivity check
        connectivity_section = await self._check_connectivity()
        sections.append(connectivity_section)
        
        if connectivity_section.status == "error":
            return MorningReportResult(
                timestamp=start_time,
                status="error",
                sections=sections,
                market_profile=None,
                candidates=[],
                approved_candidates=[],
                summary={"error": "Data connectivity failed"},
            )
        
        # Section 2: Market profile analysis
        profile_section = await self._analyze_market()
        sections.append(profile_section)
        
        market_profile = profile_section.data.get("profile")
        
        # Section 3: Generate candidates
        candidates_section = await self._generate_candidates(market_profile)
        sections.append(candidates_section)
        
        candidates = candidates_section.data.get("candidates", [])
        
        # Section 4: Score and gate candidates
        approved_section = await self._score_and_gate(candidates, market_profile)
        sections.append(approved_section)
        
        approved = approved_section.data.get("approved", [])
        
        # Calculate overall status
        statuses = [s.status for s in sections]
        if all(s == "success" for s in statuses):
            overall_status = "success"
        elif "error" in statuses:
            overall_status = "partial"
        else:
            overall_status = "success"
        
        # Build summary
        summary = {
            "duration_ms": (datetime.now() - start_time).total_seconds() * 1000,
            "data_sources": connectivity_section.data.get("connected_providers", []),
            "market_condition": market_profile.condition.value if market_profile else "unknown",
            "candidates_generated": len(candidates),
            "candidates_approved": len(approved),
            "recommended_strategies": (
                [s.value for s in market_profile.recommended_strategies]
                if market_profile else []
            ),
        }
        
        return MorningReportResult(
            timestamp=start_time,
            status=overall_status,
            sections=sections,
            market_profile=market_profile,
            candidates=candidates,
            approved_candidates=approved,
            summary=summary,
        )
    
    async def _check_connectivity(self) -> ReportSection:
        """Check data connectivity."""
        start = datetime.now()
        
        try:
            connected = await with_timeout(
                self.data_connector.connect(),
                timeout=self.timeout,
                default=False,
                operation_name="connectivity_check",
            )
            
            if connected:
                return ReportSection(
                    title="Data Connectivity",
                    status="success",
                    data={
                        "connected": True,
                        "connected_providers": self.data_connector.connected_providers,
                        "primary_provider": self.data_connector.primary_provider,
                    },
                    duration_ms=(datetime.now() - start).total_seconds() * 1000,
                )
            else:
                return ReportSection(
                    title="Data Connectivity",
                    status="error",
                    data={"connected": False},
                    error="Failed to connect to any data provider",
                    duration_ms=(datetime.now() - start).total_seconds() * 1000,
                )
                
        except Exception as e:
            return ReportSection(
                title="Data Connectivity",
                status="error",
                error=str(e),
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
    
    async def _analyze_market(self) -> ReportSection:
        """Analyze market conditions."""
        start = datetime.now()
        
        try:
            profile = await with_timeout(
                self.profiler.analyze(),
                timeout=self.timeout,
                default=None,
                operation_name="market_analysis",
            )
            
            if profile:
                return ReportSection(
                    title="Market Analysis",
                    status="success",
                    data={
                        "profile": profile,
                        "condition": profile.condition.value,
                        "regime": profile.regime.value,
                        "direction": profile.direction_bias.value,
                        "volatility_percentile": profile.volatility_percentile,
                    },
                    duration_ms=(datetime.now() - start).total_seconds() * 1000,
                )
            else:
                # Provide fallback profile
                fallback = self._create_fallback_profile()
                return ReportSection(
                    title="Market Analysis",
                    status="partial",
                    data={
                        "profile": fallback,
                        "condition": fallback.condition.value,
                        "note": "Using fallback profile due to data issues",
                    },
                    duration_ms=(datetime.now() - start).total_seconds() * 1000,
                )
                
        except Exception as e:
            fallback = self._create_fallback_profile()
            return ReportSection(
                title="Market Analysis",
                status="partial",
                data={"profile": fallback},
                error=str(e),
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
    
    async def _generate_candidates(
        self,
        profile: Optional[MarketProfile],
    ) -> ReportSection:
        """Generate trading candidates."""
        start = datetime.now()
        
        # Create signal alignment from profile
        if profile:
            signals = SignalAlignment(
                direction=profile.direction_bias,
                direction_confidence=profile.confidence,
                regime=profile.regime,
                regime_confidence=profile.confidence,
                iv_signal=(profile.volatility_percentile - 50) / 50,  # Normalize to -1 to 1
                order_flow_signal=profile.momentum_score,
            )
        else:
            signals = self._create_fallback_signals()
        
        try:
            candidates = await with_timeout(
                self.generator.generate_all_candidates(
                    signals,
                    underlyings=self.underlyings,
                    primary_expiration=self.primary_expiration,
                ),
                timeout=self.timeout * 2,  # Allow more time for candidate generation
                default=[],
                operation_name="candidate_generation",
            )
            
            return ReportSection(
                title="Candidate Generation",
                status="success" if candidates else "partial",
                data={
                    "candidates": candidates,
                    "count": len(candidates),
                    "by_underlying": self._count_by_underlying(candidates),
                    "by_strategy": self._count_by_strategy(candidates),
                },
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
            
        except Exception as e:
            return ReportSection(
                title="Candidate Generation",
                status="error",
                data={"candidates": []},
                error=str(e),
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
    
    async def _score_and_gate(
        self,
        candidates: List[Candidate],
        profile: Optional[MarketProfile],
    ) -> ReportSection:
        """Score and gate candidates."""
        start = datetime.now()
        
        if not candidates:
            return ReportSection(
                title="Candidate Approval",
                status="success",
                data={"approved": [], "count": 0},
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
        
        # Create signals from profile
        if profile:
            signals = SignalAlignment(
                direction=profile.direction_bias,
                direction_confidence=profile.confidence,
                regime=profile.regime,
                regime_confidence=profile.confidence,
                iv_signal=(profile.volatility_percentile - 50) / 50,
                order_flow_signal=profile.momentum_score,
            )
        else:
            signals = self._create_fallback_signals()
        
        try:
            # Score candidates
            scored = self.scorer.score_candidates(candidates, signals)
            
            # Filter through gating
            gating_results = self.gating.filter_candidates(scored, max_approved=10)
            
            approved = [r.candidate for r in gating_results]
            
            return ReportSection(
                title="Candidate Approval",
                status="success",
                data={
                    "approved": approved,
                    "count": len(approved),
                    "total_scored": len(scored),
                    "scoring_metrics": self.scorer.get_metrics(),
                    "gating_metrics": self.gating.get_metrics(),
                },
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
            
        except Exception as e:
            return ReportSection(
                title="Candidate Approval",
                status="error",
                data={"approved": []},
                error=str(e),
                duration_ms=(datetime.now() - start).total_seconds() * 1000,
            )
    
    def _create_fallback_profile(self) -> MarketProfile:
        """Create a fallback market profile when analysis fails."""
        from zero_dte_pipeline.profiler.market_profiler import MarketCondition
        from zero_dte_pipeline.candidates.scoring import Strategy
        
        return MarketProfile(
            condition=MarketCondition.UNCERTAIN,
            volatility_percentile=50,
            momentum_score=0,
            breadth_score=0.5,
            recommended_strategies=[Strategy.VERTICAL, Strategy.SHORT_PREMIUM],
            direction_bias=Direction.NEUTRAL,
            regime=Regime.UNKNOWN,
            confidence=0.3,
            details={"fallback": True},
        )
    
    def _create_fallback_signals(self) -> SignalAlignment:
        """Create fallback signal alignment."""
        return SignalAlignment(
            direction=Direction.NEUTRAL,
            direction_confidence=0.3,
            regime=Regime.UNKNOWN,
            regime_confidence=0.3,
            iv_signal=0,
            order_flow_signal=0,
        )
    
    def _count_by_underlying(self, candidates: List[Candidate]) -> Dict[str, int]:
        """Count candidates by underlying."""
        counts: Dict[str, int] = {}
        for c in candidates:
            counts[c.underlying] = counts.get(c.underlying, 0) + 1
        return counts
    
    def _count_by_strategy(self, candidates: List[Candidate]) -> Dict[str, int]:
        """Count candidates by strategy."""
        counts: Dict[str, int] = {}
        for c in candidates:
            key = c.strategy.value if c.strategy else "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts
