from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore


ET = ZoneInfo("America/New_York")


def _ask_debug_enabled() -> bool:
    return (os.getenv("TNT_ASK_DEBUG", "0") or "0").strip() == "1"


def _ask_dbg(tag: str, msg: str) -> None:
    if not _ask_debug_enabled():
        return
    try:
        print(f"[TNT][ASK]{tag} {msg}")
    except Exception:
        pass


@dataclass(frozen=True)
class AskConfig:
    ask_channel_id: int
    cooldown_sec: int = 6
    enabled: bool = False


def ask_channel_only_enabled() -> bool:
    return (os.getenv("TNT_ASK_CHANNEL_ONLY_ENABLED", "0") or "0").strip() == "1"


def load_ask_config() -> AskConfig:
    raw = (os.getenv("TNT_ASK_CHANNEL_ID") or os.getenv("ASK_TNT_CHANNEL_ID") or "0").strip()
    try:
        cid = int(raw)
    except Exception:
        cid = 0

    raw_cd = (os.getenv("TNT_ASK_CHANNEL_COOLDOWN_SEC") or os.getenv("TNT_ASK_COOLDOWN_SEC") or "6").strip()
    try:
        cd = int(raw_cd)
    except Exception:
        cd = 6

    return AskConfig(ask_channel_id=max(int(cid), 0), cooldown_sec=max(int(cd), 0), enabled=ask_channel_only_enabled())


def is_ask_channel(channel, *, cfg: Optional[AskConfig] = None, strict: bool = False) -> bool:
    cfg = cfg or load_ask_config()

    try:
        ch_id = int(getattr(channel, "id", 0) or 0)
    except Exception:
        ch_id = 0

    if cfg.ask_channel_id > 0 and ch_id == int(cfg.ask_channel_id):
        return True

    if strict:
        return False

    # Back-compat + tests: allow the canonical channel name when channel-only mode is off.
    # When channel-only mode is enabled and an id is configured, do NOT fall back to name.
    if bool(cfg.enabled) and cfg.ask_channel_id > 0:
        return False

    try:
        name = str(getattr(channel, "name", "") or "").strip().lower()
    except Exception:
        name = ""

    if name in {"ask-tnt", "ask_tnt", "ask tnt"}:
        return True

    return False


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def _intent(text: str) -> str:
    t = (text or "").lower().strip()

    # stand down / risk / regime
    # Priority: if the user is explicitly asking posture / stand-down, answer with the posture card
    # even if they mention CPI/FOMC/etc.
    if any(
        k in t
        for k in (
            "stand down",
            "stand-down",
            "blackout",
            "risk",
            "regime",
            "posture",
            "safe",
            "no trade",
            "do nothing",
            "should we trade",
            "should i trade",
        )
    ):
        return "macro_posture"

    # macro timing / events
    if any(
        k in t
        for k in (
            "next",
            "when",
            "upcoming",
            "schedule",
            "calendar",
            "48h",
            "48 h",
            "2 days",
            "two days",
            "cpi",
            "fomc",
            "powell",
            "gdp",
            "nfp",
            "jobs report",
            "ppi",
            "ism",
            "pmi",
            "confidence",
        )
    ):
        return "macro_events"

    # rates
    if any(k in t for k in ("rates", "yield", "10y", "10-year", "2y", "2-year", "curve", "inverted", "2s10s", "treasury")):
        return "rates"

    # inflation
    if any(k in t for k in ("inflation", "core", "pce", "breakeven", "5y5y", "expectations")):
        return "inflation"

    # labor
    if any(k in t for k in ("labor", "jobs", "unemployment", "wages", "jolts", "participation")):
        return "labor"

    return "help"


def _cooldown_key(user_id: int) -> str:
    return f"ask:cooldown:{int(user_id)}"


def _fmt_bp(spread_percent_points: Optional[float]) -> str:
    if spread_percent_points is None:
        return "?"
    try:
        return f"{int(round(float(spread_percent_points) * 100.0)):+d}bp"
    except Exception:
        return "?"


def _fmt_pct(v: Optional[float], *, dp: int = 2) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.{int(dp)}f}%"
    except Exception:
        return "?"


def _redis_get_json(redis_client, key: str) -> Optional[Dict[str, Any]]:
    try:
        raw = redis_client.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _econ_key(series: str) -> str:
    try:
        from tnt_redis import rkey as _rkey

        return _rkey("econ", "latest", str(series))
    except Exception:
        return f"econ:latest:{str(series)}"


def _cooldown_allow(redis_client, *, user_id: int, ttl_s: int) -> bool:
    ttl = max(int(ttl_s), 0)
    if ttl <= 0:
        return True

    try:
        ok = redis_client.set(_cooldown_key(user_id), "1", ex=int(ttl), nx=True)
        return bool(ok)
    except Exception:
        # In a failure, allow rather than dead-silencing the channel.
        return True


def _get_or_compute_regime(redis_client, *, now_utc: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from services.macro_regime import compute_and_cache_macro_regime, get_cached_macro_regime

    reg = None
    try:
        reg = get_cached_macro_regime(redis_client)
    except Exception:
        reg = None

    if isinstance(reg, dict) and reg.get("ok"):
        return reg

    tre = _redis_get_json(redis_client, _econ_key("treasury_yields"))
    inf = _redis_get_json(redis_client, _econ_key("inflation"))
    iex = _redis_get_json(redis_client, _econ_key("inflation_expectations"))
    lab = _redis_get_json(redis_client, _econ_key("labor_market"))

    if not any([tre, inf, iex, lab]):
        return None

    try:
        return compute_and_cache_macro_regime(
            redis_client,
            treasury_yields=tre,
            inflation=inf,
            inflation_expectations=iex,
            labor_market=lab,
            updated_utc=now_utc,
        )
    except Exception:
        return None


def _fmt_event_dt(dt: datetime) -> str:
    # Windows-safe formatting.
    return dt.strftime("%a %b %d %I:%M %p ET").replace(" 0", " ")


def _fmt_time_only_et(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0") + " ET"


def _fmt_day_time_et(dt: datetime) -> str:
    dow = dt.strftime("%a")
    tm = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dow} {tm} ET"


def _fmt_countdown(dt: datetime, now_et: datetime) -> str:
    try:
        sec = float((dt - now_et).total_seconds())
    except Exception:
        return "(T-?)"

    if sec <= 0:
        return "(T-0m)"

    d = int(sec // 86400)
    h = int((sec % 86400) // 3600)
    m = int((sec % 3600) // 60)

    if d > 0:
        return f"(T-{d}d {h}h)"
    if h > 0:
        return f"(T-{h}h {m}m)"
    return f"(T-{max(m, 1)}m)"


def _payload_values(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    v = payload.get("values")
    return v if isinstance(v, dict) else {}


def _safe_float(x: object) -> Optional[float]:
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


def _get_val(d: Dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
    for k in keys:
        if k in d:
            v = _safe_float(d.get(k))
            if v is not None:
                return v
    return None


def _fmt_plain(v: Optional[float], *, dp: int = 1) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.{int(dp)}f}"
    except Exception:
        return "?"


def _fmt_jolts(v: Optional[float]) -> str:
    if v is None:
        return "?"
    try:
        x = float(v)
    except Exception:
        return "?"
    # Many sources report job openings in millions already (e.g., 8.7).
    if 0.0 <= x <= 50.0:
        return f"{x:.1f}"
    if x >= 1_000_000:
        return f"{x/1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x/1_000:.1f}k"
    return f"{x:.0f}"


def format_macro_posture_reply(snapshot: Dict[str, Any]) -> str:
    posture = str(snapshot.get("posture") or "UNKNOWN").upper().strip() or "UNKNOWN"

    lines: list[str] = []
    lines.append(f"**TNT Macro Posture: {posture}**")

    # Optional guidance-first ordering for STAND DOWN.
    guidance = str(snapshot.get("guidance") or "").strip()
    if posture == "STAND DOWN" and guidance:
        lines.append(guidance)

    lines.append(str(snapshot.get("rates_line") or "Rates: 10Y ?% | 2Y ?% | 2s10s ?"))
    lines.append(str(snapshot.get("inflation_line") or "Inflation: CPI YoY ? | Core ? | PCE Core ?"))
    lines.append(str(snapshot.get("expectations_line") or "Expectations: 5y5y ? | 10y BE ?"))
    lines.append(str(snapshot.get("labor_line") or "Labor: U ? | Wages ? | JOLTS ?"))
    lines.append("")
    lines.append(str(snapshot.get("next_line") or "Next catalyst: ? | Blackout: ?"))

    if posture != "STAND DOWN" and guidance:
        lines.append(guidance)
    elif posture == "STAND DOWN" and not guidance:
        lines.append("Guidance: Stand down until after the release window. Trade only post-release structure.")

    return "\n".join([ln for ln in lines if ln is not None]).strip()


def format_next_events_reply(events, blackout: Optional[Dict[str, Any]], *, now_et: datetime) -> str:
    lines: list[str] = ["**Next macro catalysts (ET)**"]

    if events:
        for e in events[:6]:
            dt = getattr(e, "dt_et", None)
            if not isinstance(dt, datetime):
                continue
            title = str(getattr(e, "title", "") or "").strip() or "(event)"
            imp = str(getattr(e, "importance", "") or "").upper().strip() or "MED"
            dow = dt.strftime("%a")
            tm = dt.strftime("%I:%M %p").lstrip("0")
            cd = _fmt_countdown(dt, now_et)
            lines.append(f"- {title} — {dow} {tm} {cd} {imp}")
    else:
        lines.append("- (no events found)")

    if blackout and bool(blackout.get("active")):
        ends = str(blackout.get("blackout_ends_et") or "?")
        lines.append(f"Blackout: ACTIVE until {ends}")

    return "\n".join(lines).strip()


def build_macro_reply(redis_client, question: str) -> str:
    """Deterministic answer from cached truth (Redis)."""

    from services.macro_calendar import get_next_macro_events, macro_blackout_state

    q = (question or "").strip()
    intent = _intent(q)

    now = _now_et()
    blackout = macro_blackout_state(now)
    events = get_next_macro_events(now, horizon_days=14)

    _ask_dbg(
        "[ROUTE]",
        f"intent={intent} events_n={len(events) if isinstance(events, list) else '?'} blackout_active={int(bool(blackout and blackout.get('active')))}",
    )

    if intent == "macro_events":
        return format_next_events_reply(events, blackout, now_et=now)

    # Everything else benefits from regime + levels.
    reg = _get_or_compute_regime(redis_client)
    if not reg:
        try:
            from services.macro_regime import get_cached_macro_regime

            has_regime = bool(get_cached_macro_regime(redis_client))
        except Exception:
            has_regime = False

        # Best-effort cache presence hints.
        tre = _redis_get_json(redis_client, _econ_key("treasury_yields"))
        inf0 = _redis_get_json(redis_client, _econ_key("inflation"))
        iex0 = _redis_get_json(redis_client, _econ_key("inflation_expectations"))
        lab0 = _redis_get_json(redis_client, _econ_key("labor_market"))
        _ask_dbg(
            "[CACHE]",
            f"macro_regime_present={int(has_regime)} econ_treasury={int(bool(tre))} econ_inflation={int(bool(inf0))} econ_expectations={int(bool(iex0))} econ_labor={int(bool(lab0))}",
        )

        if intent in {"rates", "inflation", "labor", "macro_posture", "help"}:
            return (
                "**TNT Macro Posture: UNKNOWN**\n"
                "Data is warming (econ cache missing). Try again in ~1 minute or run /macro_refresh (admin)."
            ).strip()
        return (
            "Ask me things like:\n"
            "- next CPI/FOMC/NFP?\n"
            "- stand down? / macro posture?\n"
            "- rates/curve?\n"
            "- inflation snapshot?\n"
            "- labor snapshot?"
        )

    posture = str(reg.get("posture") or "").upper().replace("_", " ").strip() or "UNKNOWN"
    macro_risk = str(reg.get("macro_risk") or "").upper().strip() or "?"

    _ask_dbg("[CACHE]", f"macro_regime_present=1 posture={posture} macro_risk={macro_risk}")

    levels = reg.get("levels") if isinstance(reg.get("levels"), dict) else {}
    y10 = levels.get("yield_10y") if isinstance(levels, dict) else None
    y2 = levels.get("yield_2y") if isinstance(levels, dict) else None
    spr = reg.get("spread_2s10s")

    cpi_yoy = levels.get("cpi_yoy") if isinstance(levels, dict) else None
    core = levels.get("core_cpi") if isinstance(levels, dict) else None
    exp_5y5y = levels.get("exp_5y5y") if isinstance(levels, dict) else None

    unemp = levels.get("unemployment_rate") if isinstance(levels, dict) else None

    # Enrich with best-effort extra fields from cached econ blobs.
    inf = _redis_get_json(redis_client, _econ_key("inflation"))
    iex = _redis_get_json(redis_client, _econ_key("inflation_expectations"))
    lab = _redis_get_json(redis_client, _econ_key("labor_market"))
    inf_v = _payload_values(inf)
    iex_v = _payload_values(iex)
    lab_v = _payload_values(lab)

    pce_core = _get_val(inf_v, ("pce_core", "pce_core_yoy", "pce_core_year_over_year", "core_pce", "core_pce_yoy"))
    be10 = _get_val(iex_v, ("breakeven_10_year", "breakeven_10y", "ten_year_breakeven", "inflation_breakeven_10y", "10y_breakeven"))
    wages = _get_val(lab_v, ("wage_growth", "wage_growth_yoy", "avg_hourly_earnings_yoy", "ahe_yoy", "wages_yoy"))
    jolts = _get_val(lab_v, ("jolts", "jolts_job_openings", "job_openings", "job_openings_total"))

    def _pct_like_or_none(v: object, *, max_abs: float = 25.0) -> float | None:
        try:
            if v is None:
                return None
            x = float(v)
            if not math.isfinite(x):
                return None
            if abs(x) > float(max_abs):
                return None
            return x
        except Exception:
            return None

    # Guardrail: the regime snapshot may hold inflation *index* levels (e.g., ~330)
    # rather than YoY % values; don't render those as percentages.
    cpi_yoy = _pct_like_or_none(cpi_yoy)
    core = _pct_like_or_none(core)
    pce_core = _pct_like_or_none(pce_core)

    # macro_posture/help (and also rates/inflation/labor): canonical posture card.
    stand_down = False
    if blackout and bool(blackout.get("active")) and str(blackout.get("importance") or "").upper().strip() in {"HIGH", "MED"}:
        stand_down = True
    if posture == "STAND DOWN" or macro_risk == "HIGH":
        stand_down = True

    posture_out = "STAND DOWN" if stand_down else ("CAUTIOUS" if posture == "CAUTIOUS" else "NORMAL")

    # Next catalyst line (single top event).
    next_event = events[0] if events else None
    if next_event is not None and isinstance(getattr(next_event, "dt_et", None), datetime):
        dt = next_event.dt_et
        title = str(getattr(next_event, "title", "") or "").strip() or "(event)"
        next_txt = f"Next catalyst: {title} {_fmt_day_time_et(dt)} {_fmt_countdown(dt, now)}"
    else:
        next_txt = "Next catalyst: (none found)"

    blackout_on = "ON" if (blackout and bool(blackout.get("active"))) else "OFF"
    next_line = f"{next_txt}  | Blackout: {blackout_on}"

    # Guidance line.
    guidance = "Guidance: Prefer post-event structure; avoid pre-release hero trades."
    if posture_out == "STAND DOWN":
        until = None
        if blackout and bool(blackout.get("active")):
            until = str(blackout.get("blackout_ends_et") or "").strip() or None
        if not until and next_event is not None and isinstance(getattr(next_event, "dt_et", None), datetime):
            until = _fmt_day_time_et(next_event.dt_et)
        if until:
            guidance = f"Guidance: Stand down until {until}. Trade only post-release structure."
        else:
            guidance = "Guidance: Stand down until after the release window. Trade only post-release structure."
    elif posture_out == "CAUTIOUS":
        guidance = "Guidance: Size down pre-release; prefer post-event structure."

    snapshot = {
        "posture": posture_out,
        "rates_line": f"Rates: 10Y {_fmt_pct(y10)} | 2Y {_fmt_pct(y2)} | 2s10s {_fmt_bp(spr)}",
        "inflation_line": f"Inflation: CPI YoY {_fmt_pct(cpi_yoy, dp=1)} | Core {_fmt_pct(core, dp=1)} | PCE Core {_fmt_pct(pce_core, dp=1)}",
        "expectations_line": f"Expectations: 5y5y {_fmt_pct(exp_5y5y, dp=2)} | 10y BE {_fmt_pct(be10, dp=2)}",
        "labor_line": f"Labor: U {_fmt_pct(unemp, dp=1)} | Wages {_fmt_pct(wages, dp=1)} | JOLTS {_fmt_jolts(jolts)}",
        "next_line": next_line,
        "guidance": guidance,
    }
    return format_macro_posture_reply(snapshot)


async def handle_ask_channel_message(message, bot, redis_client) -> bool:
    """Ask-TNT channel router. Returns True if handled (including silent cooldown ignore)."""

    cfg = load_ask_config()
    if not bool(cfg.enabled):
        return False
    if cfg.ask_channel_id <= 0:
        return False

    # Strict: only trigger in the configured channel id.
    ch_obj = getattr(message, "channel", None)
    ch_id = None
    try:
        ch_id = int(getattr(ch_obj, "id", 0) or 0)
    except Exception:
        ch_id = None

    match = is_ask_channel(ch_obj, cfg=cfg, strict=True)
    _ask_dbg("[MATCH]", f"channel_id={ch_id} expected={cfg.ask_channel_id} match={int(bool(match))}")

    if not match:
        return False

    try:
        q0 = (getattr(message, "content", None) or "").strip()
        print(
            "[TNT][ASK][ROUTE] "
            + f"channel_id={int(ch_id or 0)} ask_channel_id={int(cfg.ask_channel_id)} match=1 len={len(q0)}"
        )
    except Exception:
        pass

    try:
        if bool(getattr(getattr(message, "author", None), "bot", False)):
            return False
    except Exception:
        return False

    q = (getattr(message, "content", None) or "").strip()
    if not q:
        return False

    _ask_dbg("[MSG]", f"user_id={int(getattr(getattr(message, 'author', None), 'id', 0) or 0)} len={len(q)}")

    # Guard: avoid huge copy-pastes.
    if len(q) > 1200:
        return True

    # Per-user cooldown (silently ignore during cooldown).
    try:
        allowed = _cooldown_allow(redis_client, user_id=int(message.author.id), ttl_s=int(cfg.cooldown_sec))
        _ask_dbg("[COOLDOWN]", f"allowed={int(bool(allowed))} ttl_s={int(cfg.cooldown_sec)}")
        if not allowed:
            return True
    except Exception:
        pass

    reply = build_macro_reply(redis_client, q)
    _ask_dbg("[REPLY]", "\n" + str(reply))
    try:
        await message.reply(reply, mention_author=False)
    except Exception:
        pass

    return True
