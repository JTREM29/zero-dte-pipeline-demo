"""Synchronous helper for building a lightweight morning report."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import config
from ..candidates.generator import generate_0dte_candidates
from ..data_connectors.unified import UnifiedDataConnector
from ..discord import post_message as post_discord_message
from ..openai_client import get_client as get_openai_client
from ..utils.timeout import Timeout, TimeoutError, sync_with_timeout

logger = logging.getLogger(__name__)


def _run_coro(factory):
    """Run an async factory in a synchronous context with a dedicated loop."""

    try:
        return asyncio.run(factory())
    except RuntimeError:
        # Fallback for contexts where an event loop is already running.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(factory())
        finally:
            loop.close()


@dataclass
class Snapshot:
    symbol: str
    price: float
    open_price: float
    change_pct: float
    expected_move: float
    provenance: str


@dataclass
class MorningReportContext:
    snapshot: Snapshot
    regime_label: str
    regime_note: str
    es_direction: str
    news_headlines: List[str]
    headlines_summary: Optional[str] = None
    headlines_error: Optional[str] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    target_expiration: Optional[datetime] = None



async def _fetch_snapshot_async(symbol: str, providers: List[str]) -> Dict[str, Any]:
    async with UnifiedDataConnector(priority=providers, timeout=config.default_timeout) as connector:
        quote = await connector.get_quote(symbol, providers=providers)
        provenance = connector.primary_provider or (providers[0] if providers else "unified")
        return {
            "quote": quote or {},
            "snapshot_provenance": provenance,
        }


def _fetch_snapshot_from_providers(symbol: str, providers: List[str]) -> Dict[str, Any]:
    return _run_coro(lambda: _fetch_snapshot_async(symbol, providers))


def get_morning_snapshot(symbol: str) -> Dict[str, Any]:
    """Fetch a snapshot that prefers IQFeed but falls back quickly to the secondary provider."""

    iqfeed_priority = ["iqfeed"]
    try:
        with Timeout(
            config.morning_snapshot_timeout_seconds,
            operation_name=f"iqfeed_snapshot_{symbol}",
        ):
            result = _fetch_snapshot_from_providers(symbol, iqfeed_priority)
            if result.get("quote"):
                return result
    except TimeoutError as exc:
        logger.warning("IQFeed snapshot timed out for %s: %s", symbol, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("IQFeed snapshot failed for %s: %s – falling back to secondary provider", symbol, exc)

    polygon_priority = ["polygon"]
    try:
        fallback = _fetch_snapshot_from_providers(symbol, polygon_priority)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Snapshot fetch failed for %s: %s", symbol, exc)
        return {"quote": {}, "snapshot_provenance": "polygon_only"}

    fallback["snapshot_provenance"] = fallback.get("snapshot_provenance", "polygon_only")
    return fallback


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compute_regime_label(snapshot: Snapshot) -> str:
    """Very lightweight regime proxy based on percent change."""
    if abs(snapshot.change_pct) < 0.3:
        return "RANGE_LOW_VOL"
    if snapshot.change_pct >= 0.3:
        return "TREND_UP"
    return "TREND_DOWN"


def _fetch_snapshot(symbol: str) -> Snapshot:
    raw = get_morning_snapshot(symbol)
    quote = raw.get("quote", {})
    provenance = raw.get("snapshot_provenance", raw.get("provenance", "unified"))

    price = _safe_float(
        quote.get("last") or quote.get("price") or quote.get("close") or quote.get("bid"),
        default=0.0,
    )
    open_price = _safe_float(quote.get("open"), default=price)
    if not open_price:
        open_price = price

    change_pct = quote.get("change_pct")
    if change_pct is None:
        change_pct = ((price - open_price) / open_price * 100.0) if open_price else 0.0
    else:
        change_pct = _safe_float(change_pct)

    expected_move = _safe_float(quote.get("expected_move"), default=0.0)

    return Snapshot(
        symbol=symbol,
        price=price,
        open_price=open_price,
        change_pct=change_pct,
        expected_move=expected_move,
        provenance=str(provenance),
    )


def _fetch_headlines_for_symbol(symbol: str) -> List[str]:
    """Placeholder hook that can be wired up to IQFeed or Polygon news."""
    # TODO: integrate real news sources
    return []


def _build_context(symbol: str, primary_expiration: Optional[datetime]) -> MorningReportContext:
    snapshot = _fetch_snapshot(symbol)

    regime_label = _compute_regime_label(snapshot)
    regime_note = f"proxy regime from intraday change {snapshot.change_pct:.2f}%"

    es_direction = "NEUTRAL"  # TODO: integrate ES direction if available

    headlines = _fetch_headlines_for_symbol(symbol)
    headlines_summary: Optional[str] = None
    headlines_error: Optional[str] = None
    if config.openai_enabled and headlines:
        try:
            openai_client = get_openai_client()
            headlines_summary = openai_client.summarize_headlines(
                headlines,
                symbol=symbol,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Headline summarization failed: %s", exc)
            headlines_error = str(exc)

    max_candidates = max(1, config.max_morning_candidates)
    candidates: List[Dict[str, Any]] = []
    try:
        all_candidates = generate_0dte_candidates(
            underlying=symbol,
            max_structures=max_candidates * 3,
            primary_expiration=primary_expiration,
        )
        if all_candidates:
            if isinstance(all_candidates[0], dict) and "score" in all_candidates[0]:
                all_candidates = sorted(
                    all_candidates,
                    key=lambda candidate: candidate.get("score", 0.0),
                    reverse=True,
                )
            candidates = all_candidates[:max_candidates]
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_0dte_candidates failed: %s", exc)

    return MorningReportContext(
        snapshot=snapshot,
        regime_label=regime_label,
        regime_note=regime_note,
        es_direction=es_direction,
        news_headlines=headlines,
        headlines_summary=headlines_summary,
        headlines_error=headlines_error,
        candidates=candidates,
        target_expiration=primary_expiration,
    )


def _format_report_text(ctx: MorningReportContext) -> str:
    snap = ctx.snapshot
    lines: List[str] = []
    lines.append(f"🔔 Morning Report — {snap.symbol}")
    lines.append("")
    lines.append("## Index snapshot")
    lines.append(f"• Price: {snap.price:.2f}")
    lines.append(f"• Open: {snap.open_price:.2f}")
    lines.append(f"• Change: {snap.change_pct:+.2f}%")
    if snap.expected_move:
        lines.append(f"• Exp. move (1d): ±{snap.expected_move:.1f} pts")
    lines.append(f"• Price source: {snap.provenance}")
    lines.append("")
    lines.append("## Regime / Direction")
    lines.append(f"• Regime: {ctx.regime_label} ({ctx.regime_note})")
    lines.append(f"• ES direction: {ctx.es_direction}")
    lines.append("")
    lines.append("## News (OpenAI summary)")
    if ctx.headlines_summary:
        lines.append(ctx.headlines_summary)
    elif ctx.headlines_error:
        lines.append(f"• Headline summary unavailable: {ctx.headlines_error}")
    else:
        lines.append("• No recent headline summary available.")
    lines.append("")
    lines.append(
        f"## 0DTE structures (top {len(ctx.candidates)}/{config.max_morning_candidates})"
    )
    if not ctx.candidates:
        lines.append("• No 0DTE candidates passed gates.")
    else:
        for candidate in ctx.candidates:
            side = candidate.get("direction") or candidate.get("side") or "N/A"
            description = (
                candidate.get("description") or candidate.get("strategy") or candidate.get("structure") or "structure"
            )
            score = candidate.get("score", 0.0)
            credit = candidate.get("credit")
            credit_txt = ""
            if isinstance(credit, (int, float)):
                credit_txt = f", credit≈{credit:.2f}"
            lines.append(f"• {side} {description} (score={score:.2f}{credit_txt})")

    lines.append("")
    lines.append(
        "_Informational only. This report is not a recommendation to trade or "
        "a guarantee of future performance._"
    )

    return "\n".join(lines)


def build_morning_report(
    symbol: str,
    *,
    as_dict: bool = False,
    post_to_discord: bool = False,
    primary_expiration: Optional[datetime] = None,
) -> Dict[str, Any] | str:
    """Public entry point to build the report with a hard timeout."""

    def _inner() -> Dict[str, Any]:
        started = time.time()
        ctx = _build_context(symbol, primary_expiration)
        text = _format_report_text(ctx)

        tldr = ""
        try:
            tldr = get_openai_client().summarize_morning_brief(text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("summarize_morning_brief failed: %s", exc)

        elapsed = time.time() - started

        payload: Dict[str, Any] = {
            "ok": True,
            "symbol": symbol,
            "snapshot": {
                "price": ctx.snapshot.price,
                "open_price": ctx.snapshot.open_price,
                "change_pct": ctx.snapshot.change_pct,
                "expected_move": ctx.snapshot.expected_move,
                "provenance": ctx.snapshot.provenance,
            },
            "regime": {
                "label": ctx.regime_label,
                "note": ctx.regime_note,
                "es_direction": ctx.es_direction,
            },
            "news": {
                "headlines_count": len(ctx.news_headlines),
                "summary": ctx.headlines_summary or "",
                "error": ctx.headlines_error,
            },
            "headlines_summary": ctx.headlines_summary,
            "headlines_error": ctx.headlines_error,
            "candidates": ctx.candidates,
            "meta": {
                "elapsed_seconds": elapsed,
                "candidate_cap": config.max_morning_candidates,
            },
            "text": text,
            "tldr": tldr,
        }

        if ctx.target_expiration:
            payload["meta"]["primary_expiration"] = ctx.target_expiration.strftime("%Y-%m-%d")

        if post_to_discord:
            content = tldr or text
            post_discord_message(content=content, username="ZeroDTE Morning Bot")

        return payload

    wrapped = sync_with_timeout(
        timeout=config.morning_report_timeout_seconds,
        default=None,
    )(_inner)

    result = wrapped()
    if result is None:
        result = {
            "ok": False,
            "error": "morning_report_timeout",
        }

    if as_dict:
        return result
    return result.get("text", "")
