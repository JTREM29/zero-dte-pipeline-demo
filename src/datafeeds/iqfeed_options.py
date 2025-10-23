from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
import math, random, re, time
from ..utils.logging import setup_logger
from ..utils.bs_greeks import delta as bs_delta
from .iqfeed_chains import request_equity_index_chain
from .iqfeed_l1_parser import L1Watcher


@dataclass
class OptionQuote:
    symbol: str
    right: str   # 'C' or 'P'
    strike: float
    delta: Optional[float]
    iv: Optional[float]


_SPX_OPT_ROOT = "SPX.XO"
_sym_re = re.compile(r"([A-Z@.]+)(\d{6})([CP])(\d+)", re.I)


def parse_symbol(sym: str) -> Tuple[str, str, float]:
    m = _sym_re.match(sym)
    if not m:
        right = "C" if sym[-1].upper() == "C" else "P"
        nums = re.findall(r"\d+", sym)
        strike = float(nums[-1]) if nums else 0.0
        return sym, right, strike
    root, yymmdd, right, k = m.groups()
    return root, right.upper(), float(k)


class IQFeedOptionsGreeks:
    """Hybrid implementation: real-time (best-effort) + simulation fallback.

    fetch_chain_greeks(arg):
      - If arg is str -> legacy simulation (for tests/offline).
      - If arg is numeric -> treat as underlying price and attempt live chain + greeks via Level1.
    """

    def __init__(self):
        self.log = setup_logger("iqfeed_opt")
        self.last_source: Optional[str] = None  # 'live' or 'sim'

    # --- Simulation (legacy) -------------------------------------------------
    def _simulate_chain(self) -> List[OptionQuote]:
        strikes = [4500, 4750, 5000, 5250, 5500]
        quotes: List[OptionQuote] = []
        for k in strikes:
            iv_call = 0.12 + 0.00001 * max(0, 5500 - k) + random.uniform(-0.003, 0.003)
            iv_put = 0.14 + 0.00001 * max(0, k - 4500) + random.uniform(-0.003, 0.003)
            call_delta = min(0.95, max(0.05, 0.5 + (5250 - k) / 1000))
            put_delta = -min(0.95, max(0.05, 0.5 + (k - 5250) / 1000))
            quotes.append(OptionQuote(f"SPX{k}C", "C", k, call_delta, iv_call))
            quotes.append(OptionQuote(f"SPX{k}P", "P", k, put_delta, iv_put))
        return quotes

    # --- Live attempt -------------------------------------------------------
    def _chain_symbols_live(self, near_months: int = 1) -> List[str]:
        try:
            calls, puts = request_equity_index_chain(_SPX_OPT_ROOT, call_put="pc", months="", strikes_filter="", near=near_months, include_weeklies=True)
            return (calls or []) + (puts or [])
        except Exception as exc:  # noqa: BLE001
            self.log.debug("chain_symbols_live failed: %s", exc)
            return []

    def _fetch_chain_greeks_live(self, underlying_price: float, risk_free: float = 0.05) -> Optional[List[OptionQuote]]:
        syms = self._chain_symbols_live(near_months=1)
        if not syms:
            return None

        def near_money(sym: str, pct=0.03) -> bool:
            _, _, k = parse_symbol(sym)
            return abs(k - underlying_price) / max(1.0, underlying_price) <= pct

        watch_list = [s for s in syms if near_money(s)][:80] or syms[:40]
        if not watch_list:
            return None
        l1 = L1Watcher()
        l1.start()
        try:
            for s in watch_list:
                l1.watch(s)
            time.sleep(0.6)
            greeks: Dict[str, Dict[str, float]] = {}
            for _ in range(10):
                for rec in l1.drain(max_items=500):
                    if rec["type"] == "F":
                        payload = rec["raw"]
                        g = l1.extract_greeks(payload)
                        if g:
                            sym = payload[0]
                            greeks[sym] = g
                time.sleep(0.1)
            out: List[OptionQuote] = []
            for s in watch_list:
                _, right, k = parse_symbol(s)
                g = greeks.get(s, {})
                iv = g.get("iv")
                dlt = g.get("delta")
                if dlt is None and iv is not None and iv > 0:
                    T = 1 / 252  # ~1 trading day
                    right_lit = right.upper()
                    dlt = bs_delta(underlying_price, k, risk_free, iv, T, right_lit)  # type: ignore[arg-type]
                out.append(OptionQuote(symbol=s, right=right, strike=k, delta=dlt, iv=iv))
            return out
        finally:
            l1.stop()

    # Public unified method
    def fetch_chain_greeks(self, underlying_or_price, risk_free: float = 0.05) -> Optional[List[OptionQuote]]:  # type: ignore[no-untyped-def]
        try:
            if isinstance(underlying_or_price, (int, float)):
                live = self._fetch_chain_greeks_live(float(underlying_or_price), risk_free=risk_free)
                if live:
                    self.last_source = "live"
                    return live
                self.log.debug("Live chain fallback to simulation (no data)")
                self.last_source = "sim"
                return self._simulate_chain()
            # String path (legacy simulation)
            self.last_source = "sim"
            return self._simulate_chain()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("fetch_chain_greeks error: %s", exc)
            return None


def skew_signal_from_chain(
    chain: List[OptionQuote],
    buckets: Optional[List[float]] = None,
    tol: float = 0.05,
    scale: float = 0.20,
) -> Dict[str, float]:
    if not chain:
        return {"skew_score": 0.0, "put_call_iv_spread": 0.0}

    if not buckets:
        buckets = [0.25, 0.50]

    def bucket_iv(side: str, target_abs_delta: float):
        cands = [q.iv for q in chain if q.right == side and q.iv is not None and q.delta is not None and abs(abs(q.delta) - target_abs_delta) <= tol]
        return sum(cands) / len(cands) if cands else float("nan")

    bucket_spreads: Dict[str, float] = {}
    spreads = []
    for b in buckets:
        c = bucket_iv("C", b)
        p = bucket_iv("P", b)
        if not (math.isnan(c) or math.isnan(p)):
            sp = c - p
            spreads.append(sp)
            bucket_spreads[f"spread_{int(b*100)}d"] = float(sp)
    avg_spread = sum(spreads) / len(spreads) if spreads else 0.0
    skew_score = max(-1.0, min(1.0, avg_spread / scale))
    return {"skew_score": float(skew_score), "put_call_iv_spread": float(avg_spread), **bucket_spreads}


__all__ = ["OptionQuote", "IQFeedOptionsGreeks", "skew_signal_from_chain", "parse_symbol"]