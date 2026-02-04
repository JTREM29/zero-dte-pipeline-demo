from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional


def _utc_date_str(ts: Optional[float] = None) -> str:
    ts = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def tail_jsonl(path: Path, n: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    # simple, safe tail: read all if small; otherwise read bytes from end
    try:
        data = path.read_text(encoding="utf-8").splitlines()
        lines = data[-n:]
    except Exception:
        # fallback: best-effort
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()[-n:]

    out: List[Dict[str, Any]] = []
    for ln in lines:
        ln = (ln or "").strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def format_modlog_rows(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No moderator audit entries found for today."
    # newest last in file; we want newest first for display
    rows = list(reversed(rows))

    def one(r: Dict[str, Any]) -> str:
        ts = r.get("ts_utc", "?")
        state = r.get("state", "?")
        intent = r.get("intent", "?")
        rule = r.get("rule") or r.get("reason") or "-"
        conf = r.get("confidence", "?")
        uid = r.get("user_id", "?")
        ch = r.get("channel_id", "?")

        f_ok = bool(r.get("futures_ok", False))
        f_age = r.get("futures_age_s", None)
        f_lbl = r.get("futures_label", None)

        snap_ok = bool(r.get("snapshot_ok", r.get("symbol_ok", False)))
        snap_age = r.get("snapshot_age_s", None)
        if snap_age is None:
            snap_age = r.get("symbol_age_s", None)

        fut = f"fut={'OK' if f_ok else 'NO'}"
        if f_age is not None:
            fut += f" age={f_age}s"
        if f_lbl:
            fut += f" {f_lbl}"

        snap = f"snap={'OK' if snap_ok else 'NO'}"
        if snap_age is not None:
            snap += f" age={snap_age}s"

        return f"{ts} | {state} | {intent} | rule={rule} | conf={conf} | u={uid} ch={ch} | {fut} | {snap}"

    # Discord message safety: cap output
    lines = [one(r) for r in rows[:25]]
    text = "\n".join(lines)
    if len(text) > 1800:
        text = text[:1800] + "\n...(truncated)"
    return text


def read_today_modlog(root_dir: str, tail_n: int) -> str:
    day = _utc_date_str()
    path = Path(root_dir) / day / "moderator.jsonl"
    rows = tail_jsonl(path, tail_n)
    return format_modlog_rows(rows)
