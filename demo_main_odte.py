"""Demonstration entrypoint combining price snapshot, simulated options skew,
and the new odte_blended strategy.

This adapts the user-provided snippet to the current codebase:
 - Uses IQFeedClient.lookup_last (stub) then falls back to PolygonClient
 - Builds a synthetic close history for indicator warm-up
 - Fetches a simulated options chain + skew via IQFeedOptionsGreeks
 - Passes skew into strategy via market_ctx['skew_norm']
 - Emits decisions gated by a confidence threshold

Notes:
 - ODTeStrategy referenced in the snippet does not exist; replaced by
   registered 'odte_blended' strategy.
 - Logging uses existing logging_setup utilities.
 - OpenAI summarization replaced by OpenAIWrapper if API key present, else
   prints a simple summary dictionary.
"""
from __future__ import annotations
from datetime import datetime
import numpy as np

from src.utils.logging_setup import get_logger
from src.config import Settings
from src.datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig
from src.datafeeds.iqfeed_options import IQFeedOptionsGreeks, skew_signal_from_chain
from src.datafeeds.polygon_client import PolygonClient, PolygonConfig
from src.openai_client import OpenAIWrapper
from src.strategies.registry import get_strategy

CONFIDENCE_THRESHOLD = 0.06
log = get_logger("demo.odte")


def _init_strategy():
    Strat = get_strategy("odte_blended")
    # Provide slightly lower thresholds for demo to show signals
    return Strat(entry_threshold=0.5, exit_threshold=0.2)


def main() -> int:
    settings = Settings()
    cfg = IQFeedConfig.from_env()
    iq = IQFeedClient(cfg)
    iq_opt = IQFeedOptionsGreeks()
    poly = PolygonClient(PolygonConfig.from_env())
    strat = _init_strategy()

    spx_symbol = "@SPX.X"
    snap = iq.lookup_last(spx_symbol)
    if snap and isinstance(snap, dict) and snap.get("last_price") is not None:
        price = float(snap.get("last_price") or 0.0)
        high = price * 1.002
        low = price * 0.998
    else:
        log.warning("IQFeed snapshot unavailable; falling back to Polygon last trade")
        prev = poly.last_trade_spx()
        if not prev or "results" not in prev or not prev["results"]:
            log.error("No price source available")
            return 1
        price = float(prev["results"][0].get("c", 0.0))
        high = price * 1.002
        low = price * 0.998

    # Synthetic history for indicator warm-up
    closes = np.array([price * (1 + 0.0004 * i * np.sin(i / 7.0)) for i in range(80)], dtype=float)
    highs = closes * 1.0015
    lows = closes * 0.9985
    for h, l, c in zip(highs, lows, closes):
        # Provide minimal context for evaluate; we don't have separate bar API on blended
        strat.evaluate({"lastPrice": c, "lastSize": 10})

    # Options skew (simulated)
    chain = iq_opt.fetch_chain_greeks("SPX") or []
    skew_stats = skew_signal_from_chain(chain)
    skew_score = skew_stats["skew_score"]
    log.info("Skew stats: %s", skew_stats)

    # Final tick evaluation with contextual fields
    month = datetime.utcnow().month
    market_ctx = {
        "lastPrice": price,
        "lastSize": 1000,
        "skew_norm": skew_score,
        # Placeholder regime/seasonality heuristics
        "regime": "trending" if abs(closes[-1] - closes[-20]) > 0.002 * price else "choppy",
        "seasonality_bias": 0.05 if month in (4, 11) else 0.0,
    }
    emitted = list(strat.evaluate(market_ctx))
    score_sig = next((s for s in emitted if s.name == "odte_score"), None)
    if not score_sig:
        log.error("No odte_score emitted")
        return 1
    score = score_sig.metadata.get("raw_score", 0.0)
    composite_strength = score_sig.strength or 0.0

    decision = "NO-TRADE"
    if composite_strength >= CONFIDENCE_THRESHOLD:
        if strat._state.position == 1:  # type: ignore[attr-defined]
            decision = "CONSIDER LONG_CALLS"
        elif strat._state.position == -1:  # type: ignore[attr-defined]
            decision = "CONSIDER LONG_PUTS"

    features = {
        "price": price,
        "score_strength": round(composite_strength, 4),
        "decision": decision,
        "skew_score": round(skew_score, 4),
        "regime": market_ctx["regime"],
        "seasonality_bias": market_ctx["seasonality_bias"],
    }

    if settings.has_openai:
        wrapper = OpenAIWrapper(settings.openai_api_key)
        summary = wrapper.summarize_signals([{
            "name": "odte_score",
            "value": price,
            "metadata": features,
            "strength": composite_strength,
        }])
    else:
        summary = f"Features: {features}"

    print("\n=== ODTE BLENDED DEMO ===")
    print("Price:", price)
    print("Skew Score:", round(skew_score, 4))
    print("Score Strength:", round(composite_strength, 4), "| Decision gate(|score|>=0.06):", decision)
    print("Regime:", market_ctx["regime"], "| Season Bias:", market_ctx["seasonality_bias"]) 
    print("\nSummary:\n", summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
