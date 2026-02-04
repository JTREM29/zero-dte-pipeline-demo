from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import GateResult
from services.calendar.earnings_overlay import build_earnings_risk_overlay
from tnt_alerts.reasons import SKIP_CTX_MISSING_EARNINGS


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        raw = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def earnings_blackout(
    symbol: str,
    calendar: Any,
    *,
    now_utc: datetime,
    pre_minutes: int,
    post_minutes: int,
    confirmed_only: bool,
) -> GateResult:
    """Skip if currently inside the earnings blackout window for a symbol.

    v1 uses `cal:earnings:{SYMBOL}` written manually for testing.
    """

    sym = str(symbol or "").strip().upper()
    if not sym:
        return GateResult(ok=True)

    # Preferred: canonical ctx snapshot.
    try:
        if hasattr(calendar, "get_symbol_context_snapshot"):
            ctx = calendar.get_symbol_context_snapshot(sym)
        else:
            ctx = None
    except Exception:
        ctx = None

    if isinstance(ctx, dict):
        e = ctx.get("earnings")
        if isinstance(e, dict) and bool(e.get("fresh")):
            ts = _parse_iso(str(e.get("ts_utc") or ""))
            if ts is None:
                return GateResult(ok=True)

            confirmed = bool(e.get("confirmed", True))
            if confirmed_only and not confirmed:
                return GateResult(ok=True)

            try:
                pre = int(pre_minutes)
                post = int(post_minutes)
            except Exception:
                pre = 0
                post = 0

            w0 = ts - timedelta(minutes=pre)
            w1 = ts + timedelta(minutes=post)
            if w0 <= now_utc <= w1:
                ov = e.get("overlay") if isinstance(e.get("overlay"), dict) else {}
                headline = str(ov.get("headline") or "").strip()
                detail = str(ov.get("detail") or "").strip()
                tags = ov.get("tags") if isinstance(ov.get("tags"), dict) else None

                return GateResult(
                    ok=False,
                    code="SKIP_EARNINGS_WINDOW",
                    note=f"earnings window ({sym})",
                    details={
                        "symbol": sym,
                        "earnings_ts_utc": ts.isoformat(),
                        "pre_min": pre,
                        "post_min": post,
                        "confirmed": confirmed,
                        "headline": headline,
                        "detail": detail,
                        "tags": tags,
                        "ctx": True,
                    },
                )

        # ctx present but earnings are stale/missing.
        try:
            from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
            from services.context.context_miss import record_ctx_miss

            if ctx_enabled():
                mode = ctx_strict_mode()
                record_ctx_miss(getattr(calendar, "r", None), "earnings_blackout_gate", sym=sym, why="ctx_earnings_stale")
                if mode == "on":
                    # Strict: fail closed for risk gates.
                    return GateResult(
                        ok=False,
                        code=SKIP_CTX_MISSING_EARNINGS,
                        note="Suppressed: context snapshot missing (earnings)",
                        details={"symbol": sym, "ctx": True, "why": "stale_or_missing"},
                    )
        except Exception:
            pass

        # Non-strict: treat as no blackout.
        return GateResult(ok=True)

    # ctx missing entirely.
    try:
        from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
        from services.context.context_miss import record_ctx_miss

        if ctx_enabled():
            mode = ctx_strict_mode()
            record_ctx_miss(getattr(calendar, "r", None), "earnings_blackout_gate", sym=sym, why="ctx_missing")
            if mode == "on":
                return GateResult(
                    ok=False,
                    code=SKIP_CTX_MISSING_EARNINGS,
                    note="Suppressed: context snapshot missing (earnings)",
                    details={"symbol": sym, "ctx": False, "why": "missing"},
                )
    except Exception:
        pass

    try:
        payload = calendar.get_earnings(symbol=sym)
    except Exception:
        payload = None

    if not isinstance(payload, dict):
        return GateResult(ok=True)

    ts = _parse_iso(str(payload.get("ts_utc") or ""))
    if ts is None:
        return GateResult(ok=True)

    confirmed = bool(payload.get("confirmed", True))
    if confirmed_only and not confirmed:
        return GateResult(ok=True)

    try:
        pre = int(pre_minutes)
        post = int(post_minutes)
    except Exception:
        pre = 0
        post = 0

    w0 = ts - timedelta(minutes=pre)
    w1 = ts + timedelta(minutes=post)
    if w0 <= now_utc <= w1:
        overlay = {}
        try:
            overlay = build_earnings_risk_overlay(payload)
        except Exception:
            overlay = {}

        headline = str(overlay.get("headline") or "").strip()
        detail = str(overlay.get("detail") or "").strip()
        tags = overlay.get("tags") if isinstance(overlay.get("tags"), dict) else None

        return GateResult(
            ok=False,
            code="SKIP_EARNINGS_WINDOW",
            note=f"earnings window ({sym})",
            details={
                "symbol": sym,
                "earnings_ts_utc": ts.isoformat(),
                "pre_min": pre,
                "post_min": post,
                "confirmed": confirmed,
                "headline": headline,
                "detail": detail,
                "tags": tags,
            },
        )

    return GateResult(ok=True)
