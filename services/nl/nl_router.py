from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from delivery import qa_router as delivery_qa


@dataclass(frozen=True)
class NLRouteDecision:
    """Stable contract for NL routing (golden-tested).

    route: high-level bucket (alerts/earnings/news/coach/ignore)
    handler: the deterministic handling path
    symbol: optional resolved ticker for symbol-specific paths
    """

    route: str
    handler: str
    symbol: Optional[str] = None


_RE_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _RE_WS.sub(" ", str(text or "").strip())


def _norm_lower(text: str) -> str:
    return _norm(text).lower()


def _looks_like_single_token_ticker(tok: str) -> bool:
    t = str(tok or "").strip().upper().strip("$")
    if not t:
        return False
    # Single-token tickers can have dot/dash (BRK.B, RDS-A) but we keep this conservative.
    if not re.fullmatch(r"[A-Z][A-Z\.-]{0,7}", t):
        return False

    # Obvious non-tickers (must not steal intents like "news" or "earnings").
    stop = {
        "HELP",
        "NEWS",
        "EARNINGS",
        "ER",
        "WHO",
        "WHAT",
        "WHICH",
        "WHEN",
        "WHERE",
        "WHY",
        "HOW",
        "THIS",
        "TODAY",
        "TOMORROW",
        "NEXT",
        "ALERT",
        "NOTIFY",
        "SET",
    }
    if t in stop:
        return False

    # Avoid treating company-name words as tickers; prefer alias layer.
    company_words = {"APPLE", "GOOGLE", "ALPHABET", "MICROSOFT", "TESLA", "NVIDIA", "AMAZON", "META", "FACEBOOK", "NETFLIX"}
    if t in company_words:
        return False

    return True


def _looks_like_symbol_token(tok: str) -> bool:
    t = (tok or "").strip().upper().strip("$")
    if not t:
        return False
    if not re.fullmatch(r"[A-Z]{1,6}", t):
        return False

    # Never treat these as symbols.
    stop = {
        "A",
        "AN",
        "THE",
        "AND",
        "OR",
        "TO",
        "ON",
        "IN",
        "OF",
        "FOR",
        "IS",
        "ARE",
        "WAS",
        "WERE",
        "BE",
        "BEEN",
        "DO",
        "DOES",
        "DID",
        "HAVE",
        "HAS",
        "WHO",
        "WHICH",
        "WHAT",
        "WHY",
        "WHEN",
        "WHERE",
        "HOW",
        "ALERT",
        "NOTIFY",
        "SET",
        "MAKE",
        "CREATE",
        "NEWS",
        "HEADLINE",
        "HEADLINES",
        "EARNINGS",
        "DATE",
        "REPORT",
        "NEXT",
        "THIS",
        "TODAY",
        "TOMORROW",
        "WEEK",
        "ER",
    }

    # Company-name tokens should resolve via alias mapping, not be treated as tickers.
    company_words = {"APPLE", "GOOGLE", "ALPHABET", "MICROSOFT", "TESLA", "NVIDIA", "AMAZON", "META", "FACEBOOK", "NETFLIX"}
    if t in stop or t in company_words:
        return False
    return True


def _extract_ticker(text: str) -> str | None:
    raw = str(text or "")
    if not raw.strip():
        return None

    # Prefer explicit $TICKER mentions.
    m = re.search(r"\$([A-Za-z]{1,6})\b", raw)
    if m:
        t = m.group(1).upper()
        if _looks_like_symbol_token(t):
            return t

    # Common uppercase token.
    tokens = re.findall(r"\b([A-Za-z]{1,6})\b", raw)
    for tok in tokens:
        t = tok.upper()
        if _looks_like_symbol_token(t):
            return t

    # Fallback: first token if it looks like a symbol.
    first = (raw.split()[0] if raw.split() else "").strip().lstrip("$").strip(" ,.;:()[]{}<>\"'\n\t")
    if _looks_like_symbol_token(first):
        return first.upper()

    return None


def _company_alias_symbol(text_lower: str) -> str | None:
    t = str(text_lower or "")
    aliases: dict[str, str] = {
        "apple": "AAPL",
        "microsoft": "MSFT",
        "tesla": "TSLA",
        "nvidia": "NVDA",
        "amazon": "AMZN",
        "meta": "META",
        "facebook": "META",
        "google": "GOOGL",
        "alphabet": "GOOGL",
        "netflix": "NFLX",
    }
    for name, sym in aliases.items():
        if re.search(rf"\b{re.escape(name)}\b", t):
            return sym
    return None


def _strip_alert_prefixes(raw: str) -> str:
    t = str(raw or "").strip()
    tl = _norm_lower(t)
    for pref in ("alert:", "alert ", "notify:", "notify "):
        if tl.startswith(pref):
            return t[len(pref) :].strip()
    # Common natural language lead-in.
    if tl.startswith("set an alert for "):
        return t[len("set an alert for ") :].strip()
    if tl.startswith("create an alert for "):
        return t[len("create an alert for ") :].strip()
    if tl.startswith("make an alert for "):
        return t[len("make an alert for ") :].strip()
    return t


def looks_like_alert_request(text: str) -> bool:
    """Conservative alert intent detector.

    Mirrors the production heuristics in cli.discord_bot, but lives in a pure
    module so golden tests can lock it down.
    """

    raw = _norm(text)
    if not raw:
        return False

    tl = _norm_lower(raw)

    # Deterministic prefix: always treat as alert intent.
    if tl.startswith("alert:") or tl.startswith("alert ") or tl.startswith("notify:") or tl.startswith("notify "):
        return True

    explicit = (
        "alert me when" in tl
        or "notify me when" in tl
        or "set an alert" in tl
        or "create an alert" in tl
        or "make an alert" in tl
    )

    op_phrase = any(
        p in tl
        for p in (
            "crosses above",
            "cross above",
            "crosses below",
            "cross below",
            "breaks above",
            "break above",
            "breaks below",
            "break below",
            "crossing above",
            "crossing below",
            "breaking above",
            "breaking below",
        )
    )

    if explicit:
        return True

    if not op_phrase:
        return False

    # If we see a cross/break operator, require a symbol to reduce false positives.
    sym = _extract_ticker(raw)
    return bool(sym)


def route_text(*, text: str) -> NLRouteDecision:
    """Return a deterministic routing decision for a user text message."""

    raw = _norm(text)
    if not raw:
        return NLRouteDecision(route="ignore", handler="empty", symbol=None)

    # Ignore messages that look like commands.
    if raw.startswith(("/", "!", ".")):
        return NLRouteDecision(route="ignore", handler="command", symbol=None)

    # Hard route: a single ticker token should always behave like "show me this ticker".
    # This must win over general market/context/coach fallbacks.
    if " " not in raw and _looks_like_single_token_ticker(raw):
        sym = str(raw).strip().upper().strip("$")
        return NLRouteDecision(route="symbol", handler="symbol_default", symbol=sym)

    if looks_like_alert_request(raw):
        cleaned = _strip_alert_prefixes(raw)
        sym = _extract_ticker(cleaned)
        if sym is None:
            sym = _company_alias_symbol(_norm_lower(cleaned))
        return NLRouteDecision(route="alerts", handler="alert_create", symbol=sym)

    q = delivery_qa.route_question(text=raw)
    if q.kind == "earnings":
        # Calendar/window requests vs single-symbol requests.
        if delivery_qa.looks_like_earnings_calendar_query(text=raw):
            return NLRouteDecision(route="earnings", handler="earnings_calendar", symbol=None)
        sym = _extract_ticker(raw)
        if sym is None:
            sym = _company_alias_symbol(_norm_lower(raw))
        return NLRouteDecision(route="earnings", handler="earnings_symbol", symbol=sym)

    if q.kind == "news":
        sym = _extract_ticker(raw)
        if sym is None:
            sym = _company_alias_symbol(_norm_lower(raw))
        return NLRouteDecision(route="news", handler="news_symbol", symbol=sym)

    return NLRouteDecision(route="coach", handler="coach", symbol=None)
