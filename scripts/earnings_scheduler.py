from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running this script from any working directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.earnings_precache import main_async


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, str(default)) or str(default)).strip()
    try:
        return int(raw)
    except Exception:
        return int(default)


def _truthy(name: str, default: str = "1") -> bool:
    try:
        v = (os.getenv(name, default) or default).strip().lower()
        return v in {"1", "true", "yes", "on"}
    except Exception:
        return False


async def _run_weekday_once(*, dry_run: bool = False) -> None:
    try:
        await main_async("weekday", dry_run=dry_run)
    except Exception:
        # Best-effort runner: failures should not kill the loop.
        return


async def _run_sunday_once(*, dry_run: bool = False) -> None:
    try:
        await main_async("sunday", dry_run=dry_run)
    except Exception:
        return


async def scheduler_loop() -> None:
    enabled = _truthy("EARNINGS_SCHEDULER_ENABLED", "1")
    if not enabled:
        return

    poll_sec = max(60, _env_int("EARNINGS_SCHEDULER_POLL_SEC", 15 * 60))
    sunday_poll_sec = max(60, _env_int("EARNINGS_SCHEDULER_SUNDAY_POLL_SEC", 60 * 60))

    last_weekday_ok: int | None = None
    last_sunday_ok: int | None = None

    while True:
        now = int(time.time())
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
        weekday = dt.weekday()  # Mon=0..Sun=6

        # Sunday: run a heavy warm at most once per day.
        if weekday == 6:
            if last_sunday_ok is None or (now - last_sunday_ok) > 18 * 60 * 60:
                await _run_sunday_once(dry_run=False)
                last_sunday_ok = int(time.time())
            await asyncio.sleep(float(sunday_poll_sec))
            continue

        # Mon-Sat: light refresh on interval.
        if last_weekday_ok is None or (now - last_weekday_ok) >= int(poll_sec):
            await _run_weekday_once(dry_run=False)
            last_weekday_ok = int(time.time())

        await asyncio.sleep(float(poll_sec))


def main() -> None:
    raise SystemExit(asyncio.run(scheduler_loop()))


if __name__ == "__main__":
    main()
