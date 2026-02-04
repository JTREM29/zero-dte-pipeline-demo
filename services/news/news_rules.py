from __future__ import annotations

import hashlib
from typing import Any


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def headline_dedup_hash(*, headline: str, tickers: list[str] | None = None, source: str | None = None) -> str:
    """24h dedup key: stable hash over headline+ticker-set+source."""

    t = sorted({(x or "").strip().upper() for x in (tickers or []) if (x or "").strip()})
    base = f"{_norm(headline)}|{','.join(t)}|{_norm(source or '')}"
    h = hashlib.sha1()  # noqa: S324
    h.update(base.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def classify_news(item: dict[str, Any]) -> tuple[list[str], str]:
    """Rule-based tags + severity.

    Severity is intentionally conservative to avoid spam.
    """

    headline = _norm(str(item.get("headline") or item.get("title") or ""))
    tags: list[str] = []

    def has(*words: str) -> bool:
        return any(w in headline for w in words)

    if has("earnings", "eps", "revenue", "guidance", "profit warning"):
        tags.append("earnings")
    if has("guidance", "raises", "cuts", "outlook"):
        tags.append("guidance")
    if has("upgrade", "downgrade", "initiated", "price target"):
        tags.append("rating")
    if has("acquire", "acquisition", "merger", "m&a", "buyout"):
        tags.append("m&a")
    if has("sec", "doj", "investigation", "subpoena", "lawsuit", "settlement"):
        tags.append("sec")
    if has("fda", "clinical", "trial", "phase", "approval", "rejection"):
        tags.append("fda")
    if has("cpi", "ppi", "fed", "fomc", "jobs report", "payroll", "macro"):
        tags.append("macro")

    # Severity heuristic
    severity = "LOW"
    if has(
        "bankruptcy",
        "halted",
        "suspends",
        "default",
        "fraud",
        "investigation",
        "fda approval",
        "fda rejects",
        "sec charges",
        "doj",
        "merger",
        "acquisition",
        "buyout",
        "profit warning",
    ):
        severity = "HIGH"
    elif has(
        "guidance",
        "raises",
        "cuts",
        "upgrade",
        "downgrade",
        "earnings",
        "eps",
        "revenue",
    ):
        severity = "MED"

    # Market-wide macro items should be MED at minimum.
    if "macro" in tags and severity == "LOW":
        severity = "MED"

    return tags, severity
