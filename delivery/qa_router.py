from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


QAKind = Literal[
    "earnings",
    "news",
    "price_range",
    "structure_timing",
    "policy",
    "market_context",
    "general",
]


@dataclass(frozen=True)
class QARoute:
    kind: QAKind
    reason: str
    sub_intent: str | None = None


_RE_WS = re.compile(r"\s+")
_RE_EARNINGS_TYPO = re.compile(r"\b(earnigns|ernings|earings|earnngs|earningz)\b")


def _normalize_earnings_typos(text: str) -> str:
    t = str(text or "")
    if not t:
        return t
    # Fix high-frequency mobile typos.
    return _RE_EARNINGS_TYPO.sub("earnings", t)


def _norm(text: str) -> str:
    t = _RE_WS.sub(" ", str(text or "").strip().lower())
    return _normalize_earnings_typos(t)


def route_question(*, text: str) -> QARoute:
    """Deterministic routing for free-form Q&A.

    Priority:
    1) earnings
    2) news
    3) market_context
    4) general

    This intentionally avoids probabilistic LLM intent guessing.
    """

    t = _norm(text)
    if not t:
        return QARoute(kind="general", reason="empty")

    # Phase A: explicit paid-intent map (deterministic, pre-dispatch)
    # Policy / trust questions
    if (
        "why didn" in t and "alert" in t
        or "why no alert" in t
        or "why no alerts" in t
        or "didnt alert" in t
        or "didn't alert" in t
    ):
        return QARoute(kind="policy", reason="keyword", sub_intent="why_no_alert")
    if "what does tnt think" in t or "what does tnt say" in t:
        return QARoute(kind="policy", reason="keyword", sub_intent="what_think")

    # Structure / timing questions
    if (
        ("should i trade" in t and "now" in t)
        or ("is now" in t and "good time" in t)
        or ("trade" in t and "right now" in t)
    ):
        return QARoute(kind="structure_timing", reason="keyword", sub_intent="trade_now")
    if "safest way" in t and "trade" in t:
        return QARoute(kind="structure_timing", reason="keyword", sub_intent="safest_way")

    # Price vs earnings range questions
    if (
        "earnings range" in t
        or "vs earnings range" in t
        or "earnings bounds" in t
        or "price vs earnings" in t
        or ("price" in t and "bounds" in t and "earnings" in t)
    ):
        return QARoute(kind="price_range", reason="keyword", sub_intent="price_vs_range")
    if "stretched" in t and "earnings" in t:
        return QARoute(kind="price_range", reason="keyword", sub_intent="stretched")

    # Earnings intent + sub-intents
    if (
        "how does" in t and "react" in t and "earnings" in t
        or "earnings reaction" in t
        or "earnings history" in t
    ):
        return QARoute(kind="earnings", reason="keyword", sub_intent="earnings_reaction")
    if (
        ("earnings" in t and "risky" in t)
        or "earnings risk" in t
        or ("risk of" in t and "earnings" in t)
    ):
        return QARoute(kind="earnings", reason="keyword", sub_intent="earnings_risk")
    if "should i trade" in t and "earnings" in t:
        return QARoute(kind="earnings", reason="keyword", sub_intent="earnings_should_trade")

    earnings_hits = (
        "earnings" in t
        or re.search(r"\ber\b", t) is not None
        or "eps" in t
        or "guidance" in t
        or "results" in t
        or "report" in t
        or "quarter" in t
        or "earnings date" in t
        or "report date" in t
        or "next report" in t
        or "quarterly results" in t
        or "conference call" in t
    )
    if earnings_hits:
        return QARoute(kind="earnings", reason="keyword")

    # News intent
    news_hits = (
        "news" in t
        or "headline" in t
        or "headlines" in t
        or "breaking" in t
        or "press release" in t
        or "what happened" in t
        or "why did" in t
        or "why is" in t
        or "downgrade" in t
        or "upgrade" in t
    )
    if news_hits:
        return QARoute(kind="news", reason="keyword")

    # Market context intent
    ctx_hits = (
        "market context" in t
        or "futures" in t
        or "vix" in t
        or "macro" in t
        or "breadth" in t
        or "regime" in t
        or "rates" in t
        or "cpi" in t
        or "fomc" in t
        or "pce" in t
        or "jobs report" in t
    )
    if ctx_hits:
        return QARoute(kind="market_context", reason="keyword")

    return QARoute(kind="general", reason="default")


def looks_like_earnings_calendar_query(*, text: str) -> bool:
    """Return True for calendar/window earnings questions.

    Examples:
    - "Who has earnings next week?"
    - "Earnings this week"
    - "Which companies report tomorrow?"

    This is intentionally conservative: only triggers when the user is clearly
    asking for a time window (not a symbol-specific lookup).
    """

    t = _norm(text)
    if not t:
        return False
    if "earnings" not in t and "earnings date" not in t and "report date" not in t:
        return False

    # If the user is asking about a *specific* company/ticker (not a list/window),
    # do NOT treat this as a calendar query.
    # Examples:
    # - "Apple earnings next week"
    # - "Does Apple have earnings next week?"
    # - "When is TSLA earnings?"
    try:
        # "does/when/is <entity> (have) earnings ..."
        m_entity = re.search(r"\b(does|do|is|when)\s+([a-z0-9&\.-]{2,})\s+(?:have\s+)?earnings\b", t)
        if m_entity:
            ent = str(m_entity.group(2) or "").strip()
            if ent and ent not in {
                "anyone",
                "someone",
                "anybody",
                "somebody",
                "who",
                "which",
                "what",
                "stocks",
                "companies",
                "company",
                "stock",
            }:
                return False

        # "<entity> earnings ..." at the start (very common mobile shorthand).
        m_prefix = re.search(r"^([a-z][a-z0-9&\.-]{1,24})\s+earnings\b", t)
        if m_prefix:
            ent = str(m_prefix.group(1) or "").strip()
            if ent and ent not in {
                "who",
                "which",
                "what",
                "upcoming",
                "next",
                "this",
                "today",
                "tomorrow",
            }:
                return False

        # "<entity>, earnings ..." at the start (mobile shorthand with comma).
        m_prefix_comma = re.search(r"^([a-z][a-z0-9&\.-]{1,24})\s*,\s*earnings\b", t)
        if m_prefix_comma:
            ent = str(m_prefix_comma.group(1) or "").strip()
            if ent and ent not in {
                "who",
                "which",
                "what",
                "upcoming",
                "next",
                "this",
                "today",
                "tomorrow",
            }:
                return False
    except Exception:
        pass

    # High-signal phrases.
    if "who has earnings" in t or "which stocks have earnings" in t or "which companies" in t:
        return True
    if "upcoming earnings" in t or "earnings calendar" in t:
        return True

    # Window keywords + earnings.
    window_hits = (
        "next week" in t
        or "this week" in t
        or "today" in t
        or "tomorrow" in t
        or "next " in t  # e.g., next 7 days
        or "in " in t  # e.g., in 7 days
        or "upcoming" in t
    )
    if not window_hits:
        return False

    # Require that this reads like a list/window query, not a symbol query.
    # (Avoid triggering on short inputs like "INTC earnings".)
    if re.search(r"\bearnings\s+(today|tomorrow|this\s+week|next\s+week|upcoming)\b", t):
        return True
    if re.search(r"\b(who|which|what)\b.*\bearnings\b", t):
        return True
    if re.search(r"\bearnings\b.*\b(next|this)\b", t):
        return True

    return False
