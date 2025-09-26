"""Very naive placeholder 0DTE strategy skeleton.

The real implementation would:
- Load order book / options chain snapshot(s)
- Evaluate metrics (IV rank, delta bands, expected move, gamma levels)
- Decide on spread / iron condor / scalp structure
- Manage exits (time-based, profit target, delta shift)
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Deque, Optional
from collections import deque
from .registry import register_strategy


@dataclass
class StrategySignal:
    name: str
    value: float
    metadata: dict[str, Any]
    strength: Optional[float] = None  # Optional normalized strength / confidence metric

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop None strength for cleaner serialization
        if d.get("strength") is None:
            d.pop("strength", None)
        return d


@register_strategy("simple_intraday_spx")
class SimpleIntradaySPXStrategy:
    """Adds a trivial fast/slow moving average crossover to emit position change signals.

    Maintains in-memory windows; state resets between process runs.
    """

    def __init__(self, fast: int = 5, slow: int = 20):
        if fast <= 0 or slow <= 0 or fast >= slow:
            raise ValueError("Require 0 < fast < slow")
        self.fast = fast
        self.slow = slow
        self._fast_win: Deque[float] = deque(maxlen=fast)
        self._slow_win: Deque[float] = deque(maxlen=slow)
        self._position: int = 0  # 1 long, 0 flat

    def evaluate(self, market_ctx: dict[str, Any]) -> Iterable[StrategySignal]:
        price = float(market_ctx.get("lastPrice", 0.0))
        self._fast_win.append(price)
        self._slow_win.append(price)
        # Always echo price for reference
        yield StrategySignal(
            name="price_echo",
            value=price,
            metadata={"note": "Echoes current price as a pseudo-signal"},
        )
        if len(self._slow_win) < self.slow:
            return
        fast_ma = sum(self._fast_win) / len(self._fast_win)
        slow_ma = sum(self._slow_win) / len(self._slow_win)
        # Crossover logic
        # Simple normalized difference as a rudimentary strength metric
        # strength ~ |fast - slow| / slow (bounded >0; not capped)
        diff = abs(fast_ma - slow_ma)
        strength = diff / slow_ma if slow_ma else 0.0
        if fast_ma > slow_ma and self._position <= 0:
            self._position = 1
            yield StrategySignal(
                name="enter_long",
                value=price,
                metadata={"fast_ma": fast_ma, "slow_ma": slow_ma},
                strength=strength,
            )
        elif fast_ma < slow_ma and self._position > 0:
            self._position = 0
            yield StrategySignal(
                name="exit_long",
                value=price,
                metadata={"fast_ma": fast_ma, "slow_ma": slow_ma},
                strength=strength,
            )
