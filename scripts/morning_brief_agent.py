#!/usr/bin/env python
"""Generate an OpenAI-powered morning brief backed by live pipeline data."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp
import pandas as pd

from data.fetchers import (
    get_5m_bars,
    get_60d_bars,
    get_es_snapshot,
    get_market_profile,
    get_quote,
    get_vix_quote,
)
from zero_dte_pipeline.config import config
from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
from zero_dte_pipeline.openai_client import OpenAIClient, OpenAIError
from zero_dte_pipeline.reports.morning_report import MorningReport
from zero_dte_pipeline.reports.sentiment import build_agent_sentiments

_DEFAULT_SENTIMENT_TIMEOUT = config.get_int("MORNING_BRIEF_SENTIMENT_TIMEOUT_SECONDS", 150)


def _run_async(factory):
    try:
        return asyncio.run(factory())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(factory())
        finally:
            loop.close()


@dataclass
class MarketSnapshot:
    symbol: str
    as_of: dt.datetime

    regime: str
    regime_note: str
    direction: str
    direction_conf: float

    spot: float
    futures: Optional[float]
    intraday_change_pct: Optional[float]
    atr: Optional[float]
    rsi: Optional[float]
    macd_signal: Optional[str]

    support_level: Optional[float]
    resistance_level: Optional[float]
    vwap: Optional[float]

    vix: Optional[float]
    iv_rank: Optional[float]
    expected_move: Optional[float]

    breadth_summary: Optional[str]
    volume_flow_comment: Optional[str]

    news_headlines: List[str]
    news_sentiment: Optional[str]
    news_risk_comment: Optional[str]

    data_quality_note: Optional[str] = None


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _calc_intraday_pct(
    spot: Optional[float], daily: Optional[pd.DataFrame]
) -> Optional[float]:
    if spot is None or daily is None or daily.empty:
        return None
    if len(daily) < 2:
        return None
    prev_close = daily["close"].iloc[-2]
    if not prev_close:
        return None
    return _round((spot - prev_close) / prev_close * 100, 2)


def _calc_atr(daily: Optional[pd.DataFrame], period: int = 14) -> Optional[float]:
    if daily is None or len(daily) < period + 1:
        return None
    high = daily["high"]
    low = daily["low"]
    close = daily["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return _round(atr, 2)


def _calc_rsi(daily: Optional[pd.DataFrame], period: int = 14) -> Optional[float]:
    if daily is None or len(daily) < period + 1:
        return None
    close = daily["close"]
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / period, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / period, adjust=False).mean()
    denom = roll_down.iloc[-1]
    if denom == 0:
        return 100.0
    rs = roll_up.iloc[-1] / denom
    rsi = 100 - (100 / (1 + rs))
    return _round(rsi, 2)


def _calc_macd_signal(daily: Optional[pd.DataFrame]) -> Optional[str]:
    if daily is None or len(daily) < 35:
        return None
    close = daily["close"]
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    last_macd = macd.iloc[-1]
    last_signal = signal.iloc[-1]
    diff = last_macd - last_signal
    if diff > 0.15:
        return "bullish"
    if diff < -0.15:
        return "bearish"
    return "flat"


def _calc_support_resistance(
    daily: Optional[pd.DataFrame],
    atr: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    if daily is None or daily.empty:
        return (None, None)
    last_close = daily["close"].iloc[-1]
    if atr is None:
        return (_round(last_close * 0.99, 2), _round(last_close * 1.01, 2))
    return (_round(last_close - atr, 2), _round(last_close + atr, 2))


def _calc_vwap(intraday: Optional[pd.DataFrame]) -> Optional[float]:
    if intraday is None or intraday.empty:
        return None
    if "vwap" in intraday.columns and not intraday["vwap"].isna().all():
        return _round(intraday["vwap"].dropna().iloc[-1], 2)
    if "volume" not in intraday.columns:
        return None
    typical = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
    vol = intraday["volume"].fillna(0)
    denom = vol.sum()
    if denom == 0:
        return None
    vwap = (typical * vol).sum() / denom
    return _round(vwap, 2)


def _calc_expected_move(
    spot: Optional[float],
    atr: Optional[float],
    profile_details: Optional[Dict[str, Any]],
) -> Optional[float]:
    if spot and profile_details:
        vol = profile_details.get("volatility", {})
        realized = vol.get("realized")
        if realized:
            daily_move = spot * (realized / 100) / 16
            return _round(daily_move, 2)
    return atr


def build_market_snapshot(symbol: str = "SPX") -> MarketSnapshot:
    notes: List[str] = []

    try:
        profile = get_market_profile(symbol)
    except Exception as exc:  # pragma: no cover - defensive path
        notes.append(f"Market profile unavailable: {exc}")
        profile = None

    try:
        daily: Optional[pd.DataFrame] = get_60d_bars(symbol)
    except Exception as exc:  # pragma: no cover - defensive path
        notes.append(f"Daily bars unavailable: {exc}")
        daily = None

    try:
        intraday: Optional[pd.DataFrame] = get_5m_bars(symbol)
    except Exception as exc:  # pragma: no cover - defensive path
        notes.append(f"Intraday bars unavailable: {exc}")
        intraday = None

    try:
        quote = get_quote(symbol) or {}
    except Exception as exc:  # pragma: no cover - defensive path
        notes.append(f"Quote unavailable: {exc}")
        quote = {}

    try:
        vix_quote = get_vix_quote() or {}
    except Exception as exc:  # pragma: no cover - defensive path
        notes.append(f"VIX quote unavailable: {exc}")
        vix_quote = {}

    try:
        es_snapshot = get_es_snapshot() or {}
    except Exception as exc:  # pragma: no cover - defensive path
        notes.append(f"ES snapshot unavailable: {exc}")
        es_snapshot = {}

    try:
        polygon_news = _run_async(lambda: _fetch_polygon_news(symbol)) or []
    except Exception as exc:  # pragma: no cover - defensive path
        notes.append(f"Polygon news unavailable: {exc}")
        polygon_news = []

    spot = quote.get("last")
    if spot is None and daily is not None and not daily.empty:
        spot = float(daily["close"].iloc[-1])

    atr = _calc_atr(daily)
    rsi = _calc_rsi(daily)
    macd_signal = _calc_macd_signal(daily)
    support, resistance = _calc_support_resistance(daily, atr)
    vwap = _calc_vwap(intraday)
    intraday_pct = _calc_intraday_pct(spot, daily)
    expected_move = _calc_expected_move(spot, atr, profile.details if profile else None)

    iv_rank = profile.volatility_percentile if profile else None
    regime = profile.regime.value if profile else "unknown"
    direction = profile.direction_bias.value if profile else "neutral"
    direction_conf = profile.confidence if profile else 0.0
    regime_note = ""
    breadth_summary = None
    volume_flow_comment = None
    vix_value = vix_quote.get("last")

    if profile:
        detail = profile.details or {}
        vol = detail.get("volatility") or {}
        momentum = detail.get("momentum") or {}
        breadth = detail.get("breadth") or {}

        vix_value = vix_value or vol.get("vix_level")
        breadth_score = breadth.get("score")
        if breadth_score is not None:
            breadth_summary = f"Breadth score {breadth_score:.2f}"
        trend = momentum.get("trend")
        strength = momentum.get("strength")
        if trend:
            strength_note = f"strength {strength:.2f}" if strength is not None else ""
            volume_flow_comment = f"Momentum {trend} {strength_note}".strip()
        iv_rank = vol.get("percentile", iv_rank)
        regime_note = (
            f"{profile.condition.value} | vol%={profile.volatility_percentile:.1f}"
        )
    else:
        regime_note = "Profile unavailable"
        notes.append("Market profile missing; using defaults")

    futures_price = es_snapshot.get("last") or es_snapshot.get("price")
    futures = float(futures_price) if futures_price is not None else None

    news_items = _merge_news_entries(polygon_news)
    news_headlines = [item["headline"] for item in news_items]
    news_sentiment, news_risk_comment = _summarize_news(news_items)
    if not news_items:
        notes.append("No news items returned from Massive/Polygon")

    data_quality = ", ".join(notes) if notes else None
    spot_value = float(spot or 0.0)

    return MarketSnapshot(
        symbol=symbol,
        as_of=dt.datetime.now(dt.timezone.utc),
        regime=regime,
        regime_note=regime_note,
        direction=direction,
        direction_conf=direction_conf,
        spot=spot_value,
        futures=futures,
        intraday_change_pct=intraday_pct,
        atr=atr,
        rsi=rsi,
        macd_signal=macd_signal,
        support_level=support,
        resistance_level=resistance,
        vwap=vwap,
        vix=_round(vix_value, 2),
        iv_rank=_round(iv_rank, 2) if iv_rank is not None else None,
        expected_move=expected_move,
        breadth_summary=breadth_summary,
        volume_flow_comment=volume_flow_comment,
        news_headlines=news_headlines[:6],
        news_sentiment=news_sentiment,
        news_risk_comment=news_risk_comment,
        data_quality_note=data_quality,
    )


def build_prompt_sections(snapshot: MarketSnapshot) -> Dict[str, Any]:
    return {
        "meta": {
            "symbol": snapshot.symbol,
            "as_of_utc": snapshot.as_of.isoformat(),
            "regime": snapshot.regime,
            "direction": snapshot.direction,
            "direction_conf": snapshot.direction_conf,
            "data_quality_note": snapshot.data_quality_note,
        },
        "prompt_1_trend_direction": {
            "description": "Trend Direction Detector",
            "inputs": {
                "spot": snapshot.spot,
                "futures": snapshot.futures,
                "intraday_change_pct": snapshot.intraday_change_pct,
                "regime": snapshot.regime,
                "direction": snapshot.direction,
                "direction_conf": snapshot.direction_conf,
                "breadth_summary": snapshot.breadth_summary,
                "volume_flow_comment": snapshot.volume_flow_comment,
            },
        },
        "prompt_2_entry_confirmation": {
            "description": "Smart Entry Confirmation",
            "inputs": {
                "rsi": snapshot.rsi,
                "macd_signal": snapshot.macd_signal,
                "support_level": snapshot.support_level,
                "resistance_level": snapshot.resistance_level,
                "regime": snapshot.regime,
                "direction": snapshot.direction,
            },
        },
        "prompt_3_exit_blueprint": {
            "description": "Exit Timing Blueprint",
            "inputs": {
                "atr": snapshot.atr,
                "expected_move": snapshot.expected_move,
                "resistance_level": snapshot.resistance_level,
                "support_level": snapshot.support_level,
                "vol_comment": f"VIX={snapshot.vix}, iv_rank={snapshot.iv_rank}",
            },
        },
        "prompt_4_risk_guardrail": {
            "description": "Risk and Stop-Loss Guardrail",
            "inputs": {
                "atr": snapshot.atr,
                "support_level": snapshot.support_level,
                "recent_low_comment": "Use recent swing lows near support.",
            },
        },
        "prompt_5_insider_catalysts": {
            "description": "Insider / News Awareness",
            "inputs": {
                "news_headlines": snapshot.news_headlines,
                "news_sentiment": snapshot.news_sentiment,
                "news_risk_comment": snapshot.news_risk_comment,
            },
        },
        "prompt_6_win_or_walk": {
            "description": "Win or Walk-Away Verdict",
            "inputs": {
                "regime": snapshot.regime,
                "direction": snapshot.direction,
                "direction_conf": snapshot.direction_conf,
                "iv_rank": snapshot.iv_rank,
                "expected_move": snapshot.expected_move,
                "breadth_summary": snapshot.breadth_summary,
                "news_sentiment": snapshot.news_sentiment,
            },
        },
    }


def _fallback_brief(snapshot: MarketSnapshot) -> str:
    lines = [
        f"### Morning Brief ({snapshot.symbol})",
        f"Regime: {snapshot.regime_note}",
        f"Direction: {snapshot.direction} (conf {snapshot.direction_conf:.2f})",
        f"Spot: {snapshot.spot:.2f} | ATR: {snapshot.atr or '--'} | RSI: {snapshot.rsi or '--'}",
    ]
    if snapshot.support_level and snapshot.resistance_level:
        lines.append(
            f"Levels: support {snapshot.support_level:.2f} / resistance {snapshot.resistance_level:.2f}"
        )
    if snapshot.expected_move:
        lines.append(f"Expected move: ±{snapshot.expected_move:.2f}")
    if snapshot.data_quality_note:
        lines.append(f"Data note: {snapshot.data_quality_note}")
    return "\n".join(lines)


def render_brief_with_openai(
    sections: Dict[str, Any],
    model: str = "gpt-4.1-mini",
) -> str:
    serialized = json.dumps(sections, indent=2, default=str)
    system = "You are a financial analyst. Return ONLY valid JSON."
    user_prompt = (
        "Write a structured morning brief for the index in this JSON.\n"
        "Respond in JSON matching this schema:\n"
        "{\n"
        "  'trend': str,\n"
        "  'entry': list[str],\n"
        "  'exit': dict[str,str],\n"
        "  'risk': list[str],\n"
        "  'news': list[str],\n"
        "  'verdict': str\n"
        "}\n\n"
        f"JSON:\n```json\n{serialized}\n```"
    )
    client = OpenAIClient()
    return client.complete(system=system, prompt=user_prompt, model=model)


def _compute_agent_sentiment(symbols: Sequence[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    unique_symbols = [sym.upper() for sym in symbols if sym]

    async def _run():
        async with UnifiedDataConnector() as connector:
            report = MorningReport(
                connector,
                total_timeout=_DEFAULT_SENTIMENT_TIMEOUT,
                underlyings=unique_symbols,
            )
            return await report.generate()

    try:
        report_result = _run_async(_run)
        sentiments = build_agent_sentiments(report_result, unique_symbols)
        return sentiments, None
    except Exception as exc:  # pragma: no cover - diagnostic only
        return [], str(exc)


def build_morning_brief(
    symbol: str = "SPX",
    *,
    basket: Optional[Sequence[str]] = None,
    use_openai: bool = True,
) -> Dict[str, Any]:
    primary = symbol.upper()
    basket_symbols = list(dict.fromkeys([primary, *(s.upper() for s in (basket or []))]))

    snapshot = build_market_snapshot(primary)
    sections = build_prompt_sections(snapshot)
    brief_md = None
    ai_error: Optional[str] = None

    if use_openai:
        try:
            brief_md = render_brief_with_openai(sections)
        except OpenAIError as exc:
            ai_error = str(exc)

    if brief_md is None:
        brief_md = _fallback_brief(snapshot)
        if ai_error:
            brief_md += f"\n\n_OpenAI unavailable: {ai_error}_"

    snapshot_payload = asdict(snapshot)
    snapshot_payload["as_of"] = snapshot.as_of.isoformat()

    sentiments: List[Dict[str, Any]] = []
    sentiment_error: Optional[str] = None
    if basket_symbols:
        sentiments, sentiment_error = _compute_agent_sentiment(basket_symbols)
        if sentiments:
            sentiment_lines = [brief_md.rstrip(), "", "### Agent Sentiment"]
            for entry in sentiments:
                sentiment_lines.append(f"- {entry['summary']}")
            brief_md = "\n".join(sentiment_lines)
        elif sentiment_error:
            brief_md = brief_md.rstrip() + f"\n\n_Agent sentiment unavailable: {sentiment_error}_"

    return {
        "snapshot": snapshot_payload,
        "sections": sections,
        "brief_markdown": brief_md,
        "openai_error": ai_error,
        "agent_sentiment": sentiments,
        "agent_sentiment_error": sentiment_error,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate the advanced morning brief")
    parser.add_argument("symbol", nargs="?", default="SPX", help="Symbol to analyze")
    parser.add_argument("--no-openai", action="store_true", help="Skip OpenAI call")
    parser.add_argument("--json", action="store_true", help="Emit JSON payload")
    args = parser.parse_args()

    payload = build_morning_brief(symbol=args.symbol.upper(), use_openai=not args.no_openai)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload["brief_markdown"])


if __name__ == "__main__":
    _main()


# --- News helpers ---------------------------------------------------------


async def _fetch_polygon_news(symbol: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Fetch latest headlines from Polygon's REST API."""
    api_key = config.polygon_api_key
    if not api_key:
        return []

    url = "https://api.polygon.io/v2/reference/news"
    params = {
        "ticker": symbol,
        "limit": limit,
        "order": "desc",
        "sort": "published_utc",
    }

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, params=params, headers={"Authorization": f"Bearer {api_key}"}) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
        except aiohttp.ClientError:
            return []

    results = payload.get("results") or []
    news: List[Dict[str, Any]] = []
    for item in results:
        news.append(
            {
                "headline": item.get("headline"),
                "summary": item.get("description"),
                "source": item.get("source") or "Polygon",
                "published": item.get("published_utc"),
                "url": item.get("article_url"),
            }
        )
    return news


def _merge_news_entries(*sources: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for src in sources:
        merged.extend(src or [])
    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for item in sorted(
        merged,
        key=lambda x: (x.get("published") or "", x.get("headline") or ""),
        reverse=True,
    ):
        headline = (item.get("headline") or "").strip()
        if not headline:
            continue
        key = headline.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _summarize_news(entries: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not entries:
        return (None, "No fresh headlines from Massive/Polygon")

    pos_words = {"beat", "optimistic", "surge", "record", "upbeat"}
    neg_words = {"miss", "warning", "slump", "selloff", "downgrade", "fear"}
    score = 0
    for item in entries:
        text = f"{item.get('headline','')} {item.get('summary','')}".lower()
        score += sum(1 for word in pos_words if word in text)
        score -= sum(1 for word in neg_words if word in text)

    if score >= 2:
        sentiment = "positive"
    elif score <= -2:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    sources = sorted({(item.get("source") or "unknown").upper() for item in entries})
    risk_comment = f"{len(entries)} headlines from {', '.join(sources)}"
    if sentiment == "negative":
        risk_comment += " — tone skews defensive"
    elif sentiment == "positive":
        risk_comment += " — tone skewing constructive"

    return (sentiment, risk_comment)
