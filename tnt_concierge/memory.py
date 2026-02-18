from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


def _audit_path() -> Path:
    return Path(os.getenv("TNT_CONCIERGE_AUDIT_PATH", str(Path("logs") / "concierge_events.jsonl")))


def memory_hint(*, ticker: str, max_lines: int = 500) -> Optional[str]:
    """Very lightweight 'comparative memory'.

    Reads recent concierge events and returns a small hint like:
      "Memory: 2 nudges today (last 12m ago)."

    Best-effort: returns None on any error.
    """
    if os.getenv("TNT_CONCIERGE_MEMORY", "0") != "1":
        return None

    sym = (ticker or "").strip().upper()
    if not sym:
        return None

    p = _audit_path()
    if not p.exists():
        return None

    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return None

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None

    lines = lines[-max_lines:]

    now = time.time()
    today = datetime.utcnow().date().isoformat()

    count_today = 0
    last_ts: Optional[float] = None

    for ln in reversed(lines):
        try:
            evt = json.loads(ln)
        except Exception:
            continue
        if str(evt.get("ticker", "") or "").upper() != sym:
            continue

        ts_s = str(evt.get("ts", "") or "")
        if ts_s.startswith(today):
            count_today += 1

        if last_ts is None:
            try:
                # Parse ISO "...Z" to datetime
                dt = datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
                last_ts = dt.timestamp()
            except Exception:
                last_ts = None

        # We can stop early once we have both.
        if last_ts is not None and count_today >= 3:
            break

    if count_today <= 0 and last_ts is None:
        return None

    parts = []
    if count_today > 0:
        parts.append(f"{count_today} nudges today")
    if last_ts is not None:
        mins = max(0, int(round((now - last_ts) / 60.0)))
        parts.append(f"last {mins}m ago")

    if not parts:
        return None

    return "Memory: " + " (".join([parts[0], ", ".join(parts[1:])]) + ")." if len(parts) > 1 else "Memory: " + parts[0] + "."
