from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class LockState(str, Enum):
    UNLOCKED = "UNLOCKED"
    SOFT = "SOFT"
    HARD = "HARD"


class TemplateId(str, Enum):
    DECISION_ZONE = "DECISION_ZONE"
    NEAR_WALL = "NEAR_WALL"
    POST_STOPOUT = "POST_STOPOUT"
    CLEAN_BREAKOUT = "CLEAN_BREAKOUT"
    DISCIPLINE_CHECK = "DISCIPLINE_CHECK"


@dataclass(frozen=True)
class ConciergeContext:
    ticker: str
    price: float

    # OI / structure summary (cheap inputs – can be derived from your cached OI snapshot summary)
    between_walls: bool = False
    near_wall: bool = False
    wall_side: Optional[str] = None  # "CALL" or "PUT"
    wall_strike: Optional[float] = None
    oi_shift: bool = False  # did OI unwind/rotate meaningfully?
    acceptance: bool = False  # accepted beyond key wall/level?

    # regime
    regime: str = "UNKNOWN"  # "COMPRESSION", "EXPANSION", "TREND", "CHOP", etc.

    # user / behavioral signals (optional)
    user_frustration: bool = False  # user said "stopped out", "wtf", etc.
    overactivity: bool = False  # many alerts / commands in short time
    late_day: bool = False  # near close / high decay window

    # lock
    decision_lock: LockState = LockState.SOFT


def pick_template(ctx: ConciergeContext) -> TemplateId:
    # Priority matters: behavioral saves first, then structure, then opportunity
    if ctx.user_frustration:
        return TemplateId.POST_STOPOUT
    if ctx.overactivity or (ctx.late_day and ctx.regime in {"COMPRESSION", "CHOP"}):
        return TemplateId.DISCIPLINE_CHECK
    if ctx.regime in {"COMPRESSION", "CHOP"} and ctx.between_walls:
        return TemplateId.DECISION_ZONE
    if ctx.near_wall and not ctx.oi_shift:
        return TemplateId.NEAR_WALL
    if ctx.oi_shift and ctx.acceptance and ctx.regime in {"EXPANSION", "TREND"}:
        return TemplateId.CLEAN_BREAKOUT

    # default: decision zone framing is safest
    return TemplateId.DECISION_ZONE


def render_message(tid: TemplateId, ctx: ConciergeContext) -> str:
    t = ctx.ticker.upper()
    lock = ctx.decision_lock.value

    if tid == TemplateId.DECISION_ZONE:
        return (
            f"👀 **{t} Structure Check**\n"
            f"Price is trading between major OI walls with no dominant side.\n\n"
            f"This is a decision zone, not confirmation.\n\n"
            f"👉 Type `/oi {t}`\n"
            f"**Interpretation:**\n"
            f"• OI unwind = continuation\n"
            f"• No OI shift = stop-run risk both ways\n\n"
            f"🔒 **Decision Lock: {lock}**"
        )

    if tid == TemplateId.NEAR_WALL:
        side = (ctx.wall_side or "CALL/PUT").upper()
        strike = f"{ctx.wall_strike:.2f}" if ctx.wall_strike is not None else "key strike"
        return (
            f"👀 **{t} Watchlist Insight**\n"
            f"Price is approaching the largest **{side} wall** near **{strike}**, but positioning hasn’t shifted yet.\n\n"
            f"This level matters only if structure resolves.\n\n"
            f"👉 Type `/oi {t}`\n"
            f"**Watch for:**\n"
            f"• Wall unwind → continuation\n"
            f"• Rejection → fade / chop risk"
        )

    if tid == TemplateId.POST_STOPOUT:
        return (
            f"⚠️ **Structure Note**\n"
            f"This is classic compression chop.\n\n"
            f"• Price moves\n"
            f"• Positioning doesn’t resolve\n"
            f"• Stops get harvested both ways\n\n"
            f"👉 Type `/regime {t}`\n"
            f"**Action:** Stand down until expansion + OI confirmation."
        )

    if tid == TemplateId.CLEAN_BREAKOUT:
        side = (ctx.wall_side or "CALL/PUT").upper()
        strike = f"{ctx.wall_strike:.2f}" if ctx.wall_strike is not None else "key strike"
        return (
            f"🟢 **{t} Structure Break**\n"
            f"Price accepted beyond the **{side} wall** near **{strike}** with OI unwinding.\n\n"
            f"This is real structure, not just price.\n\n"
            f"👉 Type `/oi {t}`\n"
            f"Bias remains valid while OI continues to unwind."
        )

    if tid == TemplateId.DISCIPLINE_CHECK:
        return (
            f"🧠 **Trade Discipline Check**\n"
            f"Multiple setups with no resolution = overtrading risk.\n\n"
            f"Speed is high. Edge is low.\n\n"
            f"🔒 **Decision Lock: {lock}**\n"
            f"Wait for structure to choose."
        )

    # Fallback
    return render_message(TemplateId.DECISION_ZONE, ctx)


def concierge_reply(ctx: ConciergeContext) -> Tuple[TemplateId, str]:
    tid = pick_template(ctx)
    msg = render_message(tid, ctx)
    return tid, msg
