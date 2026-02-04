from __future__ import annotations

import json
import os
import time
from typing import Any, Optional


DEFAULT_STALE_HOURS = 48
CTX_VERSION = 1


def _now() -> int:
    return int(time.time())


def _decode(raw: Any) -> str | None:
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


def _safe_json_load(s: Any) -> dict | None:
    raw = _decode(s)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _refreshed_epoch(refreshed_utc: Any) -> int | None:
    """Best-effort: support epoch seconds or ISO strings."""

    if isinstance(refreshed_utc, (int, float)):
        try:
            return int(refreshed_utc)
        except Exception:
            return None

    if isinstance(refreshed_utc, str):
        s = refreshed_utc.strip()
        if not s:
            return None
        try:
            # Avoid importing datetime for a single parse path: accept numeric strings first.
            if s.isdigit():
                return int(s)
        except Exception:
            pass
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None

    return None


def _fresh(refreshed_utc: Any, stale_hours: int) -> bool:
    ts = _refreshed_epoch(refreshed_utc)
    if ts is None:
        return False
    age_s = _now() - int(ts)
    return age_s >= 0 and age_s <= int(stale_hours) * 3600


def _news_state(age_min: Optional[int]) -> Optional[str]:
    if age_min is None:
        return None
    if age_min <= 10:
        return "just_hit"
    if age_min <= 60:
        return "recent"
    return "quiet"


def _age_min(ts: Any) -> Optional[int]:
    try:
        raw = _decode(ts)
        if raw is None:
            return None
        return int((_now() - int(float(raw))) / 60)
    except Exception:
        return None


def _coerce_futures_bias(obj: dict) -> str | None:
    # Preferred: explicit bias.
    b = str(obj.get("bias") or "").strip().upper()
    if b in ("BULLISH", "BEARISH", "NEUTRAL"):
        return b

    # Compatibility: derive from regime.
    reg = str(obj.get("regime") or "").strip().upper()
    # Databento ingest uses regimes like RISK_OFF / RISK_ON.
    if reg in {"RISK_OFF", "OFF"} or "RISK_OFF" in reg or reg.endswith("_OFF"):
        return "BEARISH"
    if reg in {"RISK_ON", "ON"} or "RISK_ON" in reg or reg.endswith("_ON"):
        return "BULLISH"
    if "BEAR" in reg:
        return "BEARISH"
    if "BULL" in reg:
        return "BULLISH"
    if reg:
        return "NEUTRAL"
    return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return default


def _format_as_of_et(ts_epoch: int) -> str | None:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        dt = datetime.fromtimestamp(int(ts_epoch), tz=et)
        return dt.strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return None


def _coerce_confidence_label(value: Any) -> str:
    """Map numeric confidence/score to HIGH|MED|LOW. Defaults to LOW."""

    try:
        x = float(value)
    except Exception:
        return "LOW"

    if x >= 0.75:
        return "HIGH"
    if x >= 0.55:
        return "MED"
    return "LOW"


def _build_candidates_block(store, sym: str) -> dict[str, Any]:
    """Build the canonical ctx:sym:{SYM}.candidates block.

    This is writer-side best-effort: it may read auxiliary keys (e.g.
    `tnt:candidates:{SYM}`) and project them into the ctx snapshot.
    """

    now = _now()
    stale_sec = max(30, _env_int("CANDIDATES_STALE_SEC", 10 * 60))

    out: dict[str, Any] = {
        "as_of_et": None,
        "age_s": None,
        "status": "NONE",  # APPROVED | NONE | STALE | ERROR
        "why": None,
        "eligibility": "NO_TRADE",  # ELIGIBLE | NO_TRADE
        "confidence": "LOW",  # HIGH | MED | LOW
        "approved": [],
        "top": None,
    }

    sym_u = str(sym or "").strip().upper()
    if not sym_u:
        out["status"] = "ERROR"
        out["why"] = "missing_symbol"
        return out

    approved_cap = _env_int("CTX_CANDIDATES_APPROVED_CAP", 5)
    approved_cap = max(0, min(int(approved_cap), 50))

    raw_obj: dict | None = None
    try:
        raw = store.r.get(f"tnt:candidates:{sym_u}")
        raw_obj = _safe_json_load(raw)
    except Exception:
        raw_obj = None
    if raw_obj is None:
        # Compatibility fallback (older key name).
        try:
            raw = store.r.get(f"cand:sym:{sym_u}")
            raw_obj = _safe_json_load(raw)
        except Exception:
            raw_obj = None

    if raw_obj is None:
        out["status"] = "NONE"
        out["why"] = "NO_CANDIDATE_CACHE"
        out["as_of_et"] = _format_as_of_et(now)
        out["age_s"] = 0
        return out

    # If the upstream writer stored the block directly, prefer it.
    if isinstance(raw_obj.get("candidates"), dict):
        raw_obj = raw_obj.get("candidates")  # type: ignore[assignment]

    ts_utc = raw_obj.get("ts_utc") or raw_obj.get("ts") or raw_obj.get("timestamp")
    ts_epoch = _safe_int(ts_utc)
    if ts_epoch is None:
        ts_epoch = now

    age_s = None
    try:
        age_s = max(0, int(now - int(ts_epoch)))
    except Exception:
        age_s = None

    status = str(raw_obj.get("status") or "").strip().upper() or None
    why = str(raw_obj.get("why") or "").strip() or None
    eligibility = str(raw_obj.get("eligibility") or "").strip().upper() or None
    confidence = str(raw_obj.get("confidence") or "").strip().upper() or None

    approved = raw_obj.get("approved")
    if not isinstance(approved, list):
        approved = []
    approved = [x for x in approved if isinstance(x, dict)]

    top = raw_obj.get("top")
    if not isinstance(top, dict):
        top = approved[0] if approved else None

    # Snapshot size safety: keep only a small approved set (and always keep `top`).
    if approved_cap == 0:
        approved = []
    elif approved_cap and len(approved) > approved_cap:
        capped: list[dict[str, Any]] = []
        if isinstance(top, dict):
            capped.append(top)
        for item in approved:
            if len(capped) >= approved_cap:
                break
            if isinstance(top, dict) and item == top:
                continue
            capped.append(item)
        approved = capped

    # Normalize eligibility.
    if eligibility not in {"ELIGIBLE", "NO_TRADE"}:
        eligibility = "ELIGIBLE" if approved else "NO_TRADE"

    # Normalize confidence.
    if confidence not in {"HIGH", "MED", "LOW"}:
        if isinstance(top, dict):
            confidence = _coerce_confidence_label(top.get("confidence") if top.get("confidence") is not None else top.get("score"))
        else:
            confidence = "LOW"

    if status not in {"APPROVED", "NONE", "STALE", "ERROR"}:
        status = "APPROVED" if approved else "NONE"

    # Staleness override.
    if age_s is not None and age_s > stale_sec:
        status = "STALE"

    out.update(
        {
            "as_of_et": str(raw_obj.get("as_of_et") or "").strip() or _format_as_of_et(ts_epoch),
            "age_s": age_s,
            "status": status,
            "why": why,
            "eligibility": eligibility,
            "confidence": confidence,
            "approved": approved,
            "top": top,
        }
    )
    return out


def _news_latest_item(store, sym: str) -> dict[str, Any] | None:
    """Best-effort latest cached news item (title/url).

    Consumers should read ctx:* only. The ctx writer may synthesize this from
    the optional history layer (news:tick:* + news:item:*).
    """

    symbol = str(sym or "").strip().upper()
    if not symbol:
        return None

    # Fast path: if zrevrange is unsupported (unit tests / minimal redis), skip.
    try:
        zrevrange = getattr(store.r, "zrevrange", None)
    except Exception:
        zrevrange = None
    if zrevrange is None:
        return None

    try:
        ids = zrevrange(f"news:tick:{symbol}", 0, 0)
    except Exception:
        ids = None
    if not isinstance(ids, list) or not ids:
        return None
    nid = ids[0]
    if isinstance(nid, (bytes, bytearray)):
        nid = nid.decode("utf-8", errors="replace")
    nid_s = str(nid or "").strip()
    if not nid_s:
        return None

    try:
        raw = store.r.get(f"news:item:{nid_s}")
    except Exception:
        raw = None
    obj = _safe_json_load(raw)
    if not obj:
        return None

    title = str(obj.get("headline") or obj.get("title") or "").strip()
    url = str(obj.get("url") or "").strip()
    published_utc = str(obj.get("published_utc") or "").strip()
    out = {
        "id": nid_s,
        "title": title or None,
        "url": url or None,
        "published_utc": published_utc or None,
    }
    # If nothing useful, return None.
    if not (out.get("title") or out.get("url")):
        return None
    return out


def build_symbol_context(store, sym: str, stale_hours: int = DEFAULT_STALE_HOURS) -> dict:
    sym = str(sym or "").strip().upper()
    now = _now()

    # FUTURES (bias-only)
    fut_bias = None
    fut_updated = None
    fut_regime = None
    fut_vol_mult = None
    try:
        raw = store.r.get("fut:scores")
        if raw:
            obj = _safe_json_load(raw) or {}
            fut_bias = _coerce_futures_bias(obj)
            fut_updated = obj.get("ts_utc") or obj.get("updated_utc") or None
            fut_regime = str(obj.get("regime") or "").strip().upper() or None
            fut_vol_mult = _safe_float(obj.get("vol_mult"))
    except Exception:
        pass

    # EARNINGS (cached blob)
    earn = None
    try:
        raw = store.r.get(f"cal:earnings:{sym}")
        if raw:
            ev = _safe_json_load(raw)
            if ev:
                refreshed = ev.get("refreshed_utc")
                fresh = _fresh(refreshed, stale_hours)

                earn = {
                    "ts_utc": ev.get("ts_utc"),
                    "session": str(ev.get("session") or "UNKNOWN").strip().upper(),
                    "confirmed": bool(ev.get("confirmed", False)),
                    "expected_move_pct": ev.get("expected_move_pct"),
                    "liquidity_risk": str(ev.get("liquidity_risk") or "").strip().upper() or None,
                    "front_iv": ev.get("front_iv"),
                    "refreshed_utc": refreshed,
                    "fresh": bool(fresh),
                }

                # Phase 4: store note + tags so formatters don't recompute.
                if fresh:
                    try:
                        from services.calendar.earnings_overlay import build_earnings_near_note, build_earnings_risk_overlay

                        overlay = build_earnings_risk_overlay(ev)
                        note = build_earnings_near_note(ev)
                        earn["note"] = note
                        earn["overlay"] = {
                            "headline": (overlay or {}).get("headline") if isinstance(overlay, dict) else None,
                            "detail": (overlay or {}).get("detail") if isinstance(overlay, dict) else None,
                            "tags": (overlay or {}).get("tags") if isinstance(overlay, dict) else {},
                        }
                    except Exception:
                        pass
    except Exception:
        pass

    # NEWS (state-only)
    news: dict[str, Any]
    try:
        sym_last = store.r.get(f"news:symbol:{sym}:last_ts")
        sym_title = store.r.get(f"news:symbol:{sym}:last_title")
        # Fallback: cache/ingest freshness stamps (used by pollers) when broadcast/gate stamps are absent.
        if not sym_last:
            sym_last = store.r.get(f"news:symbol:{sym}:last_seen_ts")
        if not sym_title:
            sym_title = store.r.get(f"news:symbol:{sym}:last_seen_title")

        mkt_last = store.r.get("news:market:last_ts")
        mkt_title = store.r.get("news:market:last_title")
        if not mkt_last:
            mkt_last = store.r.get("news:market:last_seen_ts")
        if not mkt_title:
            mkt_title = store.r.get("news:market:last_seen_title")
        # Compatibility: some pollers cache market news under the synthetic symbol bucket.
        if not mkt_last:
            mkt_last = store.r.get("news:symbol:MARKET:last_seen_ts")
        if not mkt_title:
            mkt_title = store.r.get("news:symbol:MARKET:last_seen_title")
        sym_age = _age_min(sym_last)
        mkt_age = _age_min(mkt_last)
        sym_last_s = _decode(sym_last)
        mkt_last_s = _decode(mkt_last)
        sym_title_s = _decode(sym_title)
        mkt_title_s = _decode(mkt_title)
        news = {
            "sym_last_ts": int(sym_last_s) if (sym_last_s or "").isdigit() else None,
            "market_last_ts": int(mkt_last_s) if (mkt_last_s or "").isdigit() else None,
            "sym_state": _news_state(sym_age),
            "market_state": _news_state(mkt_age),
            "sym_last_title": (sym_title_s.strip() if isinstance(sym_title_s, str) and sym_title_s.strip() else None),
            "market_last_title": (mkt_title_s.strip() if isinstance(mkt_title_s, str) and mkt_title_s.strip() else None),
        }

        # Optional: attach the latest cached item (for linked headline).
        latest = _news_latest_item(store, sym)
        if latest:
            news["latest"] = latest
    except Exception:
        news = {}

    ctx = {
        "ctx_version": CTX_VERSION,
        "ts_utc": now,
        "symbol": sym,
        "sources_present": {
            "futures": fut_bias is not None,
            "earnings": bool(earn is not None),
            "news": bool(news),
            "candidates": True,
        },
        "futures": {
            "bias": fut_bias,
            "updated_utc": fut_updated,
            "regime": fut_regime,
            "vol_mult": fut_vol_mult,
            "fresh": fut_bias is not None,
        },
        "earnings": earn,
        "news": news,
        "candidates": _build_candidates_block(store, sym),
        "quality": {
            "earnings_fresh": bool(earn and earn.get("fresh")),
            "has_news": bool(news.get("sym_state") or news.get("market_state")),
            "has_futures": fut_bias is not None,
        },
    }
    return ctx


def build_market_context(store) -> dict:
    now = _now()

    mkt_last = None
    try:
        mkt_last = store.r.get("news:market:last_ts")
    except Exception:
        mkt_last = None
    if not mkt_last:
        try:
            mkt_last = store.r.get("news:market:last_seen_ts")
        except Exception:
            mkt_last = None
    if not mkt_last:
        try:
            mkt_last = store.r.get("news:symbol:MARKET:last_seen_ts")
        except Exception:
            mkt_last = None
    mkt_age = _age_min(mkt_last)
    mkt_last_s = _decode(mkt_last)
    try:
        mkt_title = store.r.get("news:market:last_title")
    except Exception:
        mkt_title = None
    if not mkt_title:
        try:
            mkt_title = store.r.get("news:market:last_seen_title")
        except Exception:
            mkt_title = None
    if not mkt_title:
        try:
            mkt_title = store.r.get("news:symbol:MARKET:last_seen_title")
        except Exception:
            mkt_title = None
    mkt_title_s = _decode(mkt_title)
    news = {
        "market_last_ts": int(mkt_last_s) if (mkt_last_s or "").isdigit() else None,
        "market_state": _news_state(mkt_age),
        "market_last_title": (mkt_title_s.strip() if isinstance(mkt_title_s, str) and mkt_title_s.strip() else None),
    }

    fut_bias = None
    fut_regime = None
    fut_vol_mult = None
    fut_updated = None
    try:
        raw = store.r.get("fut:scores")
        if raw:
            obj = _safe_json_load(raw) or {}
            fut_bias = _coerce_futures_bias(obj)
            fut_regime = str(obj.get("regime") or "").strip().upper() or None
            fut_vol_mult = _safe_float(obj.get("vol_mult"))
            fut_updated = obj.get("ts_utc") or obj.get("updated_utc") or None
    except Exception:
        pass

    # Futures heartbeat/status (best-effort; enables ops banner without reading fut:* directly).
    hb_ts = None
    hb_msg = None
    status_state = None
    status_note = None
    try:
        hb_ts = _safe_int(store.r.get("fut:hb"))
    except Exception:
        hb_ts = None
    try:
        hb_msg = _decode(store.r.get("fut:hb_msg"))
    except Exception:
        hb_msg = None
    try:
        st = _safe_json_load(store.r.get("fut:status")) or {}
        status_state = str(st.get("state") or "").strip().lower() or None
        status_note = str(st.get("note") or st.get("last_error") or "").strip() or None
    except Exception:
        status_state = None
        status_note = None

    hb_age_sec = None
    if hb_ts is not None:
        try:
            hb_age_sec = max(0, int(now - int(hb_ts)))
        except Exception:
            hb_age_sec = None

    stale_sec = max(10, _env_int("FUTURES_HB_STALE_SEC", 60))
    degraded = False
    if hb_age_sec is not None and hb_age_sec > stale_sec:
        degraded = True
    hb_msg_low = (str(hb_msg or "") or "").strip().lower()
    if hb_msg_low in {"auth_error", "rate_limited", "symbology_error"}:
        degraded = True
    if (status_state or "") == "error":
        degraded = True

    return {
        "ctx_version": CTX_VERSION,
        "ts_utc": now,
        "sources_present": {
            "futures": fut_bias is not None,
            "news": bool(news),
        },
        "futures": {
            "bias": fut_bias,
            "regime": fut_regime,
            "vol_mult": fut_vol_mult,
            "updated_utc": fut_updated,
            "hb_ts": hb_ts,
            "hb_age_sec": hb_age_sec,
            "hb_msg": (hb_msg.strip() if isinstance(hb_msg, str) and hb_msg.strip() else None),
            "status_state": status_state,
            "status_note": status_note,
            "degraded": bool(degraded),
        },
        "news": news,
    }
