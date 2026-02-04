from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

import discord

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = dt.timezone.utc


def _fmt_et(ts_utc_iso: str) -> str:
    try:
        raw = str(ts_utc_iso).strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(raw)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(ET).strftime("%a %b %d • %I:%M %p ET")
    except Exception:
        return "unknown"


def build_news_embed(
    *,
    title: str,
    items: Iterable[dict[str, Any]],
    footer: str | None = None,
) -> discord.Embed:
    e = discord.Embed(title=title, color=discord.Color.gold())

    lines: list[str] = []
    for it in list(items)[:3]:
        headline = str(it.get("headline") or it.get("title") or "").strip()
        if not headline:
            continue
        ts = str(it.get("ts_utc") or it.get("published_utc") or "").strip()
        tickers = it.get("tickers")
        if isinstance(tickers, list) and tickers:
            tick = ",".join(str(t).upper() for t in tickers[:6] if t)
            tick_txt = f" [{tick}]"
        else:
            tick_txt = ""
        tags = it.get("tags")
        tags_txt = ""
        if isinstance(tags, list) and tags:
            tags_txt = " • " + ",".join(str(t) for t in tags[:6] if t)
        sev = str(it.get("severity") or "").upper().strip()
        sev_txt = f"{sev} • " if sev in {"LOW", "MED", "HIGH"} else ""

        url = str(it.get("url") or "").strip()
        link = f"\n<{url}>" if url else ""

        lines.append(f"• {sev_txt}{_fmt_et(ts)}{tick_txt}{tags_txt}\n{headline}{link}")

    e.description = "\n\n".join(lines) if lines else "—"
    if footer:
        e.set_footer(text=footer)
    return e
