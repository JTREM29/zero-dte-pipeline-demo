from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import discord

from .futures_format import arrow, fmt_chg, fmt_pct, fmt_px, score_word
from .futures_models import FutQuote, FutScores, PublishMode

FUTS = ["ES", "NQ", "RTY"]


def _is_stale(ts_utc: int, *, threshold_sec: int = 30) -> bool:
    if not ts_utc:
        return True
    age = int(dt.datetime.now(dt.timezone.utc).timestamp()) - int(ts_utc)
    return age >= int(threshold_sec)


def _age_str(ts_utc: int) -> str:
    if not ts_utc:
        return "unknown"
    age = int(dt.datetime.now(dt.timezone.utc).timestamp()) - int(ts_utc)
    if age < 0:
        age = 0
    if age < 60:
        return f"{age}s"
    return f"{age // 60}m"


def build_futures_embed(
    quotes: Dict[str, FutQuote],
    scores: Optional[FutScores],
    mode: PublishMode,
    status: dict | None = None,
) -> discord.Embed:
    color = discord.Color.blurple()
    if scores:
        rg = (scores.regime or "").upper()
        if "RISK_OFF" in rg:
            color = discord.Color.red()
        elif "RISK_ON" in rg:
            color = discord.Color.green()
        elif "CHOP" in rg:
            color = discord.Color.gold()

    e = discord.Embed(title="TNT Futures — Market Context", color=color)

    if mode in ("quotes", "full"):
        lines: List[str] = []
        for sym in FUTS:
            q = quotes.get(sym)
            anchor = {"ES": "SPY", "NQ": "QQQ", "RTY": "IWM"}.get(sym)
            label = f"{sym} ({anchor})" if anchor else sym
            if not q:
                lines.append(f"**{label}**  |  —")
                continue
            lines.append(
                f"**{label}**  |  **{fmt_px(q.px)}**  {arrow(q.chg_pct)}  {fmt_chg(q.chg)} ({fmt_pct(q.chg_pct)})"
            )
        e.add_field(name="Prices", value="\n".join(lines), inline=False)

    if scores:
        t_es = scores.trend.get("ES")
        i_nq = scores.impulse.get("NQ")
        vol = scores.vol_mult
        breadth = f"{scores.breadth_bearish} / {scores.breadth_total} bearish"
        rg = scores.regime

        intel_lines: List[str] = []
        if t_es is not None:
            intel_lines.append(f"• ES trend: **{float(t_es):+.2f}** ({score_word(float(t_es))})")
        if i_nq is not None:
            intel_lines.append(f"• NQ impulse: **{float(i_nq):+.2f}** ({score_word(float(i_nq))})")
        intel_lines.append(f"• Volatility: **{vol:.2f}×** baseline")
        intel_lines.append(f"• Breadth: **{breadth}**")
        intel_lines.append(f"• Regime: **{rg}**")

        e.add_field(name="Futures Intelligence", value="\n".join(intel_lines), inline=False)

        tactical: List[str] = []
        if rg.upper().startswith("RISK_OFF"):
            tactical.append("• Bullish setups suppressed (futures conflict likely)")
            tactical.append("• Expect sharper pullbacks + failed bounces")
        elif rg.upper().startswith("RISK_ON"):
            tactical.append("• Bullish setups favored on clean pullback entries")
            tactical.append("• Breakouts more likely to follow-through")
        else:
            tactical.append("• Mixed tape — reduce size, avoid chasing")
            tactical.append("• Favor mean-reversion edges")

        e.add_field(name="Tactical Impact", value="\n".join(tactical), inline=False)
        footer = f"Updated: {_age_str(scores.updated_utc)} ago"
        if _is_stale(int(scores.updated_utc or 0)):
            footer += "  •  ⚠️ Futures feed stale"
        e.set_footer(text=footer)
    else:
        freshest = 0
        for q in quotes.values():
            freshest = max(freshest, q.ts_utc or 0)
        if freshest:
            footer = f"Updated: {_age_str(freshest)} ago"
            if _is_stale(int(freshest or 0)):
                footer += "  •  ⚠️ Futures feed stale"
            e.set_footer(text=footer)
        else:
            st = (status or {}) if isinstance(status, dict) else {}
            if (st.get("state") or "").lower() == "running":
                note = str(st.get("note") or "").strip()
                if not note:
                    note = "connected"
                if "await" in note.lower():
                    e.set_footer(text="Live feed: connected (awaiting prints)")
                else:
                    e.set_footer(text=f"Live feed: {note}")
            else:
                e.set_footer(text="No futures cache yet. Start the futures ingest on CLX.")

    return e
