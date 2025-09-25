"""Entry point for the ZeroDTE pipeline.

Orchestrates a minimal demo run:
1. Load environment (.env optional)
2. Initialize logging
3. Instantiate Polygon client (placeholder)
4. Fetch underlying snapshot (SPX)
5. Run simple strategy evaluation
6. Print signals
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime
from typing import Iterable

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

from src.utils.logging_setup import get_logger
from src.datafeeds.polygon_client import PolygonClient, PolygonConfig
from src.strategies.simple_intraday_spx import SimpleIntradaySPXStrategy, StrategySignal

PROJECT_ROOT = Path(__file__).parent.resolve()
LOGGER = get_logger("zero_dte.main")


def _emit_signals(signals: Iterable[StrategySignal]) -> None:
    for sig in signals:
        LOGGER.info("Signal %s = %.4f | meta=%s", sig.name, sig.value, sig.metadata)


def main() -> int:
    if load_dotenv:
        load_dotenv()  # load from .env if present
    ts = datetime.utcnow().isoformat()
    LOGGER.info("Bootstrap at %s UTC", ts)
    LOGGER.info("Python version: %s", sys.version.split()[0])
    LOGGER.info("Project root: %s", PROJECT_ROOT)

    # Data feed client
    try:
        poly_cfg = PolygonConfig.from_env()
        poly_client = PolygonClient(poly_cfg)
        snap = poly_client.fetch_underlying_snapshot("SPX")
        LOGGER.info("Fetched snapshot: %s", snap)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Polygon snapshot unavailable: %s", exc)
        snap = {"symbol": "SPX", "lastPrice": 0.0}

    # Strategy evaluation
    strat = SimpleIntradaySPXStrategy()
    signals = strat.evaluate(snap)
    _emit_signals(signals)

    LOGGER.info("Run complete")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
