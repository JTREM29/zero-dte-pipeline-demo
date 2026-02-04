from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def upcoming_events(now_utc: datetime | None = None) -> list[dict[str, Any]]:
    """Return a static list of upcoming macro events.

    v1: intentionally minimal; operators can seed events via /macro_ping.
    """

    _ = now_utc or datetime.now(timezone.utc)
    return []
