"""Simple 0DTE placeholder strategy producing directional signal from price change.

Registered name: odte_direction

Emits a naive signal:
  - 'init' for the first price
  - 'up' if price increased vs previous
  - 'down' if price decreased vs previous
  - 'flat' if unchanged

Provides a meta dict with diff and pct_change for downstream summarization.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any
from .registry import register_strategy


@dataclass
class PriceDirectionResult:
    signal: str
    meta: Dict[str, Any]


@register_strategy("odte_direction")
class ODTeDirectionStrategy:
    def __init__(self):  # no fast/slow args so we exercise dynamic instantiation filtering
        self._last_price: Optional[float] = None

    def on_price(self, price: float) -> PriceDirectionResult:
        if self._last_price is None:
            self._last_price = price
            return PriceDirectionResult("init", {"price": price, "diff": 0.0, "pct_change": 0.0})
        diff = price - self._last_price
        pct = (diff / self._last_price) if self._last_price else 0.0
        if diff > 0:
            sig = "up"
        elif diff < 0:
            sig = "down"
        else:
            sig = "flat"
        self._last_price = price
        return PriceDirectionResult(sig, {"price": price, "diff": diff, "pct_change": pct})

    # Provide evaluate adapter to fit BacktestEngine Strategy protocol if needed
    def evaluate(self, market_ctx: dict[str, Any]):  # pragma: no cover - optional integration path
        price = float(market_ctx.get("lastPrice", 0.0))
        res = self.on_price(price)
        # Re-use StrategySignal shape from simple strategy only for compatibility if imported.
        try:
            from .simple_intraday_spx import StrategySignal  # local import
            yield StrategySignal(name=f"direction_{res.signal}", value=price, metadata=res.meta)
        except Exception:
            return

__all__ = ["ODTeDirectionStrategy", "PriceDirectionResult"]