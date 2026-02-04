from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import GateResult
from tnt_alerts.reasons import SKIP_CTX_MISSING_NEWS


def news_blackout(
    symbol: str,
    news: Any,
    *,
    now_utc: datetime,
    symbol_minutes: int,
    market_minutes: int | None,
) -> GateResult:
    """Skip if symbol/market news is too recent.

    v1 reads stamps from NewsService.
    """

    sym = str(symbol or "").strip().upper()
    if not sym:
        return GateResult(ok=True)

    try:
        sym_min = int(symbol_minutes)
    except Exception:
        sym_min = 0

    try:
        mkt_min = int(market_minutes) if market_minutes is not None else None
    except Exception:
        mkt_min = None

    now_s = int(now_utc.replace(tzinfo=timezone.utc).timestamp())

    # Preferred: use canonical ctx snapshot if the NewsService supports it.
    # This keeps all consumers aligned on the same derived fields.
    ctx_sym = None
    ctx_mkt = None
    try:
        if hasattr(news, "get_symbol_context_snapshot"):
            ctx_sym = news.get_symbol_context_snapshot(sym)
        if hasattr(news, "get_market_context_snapshot"):
            ctx_mkt = news.get_market_context_snapshot()
    except Exception:
        ctx_sym = None
        ctx_mkt = None

    try:
        if sym_min > 0 and isinstance(ctx_sym, dict):
            n = ctx_sym.get("news") or {}
            last_s = n.get("sym_last_ts")
            if last_s is not None:
                age_s = now_s - int(last_s)
                if age_s >= 0 and age_s <= sym_min * 60:
                    return GateResult(
                        ok=False,
                        code="SKIP_NEWS_RECENT",
                        note=f"symbol news recent ({sym})",
                        details={
                            "symbol": sym,
                            "age_sec": age_s,
                            "window_min": sym_min,
                            "sym_state": n.get("sym_state"),
                        },
                    )
    except Exception:
        pass

    # If we require symbol news gating but ctx doesn't have the needed stamp, treat as missing.
    if sym_min > 0 and isinstance(ctx_sym, dict):
        try:
            n = ctx_sym.get("news") or {}
            _has_stamp = (isinstance(n, dict) and n.get("sym_last_ts") is not None)
        except Exception:
            _has_stamp = False
        if not _has_stamp:
            try:
                from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
                from services.context.context_miss import record_ctx_miss

                if ctx_enabled():
                    mode = ctx_strict_mode()
                    record_ctx_miss(getattr(news, "r", None), "news_blackout_gate", sym=sym, why="ctx_missing_sym_ts")
                    if mode == "on":
                        return GateResult(
                            ok=False,
                            code=SKIP_CTX_MISSING_NEWS,
                            note="Suppressed: context snapshot missing (news)",
                            details={"symbol": sym, "scope": "symbol", "window_min": sym_min, "why": "missing_stamp"},
                        )
            except Exception:
                pass

    # Strict: if symbol news gating is enabled but ctx is missing/insufficient, fail closed.
    if sym_min > 0 and not isinstance(ctx_sym, dict):
        try:
            from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
            from services.context.context_miss import record_ctx_miss

            if ctx_enabled():
                mode = ctx_strict_mode()
                record_ctx_miss(getattr(news, "r", None), "news_blackout_gate", sym=sym, why="ctx_missing_sym")
                if mode == "on":
                    return GateResult(
                        ok=False,
                        code=SKIP_CTX_MISSING_NEWS,
                        note="Suppressed: context snapshot missing (news)",
                        details={"symbol": sym, "scope": "symbol", "window_min": sym_min},
                    )
        except Exception:
            pass

    try:
        if mkt_min is not None and mkt_min > 0 and isinstance(ctx_mkt, dict):
            n = ctx_mkt.get("news") or {}
            last_s = n.get("market_last_ts")
            if last_s is not None:
                age_s = now_s - int(last_s)
                if age_s >= 0 and age_s <= mkt_min * 60:
                    return GateResult(
                        ok=False,
                        code="SKIP_MARKET_NEWS_RECENT",
                        note="market news recent",
                        details={
                            "age_sec": age_s,
                            "window_min": mkt_min,
                            "market_state": n.get("market_state"),
                        },
                    )
    except Exception:
        pass

    if mkt_min is not None and mkt_min > 0 and isinstance(ctx_mkt, dict):
        try:
            n = ctx_mkt.get("news") or {}
            _has_stamp = (isinstance(n, dict) and n.get("market_last_ts") is not None)
        except Exception:
            _has_stamp = False
        if not _has_stamp:
            try:
                from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
                from services.context.context_miss import record_ctx_miss

                if ctx_enabled():
                    mode = ctx_strict_mode()
                    record_ctx_miss(getattr(news, "r", None), "news_blackout_gate", sym="MARKET", why="ctx_missing_market_ts")
                    if mode == "on":
                        return GateResult(
                            ok=False,
                            code=SKIP_CTX_MISSING_NEWS,
                            note="Suppressed: context snapshot missing (news)",
                            details={"scope": "market", "window_min": mkt_min, "why": "missing_stamp"},
                        )
            except Exception:
                pass

    # Strict: if market news gating is enabled but ctx is missing/insufficient, fail closed.
    if mkt_min is not None and mkt_min > 0 and not isinstance(ctx_mkt, dict):
        try:
            from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
            from services.context.context_miss import record_ctx_miss

            if ctx_enabled():
                mode = ctx_strict_mode()
                record_ctx_miss(getattr(news, "r", None), "news_blackout_gate", sym="MARKET", why="ctx_missing_market")
                if mode == "on":
                    return GateResult(
                        ok=False,
                        code=SKIP_CTX_MISSING_NEWS,
                        note="Suppressed: context snapshot missing (news)",
                        details={"scope": "market", "window_min": mkt_min},
                    )
        except Exception:
            pass

    if sym_min > 0:
        try:
            last_s = news.get_symbol_last_epoch_s(sym)
        except Exception:
            last_s = None
        if last_s is not None:
            age_s = now_s - int(last_s)
            if age_s >= 0 and age_s <= sym_min * 60:
                return GateResult(
                    ok=False,
                    code="SKIP_NEWS_RECENT",
                    note=f"symbol news recent ({sym})",
                    details={"symbol": sym, "age_sec": age_s, "window_min": sym_min},
                )

    if mkt_min is not None and mkt_min > 0:
        try:
            last_s = news.get_market_last_epoch_s()
        except Exception:
            last_s = None
        if last_s is not None:
            age_s = now_s - int(last_s)
            if age_s >= 0 and age_s <= mkt_min * 60:
                return GateResult(
                    ok=False,
                    code="SKIP_MARKET_NEWS_RECENT",
                    note="market news recent",
                    details={"age_sec": age_s, "window_min": mkt_min},
                )

    return GateResult(ok=True)
