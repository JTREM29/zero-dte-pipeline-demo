from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import redis

# Allow running this script from any working directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.context.context_writer import write_context_once
from services.redis_env import redis_client


_DEFAULT_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "VIX",
    "SPX",
)


def _redis_client() -> redis.Redis:
    return redis_client(timeout_s=None, decode_responses=True)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


class _Store:
    def __init__(self, r: redis.Redis):
        self.r = r


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="One-shot ctx snapshot publisher (ctx:market + ctx:sym:*)")
    ap.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols to publish (default: SPY,QQQ,IWM,VIX,SPX).",
    )
    ap.add_argument(
        "--stale-hours",
        type=int,
        default=_env_int("EARNINGS_PREVIEW_STALE_HOURS", 48),
        help="Earnings overlay stale hours (default from env EARNINGS_PREVIEW_STALE_HOURS or 48).",
    )
    ap.add_argument(
        "--ttl-sec",
        type=int,
        default=0,
        help="Optional Redis TTL seconds for ctx keys (0=none).",
    )

    args = ap.parse_args()

    symbols = _parse_symbols(args.symbols) or list(_DEFAULT_SYMBOLS)
    symbols = [s for s in symbols if s]

    ttl = int(args.ttl_sec) if int(args.ttl_sec) > 0 else None

    store = _Store(_redis_client())
    out = write_context_once(store, symbols=symbols, stale_hours=int(args.stale_hours), ttl_sec=ttl)

    print("CTX_WRITE_ONCE")
    print("  wrote_count=", out.get("count"))
    print("  symbols=", out.get("symbols"))


if __name__ == "__main__":
    main()
