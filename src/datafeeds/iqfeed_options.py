from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Dict
import math
import random
from ..utils.logging import setup_logger


@dataclass
class OptionQuote:
    symbol: str
    right: str   # 'C' or 'P'
    strike: float
    delta: float
    iv: float


class IQFeedOptionsGreeks:
    """
    Placeholder wiring for IQFeed options greeks.
    Implement actual IQFeed derivatives protocol calls here (chain, greeks stream).
    For now, we simulate a small chain so downstream logic works.
    """

    def __init__(self):
        self.log = setup_logger("iqfeed_opt")

    def fetch_chain_greeks(self, underlying: str) -> Optional[List[OptionQuote]]:  # noqa: D401
        try:
            # TODO: replace with real IQFeed calls and parsing
            # Simulate a skew where puts are richer (bearish skew)
            strikes = [4500, 4750, 5000, 5250, 5500]
            quotes: List[OptionQuote] = []
            for k in strikes:
                # calls: lower IV on OTM, puts: higher IV on OTM
                iv_call = 0.12 + 0.00001 * max(0, 5500 - k) + random.uniform(-0.003, 0.003)
                iv_put = 0.14 + 0.00001 * max(0, k - 4500) + random.uniform(-0.003, 0.003)
                # rough synthetic deltas
                call_delta = min(0.95, max(0.05, 0.5 + (5250 - k) / 1000))
                put_delta = -min(0.95, max(0.05, 0.5 + (k - 5250) / 1000))
                quotes.append(OptionQuote(f"SPX{k}C", "C", k, call_delta, iv_call))
                quotes.append(OptionQuote(f"SPX{k}P", "P", k, put_delta, iv_put))
            return quotes
        except Exception as e:  # noqa: BLE001
            self.log.exception("fetch_chain_greeks failed: %s", e)
            return None


def skew_signal_from_chain(chain: List[OptionQuote]) -> Dict[str, float]:
    """
    Estimate skew bias:
      - Compute IV at ~±25Δ, ~50Δ buckets for calls/puts and compare.
      - Positive score -> bullish (calls richer), negative -> bearish (puts richer).
    """
    if not chain:
        return {"skew_score": 0.0, "put_call_iv_spread": 0.0}

    # bucket by |delta| near 0.25 and 0.5
    def bucket_iv(side: str, target_abs_delta: float, tol: float = 0.05):
        cands = [q.iv for q in chain if q.right == side and abs(abs(q.delta) - target_abs_delta) <= tol]
        return sum(cands) / len(cands) if cands else float("nan")

    call_25 = bucket_iv("C", 0.25)
    put_25 = bucket_iv("P", 0.25)
    call_50 = bucket_iv("C", 0.50)
    put_50 = bucket_iv("P", 0.50)

    spreads = []
    for c, p in [(call_25, put_25), (call_50, put_50)]:
        if not (math.isnan(c) or math.isnan(p)):
            spreads.append(c - p)
    avg_spread = sum(spreads) / len(spreads) if spreads else 0.0  # >0 bullish, <0 bearish

    # map spread to [-1,1]
    scale = 0.20  # 20 vol points would be extreme
    skew_score = max(-1.0, min(1.0, avg_spread / scale))
    return {"skew_score": float(skew_score), "put_call_iv_spread": float(avg_spread)}


__all__ = [
    "OptionQuote",
    "IQFeedOptionsGreeks",
    "skew_signal_from_chain",
]