"""Discord channel purpose router (single source of truth).

This enforces TNT's channel-aware behavior contract:
- Only ASK_TNT is "full power" (public Q&A + candidates when APPROVED).
- Other channels are restricted (silence/redirect/ack-only/review-only).

Env vars (canonical):
- ASK_TNT_CHANNEL_ID
- ALERTS_CHANNEL_ID
- CALENDAR_EARNINGS_CHANNEL_ID
- AI_TRADE_JOURNAL_CHANNEL_ID
- PAPER_DESK_TRADES_CHANNEL_ID
- PAPER_DESK_DISCUSSION_CHANNEL_ID
- ANNOUNCEMENTS_CHANNEL_ID
- HOW_TO_USE_TNT_CHANNEL_ID
- TNT_CANARY_CHANNEL_ID

Optional enable switch:
- TNT_CHANNEL_ROUTER_ENABLED=1

This module is intentionally dependency-light so it can be imported by both
cli.discord_bot and delivery.discord_bot.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Mapping


REDIRECT_TEXT_VERBATIM = "For current market context or candidates, please ask in #ask-tnt."


def _normalize_channel_name(name: str | None) -> str:
    t = str(name or "").strip().lower()
    if t.startswith("#"):
        t = t[1:]
    return t


def ask_tnt_channel_id() -> int:
    return _env_int("ASK_TNT_CHANNEL_ID", 0)


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return default


def channel_purposes() -> Dict[int, str]:
    """Return channel-id -> purpose mapping (0/invalid ids omitted)."""

    mapping = {
        _env_int("ASK_TNT_CHANNEL_ID", 0): "ASK_TNT",
        _env_int("ALERTS_CHANNEL_ID", 0): "ALERTS",
        _env_int("CALENDAR_EARNINGS_CHANNEL_ID", 0): "CALENDAR",
        _env_int("AI_TRADE_JOURNAL_CHANNEL_ID", 0): "JOURNAL",
        _env_int("PAPER_DESK_TRADES_CHANNEL_ID", 0): "PAPER_TRADES",
        _env_int("PAPER_DESK_DISCUSSION_CHANNEL_ID", 0): "PAPER_DISCUSSION",
        _env_int("ANNOUNCEMENTS_CHANNEL_ID", 0): "ANNOUNCEMENTS",
        _env_int("HOW_TO_USE_TNT_CHANNEL_ID", 0): "HOW_TO_USE",
        _env_int("TNT_CANARY_CHANNEL_ID", 0): "CANARY",
    }
    return {k: v for k, v in mapping.items() if isinstance(k, int) and k > 0}


def router_enabled() -> bool:
    """Enable routing only when configured, to avoid surprising legacy setups."""

    if os.getenv("TNT_CHANNEL_ROUTER_ENABLED", "0") == "1":
        return True
    # Auto-enable if operator supplied any channel ids.
    return bool(channel_purposes())


def purpose_for_channel_id(channel_id: int) -> str:
    return channel_purposes().get(int(channel_id or 0), "OTHER")


SILENT_PURPOSES = {"ANNOUNCEMENTS", "HOW_TO_USE", "CANARY", "ALERTS", "CALENDAR"}


def message_is_trade_log(text: str) -> bool:
    """Heuristic for paper trade log lines (ack-only channel)."""

    t = str(text or "").strip().lower()
    if not t:
        return False

    # Common broker-style verbs.
    if re.search(r"\b(bto|stc|sto|btc)\b", t):
        return True

    # Typical log fields.
    hits = 0
    for kw in (
        "qty",
        "contracts",
        "fill",
        "filled",
        "entry",
        "exit",
        "pnl",
        "profit",
        "loss",
        "avg",
        "credit",
        "debit",
    ):
        if kw in t:
            hits += 1
    return hits >= 2


def question_is_forward_looking(text: str) -> bool:
    """Heuristic: forward-looking / actionable questions."""

    t = str(text or "").strip().lower()
    if not t:
        return False

    return bool(
        re.search(
            r"\b(should i|what should i|do i|is it time to|enter|buy|sell|open|close|take|add|trim|"
            r"recommend|candidate|signal|entry|calls?|puts?|0dte|dte|today|right now|next)\b",
            t,
        )
    )


@dataclass(frozen=True)
class RouteDecision:
    action: str
    reply_text: str | None = None


def decide_route(*, channel_id: int, content: str, channel_name: str | None = None) -> RouteDecision:
    """Return the router decision for a message.

    Actions:
    - allow_full: only ASK_TNT
    - silent: ignore
    - redirect: reply with redirect text
    - ack_trade_log: short ack for trade log channel
    - review_only: reply with review prompt (no market context)
    """

    purpose = purpose_for_channel_id(channel_id)

    # Safety: if router is enabled but ASK_TNT_CHANNEL_ID isn't configured,
    # allow the canonical channel name to act as a fallback so #ask-tnt doesn't lock out.
    if purpose == "OTHER":
        if ask_tnt_channel_id() <= 0 and _normalize_channel_name(channel_name) == "ask-tnt":
            purpose = "ASK_TNT"

    if purpose == "ASK_TNT":
        return RouteDecision(action="allow_full")

    if purpose in SILENT_PURPOSES:
        return RouteDecision(action="silent")

    if purpose == "PAPER_TRADES":
        if message_is_trade_log(content):
            return RouteDecision(action="ack_trade_log", reply_text="✅ Logged.")
        return RouteDecision(action="redirect", reply_text=REDIRECT_TEXT_VERBATIM)

    if purpose in {"PAPER_DISCUSSION", "JOURNAL"}:
        if question_is_forward_looking(content):
            return RouteDecision(action="redirect", reply_text=REDIRECT_TEXT_VERBATIM)

        prompt = (
            "Post-trade review only (no candidates here).\n"
            "Share: thesis, entry/exit, risk plan, what you followed, what you violated, and the lesson.\n"
            + REDIRECT_TEXT_VERBATIM
        )
        return RouteDecision(action="review_only", reply_text=prompt)

    return RouteDecision(action="redirect", reply_text=REDIRECT_TEXT_VERBATIM)
