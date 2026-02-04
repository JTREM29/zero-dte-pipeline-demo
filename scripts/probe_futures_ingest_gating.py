from __future__ import annotations

import os
import time

from services.futures.futures_store import FuturesStore
from services.observability.futures_ingest_health import classify_futures_ingest


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(float(str(raw).strip())) if raw is not None and str(raw).strip() else int(default)
    except Exception:
        return int(default)


def main() -> None:
    store = FuturesStore()

    hb_ts, hb_msg = store.get_heartbeat()
    now_s = float(time.time())

    hb_age: int | None
    if hb_ts is None:
        hb_age = None
    else:
        hb_age = max(0, int(round(now_s - float(hb_ts))))

    scores = store.get_scores()
    scores_present = scores is not None
    scores_updated = getattr(scores, "updated_utc", None) if scores is not None else None
    if scores_updated is None and isinstance(scores, dict):
        scores_updated = scores.get("updated_utc")

    scores_age: int | None
    if scores_present and scores_updated is not None:
        try:
            scores_age = max(0, int(round(now_s - float(scores_updated))))
        except Exception:
            scores_age = None
    else:
        scores_age = None

    status = store.get_status() or {}
    note = status.get("note") if isinstance(status, dict) else None

    hb_max = _env_int("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", 90)
    scores_max = _env_int("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", 120)

    state, reason = classify_futures_ingest(
        hb_age_s=hb_age,
        scores_age_s=scores_age,
        scores_present=scores_present,
        note=str(note) if note is not None else None,
        hb_msg=str(hb_msg) if hb_msg is not None else None,
        hb_max_age=hb_max,
        scores_max_age=scores_max,
    )

    # Mirror /status "legacy futures ingest" line logic (cli/discord_bot.py).
    if hb_age is None:
        legacy = None
    elif hb_age > hb_max:
        legacy = f"DOWN (hb {hb_age}s stale)"
    elif not scores_present or scores_age is None:
        legacy = f"DEGRADED (hb {hb_age}s, scores missing)"
    elif scores_age > scores_max:
        legacy = f"DEGRADED (hb {hb_age}s, scores {scores_age}s stale)"
    else:
        legacy = f"OK (hb {hb_age}s, scores {scores_age}s)"

    print(
        " ".join(
            [
                f"classify={state}",
                (f"reason={reason}" if reason else "reason=—"),
                f"hb_age={hb_age if hb_age is not None else '—'}s",
                f"scores_age={scores_age if scores_age is not None else '—'}s",
                f"hb_msg={hb_msg or '—'}",
                f"note={note or '—'}",
                f"status_line={legacy or '—'}",
            ]
        )
    )


if __name__ == "__main__":
    main()
