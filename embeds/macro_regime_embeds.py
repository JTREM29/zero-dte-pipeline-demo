from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import discord


def _fmt_pct(x: object) -> str:
    try:
        if x is None:
            return "n/a"
        v = float(x)
        return f"{v:.2f}%"
    except Exception:
        return "n/a"


def _fmt_bp(x: object) -> str:
    try:
        if x is None:
            return "n/a"
        v = float(x)
        return f"{v*100:.0f} bp"  # spread is in %
    except Exception:
        return "n/a"


def build_macro_regime_embed(
    *,
    regime: Dict[str, Any],
    now_et: datetime,
    next_events: Optional[list[Dict[str, Any]]] = None,
    blackout_state: Optional[Dict[str, Any]] = None,
    schedule_label: Optional[str] = None,
    econ_refreshed_age_min: Optional[int] = None,
) -> discord.Embed:
    macro_risk = str(regime.get("macro_risk") or "UNKNOWN").upper()
    posture = str(regime.get("posture") or "NORMAL").upper()

    color = 0x2F81F7
    if macro_risk == "HIGH" or posture == "STAND DOWN":
        color = 0xD73A49
    elif macro_risk == "MED" or posture == "CAUTIOUS":
        color = 0xF2CC60

    e = discord.Embed(
        title="📊 Macro Pulse (TNT)",
        description=f"**Posture:** **{posture}**  •  **Macro risk:** **{macro_risk}**  •  {now_et.strftime('%a %b %d %I:%M %p ET')}",
        color=color,
    )

    levels = regime.get("levels") if isinstance(regime.get("levels"), dict) else {}
    spread = regime.get("spread_2s10s")

    e.add_field(
        name="Rates",
        value=(
            f"10Y: **{_fmt_pct(levels.get('yield_10y'))}**\n"
            f"2Y: **{_fmt_pct(levels.get('yield_2y'))}**\n"
            f"2s10s: **{_fmt_bp(spread)}**\n"
            f"level={regime.get('rates_level','?')} • curve={regime.get('curve_shape','?')}"
        ),
        inline=True,
    )

    e.add_field(
        name="Inflation",
        value=(
            f"CPI YoY: **{_fmt_pct(levels.get('cpi_yoy'))}**\n"
            f"Core: **{levels.get('core_cpi','n/a')}**\n"
            f"trend={regime.get('inflation_trend','?')} • core={regime.get('core_trend','?')}"
        ),
        inline=True,
    )

    e.add_field(
        name="Labor / Expectations",
        value=(
            f"Unemp: **{_fmt_pct(levels.get('unemployment_rate'))}**\n"
            f"5y5y: **{_fmt_pct(levels.get('exp_5y5y'))}**\n"
            f"labor={regime.get('labor_tightness','?')} • exp={regime.get('infl_expectations','?')}"
        ),
        inline=True,
    )

    if blackout_state and bool(blackout_state.get("active")):
        e.add_field(
            name="Macro blackout",
            value=f"ACTIVE • {blackout_state.get('title','event')} • ends {blackout_state.get('blackout_ends_et','?')}",
            inline=False,
        )

    if next_events:
        lines = []
        for item in next_events[:4]:
            lines.append(str(item))
        if lines:
            e.add_field(name="Next releases", value="\n".join(lines), inline=False)

    footer = ["TNT • Macro Regime"]
    if schedule_label:
        footer.append(f"schedule: {schedule_label}")
    if isinstance(econ_refreshed_age_min, int) and econ_refreshed_age_min >= 0:
        footer.append(f"econ refreshed: {econ_refreshed_age_min}m ago")
    e.set_footer(text=" | ".join(footer))
    return e
