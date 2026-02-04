from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal


Strictness = Literal["off", "shadow", "on"]


class Bucket(str, Enum):
    GREETING = "greeting"
    HELP = "help"
    MARKET_CONTEXT = "market_context"
    FUTURES = "futures"
    TRADE_ENTRY = "trade_entry"
    TRADE_EXIT = "trade_exit"
    SIZING = "sizing"
    PREDICTION = "prediction"
    EARNINGS = "earnings"
    NEWS = "news"
    OTHER = "other"


class Action(str, Enum):
    PASS_THROUGH = "pass_through"  # let existing router handle (deterministic qa / coach)
    REPLY_TEMPLATE = "reply_template"  # short deterministic moderator response


class TemplateId(str, Enum):
    GREETING = "greeting"
    HELP = "help"
    MARKET_CONTEXT = "market_context"
    FUTURES = "futures"
    NO_ENTRIES = "no_entries"
    NO_SIZING = "no_sizing"
    NO_PREDICTIONS = "no_predictions"
    NO_EXITS = "no_exits"


@dataclass(frozen=True)
class FuturesSignal:
    """Minimal deterministic futures context for policy gating."""

    state: Literal["OK", "AGING", "STALE", "MISSING"]
    trend: str | None = None
    mode: str | None = None
    divergence_level: str | None = None  # aligned|mixed|divergent


@dataclass(frozen=True)
class ModeratorEnv:
    primary_moderator: bool
    owner_available: bool
    strictness: Strictness


@dataclass(frozen=True)
class ModeratorDecision:
    action: Action
    bucket: Bucket
    template_id: TemplateId | None
    reason: str


_GREETING_RE = re.compile(r"\b(hi|hello|hey|gm|good morning|good afternoon|good evening)\b", re.I)
_HELP_RE = re.compile(r"\b(help|commands|what can you do|what do you do)\b", re.I)

# Entries/sizing/prediction phrases.
_ENTRY_RE = re.compile(
    r"\b(entry|entries|enter|buy calls|buy puts|sell calls|sell puts|go long|go short|0dte|0 dte|scalp|send it|which strike|what strike|what exp|expiration|expiry|open a position)\b",
    re.I,
)
_EXIT_RE = re.compile(r"\b(exit|take profit|tp|stop|stoploss|stop loss|close the trade|cut it)\b", re.I)
_SIZING_RE = re.compile(r"\b(size|sizing|contracts|how many|position size|risk \%|risk percent|all in)\b", re.I)
_PRED_RE = re.compile(r"\b(will|gonna|going to)\b.*\b(up|down|pump|dump|rip|tank)\b|\bprice target\b|\bhow high\b|\bhow low\b", re.I)

_FUTURES_RE = re.compile(r"\b(futures|es\b|nq\b)\b", re.I)
_EARNINGS_RE = re.compile(r"\b(earnings|er|report|iv crush)\b", re.I)
_NEWS_RE = re.compile(r"\b(news|headline|what happened|why is)\b", re.I)
_MARKET_CTX_RE = re.compile(r"\b(market|spy|qqq|vix|open|today|what's the market doing|what is the market doing)\b", re.I)


def classify_bucket(text: str) -> Bucket:
    t = str(text or "").strip()
    if not t:
        return Bucket.OTHER

    tl = t.lower()
    if _GREETING_RE.search(tl):
        return Bucket.GREETING
    if _HELP_RE.search(tl):
        return Bucket.HELP

    # Deterministic, safety-first moderation buckets.
    if _SIZING_RE.search(tl):
        return Bucket.SIZING
    if _ENTRY_RE.search(tl):
        return Bucket.TRADE_ENTRY
    if _EXIT_RE.search(tl):
        return Bucket.TRADE_EXIT
    if _PRED_RE.search(tl):
        return Bucket.PREDICTION

    # Context buckets (normally ok to pass through).
    if _EARNINGS_RE.search(tl):
        return Bucket.EARNINGS
    if _NEWS_RE.search(tl):
        return Bucket.NEWS
    if _FUTURES_RE.search(tl):
        return Bucket.FUTURES
    if _MARKET_CTX_RE.search(tl):
        return Bucket.MARKET_CONTEXT

    return Bucket.OTHER


def decide_policy(
    *,
    user_text: str,
    env: ModeratorEnv,
    futures: FuturesSignal | None = None,
) -> ModeratorDecision:
    """Pure decision function.

    - When strictness is off, always pass through.
    - In shadow mode, caller should log the decision but still pass through.
    - In on mode, block entries/sizing/predictions/exits with fixed templates.
    """

    bucket = classify_bucket(user_text)

    if not env.primary_moderator or env.strictness == "off":
        return ModeratorDecision(Action.PASS_THROUGH, bucket, None, "disabled")

    # If futures are stale/missing and user asks for high-risk action, stand down.
    fut_state = (futures.state if futures is not None else None)

    if bucket == Bucket.TRADE_ENTRY:
        return ModeratorDecision(Action.REPLY_TEMPLATE, bucket, TemplateId.NO_ENTRIES, f"no_entries fut={fut_state}")
    if bucket == Bucket.SIZING:
        return ModeratorDecision(Action.REPLY_TEMPLATE, bucket, TemplateId.NO_SIZING, f"no_sizing fut={fut_state}")
    if bucket == Bucket.PREDICTION:
        return ModeratorDecision(Action.REPLY_TEMPLATE, bucket, TemplateId.NO_PREDICTIONS, f"no_predictions fut={fut_state}")
    if bucket == Bucket.TRADE_EXIT:
        return ModeratorDecision(Action.REPLY_TEMPLATE, bucket, TemplateId.NO_EXITS, f"no_exits fut={fut_state}")

    # Everything else: do not interfere (lets deterministic QA or coach handle it).
    return ModeratorDecision(Action.PASS_THROUGH, bucket, None, "pass")
