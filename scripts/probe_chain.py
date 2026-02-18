from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
from zero_dte_pipeline.utils.logging import get_logger

log = get_logger(__name__)


async def _probe_chain(symbol: str, expiry_str: Optional[str]) -> None:
    expiry = datetime.fromisoformat(expiry_str) if expiry_str else None

    async with UnifiedDataConnector() as conn:
        log.info("Starting chain probe for %s (expiry=%s)", symbol, expiry or "today")
        t0 = time.perf_counter()
        try:
            chain = await conn.get_options_chain(symbol, expiration=expiry)
        except Exception as exc:
            t1 = time.perf_counter()
            log.error("Chain request FAILED after %.2fs: %r", t1 - t0, exc)
            return

        t1 = time.perf_counter()
        n = len(chain) if chain is not None else 0
        log.info("Chain request OK in %.2fs, contracts=%d", t1 - t0, n)

        if chain is None or chain.empty:
            log.warning("Chain frame is empty")
            return

        sample = chain.head(5)
        log.info("Sample rows:\n%s", sample)


def main() -> None:
    asyncio.run(_probe_chain(symbol="SPX", expiry_str=None))


if __name__ == "__main__":
    main()
