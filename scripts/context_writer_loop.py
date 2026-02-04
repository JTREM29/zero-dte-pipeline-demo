from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

import redis

# Allow running this script from any working directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.context.context_writer import context_writer_loop
from services.context.context_writer import write_context_once
from services.redis_env import redis_client


def _redis_client() -> redis.Redis:
    return redis_client(timeout_s=None, decode_responses=True)


class _Store:
    def __init__(self, r: redis.Redis):
        self.r = r


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="TNT context writer")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Write ctx:market + ctx:sym:* once and exit (useful for debugging).",
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        default=0.0,
        help="Run the async loop for N seconds then exit cleanly (smoke-test helper).",
    )
    args = parser.parse_args()

    store = _Store(_redis_client())
    try:
        if args.once:
            # Mirror the loop's symbol selection behavior.
            symbols = []
            try:
                raw = (os.getenv("EARNINGS_AUTOPOST_SYMBOLS", "") or "").strip()
                if raw:
                    symbols.extend([s.strip().upper() for s in raw.split(",") if s.strip()])
            except Exception:
                pass
            try:
                raw = (os.getenv("CONTEXT_WRITER_SYMBOLS", "") or "").strip()
                if raw:
                    symbols.extend([s.strip().upper() for s in raw.split(",") if s.strip()])
            except Exception:
                pass

            # Use same stale horizon as the loop.
            try:
                stale_h = int((os.getenv("EARNINGS_PREVIEW_STALE_HOURS", "48") or "48").strip())
            except Exception:
                stale_h = 48

            write_context_once(store, symbols=symbols, stale_hours=stale_h)
            print("[context_writer] wrote once")
        else:
            async def _runner() -> None:
                run_s = float(getattr(args, "run_seconds", 0.0) or 0.0)
                if run_s > 0:
                    task = asyncio.create_task(context_writer_loop(store))
                    try:
                        await asyncio.sleep(run_s)
                    finally:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
                    print(f"[context_writer] stopped after run_seconds={run_s:g}")
                    return

                await context_writer_loop(store)

            asyncio.run(_runner())
    except KeyboardInterrupt:
        # A long-running loop: Ctrl+C should be a clean stop, not a crash.
        print("[context_writer] stopped (KeyboardInterrupt)")
        raise SystemExit(0)
    except Exception as exc:  # noqa: BLE001
        print(f"[context_writer] failed: {type(exc).__name__}: {exc}")
        try:
            print(traceback.format_exc())
        except Exception:
            pass
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
