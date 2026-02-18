"""Candidate generation for 0DTE options.

Generates trading candidates based on market conditions and
options chain data.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

from zero_dte_pipeline.candidates.scoring import (
    Candidate,
    Direction,
    Regime,
    SignalAlignment,
    Strategy,
)
from zero_dte_pipeline.config import config
from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
from zero_dte_pipeline.utils.logging import get_logger
from zero_dte_pipeline.utils.timeout import with_timeout

logger = get_logger(__name__)

CHAIN_TIMEOUT_SECONDS = config.options_chain_timeout_seconds
CHAIN_FALLBACK_TIMEOUT_SECONDS = config.options_chain_fallback_timeout_seconds


class CandidateGenerator:
    """Generates 0DTE and 0DTE+1 trading candidates.
    
    Features:
    - Support for SPX, SPY, QQQ, IWM
    - Multiple strategy types
    - Signal-based filtering
    """
    
    # Supported underlyings with their characteristics
    UNDERLYINGS = {
        "SPX": {"multiplier": 100, "settlement": "AM", "style": "european"},
        "SPY": {"multiplier": 100, "settlement": "PM", "style": "american"},
        "QQQ": {"multiplier": 100, "settlement": "PM", "style": "american"},
        "IWM": {"multiplier": 100, "settlement": "PM", "style": "american"},
    }
    
    def __init__(
        self,
        data_connector: UnifiedDataConnector,
        min_open_interest: int = 50,  # Reduced for less strict gating
        min_volume: int = 10,
        max_spread_percent: float = 0.10,  # Increased from 0.05
        delta_range: tuple = (0.15, 0.45),  # Widened range
    ):
        self.data_connector = data_connector
        self.min_open_interest = min_open_interest
        self.min_volume = min_volume
        self.max_spread_percent = max_spread_percent
        self.delta_range = delta_range
        
        # Metrics
        self.metrics = {
            "generated": 0,
            "filtered_liquidity": 0,
            "filtered_delta": 0,
            "filtered_spread": 0,
        }
    
    async def generate_candidates(
        self,
        underlying: str,
        signals: SignalAlignment,
        include_0dte_plus_1: bool = True,
        primary_expiration: Optional[datetime] = None,
    ) -> List[Candidate]:
        """Generate trading candidates for an underlying.
        
        Args:
            underlying: The underlying symbol (SPX, SPY, QQQ, IWM)
            signals: Current signal alignment
            include_0dte_plus_1: Whether to include 0DTE+1 candidates
            
        Returns:
            List of generated candidates.
        """
        if underlying not in self.UNDERLYINGS:
            logger.warning(f"Unsupported underlying: {underlying}")
            return []
        
        candidates = []
        
        # Get 0DTE options
        if primary_expiration is not None:
            today = primary_expiration.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        chain_0dte = await with_timeout(
            self.data_connector.get_options_chain(underlying, today),
            timeout=CHAIN_TIMEOUT_SECONDS,
            default=None,
            operation_name=f"get_options_chain_{underlying}_0dte",
        )
        
        if chain_0dte is not None:
            candidates.extend(
                self._generate_from_chain(underlying, chain_0dte, signals, today)
            )
        else:
            logger.warning(f"No 0DTE chain available for {underlying}")
        
        # Get 0DTE+1 options
        if include_0dte_plus_1:
            tomorrow = today + timedelta(days=1)
            
            chain_1dte = await with_timeout(
                self.data_connector.get_options_chain(underlying, tomorrow),
                timeout=CHAIN_TIMEOUT_SECONDS,
                default=None,
                operation_name=f"get_options_chain_{underlying}_1dte",
            )
            
            if chain_1dte is None:
                logger.warning(
                    "Primary providers missing 1DTE chain for %s; retrying via polygon fallback",
                    underlying,
                )
                chain_1dte = await with_timeout(
                    self.data_connector.get_options_chain(
                        underlying,
                        tomorrow,
                        providers=["polygon"],
                    ),
                    timeout=CHAIN_FALLBACK_TIMEOUT_SECONDS,
                    default=None,
                    operation_name=f"get_options_chain_{underlying}_1dte_polygon",
                )
            
            if chain_1dte is not None:
                candidates.extend(
                    self._generate_from_chain(underlying, chain_1dte, signals, tomorrow)
                )
            else:
                logger.warning("No 1DTE chain available for %s via any provider", underlying)
        
        self.metrics["generated"] += len(candidates)
        
        return candidates
    
    def _generate_from_chain(
        self,
        underlying: str,
        chain: pd.DataFrame,
        signals: SignalAlignment,
        expiration: datetime,
    ) -> List[Candidate]:
        """Generate candidates from an options chain.
        
        Args:
            underlying: The underlying symbol
            chain: Options chain DataFrame
            signals: Signal alignment
            expiration: Expiration date
            
        Returns:
            List of candidates.
        """
        candidates = []
        
        # Filter by liquidity
        liquid = chain[
            (chain.get("open_interest", 0) >= self.min_open_interest) |
            (chain.get("volume", 0) >= self.min_volume)
        ].copy()
        
        filtered_count = len(chain) - len(liquid)
        self.metrics["filtered_liquidity"] += filtered_count
        
        if liquid.empty:
            return candidates
        
        # Filter by delta range (only if usable delta data is present)
        if "delta" in liquid.columns:
            numeric_delta = pd.to_numeric(liquid["delta"], errors="coerce")
            liquid = liquid.assign(delta=numeric_delta)
            if numeric_delta.notna().any():
                before_delta_filter = len(liquid)
                delta_mask = (
                    (liquid["delta"].abs() >= self.delta_range[0]) &
                    (liquid["delta"].abs() <= self.delta_range[1])
                )
                liquid = liquid[delta_mask & liquid["delta"].notna()]
                self.metrics["filtered_delta"] += before_delta_filter - len(liquid)
        
        # Filter by spread (only when both bid/ask are available)
        if "bid" in liquid.columns and "ask" in liquid.columns:
            with_quotes = (
                liquid["bid"].notna() & liquid["ask"].notna() &
                (liquid["ask"] != 0)
            )
            if with_quotes.any():
                liquid.loc[with_quotes, "spread_pct"] = (
                    (liquid.loc[with_quotes, "ask"] - liquid.loc[with_quotes, "bid"]) /
                    liquid.loc[with_quotes, "ask"]
                )
                before_spread = len(liquid)
                spread_mask = (
                    (~with_quotes) |
                    (liquid["spread_pct"] <= self.max_spread_percent)
                )
                liquid = liquid[spread_mask]
                self.metrics["filtered_spread"] += before_spread - len(liquid)
        
        # Determine direction preference
        direction_pref = signals.direction if signals.direction != Direction.UNKNOWN else Direction.NEUTRAL
        
        # Generate candidates based on direction and regime
        strategies = self._select_strategies(signals)
        
        for _, option in liquid.iterrows():
            for strategy in strategies:
                candidate = self._create_candidate(
                    underlying, option, expiration, strategy, direction_pref
                )
                if candidate:
                    candidates.append(candidate)
        
        return candidates
    
    def _select_strategies(self, signals: SignalAlignment) -> List[Strategy]:
        """Select appropriate strategies based on signals.
        
        Args:
            signals: Signal alignment
            
        Returns:
            List of strategies to consider.
        """
        strategies = []
        
        # High IV favors short premium
        if signals.iv_signal > 0.2:
            strategies.append(Strategy.SHORT_PREMIUM)
            strategies.append(Strategy.IRON_CONDOR)
        
        # Low IV favors directional/trend
        if signals.iv_signal < -0.1:
            strategies.append(Strategy.TREND_FOLLOWING)
            strategies.append(Strategy.VERTICAL)
        
        # Ranging regime favors butterflies
        if signals.regime == Regime.RANGING:
            strategies.append(Strategy.BUTTERFLY)
        
        # Trending regime favors directional
        if signals.regime == Regime.TRENDING:
            strategies.append(Strategy.TREND_FOLLOWING)
            if Strategy.VERTICAL not in strategies:
                strategies.append(Strategy.VERTICAL)
        
        # Default strategies if none selected
        if not strategies:
            strategies = [Strategy.VERTICAL, Strategy.SHORT_PREMIUM]
        
        return strategies
    
    def _create_candidate(
        self,
        underlying: str,
        option: pd.Series,
        expiration: datetime,
        strategy: Strategy,
        direction: Direction,
    ) -> Optional[Candidate]:
        """Create a candidate from option data.
        
        Args:
            underlying: The underlying symbol
            option: Option data row
            expiration: Expiration date
            strategy: Strategy type
            direction: Trade direction
            
        Returns:
            Candidate or None if invalid.
        """
        try:
            option_type = option.get("type", "call")
            
            # Determine direction from option type and strategy
            if strategy == Strategy.SHORT_PREMIUM:
                # Short premium is neutral/range-bound
                trade_direction = Direction.NEUTRAL
            elif option_type == "call":
                trade_direction = Direction.BULLISH
            else:
                trade_direction = Direction.BEARISH
            
            # Calculate entry/target/stop
            mid_price = None
            if option.get("bid") and option.get("ask"):
                mid_price = (option["bid"] + option["ask"]) / 2
            
            entry_price = mid_price or option.get("last")
            
            # Set targets based on strategy
            if entry_price and strategy in [Strategy.SHORT_PREMIUM, Strategy.IRON_CONDOR]:
                target_price = entry_price * 0.5  # 50% profit target
                stop_price = entry_price * 2.0  # 100% loss stop
            elif entry_price:
                target_price = entry_price * 2.0  # 100% profit target
                stop_price = entry_price * 0.5  # 50% loss stop
            else:
                target_price = None
                stop_price = None
            
            return Candidate(
                symbol=option.get("symbol", f"{underlying}_OPT"),
                underlying=underlying,
                strike=option.get("strike", 0),
                option_type=option_type,
                expiration=expiration,
                strategy=strategy,
                direction=trade_direction,
                entry_price=entry_price,
                target_price=target_price,
                stop_price=stop_price,
                metadata={
                    "delta": option.get("delta"),
                    "gamma": option.get("gamma"),
                    "theta": option.get("theta"),
                    "vega": option.get("vega"),
                    "iv": option.get("iv"),
                    "volume": option.get("volume"),
                    "open_interest": option.get("open_interest"),
                    "underlying_price": option.get("underlying_price"),
                    "bid": option.get("bid"),
                    "ask": option.get("ask"),
                },
            )
        except Exception as e:
            logger.warning(f"Error creating candidate: {e}")
            return None
    
    async def generate_all_candidates(
        self,
        signals: SignalAlignment,
        underlyings: Optional[List[str]] = None,
        primary_expiration: Optional[datetime] = None,
    ) -> List[Candidate]:
        """Generate candidates for all supported underlyings.
        
        Args:
            signals: Signal alignment
            underlyings: Optional list of underlyings (defaults to all supported)
            
        Returns:
            Combined list of candidates from all underlyings.
        """
        underlyings = underlyings or list(self.UNDERLYINGS.keys())
        
        all_candidates = []
        
        for underlying in underlyings:
            candidates = await self.generate_candidates(
                underlying,
                signals,
                primary_expiration=primary_expiration,
            )
            all_candidates.extend(candidates)
            logger.info(f"Generated {len(candidates)} candidates for {underlying}")
        
        return all_candidates
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get generation metrics."""
        return self.metrics.copy()
    
    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        self.metrics = {
            "generated": 0,
            "filtered_liquidity": 0,
            "filtered_delta": 0,
            "filtered_spread": 0,
        }


async def _generate_0dte_candidates_async(
    underlying: str,
    max_structures: int,
    include_0dte_plus_1: bool = True,
    primary_expiration: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Internal helper to produce basic candidate dictionaries asynchronously."""

    async with UnifiedDataConnector() as connector:
        generator = CandidateGenerator(connector)

        signals = SignalAlignment(
            direction=Direction.NEUTRAL,
            direction_confidence=0.5,
            regime=Regime.RANGING,
            regime_confidence=0.5,
            iv_signal=0.0,
            order_flow_signal=0.0,
        )

        candidates = await generator.generate_candidates(
            underlying,
            signals,
            include_0dte_plus_1=include_0dte_plus_1,
            primary_expiration=primary_expiration,
        )

    trimmed = candidates[:max_structures] if max_structures else candidates
    return [candidate.to_dict() for candidate in trimmed]


def generate_0dte_candidates(
    underlying: str,
    max_structures: int = 40,
    include_0dte_plus_1: bool = True,
    primary_expiration: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Convenience wrapper that synchronously returns 0DTE candidate dictionaries."""

    async def _run() -> List[Dict[str, Any]]:
        return await _generate_0dte_candidates_async(
            underlying=underlying,
            max_structures=max_structures,
            include_0dte_plus_1=include_0dte_plus_1,
            primary_expiration=primary_expiration,
        )

    try:
        return asyncio.run(_run())
    except RuntimeError:
        # If an event loop is already running, fall back to create a new one.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
