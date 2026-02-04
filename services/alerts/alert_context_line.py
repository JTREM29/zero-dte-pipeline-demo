from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


def _env_bool(name: str, default: str = "0") -> bool:
    try:
        return (os.getenv(name, default) or default).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def _clip(s: str, n: int) -> str:
    try:
        n2 = int(n)
    except Exception:
        n2 = 0
    if n2 <= 0:
        return ""
    if len(s) <= n2:
        return s
    return s[: max(0, n2 - 1)] + "…"


def _decode_redis_value(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    try:
        return str(raw)
    except Exception:
        return None


def _parse_refreshed_utc_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def build_alert_context_line(store: Any, symbol: str, *, alert_id: str | None = None) -> str | None:
    """Build a short, cache-only context line for triggered alerts.

    Off by default. Uses only Redis reads (and an optional Redis TTL write for rate-limiting).

    Example:
      "Context: ES bearish • earnings AMC tomorrow • News quiet"
    """

    if not _env_bool("ALERT_CONTEXT_LINE_ENABLED", "0"):
        return None

    sym = str(symbol or "").strip().upper()
    if not sym:
        return None

    # Rate-limit per-alert_id to avoid spam on rapid re-triggers.
    cooldown_s = max(0, min(_env_int("ALERT_CONTEXT_LINE_COOLDOWN_SEC", 300), 3600))
    if cooldown_s > 0 and alert_id:
        try:
            k = f"tnt:alerts:context_line:last:{str(alert_id).strip()}"
            r = getattr(store, "r", None)
            if r is not None:
                if r.get(k):
                    return None
                # Best-effort: only a TTL stamp.
                r.setex(k, int(cooldown_s), "1")
        except Exception:
            pass

    parts: list[str] = []

    r = getattr(store, "r", None)
    if r is None:
        return None

    # Preferred: canonical snapshot.
    ctx_sym = None
    try:
        raw_ctx = _decode_redis_value(r.get(f"ctx:sym:{sym}"))
        if raw_ctx:
            obj = json.loads(raw_ctx)
            if isinstance(obj, dict) and int(obj.get("ctx_version") or 0) == 1:
                ctx_sym = obj
    except Exception:
        ctx_sym = None

    if isinstance(ctx_sym, dict):
        try:
            from services.context.context_formatters import fmt_edge_clarity, fmt_earnings_near, fmt_futures, fmt_news

            if _env_bool("ALERT_CONTEXT_LINE_INCLUDE_FUTURES", "1"):
                p = fmt_futures(ctx_sym)
                if p:
                    parts.append(p)

            if _env_bool("ALERT_CONTEXT_LINE_INCLUDE_EARNINGS", "1"):
                p = fmt_earnings_near(ctx_sym)
                if p:
                    parts.append(p)

            if _env_bool("ALERT_CONTEXT_LINE_INCLUDE_NEWS", "1"):
                p = fmt_news(ctx_sym)
                if p:
                    parts.append(p)

            max_len = max(40, min(_env_int("ALERT_CONTEXT_LINE_MAX_TOKENS", 120), 240))
            return fmt_edge_clarity(parts, max_len=max_len)
        except Exception:
            # If formatter fails, fall back to legacy below.
            pass

    # Strict cutover behavior.
    try:
        from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
        from services.context.context_miss import record_ctx_miss

        if ctx_enabled():
            mode = ctx_strict_mode()
            record_ctx_miss(r, "edge_clarity", sym=sym, why="ctx_missing")
            if mode == "on":
                # Strict: omit the ctx line (do not crash, no legacy fallback).
                return None
    except Exception:
        pass

    # Fallback (temporary): legacy raw keys.

    # Futures (bias only, derived from fut:scores.regime)
    if _env_bool("ALERT_CONTEXT_LINE_INCLUDE_FUTURES", "1"):
        try:
            raw = _decode_redis_value(r.get("fut:scores"))
            if raw:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    reg = str(obj.get("regime") or "").strip().upper()
                    if "BEAR" in reg:
                        parts.append("ES: bearish")
                    elif "BULL" in reg:
                        parts.append("ES: bullish")
                    elif reg:
                        parts.append("ES: neutral")
        except Exception:
            pass

    # Earnings nearby (today/tomorrow only; fresh only)
    if _env_bool("ALERT_CONTEXT_LINE_INCLUDE_EARNINGS", "1"):
        try:
            raw = _decode_redis_value(r.get(f"cal:earnings:{sym}"))
            if raw:
                ev = json.loads(raw)
            else:
                ev = None
            if isinstance(ev, dict):
                refreshed_s = str(ev.get("refreshed_utc") or "").strip()
                dt_ref = _parse_refreshed_utc_iso(refreshed_s)
                if dt_ref is not None:
                    stale_h = max(1, min(_env_int("EARNINGS_PREVIEW_STALE_HOURS", 48), 168))
                    age_s = int((datetime.now(timezone.utc) - dt_ref).total_seconds())
                    if age_s < 0:
                        age_s = 0
                    if age_s <= stale_h * 3600:
                        from services.calendar.earnings_overlay import build_earnings_near_note

                        note = build_earnings_near_note(ev)
                        if note:
                            parts.append(note)
        except Exception:
            pass

    # News “quiet/recent/just hit” (no headlines; stamp-only)
    if _env_bool("ALERT_CONTEXT_LINE_INCLUDE_NEWS", "1"):
        try:
            now = int(datetime.now(timezone.utc).timestamp())

            sym_ts = _decode_redis_value(r.get(f"news:symbol:{sym}:last_ts"))
            mkt_ts = _decode_redis_value(r.get("news:market:last_ts"))

            def _age_min(v: str | None) -> int | None:
                if not v:
                    return None
                try:
                    return int((now - int(float(v))) / 60)
                except Exception:
                    return None

            ages = [a for a in (_age_min(sym_ts), _age_min(mkt_ts)) if a is not None]
            if ages:
                age = min(ages)
                if age <= 10:
                    parts.append("News: just hit")
                elif age <= 60:
                    parts.append("News: recent")
                else:
                    parts.append("News: quiet")
        except Exception:
            pass

    if not parts:
        return None

    max_len = max(40, min(_env_int("ALERT_CONTEXT_LINE_MAX_TOKENS", 120), 240))
    line = "Context: " + " • ".join(parts)
    return _clip(line, max_len)
