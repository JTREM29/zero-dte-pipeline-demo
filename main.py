"""Entry point for the ZeroDTE pipeline.

Provides a default run that:
1. Loads .env (if present)
2. Loads Settings
3. Initializes logging (console + rotating file)
4. Fetches snapshot (Polygon placeholder)
5. Evaluates simple strategy
6. Writes signals to parquet
7. Optionally summarizes with OpenAI
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime
from typing import Iterable
import pandas as pd

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

from src.utils.logging_setup import get_logger
from src.datafeeds.polygon_client import PolygonClient, PolygonConfig
from src.strategies.simple_intraday_spx import SimpleIntradaySPXStrategy, StrategySignal
from src.config import Settings
from src.persistence import write_parquet
from src.openai_client import OpenAIWrapper

PROJECT_ROOT = Path(__file__).parent.resolve()
LOGGER = get_logger("zero_dte.main")


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

    try:
        poly_cfg = PolygonConfig.from_env()
        poly_client = PolygonClient(poly_cfg)
        snap = poly_client.fetch_underlying_snapshot("SPX")
        LOGGER.info("Fetched snapshot: %s", snap)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Polygon snapshot unavailable: %s", exc)
        snap = {"symbol": "SPX", "lastPrice": 0.0}

    strat = SimpleIntradaySPXStrategy()
    signals_list = _emit_signals(strat.evaluate(snap))

    # Persist signals
    if signals_list:
        df = pd.DataFrame([s.to_dict() for s in signals_list])
        out_path = write_parquet(df, Path(settings.data_dir) / "signals", f"signals_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}")
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
