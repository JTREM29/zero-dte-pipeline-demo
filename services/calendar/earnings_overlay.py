from __future__ import annotations

import datetime as dt

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback for minimal envs
    ET = dt.timezone.utc


def _fmt_session(ev: dict) -> str:
    s = str(ev.get("session") or "UNKNOWN").strip().upper()
    if s in {"BMO", "AMC", "DURING"}:
        return s
    return "UNKNOWN"


def _is_today_or_tomorrow(ts_utc_iso: str) -> tuple[bool, bool]:
    try:
        d = dt.datetime.fromisoformat(str(ts_utc_iso).replace("Z", "+00:00")).astimezone(ET)
    except Exception:
        return False, False

    now = dt.datetime.now(ET)
    try:
        return d.date() == now.date(), d.date() == (now.date() + dt.timedelta(days=1))
    except Exception:
        return False, False


def build_earnings_risk_overlay(ev: dict | None) -> dict:
    """Build a compact, trader-facing earnings overlay.

    Returns a dict suitable for reason enrichment:
      {"headline": "...", "detail": "...", "tags": {...}}

    If ev is missing/invalid, returns {}.
    """

    if not isinstance(ev, dict) or not ev.get("ts_utc"):
        return {}

    ts = str(ev.get("ts_utc") or "").strip()
    if not ts:
        return {}

    session = _fmt_session(ev)
    confirmed = bool(ev.get("confirmed", False))
    today, tomorrow = _is_today_or_tomorrow(ts)

    when = "today" if today else ("tomorrow" if tomorrow else "")
    base = f"earnings {session}"
    if when:
        base += f" {when}"
    if confirmed:
        base += " (confirmed)"

    parts: list[str] = []
    em = ev.get("expected_move_pct")
    if isinstance(em, (int, float)):
        parts.append(f"expected move ±{abs(float(em)):.1f}%")

    lr = str(ev.get("liquidity_risk") or "").strip().upper()
    if lr in {"LOW", "MED", "HIGH"}:
        parts.append(f"liquidity: {lr}")

    iv = ev.get("front_iv")
    if isinstance(iv, (int, float)):
        parts.append(f"front IV: {float(iv):.0f}")

    detail = " • ".join(parts) if parts else ""

    return {
        "headline": base,
        "detail": detail,
        "tags": {
            "session": session,
            "confirmed": confirmed,
            "today": today,
            "tomorrow": tomorrow,
            "expected_move_pct": float(em) if isinstance(em, (int, float)) else None,
            "liquidity_risk": lr if lr in {"LOW", "MED", "HIGH"} else None,
        },
    }


def build_earnings_near_note(ev: dict | None) -> str | None:
    """Build a lightweight 'earnings nearby' note for today/tomorrow.

    Intended for *non-blackout* suppressions where earnings isn't the primary cause,
    but we still want to warn the user about likely upcoming blackout behavior.

    Returns a single string like:
      "earnings AMC tomorrow (expected move ±4.2% • liquidity: HIGH)"

    Returns None if ev is missing/invalid or not today/tomorrow (ET).
    """

    o = build_earnings_risk_overlay(ev)
    if not isinstance(o, dict) or not o.get("headline"):
        return None

    tags = o.get("tags") if isinstance(o.get("tags"), dict) else {}
    if not (tags.get("today") or tags.get("tomorrow")):
        return None

    headline = str(o.get("headline") or "").strip()
    if not headline:
        return None

    detail = str(o.get("detail") or "").strip()
    if detail:
        return f"{headline} • {detail}"[:240]
    return headline[:240]
