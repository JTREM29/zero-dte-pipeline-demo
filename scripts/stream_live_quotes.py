#!/usr/bin/env python
"""Stream real-time quotes with Polygon primary and IQFeed fallback."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

from data import iqfeed_client
from zero_dte_pipeline.data_connectors.polygon import PolygonConnector

LOG = logging.getLogger("stream_live_quotes")
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")

_POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")


async def _fetch_polygon_quote(symbol: str) -> Optional[Tuple[str, float]]:
    connector = PolygonConnector(api_key=_POLYGON_API_KEY, timeout=10)
    try:
        connected = await connector.connect()
        if not connected:
            LOG.warning("Polygon connection failed: %s", connector.last_error)
            return None
        quote = await connector.get_quote(symbol)
        if not quote:
            LOG.warning("Polygon returned no quote for %s", symbol)
            return None
        price = quote.get("last")
        if price is None:
            LOG.warning("Polygon quote missing price field for %s", symbol)
            return None
        return "polygon", float(price)
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("Polygon quote error (%s): %s", symbol, exc)
        return None
    finally:
        await connector.disconnect()


async def _fetch_iqfeed_quote(symbol: str) -> Optional[Tuple[str, float]]:
    if not iqfeed_client.IQFEED_ENABLED:
        return None
    try:
        connector = iqfeed_client.create_connector(timeout=10)
    except RuntimeError:
        return None
    try:
        connected = await connector.connect()
        if not connected:
            LOG.warning("IQFeed connection failed: %s", getattr(connector, "last_error", "unknown"))
            return None
        quote = await connector.get_quote(symbol)
        if not quote:
            LOG.warning("IQFeed returned no quote for %s", symbol)
            return None
        price = quote.get("last") or quote.get("bid") or quote.get("ask")
        if price is None:
            LOG.warning("IQFeed quote missing price field for %s", symbol)
            return None
        return "iqfeed", float(price)
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("IQFeed quote error (%s): %s", symbol, exc)
        return None
    finally:
        try:
            await connector.disconnect()
        except Exception:
            pass


async def _fetch_with_fallback(symbol: str, providers: Iterable[str]) -> Tuple[Optional[str], Optional[float]]:
    for provider in providers:
        if provider == "polygon":
            result = await _fetch_polygon_quote(symbol)
        elif provider == "iqfeed":
            result = await _fetch_iqfeed_quote(symbol)
        else:
            result = None
        if result is not None:
            return result
    return None, None


def _provider_order(use_iqfeed_first: bool) -> Tuple[str, ...]:
    if use_iqfeed_first and iqfeed_client.IQFEED_ENABLED:
        return ("iqfeed", "polygon")
    if iqfeed_client.IQFEED_ENABLED:
        return ("polygon", "iqfeed")
    return ("polygon",)


async def _stream_quotes(symbol: str, duration: float, use_iqfeed_first: bool) -> None:
    order = _provider_order(use_iqfeed_first)
    if not order:
        LOG.error("No providers available; aborting")
        return
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        loop_started = time.monotonic()
        source, price = await _fetch_with_fallback(symbol, order)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "source": source or "unavailable",
            "price": price,
        }
        print(json.dumps(payload, separators=(",", ":")), flush=True)
        sleep_for = 1.0 - (time.monotonic() - loop_started)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch live quotes with Polygon/IQFeed fallback")
    parser.add_argument("--symbol", default="SPX", help="Ticker symbol to stream")
    parser.add_argument("--duration", type=float, default=30.0, help="Duration to stream in seconds")
    parser.add_argument("--use-iqfeed-first", action="store_true", help="Try IQFeed before Polygon")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.duration <= 0:
        LOG.error("Duration must be positive")
        return 1
    try:
        asyncio.run(_stream_quotes(args.symbol.upper(), args.duration, args.use_iqfeed_first))
    except KeyboardInterrupt:
        LOG.info("Interrupted")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
