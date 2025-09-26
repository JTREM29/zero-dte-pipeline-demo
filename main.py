"""Entry point for the ZeroDTE pipeline.

Merged functionality:
1. Environment + settings bootstrap
2. Snapshot via IQFeed (stub) fallback to Polygon
3. Evaluate legacy moving-average strategy (simple_intraday_spx)
4. Warm and evaluate new multi-factor blended strategy (odte_blended) with:
    - Synthetic history warm-up
    - Simulated options IV skew (placeholder)
    - Regime & seasonality contextual adjustments
5. Persist all emitted signals to parquet
6. Optional OpenAI summarization over combined signals
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime
from typing import Iterable
import math
import pandas as pd
import numpy as np

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

from src.utils.logging_setup import get_logger
from src.datafeeds.polygon_client import PolygonClient, PolygonConfig
from src.strategies.simple_intraday_spx import SimpleIntradaySPXStrategy, StrategySignal
from src.strategies.registry import get_strategy
from src.datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig
from src.datafeeds.iqfeed_options import IQFeedOptionsGreeks, skew_signal_from_chain
from src.config import Settings
from src.persistence import write_parquet
from src.openai_client import OpenAIWrapper

PROJECT_ROOT = Path(__file__).parent.resolve()
LOGGER = get_logger("zero_dte.main")

CONFIDENCE_THRESHOLD = 0.06  # gating for blended composite strength


def _emit_signals(signals: Iterable[StrategySignal]) -> list[StrategySignal]:
    collected: list[StrategySignal] = []
    for sig in signals:
        LOGGER.info("Signal %s = %.4f | meta=%s", sig.name, sig.value, sig.metadata)
        collected.append(sig)
    return collected


def main() -> int:
    if load_dotenv:
        load_dotenv()
    settings = Settings()

    ts = datetime.utcnow().isoformat()
    LOGGER.info("Bootstrap at %s UTC", ts)
    LOGGER.info("Python version: %s", sys.version.split()[0])
    LOGGER.info("Project root: %s", PROJECT_ROOT)

    # --- Price snapshot (IQFeed stub -> Polygon fallback) ---
    iq_cfg = IQFeedConfig.from_env()
    iq_client = IQFeedClient(iq_cfg)
    iq_snap = None
    try:
        iq_snap = iq_client.lookup_last("@SPX.X")
    except Exception:  # noqa: BLE001
        iq_snap = None

    try:
        poly_cfg = PolygonConfig.from_env()
        poly_client = PolygonClient(poly_cfg)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Polygon client init failed: %s", exc)
        poly_client = None  # type: ignore

    if iq_snap and iq_snap.get("last_price"):
        price = float(iq_snap.get("last_price") or 0.0)
    else:
        prev = None
        if poly_client:
            try:
                prev = poly_client.last_trade_spx(use_cache=True)
            except Exception:  # noqa: BLE001
                prev = None
        if prev and "results" in prev and prev["results"]:
            price = float(prev["results"][0].get("c", 0.0))
        else:
            # Synthetic fallback for offline / CI contexts
            LOGGER.warning("No external price sources; using synthetic fallback price 5000.0")
            price = 5000.0

    # Build synthetic pseudo bar extremes for context (these are placeholders)
    day_high = price * 1.002
    day_low = price * 0.998
    snap = {"symbol": "SPX", "lastPrice": price, "day_high": day_high, "day_low": day_low}

    # --- Legacy simple strategy ---
    strat_simple = SimpleIntradaySPXStrategy()
    signals_simple = list(strat_simple.evaluate(snap))
    signals_list = _emit_signals(signals_simple)

    # --- Blended ODTE strategy warm-up ---
    StratBlended = get_strategy("odte_blended")
    strat_blended = StratBlended()

    # Synthetic intraday-like history for indicator warm-up
    closes = np.array([price * (1 + 0.0004 * i * math.sin(i / 7.0)) for i in range(80)], dtype=float)
    for c in closes:
        list(strat_blended.evaluate({"lastPrice": float(c), "lastSize": 10}))

    # Simulated options chain skew
    opt = IQFeedOptionsGreeks()
    chain = opt.fetch_chain_greeks("SPX") or []
    skew_stats = skew_signal_from_chain(chain)
    skew_score = skew_stats["skew_score"]
    LOGGER.info("Skew stats: %s", skew_stats)

    regime = "trending" if abs(closes[-1] - closes[-20]) > 0.002 * price else "choppy"
    season_bias = 0.05 if datetime.utcnow().month in (4, 11) else 0.0
    blended_ctx = {
        "lastPrice": price,
        "lastSize": 1000,
        "skew_norm": skew_score,
        "regime": regime,
        "seasonality_bias": season_bias,
    }
    blended_signals = list(strat_blended.evaluate(blended_ctx))
    signals_list.extend(_emit_signals(blended_signals))

    # Confidence / decision annotation (not trading advice)
    blended_score_sig = next((s for s in blended_signals if s.name == "odte_score"), None)
    if blended_score_sig:
        strength = blended_score_sig.strength or 0.0
        decision = "NO-TRADE"
        if strength >= CONFIDENCE_THRESHOLD:
            pos = getattr(strat_blended, "_state").position  # type: ignore[attr-defined]
            if pos == 1:
                decision = "CONSIDER LONG_CALLS"
            elif pos == -1:
                decision = "CONSIDER LONG_PUTS"
        LOGGER.info(
            "Blended decision | strength=%.4f threshold=%.2f decision=%s", strength, CONFIDENCE_THRESHOLD, decision
        )

    # Persist signals
    if signals_list:
        df = pd.DataFrame([s.to_dict() for s in signals_list])
        out_path = write_parquet(
            df,
            Path(settings.data_dir) / "signals",
            f"signals_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}",
        )
        LOGGER.info("Wrote signals parquet: %s", out_path)

    # Optional OpenAI summarization
    if settings.has_openai:
        wrapper = OpenAIWrapper(settings.openai_api_key)
        summary = wrapper.summarize_signals([s.to_dict() for s in signals_list])
        LOGGER.info("OpenAI summary: %s", summary)
    else:
        LOGGER.info("OpenAI summary skipped (no API key)")

    LOGGER.info("Run complete")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
