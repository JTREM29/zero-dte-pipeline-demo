from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import discord

ET = ZoneInfo("America/New_York")


def _fmt_dt_et(dt_et: datetime) -> str:
    # Example: Wed Feb 11 • 8:30 AM ET
    return dt_et.astimezone(ET).strftime("%a %b %d • %I:%M %p ET").replace(" 0", " ")


def _fmt_in(dt_et: datetime, now_et: datetime) -> str:
    delta = dt_et - now_et
    s = int(delta.total_seconds())
    if s <= 0:
        return "now"
    mins = s // 60
    hrs = mins // 60
    days = hrs // 24
    hrs = hrs % 24
    mins = mins % 60
    if days > 0:
        return f"in {days}d {hrs}h"
    if hrs > 0:
        return f"in {hrs}h {mins}m"
    return f"in {mins}m"


def build_macro_event_embed(
    *,
    event_title: str,
    event_id: str,
    dt_et: datetime,
    importance: str,
    blackout_before_min: int,
    blackout_after_min: int,
    now_et: datetime,
    econ_latest: Optional[Dict[str, Any]] = None,
) -> discord.Embed:
    e = discord.Embed(
        title=f"📅 {event_title}",
        description=f"**{_fmt_dt_et(dt_et)}** ({_fmt_in(dt_et, now_et)})",
        color=0x2F81F7 if str(importance).upper() == "HIGH" else 0x8B949E,
    )
    e.add_field(
        name="Blackout window",
        value=f"{int(blackout_before_min)}m before → {int(blackout_after_min)}m after",
        inline=True,
    )
    e.add_field(name="Impact", value=str(importance).upper(), inline=True)
    e.add_field(name="Event ID", value=str(event_id), inline=True)

    if econ_latest:
        asof = econ_latest.get("asof_date")
        vals = econ_latest.get("values") or {}
        lines: list[str] = []
        if isinstance(vals, dict):
            if "cpi_year_over_year" in vals:
                lines.append(f"• CPI YoY: **{vals.get('cpi_year_over_year')}%**")
            if "cpi_core" in vals:
                lines.append(f"• Core CPI: **{vals.get('cpi_core')}**")
            if "unemployment_rate" in vals:
                lines.append(f"• Unemployment: **{vals.get('unemployment_rate')}%**")
            if "yield_10_year" in vals:
                lines.append(f"• 10Y: **{vals.get('yield_10_year')}%**")

        if not lines:
            if isinstance(vals, dict):
                keys = list(vals.keys())[:6]
                if keys:
                    lines.append("• " + ", ".join(str(k) for k in keys))

        updated = econ_latest.get("updated_utc")
        e.add_field(
            name=f"Latest data (as of {asof})",
            value=("\n".join(lines) if lines else "(no fields)") + (f"\n\nLast refreshed: {updated}" if updated else ""),
            inline=False,
        )

    if str(importance).upper() == "HIGH":
        e.add_field(
            name="Guidance",
            value=(
                "⚠️ **TNT favors post-release structure only** during HIGH macro events.\n"
                "Expect whip/vol around release; avoid pre-event exposure."
            ),
            inline=False,
        )
    else:
        e.add_field(
            name="Guidance",
            value="Context event. Use for regime framing; avoid forcing trades around the timestamp.",
            inline=False,
        )

    e.set_footer(text="TNT • Macro Calendar")
    return e
