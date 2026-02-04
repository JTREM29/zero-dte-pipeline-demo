from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Optional, Sequence, Set, Tuple, TYPE_CHECKING

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore


if TYPE_CHECKING:  # pragma: no cover
    import discord


ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class AskDecision:
    handled: bool
    reply: Optional[str] = None


def _env_flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip() == "1"


def _parse_int_set(raw: str) -> Set[int]:
    out: Set[int] = set()
    for part in (raw or "").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            continue
    return out


def _allowed_channel(channel_id: int) -> bool:
    raw = (os.getenv("TNT_ASK_ALLOWED_CHANNEL_IDS") or "").strip()
    if not raw:
        return True
    allowed = _parse_int_set(raw)
    return (channel_id in allowed) if allowed else True


def _cooldown_sec() -> int:
    raw = (os.getenv("TNT_ASK_COOLDOWN_SEC") or "").strip()
    try:
        v = int(raw)
        return max(v, 0)
    except Exception:
        return 8


def _strip_bot_mention(text: str, bot_user_id: int) -> str:
    t = text or ""
    # <@123> or <@!123>
    t = re.sub(rf"^\s*<@!?{bot_user_id}>\s*", "", t)
    return t.strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def detect_intent(query: str) -> str:
    t = _norm(query)
    if not t:
        return "help"

    # Priority: explicit next/when -> events.
    if any(k in t for k in ("next", "upcoming", "what's next", "whats next", "in the next", "48h", "48 h", "2 days", "two days")):
        if any(k in t for k in ("cpi", "fomc", "powell", "gdp", "nfp", "jobs report", "ppi", "ism", "pmi", "consumer confidence")):
            return "macro_events"

    # Explicit blackout / stand down asks.
    if any(k in t for k in ("blackout", "stand down", "stand-down", "do nothing", "risk off", "halt")):
        return "macro_blackout"

    # Regime / posture.
    if any(k in t for k in ("regime", "posture", "macro regime", "macro posture", "macro risk", "risk score")):
        return "macro_regime"

    # Rates / curve.
    if any(k in t for k in ("rates", "yield", "10y", "10-year", "2y", "2-year", "curve", "2s10s", "inverted", "steep", "flat")):
        return "rates"

    # Inflation.
    if any(k in t for k in ("inflation", "cpi", "core", "pce", "breakeven", "5y5y", "five year five year")):
        return "inflation"

    # Labor.
    if any(k in t for k in ("unemployment", "jobs", "wages", "jolts", "nfp")):
        return "labor"

    # Macro event keywords fall back to events.
    if any(k in t for k in ("cpi", "fomc", "powell", "gdp", "nfp", "jobs report", "ppi", "ism", "pmi", "consumer confidence")):
        return "macro_events"

    return "help"


def _fmt_pct(v: Optional[float], *, dp: int = 2) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v):.{int(dp)}f}%"
    except Exception:
        return "?"


def _fmt_bp(v: Optional[float]) -> str:
    if v is None:
        return "?"
    try:
        return f"{float(v) * 100.0:+.0f}bp"
    except Exception:
        return "?"


def _fmt_days(delta: timedelta) -> str:
    try:
        sec = float(delta.total_seconds())
    except Exception:
        return "?"
    if sec < 0:
        return "T+"
    d = int(sec // 86400)
    if d == 0:
        return "T-0d"
    return f"T-{d}d"


def _econ_key(series: str) -> str:
    try:
        from tnt_redis import rkey as _rkey

        return _rkey("econ", "latest", str(series))
    except Exception:
        return f"econ:latest:{str(series)}"


def _redis_get_json(redis_client, key: str) -> Optional[Dict[str, Any]]:
    try:
        raw = redis_client.get(key)
    except Exception:
        return None
    if not raw:
        return None
    try:
        import json

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _get_or_compute_regime(redis_client) -> Optional[Dict[str, Any]]:
    from services.macro_regime import compute_and_cache_macro_regime, get_cached_macro_regime

    reg = None
    try:
        reg = get_cached_macro_regime(redis_client)
    except Exception:
        reg = None
    if isinstance(reg, dict) and reg.get("ok"):
        return reg

    # Fallback: compute from cached econ blobs.
    tre = _redis_get_json(redis_client, _econ_key("treasury_yields"))
    inf = _redis_get_json(redis_client, _econ_key("inflation"))
    iex = _redis_get_json(redis_client, _econ_key("inflation_expectations"))
    lab = _redis_get_json(redis_client, _econ_key("labor_market"))
    if not any([tre, inf, iex, lab]):
        return None

    try:
        # This writes `macro:regime:v1` so future asks are cheaper.
        return compute_and_cache_macro_regime(redis_client, treasury_yields=tre, inflation=inf, inflation_expectations=iex, labor_market=lab)
    except Exception:
        return None


def _next_events_lines(now_et: datetime, *, horizon_hours: int = 48, limit: int = 4) -> Tuple[Sequence[str], Optional[dict[str, Any]]]:
    from services.macro_calendar import get_next_macro_events, macro_blackout_state

    events = get_next_macro_events(now_et, horizon_days=max(2, int((horizon_hours + 23) // 24)))
    end = now_et + timedelta(hours=int(horizon_hours))
    evs = [e for e in events if now_et <= e.dt_et <= end]
    evs.sort(key=lambda e: e.dt_et)

    lines: list[str] = []
    for e in evs[: max(int(limit), 0)]:
        dt = e.dt_et
        dow = dt.strftime("%a")
        # Windows-safe: avoid %-I.
        tm = dt.strftime("%I:%M %p").lstrip("0")
        imp = str(getattr(e, "importance", "")).upper().strip() or "MED"
        lines.append(f"{e.title} — {dow} {tm} ET ({imp})")

    blackout = None
    try:
        blackout = macro_blackout_state(now_et)
    except Exception:
        blackout = None

    return lines, blackout


def format_macro_regime_reply(*, query: str, regime: Dict[str, Any], now_et: datetime) -> str:
    posture = str(regime.get("posture") or "").upper().replace("_", " ").strip() or "UNKNOWN"
    macro_risk = str(regime.get("macro_risk") or "").upper().strip() or "?"

    levels = regime.get("levels") if isinstance(regime.get("levels"), dict) else {}
    y10 = levels.get("yield_10y") if isinstance(levels, dict) else None
    y2 = levels.get("yield_2y") if isinstance(levels, dict) else None
    spr = regime.get("spread_2s10s")

    cpi = levels.get("cpi_yoy") if isinstance(levels, dict) else None
    core = levels.get("core_cpi") if isinstance(levels, dict) else None
    exp_5y5y = levels.get("exp_5y5y") if isinstance(levels, dict) else None

    unemp = levels.get("unemployment_rate") if isinstance(levels, dict) else None

    curve_shape = str(regime.get("curve_shape") or "").upper().strip() or "?"

    ev_lines, blackout = _next_events_lines(now_et)

    parts: list[str] = []
    parts.append(f"TNT Macro Posture: {posture} (risk={macro_risk})")
    parts.append(f"Rates: 10Y {_fmt_pct(y10)}, 2Y {_fmt_pct(y2)} (2s10s {_fmt_bp(spr)}, {curve_shape})")
    parts.append(f"Inflation: CPI YoY {_fmt_pct(cpi, dp=1)}, Core {_fmt_pct(core, dp=1)}")
    parts.append(f"Expectations: 5y5y {_fmt_pct(exp_5y5y, dp=2)}")
    parts.append(f"Labor: Unemployment {_fmt_pct(unemp, dp=1)}")

    if blackout and bool(blackout.get("active")):
        title = str(blackout.get("title") or "macro event").strip() or "macro event"
        ends = str(blackout.get("blackout_ends_et") or "?")
        parts.append(f"Blackout: ACTIVE • {title} • ends {ends}")

    if ev_lines:
        parts.append("Next catalysts (ET):")
        parts.extend([f"- {ln}" for ln in ev_lines])

    # Deterministic guidance line.
    guidance = "Guidance: Standard risk; respect blackout windows."
    if blackout and bool(blackout.get("active")):
        guidance = "Guidance: In blackout — stand down until after the release window."
    elif posture == "STAND DOWN":
        guidance = "Guidance: Stand down — avoid pre-macro fades; reassess after catalysts."
    elif posture == "CAUTIOUS":
        guidance = "Guidance: Size down pre-release; prefer post-event structure."

    parts.append(guidance)
    return "\n".join(parts).strip()


def format_macro_events_reply(*, now_et: datetime) -> str:
    ev_lines, blackout = _next_events_lines(now_et)
    parts: list[str] = []
    parts.append("Next macro catalysts (ET)")
    if ev_lines:
        parts.extend([f"- {ln}" for ln in ev_lines])
    else:
        parts.append("- None in the next 48h (schedule horizon empty).")

    if blackout and bool(blackout.get("active")):
        title = str(blackout.get("title") or "macro event").strip() or "macro event"
        parts.append(f"Blackout: ACTIVE • {title}")
    else:
        parts.append("Stand-down windows auto-trigger around HIGH events.")

    return "\n".join(parts).strip()


def format_blackout_reply(*, now_et: datetime) -> str:
    from services.macro_calendar import macro_blackout_state

    state = macro_blackout_state(now_et)
    if state and bool(state.get("active")):
        title = str(state.get("title") or "macro event").strip() or "macro event"
        ends = str(state.get("blackout_ends_et") or "?")
        return f"Macro blackout: ACTIVE • {title} • ends {ends}"

    # If no blackout, still show next catalysts.
    return format_macro_events_reply(now_et=now_et)


def format_help_reply() -> str:
    return "Ask me about: macro posture/regime, next CPI/FOMC/NFP (48h), rates/curve, inflation, or blackout/stand-down.".strip()


def _cooldown_key(user_id: int) -> str:
    return f"ask:cooldown:{int(user_id)}"


def _cooldown_allow(redis_client, *, user_id: int, ttl_s: int) -> bool:
    ttl = max(int(ttl_s), 0)
    if ttl <= 0:
        return True

    # Redis atomic: SET key 1 EX ttl NX
    try:
        ok = redis_client.set(_cooldown_key(user_id), "1", ex=int(ttl), nx=True)
        return bool(ok)
    except Exception:
        # Fallback: in-process cooldown.
        now = time.monotonic()
        if not hasattr(_cooldown_allow, "_mem"):
            setattr(_cooldown_allow, "_mem", {})
        mem = getattr(_cooldown_allow, "_mem")
        if not isinstance(mem, dict):
            mem = {}
            setattr(_cooldown_allow, "_mem", mem)
        last = float(mem.get(user_id) or 0.0)
        if (now - last) < float(ttl):
            return False
        mem[user_id] = now
        return True


async def _is_reply_to_bot(message: "discord.Message", *, bot_user_id: int) -> bool:
    ref = getattr(message, "reference", None)
    if ref is None:
        return False

    # Fast path when resolved.
    try:
        resolved = getattr(ref, "resolved", None)
        if resolved is not None and getattr(getattr(resolved, "author", None), "id", None) == bot_user_id:
            return True
    except Exception:
        pass

    # Best-effort fetch.
    try:
        mid = getattr(ref, "message_id", None)
        if mid:
            m = await message.channel.fetch_message(int(mid))
            return bool(getattr(getattr(m, "author", None), "id", None) == bot_user_id)
    except Exception:
        return False

    return False


async def maybe_handle_ask_mentions(bot: "discord.Client", message: "discord.Message") -> AskDecision:
    if not _env_flag("TNT_ASK_MENTIONS_ENABLED", "0"):
        return AskDecision(handled=False)

    if bot.user is None:
        return AskDecision(handled=False)

    if not _allowed_channel(int(getattr(message.channel, "id", 0) or 0)):
        return AskDecision(handled=False)

    mentioned = False
    try:
        mentioned = bool(bot.user in (getattr(message, "mentions", None) or []))
    except Exception:
        mentioned = False

    replied = False
    try:
        replied = await _is_reply_to_bot(message, bot_user_id=int(bot.user.id))
    except Exception:
        replied = False

    if not mentioned and not replied:
        return AskDecision(handled=False)

    # Ignore empty / massive messages.
    raw = (message.content or "").strip()
    if not raw or len(raw) > 800:
        return AskDecision(handled=True, reply=None)

    query = _strip_bot_mention(raw, int(bot.user.id)) if mentioned else raw
    intent = detect_intent(query)

    # Cooldown gate.
    cd_s = _cooldown_sec()
    try:
        from services.redis_env import redis_client

        r = redis_client(timeout_s=1.5, decode_responses=True)
    except Exception:
        r = None

    if r is not None:
        allowed = _cooldown_allow(r, user_id=int(message.author.id), ttl_s=cd_s)
    else:
        allowed = _cooldown_allow(object(), user_id=int(message.author.id), ttl_s=cd_s)  # type: ignore[arg-type]

    if not allowed:
        return AskDecision(handled=True, reply="⏳ Cooldown: try again in a few seconds.")

    now_et = datetime.now(tz=ET)

    if intent in {"macro_regime", "rates", "inflation", "labor"}:
        if r is None:
            return AskDecision(handled=True, reply="Macro cache unavailable right now (Redis not configured).")
        reg = _get_or_compute_regime(r)
        if not reg:
            return AskDecision(handled=True, reply="Macro cache not ready yet — try again in ~1 minute.")
        return AskDecision(handled=True, reply=format_macro_regime_reply(query=query, regime=reg, now_et=now_et))

    if intent == "macro_events":
        return AskDecision(handled=True, reply=format_macro_events_reply(now_et=now_et))

    if intent == "macro_blackout":
        return AskDecision(handled=True, reply=format_blackout_reply(now_et=now_et))

    return AskDecision(handled=True, reply=format_help_reply())
