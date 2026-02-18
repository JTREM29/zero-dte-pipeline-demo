import asyncio
from datetime import datetime
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector


async def main() -> None:
    connector = UnifiedDataConnector()
    await connector.connect()
    chain = await connector.get_options_chain("SPX", datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    await connector.disconnect()
    size = 0 if chain is None else len(chain)
    print(f"CHAIN_ROWS={size}")
    if chain is not None:
        liquid = chain[
            ((chain.get("open_interest", 0) >= 50) |
             (chain.get("volume", 0) >= 10))
        ]
        print("LIQUID_ROWS=", len(liquid))
        print(chain[["symbol", "open_interest", "volume", "bid", "ask"]].head(20))


if __name__ == "__main__":
    asyncio.run(main())
