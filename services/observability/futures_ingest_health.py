from __future__ import annotations


def _norm_str(x: object | None) -> str:
    if x is None:
        return ""
    return str(x).strip()


def classify_futures_ingest(
    hb_age_s: int | None,
    scores_age_s: int | None,
    scores_present: bool,
    note: str | None,
    hb_msg: str | None,
    hb_max_age: int,
    scores_max_age: int,
) -> tuple[str, str]:
    """Classify futures ingest health as OK/DEGRADED/DOWN.

    This is the single source of truth for operator-facing status and canaries.

    Inputs:
    - hb_age_s: heartbeat age in seconds (None => missing)
    - scores_age_s: scores age in seconds (None allowed; used only if scores_present)
    - scores_present: whether scores are present at all
    - note/hb_msg: optional short strings indicating degraded state
    - hb_max_age/scores_max_age: thresholds in seconds

    Returns:
    - state: "OK" | "DEGRADED" | "DOWN"
    - reason: short, calm operator string (may be "")
    """

    hb_max = max(1, int(hb_max_age))
    scores_max = max(1, int(scores_max_age))

    note_s = _norm_str(note)
    hb_msg_s = _norm_str(hb_msg)

    # DOWN: heartbeat missing or too old.
    if hb_age_s is None:
        return "DOWN", "hb missing"
    try:
        hb_age = int(hb_age_s)
    except Exception:
        return "DOWN", "hb missing"
    if hb_age > hb_max:
        return "DOWN", f"hb {hb_age}s stale"

    # Heartbeat is fresh enough.
    if not bool(scores_present):
        return "DEGRADED", "scores missing"

    if scores_age_s is None:
        return "DEGRADED", "scores stale"
    try:
        scores_age = int(scores_age_s)
    except Exception:
        return "DEGRADED", "scores stale"
    if scores_age > scores_max:
        return "DEGRADED", f"scores {scores_age}s stale"

    # Explicit non-ok markers degrade.
    if hb_msg_s and hb_msg_s.lower() not in {"ok"}:
        return "DEGRADED", f"hb_msg={hb_msg_s}"[:120]
    if note_s and note_s.lower() not in {"ok"}:
        return "DEGRADED", note_s[:120]

    return "OK", ""
