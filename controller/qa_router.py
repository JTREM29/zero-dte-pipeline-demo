from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional


@dataclass
class QAResult:
    title: str
    body: str
    confidence: str = "HIGH"
    sources: str = "internal"
    as_of: Optional[str] = None


_PAT_EARNINGS = re.compile(r"\b(earnings|er)\b", re.I)
_PAT_NEWS = re.compile(r"\b(news|headline|catalyst)\b", re.I)
_PAT_MOVE = re.compile(r"\b(what happened|why (did|didn't|didnt)|shot up|dumped|spike|rip|selloff)\b", re.I)
_PAT_DIVERGENCE = re.compile(r"\b(divergence|rsi\s+divergence|bearish\s+divergence|bullish\s+divergence)\b", re.I)
_PAT_TF = re.compile(r"\b(1m|3m|5m|15m|30m|1h|2h|4h|1d)\b", re.I)
_PAT_DOLLAR_SYM = re.compile(r"\$([A-Za-z]{1,6})\b")
_PAT_SYM_TOKEN = re.compile(r"\b([A-Z]{1,6})\b")


def classify_question(text: str) -> str:
    t = text.strip().lower()
    if _PAT_EARNINGS.search(t):
        return "earnings"
    if _PAT_NEWS.search(t) or _PAT_MOVE.search(t):
        return "news_explain"
    if _PAT_DIVERGENCE.search(t):
        return "divergence"
    return "context"


def answer_earnings(now_et: str, earnings_provider) -> QAResult:
    if earnings_provider is None:
        return QAResult(
            title="Earnings this week",
            body="Earnings is not configured on this bot yet.",
            sources="none",
            as_of=now_et,
        )

    items = earnings_provider.get_earnings_week(now_et)  # must return list[dict]
    if not items:
        return QAResult(
            title="Earnings this week",
            body="No earnings found in the configured earnings feed for this week.",
            sources="earnings",
            as_of=now_et,
        )

    lines = []
    for it in items[:25]:
        sym = it.get("ticker", "?")
        dt = it.get("date", "")
        when = it.get("when", "")
        name = it.get("name", "")
        line = f"- **{sym}**" + (f" — {name}" if name else "") + (f" ({dt} {when})" if (dt or when) else "")
        lines.append(line)

    return QAResult(
        title="Earnings this week",
        body="\n".join(lines),
        sources="earnings",
        as_of=now_et,
    )


def answer_news_explain(now_et: str, context_provider, news_provider) -> QAResult:
    impulse = context_provider.get_market_impulse(now_et) if context_provider else None
    headlines = news_provider.get_recent_headlines(now_et, minutes=10) if news_provider else []

    if impulse:
        what_moved = f"ES {impulse['es_move_pct']:+.2f}% | NQ {impulse['nq_move_pct']:+.2f}% over ~{int(impulse['window_s'] / 60)}m"
    else:
        what_moved = "Move data unavailable (context provider not configured)."

    if headlines:
        h = "\n".join([f"- {x['ts']}: {x['title']}" for x in headlines[:3]])
        catalyst = f"Top headlines (last 10m):\n{h}"
        src = "futures,news"
    else:
        catalyst = "No matching catalyst found in the configured news feed in the last 10 minutes."
        src = "futures"

    body = "\n".join(
        [
            f"**What moved:** {what_moved}",
            f"**Catalyst:** {catalyst}",
            "",
            "If TNT still says DO_NOTHING, this explains *why* (no clean edge / no confirmed catalyst yet).",
        ]
    )

    return QAResult(
        title="What just happened?",
        body=body,
        sources=src,
        as_of=now_et,
    )


def _parse_symbol_and_timeframe(text: str) -> tuple[str, str]:
    t = (text or "").strip()

    tf = "5m"
    m_tf = _PAT_TF.search(t)
    if m_tf:
        tf = str(m_tf.group(1)).lower()

    # Prefer explicit $SYMBOL mentions.
    m_sym = _PAT_DOLLAR_SYM.search(t)
    if m_sym:
        sym = str(m_sym.group(1)).upper()
        return sym, tf

    # Otherwise, choose a reasonable uppercase token, excluding common non-tickers.
    stop = {
        "RSI",
        "DTE",
        "IV",
        "OI",
        "VWAP",
        "EMA",
        "SMA",
        "MACD",
        "CALL",
        "PUT",
        "ITM",
        "OTM",
        "ATM",
        "SPREAD",
        "ODTE",
        "TNT",
    }
    tokens = [m.group(1) for m in _PAT_SYM_TOKEN.finditer(t.upper())]
    preferred = ["SPY", "SPX", "QQQ", "IWM", "ES", "NQ", "NDX", "VIX"]
    for p in preferred:
        if p in tokens:
            return p, tf
    for tok in tokens:
        if tok in stop:
            continue
        if 1 <= len(tok) <= 6:
            return tok, tf
    return "SPY", tf


def answer_divergence(now_et: str, text: str, bars_provider=None) -> QAResult:
    """Deterministic divergence answer.

    If bars_provider is None, this attempts to construct a provider from env.
    """

    sym, tf = _parse_symbol_and_timeframe(text)

    provider = bars_provider
    if provider is None:
        try:
            from technicals.bars_provider_factory import build_bars_provider_from_env

            provider = build_bars_provider_from_env()
        except Exception:
            provider = None

    if provider is None:
        return QAResult(
            title=f"{sym} — Divergence",
            body="Bars provider not configured (set POLYGON_API_KEY / MASSIVE_API_KEY).",
            sources="none",
            as_of=now_et,
            confidence="LOW",
        )

    try:
        ohlc = provider.get_bars(sym, tf, lookback_bars=260)
    except Exception as exc:
        return QAResult(
            title=f"RSI divergence ({sym} {tf})",
            body=f"Unable to fetch bars right now ({type(exc).__name__}).",
            sources="bars",
            as_of=now_et,
            confidence="LOW",
        )

    if ohlc is None or len(ohlc) < 80:
        return QAResult(
            title=f"RSI divergence ({sym} {tf})",
            body="Not enough recent bars to evaluate divergence.",
            sources="bars",
            as_of=now_et,
            confidence="LOW",
        )

    from technicals.divergence import detect_rsi_divergence, divergence_trade_plan, format_divergence_market_speak

    res = detect_rsi_divergence(
        ohlc,
        timeframe=tf,
        rsi_period=14,
        left_right=3,
        lookback_bars=180,
        max_bar_gap=6,
    )

    extra = ""
    try:
        if bool(getattr(provider, "last_cache_hit", False)):
            ttl_s = int(getattr(provider, "last_ttl_s", 0) or 0)
            if ttl_s > 0:
                extra = f"\n_Bars: cache HIT ({ttl_s}s TTL)_"
            else:
                extra = "\n_Bars: cache HIT_"
    except Exception:
        extra = ""

    body = "\n".join(
        [
            format_divergence_market_speak(res),
            "",
            divergence_trade_plan(res, symbol=sym, bars_df=ohlc) + (extra or ""),
        ]
    ).strip()

    conf = "HIGH" if res.kind != "none" else "MED"
    return QAResult(
        title=f"RSI divergence ({sym} {tf})",
        body=body,
        sources="bars,technicals",
        as_of=now_et,
        confidence=conf,
    )
