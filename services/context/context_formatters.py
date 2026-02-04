from __future__ import annotations


def fmt_futures(ctx_sym: dict) -> str | None:
    b = (((ctx_sym.get("futures") or {}).get("bias")) or "").upper()
    if b in ("BULLISH", "BEARISH", "NEUTRAL"):
        return f"ES: {b.lower()}"
    return None


def fmt_news(ctx_sym: dict) -> str | None:
    n = ctx_sym.get("news") or {}
    state = n.get("sym_state") or n.get("market_state")
    if state == "just_hit":
        return "News: just hit"
    if state == "recent":
        return "News: recent"
    if state == "quiet":
        return "News: quiet"
    return None


def fmt_earnings_near(ctx_sym: dict) -> str | None:
    e = ctx_sym.get("earnings")
    if not e or not e.get("fresh"):
        return None

    note = str(e.get("note") or "").strip()
    return note or None


def fmt_earnings_preview_footer(ctx_sym: dict, *, max_len: int = 240) -> str | None:
    e = ctx_sym.get("earnings")
    if not e or not e.get("fresh"):
        return None

    ov = e.get("overlay") if isinstance(e, dict) else None
    if not isinstance(ov, dict):
        return None

    headline = str(ov.get("headline") or "").strip()
    if not headline:
        return None

    tags = ov.get("tags") if isinstance(ov.get("tags"), dict) else {}
    if not (tags.get("today") or tags.get("tomorrow")):
        return None

    headline = headline[0:1].upper() + headline[1:]
    msg = f"⚠ {headline}"

    detail = str(ov.get("detail") or "").strip()
    if detail:
        msg += f" • {detail}"

    note = str(e.get("note") or "").strip()
    if note and note.lower() not in msg.lower():
        msg += f" — {note}"

    return msg if len(msg) <= max_len else msg[:max_len]


def fmt_edge_clarity(parts: list[str], max_len: int = 120) -> str | None:
    parts = [p for p in (parts or []) if p]
    if not parts:
        return None
    s = "Context: " + " • ".join(parts)
    return s if len(s) <= max_len else (s[: max(0, max_len - 1)] + "…")
