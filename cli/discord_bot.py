"""Discord bot for serving morning briefs on demand."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from collections import OrderedDict, deque
from pathlib import Path
import threading
import inspect
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TNT_ENTRYPOINT = "cli.discord_bot"


def _tnt_mode() -> str:
    raw = (os.getenv("TNT_RUN_MODE", "dev") or "dev").strip().lower()
    return raw if raw else "dev"


def _env_onoff(name: str, default: str = "0") -> str:
    return "on" if (os.getenv(name, default) == "1") else "off"


def _print_start_banner() -> None:
    try:
        node_role = (os.getenv("TNT_NODE_ROLE") or "").strip() or "(unset)"
        print(f"[TNT][START] mode={_tnt_mode()} entrypoint={_TNT_ENTRYPOINT} pid={os.getpid()} node_role={node_role}")
    except Exception:
        pass


def _print_env_snapshot() -> None:
    # IMPORTANT: do not log secrets.
    try:
        sync_scope = (os.getenv("TNT_COMMAND_SYNC_SCOPE", "") or "").strip() or "(default)"
        allow_multi = (os.getenv("TNT_ALLOW_MULTIPLE_BOTS", "") or "").strip() or "0"
        ttl = (os.getenv("TNT_DECISION_LOCK_TTL_SEC", "900") or "900").strip()
        print(
            "[TNT][ENV] concierge={concierge} auto_concierge={auto} bot_to_bot={bot_to_bot} "
            "decision_lock={dl} decision_lock_ttl={ttl} memory={mem} sync_scope={scope} allow_multi={am}".format(
                concierge=_env_onoff("TNT_CONCIERGE_ENABLED", "0"),
                auto=_env_onoff("TNT_CONCIERGE_AUTO", "0"),
                bot_to_bot=_env_onoff("TNT_CONCIERGE_ALLOW_BOT_MESSAGES", "0"),
                dl=_env_onoff("TNT_DECISION_LOCK_ENABLED", "1"),
                ttl=ttl,
                mem=_env_onoff("TNT_CONCIERGE_MEMORY", "0"),
                scope=sync_scope,
                am=allow_multi,
            )
        )

        oi_mode = (os.getenv("TNT_OI_WORKER_MODE") or "").strip() or "(default)"
        if not oi_mode:
            oi_mode = "(default)"
        worker_url = (os.getenv("TNT_WORKER_URL") or "").strip() or "(missing)"
        sanitize_always = (os.getenv("TNT_WORKER_SANITIZE_ALWAYS") or "").strip() or "0"
        node_role = (os.getenv("TNT_NODE_ROLE") or "").strip() or "(unset)"
        print(
            f"[TNT][OI][ENV] worker_mode={oi_mode} worker_url={worker_url} sanitize_always={sanitize_always}"
        )
        print(f"[TNT][NODE] role={node_role}")
        try:
            parity_mode = _oi_worker_parity_mode()
        except Exception:
            parity_mode = "(unknown)"
        print(f"[TNT][WORKER] url={worker_url} parity_mode={parity_mode}")
    except Exception:
        pass


def _normalize_worker_base(url: str) -> str:
    """Normalize TNT worker base URL for stable keys and requests.

    Normalizes common equivalents:
    - strips trailing slashes
    - lowercases scheme + hostname
    - removes default ports (:80 for http, :443 for https)
    """

    raw = (str(url or "") or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip("/")
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return raw

        scheme = parts.scheme.lower()

        host = (parts.hostname or "").strip().lower()
        port = parts.port
        userinfo = ""
        try:
            if parts.username:
                userinfo = parts.username
                if parts.password:
                    userinfo = f"{userinfo}:{parts.password}"
                userinfo = f"{userinfo}@"
        except Exception:
            userinfo = ""

        keep_port = port
        if scheme == "http" and port == 80:
            keep_port = None
        if scheme == "https" and port == 443:
            keep_port = None

        netloc = f"{userinfo}{host}"
        if keep_port:
            netloc = f"{netloc}:{int(keep_port)}"

        normalized = urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, parts.fragment))
        return normalized.rstrip("/")
    except Exception:
        return raw


def _ensure_dir_writable(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise RuntimeError(f"failed to create dir {path}: {exc}")

    probe = path / ".tnt_write_probe"
    try:
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as exc:
        raise RuntimeError(f"dir not writable {path}: {exc}")


def _preflight_filesystem() -> None:
    # Fresh machines fail here first; make it explicit.
    for d in (Path("logs"), Path("config"), Path("cache")):
        _ensure_dir_writable(d)

import delivery.discord_bot as delivery
from delivery.on_demand_data import build_on_demand_analyze_render
from scripts.morning_brief_agent import build_morning_brief
from delivery.tnt_chart_contract import FOOTER_DISCLAIMER

# Channel-aware behavior router (paper trades ack, redirects, etc.)
try:
    from delivery.channel_router import decide_route as _decide_route
    from delivery.channel_router import router_enabled as _router_enabled
except Exception:  # noqa: BLE001
    _decide_route = None  # type: ignore[assignment]
    _router_enabled = None  # type: ignore[assignment]

# Optional: deterministic "Concierge" nudge (non-LLM).
try:
    from tnt_concierge.engine import schedule_concierge_nudge
    from tnt_concierge import throttle as concierge_throttle
except Exception:  # noqa: BLE001
    schedule_concierge_nudge = None  # type: ignore[assignment]
    concierge_throttle = None  # type: ignore[assignment]

from cli.gprr import GPRRManager, ProfileLevel, RenderProfile


_MACRO_EVENTS_FILE = (os.getenv("TNT_MACRO_EVENTS_FILE") or os.getenv("MACRO_EVENTS_FILE") or "data/macro_events.csv").strip() or "data/macro_events.csv"


def _parse_iso_utc(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        raw = str(ts).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _macro_calendar_requested_type(text: str) -> str | None:
    t = (text or "").lower()
    if not t:
        return None
    if "fomc" in t or "rate decision" in t or "fed decision" in t or "powell" in t:
        return "FOMC"
    if "cpi" in t or "consumer price" in t:
        return "CPI"
    if "ppi" in t or "producer price" in t:
        return "PPI"
    if "pce" in t or "personal consumption" in t:
        return "PCE"
    if "nonfarm" in t or "non-farm" in t or "payroll" in t or "nfp" in t:
        return "NFP"
    if "jobless" in t or "initial claims" in t or "unemployment claims" in t:
        return "JOBLESS"
    if "gdp" in t:
        return "GDP"
    if "retail sales" in t:
        return "RETAIL_SALES"
    if "pmi" in t or ("ism" in t and ("manufact" in t or "services" in t)):
        return "PMI"
    return None


def _macro_calendar_load_upcoming_from_csv(*, start_utc: datetime, end_utc: datetime, limit: int = 25) -> list[dict[str, object]]:
    path = Path(_MACRO_EVENTS_FILE)
    if not path.exists():
        return []

    out: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not isinstance(row, dict):
                    continue
                ts_utc = _parse_iso_utc(str(row.get("ts_utc") or ""))
                if ts_utc is None:
                    continue
                if ts_utc < start_utc or ts_utc > end_utc:
                    continue

                out.append(
                    {
                        "ts_utc": ts_utc,
                        "type": (str(row.get("type") or "") or "").strip().upper(),
                        "title": (str(row.get("title") or "") or "").strip(),
                        "impact": (str(row.get("impact") or "MED") or "MED").strip().upper(),
                        "source": (str(row.get("source") or "") or "csv").strip(),
                    }
                )
    except Exception:
        return []

    out.sort(key=lambda x: x.get("ts_utc") or datetime.max.replace(tzinfo=timezone.utc))
    return out[: max(1, int(limit))]


def _macro_calendar_load_upcoming_from_redis(*, start_utc: datetime, end_utc: datetime, limit: int = 25) -> list[dict[str, object]]:
    try:
        from services.redis_env import redis_client
        from services.calendar.calendar_service import CalendarService

        r = redis_client(timeout_s=2.0, decode_responses=True)
        events = CalendarService(r).macro_events_between(start_utc=start_utc, end_utc=end_utc)
    except Exception:
        return []

    out: list[dict[str, object]] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        ts_utc = _parse_iso_utc(str(ev.get("ts_utc") or ""))
        if ts_utc is None:
            continue
        typ = (str(ev.get("type") or "") or "").strip().upper()
        if not typ:
            continue
        out.append({"ts_utc": ts_utc, "type": typ, "title": "", "impact": "", "source": "redis"})

    out.sort(key=lambda x: x.get("ts_utc") or datetime.max.replace(tzinfo=timezone.utc))
    return out[: max(1, int(limit))]


def _macro_calendar_load_upcoming(*, start_utc: datetime, end_utc: datetime, limit: int = 25) -> list[dict[str, object]]:
    # Prefer CSV for display (title/impact). Redis is a fallback for gating-only installs.
    items = _macro_calendar_load_upcoming_from_csv(start_utc=start_utc, end_utc=end_utc, limit=limit)
    if items:
        return items
    return _macro_calendar_load_upcoming_from_redis(start_utc=start_utc, end_utc=end_utc, limit=limit)


def _macro_calendar_render_reply(*, query: str, now_utc: datetime | None = None) -> str | None:
    t = (query or "").strip().lower()
    if not t:
        return None

    wants_macro = any(k in t for k in ("macro", "econ", "economic", "cpi", "fomc", "nfp", "pce", "ppi", "jobless", "payroll", "gdp", "pmi", "fed"))
    if not wants_macro:
        return None

    now = now_utc or datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    items = _macro_calendar_load_upcoming(start_utc=now, end_utc=end, limit=20)

    req_type = _macro_calendar_requested_type(query)
    if req_type:
        items_t = [x for x in items if str(x.get("type") or "").upper() == req_type]
        if not items_t:
            return f"No upcoming {req_type} events found in the next 7d. (Macro calendar file: {_MACRO_EVENTS_FILE})"

        ev = items_t[0]
        ts = ev.get("ts_utc")
        if isinstance(ts, datetime):
            ts_et = ts.astimezone(getattr(delivery, "ET", timezone.utc))
            minutes = int(round((ts - now).total_seconds() / 60.0))
            title = str(ev.get("title") or req_type).strip() or req_type
            impact = str(ev.get("impact") or "").strip().upper()
            impact_txt = f" | {impact}" if impact else ""
            return f"Next {req_type}: {ts_et.strftime('%a %Y-%m-%d %H:%M ET')} ({minutes} min){impact_txt} — {title}".strip()
        return f"Next {req_type}: scheduled (time unknown)"

    if not items:
        return f"No upcoming macro events found in the next 7d. (Macro calendar file: {_MACRO_EVENTS_FILE})"

    lines: list[str] = []
    lines.append("📅 Macro calendar (next 7d, ET)")
    for ev in items[:8]:
        ts = ev.get("ts_utc")
        if not isinstance(ts, datetime):
            continue
        ts_et = ts.astimezone(getattr(delivery, "ET", timezone.utc))
        typ = str(ev.get("type") or "").strip().upper() or "(event)"
        title = str(ev.get("title") or "").strip()
        impact = str(ev.get("impact") or "").strip().upper()
        rhs = typ
        if title and title.upper() != typ:
            rhs = f"{typ} — {title}"
        if impact:
            rhs = f"{rhs} ({impact})"
        lines.append(f"• {ts_et.strftime('%a %m/%d %H:%M')} — {rhs}")

    return "\n".join(lines).strip()

OWNER_ID = int(os.getenv("DISCORD_OWNER_ID", "0"))
CANARY_ID = int(os.getenv("DISCORD_CANARY_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# IMPORTANT: `delivery.discord_bot` defines a lot of the legacy automation loops and helpers,
# but we want a *single* Discord gateway session. Re-bind the delivery module's `bot`
# reference to this instance so delivery's automation scheduler can run safely here.
try:
    delivery.bot = bot  # type: ignore[attr-defined]
except Exception:
    pass


def _route_channel_id(ch: object) -> int:
    """Return routing channel id (thread routes by parent channel id)."""

    try:
        if isinstance(ch, discord.Thread):
            return int(getattr(ch, "parent_id", 0) or 0)
    except Exception:
        pass
    try:
        return int(getattr(ch, "id", 0) or 0)
    except Exception:
        return 0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or str(default))
    except Exception:
        return float(default)


def _env_str(name: str, default: str = "") -> str:
    try:
        v = os.getenv(name, default)
        return (v or default).strip()
    except Exception:
        return str(default)


def _format_alert_trigger_line(job: dict[str, Any]) -> str:
    sym = str(job.get("symbol") or "?").strip().upper() or "?"
    tf = str(job.get("tf") or "").strip() or "?"
    ts = str(job.get("ts_utc") or "").strip() or ""
    aid = str(job.get("alert_id") or "").strip() or ""
    url = str(job.get("worker_artifact_url") or "").strip() or ""

    parts: list[str] = []
    parts.append(f"{sym} {tf}")
    if ts:
        parts.append(ts)
    if aid:
        parts.append(f"alert_id={aid}")
    if url:
        parts.append(url)
    return " — ".join(parts)


async def _alerts_delivery_loop() -> None:
    """Consumes TNT alerts delivery queue and posts to Discord.

    Producer(s): massive_service.redis_worker (alert_trigger jobs) or scripts/run_alerts_mvp_live.py
    Queue: TNT_ALERTS_DISCORD_QUEUE (default: tnt:alerts:discord_queue)
    Channel: TNT_ALERTS_CHANNEL_ID
    """

    await bot.wait_until_ready()

    if not _truthy_env("TNT_ALERTS_DISCORD_DELIVERY_ENABLED", "0"):
        print("[TNT][ALERTS][DELIVERY] disabled (TNT_ALERTS_DISCORD_DELIVERY_ENABLED=0)")
        return

    channel_id = _env_int("TNT_ALERTS_CHANNEL_ID", 0)
    if channel_id <= 0:
        print("[TNT][ALERTS][DELIVERY][WARN] missing TNT_ALERTS_CHANNEL_ID; delivery loop disabled")
        return

    queue_key = (_env_str("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue").strip()
    poll_timeout = max(1, _env_int("TNT_REDIS_BLPOP_TIMEOUT", 5))
    bundle_window_sec = max(0.0, _env_float("TNT_ALERTS_BUNDLE_WINDOW_SEC", 0.0))
    dry_run = _truthy_env("DRY_RUN", "0")

    # Create Redis client (sync) and call it via executor to avoid blocking the loop.
    # NOTE: BLPOP is a blocking command; the Redis socket timeout must exceed the BLPOP timeout.
    try:
        from services.redis_env import redis_client

        socket_timeout = max(5.0, float(poll_timeout) + 2.0)
        r = redis_client(timeout_s=socket_timeout, decode_responses=True)
        try:
            await asyncio.get_running_loop().run_in_executor(None, r.ping)
        except Exception as exc:
            print(f"[TNT][ALERTS][DELIVERY][WARN] Redis ping failed: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"[TNT][ALERTS][DELIVERY][WARN] Redis unavailable: {type(exc).__name__}: {exc}")
        return

    async def _resolve_channel() -> discord.abc.Messageable | None:
        try:
            ch = bot.get_channel(channel_id)
            if ch is not None:
                return ch
        except Exception:
            pass
        try:
            return await bot.fetch_channel(channel_id)
        except Exception as exc:
            print(f"[TNT][ALERTS][DELIVERY][WARN] fetch_channel failed id={channel_id}: {type(exc).__name__}: {exc}")
            return None

    channel = await _resolve_channel()
    if channel is None:
        print(f"[TNT][ALERTS][DELIVERY][WARN] channel not found id={channel_id}; delivery loop disabled")
        return

    print(
        "[TNT][ALERTS][DELIVERY] enabled=1 "
        f"channel_id={channel_id} queue={queue_key} poll_timeout={poll_timeout} "
        f"bundle_window_sec={bundle_window_sec} dry_run={int(bool(dry_run))}"
    )

    while not bot.is_closed():
        try:
            item = await asyncio.get_running_loop().run_in_executor(None, lambda: r.blpop([queue_key], timeout=poll_timeout))
        except Exception as exc:
            print(f"[TNT][ALERTS][DELIVERY][WARN] BLPOP failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)
            continue

        if not item:
            continue

        _q, raw = item
        if not isinstance(raw, str) or not raw.strip():
            continue

        jobs: list[dict[str, Any]] = []
        try:
            j0 = json.loads(raw)
            if isinstance(j0, dict):
                jobs.append(j0)
        except Exception:
            continue

        print(f"[TNT][ALERTS][DELIVERY][POP] queue={queue_key} n=1")

        # Optional bundling: gather more items for a short window.
        if bundle_window_sec > 0:
            t_end = time.time() + float(bundle_window_sec)
            while time.time() < t_end and len(jobs) < 12:
                try:
                    raw2 = await asyncio.get_running_loop().run_in_executor(None, lambda: r.lpop(queue_key))
                except Exception:
                    raw2 = None
                if not raw2:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    j2 = json.loads(raw2)
                    if isinstance(j2, dict):
                        jobs.append(j2)
                except Exception:
                    continue
            if len(jobs) > 1:
                print(f"[TNT][ALERTS][DELIVERY][BUNDLE] queue={queue_key} n={len(jobs)}")

        lines: list[str] = []
        for jb in jobs:
            if str(jb.get("type") or jb.get("job_type") or "") not in {"alert_trigger", "ALERT_TRIGGER"}:
                continue
            lines.append(_format_alert_trigger_line(jb))
            if len(lines) >= 12:
                break

        if not lines:
            continue

        if len(lines) == 1:
            content = "🚨 ALERT TRIGGERED — " + lines[0]
        else:
            content = "🚨 ALERTS TRIGGERED (bundle)\n" + "\n".join([f"- {ln}" for ln in lines])

        if dry_run:
            print(f"[TNT][ALERTS][DELIVERY][POSTED] DRY_RUN=1 bytes={len(content)}")
            continue

        try:
            await channel.send(content)
            print(f"[TNT][ALERTS][DELIVERY][POSTED] n={len(lines)}")
        except Exception as exc:
            print(f"[TNT][ALERTS][DELIVERY][WARN] send failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(0.5)



# --- Burst-scale throttles/caches (in-process) ---

_MAX_CHART_RENDERS = min(max(int(os.getenv("TNT_MAX_CHART_RENDERS", "2")), 1), 2)
_CHART_RENDER_SEM = asyncio.Semaphore(_MAX_CHART_RENDERS)

_START_TS = time.time()


_MPL_SPEED_CONFIGURED = False


def _configure_matplotlib_speed_flags(matplotlib: object) -> None:
    """Apply conservative matplotlib rcParams to reduce render overhead.

    Best-effort and idempotent; avoids aesthetic-impacting changes.
    """

    global _MPL_SPEED_CONFIGURED
    if _MPL_SPEED_CONFIGURED:
        return
    try:
        rc = getattr(matplotlib, "rcParams", None)
        if rc is None:
            return

        rc["path.simplify"] = True
        rc["path.simplify_threshold"] = 1.0
        rc["agg.path.chunksize"] = 10_000
    except Exception:
        return
    else:
        _MPL_SPEED_CONFIGURED = True


# Optional WebSocket (or other live) spot cache for cache warmer.
LIVE_PRICE: dict[str, dict[str, float]] = {}


def on_ws_price_update(symbol: str, price: float) -> None:
    sym = (symbol or "").strip().upper()
    if not sym:
        return
    try:
        LIVE_PRICE[sym] = {"price": float(price), "ts": float(time.time())}
    except Exception:
        pass

_GPRR: GPRRManager | None = None
_GPRR_LAST_PROFILE: RenderProfile | None = None

_RENDER_STATE_LOCK = asyncio.Lock()
_RENDER_ACTIVE = 0
_RENDER_WAITING = 0

_PNG_CACHE_MAX = max(int(os.getenv("TNT_PNG_CACHE_MAX", "128")), 0)
_PNG_CACHE_MAX_BYTES = max(int(os.getenv("TNT_PNG_CACHE_MAX_BYTES", str(128 * 1024 * 1024))), 0)


def _truthy_env(key: str, default: str = "0") -> bool:
    try:
        return (os.getenv(key, default) or "").strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _busy_cached_only_enabled() -> bool:
    return _truthy_env("TNT_BUSY_CACHED_ONLY", "1")


def _busy_queue_depth_threshold() -> int:
    try:
        v = int(os.getenv("TNT_BUSY_QUEUE_DEPTH", "6"))
    except Exception:
        v = 6
    return max(0, v)


def _busy_stale_max_age_sec() -> int:
    try:
        v = int(os.getenv("TNT_BUSY_STALE_MAX_SEC", "300"))
    except Exception:
        v = 300
    return max(0, v)


def _heartbeat_interval_sec() -> int:
    try:
        v = int(os.getenv("TNT_HEARTBEAT_INTERVAL_SEC", "60"))
    except Exception:
        v = 60
    return max(10, v)


def _heartbeat_path() -> Path:
    raw = (os.getenv("TNT_HEARTBEAT_PATH") or "logs/tnt_heartbeat.json").strip()
    if not raw:
        raw = "logs/tnt_heartbeat.json"
    return Path(raw)


def _png_cache_enabled() -> bool:
    return _PNG_CACHE_MAX > 0 and _PNG_CACHE_MAX_BYTES > 0


def _gprr_enabled() -> bool:
    try:
        return bool(_GPRR is not None and _GPRR.enabled())
    except Exception:
        return False


def _gprr_profile() -> RenderProfile:
    # Always return a safe profile.
    global _GPRR_LAST_PROFILE
    try:
        if _GPRR is not None and _GPRR.enabled():
            p = _GPRR.current_profile()
            _GPRR_LAST_PROFILE = p
            return p
    except Exception:
        pass
    if _GPRR_LAST_PROFILE is not None:
        return _GPRR_LAST_PROFILE
    # Safe default if anything goes wrong.
    return RenderProfile(ProfileLevel.DEGRADED, dpi=110, top_strikes=12, include_iv_overlay=False, allow_cache_miss_render=True, heavy_cmd_allowed=True, heavy_cooldown_mult=1.6)


ACTIVE_SYMBOLS = ["SPY", "QQQ"]
WARM_CHARTS = ["gex", "ddp"]

_OI_WARM_RR = 0

# Optional: promote-on-demand for extended OI symbols.
# If an extended symbol sees >=4 /oi requests in 60s, warm it for 10 minutes.
_OI_PROMOTED_UNTIL: dict[str, float] = {}
_OI_DEMAND_60S: dict[str, deque[float]] = {}


def _oi_note_demand(sym: str) -> None:
    s = (sym or "").strip().upper()
    if not s:
        return
    now = float(time.time())
    try:
        until = float(_OI_PROMOTED_UNTIL.get(s, 0.0))
        if until and until <= now:
            _OI_PROMOTED_UNTIL.pop(s, None)
    except Exception:
        pass

    dq = _OI_DEMAND_60S.get(s)
    if dq is None:
        dq = deque(maxlen=16)
        _OI_DEMAND_60S[s] = dq
    dq.append(now)
    cutoff = now - 60.0
    try:
        while dq and float(dq[0]) < cutoff:
            dq.popleft()
    except Exception:
        pass

    # Promote if demand is high.
    try:
        if len(dq) >= 4:
            _OI_PROMOTED_UNTIL[s] = max(float(_OI_PROMOTED_UNTIL.get(s, 0.0)), now + 600.0)
    except Exception:
        pass


def _oi_promoted_symbols(*, max_syms: int = 6) -> list[str]:
    now = float(time.time())
    out: list[str] = []
    try:
        for s, until in list(_OI_PROMOTED_UNTIL.items()):
            try:
                if float(until) <= now:
                    _OI_PROMOTED_UNTIL.pop(s, None)
                    continue
            except Exception:
                continue
            out.append(str(s))
    except Exception:
        return []
    out = sorted(set(x.strip().upper() for x in out if x and str(x).strip()))
    if max_syms > 0:
        out = out[: int(max_syms)]
    return out


def _oi_universe_hint_line() -> str:
    return f"Universe: {len(OI_UNIVERSE)} symbols (Warmed {len(OI_WARMED)} | Extended {len(OI_EXTENDED)})"


def _append_oi_universe_hint(text: str) -> str:
    t = str(text or "")
    hint = _oi_universe_hint_line()
    if not t:
        return hint
    if "Universe:" in t:
        return t
    return t + "\n" + hint


_OI_EXPIRY_HINT_CACHE: dict[tuple[str, int], tuple[float, list[str]]] = {}


def _oi_expiry_hint_ttl_sec() -> int:
    try:
        v = int(os.getenv("TNT_OI_EXPIRY_HINT_TTL_SEC", "120"))
    except Exception:
        v = 120
    return max(30, min(600, int(v)))


async def _oi_get_expiries_cached(sym: str, *, api_key: str, max_expiries: int) -> list[str]:
    s = (sym or "").strip().upper()
    n = max(1, int(max_expiries))
    ttl = float(_oi_expiry_hint_ttl_sec())
    now = float(time.time())
    k = (s, int(n))
    try:
        hit = _OI_EXPIRY_HINT_CACHE.get(k)
        if hit is not None:
            ts0, expiries0 = hit
            if (now - float(ts0)) <= ttl and isinstance(expiries0, list) and expiries0:
                return [str(x) for x in expiries0 if str(x).strip()]
    except Exception:
        pass

    expiries = await _warm_active_expiries(s, api_key=api_key, max_expiries=n)
    expiries = [str(x).strip() for x in (expiries or []) if str(x).strip()]
    try:
        _OI_EXPIRY_HINT_CACHE[k] = (now, list(expiries))
    except Exception:
        pass
    return expiries


def _oi_cache_fresh_window_min() -> int:
    try:
        v = int(os.getenv("TNT_OI_WARM_FRESH_MIN", "45"))
    except Exception:
        v = 45
    return max(5, min(240, int(v)))


def _infer_iv_percent_units(values: list[float]) -> list[float]:
    clean = [v for v in values if isinstance(v, (int, float)) and not math.isnan(float(v))]
    if not clean:
        return values
    clean_sorted = sorted(float(v) for v in clean)
    median = clean_sorted[len(clean_sorted) // 2]
    if median > 3.0:
        return [float(v) if (isinstance(v, (int, float)) and not math.isnan(float(v))) else math.nan for v in values]
    return [float(v) * 100.0 if (isinstance(v, (int, float)) and not math.isnan(float(v))) else math.nan for v in values]


def _bucket_oi_iv_by_strike_warm(df, *, sym: str) -> tuple[list[float], list[float], list[float], list[float]]:
    try:
        import pandas as pd
    except Exception:
        return [], [], [], []

    if df is None or getattr(df, "empty", True):
        return [], [], [], []

    work = df.copy()
    work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
    work["open_interest"] = pd.to_numeric(work.get("open_interest"), errors="coerce")
    work["iv"] = pd.to_numeric(work.get("iv"), errors="coerce")
    work = work.dropna(subset=["strike"]).copy()
    if work.empty:
        return [], [], [], []

    sym_up = (sym or "").strip().upper()
    bucket = 5.0 if sym_up in {"SPX", "SPXW"} else 1.0
    try:
        median_strike = float(work["strike"].median())
        if median_strike >= 1000.0:
            bucket = max(bucket, 5.0)
    except Exception:
        pass

    work["type"] = work.get("type")
    work["type"] = work["type"].astype(str).str.lower()

    oi_raw = work["open_interest"].fillna(0.0)
    work["oi_pos"] = oi_raw.where(oi_raw > 0.0, 0.0)

    work["strike_bucket"] = (work["strike"] / float(bucket)).round() * float(bucket)
    work["strike_bucket"] = pd.to_numeric(work["strike_bucket"], errors="coerce")
    work = work.dropna(subset=["strike_bucket"]).copy()
    if work.empty:
        return [], [], [], []

    strikes: list[float] = []
    oi_calls: list[float] = []
    oi_puts: list[float] = []
    iv_avgs: list[float] = []
    for strike_b, grp in work.groupby("strike_bucket"):
        try:
            strike_f = float(strike_b)
        except Exception:
            continue

        call_mask = grp["type"].eq("call")
        put_mask = grp["type"].eq("put")

        oi_c = float(grp.loc[call_mask, "oi_pos"].sum()) if bool(call_mask.any()) else 0.0
        oi_p = float(grp.loc[put_mask, "oi_pos"].sum()) if bool(put_mask.any()) else 0.0

        iv = grp["iv"]
        w = grp["oi_pos"]
        mask = (~iv.isna()) & (w > 0.0)
        if bool(mask.any()):
            iv_avg = float((iv[mask] * w[mask]).sum() / w[mask].sum())
        else:
            try:
                iv_avg = float(iv.mean())
            except Exception:
                iv_avg = math.nan

        strikes.append(strike_f)
        oi_calls.append(oi_c)
        oi_puts.append(oi_p)
        iv_avgs.append(iv_avg)

    order = sorted(range(len(strikes)), key=lambda i: strikes[i])
    strikes = [strikes[i] for i in order]
    oi_calls = [oi_calls[i] for i in order]
    oi_puts = [oi_puts[i] for i in order]
    iv_avgs = [iv_avgs[i] for i in order]
    iv_pct = _infer_iv_percent_units(iv_avgs)
    return strikes, oi_calls, oi_puts, iv_pct


def _render_oi_iv_png_warm(
    *,
    title: str,
    x_labels: list[str],
    iv_pct: list[float],
    oi_calls: list[float],
    oi_puts: list[float],
    profile: RenderProfile | None,
    include_iv_overlay: bool,
) -> bytes | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        _configure_matplotlib_speed_flags(matplotlib)
        import matplotlib.pyplot as plt
    except Exception:
        return None

    # Prefer the shared premium v2 renderer so warm-cache outputs match live/worker output
    # (and inherit the tick-label anti-clipping guards).
    try:
        from delivery.oi_iv_render import render_oi_iv_png as _render_premium_oi_iv_png

        dpi_used = 150
        try:
            dpi_used = int(os.getenv("TNT_OI_IV_DPI", "150"))
        except Exception:
            dpi_used = 150

        png = _render_premium_oi_iv_png(
            title=str(title or "").strip() or "OI/IV",
            x_labels=list(x_labels),
            iv_pct=list(iv_pct or []),
            oi_calls=list(oi_calls),
            oi_puts=list(oi_puts),
            dpi=int(dpi_used) if int(dpi_used) > 0 else 150,
            include_iv_overlay=bool(include_iv_overlay),
        )
        if isinstance(png, (bytes, bytearray)) and png:
            return bytes(png)
    except Exception:
        pass

    if not x_labels:
        return None

    n = len(x_labels)
    if len(oi_calls) != n or len(oi_puts) != n:
        return None
    if iv_pct and len(iv_pct) != n:
        return None

    xs = list(range(n))
    fig, ax = plt.subplots(figsize=(11.25, 5.4))
    fig.patch.set_facecolor("#0b0f14")
    ax.set_facecolor("#0b0f14")
    ax.tick_params(colors="#c9d1d9")
    for spine in ax.spines.values():
        spine.set_color("#2d333b")

    _add_tnt_watermark(ax)

    call_color = "#00ff66"
    put_color = "#ff3344"
    line_color = "#ffa657"

    width = 0.38
    ax.bar([x - width / 2.0 for x in xs], oi_calls, width=width, color=call_color, alpha=0.55, label="Calls OI")
    ax.bar([x + width / 2.0 for x in xs], oi_puts, width=width, color=put_color, alpha=0.48, label="Puts OI")
    ax.set_ylabel("Open interest", color="#c9d1d9")
    ax.grid(True, alpha=0.12, linestyle="--")
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")

    ax2 = None
    if bool(include_iv_overlay):
        ax2 = ax.twinx()
        ax2.set_facecolor("#0b0f14")
        ax2.tick_params(colors="#c9d1d9")
        for spine in ax2.spines.values():
            spine.set_color("#2d333b")
        ax2.plot(
            xs,
            list(iv_pct) if iv_pct else [math.nan] * n,
            color=line_color,
            linewidth=1.25,
            alpha=0.60,
            linestyle="--",
            label="IV",
        )
        ax2.set_ylabel("IV (%)", color="#c9d1d9")

    ax.set_title(title, color="#c9d1d9")
    ax.set_xlabel("Strike", color="#c9d1d9")

    # One-line takeaway (top-right). Keep it decisive.
    try:
        put_i = int(max(range(n), key=lambda i: float(oi_puts[i]) if oi_puts else 0.0))
        call_i = int(max(range(n), key=lambda i: float(oi_calls[i]) if oi_calls else 0.0))
        put_max = float(oi_puts[put_i])
        call_max = float(oi_calls[call_i])
        takeaway = ""
        if put_max >= max(1.0, call_max) * 1.15:
            takeaway = f"Put wall @ {x_labels[put_i]}"
        elif call_max >= max(1.0, put_max) * 1.15:
            takeaway = f"Call wall @ {x_labels[call_i]}"
        else:
            takeaway = "Balanced OI"
        ax.text(
            0.985,
            0.92,
            takeaway,
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=9,
            color="#c9d1d9",
            alpha=0.92,
            bbox={"facecolor": "#0b0f14", "edgecolor": "#2d333b", "alpha": 0.75, "pad": 3.0},
        )
    except Exception:
        pass

    max_ticks = 18
    step = max(1, int(math.ceil(float(n) / float(max_ticks))))
    tick_idx = list(range(0, n, step))
    if (n - 1) not in tick_idx:
        tick_idx.append(n - 1)
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([x_labels[i] for i in tick_idx], rotation=45, ha="right", color="#c9d1d9", fontsize=8)

    try:
        h1, l1 = ax.get_legend_handles_labels()
        if ax2 is not None:
            h2, l2 = ax2.get_legend_handles_labels()
        else:
            h2, l2 = [], []
        if l1 or l2:
            ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, framealpha=0.15)
    except Exception:
        pass

    fig.tight_layout()
    dpi_used = 150
    try:
        if profile is not None:
            dpi_used = int(getattr(profile, "dpi", dpi_used))
    except Exception:
        dpi_used = 150
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=int(dpi_used), metadata=_tnt_png_metadata())
    plt.close(fig)
    return buf.getvalue()


def _market_open_now() -> bool:
    try:
        now = delivery._now_et()
        if int(getattr(now, "weekday")()) >= 5:
            return False
        h = int(getattr(now, "hour"))
        m = int(getattr(now, "minute"))
        mins = (h * 60) + m
        return (9 * 60 + 30) <= mins <= (16 * 60)
    except Exception:
        return False


async def _warm_active_expiries(symbol: str, *, api_key: str, max_expiries: int = 2) -> list[str]:
    """Discover a small set of upcoming expiries (bounded, best-effort)."""
    import aiohttp

    sym = delivery._normalize_symbol_token(symbol)
    if not sym:
        return []
    underlying = delivery._map_underlying_for_options(sym)
    now_et = delivery._now_et()
    start = now_et.date().isoformat()

    # Opportunistic spot from WS (optional), else snapshot.
    underlying_px = None
    try:
        rec = LIVE_PRICE.get(sym)
        if isinstance(rec, dict) and (time.time() - float(rec.get("ts") or 0.0)) < 5.0:
            underlying_px = float(rec.get("price"))
    except Exception:
        underlying_px = None
    if underlying_px is None:
        try:
            snap = delivery._get_last_price_snapshot(sym)
            if snap and snap.px is not None:
                underlying_px = float(snap.px)
        except Exception:
            underlying_px = None

    # Keep expiry discovery bounded; match /oi default window (tighter = fewer contracts).
    strike_window_pct = 0.07
    strike_min = strike_max = None
    if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
        strike_min = float(underlying_px) * (1.0 - float(strike_window_pct))
        strike_max = float(underlying_px) * (1.0 + float(strike_window_pct))

    params: dict[str, object] = {
        "underlying_ticker": underlying,
        "expiration_date.gte": start,
        "limit": 1000,
        "apiKey": api_key,
    }
    if strike_min is not None and strike_max is not None:
        params["strike_price.gte"] = f"{strike_min:.6f}"
        params["strike_price.lte"] = f"{strike_max:.6f}"

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"{delivery._polygon_key_and_base()[1]}/v3/reference/options/contracts"
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()
    except Exception:
        return []

    if not isinstance(payload, dict) or payload.get("status") != "OK":
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    expirations: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        exp = str(item.get("expiration_date") or "").strip()
        if exp:
            expirations.add(exp)
    return sorted(expirations)[: max(1, int(max_expiries))]


async def run_cache_warm_cycle() -> None:
    api_key, _base_url, _provider = delivery._polygon_key_and_base()
    if not api_key:
        return

    # Never contend with user commands: only warm when render capacity is free.
    try:
        async with _RENDER_STATE_LOCK:
            if int(_RENDER_ACTIVE) > 0 or int(_RENDER_WAITING) > 0:
                return
    except Exception:
        return

    prof = _gprr_profile() if _gprr_enabled() else None

    # Warm core charts for SPY/QQQ (keep small).
    for sym in list(ACTIVE_SYMBOLS):
        expiries = await _warm_active_expiries(sym, api_key=api_key, max_expiries=2)
        for exp in expiries:
            for chart in list(WARM_CHARTS):
                try:
                    # Build cache keys consistent with the command handlers.
                    if chart == "oi":
                        continue
                    elif chart == "gex":
                        strike_window_pct = 0.08
                        max_contracts = 250
                        base = f"gex_png:v1:{sym}:{exp}:{int(strike_window_pct*100)}:{max_contracts}"
                        key = _gprr_cache_key(base)
                        fam = "gex"
                    else:
                        strike_window_pct = 0.08
                        max_contracts = 250
                        base = f"ddp_png:v1:{sym}:{exp}:{int(strike_window_pct*100)}:{max_contracts}"
                        key = _gprr_cache_key(base)
                        fam = "ddp"

                    existing = await _png_cache_get(key, family=fam)
                    if existing is not None and existing[0]:
                        continue

                    # Warm by invoking the same command render functions is intrusive; do a minimal render here.
                    # Only warm if render slots are available.
                    if _sem_available(_CHART_RENDER_SEM) <= 0:
                        return

                    # Note: warmer only primes cache; keep output minimal.
                    df = await delivery._fetch_polygon_options_chain_df(
                        sym,
                        expiration_ymd=exp,
                        strike_window_pct=0.08,
                        max_contracts=250,
                        concurrency=8,
                    )
                    if df is None or getattr(df, "empty", True):
                        continue

                    await _acquire_render_slot_global(family=fam, interaction=None)
                    try:
                        # Best-effort: warm only caches that have stable module-scope renderers.
                        if chart == "gex":
                            # Skip if not supported in this runtime.
                            continue
                        if chart == "ddp":
                            continue
                    finally:
                        await _release_render_slot_global()
                except Exception:
                    # Never retry aggressively.
                    continue

    # Warm-cache the 15 "warmed" OI names (+ any promoted-on-demand symbols) in a small batch per cycle.
    try:
        global _OI_WARM_RR
        promoted = [s for s in _oi_promoted_symbols(max_syms=6) if s not in set(OI_WARMED)]
        warmed = list(promoted) + list(OI_WARMED)
        if warmed:
            batch = 3 if _market_open_now() else 1
            batch = min(batch, len(warmed))
            for _ in range(batch):
                sym = warmed[int(_OI_WARM_RR) % len(warmed)]
                _OI_WARM_RR = int(_OI_WARM_RR) + 1
                expiries = await _warm_active_expiries(sym, api_key=api_key, max_expiries=2)
                for exp in expiries:
                    try:
                        top_n = 25
                        strike_window_pct = 0.07
                        max_contracts = 250
                        # Prefer absolute strike fan-out for OI (default ±$10).
                        window_abs = None
                        try:
                            window_abs = float(os.getenv("TNT_OI_WINDOW_ABS", "10"))
                        except Exception:
                            window_abs = 10.0
                        if not (
                            isinstance(window_abs, (int, float))
                            and window_abs
                            and window_abs > 0
                            and window_abs < 1_000_000
                        ):
                            window_abs = None
                        if window_abs is not None:
                            w_abs = float(window_abs)
                            window_key = f"abs{int(round(w_abs * 100.0))}"
                        else:
                            window_key = f"pct{int(round(float(strike_window_pct) * 100.0))}"

                        include_iv = bool(getattr(prof, "include_iv_overlay", True)) if prof is not None else True
                        iv_key = "iv1" if include_iv else "iv0"
                        # IMPORTANT: include a stamp-visibility bit so debug-stamped renders
                        # never poison the default (no-visible-stamp) cache.
                        truthy = {"1", "true", "yes", "y", "on"}
                        allow_stamp = (os.getenv("TNT_RENDER_STAMP_ALLOW", "0") or "0").strip().lower()
                        v_stamp = (os.getenv("TNT_RENDER_STAMP_VISIBLE", "0") or "0").strip().lower()
                        stamp_key = "sv1" if (allow_stamp in truthy and v_stamp in truthy) else "sv0"

                        base = f"oi_png:v12:{sym}:strike:{exp}:{int(top_n)}:{window_key}:{int(max_contracts)}:{iv_key}:{stamp_key}"
                        key = _gprr_cache_key(base)
                        existing = await _png_cache_get(key, family="oi")
                        if existing is not None and existing[0]:
                            continue
                        if _sem_available(_CHART_RENDER_SEM) <= 0:
                            return

                        df = await delivery._fetch_polygon_options_chain_df(
                            sym,
                            expiration_ymd=exp,
                            strike_window_pct=strike_window_pct,
                            max_contracts=max_contracts,
                            concurrency=8,
                        )
                        if df is None or getattr(df, "empty", True):
                            continue

                        strikes, oi_calls, oi_puts, iv_pct = _bucket_oi_iv_by_strike_warm(df, sym=sym)
                        if not strikes:
                            continue
                        if len(strikes) > 25:
                            totals = [float(oi_calls[i]) + float(oi_puts[i]) for i in range(len(strikes))]
                            keep = sorted(range(len(strikes)), key=lambda i: totals[i], reverse=True)[:25]
                            keep_sorted = sorted(keep, key=lambda i: strikes[i])
                            strikes = [strikes[i] for i in keep_sorted]
                            oi_calls = [oi_calls[i] for i in keep_sorted]
                            oi_puts = [oi_puts[i] for i in keep_sorted]
                            iv_pct = [iv_pct[i] for i in keep_sorted]
                        labels = [f"{s:g}" for s in strikes]

                        include_iv = bool(getattr(prof, "include_iv_overlay", True)) if prof is not None else True
                        await _acquire_render_slot_global(family="oi", interaction=None)
                        try:
                            png_bytes = await asyncio.to_thread(
                                _render_oi_iv_png_warm,
                                title=f"{sym} Options — OI by Strike + IV Overlay | Exp {exp}",
                                x_labels=labels,
                                iv_pct=iv_pct,
                                oi_calls=oi_calls,
                                oi_puts=oi_puts,
                                profile=prof,
                                include_iv_overlay=include_iv,
                            )
                        finally:
                            await _release_render_slot_global()
                        if not png_bytes:
                            continue
                        caption = f"🧾 **Options OI/IV** — **{sym}** | by **strike** | exp **{exp}** | _warm cache_"
                        meta2 = {"mode": "strike", "exp": str(exp), "caption": caption, "filename": f"{sym.lower()}_oi_strike.png"}
                        await _png_cache_set(key, (png_bytes, None, meta2), max(int(os.getenv("TNT_TTL_OI_PNG_SEC", "45")), 5))
                    except Exception:
                        continue
    except Exception:
        pass


async def cache_warmer_loop() -> None:
    while True:
        # Low priority: if users are waiting, or if we're not in EFFICIENT profile,
        # yield and try again soon. This ensures user commands always win.
        try:
            async with _RENDER_STATE_LOCK:
                waiting = int(_RENDER_WAITING)
            if waiting >= 1:
                await asyncio.sleep(10)
                continue
            if _gprr_enabled():
                p = _gprr_profile()
                lvl = ProfileLevel(int(getattr(p, "level", 0)))
                if lvl != ProfileLevel.EFFICIENT:
                    await asyncio.sleep(10)
                    continue
        except Exception:
            pass
        try:
            await run_cache_warm_cycle()
        except Exception:
            pass
        await asyncio.sleep(60 if _market_open_now() else 300)


def _gprr_banner_line() -> str | None:
    try:
        return _gprr_profile().banner_line
    except Exception:
        return None


_BUSY_BANNER_EFFICIENT = "System: Busy Mode (efficient) — reduced detail to stay responsive."
_BUSY_BANNER_DEGRADED = "System: Busy Mode (degraded) — reduced detail; some overlays may be hidden."
_BUSY_BANNER_SURVIVAL = "System: Busy Mode (survival) — cache-only for heavy charts right now. Try again in 30–60s."


def _gprr_busy_banner(profile: RenderProfile, *, cached_asof_ts: float | None = None) -> str:
    try:
        lvl = ProfileLevel(int(getattr(profile, "level", 0)))
    except Exception:
        lvl = ProfileLevel.DEGRADED

    if lvl == ProfileLevel.NORMAL:
        return ""

    if lvl == ProfileLevel.EFFICIENT:
        banner = _BUSY_BANNER_EFFICIENT
    elif lvl == ProfileLevel.SURVIVAL:
        banner = _BUSY_BANNER_SURVIVAL
    else:
        banner = _BUSY_BANNER_DEGRADED

    if cached_asof_ts is None:
        return banner
    hhmm = _format_et_hhmm(float(cached_asof_ts))
    if not hhmm:
        return banner
    return banner + "\n" + f"Served cached output (as-of {hhmm})."


def _busy_cached_only_banner(*, cached_asof_ts: float | None = None) -> str:
    """Canonical cached-only message used during load shedding.

    Prefer the current GPRR profile when available; otherwise fall back to Survival.
    """

    try:
        if _gprr_enabled():
            prof = _gprr_profile()
            return _gprr_busy_banner(prof, cached_asof_ts=cached_asof_ts)
    except Exception:
        pass

    # Non-GPRR cached-only mode (legacy busy-mode): still keep the canonical copy.
    banner = _BUSY_BANNER_SURVIVAL
    if cached_asof_ts is None:
        return banner
    hhmm = _format_et_hhmm(float(cached_asof_ts))
    if not hhmm:
        return banner
    return banner + "\n" + f"Served cached output (as-of {hhmm})."


def _append_gprr_banner(text: str) -> str:
    if not _gprr_enabled():
        return str(text or "")
    prof = _gprr_profile()
    banner = _gprr_busy_banner(prof)
    if not banner:
        return str(text or "")
    t = str(text or "")
    if banner in t:
        return t
    if not t.strip():
        return banner
    return t + "\n" + banner


def _append_gprr_banner_cached(text: str, *, cached_asof_ts: float | None) -> str:
    if not _gprr_enabled():
        return str(text or "")
    prof = _gprr_profile()
    banner = _gprr_busy_banner(prof, cached_asof_ts=cached_asof_ts)
    if not banner:
        return str(text or "")
    t = str(text or "")
    if banner in t:
        return t
    if not t.strip():
        return banner
    return t + "\n" + banner


def _file_from_png_bytes(png_bytes: bytes, *, filename: str) -> discord.File:
    return discord.File(fp=io.BytesIO(png_bytes), filename=str(filename or "chart.png"))


def _tnt_png_metadata() -> dict[str, str]:
    """Invisible PNG metadata for attribution/auditing (no visible stamp)."""

    meta: dict[str, str] = {}
    try:
        truthy = {"1", "true", "yes", "y", "on"}
        allow = (os.getenv("TNT_RENDER_STAMP_ALLOW", "0") or "0").strip().lower()
        v = (os.getenv("TNT_RENDER_STAMP_VISIBLE", "0") or "0").strip().lower()
        if allow in truthy and v in truthy:
            meta["tnt_debug"] = "1"
    except Exception:
        pass
    try:
        tag = (os.getenv("TNT_RENDER_TAG") or "").strip() or (os.getenv("COMPUTERNAME") or "").strip()
        if tag:
            meta["tnt_render_tag"] = str(tag)
    except Exception:
        pass
    try:
        build = (os.getenv("TNT_BUILD") or "").strip()
        if build:
            meta["tnt_build"] = str(build)
    except Exception:
        pass
    return meta


def _oi_worker_mode() -> str:
    """Worker policy for /oi.

    - preferred (default): use worker when healthy; fall back to local but log loudly.
    - required: if worker fails, do not render; return an error to the user.
    """

    raw = (os.getenv("TNT_OI_WORKER_MODE") or "").strip().lower()
    if raw in {"required", "require", "strict", "worker_required"}:
        return "required"
    if raw in {"preferred", "prefer", "default", ""}:
        return "preferred"

    # Back-compat toggles
    truthy = {"1", "true", "yes", "y", "on"}
    if (os.getenv("TNT_OI_WORKER_REQUIRED", "0") or "0").strip().lower() in truthy:
        return "required"
    return "preferred"


def _oi_log_worker_fallback(*, symbol: str, reason: str, key: str | None = None, worker: str | None = None) -> None:
    try:
        w = (worker or (os.getenv("TNT_WORKER_URL", "") or "")).strip()
        k = (key or "").strip()
        k_part = f" key={k}" if k else ""
        print(f"[TNT][OI][WARN] WORKER_FALLBACK reason={reason} symbol={symbol} worker={w}{k_part}")
    except Exception:
        pass


def _sanitize_worker_png_bytes(png_bytes: bytes, *, force: bool = False) -> tuple[bytes, bool, str]:
    """Best-effort: strip any visible worker tag from a returned PNG.

    Returns: (png_bytes_out, did_sanitize, reason)
    """

    try:
        truthy = {"1", "true", "yes", "y", "on"}
        allow = (os.getenv("TNT_RENDER_STAMP_ALLOW", "0") or "0").strip().lower() in truthy
        visible = (os.getenv("TNT_RENDER_STAMP_VISIBLE", "0") or "0").strip().lower() in truthy
        if allow and visible:
            return png_bytes, False, "env_debug_visible_allow"
    except Exception:
        pass

    sanitize_always = False
    try:
        truthy = {"1", "true", "yes", "y", "on"}
        sanitize_always = (os.getenv("TNT_WORKER_SANITIZE_ALWAYS", "0") or "0").strip().lower() in truthy
    except Exception:
        sanitize_always = False

    # IMPORTANT: heuristic wiping can false-positive on chart tick labels and
    # create visible artifacts (small black rectangles) in Discord screenshots.
    # Only sanitize when the worker explicitly indicates debug stamping,
    # unless an operator explicitly forces it.
    force = bool(force)

    sanitize_heuristic = False
    try:
        truthy = {"1", "true", "yes", "y", "on"}
        sanitize_heuristic = (os.getenv("TNT_WORKER_SANITIZE_HEURISTIC", "0") or "0").strip().lower() in truthy
    except Exception:
        sanitize_heuristic = False

    try:
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        img = Image.open(io.BytesIO(png_bytes))
        img.load()
        w, h = img.size
        if w <= 0 or h <= 0:
            return png_bytes, False, "bad_dimensions"

        # Only sanitize when worker explicitly indicates visible stamping (preferred)
        # or when forced by env for safety testing.
        stamp_debug = False
        stamp_pos = ""
        try:
            info = getattr(img, "text", None)
            if isinstance(info, dict):
                stamp_debug = (str(info.get("tnt_debug") or "").strip() == "1")
                stamp_pos = str(info.get("tnt_render_pos") or info.get("tnt_stamp_pos") or "").strip().lower()
        except Exception:
            stamp_debug = False
            stamp_pos = ""

        # Default: no heuristic wiping unless stamping is explicitly indicated.
        # This prevents wiping/cropping strike tick labels (the "6." fragments).
        if not (sanitize_always or force or stamp_debug or sanitize_heuristic):
            return png_bytes, False, "skip_no_stamp_meta"

        # If metadata doesn't explicitly indicate debug stamping, still attempt a
        # conservative heuristic in the bottom corners. We only wipe when a
        # text-like bright signature is detected, so unstamped images remain
        # unchanged.

        # Only wipe when we detect a text-like bright signature in that corner.
        out_img = img.convert("RGBA")

        def _bright_ratio(im: Image.Image) -> float:
            try:
                im2 = im.convert("RGBA")
                px = im2.getdata()
                total = 0
                bright = 0
                for r, g, b, a in px:
                    total += 1
                    if a < 10:
                        continue
                    # Detect light text (not necessarily pure white).
                    if max(r, g, b) >= 215:
                        bright += 1
                return float(bright) / float(max(1, total))
            except Exception:
                return 0.0

        def _corner_has_stamp(x0: int, x1: int, y0: int, y1: int) -> bool:
            try:
                box = img.crop((x0, y0, x1, y1))
                box_h = max(1, y1 - y0)
                src_y1 = max(0, y0 - 2)
                src_y0 = max(0, src_y1 - box_h)
                if src_y1 <= src_y0:
                    return False
                above = img.crop((x0, src_y0, x1, src_y1))
                rb = _bright_ratio(box)
                ra = _bright_ratio(above)
                # Require a meaningful bright-signal increase vs above-region.
                return (rb >= 0.002) and ((rb - ra) >= 0.0015)
            except Exception:
                return False

        def _wipe_box(x0: int, x1: int, y0: int, y1: int) -> bool:
            box_w = x1 - x0
            box_h = y1 - y0
            if box_w < 20 or box_h < 10:
                return False
            src_y1 = max(0, y0 - 2)
            src_y0 = max(0, src_y1 - box_h)
            if src_y1 <= src_y0:
                return False
            src = img.crop((x0, src_y0, x1, src_y1))
            out_img.paste(src, (x0, y0))
            return True

        # Target common stamp placements:
        # 1) bottom margin (legacy)
        # 2) lower plot area (some charts stamp above x-axis labels)
        bands: list[tuple[int, int, str]] = [
            (max(0, int(h * 0.955)), min(h, int(h * 0.995)), "bottom_margin"),
            (max(0, int(h * 0.80)), min(h, int(h * 0.92)), "lower_plot"),
        ]

        corners: list[str] = ["bl", "br"]
        if stamp_pos in {"bl", "bottom-left", "left"}:
            corners = ["bl"]
        elif stamp_pos in {"br", "bottom-right", "right"}:
            corners = ["br"]

        did = False
        for y0, y1, band_name in bands:
            # Use a wider box for the lower-plot band (covers longer hostnames).
            bl_x0 = max(0, int(w * 0.01))
            bl_x1 = min(w, int(w * (0.16 if band_name == "bottom_margin" else 0.22)))
            br_x1 = min(w, int(w * 0.99))
            br_x0 = max(0, int(w * (0.84 if band_name == "bottom_margin" else 0.70)))

            if "bl" in corners:
                must = sanitize_always or (force and band_name == "lower_plot")
                if must or _corner_has_stamp(bl_x0, bl_x1, y0, y1):
                    did = _wipe_box(bl_x0, bl_x1, y0, y1) or did

            if "br" in corners:
                must = sanitize_always or (force and band_name == "lower_plot")
                if must or _corner_has_stamp(br_x0, br_x1, y0, y1):
                    did = _wipe_box(br_x0, br_x1, y0, y1) or did

        if not did:
            return png_bytes, False, "no_stamp_detected"

        out = io.BytesIO()
        pnginfo = None
        try:
            pnginfo = PngInfo()
            info = getattr(img, "text", None)
            if isinstance(info, dict):
                for k, v in info.items():
                    if isinstance(k, str) and isinstance(v, str):
                        try:
                            pnginfo.add_text(k, v)
                        except Exception:
                            pass
        except Exception:
            pnginfo = None

        if pnginfo is not None:
            out_img.save(out, format="PNG", pnginfo=pnginfo)
        else:
            out_img.save(out, format="PNG")
        if sanitize_always:
            reason = "forced"
        elif force:
            reason = "forced_corners"
        else:
            reason = "sanitized"
        return out.getvalue(), True, reason
    except Exception:
        return png_bytes, False, "sanitize_exception"


def _read_png_text_metadata(png_bytes: bytes) -> dict[str, str]:
    """Best-effort read of PNG tEXt metadata (invisible attribution/version tags)."""

    try:
        from PIL import Image

        im = Image.open(io.BytesIO(png_bytes))
        im.load()
        info = getattr(im, "text", None)
        if isinstance(info, dict):
            out: dict[str, str] = {}
            for k, v in info.items():
                if isinstance(k, str) and isinstance(v, str):
                    out[k] = v
            return out
    except Exception:
        pass
    return {}


async def _worker_render_oi_iv_payload_png(payload: dict[str, object]) -> bytes:
    """Ask TNT worker to render OI/IV from a deterministic payload."""

    return await _worker_post_oi_iv_png(payload, sanitize=True)


async def _worker_post_oi_iv_png(payload: dict[str, object], *, sanitize: bool) -> bytes:
    base = _normalize_worker_base(os.getenv("TNT_WORKER_URL", "") or "")
    if not base:
        raise RuntimeError("TNT_WORKER_URL not set")

    # When /oi is worker-required, ensure we're talking to the parity worker API.
    # Otherwise we can silently hit a legacy worker that renders the old stacked layout.
    try:
        if _oi_worker_mode() == "required":
            import aiohttp

            timeout_h = aiohttp.ClientTimeout(total=2.0)
            async with aiohttp.ClientSession(timeout=timeout_h) as s:
                async with s.get(f"{base}/healthz") as r:
                    hz = await r.json(content_type=None)
            svc = str(hz.get("service") or "") if isinstance(hz, dict) else ""
            if svc and svc != "tnt-worker-parity":
                raise RuntimeError(f"worker_wrong_service:{svc}")
            if not svc:
                raise RuntimeError("worker_missing_service")
    except Exception:
        # Bubble up: caller decides whether to fall back or error (required mode errors).
        raise

    import aiohttp

    try:
        timeout_s = float(os.getenv("TNT_WORKER_TIMEOUT_SEC", "10"))
    except Exception:
        timeout_s = 10.0
    timeout_s = float(max(2.0, min(timeout_s, 45.0)))
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = f"{base}/v1/render/oi_iv"
        async with session.post(url, json=payload, headers={"Accept": "image/png"}) as resp:
            if int(resp.status) != 200:
                body = ""
                try:
                    body = await resp.text()
                except Exception:
                    body = ""
                raise RuntimeError(f"worker render failed status={resp.status} body={(body or '')[:200]}")
            data = await resp.read()
            if not data:
                raise RuntimeError("worker render returned empty")
            png = bytes(data)
            if not sanitize:
                return png
            force_sanitize = False
            try:
                truthy = {"1", "true", "yes", "y", "on"}
                force_sanitize = (os.getenv("TNT_WORKER_SANITIZE_FORCE", "0") or "0").strip().lower() in truthy
            except Exception:
                force_sanitize = False
            out_png, did, reason = _sanitize_worker_png_bytes(png, force=force_sanitize)
            try:
                print(f"[TNT][OI][SANITIZE] SANITIZE={1 if did else 0} force={1 if force_sanitize else 0} reason={reason}")
            except Exception:
                pass

            # Proof (in logs only): confirm which OI/IV layout the worker actually rendered.
            try:
                meta = _read_png_text_metadata(out_png)
                layout = str(meta.get("tnt_oi_iv_layout") or "")
                palette = str(meta.get("tnt_oi_iv_palette") or "")
                wm = str(meta.get("tnt_oi_iv_watermark") or "")
                b = str(meta.get("tnt_build") or "")
                tag = str(meta.get("tnt_render_tag") or "")
                if layout or palette or wm or b or tag:
                    print(
                        f"[TNT][OI][WORKER][META] layout={layout or '-'} palette={palette or '-'} watermark={wm or '-'} "
                        f"build={b or '-'} tag={tag or '-'}"
                    )
            except Exception:
                pass
            return out_png


_OI_WORKER_OI_IV_PARITY: dict[str, object] = {
    "checked": False,
    "ok": False,
    "reason": "",
    "worker": "",
    "mode": "",
    "checked_ts": 0.0,
}


def _oi_worker_parity_mode() -> str:
    """Parity policy for enabling worker payload rendering.

    - strict (default): require worker pixels == local pixels for deterministic payload
    - deterministic: require worker pixels stable across repeated renders; warn if local differs
    """

    raw = (os.getenv("TNT_OI_WORKER_PARITY_MODE", "") or "").strip().lower()
    if raw in {"deterministic", "self", "selfcheck", "worker"}:
        return "deterministic"
    return "strict"


def _sha256_pixels_png(png: bytes) -> str | None:
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(png)).convert("RGBA")
        return __import__("hashlib").sha256(im.tobytes()).hexdigest()
    except Exception:
        return None


async def _oi_worker_payload_renderer_ok() -> bool:
    """One-time capability check: only use worker if its payload renderer matches local pixels."""

    try:
        base = _normalize_worker_base(os.getenv("TNT_WORKER_URL", "") or "")
    except Exception:
        base = ""
    if not base:
        return False

    mode = _oi_worker_parity_mode()

    state = _OI_WORKER_OI_IV_PARITY
    if bool(state.get("checked")) and str(state.get("worker") or "") == base and str(state.get("mode") or "") == mode:
        # TTL cache: prevents probe chatter over time.
        try:
            checked_ts = float(state.get("checked_ts") or 0.0)
        except Exception:
            checked_ts = 0.0
        age = (time.time() - checked_ts) if checked_ts else 1e9
        ok_cached = bool(state.get("ok"))

        try:
            ttl_ok = float(os.getenv("TNT_OI_WORKER_PARITY_OK_TTL_SEC", "180"))
        except Exception:
            ttl_ok = 180.0
        try:
            ttl_fail = float(os.getenv("TNT_OI_WORKER_PARITY_FAIL_TTL_SEC", "10"))
        except Exception:
            ttl_fail = 10.0

        ttl = float(ttl_ok if ok_cached else ttl_fail)
        if ttl > 0 and age <= ttl:
            try:
                truthy = {"1", "true", "yes", "y", "on"}
                if (os.getenv("TNT_OI_WORKER_PARITY_PROBE_LOG", "0") or "0").strip().lower() in truthy:
                    print(f"[TNT][OI][WORKER][PARITY] probe_cache_hit ok={1 if ok_cached else 0} age_s={age:.1f} ttl_s={ttl:.0f} mode={mode} worker={base}")
            except Exception:
                pass
            return ok_cached

        # Expired: allow re-check.
        state["checked"] = False

    # Coalesce concurrent parity probes across different /oi cache keys.
    # Key includes normalized base + parity mode so equivalent URLs don't fragment.
    sf_key = f"oi_worker_parity:{base}:{mode}"

    async def _do_check() -> bool:
        state["checked"] = True
        state["checked_ts"] = float(time.time())
        state["worker"] = base
        state["mode"] = mode
        state["ok"] = False
        state["reason"] = ""

        try:
            truthy = {"1", "true", "yes", "y", "on"}
            if (os.getenv("TNT_OI_WORKER_PARITY_PROBE_LOG", "0") or "0").strip().lower() in truthy:
                print(f"[TNT][OI][WORKER][PARITY] probe_start mode={mode} worker={base}")
        except Exception:
            pass

        # Deterministic payload (mirrors scripts/parity_check_oi_iv.py)
        payload = {
            "symbol": "SPY",
            "strikes": [480.0, 485.0, 490.0, 495.0, 500.0],
            "call_oi": [12000, 18000, 35000, 42000, 78000],
            "put_oi": [9000, 15000, 24000, 38000, 66000],
            "call_iv": [22.1, 21.7, 21.4, 21.2, 20.9],
            "put_iv": None,
            "title": "PARITY-PROBE — deterministic payload",
        }

        # Always verify worker self-determinism first.
        try:
            w1 = await _worker_post_oi_iv_png(dict(payload), sanitize=True)
            w2 = await _worker_post_oi_iv_png(dict(payload), sanitize=True)
            if not w1 or not w2:
                state["reason"] = "worker_empty"
                return False
        except Exception as exc:
            msg = str(exc) if exc is not None else ""
            if msg.startswith("worker_"):
                # Preserve actionable worker identity/contract failures.
                state["reason"] = msg
            else:
                state["reason"] = f"worker_fail:{type(exc).__name__}"
            return False

        w1h = _sha256_pixels_png(bytes(w1))
        w2h = _sha256_pixels_png(bytes(w2))
        if not (w1h and w2h and w1h == w2h):
            state["reason"] = "worker_nondeterministic"
            try:
                print(f"[TNT][OI][WORKER][PARITY][WARN] disabling worker render: worker_nondeterministic mode={mode} worker={base}")
            except Exception:
                pass
            return False

        if mode != "strict":
            state["ok"] = True
            state["reason"] = "ok_deterministic"
            try:
                truthy = {"1", "true", "yes", "y", "on"}
                if (os.getenv("TNT_OI_WORKER_PARITY_PROBE_LOG", "0") or "0").strip().lower() in truthy:
                    print(f"[TNT][OI][WORKER][PARITY] probe_ok mode={mode} worker={base} reason={state.get('reason')}")
            except Exception:
                pass
            return True

        # Strict mode: require local pixels match worker pixels.
        try:
            from delivery.oi_iv_render import render_oi_iv_png as _shared_render

            labels = [f"{float(s):g}" for s in payload["strikes"]]
            local_png = _shared_render(
                title=str(payload["title"]),
                x_labels=labels,
                iv_pct=[float(x) for x in payload["call_iv"]],
                oi_calls=[float(x) for x in payload["call_oi"]],
                oi_puts=[float(x) for x in payload["put_oi"]],
                dpi=150,
                include_iv_overlay=True,
            )
            if not isinstance(local_png, (bytes, bytearray)) or not local_png:
                state["reason"] = "local_empty"
                return False
        except Exception as exc:
            state["reason"] = f"local_fail:{type(exc).__name__}"
            return False

        lh = _sha256_pixels_png(bytes(local_png))
        wh = w1h
        if lh and wh and lh == wh:
            state["ok"] = True
            state["reason"] = "ok"
            try:
                truthy = {"1", "true", "yes", "y", "on"}
                if (os.getenv("TNT_OI_WORKER_PARITY_PROBE_LOG", "0") or "0").strip().lower() in truthy:
                    print(f"[TNT][OI][WORKER][PARITY] probe_ok mode={mode} worker={base} reason={state.get('reason')}")
            except Exception:
                pass
            return True

        state["reason"] = "pixels_mismatch"
        try:
            print(f"[TNT][OI][WORKER][PARITY][WARN] disabling worker render: pixels_mismatch mode={mode} worker={base}")
        except Exception:
            pass
        try:
            truthy = {"1", "true", "yes", "y", "on"}
            if (os.getenv("TNT_OI_WORKER_PARITY_PROBE_LOG", "0") or "0").strip().lower() in truthy:
                print(f"[TNT][OI][WORKER][PARITY] probe_fail mode={mode} worker={base} reason={state.get('reason')}")
        except Exception:
            pass
        return False

    ok = await _singleflight(sf_key, _do_check)
    return bool(ok)


async def _worker_render_oi_png(*, symbol: str) -> bytes:
    """Ask TNT worker to render an OI chart and return PNG bytes.

    Tries (in order):
    - GET `${TNT_WORKER_URL}/v1/render/oi_iv?symbol=...` (current worker API)
    - POST `${TNT_WORKER_URL}/v1/render/oi` with JSON `{symbol: ...}` (fallback)

    Raises on failure.
    """

    base = _normalize_worker_base(os.getenv("TNT_WORKER_URL", "") or "")
    if not base:
        raise RuntimeError("TNT_WORKER_URL not set")

    import aiohttp

    # Fail fast: prefer quick local fallback over blocking on worker timeouts.
    try:
        timeout_s = float(os.getenv("TNT_WORKER_TIMEOUT_SEC", "6"))
    except Exception:
        timeout_s = 6.0
    timeout_s = float(max(2.0, min(timeout_s, 30.0)))
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        url_oi_iv = f"{base}/v1/render/oi_iv"
        try:
            async with session.get(url_oi_iv, params={"symbol": str(symbol)}, headers={"Accept": "image/png"}) as resp:
                if int(getattr(resp, "status", 0)) == 200:
                    data = await resp.read()
                    if data:
                        out_png, _did, _reason = _sanitize_worker_png_bytes(bytes(data), force=True)
                        return out_png
        except Exception:
            pass

        url_oi = f"{base}/v1/render/oi"
        async with session.post(url_oi, json={"symbol": str(symbol)}, headers={"Accept": "image/png"}) as resp:
            if int(getattr(resp, "status", 0)) != 200:
                body = ""
                try:
                    body = await resp.text()
                except Exception:
                    body = ""
                raise RuntimeError(f"worker render failed status={resp.status} body={(body or '')[:200]}")
            data2 = await resp.read()
            if not data2:
                raise RuntimeError("worker render returned empty")
            out_png2, _did2, _reason2 = _sanitize_worker_png_bytes(bytes(data2), force=True)
            return out_png2


# Redis ops in this module are best-effort; if Redis is down, back off briefly.
_REDIS_SF_DISABLE_UNTIL = 0.0


async def run_heavy_chart(
    *,
    interaction: discord.Interaction,
    cmd: str,
    cache_key: str,
    render_fn,
    cache_get,
    cache_set,
    profile_snapshot: RenderProfile | None = None,
    post_to_channel: bool = False,
    redis_sf_namespace: str | None = None,
    redis_sf_work_key: str | None = None,
    redis_sf_lock_ttl_s: int = 60,
    redis_sf_wait_timeout_s: int = 25,
    redis_sf_poll_ms: int = 250,
) -> None:
    """Standardized heavy command execution.

    Mandatory invariant:
    - No heavy command should render outside this wrapper.
    """

    ack = await _instant_ack_editor(interaction)

    def _maybe_log_render_proof(*, meta: dict[str, object] | None) -> None:
        try:
            if str(cmd) != "oi":
                return
            m = meta if isinstance(meta, dict) else {}
            render_mode = str(m.get("render_mode") or m.get("mode") or "unknown")
            sym = str(m.get("symbol") or m.get("sym") or "")
            iv_bit = m.get("include_iv_overlay")
            iv_key = m.get("iv_key")
            worker = (os.getenv("TNT_WORKER_URL", "") or "").strip()
            print(
                f"[TNT][OI][PROOF] render_mode={render_mode} symbol={sym} worker={worker} "
                + f"include_iv={1 if bool(iv_bit) else 0} iv_key={str(iv_key or '')}"
            )
        except Exception:
            pass

    async def _serve_cached(_cached) -> bool:
        if not _cached:
            return False
        try:
            png_bytes = _cached.get("png_bytes")
            caption = _cached.get("caption")
            filename = _cached.get("filename") or f"{str(cmd).lower()}_cached.png"
            cached_ts = _cached.get("cached_asof_ts")
            meta = _cached.get("meta") if isinstance(_cached.get("meta"), dict) else None
        except Exception:
            png_bytes, caption, filename, cached_ts, meta = None, None, None, None, None

        if not (isinstance(png_bytes, (bytes, bytearray)) and png_bytes):
            return False

        content = _append_gprr_banner_cached(str(caption or ""), cached_asof_ts=float(cached_ts) if cached_ts else None)
        posted = False
        try:
            if post_to_channel and (interaction.channel is not None) and bool(getattr(ack, "_ephemeral", False)):
                await interaction.channel.send(
                    content=content,
                    files=[_file_from_png_bytes(bytes(png_bytes), filename=str(filename))],
                )
                posted = True
        except Exception:
            pass

        _maybe_log_render_proof(meta=meta)

        if posted:
            await ack.edit(content=content + "\n\n(Posted to channel)")
        else:
            await ack.edit(
                content=content,
                attachments=[_file_from_png_bytes(bytes(png_bytes), filename=str(filename))],
            )
        return True

    # 2️⃣ cache-first
    try:
        cached = await cache_get(cache_key)
    except Exception:
        cached = None

    if cached and await _serve_cached(cached):
        return

    # 3️⃣ GPRR gate (cache-miss renders)
    try:
        prof = profile_snapshot if profile_snapshot is not None else _gprr_profile()
        lvl = ProfileLevel(int(getattr(prof, "level", 0)))
        if lvl == ProfileLevel.SURVIVAL:
            await ack.edit(content=_gprr_busy_banner(prof))
            return
        if lvl == ProfileLevel.DEGRADED:
            async with _RENDER_STATE_LOCK:
                waiting = int(_RENDER_WAITING)
            if waiting >= 4:
                await ack.edit(content=_gprr_busy_banner(prof))
                return
    except Exception:
        pass

    # 4️⃣ singleflight render
    async def _join_notice() -> None:
        if not _SINGLEFLIGHT_NOTICE:
            return
        try:
            await _fam_inc(str(cmd), "singleflight_waits", 1.0)
            await _fam_event(str(cmd), "singleflight_wait")
            await interaction.followup.send(
                "♻️ Coalesced: another user requested this moments ago — reusing the same render.",
                ephemeral=True,
            )
        except Exception:
            pass

    async def _work():
        await _acquire_render_slot_global(family=str(cmd), interaction=interaction)
        try:
            prof = profile_snapshot if profile_snapshot is not None else _gprr_profile()
            out = _call_render_fn(render_fn, profile=prof)
            if asyncio.iscoroutine(out):
                out = await out
            return out
        finally:
            await _release_render_slot_global()

    # Optional Redis singleflight (cross-process): best-effort and fail-open.
    got_redis_lock = False
    lock_key = None
    if redis_sf_namespace and redis_sf_work_key and time.time() >= float(_REDIS_SF_DISABLE_UNTIL):
        try:
            from tnt_redis import redis_client as _tnt_redis_client, rkey as _tnt_rkey

            lock_key = _tnt_rkey("sf", str(redis_sf_namespace), str(redis_sf_work_key), "lock")

            def _try_lock() -> bool:
                try:
                    return bool(_tnt_redis_client().set(lock_key, "1", nx=True, ex=int(redis_sf_lock_ttl_s)))
                except Exception:
                    return False

            try:
                got_redis_lock = bool(await asyncio.wait_for(asyncio.to_thread(_try_lock), timeout=0.75))
            except Exception:
                got_redis_lock = False
                try:
                    globals()["_REDIS_SF_DISABLE_UNTIL"] = time.time() + 30.0
                except Exception:
                    pass

            if not got_redis_lock:
                deadline = time.time() + float(redis_sf_wait_timeout_s)
                poll_s = max(0.05, float(redis_sf_poll_ms) / 1000.0)
                while time.time() < deadline:
                    try:
                        cached2 = await cache_get(cache_key)
                    except Exception:
                        cached2 = None
                    if cached2 and await _serve_cached(cached2):
                        return
                    await asyncio.sleep(poll_s)
        except Exception:
            got_redis_lock = False
            lock_key = None
            try:
                globals()["_REDIS_SF_DISABLE_UNTIL"] = time.time() + 30.0
            except Exception:
                pass
    try:
        result = await _singleflight(str(cache_key), _work, on_join=_join_notice)
    except Exception:
        try:
            await ack.edit(content="Unable to render right now — please retry shortly.")
        except Exception:
            pass
        return
    finally:
        if got_redis_lock and lock_key:
            try:
                from tnt_redis import redis_client as _tnt_redis_client

                def _unlock() -> None:
                    try:
                        _tnt_redis_client().delete(lock_key)
                    except Exception:
                        pass

                await asyncio.wait_for(asyncio.to_thread(_unlock), timeout=0.75)
            except Exception:
                pass

    if not isinstance(result, dict):
        await ack.edit(content="Unable to render right now — please retry shortly.")
        return

    if result.get("error"):
        await ack.edit(content=str(result.get("error")))
        # Negative cache (tiny) to avoid stampede.
        try:
            await cache_set(cache_key, {"png_bytes": None, "png_err": "render_error", "meta": None, "ttl_sec": 3})
        except Exception:
            pass
        return

    png_bytes = result.get("png_bytes")
    caption = str(result.get("caption") or "")
    filename = str(result.get("filename") or f"{str(cmd).lower()}.png")
    ttl_sec = int(result.get("ttl_sec") or 30)
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else None

    if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
        await ack.edit(content="Unable to render right now — please retry shortly.")
        try:
            await cache_set(cache_key, {"png_bytes": None, "png_err": "render_error", "meta": None, "ttl_sec": 3})
        except Exception:
            pass
        return

    # 5️⃣ store cache
    try:
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        meta = dict(meta)
        meta.setdefault("caption", caption)
        meta.setdefault("filename", filename)
        await cache_set(cache_key, {"png_bytes": bytes(png_bytes), "png_err": None, "meta": meta, "ttl_sec": ttl_sec})
    except Exception:
        pass

    # 6️⃣ respond
    content = _append_gprr_banner(caption)
    posted = False
    try:
        if post_to_channel and (interaction.channel is not None) and bool(getattr(ack, "_ephemeral", False)):
            await interaction.channel.send(
                content=content,
                files=[_file_from_png_bytes(bytes(png_bytes), filename=filename)],
            )
            posted = True
    except Exception:
        pass

    _maybe_log_render_proof(meta=meta)

    if posted:
        await ack.edit(content=content + "\n\n(Posted to channel)")
    else:
        await ack.edit(
            content=content,
            attachments=[_file_from_png_bytes(bytes(png_bytes), filename=filename)],
        )


_ACK_WORKING_TEXT = "Working…"


class _AckEditor:
    def __init__(
        self,
        interaction: discord.Interaction,
        ack_msg: discord.Message | None,
        *,
        ephemeral: bool,
    ) -> None:
        self._interaction = interaction
        self._ack_msg = ack_msg
        self._ephemeral = bool(ephemeral)

    async def edit(
        self,
        *,
        content: str,
        files: list[discord.File] | None = None,
        attachments: list[object] | None = None,
    ) -> None:
        """Best-effort edit of the ack message.

        - If possible, edits the same message (content and optionally files).
        - If editing fails, falls back to a followup send.
        """

        txt = str(content or "").strip() or "(no output)"

        # Normalize: caller may pass discord.File objects in attachments.
        norm_files: list[discord.File] | None = files
        if norm_files is None and attachments:
            try:
                norm_files = [a for a in attachments if isinstance(a, discord.File)]
            except Exception:
                norm_files = None

        # Prefer editing the known message object.
        if self._ack_msg is not None:
            try:
                if norm_files:
                    await self._ack_msg.edit(content=txt, attachments=[], files=norm_files)
                else:
                    await self._ack_msg.edit(content=txt)
                return
            except Exception:
                pass

        # If we don't have a message handle, try editing original response.
        try:
            if norm_files:
                await self._interaction.edit_original_response(content=txt, attachments=[], files=norm_files)
            else:
                await self._interaction.edit_original_response(content=txt)
            return
        except Exception:
            pass

        # Fall back to followup.
        try:
            if norm_files:
                await self._interaction.followup.send(txt, ephemeral=self._ephemeral, files=norm_files)
            else:
                await self._interaction.followup.send(txt, ephemeral=self._ephemeral)
        except Exception:
            pass

    async def __call__(self, final_text: str) -> None:
        await self.edit(content=final_text)


async def _instant_ack_editor(
    interaction: discord.Interaction,
    *,
    initial: str = _ACK_WORKING_TEXT,
    ephemeral: bool = True,
) -> _AckEditor:
    """Send an immediate ephemeral ack and return an editor to update it in-place.

    This reduces the chance a heavy command "looks dead" even if it already deferred.
    Best-effort: on any failure, the returned editor falls back to followup sends.
    """

    ack_msg: discord.Message | None = None

    async def _send_initial() -> None:
        nonlocal ack_msg
        txt = str(initial or _ACK_WORKING_TEXT)

        # If not yet acknowledged, prefer initial response (lets us edit original_response).
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(txt, ephemeral=ephemeral)
                try:
                    ack_msg = await interaction.original_response()
                except Exception:
                    ack_msg = None
                return
        except Exception:
            pass

        # If already acknowledged (e.g., deferred), prefer editing the original response
        # to avoid creating a second ephemeral message.
        try:
            await interaction.edit_original_response(content=txt)
            try:
                ack_msg = await interaction.original_response()
            except Exception:
                ack_msg = None
            return
        except Exception:
            pass

        # Fall back: use a followup message (editable via Message.edit).
        try:
            ack_msg = await interaction.followup.send(txt, ephemeral=ephemeral)
        except Exception:
            ack_msg = None

    await _send_initial()
    return _AckEditor(interaction, ack_msg, ephemeral=ephemeral)


async def _status(
    ack_update: Callable[[str], Any] | None,
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
):
    """Best-effort status update.

    Prefer updating the instant-ACK message (if available); otherwise fall back to
    sending either the initial response or a followup.
    """

    try:
        if ack_update is not None:
            return await ack_update(str(content))
    except Exception:
        pass

    try:
        if interaction.response.is_done():
            return await interaction.followup.send(str(content), ephemeral=ephemeral)
        return await interaction.response.send_message(str(content), ephemeral=ephemeral)
    except Exception:
        return None


def _cooldown_effective_sec(bucket: str) -> int:
    base = _cooldown_env_sec(bucket)
    b = (bucket or "").strip().lower()
    if b in {"heavy", "options"} and _gprr_enabled():
        try:
            return int(_GPRR.effective_heavy_cooldown(base))  # type: ignore[union-attr]
        except Exception:
            return int(base)
    return int(base)


def _gprr_cache_key(base_key: str) -> str:
    # Cache keys must include profile/tunable so degraded renders don't overwrite normal.
    k = (base_key or "").strip()
    if not k:
        k = "png"
    try:
        if _gprr_enabled():
            return k + ":" + str(_GPRR.cache_key_suffix())  # type: ignore[union-attr]
    except Exception:
        pass
    return k


def _is_heavy_family(family: str) -> bool:
    fam = (family or "").strip().lower()
    return fam in {"oi", "pcr", "gex", "ddp", "options"}


def _call_render_fn(render_fn, *, profile: RenderProfile):
    # Best-effort compatibility shim: pass profile knobs only if render_fn accepts them.
    try:
        sig = inspect.signature(render_fn)
        params = sig.parameters
        if "profile" in params:
            return render_fn(profile=profile)
        if "policy" in params:
            return render_fn(policy=profile)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return render_fn(
                profile=profile,
                dpi=int(profile.dpi),
                top_strikes=int(profile.top_strikes),
                include_iv_overlay=bool(profile.include_iv_overlay),
            )
    except Exception:
        pass
    return render_fn()


def _gather_gprr_telemetry() -> dict[str, object]:
    # Keep this best-effort and never raise.
    out: dict[str, object] = {"ts": float(time.time())}
    # Render state
    try:
        out["max_renders"] = int(_MAX_CHART_RENDERS)
        out["render_active"] = int(_RENDER_ACTIVE)
        out["render_waiting"] = int(_RENDER_WAITING)
    except Exception:
        out["max_renders"] = int(_MAX_CHART_RENDERS)
        out["render_active"] = 0
        out["render_waiting"] = 0

    # PNG cache
    try:
        out["png_cache_entries"] = int(len(_PNG_CACHE))
        out["png_cache_bytes_mb"] = float(_PNG_CACHE_BYTES) / (1024.0 * 1024.0)
    except Exception:
        out["png_cache_entries"] = 0
        out["png_cache_bytes_mb"] = 0.0

    # Render timing EMA (from per-family metrics if present).
    try:
        async def _noop() -> None:
            return

        # Fast path: read without acquiring lock (best-effort); correctness isn't critical.
        d = _FAMILY_METRICS.get("chart") if isinstance(_FAMILY_METRICS, dict) else None
        if isinstance(d, dict) and d.get("render_time_ms_ema") is not None:
            out["render_avg_ms_ema"] = float(d.get("render_time_ms_ema"))
    except Exception:
        pass

    # HTTP stats (Polygon + options best-effort)
    poly = None
    opt = None
    try:
        from delivery.on_demand_data import get_polygon_http_stats

        poly = get_polygon_http_stats()
    except Exception:
        poly = None
    try:
        from delivery.discord_bot import get_options_http_stats

        opt = get_options_http_stats()
    except Exception:
        opt = None

    poly_active = int(poly.get("active")) if isinstance(poly, dict) and poly.get("active") is not None else 0
    poly_cap = int(poly.get("cap")) if isinstance(poly, dict) and poly.get("cap") is not None else 0
    opt_active = int(opt.get("active")) if isinstance(opt, dict) and opt.get("active") is not None else 0
    opt_cap = int(opt.get("cap")) if isinstance(opt, dict) and opt.get("cap") is not None else 0

    out["http_inflight"] = int(poly_active + opt_active)
    out["max_http"] = int(max(1, poly_cap + opt_cap))
    if isinstance(poly, dict):
        # Prefer 60s windows when available; fall back to 15m if not.
        out["http_429_60s"] = int(poly.get("err_429_60s") if poly.get("err_429_60s") is not None else poly.get("err_429_15m") or 0)
        out["http_5xx_60s"] = int(poly.get("err_5xx_60s") if poly.get("err_5xx_60s") is not None else poly.get("err_5xx_15m") or 0)
        out["http_timeouts_60s"] = int(poly.get("err_timeouts_60s") or 0)
    else:
        out["http_429_60s"] = 0
        out["http_5xx_60s"] = 0
        out["http_timeouts_60s"] = 0

    # Memory/thread stats
    try:
        import psutil  # type: ignore

        p = psutil.Process(os.getpid())
        out["ram_rss_mb"] = float(p.memory_info().rss) / (1024.0 * 1024.0)
        # Lightweight CPU signal (system-level percent over interval is expensive; keep simple).
        out["cpu_pct"] = float(p.cpu_percent(interval=None))
    except Exception:
        pass
    try:
        out["threads"] = int(threading.active_count())
    except Exception:
        pass
    return out


# key -> (png_bytes, meta, expires_at, size_bytes, stored_at)
_PNG_CACHE: "OrderedDict[str, tuple[bytes | None, object | None, float, int, float, str | None]]" = OrderedDict()
_PNG_CACHE_BYTES = 0
_PNG_CACHE_LOCK = asyncio.Lock()

_INFLIGHT: dict[str, asyncio.Future] = {}
_INFLIGHT_LOCK = asyncio.Lock()

_SINGLEFLIGHT_NOTICE = (os.getenv("TNT_SINGLEFLIGHT_NOTICE", "1").strip().lower() in {"1", "true", "yes", "on"})

# Lightweight observability counters (stdout only)
_OBS_LOCK = asyncio.Lock()
_OBS: dict[str, float] = {
    "png_cache_hit": 0.0,
    "png_cache_miss": 0.0,
    "png_cache_evictions": 0.0,
    "singleflight_joins": 0.0,
    "renders": 0.0,
    "render_ms_sum": 0.0,
    "render_ms_count": 0.0,
    "render_errors": 0.0,
    "last_render_error_ts": 0.0,
    "command_errors": 0.0,
    "max_inflight": 0.0,
    "png_cache_stale_hit": 0.0,
}


# Per-command-family perf counters (kept intentionally small)
_METRICS_LOCK = asyncio.Lock()
_FAMILY_METRICS: dict[str, dict[str, float]] = {}
_FAMILY_EVENTS: dict[tuple[str, str], deque[float]] = {}
_FAMILY_SAMPLES: dict[tuple[str, str], deque[tuple[float, float]]] = {}


def _now_s() -> float:
    return float(time.time())


def _window_cutoff_s(window_sec: int = 900) -> float:
    return _now_s() - float(max(60, int(window_sec)))


async def _fam_inc(family: str, key: str, inc: float = 1.0) -> None:
    fam = (family or "").strip() or "unknown"
    k = (key or "").strip() or "unknown"
    async with _METRICS_LOCK:
        d = _FAMILY_METRICS.setdefault(fam, {})
        d[k] = float(d.get(k, 0.0)) + float(inc)


async def _fam_event(family: str, event: str, *, ts: float | None = None) -> None:
    fam = (family or "").strip() or "unknown"
    ev = (event or "").strip() or "unknown"
    t = float(ts if ts is not None else _now_s())
    key = (fam, ev)
    async with _METRICS_LOCK:
        dq = _FAMILY_EVENTS.setdefault(key, deque())
        dq.append(t)
        cutoff = _window_cutoff_s(900)
        while dq and dq[0] < cutoff:
            dq.popleft()


async def _fam_sample_max_15m(family: str, metric: str, value: float, *, ts: float | None = None) -> None:
    fam = (family or "").strip() or "unknown"
    m = (metric or "").strip() or "unknown"
    t = float(ts if ts is not None else _now_s())
    key = (fam, m)
    async with _METRICS_LOCK:
        dq = _FAMILY_SAMPLES.setdefault(key, deque())
        dq.append((t, float(value)))
        cutoff = _window_cutoff_s(900)
        while dq and dq[0][0] < cutoff:
            dq.popleft()


def _ema_update(prev: float | None, new: float, *, alpha: float = 0.2) -> float:
    if prev is None:
        return float(new)
    a = float(alpha)
    if a <= 0:
        return float(prev)
    if a >= 1:
        return float(new)
    return (a * float(new)) + ((1.0 - a) * float(prev))


async def _obs_inc(key: str, inc: float = 1.0) -> None:
    async with _OBS_LOCK:
        _OBS[key] = float(_OBS.get(key, 0.0)) + float(inc)


async def _obs_set_max(key: str, val: float) -> None:
    async with _OBS_LOCK:
        cur = float(_OBS.get(key, 0.0))
        if float(val) > cur:
            _OBS[key] = float(val)


def _sem_available(sem: asyncio.Semaphore) -> int:
    try:
        return int(getattr(sem, "_value"))
    except Exception:
        return -1


async def _acquire_render_slot_global(*, family: str, interaction: discord.Interaction | None = None) -> None:
    global _RENDER_ACTIVE, _RENDER_WAITING
    fam = (family or "").strip() or "unknown"
    cap = int(_MAX_CHART_RENDERS)
    queued_ahead = 0

    async with _RENDER_STATE_LOCK:
        if _RENDER_ACTIVE >= cap:
            queued_ahead = int(_RENDER_WAITING + cap)
            _RENDER_WAITING += 1

    if queued_ahead > 0:
        try:
            await _fam_sample_max_15m(fam, "render_queue_depth_max", float(queued_ahead))
        except Exception:
            pass
        if interaction is not None:
            try:
                await interaction.followup.send(
                    f"🕒 Busy: queued behind {queued_ahead} renders (cap: {cap}).",
                    ephemeral=True,
                )
            except Exception:
                pass

    try:
        await _CHART_RENDER_SEM.acquire()
    except BaseException:
        if queued_ahead > 0:
            async with _RENDER_STATE_LOCK:
                _RENDER_WAITING = max(0, int(_RENDER_WAITING) - 1)
        raise

    async with _RENDER_STATE_LOCK:
        if queued_ahead > 0:
            _RENDER_WAITING = max(0, int(_RENDER_WAITING) - 1)
        _RENDER_ACTIVE += 1

    try:
        await _obs_set_max("max_inflight", float(_RENDER_ACTIVE))
    except Exception:
        pass


async def _release_render_slot_global() -> None:
    global _RENDER_ACTIVE
    async with _RENDER_STATE_LOCK:
        _RENDER_ACTIVE = max(0, int(_RENDER_ACTIVE) - 1)
    try:
        _CHART_RENDER_SEM.release()
    except Exception:
        pass


# Soft per-user rate limiting for heavy commands
_USER_RATE_LOCK = asyncio.Lock()
_USER_RATE_STATE: dict[tuple[int, str], tuple[float, int, float]] = {}


async def _check_user_cooldown(user_id: int, *, bucket: str, cooldown_sec: int) -> tuple[bool, float]:
    """Soft cooldown keyed by (user, bucket) with a tiny burst allowance.

    Behavior:
    - Allow up to 2 calls within a 3s burst window (fat-finger friendly).
    - After that, enforce cooldown based on the last allowed call.
    """

    now = _now_s()
    cd = max(int(cooldown_sec), 0)
    if cd <= 0:
        return True, 0.0

    key = (int(user_id), str(bucket))
    burst_window = 3.0

    async with _USER_RATE_LOCK:
        window_start, window_count, last_allowed = _USER_RATE_STATE.get(key, (0.0, 0, 0.0))

        if window_start <= 0.0 or (now - window_start) > burst_window:
            window_start, window_count = now, 0

        # If still inside burst window, allow one immediate retry.
        if (now - window_start) <= burst_window and window_count < 2:
            window_count += 1
            last_allowed = now
            _USER_RATE_STATE[key] = (window_start, window_count, last_allowed)
            return True, 0.0

        if last_allowed > 0.0 and (now - last_allowed) < float(cd):
            return False, float(cd - (now - last_allowed))

        # Cooldown satisfied; allow and reset burst accounting.
        _USER_RATE_STATE[key] = (now, 1, now)
        return True, 0.0


def _cooldown_default_sec(bucket: str) -> int:
    b = (bucket or "").strip().lower()
    # Practical defaults (user-requested):
    # - options-heavy (/oi, /pcr): 15s
    # - chart-heavy (/chart, /vwap_range, /rs): 8s
    # - macro cached (/risk_on_off): 3s
    if b in {"heavy", "options"}:
        return 20
    if b in {"chart"}:
        return 10
    if b in {"chart_ui"}:
        return 4
    if b in {"macro"}:
        return 3
    return 8


def _cooldown_env_sec(bucket: str) -> int:
    # Back-compat: TNT_USER_HEAVY_CMD_COOLDOWN_SEC
    b = (bucket or "").strip().lower()
    env_map = {
        "heavy": ["TNT_USER_HEAVY_CMD_COOLDOWN_SEC", "TNT_USER_COOLDOWN_HEAVY_SEC"],
        "options": ["TNT_USER_OPTIONS_COOLDOWN_SEC", "TNT_USER_COOLDOWN_OPTIONS_SEC"],
        "chart": ["TNT_USER_CHART_COOLDOWN_SEC", "TNT_USER_COOLDOWN_CHART_SEC"],
        "chart_ui": ["TNT_USER_CHART_UI_COOLDOWN_SEC"],
        "macro": ["TNT_USER_MACRO_COOLDOWN_SEC", "TNT_USER_COOLDOWN_MACRO_SEC"],
    }
    for k in env_map.get(b, []):
        try:
            raw = os.getenv(k)
            if raw is None:
                continue
            v = int(str(raw).strip())
            return max(0, v)
        except Exception:
            continue
    return _cooldown_default_sec(b)


async def _send_cooldown_notice(interaction: discord.Interaction, *, family: str, bucket: str, wait_s: float) -> None:
    fam = (family or "").strip() or "unknown"
    await _fam_inc(fam, "cooldown_blocks", 1.0)
    await _fam_event(fam, "cooldown_block")
    cooldown = _cooldown_env_sec(bucket)
    wait_i = max(1, int(round(float(wait_s))))
    msg = f"⏳ Cooldown: try again in {wait_i}s (rate-limited to keep TNT fast for everyone)."
    try:
        await interaction.followup.send(msg, ephemeral=True)
        return
    except Exception:
        pass

    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


async def _singleflight(key: str, work_coro_factory, *, on_join=None):
    async with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(key)
        if existing is not None:
            await _obs_inc("singleflight_joins", 1.0)
            if on_join is not None:
                try:
                    maybe = on_join()
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception:
                    pass
            try:
                return await asyncio.shield(existing)
            except Exception:
                return await existing

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        _INFLIGHT[key] = fut

    try:
        result = await work_coro_factory()
        if not fut.done():
            fut.set_result(result)
        return result
    except BaseException as exc:  # noqa: BLE001
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        async with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)


async def _png_cache_get(key: str, *, family: str):
    if not _png_cache_enabled():
        return None
    now = time.time()
    async with _PNG_CACHE_LOCK:
        entry = _PNG_CACHE.get(key)
        if not entry:
            await _obs_inc("png_cache_miss", 1.0)
            await _fam_inc(family, "cache_miss", 1.0)
            await _fam_event(family, "cache_miss")
            return None
        png_bytes, meta, expires_at, size_bytes, stored_at, png_err = entry
        if now > float(expires_at):
            # Expired: drop and treat as miss.
            try:
                global _PNG_CACHE_BYTES
                _PNG_CACHE_BYTES = max(0, int(_PNG_CACHE_BYTES) - int(size_bytes))
            except Exception:
                pass
            _PNG_CACHE.pop(key, None)
            await _obs_inc("png_cache_miss", 1.0)
            await _fam_inc(family, "cache_miss", 1.0)
            await _fam_event(family, "cache_miss")
            return None

        # Touch LRU.
        try:
            _PNG_CACHE.move_to_end(key, last=True)
        except Exception:
            pass

        await _obs_inc("png_cache_hit", 1.0)
        await _fam_inc(family, "cache_hit", 1.0)
        await _fam_event(family, "cache_hit")
        try:
            if isinstance(meta, dict):
                m = dict(meta)
                m["__tnt_cached_ts"] = float(stored_at)
                m["__tnt_cache_state"] = "fresh"
                meta = m
        except Exception:
            pass
        return (png_bytes, png_err, meta)


async def _png_cache_get_stale_only(key: str, *, family: str, stale_max_age_sec: int) -> tuple[bytes | None, str | None, object | None] | None:
    """Return an expired cache entry if it's still within the stale window.

    Used only during high load to serve *something* rather than stampeding.
    """

    if not _png_cache_enabled():
        return None
    stale_max = max(0, int(stale_max_age_sec))
    if stale_max <= 0:
        return None

    now = float(time.time())
    async with _PNG_CACHE_LOCK:
        entry = _PNG_CACHE.get(key)
        if not entry:
            return None

        png_bytes, meta, expires_at, _size_bytes, stored_at, png_err = entry
        # Only return *expired* entries here; fresh hits are handled by _png_cache_get.
        if now <= float(expires_at):
            return None

        try:
            age = now - float(stored_at)
        except Exception:
            return None
        if age > float(stale_max):
            return None

        # Touch LRU.
        try:
            _PNG_CACHE.move_to_end(key, last=True)
        except Exception:
            pass

        await _obs_inc("png_cache_stale_hit", 1.0)
        await _fam_inc(family, "cache_stale_hit", 1.0)
        await _fam_event(family, "cache_stale_hit")
        try:
            if isinstance(meta, dict):
                m = dict(meta)
                m["__tnt_cached_ts"] = float(stored_at)
                m["__tnt_cache_state"] = "stale"
                meta = m
        except Exception:
            pass
        return (png_bytes, png_err, meta)


async def _png_cache_set(key: str, value, ttl_sec: int) -> None:
    if not _png_cache_enabled():
        return
    global _PNG_CACHE_BYTES
    ttl = max(int(ttl_sec), 0)
    if ttl <= 0:
        return
    now = time.time()
    async with _PNG_CACHE_LOCK:
        try:
            png_bytes, png_err, meta = value
        except Exception:
            png_bytes, png_err, meta = None, None, None

        size_bytes = int(len(png_bytes)) if isinstance(png_bytes, (bytes, bytearray)) else 0

        # Remove existing (if present) to keep byte accounting correct.
        if key in _PNG_CACHE:
            try:
                _old = _PNG_CACHE.get(key)
                if _old is not None:
                    _PNG_CACHE_BYTES = max(0, int(_PNG_CACHE_BYTES) - int(_old[3]))
            except Exception:
                pass
            _PNG_CACHE.pop(key, None)

        _PNG_CACHE_BYTES = int(_PNG_CACHE_BYTES) + int(size_bytes)
        _PNG_CACHE[key] = (png_bytes, meta, now + ttl, size_bytes, now, png_err)
        try:
            _PNG_CACHE.move_to_end(key, last=True)
        except Exception:
            pass

        # Prune expired entries opportunistically.
        try:
            expired_keys: list[str] = []
            for k, entry in list(_PNG_CACHE.items()):
                if now > float(entry[2]):
                    expired_keys.append(k)
            for k in expired_keys:
                entry = _PNG_CACHE.pop(k, None)
                if entry is not None:
                    _PNG_CACHE_BYTES = max(0, int(_PNG_CACHE_BYTES) - int(entry[3]))
        except Exception:
            pass

        # Evict LRU until both constraints satisfied.
        while (len(_PNG_CACHE) > int(_PNG_CACHE_MAX)) or (int(_PNG_CACHE_BYTES) > int(_PNG_CACHE_MAX_BYTES)):
            k, entry = _PNG_CACHE.popitem(last=False)
            try:
                _PNG_CACHE_BYTES = max(0, int(_PNG_CACHE_BYTES) - int(entry[3]))
            except Exception:
                pass
            await _obs_inc("png_cache_evictions", 1.0)


async def _render_png_cached(
    *,
    cache_key: str,
    ttl_sec: int,
    render_sync_fn,
    family: str,
    interaction: discord.Interaction | None = None,
    meta: dict[str, object] | None = None,
):
    fam = (family or "").strip() or "unknown"
    prof = _gprr_profile() if _gprr_enabled() else None
    full_key = _gprr_cache_key(cache_key)
    await _fam_inc(fam, "requests_total", 1.0)

    # Demand telemetry (rolling 60s)
    try:
        if _gprr_enabled() and _GPRR is not None:
            cmd = str(meta.get("cmd") if isinstance(meta, dict) else fam).strip().lower() or fam
            sym = str(meta.get("symbol") or "") if isinstance(meta, dict) else ""
            _GPRR.note_command(cmd=cmd, symbol=(sym or None))
    except Exception:
        pass

    cached = await _png_cache_get(full_key, family=fam)
    if cached is not None:
        return cached

    # Profile policy: cache-only for heavy renders in Survival mode.
    try:
        if _gprr_enabled() and prof is not None:
            is_heavy = bool(meta.get("heavy")) if isinstance(meta, dict) else _is_heavy_family(fam)
            async with _RENDER_STATE_LOCK:
                waiting = int(_RENDER_WAITING)

            if ProfileLevel(int(prof.level)) == ProfileLevel.SURVIVAL and is_heavy:
                # Try serving any cached profile variant before denying.
                stale = await _png_cache_get_stale_only(full_key, family=fam, stale_max_age_sec=_busy_stale_max_age_sec())
                if stale is not None:
                    try:
                        if interaction is not None and isinstance(stale[2], dict):
                            ts = stale[2].get("__tnt_cached_ts")
                            if isinstance(ts, (int, float)):
                                await interaction.followup.send(_busy_cached_only_banner(cached_asof_ts=float(ts)), ephemeral=True)
                    except Exception:
                        pass
                    return stale
                await _fam_inc(fam, "busy_blocks", 1.0)
                await _fam_event(fam, "busy_block")
                return (None, "busy_cached_only", {"profile": int(prof.level)})

            if ProfileLevel(int(prof.level)) == ProfileLevel.DEGRADED and int(waiting) >= 4 and prof.allow_cache_miss_render:
                stale = await _png_cache_get_stale_only(full_key, family=fam, stale_max_age_sec=_busy_stale_max_age_sec())
                if stale is not None:
                    try:
                        if interaction is not None and isinstance(stale[2], dict):
                            ts = stale[2].get("__tnt_cached_ts")
                            if isinstance(ts, (int, float)):
                                await interaction.followup.send(_busy_cached_only_banner(cached_asof_ts=float(ts)), ephemeral=True)
                    except Exception:
                        pass
                    return stale
                await _fam_inc(fam, "busy_blocks", 1.0)
                await _fam_event(fam, "busy_block")
                return (None, "busy_cached_only", {"profile": int(prof.level), "waiting": int(waiting)})
    except Exception:
        # If policy fails, proceed with normal behavior.
        pass

    # Busy-mode load shedding: if we are already saturated and queued, only serve cached.
    if _busy_cached_only_enabled():
        try:
            async with _RENDER_STATE_LOCK:
                cap = int(_MAX_CHART_RENDERS)
                active = int(_RENDER_ACTIVE)
                waiting = int(_RENDER_WAITING)
            queued = active + waiting
            if active >= cap and waiting >= _busy_queue_depth_threshold():
                stale = await _png_cache_get_stale_only(full_key, family=fam, stale_max_age_sec=_busy_stale_max_age_sec())
                if stale is not None:
                    if interaction is not None:
                        try:
                            ts = None
                            if isinstance(stale[2], dict):
                                ts = stale[2].get("__tnt_cached_ts")
                            if isinstance(ts, (int, float)):
                                await interaction.followup.send(_busy_cached_only_banner(cached_asof_ts=float(ts)), ephemeral=True)
                            else:
                                await interaction.followup.send(_busy_cached_only_banner(), ephemeral=True)
                        except Exception:
                            pass
                    return stale

                await _fam_inc(fam, "busy_blocks", 1.0)
                await _fam_event(fam, "busy_block")
                if interaction is not None:
                    try:
                        await interaction.followup.send(_busy_cached_only_banner(), ephemeral=True)
                    except Exception:
                        pass
                return (None, "busy_cached_only", {"queued": int(queued), "cap": int(cap)})
        except Exception:
            pass

    async def _join_notice():
        if not _SINGLEFLIGHT_NOTICE or interaction is None:
            return
        try:
            await _fam_inc(fam, "singleflight_waits", 1.0)
            await _fam_event(fam, "singleflight_wait")
            await interaction.followup.send(
                "♻️ Coalesced: another user requested this moments ago — reusing the same render.",
                ephemeral=True,
            )
        except Exception:
            pass

    async def _acquire_render_slot() -> None:
        global _RENDER_ACTIVE, _RENDER_WAITING
        cap = int(_MAX_CHART_RENDERS)
        queued_ahead = 0

        # Register as waiting if we're already at capacity.
        async with _RENDER_STATE_LOCK:
            if _RENDER_ACTIVE >= cap:
                queued_ahead = int(_RENDER_WAITING + cap)
                _RENDER_WAITING += 1

        if queued_ahead > 0 and interaction is not None:
            await _fam_sample_max_15m(fam, "render_queue_depth_max", float(queued_ahead))
            try:
                await interaction.followup.send(
                    f"🕒 Busy: queued behind {queued_ahead} renders (cap: {cap}).",
                    ephemeral=True,
                )
            except Exception:
                pass

        try:
            await _CHART_RENDER_SEM.acquire()
        except BaseException:
            # If we were waiting, roll it back.
            if queued_ahead > 0:
                async with _RENDER_STATE_LOCK:
                    _RENDER_WAITING = max(0, int(_RENDER_WAITING) - 1)
            raise

        async with _RENDER_STATE_LOCK:
            if queued_ahead > 0:
                _RENDER_WAITING = max(0, int(_RENDER_WAITING) - 1)
            _RENDER_ACTIVE += 1

        try:
            await _obs_set_max("max_inflight", float(_RENDER_ACTIVE))
        except Exception:
            pass

    async def _release_render_slot() -> None:
        global _RENDER_ACTIVE
        async with _RENDER_STATE_LOCK:
            _RENDER_ACTIVE = max(0, int(_RENDER_ACTIVE) - 1)
        try:
            _CHART_RENDER_SEM.release()
        except Exception:
            pass

    async def _do_work():
        await _obs_inc("renders", 1.0)
        try:
            async with _INFLIGHT_LOCK:
                await _obs_set_max("max_inflight", float(len(_INFLIGHT)))
        except Exception:
            pass

        t0 = time.perf_counter()
        await _acquire_render_slot()
        try:
            p_now = _gprr_profile() if _gprr_enabled() else RenderProfile(ProfileLevel.NORMAL, dpi=160, top_strikes=25, include_iv_overlay=True, allow_cache_miss_render=True, heavy_cmd_allowed=True, heavy_cooldown_mult=1.0)
            out = await asyncio.to_thread(lambda: _call_render_fn(render_sync_fn, profile=p_now))
        finally:
            await _release_render_slot()
        dt_ms = (time.perf_counter() - t0) * 1000.0
        await _obs_inc("render_ms_sum", float(dt_ms))
        await _obs_inc("render_ms_count", 1.0)
        # Family EMA render time.
        async with _METRICS_LOCK:
            d = _FAMILY_METRICS.setdefault(fam, {})
            prev = d.get("render_time_ms_ema")
            d["render_time_ms_ema"] = _ema_update(prev, float(dt_ms), alpha=0.2)
        try:
            if _gprr_enabled() and _GPRR is not None:
                _GPRR.set_render_avg_ms(float(dt_ms))
        except Exception:
            pass
        return out

    result = await _singleflight(full_key, _do_work, on_join=_join_notice)

    # Cache successes longer; cache failures briefly to prevent thundering herds.
    try:
        png_bytes, png_err, _meta = result
    except Exception:
        png_bytes, png_err = None, "render_error"

    if png_bytes:
        await _png_cache_set(full_key, result, ttl_sec)
    else:
        await _png_cache_set(full_key, result, min(5, max(1, int(ttl_sec))))
        await _obs_inc("render_errors", 1.0)
        await _fam_event(fam, "render_error")
        async with _OBS_LOCK:
            _OBS["last_render_error_ts"] = float(time.time())
    return result


@bot.tree.command(name="tnt_health", description="Owner-only: show runtime health snapshot")
async def tnt_health(interaction: discord.Interaction) -> None:
    try:
        uid = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
    except Exception:
        uid = 0
    if OWNER_ID and uid != OWNER_ID:
        try:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
        except Exception:
            pass
        return

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except Exception:
        pass

    async with _OBS_LOCK:
        obs = dict(_OBS)
    async with _PNG_CACHE_LOCK:
        cache_size = len(_PNG_CACHE)
        cache_bytes = int(_PNG_CACHE_BYTES)
        oldest_age = None
        try:
            if _PNG_CACHE:
                oldest_stored = min(float(v[4]) for v in _PNG_CACHE.values())
                oldest_age = max(0.0, _now_s() - oldest_stored)
        except Exception:
            oldest_age = None
    async with _INFLIGHT_LOCK:
        inflight = len(_INFLIGHT)

    async with _RENDER_STATE_LOCK:
        active_renders = int(_RENDER_ACTIVE)
        waiting_renders = int(_RENDER_WAITING)

    avg_ms = None
    try:
        if float(obs.get("render_ms_count", 0.0)) > 0:
            avg_ms = float(obs.get("render_ms_sum", 0.0)) / float(obs.get("render_ms_count", 1.0))
    except Exception:
        avg_ms = None

    # Compute 15m rollups from event deques.
    cutoff = _window_cutoff_s(900)
    fam_roll: dict[str, dict[str, int]] = {}
    queue_max_15m: dict[str, int] = {}
    async with _METRICS_LOCK:
        for (fam, ev), dq in _FAMILY_EVENTS.items():
            cnt = 0
            for t in dq:
                if t >= cutoff:
                    cnt += 1
            fam_roll.setdefault(fam, {})[ev] = int(cnt)
        for (fam, metric), dq in _FAMILY_SAMPLES.items():
            if metric != "render_queue_depth_max":
                continue
            m = 0
            for t, v in dq:
                if t >= cutoff:
                    m = max(m, int(v))
            queue_max_15m[fam] = int(m)

        fam_metrics_snapshot = {k: dict(v) for k, v in _FAMILY_METRICS.items()}

    # Best-effort external HTTP stats
    poly_stats = None
    try:
        from delivery.on_demand_data import get_polygon_http_stats

        poly_stats = get_polygon_http_stats()
    except Exception:
        poly_stats = None

    opt_stats = None
    try:
        from delivery.discord_bot import get_options_http_stats

        opt_stats = get_options_http_stats()
    except Exception:
        opt_stats = None

    # Memory/thread stats (best-effort)
    rss_mb = None
    thread_count = None
    try:
        thread_count = int(threading.active_count())
    except Exception:
        thread_count = None
    try:
        import psutil  # type: ignore

        p = psutil.Process(os.getpid())
        rss_mb = float(p.memory_info().rss) / (1024.0 * 1024.0)
    except Exception:
        rss_mb = None

    uptime_s = max(0.0, _now_s() - float(_START_TS))

    # GPRR snapshot (best-effort)
    gprr_enabled = False
    gprr_profile = None
    gprr_pressure = None
    gprr_lag_ms = None
    gprr_ram_base = None
    gprr_render_ema = None
    gprr_cmd = None
    try:
        if _GPRR is not None and _GPRR.enabled():
            gprr_enabled = True
            p = _GPRR.current_profile()
            gprr_profile = str(p.name)
            gprr_pressure = float(_GPRR.pressure())
            gprr_lag_ms = float(_GPRR.event_loop_lag_ms())
            gprr_ram_base = float(_GPRR.ram_baseline_mb())
            gprr_render_ema = _GPRR.render_avg_ms_ema()
            gprr_cmd = _GPRR.cmd_counts_60s()
    except Exception:
        gprr_enabled = False

    bits: list[str] = []
    bits.append("TNT Health")
    bits.append("\nRuntime")
    bits.append(f"Uptime: {uptime_s/60.0:.1f}m")
    bits.append(f"Python: {sys.version.split()[0]}")
    if rss_mb is not None:
        bits.append(f"Memory RSS: {rss_mb:.1f} MB")
    if thread_count is not None:
        bits.append(f"Threads: {thread_count}")

    bits.append("\nCache")
    bits.append(f"PNG cache: {cache_size}/{_PNG_CACHE_MAX} entries")
    bits.append(f"PNG bytes: {cache_bytes/(1024.0*1024.0):.1f} / {_PNG_CACHE_MAX_BYTES/(1024.0*1024.0):.1f} MB")
    if oldest_age is not None:
        bits.append(f"Oldest entry age: {oldest_age/60.0:.1f}m")
    bits.append(
        f"Hits/Misses (lifetime): {int(obs.get('png_cache_hit', 0.0))}/{int(obs.get('png_cache_miss', 0.0))} evictions={int(obs.get('png_cache_evictions', 0.0))}"
    )

    # Approx hit rate 15m across all families that record cache events.
    hits_15m = sum(int(v.get("cache_hit", 0)) for v in fam_roll.values())
    miss_15m = sum(int(v.get("cache_miss", 0)) for v in fam_roll.values())
    denom = hits_15m + miss_15m
    if denom > 0:
        bits.append(f"Hit rate (15m): {100.0*hits_15m/float(denom):.0f}% ({hits_15m}/{denom})")

    bits.append("\nOI")
    bits.append(f"OI Universe: {len(OI_UNIVERSE)} (Warmed {len(OI_WARMED)} | Extended {len(OI_EXTENDED)})")
    bits.append(f"OI Warmer: enabled | warmed symbols: {', '.join(OI_WARMED[:6])} ...")
    try:
        now_s = float(_now_s())
        fresh_min = int(_oi_cache_fresh_window_min())
        fresh_max_age = float(fresh_min) * 60.0
        async with _PNG_CACHE_LOCK:
            _oi_items = list(_PNG_CACHE.items())

        oi_warm_cache_total = int(len(OI_WARMED))
        any_present = 0
        fresh_present = 0
        for s in OI_WARMED:
            # v11 is current; keep older versions for backward visibility while they drain.
            prefixes = (f"oi_png:v11:{s}:", f"oi_png:v10:{s}:", f"oi_png:v9:{s}:", f"oi_png:v8:{s}:", f"oi_png:v7:{s}:", f"oi_png:v6:{s}:", f"oi_png:v5:{s}:", f"oi_png:v4:{s}:", f"oi_png:v3:{s}:", f"oi_png:v2:{s}:")
            has_any = False
            has_fresh = False
            for k, entry in _oi_items:
                try:
                    if not any(str(k).startswith(pfx) for pfx in prefixes):
                        continue
                    # entry = (png_bytes, meta, expires_ts, size_bytes, stored_ts, png_err)
                    expires_ts = float(entry[2])
                    stored_ts = float(entry[4])
                    if expires_ts < now_s:
                        continue
                    has_any = True
                    if (now_s - stored_ts) <= fresh_max_age:
                        has_fresh = True
                        break
                except Exception:
                    continue
            if has_any:
                any_present += 1
            if has_fresh:
                fresh_present += 1

        if oi_warm_cache_total > 0:
            bits.append(
                f"OI Cache: warmed keys present: {any_present}/{oi_warm_cache_total} (fresh<= {fresh_min}m: {fresh_present}/{oi_warm_cache_total})"
            )
    except Exception:
        pass

    bits.append("\nSingleflight")
    bits.append(f"In-flight keys: {inflight}")
    collapses_15m = sum(int(v.get("singleflight_wait", 0)) for v in fam_roll.values())
    bits.append(f"Collapses (15m): {collapses_15m}")

    bits.append("\nRender")
    bits.append(f"Render cap: {_MAX_CHART_RENDERS}")
    bits.append(f"Active renders: {active_renders}")
    bits.append(f"Waiting renders: {waiting_renders}")
    if avg_ms is not None:
        bits.append(f"Avg render ms (lifetime): {avg_ms:.0f}")
    # Global queue max 15m across families
    if queue_max_15m:
        bits.append("Queue max (15m) by family: " + ", ".join(f"{k}={v}" for k, v in sorted(queue_max_15m.items())))

    bits.append("\nHTTP")
    if isinstance(poly_stats, dict):
        bits.append(
            f"Market data aggs sem: cap={poly_stats.get('cap')} peak={poly_stats.get('peak_active')} active={poly_stats.get('active')} 429_15m={poly_stats.get('err_429_15m')} 5xx_15m={poly_stats.get('err_5xx_15m')}"
        )
    if isinstance(opt_stats, dict):
        bits.append(
            f"Options HTTP sem: cap={opt_stats.get('cap')} peak={opt_stats.get('peak_active')} active={opt_stats.get('active')}"
        )

    bits.append("\nRate limiting")
    cooldown_15m: list[str] = []
    for fam, evs in sorted(fam_roll.items()):
        c = int(evs.get("cooldown_block", 0))
        if c:
            cooldown_15m.append(f"{fam}={c}")
    bits.append("Cooldown blocks (15m): " + (", ".join(cooldown_15m) if cooldown_15m else "0"))

    bits.append("\nPer-family")
    for fam in sorted(fam_metrics_snapshot.keys()):
        d = fam_metrics_snapshot.get(fam, {})
        req = int(d.get("requests_total", 0.0))
        hit = int(d.get("cache_hit", 0.0))
        miss = int(d.get("cache_miss", 0.0))
        sfw = int(d.get("singleflight_waits", 0.0))
        cdblk = int(d.get("cooldown_blocks", 0.0))
        ema_ms = d.get("render_time_ms_ema")
        ema_s = f"{float(ema_ms):.0f}" if isinstance(ema_ms, (int, float)) else "n/a"
        bits.append(f"{fam}: req={req} hit={hit} miss={miss} sf_wait={sfw} ema_ms={ema_s} cd_blk={cdblk}")

    bits.append("\nGPRR")
    bits.append(f"Enabled: {1 if gprr_enabled else 0}")
    if gprr_profile is not None and gprr_pressure is not None:
        bits.append(f"Profile: {gprr_profile} | pressure={gprr_pressure:.2f}")
    if gprr_lag_ms is not None:
        bits.append(f"Event loop lag (EMA): {gprr_lag_ms:.0f} ms")
    if gprr_ram_base is not None:
        bits.append(f"RAM baseline: {gprr_ram_base:.0f} MB")
    if isinstance(gprr_render_ema, (int, float)):
        bits.append(f"Render avg ms (EMA): {float(gprr_render_ema):.0f}")
    if isinstance(gprr_cmd, dict):
        try:
            bits.append(
                "Cmd demand (60s): "
                + " ".join(
                    [
                        f"chart={int(gprr_cmd.get('chart', 0))}",
                        f"oi={int(gprr_cmd.get('oi', 0))}",
                        f"pcr={int(gprr_cmd.get('pcr', 0))}",
                        f"risk={int(gprr_cmd.get('risk', 0))}",
                        f"crypto={int(gprr_cmd.get('crypto', 0))}",
                        f"uniq_sym={int(gprr_cmd.get('unique_symbols', 0))}",
                    ]
                )
            )
        except Exception:
            pass

    msg = "\n".join(bits)
    try:
        await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


async def _safe_ephemeral(interaction: discord.Interaction, message: str) -> None:
    # Never raise from the global error handler.
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
            return
    except Exception:
        pass
    try:
        await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        try:
            await interaction.followup.send(message, ephemeral=True)
        except Exception:
            pass


@bot.tree.error
async def _on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    await _obs_inc("command_errors", 1.0)
    await _fam_event("global", "command_error")

    try:
        print("[TNT][CMD][ERROR]", repr(error))
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        print(tb)
    except Exception:
        pass

    msg = "TNT hit an internal error. If it was a chart, try again in ~30s (it may still be rendering / caching)."
    await _safe_ephemeral(interaction, msg)


async def _write_heartbeat_once() -> None:
    try:
        path = _heartbeat_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        async with _OBS_LOCK:
            obs = dict(_OBS)
        async with _PNG_CACHE_LOCK:
            cache_size = int(len(_PNG_CACHE))
            cache_bytes = int(_PNG_CACHE_BYTES)
        async with _INFLIGHT_LOCK:
            inflight = int(len(_INFLIGHT))
        async with _RENDER_STATE_LOCK:
            active = int(_RENDER_ACTIVE)
            waiting = int(_RENDER_WAITING)

        ts_utc = datetime.now(timezone.utc).isoformat()
        payload = {
            "ts_utc": ts_utc,
            "ts": ts_utc.replace("+00:00", "Z"),
            "pid": int(os.getpid()),
            "mode": _tnt_mode(),
            "entrypoint": _TNT_ENTRYPOINT,
            "uptime_sec": float(max(0.0, _now_s() - float(_START_TS))),
            "uptime_s": float(max(0.0, _now_s() - float(_START_TS))),
            "render": {"cap": int(_MAX_CHART_RENDERS), "active": active, "waiting": waiting},
            "singleflight": {"inflight": inflight},
            "png_cache": {
                "entries": cache_size,
                "max_entries": int(_PNG_CACHE_MAX),
                "bytes": cache_bytes,
                "max_bytes": int(_PNG_CACHE_MAX_BYTES),
            },
            "obs": {
                "render_errors": int(obs.get("render_errors", 0.0)),
                "last_render_error_ts": float(obs.get("last_render_error_ts", 0.0)),
                "command_errors": int(obs.get("command_errors", 0.0)),
                "png_cache_hit": int(obs.get("png_cache_hit", 0.0)),
                "png_cache_miss": int(obs.get("png_cache_miss", 0.0)),
                "png_cache_stale_hit": int(obs.get("png_cache_stale_hit", 0.0)),
            },
        }

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


async def _heartbeat_loop() -> None:
    while True:
        await _write_heartbeat_once()
        await asyncio.sleep(float(_heartbeat_interval_sec()))


# --- Shared macro regime state (used across commands) ---

_MACRO_REGIME_LOCK = asyncio.Lock()
_MACRO_REGIME_STATE: dict[str, object] = {}

_CRYPTO_CONTEXT_LOCK = asyncio.Lock()
_CRYPTO_CONTEXT_CACHE: dict[str, object] = {}

_CRYPTO_POLICY_LINE = "Overlay only — does not flip Macro Regime action."
_CRYPTO_RS_USE_LINE = "Use: Weak crypto RS can signal fragile equity risk appetite."
_CRYPTO_VOL_USE_LINE = "Use: Rising crypto RV can signal rising cross-asset risk sensitivity."
_CRYPTO_WEEKEND_USE_LINE = "Use: Weekend drift can set the tone into Monday's open."
_CRYPTO_DIVERGENCE_USE_LINE = "Use: Divergence can warn equity trend reliability is reduced."


# --- Crypto watchlist (fast text-only summary) ---
_CRYPTO_WL_DEFAULT = ["BTC", "ETH", "XRP", "DOGE", "LTC"]
CRYPTO_WATCHLIST = [
    x.strip().upper()
    for x in (os.getenv("TNT_CRYPTO_WATCHLIST", ",".join(_CRYPTO_WL_DEFAULT)) or "").split(",")
    if x.strip()
]


def _crypto_polygon_ticker(sym: str) -> str:
    return f"X:{(sym or '').strip().upper()}USD"


def _crypto_pair_for_last(symbol_fetch: str) -> str | None:
    """Map Polygon crypto ticker (e.g., X:BTCUSD) to a last-trade pair (e.g., BTC-USD)."""
    s = (symbol_fetch or "").strip().upper()
    if not s.startswith("X:"):
        return None
    try:
        # Common case: X:BTCUSD
        if s.endswith("USD") and len(s) > 4:
            base = s[2:-3]
            if base:
                return f"{base}-USD"
    except Exception:
        return None
    return None


try:
    TNT_TTL_CRYPTO_WATCHLIST_SEC = int(os.getenv("TNT_TTL_CRYPTO_WATCHLIST_SEC", "300"))
except Exception:
    TNT_TTL_CRYPTO_WATCHLIST_SEC = 300
TNT_TTL_CRYPTO_WATCHLIST_SEC = max(30, min(int(TNT_TTL_CRYPTO_WATCHLIST_SEC), 3600))


# Simple TTL cache for the watchlist message
_CRYPTO_WL_CACHE: dict[str, object] = {"ts": 0.0, "text": None}


def _ttl_get_crypto_wl() -> str | None:
    try:
        txt = _CRYPTO_WL_CACHE.get("text")
        ts0 = float(_CRYPTO_WL_CACHE.get("ts") or 0.0)
        if txt and (time.time() - ts0 <= float(TNT_TTL_CRYPTO_WATCHLIST_SEC)):
            return str(txt)
    except Exception:
        return None
    return None


def _ttl_set_crypto_wl(text: str) -> None:
    try:
        _CRYPTO_WL_CACHE["ts"] = float(time.time())
        _CRYPTO_WL_CACHE["text"] = str(text)
    except Exception:
        pass


def _fmt_pct(x: float) -> str:
    sign = "+" if float(x) >= 0 else ""
    return f"{sign}{float(x):.2f}%"


def _ms_to_et_datestr(ms: int) -> str:
    try:
        et_tz = delivery.ET_TZ or timezone.utc
        dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).astimezone(et_tz)
        return dt.strftime("%Y-%m-%d") + " ET"
    except Exception:
        dt = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d") + " UTC"


def _pct_change(closes: list[float], lookback_days: int) -> float | None:
    if not closes or len(closes) < (int(lookback_days) + 1):
        return None
    try:
        start = float(closes[-(int(lookback_days) + 1)])
        end = float(closes[-1])
    except Exception:
        return None
    if not start or start == 0.0:
        return None
    return (end / start - 1.0) * 100.0


async def _fetch_crypto_daily_closes(sym: str, days: int = 31) -> tuple[int | None, list[float]]:
    """Return (asof_ms, closes) from daily Polygon aggs (best-effort)."""

    try:
        from delivery.on_demand_data import polygon_aggs
    except Exception:
        return None, []

    ticker = _crypto_polygon_ticker(sym)
    want_days = max(10, int(days))
    # Ask for a bit more than needed to cover weekends/holes.
    fetch_days = min(120, want_days + 20)
    try:
        payload = await asyncio.to_thread(polygon_aggs, ticker, multiplier=1, timespan="day", days=fetch_days, cache_ttl_sec=60)
    except Exception:
        return None, []
    if not isinstance(payload, dict):
        return None, []
    results = payload.get("results") or []
    if not isinstance(results, list) or not results:
        return None, []
    closes: list[float] = []
    asof_ms = None
    for r in results:
        if not isinstance(r, dict):
            continue
        c = r.get("c")
        t = r.get("t")
        if c is None:
            continue
        try:
            closes.append(float(c))
        except Exception:
            continue
        if t is not None:
            try:
                asof_ms = int(t)
            except Exception:
                pass
    return asof_ms, closes


def _macro_state_max_age_sec() -> int:
    # Prevent showing yesterday's regime line late at night.
    # Default ~3 hours (configurable), per ops guidance: 2–4h.
    try:
        v = int(os.getenv("TNT_MACRO_STATE_MAX_AGE_SEC", "10800"))
    except Exception:
        v = 10800
    return max(600, v)


def _format_et_hhmm(ts: float) -> str | None:
    try:
        tz = delivery.ET_TZ or timezone.utc
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(tz).strftime("%H:%M ET")
    except Exception:
        return None


def _format_et_ymd(ts: float) -> str | None:
    try:
        tz = delivery.ET_TZ or timezone.utc
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
    except Exception:
        return None


def _format_et_mon_dd(ts: float, *, include_dow: bool = False) -> str | None:
    try:
        tz = delivery.ET_TZ or timezone.utc
        fmt = "%a %b %d" if include_dow else "%b %d"
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(tz).strftime(fmt)
    except Exception:
        return None


def _age_days_et(ts: float, now_ts: float | None = None) -> int | None:
    try:
        if ts <= 0:
            return None
        tz = delivery.ET_TZ or timezone.utc
        now = datetime.fromtimestamp(float(now_ts if now_ts is not None else time.time()), tz=timezone.utc).astimezone(tz)
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(tz)
        return int((now.date() - dt.date()).days)
    except Exception:
        return None


def _add_tnt_watermark(
    ax: Any,
    text: str = "TNT",
    *,
    alpha: float = 0.045,
    fontsize: int = 64,
    fontweight: str = "bold",
) -> None:
    """Add a subtle TNT watermark behind plotted data. Best-effort; never raises."""

    def _stamp_visible() -> bool:
        truthy = {"1", "true", "yes", "y", "on"}
        allow = (os.getenv("TNT_RENDER_STAMP_ALLOW", "0") or "0").strip().lower()
        if allow not in truthy:
            return False
        v = (os.getenv("TNT_RENDER_STAMP_VISIBLE", "0") or "0").strip().lower()
        return v in truthy

    def _stamp(fig: Any) -> None:
        try:
            if fig is None:
                return
            if not _stamp_visible():
                return
            if bool(getattr(fig, "_tnt_render_tag_stamped", False)):
                return
            tag = (os.getenv("TNT_RENDER_TAG") or "").strip() or (os.getenv("COMPUTERNAME") or "").strip() or "TNT?"
            fig.text(
                0.01,
                0.01,
                str(tag),
                transform=fig.transFigure,
                fontsize=9,
                color="#c9d1d9",
                alpha=0.70,
                ha="left",
                va="bottom",
                zorder=10,
            )
            setattr(fig, "_tnt_render_tag_stamped", True)
        except Exception:
            return

    try:
        _stamp(getattr(ax, "figure", None))

        # Ensure the axes background patch is behind the watermark.
        try:
            ax.patch.set_zorder(0)
        except Exception:
            pass
        a = float(alpha)
        if not (0.0 <= a <= 1.0):
            a = 0.045
        fs = int(fontsize) if int(fontsize) > 0 else 64

        ax.text(
            0.5,
            0.5,
            str(text),
            transform=ax.transAxes,
            fontsize=fs,
            fontweight=str(fontweight or "bold"),
            color="white",
            alpha=a,
            ha="center",
            va="center",
            zorder=0.5,
        )
    except Exception:
        return


def _add_tnt_watermark_fig(fig: Any, text: str = "TNT") -> None:
    """Centered watermark for multi-subplot charts, behind all axes. Best-effort; never raises."""

    def _stamp_visible() -> bool:
        truthy = {"1", "true", "yes", "y", "on"}
        allow = (os.getenv("TNT_RENDER_STAMP_ALLOW", "0") or "0").strip().lower()
        if allow not in truthy:
            return False
        v = (os.getenv("TNT_RENDER_STAMP_VISIBLE", "0") or "0").strip().lower()
        return v in truthy

    try:
        try:
            if fig is not None and _stamp_visible() and (not bool(getattr(fig, "_tnt_render_tag_stamped", False))):
                tag = (os.getenv("TNT_RENDER_TAG") or "").strip() or (os.getenv("COMPUTERNAME") or "").strip() or "TNT?"
                fig.text(
                    0.01,
                    0.01,
                    str(tag),
                    transform=fig.transFigure,
                    fontsize=9,
                    color="#c9d1d9",
                    alpha=0.70,
                    ha="left",
                    va="bottom",
                    zorder=10,
                )
                setattr(fig, "_tnt_render_tag_stamped", True)
        except Exception:
            pass

        # Force subplot backgrounds behind the watermark.
        try:
            for ax in list(getattr(fig, "axes", []) or []):
                try:
                    ax.patch.set_zorder(0)
                except Exception:
                    continue
        except Exception:
            pass

        # Draw in figure coordinates so it spans the entire chart, but keep zorder low.
        fig.text(
            0.5,
            0.5,
            str(text),
            transform=fig.transFigure,
            fontsize=64,
            fontweight="bold",
            color="white",
            alpha=0.045,
            ha="center",
            va="center",
            zorder=0.5,
        )
    except Exception:
        return


def _show_crypto_context_enabled() -> bool:
    truthy = {"1", "true", "yes", "y", "on"}
    v = (os.getenv("TNT_SHOW_CRYPTO_CONTEXT", "0") or "0").strip().lower()
    if v in truthy:
        return True
    # Optional alias for future overlay expansion (backward compatible).
    v2 = (os.getenv("TNT_ENABLE_OVERLAY_CRYPTO", "0") or "0").strip().lower()
    if v2 in truthy:
        return True
    overlays = (os.getenv("TNT_OVERLAYS", "") or "").strip().lower()
    if overlays:
        toks = {t.strip() for t in overlays.replace(";", ",").split(",") if t.strip()}
        if "crypto" in toks:
            return True
    return False


def _crypto_context_ttl_sec() -> int:
    try:
        # Context derived from daily bars; safe to cache longer.
        v = int(os.getenv("TNT_TTL_CRYPTO_CONTEXT_SEC", "1800"))
    except Exception:
        v = 1800
    return max(60, min(v, 3600))


def _crypto_context_cache_read(now_ts: float | None = None) -> str | None:
    if not _show_crypto_context_enabled():
        return None
    try:
        snap = dict(_CRYPTO_CONTEXT_CACHE)
        exp = float(snap.get("expires_ts") or 0.0)
        if exp <= 0:
            return None
        now = float(now_ts if now_ts is not None else time.time())
        if now >= exp:
            return None
        line = snap.get("line")
        return str(line) if line else None
    except Exception:
        return None


def _compute_crypto_context_line_sync() -> tuple[str | None, float]:
    """Best-effort. Returns (line, asof_ts_utc). Never raises."""
    if not _show_crypto_context_enabled():
        return None, 0.0

    try:
        from delivery.on_demand_data import polygon_aggs
    except Exception:
        return None, 0.0

    sym_spy = "SPY"
    sym_btc = (os.getenv("TNT_CRYPTO_BTC_TICKER", "X:BTCUSD") or "X:BTCUSD").strip().upper()
    sym_eth = (os.getenv("TNT_CRYPTO_ETH_TICKER", "X:ETHUSD") or "X:ETHUSD").strip().upper()

    # Keep this light; rely on Polygon caching as well.
    ttl = _crypto_context_ttl_sec()
    try:
        spy_payload = polygon_aggs(sym_spy, multiplier=1, timespan="day", days=90, cache_ttl_sec=ttl)
        btc_payload = polygon_aggs(sym_btc, multiplier=1, timespan="day", days=90, cache_ttl_sec=ttl)
        eth_payload = polygon_aggs(sym_eth, multiplier=1, timespan="day", days=90, cache_ttl_sec=ttl)
    except Exception:
        return None, 0.0

    try:
        spy = _extract_aggs_close_series(spy_payload)
        btc = _extract_aggs_close_series(btc_payload)
        eth = _extract_aggs_close_series(eth_payload)
    except Exception:
        return None, 0.0

    spy_by_d = {ts.date(): c for ts, c in spy}
    btc_by_d = {ts.date(): c for ts, c in btc}
    eth_by_d = {ts.date(): c for ts, c in eth}
    dates = sorted(set(spy_by_d).intersection(btc_by_d).intersection(eth_by_d))
    if len(dates) < 30:
        return None, 0.0

    computed_ts = time.time()

    # As-of: latest common daily bar date.
    et_tz = delivery.ET_TZ or timezone.utc
    last_d = dates[-1]
    try:
        asof_ts = float(datetime(last_d.year, last_d.month, last_d.day, 0, 0, tzinfo=et_tz).astimezone(timezone.utc).timestamp())
    except Exception:
        asof_ts = 0.0

    # --- RS state (20d change of normalized RS vs SPY, averaged BTC/ETH) ---
    try:
        rs_dates = dates[-60:]
        btc_rs = [float(btc_by_d[d]) / float(spy_by_d[d]) for d in rs_dates]
        eth_rs = [float(eth_by_d[d]) / float(spy_by_d[d]) for d in rs_dates]
        b0 = btc_rs[0] if btc_rs and btc_rs[0] else None
        e0 = eth_rs[0] if eth_rs and eth_rs[0] else None
        btc_rs_n = [v / float(b0) for v in btc_rs] if b0 else btc_rs
        eth_rs_n = [v / float(e0) for v in eth_rs] if e0 else eth_rs
        if len(btc_rs_n) < 25 or len(eth_rs_n) < 25:
            return None, asof_ts
        b20 = _pct(btc_rs_n[-1], btc_rs_n[-21])
        e20 = _pct(eth_rs_n[-1], eth_rs_n[-21])
        chgs = [float(x) for x in (b20, e20) if isinstance(x, (int, float))]
        if not chgs:
            return None, asof_ts
        rs_chg = sum(chgs) / float(len(chgs))
        if rs_chg >= 1.0:
            rs_state = "UP"
        elif rs_chg <= -1.0:
            rs_state = "DOWN"
        else:
            rs_state = "FLAT"
    except Exception:
        return None, asof_ts

    # --- Vol state (14d RV% trend: compare last vs ~10 points ago, averaged BTC/ETH) ---
    try:
        window = int(os.getenv("TNT_CRYPTO_VOL_WINDOW", "14"))
    except Exception:
        window = 14
    window = min(max(window, 7), 60)

    def _rv_last_and_prev(series: list[tuple[datetime, float]]) -> tuple[float | None, float | None]:
        ts = [t for t, _ in series]
        px = [float(p) for _, p in series]
        rets: list[float] = []
        for i in range(1, len(px)):
            if px[i - 1] > 0 and px[i] > 0:
                rets.append(math.log(px[i] / px[i - 1]))
            else:
                rets.append(0.0)
        rvs: list[float] = []
        for i in range(window, len(rets) + 1):
            w = rets[i - window : i]
            sd = _roll_std(w)
            if sd is None:
                continue
            rvs.append(float(sd) * math.sqrt(365.0) * 100.0)
        if len(rvs) < 12:
            return None, None
        last = float(rvs[-1])
        prev = float(rvs[-11])  # ~10 samples ago
        return last, prev

    try:
        b_last, b_prev = _rv_last_and_prev(btc)
        e_last, e_prev = _rv_last_and_prev(eth)
        deltas: list[float] = []
        for last, prev in ((b_last, b_prev), (e_last, e_prev)):
            if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and float(prev) > 0:
                deltas.append((float(last) / float(prev) - 1.0) * 100.0)
        if not deltas:
            return None, asof_ts
        dv = sum(deltas) / float(len(deltas))
        if dv >= 5.0:
            vol_state = "EXPANDING"
        elif dv <= -5.0:
            vol_state = "CONTRACTING"
        else:
            vol_state = "FLAT"
    except Exception:
        return None, asof_ts

    # --- Divergence flag (vs SPY over lookback, sign+mag) ---
    try:
        lookback = int(os.getenv("TNT_CRYPTO_DIVERGENCE_DAYS", "5"))
    except Exception:
        lookback = 5
    lookback = min(max(lookback, 3), 20)
    try:
        threshold = float(os.getenv("TNT_CRYPTO_DIVERGENCE_THRESHOLD", "0.05"))
    except Exception:
        threshold = 0.05
    threshold = min(max(threshold, 0.01), 0.25)

    divergence = False
    try:
        if len(dates) >= (lookback + 2):
            d0 = dates[-(lookback + 1)]
            d1 = dates[-1]
            spy_ret = float(spy_by_d[d1]) / float(spy_by_d[d0]) - 1.0
            for cret in (
                float(btc_by_d[d1]) / float(btc_by_d[d0]) - 1.0,
                float(eth_by_d[d1]) / float(eth_by_d[d0]) - 1.0,
            ):
                diff = float(cret) - float(spy_ret)
                sign_div = (cret >= 0) != (spy_ret >= 0)
                mag_div = abs(diff) >= float(threshold)
                if sign_div and mag_div:
                    divergence = True
                    break
    except Exception:
        divergence = False

    bits: list[str] = []
    if divergence:
        age_d = _age_days_et(asof_ts)
        bits.append(f"DIVERGENCE ({age_d}d)" if isinstance(age_d, int) and age_d >= 0 else "DIVERGENCE")
    bits.append(f"RS {rs_state}")
    bits.append(f"Vol {vol_state}")

    confirming = (not divergence) and (rs_state == "UP") and (vol_state in {"FLAT", "CONTRACTING"})
    confidence_word = "Confirming" if confirming else "Warning"
    hhmm = _format_et_hhmm(float(computed_ts))
    suffix = f" | as-of {hhmm}" if hhmm else ""
    return f"Crypto Context ({confidence_word}): " + " | ".join(bits) + suffix, asof_ts


async def _get_crypto_context_line() -> str | None:
    if not _show_crypto_context_enabled():
        return None

    cached = _crypto_context_cache_read()
    if cached:
        return cached

    async with _CRYPTO_CONTEXT_LOCK:
        cached2 = _crypto_context_cache_read()
        if cached2:
            return cached2

        line, asof_ts = await asyncio.to_thread(_compute_crypto_context_line_sync)
        if not line:
            _CRYPTO_CONTEXT_CACHE.clear()
            return None

        now = time.time()
        ttl = _crypto_context_ttl_sec()
        _CRYPTO_CONTEXT_CACHE.clear()
        _CRYPTO_CONTEXT_CACHE.update(
            {
                "line": str(line),
                "asof_ts": float(asof_ts or 0.0),
                "computed_ts": float(now),
                "expires_ts": float(now + float(ttl)),
            }
        )
        return str(line)


async def _maybe_append_crypto_context_if_macro(content: str, macro_line: str | None, *, sep: str) -> str:
    if not content:
        return content
    if not macro_line:
        return content
    if not _show_crypto_context_enabled():
        return content
    try:
        line = await _get_crypto_context_line()
        if line:
            return content + sep + line
    except Exception:
        return content
    return content


def _format_macro_regime_line_from_state(state: dict[str, object]) -> str | None:
    if not state:
        return None

    try:
        ts = float(state.get("ts") or 0.0)
    except Exception:
        ts = 0.0

    if ts > 0:
        age = time.time() - ts
        if age > float(_macro_state_max_age_sec()):
            return "Macro Regime unavailable"

    try:
        regime_label = str(state.get("regime_label") or "").strip()
        action = str(state.get("action") or "").strip()
        agreement = state.get("agreement")
        strength = state.get("strength_sigma")
        stability = state.get("stability_days")
    except Exception:
        return None

    if not regime_label or not action:
        return None

    bits: list[str] = [f"Macro Regime: {regime_label} ({action.title()})"]
    try:
        a = int(agreement) if agreement is not None else None
        s = float(strength) if strength is not None else None
        st = int(stability) if stability is not None else None
    except Exception:
        a = None
        s = None
        st = None

    if a is not None:
        bits.append(f"{a}/4")
    if s is not None:
        bits.append(f"{s:.1f}σ")
    if st is not None:
        bits.append(f"{st}d")

    if ts > 0:
        asof = _format_et_hhmm(ts)
        if asof:
            bits.append(f"as-of {asof}")

    return " | ".join(bits)


async def _set_macro_regime_state(state: dict[str, object]) -> None:
    # Minimal shared state for cross-command cohesion.
    # Keys: regime_sign, regime_label, agreement, strength_sigma, stability_days, conflict_flag, action, ts
    now = time.time()
    payload = dict(state or {})
    payload["ts"] = now
    async with _MACRO_REGIME_LOCK:
        _MACRO_REGIME_STATE.clear()
        _MACRO_REGIME_STATE.update(payload)


async def _get_macro_regime_line() -> str | None:
    snapshot: dict[str, object] = {}
    stale = False
    async with _MACRO_REGIME_LOCK:
        if not _MACRO_REGIME_STATE:
            return None
        snapshot = dict(_MACRO_REGIME_STATE)
        try:
            ts = float(snapshot.get("ts") or 0.0)
        except Exception:
            ts = 0.0
        if ts > 0 and (time.time() - ts) > float(_macro_state_max_age_sec()):
            stale = True
            _MACRO_REGIME_STATE.clear()

    line = _format_macro_regime_line_from_state(snapshot)
    if stale:
        # Explicitly tell users it's unavailable rather than silently omitting.
        return "Macro Regime unavailable"
    return line


# === TNT Options Universe (52) ===

OI_WARMED = [
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "AMZN",
    "META",
    "GOOGL",
    "AMD",
    "NFLX",
    "COIN",
    "MSTR",
]

OI_EXTENDED = [
    # Financials
    "BAC",
    "JPM",
    "WFC",
    "GS",
    "MS",

    # Tech / Semi / AI
    "AVGO",
    "INTC",
    "MU",
    "ARM",
    "SMCI",
    "QCOM",
    "ADBE",
    "SNOW",

    # Consumer / Growth
    "DIS",
    "UBER",
    "SHOP",
    "PYPL",
    "ABNB",
    "SQ",

    # ETFs
    "XLF",
    "XLK",
    "XLE",
    "XLV",
    "ARKK",
    "SMH",

    # Macro / Rates / Credit
    "TLT",
    "IEF",
    "HYG",

    # High-interest / Momentum
    "PLTR",
    "RIVN",
    "LCID",
    "GME",
    "AMC",

    # Volatility / Index (include only if supported)
    "VIX",
    # "SPX","XSP",
]

OI_UNIVERSE = OI_WARMED + OI_EXTENDED
OI_WARMED_SET = set(OI_WARMED)
OI_UNIVERSE_SET = set(OI_UNIVERSE)

# Back-compat: used by /oi autocomplete.
_OI_SYMBOL_SUGGESTIONS: tuple[str, ...] = tuple(OI_UNIVERSE)


def _normalize_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2:
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
    return value.strip()


def _dotenv_get_value(path: Path, key: str) -> str | None:
    if not path.exists() or not path.is_file():
        return None

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Support: KEY=VALUE (no export keyword)
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        return _normalize_env_value(v)
    return None


def _dotenv_load_file_into_environ(path: Path, *, preserve_existing: set[str]) -> dict[str, str]:
    """Best-effort: load KEY=VALUE lines into os.environ.

    - Does not overwrite keys that existed before this loader ran.
    - Supports optional leading `export `.
    - Ignores blank lines and comments.
    """

    loaded: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return loaded

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return loaded

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        if not key:
            continue
        if key in preserve_existing:
            continue
        val = _normalize_env_value(v)
        try:
            os.environ[key] = val
            loaded[key] = val
        except Exception:
            continue

    return loaded


def _dotenv_load_into_environ() -> None:
    """Load repo `.env` and `.env.local` into `os.environ`.

    This repo often configures run-mode gates via env files. Python won't load
    them automatically, so we do it here (without overriding *real* env vars).
    """

    preserve_existing = set(os.environ.keys())

    loaded_env = _dotenv_load_file_into_environ(PROJECT_ROOT / ".env", preserve_existing=preserve_existing)
    # Allow .env.local to override values that came from .env (but not values that
    # existed in the process environment before we started).
    preserve_existing2 = preserve_existing.difference(set(loaded_env.keys()))
    loaded_local = _dotenv_load_file_into_environ(PROJECT_ROOT / ".env.local", preserve_existing=preserve_existing2)

    try:
        if loaded_env or loaded_local:
            print(f"[TNT][ENV] loaded_dotenv env={len(loaded_env)} local={len(loaded_local)}")
    except Exception:
        pass


def _resolve_discord_token() -> str | None:
    # Prefer explicit env vars.
    for env_key in ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"):
        raw = os.getenv(env_key)
        if raw and raw.strip():
            return _normalize_env_value(raw)

    # Fall back to local env files in repo root.
    for env_path in (PROJECT_ROOT / ".env.local", PROJECT_ROOT / ".env"):
        val = _dotenv_get_value(env_path, "DISCORD_BOT_TOKEN")
        if val:
            return val

    return None


def _trueish(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _format_epoch_et(ts: int | float | str | None) -> str:
    if ts in (None, ""):
        return "never"
    try:
        ts_int = int(float(ts))
    except (TypeError, ValueError):
        return str(ts)

    tz = delivery.ET_TZ or timezone.utc
    try:
        dt = datetime.fromtimestamp(ts_int, tz=timezone.utc).astimezone(tz)
    except (OSError, OverflowError, ValueError):
        return str(ts_int)
    return dt.strftime("%Y-%m-%d %H:%M")


def _task_status(task: asyncio.Task | None) -> str:
    if task is None:


        return "idle"
    if task.cancelled():
        return "cancelled"
    if task.done():
        try:
            exc = task.exception()
        except Exception:  # pragma: no cover - defensive
            exc = None
        if exc is None:
            return "stopped"
        return f"error({type(exc).__name__})"
    return "running"


def _build_status_report() -> str:
    now_et = delivery._now_et()
    now_str = now_et.strftime("%Y-%m-%d %H:%M:%S")
    session = delivery.market_session_et(now_et.astimezone(timezone.utc))

    futures_payload, futures_status, futures_reason = delivery._futures_context_status(now_et.isoformat())
    futures_age = futures_payload.get("age_minutes")
    futures_age_text = f"{futures_age:.1f} min" if isinstance(futures_age, (int, float)) else "unknown"
    futures_ts = futures_payload.get("computed_dt") or futures_payload.get("computed_ts")
    futures_ts_text = delivery._format_ts_et(futures_ts) if futures_ts else "unknown"
    futures_bits = [futures_status.upper(), f"age {futures_age_text}", f"updated {futures_ts_text}"]
    if futures_reason and futures_status != "fresh":
        futures_bits.append(futures_reason)
    futures_line = " | ".join(bit for bit in futures_bits if bit)

    data_stale, data_age, data_ts = delivery._calc_data_stale()
    data_age_text = f"{data_age:.1f} min" if isinstance(data_age, (int, float)) else "unknown"
    data_ts_text = delivery._format_ts_et(data_ts) if data_ts else "unknown"
    data_prefix = "STALE" if data_stale else "OK"
    data_line = f"{data_prefix} (age {data_age_text}; last bar {data_ts_text})"

    technical_quality = "UNKNOWN"
    stale_symbols: list[str] = []
    missing_symbols: list[str] = []
    try:
        agent_payload = delivery._build_agent_payload(
            delivery.STARTUP_SYMBOLS,
            generated_at=now_et,
            post_type="status",
            extra_meta={"source": "status"},
        )
    except Exception:  # noqa: BLE001 - status should not explode
        agent_payload = None

    if isinstance(agent_payload, dict):
        meta = agent_payload.get("meta")
        if isinstance(meta, dict):
            dq = meta.get("data_quality")
            if isinstance(dq, dict):
                tech = dq.get("technical_state")
                if isinstance(tech, str):
                    technical_quality = tech.upper()
            stale_meta = meta.get("stale_symbols")
            if isinstance(stale_meta, dict):
                stale_symbols = sorted(stale_meta.keys())
            missing_meta = meta.get("missing_symbols")
            if isinstance(missing_meta, list):
                missing_symbols = sorted(str(sym).upper() for sym in missing_meta)

    tech_bits = [technical_quality]
    if stale_symbols:
        tech_bits.append(f"stale: {', '.join(stale_symbols)}")
    if missing_symbols:
        tech_bits.append(f"missing: {', '.join(missing_symbols)}")
    technical_line = " | ".join(tech_bits)

    last_daily = getattr(delivery, "_last_daily_post_et_date", None)
    if hasattr(last_daily, "strftime"):
        daily_line = last_daily.strftime("%Y-%m-%d")  # type: ignore[arg-type]
    else:
        daily_line = "never"

    last_signal_map = getattr(delivery, "_last_signal_post_by_symbol", {}) or {}
    signal_items = []
    if isinstance(last_signal_map, dict):
        for sym, ts in sorted(last_signal_map.items(), key=lambda kv: kv[1], reverse=True)[:5]:
            signal_items.append(f"{sym} {_format_epoch_et(ts)}")
    signals_line = ", ".join(signal_items) if signal_items else "none"

    daily_task = getattr(delivery, "_autopost_daily_task", None)
    signal_task = getattr(delivery, "_autopost_signal_task", None)
    task_line = (
        f"daily={_task_status(daily_task)} | "
        f"signal={_task_status(signal_task)} | "
        f"stale_guard={'ON' if getattr(delivery, '_data_stale_paused', False) else 'OFF'}"
    )

    strict_flag = "ON" if delivery.STRICT_CONTRACTS else "OFF"
    dry_run_flag = "ON" if _trueish(os.getenv("DRY_RUN")) else "OFF"
    canary_str = str(CANARY_ID) if CANARY_ID else "disabled"

    lines = [
        "🛰️ **Autopost Status Snapshot**",
        f"• Session: {session} | Now (ET): {now_str}",
        f"• Futures: {futures_line}",
        f"• Data feed: {data_line}",
        f"• Technical state: {technical_line}",
        f"• Daily autopost: {daily_line}",
        f"• Signal autopost: {signals_line}",
        f"• Loops: {task_line}",
        (
            "• Flags: "
            f"STRICT_CONTRACTS={strict_flag} | DRY_RUN={dry_run_flag} | "
            f"Contract={delivery.AUTOPOST_CONTRACT_VERSION} | Canary={canary_str}"
        ),
    ]

    return "\n".join(lines)


@bot.command(name="morning")
async def morning_cmd(ctx: commands.Context, symbol: str = "SPX"):
    await ctx.trigger_typing()
    result = build_morning_brief(symbol=symbol)
    brief = result["brief_markdown"]

    if len(brief) > 1900:
        brief = brief[:1900] + "\n\n*(truncated)*"

    await ctx.send(f"📈 Morning Brief for **{symbol.upper()}**\n\n{brief}")


@bot.tree.command(name="force_daily", description="Force-post Daily Prep to canary (owner only).")
async def force_daily(interaction: discord.Interaction) -> None:
    print("[slash] force_daily invoked")
    responded = False

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
        print("[slash] force_daily deferred successfully")
    except discord.NotFound:
        responded = False
        print("[slash] force_daily defer: interaction not found")
    except Exception as exc:  # pragma: no cover - diagnostic
        print(f"[slash] force_daily defer error: {exc}")
        responded = False

    if OWNER_ID and interaction.user.id != OWNER_ID:
        if responded:
            await interaction.followup.send("Not authorized.", ephemeral=True)
        return

    channel = interaction.client.get_channel(CANARY_ID) if CANARY_ID else interaction.channel
    if channel is None:
        if responded:
            await interaction.followup.send(
                "Canary channel not found. Check DISCORD_CANARY_CHANNEL_ID.",
                ephemeral=True,
            )
        return

    try:
        rendered = await asyncio.to_thread(delivery.build_daily_prep_render)
        print("[slash] force_daily render ready")
        await delivery._publish_autopost_render(
            channel,
            rendered,
            builder="build_daily_prep_render",
            label="daily_prep_manual",
            symbol="",
            analysis_mode="db",
            output_mode="strict",
            strict_contracts=False,
            allow_contract_violations=True,
        )
        print("[slash] force_daily publish complete")
        if responded:
            await interaction.followup.send("✅ Posted (or stand-down if gated).", ephemeral=True)
            print("[slash] force_daily followup sent")
        else:
            await channel.send("✅ Posted (or stand-down if gated). (interaction expired, posted anyway)")
    except Exception as exc:  # noqa: BLE001
        print(f"[slash] force_daily exception: {exc}")
        if responded:
            await interaction.followup.send(f"❌ force_daily failed: {exc}", ephemeral=True)
        else:
            await channel.send(f"❌ force_daily failed post-expiry: {exc}")


@bot.tree.command(name="force_focus", description="Force-post Focus List to canary (owner only).")
async def force_focus(interaction: discord.Interaction) -> None:
    responded = False

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.NotFound:
        responded = False

    if OWNER_ID and interaction.user.id != OWNER_ID:
        if responded:
            await interaction.followup.send("Not authorized.", ephemeral=True)
        return

    channel = interaction.client.get_channel(CANARY_ID) if CANARY_ID else interaction.channel
    if channel is None:
        if responded:
            await interaction.followup.send(
                "Canary channel not found. Check DISCORD_CANARY_CHANNEL_ID.",
                ephemeral=True,
            )
        return

    try:
        rendered = await asyncio.to_thread(delivery.build_focus_list_render)
        await delivery._publish_autopost_render(
            channel,
            rendered,
            builder="build_focus_list_render",
            label="focus_list",
            symbol="",
            analysis_mode="db",
            output_mode="strict",
            strict_contracts=False,
            allow_contract_violations=True,
        )
        if responded:
            await interaction.followup.send("✅ Focus List posted (or stand-down if gated).", ephemeral=True)
        else:
            await channel.send("✅ Focus List posted (or stand-down if gated). (interaction expired, posted anyway)")
    except Exception as exc:  # noqa: BLE001
        if responded:
            await interaction.followup.send(f"❌ force_focus failed: {exc}", ephemeral=True)
        else:
            await channel.send(f"❌ force_focus failed post-expiry: {exc}")


@bot.tree.command(name="force_intraday", description="Force-post Intraday Update to canary (owner only).")
async def force_intraday(interaction: discord.Interaction) -> None:
    responded = False

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.NotFound:
        responded = False

    if OWNER_ID and interaction.user.id != OWNER_ID:
        if responded:
            await interaction.followup.send("Not authorized.", ephemeral=True)
        return

    channel = interaction.client.get_channel(CANARY_ID) if CANARY_ID else interaction.channel
    if channel is None:
        if responded:
            await interaction.followup.send(
                "Canary channel not found. Check DISCORD_CANARY_CHANNEL_ID.",
                ephemeral=True,
            )
        return

    try:
        rendered = await asyncio.to_thread(delivery.build_intraday_update_render)
        await delivery._publish_autopost_render(
            channel,
            rendered,
            builder="build_intraday_update_render",
            label="intraday_update",
            symbol="",
            requester_id=getattr(interaction.user, "id", None),
            request_kind="on_demand_chart",
            client=getattr(interaction, "client", None),
            queue_mode="raise",
            analysis_mode="db",
            output_mode="strict",
            strict_contracts=False,
            allow_contract_violations=True,
        )
        if responded:
            await interaction.followup.send("✅ Intraday Update posted (or stand-down if gated).", ephemeral=True)
        else:
            await channel.send(
                "✅ Intraday Update posted (or stand-down if gated). (interaction expired, posted anyway)"
            )
    except Exception as exc:  # noqa: BLE001
        if responded:
            await interaction.followup.send(f"❌ force_intraday failed: {exc}", ephemeral=True)
        else:
            await channel.send(f"❌ force_intraday failed post-expiry: {exc}")


@bot.tree.command(name="analyze", description="On-demand analysis for a single symbol (e.g., AMD, SPY).")
@app_commands.describe(symbol="Ticker symbol (e.g., AMD)")
async def analyze(interaction: discord.Interaction, symbol: str) -> None:
    responded = False

    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
    except Exception:
        responded = False

    symbol_input = (symbol or "").strip()
    print(f"[slash] analyze invoked symbol={symbol_input!r}")

    async def _reply(message: str) -> None:
        nonlocal responded
        if responded:
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True

    sym_norm = delivery._normalize_symbol_token(symbol_input)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like AMD or SPY.")
        return

    sym = sym_norm

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    ack_update: Callable[[str], Awaitable[None]] | None = None
    try:
        ack_update = await _instant_ack_editor(interaction)
    except Exception:
        ack_update = None

    async def _status(message: str) -> None:
        if ack_update is not None:
            await ack_update(message)
        else:
            await _reply(message)

    ack_update: Callable[[str], Awaitable[None]] | None = None
    try:
        ack_update = await _instant_ack_editor(interaction)
    except Exception:
        ack_update = None

    async def _status(message: str) -> None:
        if ack_update is not None:
            await ack_update(message)
        else:
            await _reply(message)

    ack_update: Callable[[str], Awaitable[None]] | None = None
    try:
        ack_update = await _instant_ack_editor(interaction)
    except Exception:
        ack_update = None

    async def _status(message: str) -> None:
        if ack_update is not None:
            await ack_update(message)
        else:
            await _reply(message)

    attempt = 0
    final_render = None
    symbol_meta = sym
    analysis_mode = "db"
    output_mode = "strict"
    meta: dict[str, object] = {}
    last_issue: str | None = None

    while attempt < 2:
        try:
            render = await asyncio.to_thread(build_on_demand_analyze_render, sym)
        except Exception as exc:  # noqa: BLE001
            await _reply(f"Failed to build analysis: {exc}")
            return

        symbol_meta, analysis_mode, output_mode, meta = delivery._analysis_meta_from_render(render, sym)
        symbol_meta = symbol_meta or sym or "N/A"
        stand_down = bool(meta.get("stand_down")) if isinstance(meta, dict) else False

        if not stand_down:
            final_render = render
            break

        last_issue = str(meta.get("issue") or "Data not ready")

        if attempt == 0 and symbol_meta not in {"", "N/A"}:
            await _reply(f"⚙️ Warming up {symbol_meta}… retrying in 3s.")
            try:
                bootstrap_result = await delivery.ensure_symbol_ready(symbol_meta)
            except Exception as exc:  # noqa: BLE001
                last_issue = f"Bootstrap failed: {exc}"
                break

            warnings = bootstrap_result.get("warnings") if isinstance(bootstrap_result, dict) else None
            if warnings:
                print(f"[BOOTSTRAP][WARN] {symbol_meta}: {'; '.join(str(w) for w in warnings)}")

            await asyncio.sleep(3)
            attempt += 1
            continue

        final_render = render
        break

    if final_render is None:
        final_render = render  # fall back to last render for posting/logging

    # Belt-and-suspenders: ensure final user-facing output always flows through
    # the stable renderer so no future fast path can bypass the Numeric Bias Pack.
    try:
        ap = getattr(final_render, "agent_payload", None)
        meta_blob = ap.get("meta") if isinstance(ap, dict) else None
        signal_payload = meta_blob.get("signal_payload") if isinstance(meta_blob, dict) else None
        display_symbol = None
        if isinstance(meta_blob, dict):
            display_symbol = meta_blob.get("display_symbol")
        if not display_symbol and isinstance(ap, dict):
            display_symbol = ap.get("symbol")
        display_symbol = str(display_symbol or symbol_meta or sym or "N/A")

        final_text = _render_analyze_text(
            {
                "symbol": display_symbol,
                "signal_payload": signal_payload if isinstance(signal_payload, dict) else None,
                "body": getattr(final_render, "text", "") or "",
            }
        )
        final_render = delivery.RenderedPost(
            text=final_text,
            agent_payload=getattr(final_render, "agent_payload", None),
            files=getattr(final_render, "files", None),
        )
    except Exception:
        pass

    label = f"analyze_{symbol_meta.lower()}" if symbol_meta and symbol_meta != "N/A" else "analyze"
    stand_down_final = bool(meta.get("stand_down")) if isinstance(meta, dict) else False

    publish_kwargs: dict[str, object] = {}
    if stand_down_final:
        publish_kwargs["strict_contracts"] = False
        publish_kwargs["allow_contract_violations"] = True

    try:
        await delivery._publish_autopost_render(
            channel=channel,
            render=final_render,
            builder="on_demand_analyze",
            label=label,
            symbol=symbol_meta,
            requester_id=getattr(interaction.user, "id", None),
            request_kind="on_demand_text",
            client=getattr(interaction, "client", None),
            queue_mode="raise",
            analysis_mode=analysis_mode,
            output_mode=output_mode,
            **publish_kwargs,
        )
    except delivery.PublishQueuedError as exc:
        await _reply(f"⏳ Queued for next window (~{int(round(exc.wait_seconds))}s).")
        return
    except Exception as exc:  # noqa: BLE001
        await _reply(f"Failed to post analysis: {exc}")
        return

    print(
        f"[slash] analyze posted symbol={symbol_meta} stand_down={stand_down_final}"
    )

    if stand_down_final:
        reason = last_issue or str(meta.get("issue") or "Data not ready")
        await _reply(f"⚠️ Stand-down for **{symbol_meta}** — {reason}.")
    else:
        await _reply(f"✅ Posted analysis for **{symbol_meta}**.")


@bot.tree.command(name="regime", description="Show TNT regime + permissions for a symbol.")
@app_commands.describe(symbol="Ticker symbol (e.g., SPY)", tier="Output tier")
@app_commands.choices(
    tier=[
        Choice(name="default", value="default"),
        Choice(name="verbose", value="verbose"),
        Choice(name="pro", value="pro"),
    ]
)
async def regime(
    interaction: discord.Interaction,
    symbol: str,
    tier: Choice[str] | None = None,
) -> None:
    responded = False

    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
    except Exception:
        responded = False

    async def _reply(message: str) -> None:
        nonlocal responded
        if responded:
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True

    symbol_input = (symbol or "").strip()
    sym_norm = delivery._normalize_symbol_token(symbol_input)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like AMD or SPY.")
        return

    sym = sym_norm

    try:
        built = await asyncio.to_thread(delivery.build_signal_payload, sym)
    except Exception as exc:  # noqa: BLE001
        await _reply(f"Failed to load signal payload: {exc}")
        return

    if not built:
        await _reply(f"No signal payload available for {sym}.")
        return

    payload, _prob, gate = built
    tnt = payload.get("tnt") if isinstance(payload, dict) else None
    if not isinstance(tnt, dict):
        await _reply(f"TNT regime unavailable for {sym}.")
        return

    tnt_regime = str(tnt.get("regime") or "UNKNOWN").upper()
    tnt_posture = str(tnt.get("posture") or "UNKNOWN").upper()
    conf = tnt.get("confidence")
    conf_txt = "n/a"
    if isinstance(conf, (int, float)):
        conf_txt = f"{float(conf):.2f}"

    perms = tnt.get("permissions") if isinstance(tnt.get("permissions"), dict) else {}

    def _perm(key: str) -> str:
        return str(perms.get(key) or "n/a").upper()

    reasons = tnt.get("reasons") if isinstance(tnt.get("reasons"), list) else []
    why = ", ".join(str(x) for x in reasons[:6]) if reasons else "n/a"

    flips = {}
    try:
        flips = delivery.tnt_flip_triggers(payload)
    except Exception:
        flips = {"bull": "Bull: reclaim + hold above Pivot AND confirmation OK", "bear": "Bear: break + hold below S1 AND confirmation OK", "neutral": "If still NEUTRAL/UNKNOWN: wait"}

    tier_val = (tier.value if isinstance(tier, Choice) else "default").strip().lower()
    if tier_val not in {"default", "verbose", "pro"}:
        tier_val = "default"

    # Default: 5 lines max
    if tier_val == "default":
        confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper()
        edge = payload.get("edge")
        edge_txt = "n/a"
        if isinstance(edge, (int, float)):
            edge_txt = f"{float(edge):.3f}"
        pct = tnt.get("pct_to_pivot")
        pct_txt = "n/a"
        if isinstance(pct, (int, float)):
            pct_txt = f"{float(pct):.2f}%"

        lines = [
            f"TNT Regime — {sym}",
            f"{tnt_regime} | posture {tnt_posture} | conf {conf_txt}",
            f"confirm {confirm} | edge {edge_txt} | pivot_dist {pct_txt}",
            f"Flip: {flips.get('bull')}",
            f"Flip: {flips.get('bear')} | {flips.get('neutral')}",
        ]
        await _reply("\n".join(lines[:5]))
        return

    # Verbose / Pro
    lines: list[str] = []
    lines.append(f"TNT Regime — {sym}")
    lines.append(f"Regime: {tnt_regime} | Posture: {tnt_posture} | Confidence: {conf_txt}")
    lines.append(f"Why: {why}")
    lines.append("Flip Triggers:")
    lines.append(f"- {flips.get('bull')}")
    lines.append(f"- {flips.get('bear')}")
    lines.append(f"- {flips.get('neutral')}")
    lines.append("Permissions:")
    lines.append(
        f"- Trend: {_perm('trend_continuation')} | Pullbacks: {_perm('pullbacks')} | Breakouts: {_perm('breakouts')}"
    )
    lines.append(
        f"- Mean reversion: {_perm('mean_reversion')} | Countertrend: {_perm('countertrend')} | Size: {_perm('size')}"
    )

    if tier_val == "pro":
        # Best setup type + no-trade flags
        best = "WAIT"
        if tnt_regime == "TREND":
            best = "Trend continuation (break + hold with confirmation)"
        elif tnt_regime == "RANGE":
            best = "Mean reversion at edges (small size)"
        elif tnt_regime == "TRANSITION":
            best = "Pullback only (small size)"

        why_ctx = None
        try:
            why_ctx = delivery.tnt_why_payload(payload, gate)
        except Exception:
            why_ctx = None

        flags: list[str] = []
        if isinstance(why_ctx, dict):
            if str(why_ctx.get("freshness") or "").upper() == "STALE":
                flags.append("data stale")
            conf_state = ((why_ctx.get("confirmation") or {}) if isinstance(why_ctx.get("confirmation"), dict) else {})
            if str(conf_state.get("state") or "").upper() in {"UNKNOWN", "NEUTRAL"}:
                flags.append("confirmation neutral")
            vg = why_ctx.get("vix_gate")
            if isinstance(vg, dict) and str(vg.get("mode") or "").upper() not in {"OK", "UNKNOWN"}:
                flags.append(f"vix gate {vg.get('mode')}")

        if not flags and tnt_regime in {"DO_NOTHING"}:
            flags.append("stand down")

        lines.append("Best setup right now:")
        lines.append(f"- {best}")
        lines.append("No-trade flags:")
        lines.append(f"- {', '.join(flags) if flags else 'n/a'}")

    await _reply("\n".join(lines))


@bot.tree.command(name="why", description="Explain why TNT is standing down / allowing trades.")
@app_commands.describe(symbol="Ticker symbol (e.g., SPY)")
async def why(interaction: discord.Interaction, symbol: str) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
    except Exception:
        responded = False

    async def _reply(message: str) -> None:
        nonlocal responded
        if responded:
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True

    symbol_input = (symbol or "").strip()
    sym_norm = delivery._normalize_symbol_token(symbol_input)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like AMD or SPY.")
        return

    sym = sym_norm
    try:
        built = await asyncio.to_thread(delivery.build_signal_payload, sym)
    except Exception as exc:  # noqa: BLE001
        await _reply(f"Failed to load signal payload: {exc}")
        return

    if not built:
        await _reply(f"No signal payload available for {sym}.")
        return

    payload, _prob, gate = built
    try:
        why_ctx = delivery.tnt_why_payload(payload, gate)
    except Exception as exc:  # noqa: BLE001
        await _reply(f"Why unavailable: {exc}")
        return

    tnt = payload.get("tnt") if isinstance(payload, dict) else None
    tnt_regime = str((tnt or {}).get("regime") or "UNKNOWN").upper()
    tnt_posture = str((tnt or {}).get("posture") or "UNKNOWN").upper()

    conf = why_ctx.get("confirmation") if isinstance(why_ctx.get("confirmation"), dict) else {}
    vix_gate = why_ctx.get("vix_gate") if isinstance(why_ctx.get("vix_gate"), dict) else {}

    lines: list[str] = []
    lines.append(f"WHY — {sym}")
    lines.append(f"Regime: {tnt_regime} | posture {tnt_posture}")
    lines.append(f"Confirmation: {conf.get('state','?')} (VIX {conf.get('vix_trend','?')}, SQQQ {conf.get('sqqq_dir','?')})")
    lines.append(f"Pivot distance: {why_ctx.get('distance_to_pivot','n/a')}")
    lines.append(f"Edge/Conviction: {why_ctx.get('edge','n/a')} / {why_ctx.get('conviction','n/a')}")
    lines.append(f"VIX gate: {vix_gate.get('mode','?')} | {vix_gate.get('reason','n/a')}")
    lines.append(f"Freshness: {why_ctx.get('freshness','n/a')}")
    await _reply("\n".join(lines))

    return


@bot.tree.command(name="chart", description="Post a price chart PNG for a symbol.")
@app_commands.describe(
    symbol="Ticker symbol (e.g., NVDA)",
)
async def chart(
    interaction: discord.Interaction,
    symbol: str,
) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        # 40060: already acknowledged
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        # Prefer followups to avoid 40060 when we've already deferred.
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    async def _status(message: str) -> None:
        # Prefer editing the deferred ephemeral response to keep UX clean.
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=message)
                return
        except Exception:
            pass
        await _reply(message)

    def _collapse_blank_lines(text: str, *, max_consecutive: int = 2) -> str:
        # Contract forbids 3+ consecutive blank lines.
        if max_consecutive < 1:
            max_consecutive = 1
        text_norm = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        needle = "\n" * (max_consecutive + 2)
        repl = "\n" * (max_consecutive + 1)
        while needle in text_norm:
            text_norm = text_norm.replace(needle, repl)
        return text_norm

    sym_norm = delivery._normalize_symbol_token(symbol)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like NVDA, SPY, or BTC.")
        return

    # Map common crypto shorthand to Polygon crypto tickers.
    # Without this, `/chart BTC` can resolve to an equity ticker instead of spot crypto.
    sym_display = sym_norm
    sym_fetch = sym_norm
    is_crypto = False
    try:
        crypto_aliases = {"BTC", "ETH", "XRP", "DOGE", "LTC"}
        if sym_norm.startswith("X:"):
            is_crypto = True
            sym_fetch = sym_norm
        elif sym_norm in crypto_aliases:
            # Strong default: treat bare crypto symbols as crypto, not equities.
            # Allow env override, but only if it points to a crypto ticker.
            is_crypto = True
            if sym_norm == "BTC":
                env_sym = (os.getenv("TNT_CRYPTO_BTC_TICKER", "") or "").strip().upper()
                sym_fetch = env_sym if env_sym.startswith("X:") else _crypto_polygon_ticker("BTC")
            elif sym_norm == "ETH":
                env_sym = (os.getenv("TNT_CRYPTO_ETH_TICKER", "") or "").strip().upper()
                sym_fetch = env_sym if env_sym.startswith("X:") else _crypto_polygon_ticker("ETH")
            else:
                sym_fetch = _crypto_polygon_ticker(sym_norm)
    except Exception:
        is_crypto = False
        sym_fetch = sym_norm

    sym = sym_display

    # Soft per-user cooldown (chart family)
    try:
        cooldown = _cooldown_env_sec("chart")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="chart",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="chart", bucket="chart", wait_s=wait_s)
            return
    except Exception:
        pass

    # Opinionated defaults to reduce confusion:
    # - Standard lookback: 6 months
    # - Standard interval: 1 day
    window_days = 180
    mult, timespan = 1, "day"

    # User-selectable panels/overlays (max 5 chosen at once).
    # - price/volume/rsi/macd/stoch => panels
    # - vwap => overlay on price (or standalone panel if price not selected)
    allowed_choices: tuple[str, ...] = ("price", "volume", "rsi", "macd", "stoch", "vwap")
    default_selected: set[str] = {"price", "volume", "rsi", "macd", "vwap"}

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    interval_label = f"{mult}{timespan[0]}"

    # For crypto charts, show a fresher "Last" in the header without changing the 180d daily series.
    api_key_for_crypto_last = None
    try:
        api_key_for_crypto_last, _base_url, _provider = delivery._polygon_key_and_base()
    except Exception:
        api_key_for_crypto_last = None

    async def _get_crypto_last_snapshot() -> tuple[float | None, int | None, str | None]:
        if not is_crypto:
            return None, None, None
        if not api_key_for_crypto_last:
            return None, None, None

        pair = _crypto_pair_for_last(sym_fetch)
        if pair:
            try:
                from delivery.on_demand_data import polygon_last_crypto

                px, asof_ms = await asyncio.to_thread(polygon_last_crypto, pair, api_key_for_crypto_last, 10)
                if px is not None:
                    return float(px), (int(asof_ms) if asof_ms is not None else None), "last trade"
            except Exception:
                pass

        # Fallback: latest 1m close (still pretty "live").
        try:
            from delivery.on_demand_data import polygon_aggs

            payload = await asyncio.to_thread(polygon_aggs, sym_fetch, multiplier=1, timespan="minute", days=1)
            rows = payload.get("results") if isinstance(payload, dict) else None
            if isinstance(rows, list) and rows:
                last_row = rows[-1]
                px = last_row.get("c") if isinstance(last_row, dict) else None
                t = last_row.get("t") if isinstance(last_row, dict) else None
                if px is not None:
                    try:
                        return float(px), (int(t) if t is not None else None), "1m close"
                    except Exception:
                        return float(px), None, "1m close"
        except Exception:
            pass

        return None, None, None

    pivot_levels_for_chart: dict[str, float] | None = None
    if not is_crypto:
        try:
            piv_info = delivery.get_latest_daily_pivots(sym)
            piv = piv_info.get("piv") if isinstance(piv_info, dict) else None
            if isinstance(piv, dict):
                levels: dict[str, float] = {}
                for k in ("R3", "R2", "R1", "P", "S1", "S2", "S3"):
                    v = piv.get(k)
                    if v is None:
                        continue
                    try:
                        levels[k] = float(v)
                    except Exception:
                        continue
                if levels:
                    pivot_levels_for_chart = levels
        except Exception:
            pivot_levels_for_chart = None

    def _sma(values: list[float], window: int) -> list[float]:
        n = len(values)
        out = [math.nan] * n
        if window <= 1 or n == 0:
            return values[:]
        csum = 0.0
        for i, v in enumerate(values):
            csum += v
            if i >= window:
                csum -= values[i - window]
            if i >= window - 1:
                out[i] = csum / float(window)
        return out

    def _ema(values: list[float], window: int) -> list[float]:
        n = len(values)
        if n == 0:
            return []
        out = [math.nan] * n
        if window <= 1:
            return values[:]
        alpha = 2.0 / (float(window) + 1.0)
        ema_val = float(values[0])
        out[0] = ema_val
        for i in range(1, n):
            ema_val = (values[i] * alpha) + (ema_val * (1.0 - alpha))
            out[i] = ema_val
        return out

    def _ema_nan(values: list[float], window: int) -> list[float]:
        # EMA that preserves leading NaNs (used for MACD signal smoothing)
        n = len(values)
        if n == 0:
            return []
        out = [math.nan] * n
        alpha = 2.0 / (float(window) + 1.0)
        ema_val: float | None = None
        for i, v in enumerate(values):
            if math.isnan(v):
                continue
            if ema_val is None:
                ema_val = float(v)
            else:
                ema_val = (float(v) * alpha) + (ema_val * (1.0 - alpha))
            out[i] = ema_val
        return out

    def _vwap(highs: list[float], lows: list[float], closes: list[float], vols: list[float]) -> list[float]:
        # Simple session-to-date VWAP on the provided bars.
        n = len(closes)
        out = [math.nan] * n
        cum_pv = 0.0
        cum_v = 0.0
        for i in range(n):
            v = float(vols[i] or 0.0)
            tp = (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
            cum_pv += tp * v
            cum_v += v
            if cum_v > 0:
                out[i] = cum_pv / cum_v
        return out

    def _rsi(closes: list[float], period: int = 14) -> list[float]:
        n = len(closes)
        out = [math.nan] * n
        if n < period + 1:
            return out

        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, period + 1):
            change = closes[i] - closes[i - 1]
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))

        avg_gain = sum(gains) / float(period)
        avg_loss = sum(losses) / float(period)

        if avg_loss == 0:
            out[period] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[period] = 100.0 - (100.0 / (1.0 + rs))

        for i in range(period + 1, n):
            change = closes[i] - closes[i - 1]
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            avg_gain = ((avg_gain * (period - 1)) + gain) / float(period)
            avg_loss = ((avg_loss * (period - 1)) + loss) / float(period)
            if avg_loss == 0:
                out[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                out[i] = 100.0 - (100.0 / (1.0 + rs))

        return out

    def _macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        n = len(closes)
        macd_line = [math.nan] * n
        for i in range(n):
            if math.isnan(ema12[i]) or math.isnan(ema26[i]):
                continue
            macd_line[i] = ema12[i] - ema26[i]
        signal = _ema_nan(macd_line, 9)
        hist = [math.nan] * n
        for i in range(n):
            if math.isnan(macd_line[i]) or math.isnan(signal[i]):
                continue
            hist[i] = macd_line[i] - signal[i]
        return macd_line, signal, hist

    def _stoch(highs: list[float], lows: list[float], closes: list[float], k_period: int = 14, d_period: int = 3) -> tuple[list[float], list[float]]:
        n = len(closes)
        k = [math.nan] * n
        for i in range(n):
            if i < k_period - 1:
                continue
            window_high = max(highs[i - k_period + 1 : i + 1])
            window_low = min(lows[i - k_period + 1 : i + 1])
            denom = window_high - window_low
            if denom <= 0:
                k[i] = 0.0
            else:
                k[i] = 100.0 * (closes[i] - window_low) / denom
        d = _sma([0.0 if math.isnan(v) else v for v in k], d_period)
        # Re-nan leading points for d
        for i in range(n):
            if i < (k_period - 1) + (d_period - 1):
                d[i] = math.nan
        return k, d

    def _try_render_price_chart_png(
        selected: set[str],
        *,
        profile: RenderProfile | None = None,
        dpi: int | None = None,
        **_kwargs,
    ) -> tuple[bytes | None, str | None, dict[str, object] | None]:
        try:
            import matplotlib

            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
            from matplotlib.patches import Rectangle
            from matplotlib.transforms import blended_transform_factory
        except Exception:
            return None, "matplotlib_missing", None

        def _safe_float(x: object) -> float | None:
            try:
                if x is None:
                    return None
                return float(x)
            except Exception:
                return None

        def _bucket_trend(last: float | None, prev: float | None, *, flat_bps: float) -> str:
            if last is None or prev is None or prev == 0:
                return "UNKNOWN"
            rel_bps = ((float(last) - float(prev)) / float(prev)) * 10_000.0
            if abs(rel_bps) <= float(flat_bps):
                return "FLAT"
            return "UP" if rel_bps > 0 else "DOWN"

        def _tod_bucket(now_et: datetime) -> str:
            # Coarse buckets only (interpretable; no microstructure claims).
            try:
                hhmm = int(now_et.hour) * 60 + int(now_et.minute)
            except Exception:
                return "UNKNOWN"
            rth_open = 9 * 60 + 30
            rth_close = 16 * 60
            if hhmm < rth_open:
                return "PRE"
            if hhmm >= rth_close:
                return "AH"
            # RTH
            if rth_open <= hhmm <= (10 * 60 + 15):
                return "OPEN"
            if (11 * 60 + 30) <= hhmm <= (13 * 60 + 30):
                return "LUNCH"
            if (15 * 60) <= hhmm < rth_close:
                return "POWER"
            return "RTH"

        def _rv_state(closes: list[float]) -> tuple[str, float | None]:
            # Realized vol proxy: compare last 10-bar std(logret) vs prior 10.
            try:
                if len(closes) < 25:
                    return "UNKNOWN", None
                rets: list[float] = []
                for i in range(1, len(closes)):
                    if closes[i - 1] > 0 and closes[i] > 0:
                        rets.append(math.log(closes[i] / closes[i - 1]))
                    else:
                        rets.append(0.0)
                if len(rets) < 22:
                    return "UNKNOWN", None

                def _std(vals: list[float]) -> float | None:
                    if len(vals) < 3:
                        return None
                    m = sum(vals) / float(len(vals))
                    var = sum((v - m) ** 2 for v in vals) / float(max(1, len(vals) - 1))
                    return math.sqrt(var)

                last = _std(rets[-10:])
                prev = _std(rets[-20:-10])
                if last is None or prev is None or prev <= 0:
                    return "UNKNOWN", None

                ratio = float(last) / float(prev)
                # Conservative thresholds; avoid flapping.
                if ratio >= 1.20:
                    return "EXPANDING", ratio
                if ratio <= 0.83:
                    return "CONTRACTING", ratio
                return "FLAT", ratio
            except Exception:
                return "UNKNOWN", None

        def _trend_confirmed(closes: list[float]) -> tuple[bool, float | None]:
            # Linear slope over last ~30 bars; interpret as directional bias, not a signal.
            try:
                n = min(30, len(closes))
                if n < 10:
                    return False, None
                y = [float(v) for v in closes[-n:]]
                x = list(range(n))
                x_mean = sum(x) / float(n)
                y_mean = sum(y) / float(n)
                cov = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
                var = sum((x[i] - x_mean) ** 2 for i in range(n))
                if var <= 0:
                    return False, None
                slope = cov / var
                # Normalize slope to bps per bar.
                last_px = float(y[-1]) if y and y[-1] else None
                if last_px is None or last_px <= 0:
                    return False, slope
                slope_bps = (float(slope) / float(last_px)) * 10_000.0
                return abs(slope_bps) >= 6.0, slope_bps
            except Exception:
                return False, None

        def _classify_regime(ctx: dict[str, object]) -> str:
            # Interpretable, rule-based, and robust (default is TRANSITION).
            edge = _safe_float(ctx.get("edge"))
            vix_trend = str(ctx.get("vix_trend") or "UNKNOWN")
            sqqq_trend = str(ctx.get("sqqq_trend") or "UNKNOWN")
            vol_state = str(ctx.get("vol_state") or "UNKNOWN")
            tod = str(ctx.get("tod") or "UNKNOWN")
            gamma_wall_near = bool(ctx.get("gamma_wall_near"))
            trend_ok = bool(ctx.get("trend_confirmed"))
            signals_conflict = bool(ctx.get("signals_conflict"))

            if edge is None or edge < 0.02:
                return "DO_NOTHING"

            # Conflict => stand down. (Protects against overfitting to one input.)
            if signals_conflict:
                return "DO_NOTHING"

            # Expansion / open volatility can be transition even with edge.
            if tod in {"OPEN"} and (vol_state == "EXPANDING" or vix_trend == "UP"):
                return "TRANSITION"

            # RANGE: gamma/pin-like state + suppressed vol + weak edge.
            if gamma_wall_near and vix_trend in {"FLAT", "DOWN", "UNKNOWN"} and edge < 0.03:
                return "RANGE"

            # TREND: sufficient edge + volatility not rising + directional confirmation.
            if edge >= 0.05 and vix_trend in {"FLAT", "DOWN", "UNKNOWN"} and sqqq_trend in {"FLAT", "DOWN", "UNKNOWN"} and trend_ok:
                return "TREND"

            # Default.
            return "TRANSITION"

        def _regime_style(regime: str, *, up: str, down: str, wick: str) -> dict[str, object]:
            reg = (regime or "TRANSITION").strip().upper()
            if reg not in {"TREND", "RANGE", "TRANSITION", "DO_NOTHING"}:
                reg = "TRANSITION"

            # Reuse existing palette tokens already present in charts.
            yellow = "#ffd400"
            styles: dict[str, dict[str, object]] = {
                "TREND": {
                    "badge": "",
                    "banner_color": up,
                    "bg": up,
                    "bg_alpha": 0.06,
                    "market_state": "Trend-leaning / improving tape",
                    "stance": "Pullbacks / break+retest only",
                    "caption": "Trend regime: break + retest / pullbacks only",
                    "allowed": ["ALLOW: Pullbacks", "ALLOW: Break + retest"],
                    "forbidden": ["AVOID: Mean reversion"],
                    "risk": {"size": "NORMAL", "duration": "SHORT-MED", "best": "Defined-risk (spreads)"},
                    "levels": {
                        "P": (1.85, 0.38),
                        "R2": (1.45, 0.40),
                        "S2": (1.45, 0.40),
                        "R1": (1.15, 0.28),
                        "S1": (1.15, 0.28),
                        "R3": (0.95, 0.20),
                        "S3": (0.95, 0.20),
                    },
                },
                "RANGE": {
                    "badge": "",
                    "banner_color": wick,
                    "bg": wick,
                    "bg_alpha": 0.055,
                    "market_state": "Suppressed volatility / mean-revert risk",
                    "stance": "Wait or fade extremes only",
                    "caption": "Range regime: fade edges only (avoid chasing)",
                    "allowed": ["ALLOW: Edge fades", "ALLOW: Calendars"],
                    "forbidden": ["AVOID: Breakouts", "AVOID: Momentum"],
                    "risk": {"size": "SMALL", "duration": "SHORT", "best": "Spreads / calendars"},
                    "levels": {
                        "R1": (1.85, 0.55),
                        "S1": (1.85, 0.55),
                        "R2": (1.25, 0.30),
                        "S2": (1.25, 0.30),
                        "R3": (1.05, 0.22),
                        "S3": (1.05, 0.22),
                        "P": (0.95, 0.18),
                    },
                },
                "TRANSITION": {
                    "badge": "",
                    "banner_color": yellow,
                    "bg": yellow,
                    "bg_alpha": 0.050,
                    "market_state": "Transition / mixed signals",
                    "stance": "Small size; wait for clarity",
                    "caption": "Transition: reduce size; wait for acceptance",
                    "allowed": ["ALLOW: Small size only"],
                    "forbidden": ["AVOID: Full risk"],
                    "risk": {"size": "SMALL", "duration": "SHORT", "best": "Defined-risk only"},
                    "levels": {
                        "R1": (1.20, 0.32),
                        "S1": (1.20, 0.32),
                        "P": (1.20, 0.32),
                        "R2": (1.05, 0.22),
                        "S2": (1.05, 0.22),
                        "R3": (0.95, 0.18),
                        "S3": (0.95, 0.18),
                    },
                },
                "DO_NOTHING": {
                    "badge": "",
                    "banner_color": down,
                    "bg": down,
                    "bg_alpha": 0.045,
                    "market_state": "Stand down / low edge or conflict",
                    "stance": "Do nothing",
                    "caption": "Do nothing: protect capital; wait",
                    "allowed": [],
                    "forbidden": ["AVOID: Everything"],
                    "risk": {"size": "NONE", "duration": "NONE", "best": "None"},
                    "levels": {
                        "R3": (0.85, 0.10),
                        "R2": (0.85, 0.10),
                        "R1": (0.85, 0.10),
                        "P": (0.85, 0.10),
                        "S1": (0.85, 0.10),
                        "S2": (0.85, 0.10),
                        "S3": (0.85, 0.10),
                    },
                },
            }
            return styles[reg]

        try:
            from delivery.on_demand_data import polygon_aggs
            agg = polygon_aggs(sym_fetch, multiplier=mult, timespan=timespan, days=window_days)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "POLYGON_API_KEY" in msg:
                return None, "polygon_key_missing", None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                return None, f"http_{status}", None
            return None, "bars_unavailable", None

        results = agg.get("results") or []
        if not results:
            return None, "no_bars", None

        tz = delivery.ET_TZ or timezone.utc
        xs: list[datetime] = []
        opens: list[float] = []
        closes: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        vols: list[float] = []
        for row in results:
            try:
                t_ms = float(row.get("t"))
                o = float(row.get("o"))
                c = float(row.get("c"))
                h = float(row.get("h"))
                l = float(row.get("l"))
                v = float(row.get("v") or 0.0)
            except Exception:
                continue
            if c <= 0:
                continue
            dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(tz)
            xs.append(dt)
            opens.append(o if o > 0 else c)
            closes.append(c)
            highs.append(h if h > 0 else c)
            lows.append(l if l > 0 else c)
            vols.append(v)

        if len(xs) < 2:
            return None, "insufficient_bars", None

        lo = min(closes)
        hi = max(closes)
        stats: dict[str, object] = {"first": float(closes[0]), "last": float(closes[-1]), "low": float(lo), "high": float(hi)}

        # --- Premium regime engine (runs before chart styling) ---
        now_et = datetime.now(delivery.ET_TZ or timezone.utc)
        tod = _tod_bucket(now_et)
        vol_state, vol_ratio = _rv_state(closes)
        trend_ok, slope_bps = _trend_confirmed(closes)

        # VWAP (for regime context) is computed even if user doesn't select it.
        vwap_for_ctx = _vwap(highs, lows, closes, vols)
        vwap_last = None
        try:
            if vwap_for_ctx and not math.isnan(float(vwap_for_ctx[-1])):
                vwap_last = float(vwap_for_ctx[-1])
        except Exception:
            vwap_last = None

        last_px = float(closes[-1])
        pivot_p = None
        pivot_r1 = None
        pivot_s1 = None
        if pivot_levels_for_chart:
            pivot_p = _safe_float(pivot_levels_for_chart.get("P"))
            pivot_r1 = _safe_float(pivot_levels_for_chart.get("R1"))
            pivot_s1 = _safe_float(pivot_levels_for_chart.get("S1"))

        def _near_level(px: float, level: float | None, *, pct: float) -> bool:
            if level is None or level <= 0:
                return False
            return abs(px - float(level)) <= max(float(level) * float(pct), 0.5)

        # "Gamma wall near" proxy: pinned near a key level during contracting/flat vol.
        gamma_wall_near = (
            (vol_state in {"CONTRACTING", "FLAT"})
            and (
                _near_level(last_px, pivot_p, pct=0.0018)
                or _near_level(last_px, pivot_r1, pct=0.0018)
                or _near_level(last_px, pivot_s1, pct=0.0018)
                or _near_level(last_px, vwap_last, pct=0.0018)
            )
        )

        # VIX + SQQQ direction (best-effort; cached by polygon_aggs).
        vix_trend = "UNKNOWN"
        sqqq_trend = "UNKNOWN"
        try:
            vix_sym = (os.getenv("TNT_VIX_TICKER", "I:VIX") or "I:VIX").strip().upper()
            vix_payload = polygon_aggs(vix_sym, multiplier=1, timespan="day", days=10)
            vix_rows = vix_payload.get("results") or []
            vix_closes: list[float] = []
            for r in vix_rows:
                try:
                    vix_closes.append(float(r.get("c")))
                except Exception:
                    continue
            if len(vix_closes) >= 4:
                vix_trend = _bucket_trend(vix_closes[-1], vix_closes[-4], flat_bps=25.0)
        except Exception:
            vix_trend = "UNKNOWN"

        try:
            sqqq_sym = (os.getenv("TNT_SQQQ_TICKER", "SQQQ") or "SQQQ").strip().upper()
            sqqq_payload = polygon_aggs(sqqq_sym, multiplier=1, timespan="day", days=10)
            sqqq_rows = sqqq_payload.get("results") or []
            sqqq_closes: list[float] = []
            for r in sqqq_rows:
                try:
                    sqqq_closes.append(float(r.get("c")))
                except Exception:
                    continue
            if len(sqqq_closes) >= 4:
                sqqq_trend = _bucket_trend(sqqq_closes[-1], sqqq_closes[-4], flat_bps=18.0)
        except Exception:
            sqqq_trend = "UNKNOWN"

        # Model edge from signals DB (best-effort).
        edge = None
        if not is_crypto:
            try:
                from delivery.discord_bot_head import get_latest_signal

                sig = get_latest_signal(sym)
                if sig and len(sig) >= 5:
                    edge = _safe_float(sig[4])
            except Exception:
                edge = None

        # Macro conflict flag (if /risk_on_off ran recently).
        macro_conflict = False
        try:
            if _MACRO_REGIME_STATE:
                macro_conflict = bool(dict(_MACRO_REGIME_STATE).get("conflict_flag"))
        except Exception:
            macro_conflict = False

        # Simple conflict heuristic: risk proxies disagree.
        proxy_conflict = False
        try:
            if vix_trend in {"UP", "DOWN"} and sqqq_trend in {"UP", "DOWN"} and vix_trend != sqqq_trend:
                proxy_conflict = True
        except Exception:
            proxy_conflict = False

        ctx: dict[str, object] = {
            "edge": edge,
            "vix_trend": vix_trend,
            "sqqq_trend": sqqq_trend,
            "vol_state": vol_state,
            "vol_ratio": vol_ratio,
            "tod": tod,
            "trend_confirmed": bool(trend_ok),
            "slope_bps_per_bar": slope_bps,
            "gamma_wall_near": bool(gamma_wall_near),
            "signals_conflict": bool(macro_conflict or proxy_conflict),
            "macro_conflict": bool(macro_conflict),
            "proxy_conflict": bool(proxy_conflict),
            "near_pivot": bool(_near_level(last_px, pivot_p, pct=0.0025)),
            "near_vwap": bool(_near_level(last_px, vwap_last, pct=0.0025)),
        }

        regime = _classify_regime(ctx)
        stats["regime"] = regime
        stats["regime_ctx"] = ctx

        # Indicators (computed only if requested)
        rsi14 = None
        macd_line = macd_signal = macd_hist = None
        stoch_k = stoch_d = None
        vwap_line = None

        if "rsi" in selected:
            rsi14 = _rsi(closes, 14)
        if "macd" in selected:
            macd_line, macd_signal, macd_hist = _macd(closes)
        if "stoch" in selected:
            stoch_k, stoch_d = _stoch(highs, lows, closes)
        if "vwap" in selected:
            vwap_line = _vwap(highs, lows, closes, vols)

        panels: list[str] = []
        if "price" in selected:
            panels.append("price")
        if "volume" in selected:
            panels.append("volume")
        if "rsi" in selected:
            panels.append("rsi")
        if "macd" in selected:
            panels.append("macd")
        if "stoch" in selected:
            panels.append("stoch")
        # VWAP is normally an overlay on price; if price isn't selected,
        # render VWAP as its own panel.
        if "vwap" in selected and "price" not in selected:
            panels.append("vwap")

        if not panels:
            return None, "no_panels_selected", None

        nrows = len(panels)
        height = 3.4 + (nrows - 1) * 2.15
        fig, axes = plt.subplots(
            nrows=nrows,
            ncols=1,
            sharex=True,
            figsize=(11.25, height),
            gridspec_kw={"height_ratios": [3.0] + [1.25] * (nrows - 1)},
        )
        if nrows == 1:
            axes = [axes]

        ax_price = axes[0]
        # Dark theme to resemble trading platforms.
        fig.patch.set_facecolor("#0b0f14")
        for ax in axes:
            ax.set_facecolor("#0b0f14")
            ax.tick_params(colors="#c9d1d9")
            for spine in ax.spines.values():
                spine.set_color("#2d333b")

        # Background regime shading (subtle, across all panels).
        up_color = "#00ff66"
        down_color = "#ff3344"
        wick_color = "#c9d1d9"
        style = _regime_style(regime, up=up_color, down=down_color, wick=wick_color)
        try:
            bg = str(style.get("bg") or wick_color)
            bg_alpha = float(style.get("bg_alpha") or 0.05)
            for ax in axes:
                ax.add_patch(
                    Rectangle(
                        (0.0, 0.0),
                        1.0,
                        1.0,
                        transform=ax.transAxes,
                        facecolor=bg,
                        edgecolor="none",
                        alpha=bg_alpha,
                        zorder=0.1,
                    )
                )
        except Exception:
            pass

        xnums = mdates.date2num(xs)
        dx = (xnums[1] - xnums[0]) if len(xnums) > 1 else (1.0 / (24.0 * 60.0))
        candle_width = max(dx * 0.75, 1e-6)

        if panels[0] == "price":
            # Candlesticks (green/red) + wicks.
            for i in range(len(xnums)):
                o = opens[i]
                c = closes[i]
                h = highs[i]
                l = lows[i]
                color = up_color if c >= o else down_color

                body_low = min(o, c)
                body_h = abs(c - o)
                if body_h <= 0:
                    body_h = max((hi - lo) * 0.0002, 1e-6)

                ax_price.add_patch(
                    Rectangle(
                        (xnums[i] - candle_width / 2.0, body_low),
                        candle_width,
                        body_h,
                        facecolor=color,
                        edgecolor="none",
                        linewidth=0.0,
                        alpha=0.95,
                        antialiased=False,
                        zorder=2.0,
                    )
                )
                try:
                    ax_price.vlines([xnums[i]], [l], [h], color=wick_color, linewidth=0.9, alpha=0.85, zorder=2.0)
                except Exception:
                    pass

        # Support/Resistance: draw R/S pivot ladder straight across.
        # Per user preference: resistance=green, support=red.
        if panels[0] == "price" and pivot_levels_for_chart:
            r_color = up_color
            s_color = down_color

            levels_style = style.get("levels") if isinstance(style.get("levels"), dict) else {}

            def _lw_alpha(key: str, *, default_w: float, default_a: float) -> tuple[float, float]:
                if isinstance(levels_style, dict) and key in levels_style:
                    try:
                        w, a = levels_style[key]
                        return float(w), float(a)
                    except Exception:
                        return float(default_w), float(default_a)
                return float(default_w), float(default_a)

            # Draw in a consistent order.
            for key in ("R3", "R2", "R1"):
                level = pivot_levels_for_chart.get(key)
                if level is None:
                    continue
                lw, a = _lw_alpha(key, default_w=1.15, default_a=0.42)
                ax_price.axhline(level, color=r_color, linewidth=lw, alpha=a)
            p_level = pivot_levels_for_chart.get("P")
            if p_level is not None:
                lw, a = _lw_alpha("P", default_w=1.0, default_a=0.28)
                ax_price.axhline(p_level, color=wick_color, linewidth=lw, alpha=a)
            for key in ("S1", "S2", "S3"):
                level = pivot_levels_for_chart.get(key)
                if level is None:
                    continue
                lw, a = _lw_alpha(key, default_w=1.15, default_a=0.42)
                ax_price.axhline(level, color=s_color, linewidth=lw, alpha=a)

            # Add compact labels inside the chart so we don't collide with right-side axis ticks.
            try:
                trans = blended_transform_factory(ax_price.transAxes, ax_price.transData)
                x_frac = 0.875

                def _label(level_key: str, y_val: float, color: str) -> None:
                    ax_price.text(
                        x_frac,
                        y_val,
                        level_key,
                        transform=trans,
                        color=color,
                        fontsize=8,
                        alpha=0.9,
                        ha="left",
                        va="center",
                        clip_on=True,
                        bbox={"facecolor": "#0b0f14", "alpha": 0.18, "edgecolor": "none", "pad": 1.5},
                    )

                for key in ("R3", "R2", "R1"):
                    lvl = pivot_levels_for_chart.get(key)
                    if lvl is None:
                        continue
                    _label(key, float(lvl), r_color)
                if p_level is not None:
                    _label("P", float(p_level), wick_color)
                for key in ("S1", "S2", "S3"):
                    lvl = pivot_levels_for_chart.get(key)
                    if lvl is None:
                        continue
                    _label(key, float(lvl), s_color)
            except Exception:
                pass

            # Dynamic level emphasis caption + permission badges.
            try:
                caption = str(style.get("caption") or "").strip()
                if caption:
                    ax_price.text(
                        0.01,
                        0.02,
                        caption,
                        transform=ax_price.transAxes,
                        fontsize=8,
                        color="#c9d1d9",
                        ha="left",
                        va="bottom",
                        bbox={"facecolor": "#0b0f14", "alpha": 0.35, "edgecolor": "#2d333b", "pad": 3.0},
                        zorder=5.0,
                    )

                allowed = style.get("allowed") if isinstance(style.get("allowed"), list) else []
                forbidden = style.get("forbidden") if isinstance(style.get("forbidden"), list) else []
                badge_lines: list[str] = []
                for x in (allowed or [])[:2]:
                    badge_lines.append(str(x))
                for x in (forbidden or [])[:2]:
                    badge_lines.append(str(x))
                if badge_lines:
                    ax_price.text(
                        0.62,
                        0.98,
                        "\n".join(badge_lines),
                        transform=ax_price.transAxes,
                        fontsize=8,
                        color="#c9d1d9",
                        ha="left",
                        va="top",
                        bbox={"facecolor": "#0b0f14", "alpha": 0.35, "edgecolor": "#2d333b", "pad": 4.0},
                        zorder=5.0,
                    )

                risk = style.get("risk") if isinstance(style.get("risk"), dict) else {}
                ax_price.text(
                    0.01,
                    0.16,
                    "\n".join(
                        [
                            "RISK POSTURE",
                            f"Regime: {regime}",
                            f"Size: {risk.get('size', 'SMALL')}",
                            f"Duration: {risk.get('duration', 'SHORT')}",
                            f"Best: {risk.get('best', 'Defined-risk')}",
                        ]
                    ),
                    transform=ax_price.transAxes,
                    fontsize=8,
                    color="#c9d1d9",
                    ha="left",
                    va="bottom",
                    bbox={"facecolor": "#0b0f14", "alpha": 0.35, "edgecolor": "#2d333b", "pad": 4.0},
                    zorder=5.0,
                )
            except Exception:
                pass

        # 10-day projection: extend current trend (linear fit on recent closes).
        try:
            proj_days = 10
            fit_n = min(30, len(closes))
            if fit_n >= 5:
                y_fit = [float(v) for v in closes[-fit_n:]]
                x_fit = list(range(fit_n))
                x_mean = sum(x_fit) / float(fit_n)
                y_mean = sum(y_fit) / float(fit_n)
                cov = sum((x_fit[i] - x_mean) * (y_fit[i] - y_mean) for i in range(fit_n))
                var = sum((x_fit[i] - x_mean) ** 2 for i in range(fit_n))
                if var > 0:
                    slope = cov / var
                    intercept = y_mean - (slope * x_mean)

                    def _add_trading_days(start: datetime, days: int) -> list[datetime]:
                        out: list[datetime] = []
                        cur = start
                        while len(out) < days:
                            cur = cur + timedelta(days=1)
                            if cur.weekday() >= 5:
                                continue
                            out.append(cur)
                        return out

                    future_xs = _add_trading_days(xs[-1], proj_days)
                    future_xnums = mdates.date2num(future_xs)
                    # Continue the fitted line forward from the last observed index.
                    base_idx = fit_n - 1
                    proj_y = [intercept + slope * (base_idx + k) for k in range(1, proj_days + 1)]

                    if panels[0] == "price":
                        ax_price.axvline(xnums[-1], color=wick_color, linewidth=0.9, alpha=0.25, linestyle="--")
                        ax_price.plot(
                            [xnums[-1]] + list(future_xnums),
                            [closes[-1]] + proj_y,
                            color=wick_color,
                            linewidth=1.1,
                            alpha=0.75,
                            linestyle=(0, (4, 3)),
                            label="Projection (10d trend)",
                        )
                        # Ensure the x-axis includes the projection window.
                        ax_price.set_xlim(xnums[0] - dx, max(future_xnums) + dx * 6)
        except Exception:
            pass

        # Overlays.
        if panels[0] == "price" and vwap_line is not None:
            ax_price.plot(xnums, vwap_line, linewidth=1.15, color="#ffa657", label="VWAP")

        selected_label = ",".join([p for p in ("price", "volume", "rsi", "macd", "stoch", "vwap") if p in selected])
        title_bits = [f"{interval_label}, {window_days}d", f"sel: {selected_label or 'none'}"]

        # Watermark: add once (figure-centered) and behind data.
        _add_tnt_watermark_fig(fig)

        # Regime banner (top, always visible).
        try:
            badge = str(style.get("badge") or "")
            market_state = str(style.get("market_state") or "").strip()
            stance = str(style.get("stance") or "").strip()
            banner_color = str(style.get("banner_color") or "#c9d1d9")
            title = f"REGIME: {regime} {badge}".strip()
            banner = "\n".join([x for x in [title, f"Market State: {market_state}" if market_state else "", f"Professional Stance: {stance}" if stance else ""] if x])
            fig.text(
                0.5,
                0.985,
                banner,
                ha="center",
                va="top",
                fontsize=10,
                color=banner_color,
                bbox={"facecolor": "#0b0f14", "alpha": 0.45, "edgecolor": "#2d333b", "pad": 5.0},
                zorder=10.0,
            )
        except Exception:
            pass

        if panels[0] == "price":
            ax_price.set_title(f"{sym} — Candles ({' | '.join(title_bits)})", color="#c9d1d9")
            ax_price.set_ylabel("Price", color="#c9d1d9")
            ax_price.grid(True, alpha=0.18, linestyle="--")
            handles, labels = ax_price.get_legend_handles_labels()
            if labels:
                ax_price.legend(loc="upper left", fontsize=8, ncol=3, framealpha=0.15)
            ax_price.yaxis.tick_right()
            ax_price.yaxis.set_label_position("right")
            ax_price.xaxis_date()
        else:
            # If price isn't selected, re-purpose the first axis.
            ax_price.set_title(f"{sym} — {panels[0].upper()} ({' | '.join(title_bits)})", color="#c9d1d9")

        idx = 0
        for panel in panels:
            if idx == 0 and panel == "price":
                idx += 1
                continue

            if idx == 0 and panel != "price":
                ax = axes[0]
            else:
                ax = axes[idx]
            if panel == "volume":
                vol_colors = [up_color if closes[i] >= opens[i] else down_color for i in range(len(xnums))]
                ax.bar(xnums, vols, width=candle_width, color=vol_colors, alpha=0.45, linewidth=0.0, antialiased=False)
                ax.set_ylabel("Vol", color="#c9d1d9")
                ax.grid(True, alpha=0.12, linestyle="--")
                ax.yaxis.tick_right()
                ax.yaxis.set_label_position("right")
            elif panel == "rsi" and rsi14 is not None:
                ax.plot(xnums, rsi14, linewidth=1.1, color="#a5d6ff", label="RSI 14")
                ax.axhline(70.0, linewidth=0.9, alpha=0.55, color="#c9d1d9")
                ax.axhline(30.0, linewidth=0.9, alpha=0.55, color="#c9d1d9")
                ax.set_ylim(0, 100)
                ax.set_ylabel("RSI", color="#c9d1d9")
                ax.grid(True, alpha=0.12, linestyle="--")
                ax.yaxis.tick_right()
                ax.yaxis.set_label_position("right")
            elif panel == "macd" and macd_line is not None and macd_signal is not None and macd_hist is not None:
                ax.plot(xnums, macd_line, linewidth=1.1, color="#00d1ff", label="MACD")
                ax.plot(xnums, macd_signal, linewidth=1.1, color="#ffd400", label="Signal")
                hist_colors = [up_color if (not math.isnan(v) and v >= 0) else down_color for v in macd_hist]
                ax.bar(xnums, macd_hist, width=candle_width, color=hist_colors, alpha=0.35)
                ax.set_ylabel("MACD", color="#c9d1d9")
                ax.grid(True, alpha=0.12, linestyle="--")
                ax.legend(loc="upper left", fontsize=8, framealpha=0.15)
                ax.yaxis.tick_right()
                ax.yaxis.set_label_position("right")
            elif panel == "stoch" and stoch_k is not None and stoch_d is not None:
                ax.plot(xnums, stoch_k, linewidth=1.1, color="#a371f7", label="%K")
                ax.plot(xnums, stoch_d, linewidth=1.1, color="#ffa657", label="%D")
                ax.axhline(80.0, linewidth=0.9, alpha=0.55, color="#c9d1d9")
                ax.axhline(20.0, linewidth=0.9, alpha=0.55, color="#c9d1d9")
                ax.set_ylim(0, 100)
                ax.set_ylabel("Stoch", color="#c9d1d9")
                ax.grid(True, alpha=0.12, linestyle="--")
                ax.legend(loc="upper left", fontsize=8, framealpha=0.15)
                ax.yaxis.tick_right()
                ax.yaxis.set_label_position("right")
            elif panel == "vwap" and vwap_line is not None:
                ax.plot(xnums, vwap_line, linewidth=1.2, color="#ffa657", label="VWAP")
                ax.set_ylabel("VWAP", color="#c9d1d9")
                ax.grid(True, alpha=0.12, linestyle="--")
                ax.legend(loc="upper left", fontsize=8, framealpha=0.15)
                ax.yaxis.tick_right()
                ax.yaxis.set_label_position("right")

            if idx > 0 or panel != panels[0]:
                idx += 1

        axes[-1].set_xlabel("ET", color="#c9d1d9")
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        axes[-1].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))

        # Leave room for the regime banner.
        fig.tight_layout(rect=[0, 0, 1, 0.955])

        dpi_used = None
        try:
            if dpi is not None:
                dpi_used = int(dpi)
            elif profile is not None:
                dpi_used = int(profile.dpi)
        except Exception:
            dpi_used = None
        if dpi_used is None or dpi_used <= 0:
            dpi_used = 240

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=int(dpi_used), facecolor=fig.get_facecolor(), metadata=_tnt_png_metadata())
        plt.close(fig)
        return buf.getvalue(), None, stats

    def _build_chart_text(
        *,
        selected: set[str],
        png_stats: dict[str, object] | None,
        crypto_last: tuple[float | None, int | None, str | None] | None = None,
    ) -> str:
        footer = (str(FOOTER_DISCLAIMER) if FOOTER_DISCLAIMER is not None else "").replace("\r\n", " ").replace("\n", " ").strip()
        pivots_line: str | None = None
        if not is_crypto:
            try:
                piv_info = delivery.get_latest_daily_pivots(sym)
                piv = piv_info.get("piv") if isinstance(piv_info, dict) else None
                if isinstance(piv, dict):
                    keys = ("R3", "R2", "R1", "P", "S1", "S2", "S3")
                    kv: list[str] = []
                    for k in keys:
                        v = piv.get(k)
                        if v is None:
                            continue
                        try:
                            kv.append(f"{k} {float(v):.2f}")
                        except Exception:
                            continue
                    if kv:
                        pivots_line = "Levels: " + " | ".join(kv)
            except Exception:
                pivots_line = None

        pretty_map = {
            "price": "Price",
            "volume": "Volume",
            "rsi": "RSI",
            "macd": "MACD",
            "stoch": "STOCH",
            "vwap": "VWAP",
        }
        selected_pretty = [pretty_map.get(k, k) for k in allowed_choices if k in selected]
        selected_line = ", ".join(selected_pretty) if selected_pretty else "(none)"

        parts: list[str] = []
        parts.append(f"📈 **Chart** — **{sym}**")
        parts.append("Window: **6mo**")
        parts.append(f"Interval: **{interval_label}**")
        parts.append(f"Panels: **{selected_line}**")

        # Shared macro regime line (best-effort; shown only if available).
        try:
            macro_line = None
            # This is called from both sync and async contexts; use cached state by reading the dict directly.
            # If the event loop isn't running here, we simply omit it.
            if asyncio.get_event_loop().is_running():
                # Avoid blocking: read shared state without awaiting.
                # The state is small; worst-case we skip it.
                pass
        except Exception:
            macro_line = None

        # Because this function is sync, we can't await _get_macro_regime_line().
        # Use a non-locking snapshot read; race is acceptable for display.
        try:
            macro_line = _format_macro_regime_line_from_state(dict(_MACRO_REGIME_STATE)) if _MACRO_REGIME_STATE else None
        except Exception:
            macro_line = None

        if macro_line:
            parts.append(macro_line)

        # Premium regime summary (from chart meta; no indicator values).
        try:
            if isinstance(png_stats, dict) and png_stats.get("regime"):
                reg = str(png_stats.get("regime") or "").strip().upper()
                if reg:
                    parts.append(f"Regime: **{reg}**")
        except Exception:
            pass

        if png_stats:
            # Prefer a fresh live price when market is open.
            last_val = float(png_stats["last"])
            last_suffix = ""
            try:
                if is_crypto:
                    px = None
                    asof_ms = None
                    src = None
                    if crypto_last is not None:
                        try:
                            px, asof_ms, src = crypto_last
                        except Exception:
                            px, asof_ms, src = None, None, None
                    if px is not None:
                        last_val = float(px)
                        ts_et = "n/a"
                        try:
                            if asof_ms is not None:
                                raw = int(asof_ms)
                                # Heuristic: ms vs seconds.
                                ts = float(raw) / 1000.0 if raw > 2_000_000_000_000 else float(raw)
                                dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(delivery.ET_TZ or timezone.utc)
                                ts_et = dt.strftime("%H:%M ET")
                        except Exception:
                            ts_et = "n/a"
                        src_txt = str(src or "live").replace("|", "/")
                        last_suffix = f" (crypto | {src_txt} | {ts_et})"
                else:
                    from market_data.last_price import detect_market_state
                    from delivery.on_demand_data import fetch_live_price

                    now_et = datetime.now(delivery.ET_TZ or timezone.utc)
                    state = detect_market_state(now_et)
                    if state == "OPEN":
                        live_px, live_ts, live_src = fetch_live_price(sym)
                        if live_px is not None and live_ts:
                            last_val = float(live_px)
                            try:
                                dt = datetime.fromisoformat(str(live_ts).replace("Z", "+00:00"))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                ts_et = dt.astimezone(delivery.ET_TZ or timezone.utc).strftime("%H:%M ET")
                            except Exception:
                                ts_et = "n/a"
                            src2 = str(live_src or "live").replace("|", "/")
                            last_suffix = f" (live | {src2} | {ts_et})"
            except Exception:
                pass

            parts.append(
                f"Last: **{last_val:.2f}**{last_suffix} (Lo/Hi: **{png_stats['low']:.2f} / {png_stats['high']:.2f}** )"
            )
        if pivots_line:
            parts.append(pivots_line)
        if footer:
            parts.append(f"_{footer}_")

        return _append_gprr_banner(" | ".join([p for p in parts if p and p.strip()]))

    # --- interactive dropdown UI ---
    class _ChartPanelsView(discord.ui.View):
        def __init__(self, *, initial_selected: set[str]):
            super().__init__(timeout=10 * 60)
            self.selected: set[str] = set(initial_selected)
            self.add_item(_ChartPanelsSelect(self))

        def _set_selected(self, selected_now: set[str]) -> None:
            self.selected = set(selected_now)

    class _ChartPanelsSelect(discord.ui.Select):
        def __init__(self, parent_view: _ChartPanelsView):
            self._parent_view = parent_view
            options = [
                discord.SelectOption(label="Price", value="price", default=("price" in parent_view.selected)),
                discord.SelectOption(label="Volume", value="volume", default=("volume" in parent_view.selected)),
                discord.SelectOption(label="RSI", value="rsi", default=("rsi" in parent_view.selected)),
                discord.SelectOption(label="MACD", value="macd", default=("macd" in parent_view.selected)),
                discord.SelectOption(label="STOCH", value="stoch", default=("stoch" in parent_view.selected)),
                discord.SelectOption(label="VWAP", value="vwap", default=("vwap" in parent_view.selected)),
            ]
            super().__init__(
                placeholder="Select up to 5: price/volume/RSI/MACD/STOCH/VWAP",
                min_values=1,
                max_values=5,
                options=options,
            )

        async def callback(self, select_interaction: discord.Interaction) -> None:
            # Separate cooldown for interactive dropdown updates (prevents spam edits under load).
            try:
                cooldown = _cooldown_env_sec("chart_ui")
                ok, wait_s = await _check_user_cooldown(
                    int(getattr(getattr(select_interaction, "user", None), "id", 0) or 0),
                    bucket="chart_ui",
                    cooldown_sec=cooldown,
                )
                if not ok:
                    await _send_cooldown_notice(select_interaction, family="chart", bucket="chart_ui", wait_s=wait_s)
                    return
            except Exception:
                pass

            selected_now = {v for v in (self.values or []) if v in allowed_choices}
            if not selected_now:
                await select_interaction.response.send_message("Pick at least one item.", ephemeral=True)
                return

            # Ack immediately (component interactions have a short response deadline).
            # We update the message content quickly, then do the heavier render/caching work.
            try:
                prev = str(getattr(getattr(select_interaction, "message", None), "content", "") or "")
                tmp = (prev + "\n" if prev.strip() else "") + "_Updating…_"
                await select_interaction.response.edit_message(content=tmp, view=self._parent_view)
            except Exception:
                pass

            self._parent_view._set_selected(selected_now)
            for opt in self.options:
                opt.default = opt.value in selected_now

            sel_key = ",".join(sorted(selected_now))
            cache_key = f"chart_png:v1:{sym_fetch}:{interval_label}:{window_days}:{sel_key}"
            png_bytes, png_err, png_stats = await _render_png_cached(
                cache_key=cache_key,
                ttl_sec=max(int(os.getenv("TNT_TTL_CHART_PNG_SEC", "45")), 5),
                render_sync_fn=lambda: _try_render_price_chart_png(selected_now),
                family="chart",
                interaction=select_interaction,
                meta={"cmd": "chart", "symbol": sym, "heavy": False},
            )
            if not png_bytes:
                if png_err == "busy_cached_only":
                    msg = _busy_cached_only_banner()
                else:
                    msg = f"Chart unavailable: {png_err}."
                if png_err == "polygon_key_missing":
                    msg = "Chart unavailable: missing market-data API key (set it in `.env.local`)."
                try:
                    await select_interaction.followup.send(msg, ephemeral=True)
                except Exception:
                    try:
                        await select_interaction.response.send_message(msg, ephemeral=True)
                    except Exception:
                        pass
                return

            crypto_last = None
            try:
                if is_crypto:
                    crypto_last = await _get_crypto_last_snapshot()
            except Exception:
                crypto_last = None
            text_now = _build_chart_text(selected=selected_now, png_stats=png_stats, crypto_last=crypto_last)
            # Optional: append cached Crypto Context only when Macro Regime exists.
            try:
                macro_line_sync = _format_macro_regime_line_from_state(dict(_MACRO_REGIME_STATE)) if _MACRO_REGIME_STATE else None
                if macro_line_sync:
                    cached_ctx = _crypto_context_cache_read()
                    if cached_ctx:
                        text_now = text_now + " | " + cached_ctx
                    elif _show_crypto_context_enabled():
                        # Warm cache asynchronously; do not block the UI edit.
                        asyncio.create_task(_get_crypto_context_line())
            except Exception:
                pass
            filename = f"{(sym or '').lower().replace(':', '').replace('/', '_')}_chart.png"
            file = discord.File(fp=io.BytesIO(png_bytes), filename=filename)

            # Replace attachment in-place.
            try:
                msg_obj = getattr(select_interaction, "message", None)
                if msg_obj is not None:
                    await msg_obj.edit(content=text_now, attachments=[file], view=self._parent_view)
                else:
                    await select_interaction.edit_original_response(content=text_now, attachments=[file], view=self._parent_view)
            except Exception as exc:  # noqa: BLE001
                # Fallback: send a new message if edit fails.
                try:
                    print(f"[TNT][CHART][UI][ERROR] {exc}")
                    try:
                        await select_interaction.followup.send(
                            "⚠️ Failed to update chart right now. Try again in ~30s.",
                            ephemeral=True,
                        )
                    except Exception:
                        await select_interaction.response.send_message(
                            "⚠️ Failed to update chart right now. Try again in ~30s.",
                            ephemeral=True,
                        )
                except Exception:
                    pass

    # Initial render (cached + throttled)
    initial_selected = set(default_selected)
    sel_key0 = ",".join(sorted(initial_selected))
    cache_key0 = f"chart_png:v1:{sym_fetch}:{interval_label}:{window_days}:{sel_key0}"
    png_bytes, png_err, png_stats = await _render_png_cached(
        cache_key=cache_key0,
        ttl_sec=max(int(os.getenv("TNT_TTL_CHART_PNG_SEC", "45")), 5),
        render_sync_fn=lambda: _try_render_price_chart_png(initial_selected),
        family="chart",
        interaction=interaction,
        meta={"cmd": "chart", "symbol": sym, "heavy": False},
    )
    if not png_bytes:
        if png_err == "polygon_key_missing":
            await _status("Chart unavailable: missing market-data API key (set it in `.env.local`).")
        elif png_err == "busy_cached_only":
            await _status(_busy_cached_only_banner())
        else:
            await _status(f"Chart unavailable: {png_err}.")
        return

    crypto_last0 = None
    try:
        if is_crypto:
            crypto_last0 = await _get_crypto_last_snapshot()
    except Exception:
        crypto_last0 = None
    text = _build_chart_text(selected=initial_selected, png_stats=png_stats, crypto_last=crypto_last0)
    try:
        macro_line_sync = _format_macro_regime_line_from_state(dict(_MACRO_REGIME_STATE)) if _MACRO_REGIME_STATE else None
        if macro_line_sync:
            cached_ctx = _crypto_context_cache_read()
            if cached_ctx:
                text = text + " | " + cached_ctx
            elif _show_crypto_context_enabled():
                asyncio.create_task(_get_crypto_context_line())
    except Exception:
        pass
    filename = f"{(sym or '').lower().replace(':', '').replace('/', '_')}_chart.png"
    view = _ChartPanelsView(initial_selected=initial_selected)

    try:
        await channel.send(
            content=text,
            files=[discord.File(fp=io.BytesIO(png_bytes), filename=filename)],
            view=view,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[TNT][CHART][SEND][ERROR] {exc}")
        except Exception:
            pass
        await _reply("Failed to post chart right now. Try again in ~30s.")
        return

    await _status(f"✅ Posted interactive chart for **{sym}**.")


@bot.tree.command(name="vwap_range", description="VWAP + Extended (PRE+AH) and RTH range chart (ETF proxies; 1m bars).")
@app_commands.describe(
    symbol="Underlying ticker (e.g., SPY, QQQ, IWM, TLT, SHY, USO; ES/NQ/RTY/ZN/CL also work as aliases)",
)
async def vwap_range(
    interaction: discord.Interaction,
    symbol: str,
) -> None:
    responded = False
    try:
        # Acknowledge quickly; this avoids Discord's 3s timeout.
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        # 40060: already acknowledged
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    sym = delivery._normalize_symbol_token(symbol)
    if not sym:
        await _reply("Invalid symbol. Try something like SPY or QQQ.")
        return

    # Soft per-user cooldown (chart family)
    try:
        cooldown = _cooldown_env_sec("chart")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="chart",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="vwap_range", bucket="chart", wait_s=wait_s)
            return
    except Exception:
        pass

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    def _try_render_vwap_rth_on_png() -> tuple[bytes | None, str | None, dict[str, Any] | None]:
        try:
            import matplotlib
            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except Exception:
            return None, "matplotlib_missing", None

        try:
            from delivery.on_demand_data import polygon_aggs

            # Pull enough history to cover: prev AH + current PRE/RTH.
            # Note: on weekends/holidays, a short lookback can start *after* the last trading session
            # and return empty results. Use a longer window and then select the latest session in ET.
            agg = polygon_aggs(sym, multiplier=1, timespan="minute", days=7)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "POLYGON_API_KEY" in msg:
                return None, "polygon_key_missing", None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                return None, f"http_{status}", None
            return None, "bars_unavailable", None

        results = agg.get("results") or []
        if not results:
            return None, "no_bars", None

        tz = delivery.ET_TZ or timezone.utc
        xs: list[datetime] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        vols: list[float] = []
        for row in results:
            try:
                t_ms = float(row.get("t"))
                h = float(row.get("h"))
                l = float(row.get("l"))
                c = float(row.get("c"))
                v = float(row.get("v") or 0.0)
            except Exception:
                continue
            if c <= 0:
                continue
            dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(tz)
            xs.append(dt)
            highs.append(h if h > 0 else c)
            lows.append(l if l > 0 else c)
            closes.append(c)
            vols.append(v)

        if len(xs) < 10:
            return None, "insufficient_bars", None

        # Determine current session date (latest ET date we have data for).
        dates = [dt.date() for dt in xs]
        uniq_dates: list[Any] = []
        seen = set()
        for d in dates:
            if d in seen:
                continue
            seen.add(d)
            uniq_dates.append(d)
        if not uniq_dates:
            return None, "no_session_date", None
        session_date = uniq_dates[-1]
        prev_date = uniq_dates[-2] if len(uniq_dates) >= 2 else None

        # Session windows in ET.
        # User-defined overnight/extended window: 04:00–20:00 ET outside RTH.
        # In practice for equities this is PRE (04:00–09:30) + AH (16:00–20:00).
        def _hhmm(dt: datetime) -> int:
            return dt.hour * 60 + dt.minute

        PM_OPEN = 4 * 60
        RTH_OPEN = 9 * 60 + 30
        RTH_CLOSE = 16 * 60
        AH_CLOSE = 20 * 60

        idx_session: list[int] = []
        idx_pre: list[int] = []
        idx_rth: list[int] = []
        idx_ah: list[int] = []

        for i, dt in enumerate(xs):
            if dt.date() != session_date:
                continue
            m = _hhmm(dt)
            idx_session.append(i)
            if PM_OPEN <= m < RTH_OPEN:
                idx_pre.append(i)
            elif RTH_OPEN <= m < RTH_CLOSE:
                idx_rth.append(i)
            elif RTH_CLOSE <= m < AH_CLOSE:
                idx_ah.append(i)

        # "ON" range (extended hours outside RTH): 04:00–09:30 + 16:00–20:00 (same session date).
        idx_on: list[int] = []
        for i, dt in enumerate(xs):
            if dt.date() != session_date:
                continue
            m = _hhmm(dt)
            if (PM_OPEN <= m < RTH_OPEN) or (RTH_CLOSE <= m < AH_CLOSE):
                idx_on.append(i)

        # Compute ranges.
        def _range(indices: list[int]) -> tuple[float | None, float | None]:
            if not indices:
                return None, None
            hi = max(highs[i] for i in indices)
            lo = min(lows[i] for i in indices)
            try:
                return float(lo), float(hi)
            except Exception:
                return None, None

        on_lo, on_hi = _range(idx_on)
        rth_lo, rth_hi = _range(idx_rth)

        # Session-to-date VWAP for RTH only.
        vwap = [math.nan] * len(xs)
        cum_pv = 0.0
        cum_v = 0.0
        for i in idx_rth:
            v = float(vols[i] or 0.0)
            tp = (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
            cum_pv += tp * v
            cum_v += v
            if cum_v > 0:
                vwap[i] = cum_pv / cum_v

        # Slice to current session window (start at PRE open if present, else RTH open).
        plot_idx = idx_session
        if not plot_idx:
            return None, "no_session_bars", None

        # Prefer starting at PRE open; if no PRE data, start at first RTH bar.
        if idx_pre:
            start_i = idx_pre[0]
        elif idx_rth:
            start_i = idx_rth[0]
        else:
            start_i = plot_idx[0]
        plot_idx = [i for i in plot_idx if i >= start_i]

        xs_plot = [xs[i] for i in plot_idx]
        px_plot = [closes[i] for i in plot_idx]
        vwap_plot = [vwap[i] for i in plot_idx]

        # Render.
        fig, ax = plt.subplots(figsize=(11.25, 5.2))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        price_color = "#58a6ff"
        vwap_color = "#ffa657"
        up_color = "#00ff66"
        wick_color = "#c9d1d9"

        xnums = mdates.date2num(xs_plot)
        ax.plot(xnums, px_plot, linewidth=1.15, color=price_color, label="Price")
        ax.plot(xnums, vwap_plot, linewidth=1.25, color=vwap_color, label="VWAP (RTH)")

        # Shade/lines for ON and RTH ranges.
        if on_lo is not None and on_hi is not None and on_hi > on_lo:
            ax.axhspan(on_lo, on_hi, color=wick_color, alpha=0.08)
            ax.axhline(on_hi, color=wick_color, linewidth=1.0, alpha=0.35, linestyle=(0, (4, 3)))
            ax.axhline(on_lo, color=wick_color, linewidth=1.0, alpha=0.35, linestyle=(0, (4, 3)))

        if rth_lo is not None and rth_hi is not None and rth_hi > rth_lo:
            ax.axhspan(rth_lo, rth_hi, color=up_color, alpha=0.05)
            ax.axhline(rth_hi, color=up_color, linewidth=1.0, alpha=0.35, linestyle=(0, (3, 3)))
            ax.axhline(rth_lo, color=up_color, linewidth=1.0, alpha=0.35, linestyle=(0, (3, 3)))

        ax.grid(True, alpha=0.16, linestyle="--")
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")
        ax.set_ylabel("Price", color="#c9d1d9")

        # X axis in ET.
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=7, maxticks=12, tz=tz))
        ax.set_xlabel("ET", color="#c9d1d9")

        title_bits: list[str] = [f"{sym} — VWAP + EXT/RTH Ranges", str(session_date)]
        ax.set_title(" | ".join(title_bits), color="#c9d1d9")

        # Legend + small stats.
        try:
            handles, labels = ax.get_legend_handles_labels()
            if labels:
                ax.legend(loc="upper left", fontsize=9, framealpha=0.15)
        except Exception:
            pass

        meta: dict[str, Any] = {
            "session_date": str(session_date),
            "on_lo": on_lo,
            "on_hi": on_hi,
            "rth_lo": rth_lo,
            "rth_hi": rth_hi,
            "last": float(px_plot[-1]),
        }

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, metadata=_tnt_png_metadata())
        plt.close(fig)
        return buf.getvalue(), None, meta

    # Cached + throttled render (burst-safe).
    today_et = datetime.now(delivery.ET_TZ or timezone.utc).date().isoformat()
    cache_key = f"vwap_range_png:v1:{sym}:{today_et}"
    png_bytes, png_err, meta = await _render_png_cached(
        cache_key=cache_key,
        ttl_sec=max(int(os.getenv("TNT_TTL_VWAP_RANGE_PNG_SEC", "60")), 10),
        render_sync_fn=_try_render_vwap_rth_on_png,
        family="vwap_range",
        interaction=interaction,
    )
    if not png_bytes:
        if png_err == "polygon_key_missing":
            await _reply("Chart unavailable: missing market-data API key (set it in `.env.local`).")
        elif png_err == "busy_cached_only":
            await _reply(_busy_cached_only_banner())
        else:
            await _reply(f"Chart unavailable: {png_err}.")
        return

    filename = f"{sym.lower()}_vwap_range.png"
    file = discord.File(fp=io.BytesIO(png_bytes), filename=filename)

    # Safe framing.
    content = (
        f"📊 **VWAP + EXT/RTH Range (ETF proxy)** — **{sym}** | "
        "_Uses extended-hours equity session (PRE+AH) as a synthetic-futures proxy._"
    )

    # Add shared macro regime line if present.
    try:
        macro_line = await _get_macro_regime_line()
        if macro_line:
            content = content + "\n" + macro_line
            content = await _maybe_append_crypto_context_if_macro(content, macro_line, sep="\n")
    except Exception:
        pass

    try:
        await channel.send(content=content, files=[file])
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[TNT][VWAP_RANGE][SEND][ERROR] {exc}")
        except Exception:
            pass
        await _reply("Failed to post chart right now. Try again in ~30s.")
        return

    # Compact follow-up with levels if available.
    try:
        if isinstance(meta, dict):
            on_lo = meta.get("on_lo")
            on_hi = meta.get("on_hi")
            rth_lo = meta.get("rth_lo")
            rth_hi = meta.get("rth_hi")
            bits: list[str] = []
            if on_lo is not None and on_hi is not None:
                bits.append(f"EXT(04-09:30,16-20) {float(on_lo):.2f}–{float(on_hi):.2f}")
            if rth_lo is not None and rth_hi is not None:
                bits.append(f"RTH {float(rth_lo):.2f}–{float(rth_hi):.2f}")
            if bits:
                await _reply("✅ Posted. Ranges: " + " | ".join(bits))
            else:
                await _reply("✅ Posted VWAP + ranges chart.")
        else:
            await _reply("✅ Posted VWAP + ranges chart.")
    except Exception:
        await _reply("✅ Posted VWAP + ranges chart.")
@bot.tree.command(name="rs", description="Relative strength chart (ratio) vs benchmark.")
@app_commands.describe(
    symbol="Ticker to compare (e.g., NVDA)",
    vs="Benchmark ticker (e.g., SPY, QQQ)",
)
async def rs(
    interaction: discord.Interaction,
    symbol: str,
    vs: str = "SPY",
) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    sym = delivery._normalize_symbol_token(symbol)
    bench = delivery._normalize_symbol_token(vs)
    if not sym or not bench:
        await _reply("Invalid symbol(s). Try something like NVDA vs SPY.")
        return
    if sym == bench:
        await _reply("Pick two different tickers (symbol and vs).")
        return

    # Soft per-user cooldown (chart family)
    try:
        cooldown = _cooldown_env_sec("chart")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="chart",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="rs", bucket="chart", wait_s=wait_s)
            return
    except Exception:
        pass

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    def _try_render_rs_png() -> tuple[bytes | None, str | None, dict[str, Any] | None]:
        try:
            import matplotlib
            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except Exception:
            return None, "matplotlib_missing", None

        try:
            from delivery.on_demand_data import polygon_aggs

            # Match /chart lookback: ~6 months, daily bars.
            window_days = 180
            a = polygon_aggs(sym, multiplier=1, timespan="day", days=window_days)
            b = polygon_aggs(bench, multiplier=1, timespan="day", days=window_days)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "POLYGON_API_KEY" in msg:
                return None, "polygon_key_missing", None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                return None, f"http_{status}", None
            return None, "bars_unavailable", None

        def _extract(payload: dict[str, Any]) -> dict[str, tuple[datetime, float]]:
            out: dict[str, tuple[datetime, float]] = {}
            tz = delivery.ET_TZ or timezone.utc
            for row in (payload.get("results") or []):
                try:
                    t_ms = float(row.get("t"))
                    c = float(row.get("c"))
                except Exception:
                    continue
                if c <= 0:
                    continue
                dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(tz)
                key = dt.date().isoformat()
                out[key] = (dt, c)
            return out

        a_map = _extract(a)
        b_map = _extract(b)
        common = sorted(set(a_map.keys()) & set(b_map.keys()))
        if len(common) < 10:
            return None, "insufficient_overlap", None

        xs: list[datetime] = []
        ratio: list[float] = []
        a_last = b_last = None
        for k in common:
            dt_a, c_a = a_map[k]
            dt_b, c_b = b_map[k]
            if c_b <= 0:
                continue
            xs.append(dt_a)
            ratio.append(float(c_a) / float(c_b))
            a_last, b_last = c_a, c_b

        if len(xs) < 10:
            return None, "insufficient_points", None

        fig, ax = plt.subplots(figsize=(11.25, 4.9))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        xnums = mdates.date2num(xs)
        line_color = "#a5d6ff"
        ax.plot(xnums, ratio, linewidth=1.35, color=line_color, label=f"{sym}/{bench}")
        ax.grid(True, alpha=0.16, linestyle="--")
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")
        ax.set_ylabel("Ratio", color="#c9d1d9")
        ax.set_xlabel("ET", color="#c9d1d9")

        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))

        ax.set_title(f"{sym} vs {bench} — Relative Strength (ratio)", color="#c9d1d9")
        try:
            ax.legend(loc="upper left", fontsize=9, framealpha=0.15)
        except Exception:
            pass

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, metadata=_tnt_png_metadata())
        plt.close(fig)

        meta = {
            "last_ratio": float(ratio[-1]),
            "first_ratio": float(ratio[0]),
            "a_last": float(a_last) if a_last is not None else None,
            "b_last": float(b_last) if b_last is not None else None,
        }
        return buf.getvalue(), None, meta

    today_et = datetime.now(delivery.ET_TZ or timezone.utc).date().isoformat()
    cache_key = f"rs_png:v1:{sym}:{bench}:{today_et}"
    png_bytes, png_err, meta = await _render_png_cached(
        cache_key=cache_key,
        ttl_sec=max(int(os.getenv("TNT_TTL_RS_PNG_SEC", "300")), 30),
        render_sync_fn=_try_render_rs_png,
        family="rs",
        interaction=interaction,
    )
    if not png_bytes:
        if png_err == "polygon_key_missing":
            await _reply("Chart unavailable: missing market-data API key (set it in `.env.local`).")
        elif png_err == "busy_cached_only":
            await _reply(_busy_cached_only_banner())
        else:
            await _reply(f"Chart unavailable: {png_err}.")
        return

    filename = f"{sym.lower()}_vs_{bench.lower()}_rs.png"
    file = discord.File(fp=io.BytesIO(png_bytes), filename=filename)

    content = (
        f"📈 **Relative Strength** — **{sym} vs {bench}** | "
        "_Relative strength improving / deteriorating._"
    )

    # Add shared macro regime line if present.
    try:
        macro_line = await _get_macro_regime_line()
        if macro_line:
            content = content + "\n" + macro_line
            content = await _maybe_append_crypto_context_if_macro(content, macro_line, sep="\n")
    except Exception:
        pass

    try:
        await channel.send(content=content, files=[file])
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[TNT][RS][SEND][ERROR] {exc}")
        except Exception:
            pass
        await _reply("Failed to post chart right now. Try again in ~30s.")
        return

    try:
        if isinstance(meta, dict):
            last_ratio = meta.get("last_ratio")
            first_ratio = meta.get("first_ratio")
            if isinstance(last_ratio, (int, float)) and isinstance(first_ratio, (int, float)) and first_ratio != 0:
                chg = (float(last_ratio) / float(first_ratio) - 1.0) * 100.0
                await _reply(f"✅ Posted. Last ratio: {float(last_ratio):.4f} (Δ {chg:+.2f}% over window).")
            else:
                await _reply("✅ Posted relative strength chart.")
        else:
            await _reply("✅ Posted relative strength chart.")
    except Exception:
        await _reply("✅ Posted relative strength chart.")


def _extract_aggs_close_series(payload: object) -> list[tuple[datetime, float]]:
    """Extract (ts_et, close) sorted ascending from a Polygon aggs payload."""

    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return []

    et_tz = delivery.ET_TZ or timezone.utc
    out: list[tuple[datetime, float]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        try:
            t_ms = float(r.get("t"))
            c = float(r.get("c"))
        except Exception:
            continue
        if not (c > 0):
            continue
        try:
            ts = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(et_tz)
        except Exception:
            continue
        out.append((ts, c))

    out.sort(key=lambda x: x[0])
    return out


def _roll_std(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / float(n)
    var = 0.0
    for v in values:
        dv = float(v) - float(mean)
        var += dv * dv
    var /= float(max(1, n - 1))
    return math.sqrt(var)


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if float(b) == 0.0:
        return None
    return (float(a) / float(b) - 1.0) * 100.0


def _value_at_or_before(series: list[tuple[datetime, float]], target: datetime) -> float | None:
    if not series:
        return None
    best: float | None = None
    for ts, val in series:
        if ts <= target:
            best = float(val)
        else:
            break
    return best


def _is_admin_interaction(interaction: discord.Interaction) -> bool:
    try:
        user = interaction.user
        perms = getattr(user, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))
    except Exception:
        return False


def _build_help_text(*, is_admin: bool) -> str:
    lines: list[str] = []
    lines.append("TNT Commands")
    lines.append("")
    lines.append("Crypto")
    lines.append("/crypto_watchlist — BTC/ETH + alts snapshot (1D/7D/30D). Fast risk appetite check.")
    lines.append("Overlay only — does not flip Macro Regime action.")
    lines.append("")
    lines.append("/crypto_rs")
    lines.append("/crypto_vol")
    lines.append("/crypto_divergence")
    lines.append("/crypto_weekend")

    if is_admin:
        lines.append("")
        lines.append("Ops (admins)")
        lines.append('TNT_CRYPTO_WATCHLIST="BTC,ETH,XRP,DOGE,LTC"')
        lines.append("TNT_TTL_CRYPTO_WATCHLIST_SEC=300")

    return "\n".join(lines)


@bot.tree.command(name="help", description="Show command quick-start (ephemeral).")
async def help_cmd(interaction: discord.Interaction):
    msg = _build_help_text(is_admin=_is_admin_interaction(interaction))
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(
    name="crypto_watchlist",
    description="Crypto watchlist board (text-only, overlay only).",
)
async def crypto_watchlist(interaction: discord.Interaction):
    try:
        if _GPRR is not None:
            _GPRR.note_command("crypto_watchlist")
    except Exception:
        pass

    cached = _ttl_get_crypto_wl()
    if cached:
        await interaction.response.send_message(cached, ephemeral=True)
        return

    editor = await _instant_ack_editor(interaction, initial="Working…", ephemeral=True)

    syms = list(CRYPTO_WATCHLIST or _CRYPTO_WL_DEFAULT)
    syms = [s.strip().upper() for s in syms if s and s.strip()]
    if not syms:
        msg = "Crypto Watchlist\n\n(no symbols configured)\n" + _CRYPTO_POLICY_LINE
        if editor:
            await editor.edit(content=msg)
        else:
            await interaction.followup.send(msg, ephemeral=True)
        return

    sem = asyncio.Semaphore(3)

    async def one(sym: str):
        async with sem:
            asof_ms, closes = await _fetch_crypto_daily_closes(sym, days=31)
            ch1 = _pct_change(closes, 1)
            ch7 = _pct_change(closes, 7)
            ch30 = _pct_change(closes, 30)
            return sym, asof_ms, ch1, ch7, ch30

    rows = await asyncio.gather(*[one(s) for s in syms])

    asof_ms_any = None
    for _, asof_ms, _, _, _ in rows:
        if asof_ms is not None:
            asof_ms_any = asof_ms if asof_ms_any is None else max(int(asof_ms_any), int(asof_ms))

    hdr = "Crypto Watchlist"
    lines = [
        hdr,
        "",
        f"As of: {_ms_to_et_datestr(asof_ms_any) if asof_ms_any else 'n/a'} (cache {TNT_TTL_CRYPTO_WATCHLIST_SEC}s)",
        "",
        "Symbol | 1D | 7D | 30D",
        "---|---:|---:|---:",
    ]
    for sym, _, ch1, ch7, ch30 in rows:
        p1 = _fmt_pct(ch1) if ch1 is not None else "n/a"
        p7 = _fmt_pct(ch7) if ch7 is not None else "n/a"
        p30 = _fmt_pct(ch30) if ch30 is not None else "n/a"
        lines.append(f"{sym} | {p1} | {p7} | {p30}")

    lines += ["", _CRYPTO_POLICY_LINE, _CRYPTO_WEEKEND_USE_LINE]
    msg = "\n".join(lines)
    _ttl_set_crypto_wl(msg)

    if editor:
        await editor.edit(content=msg)
    else:
        await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="crypto_rs", description="BTC/ETH relative strength vs SPY (crypto aggs).")
async def crypto_rs(interaction: discord.Interaction) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    # Soft per-user cooldown (chart bucket)
    try:
        cooldown = _cooldown_env_sec("chart")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="chart",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="crypto", bucket="chart", wait_s=wait_s)
            return
    except Exception:
        pass

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    sym_spy = "SPY"
    sym_btc = (os.getenv("TNT_CRYPTO_BTC_TICKER", "X:BTCUSD") or "X:BTCUSD").strip().upper()
    sym_eth = (os.getenv("TNT_CRYPTO_ETH_TICKER", "X:ETHUSD") or "X:ETHUSD").strip().upper()

    def _try_render() -> tuple[bytes | None, str | None, dict[str, object] | None]:
        try:
            import matplotlib

            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
        except Exception:
            return None, "matplotlib_missing", None

        try:
            from delivery.on_demand_data import polygon_aggs

            days = int(os.getenv("TNT_CRYPTO_RS_DAYS", "90"))
            days = min(max(days, 30), 365)
            spy_payload = polygon_aggs(sym_spy, multiplier=1, timespan="day", days=days + 7)
            btc_payload = polygon_aggs(sym_btc, multiplier=1, timespan="day", days=days + 7)
            eth_payload = polygon_aggs(sym_eth, multiplier=1, timespan="day", days=days + 7)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "POLYGON_API_KEY" in msg:
                return None, "polygon_key_missing", None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                return None, f"http_{status}", None
            return None, "bars_unavailable", None

        spy = _extract_aggs_close_series(spy_payload)
        btc = _extract_aggs_close_series(btc_payload)
        eth = _extract_aggs_close_series(eth_payload)
        if len(spy) < 10 or len(btc) < 10 or len(eth) < 10:
            return None, "insufficient_data", None

        spy_by_d = {ts.date(): c for ts, c in spy}
        btc_by_d = {ts.date(): c for ts, c in btc}
        eth_by_d = {ts.date(): c for ts, c in eth}
        dates = sorted(set(spy_by_d).intersection(btc_by_d).intersection(eth_by_d))
        if len(dates) < 10:
            return None, "insufficient_overlap", None

        days = min(int(os.getenv("TNT_CRYPTO_RS_DAYS", "90")), len(dates))
        dates = dates[-days:]
        et_tz = delivery.ET_TZ or timezone.utc
        xs = [datetime(d.year, d.month, d.day, tzinfo=et_tz) for d in dates]
        btc_rs = [float(btc_by_d[d]) / float(spy_by_d[d]) for d in dates]
        eth_rs = [float(eth_by_d[d]) / float(spy_by_d[d]) for d in dates]

        b0 = btc_rs[0] if btc_rs and btc_rs[0] else None
        e0 = eth_rs[0] if eth_rs and eth_rs[0] else None
        btc_rs_n = [v / float(b0) for v in btc_rs] if b0 else btc_rs
        eth_rs_n = [v / float(e0) for v in eth_rs] if e0 else eth_rs

        fig, ax = plt.subplots(figsize=(11.25, 4.9))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        xnums = mdates.date2num(xs)
        ax.plot(xnums, btc_rs_n, linewidth=1.35, label=f"{sym_btc}/{sym_spy}")
        ax.plot(xnums, eth_rs_n, linewidth=1.35, label=f"{sym_eth}/{sym_spy}")
        ax.grid(True, alpha=0.16, linestyle="--")
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")
        ax.set_ylabel("RS (start=1.0)", color="#c9d1d9")
        ax.set_xlabel("ET", color="#c9d1d9")
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))
        ax.set_title("Crypto Relative Strength vs SPY (normalized)", color="#c9d1d9")
        try:
            ax.legend(loc="upper left", fontsize=9, framealpha=0.15)
        except Exception:
            pass

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, metadata=_tnt_png_metadata())
        plt.close(fig)

        def _chg20(series: list[float]) -> float | None:
            if len(series) < 21:
                return None
            return _pct(series[-1], series[-21])

        try:
            asof_ts = float(xs[-1].astimezone(timezone.utc).timestamp())
        except Exception:
            asof_ts = 0.0
        meta = {
            "btc_rs_20d_pct": _chg20(btc_rs_n),
            "eth_rs_20d_pct": _chg20(eth_rs_n),
            "asof_ts": asof_ts,
        }
        return buf.getvalue(), None, meta

    cache_key = f"crypto_rs_png:v1:{sym_btc}:{sym_eth}:{sym_spy}"
    png_bytes, png_err, meta = await _render_png_cached(
        cache_key=cache_key,
        ttl_sec=max(int(os.getenv("TNT_TTL_CRYPTO_RS_PNG_SEC", "300")), 30),
        render_sync_fn=_try_render,
        family="crypto",
        interaction=interaction,
    )
    if not png_bytes:
        if png_err == "polygon_key_missing":
            await _reply("Crypto RS unavailable: missing market-data API key (set it in `.env.local`).")
        elif png_err == "busy_cached_only":
            await _reply(_busy_cached_only_banner())
        else:
            await _reply(f"Crypto RS unavailable: {png_err}.")
        return

    file = discord.File(fp=io.BytesIO(png_bytes), filename="crypto_rs.png")
    asof_ts = 0.0
    age_d: int | None = None
    b20: float | None = None
    e20: float | None = None
    try:
        if isinstance(meta, dict):
            asof_ts = float(meta.get("asof_ts") or 0.0)
            b20_v = meta.get("btc_rs_20d_pct")
            e20_v = meta.get("eth_rs_20d_pct")
            b20 = float(b20_v) if isinstance(b20_v, (int, float)) else None
            e20 = float(e20_v) if isinstance(e20_v, (int, float)) else None
    except Exception:
        asof_ts = 0.0
        b20 = None
        e20 = None

    asof_label = _format_et_mon_dd(asof_ts) if asof_ts > 0 else None
    if asof_ts > 0:
        age_d = _age_days_et(asof_ts)

    chgs = [float(x) for x in (b20, e20) if isinstance(x, (int, float))]
    rs_chg = (sum(chgs) / float(len(chgs))) if chgs else None
    if rs_chg is None:
        emoji = "🟠"
        status = "Status: RS unavailable"
    elif rs_chg <= -1.0:
        emoji = "🟠"
        status = "Status: Crypto lagging equities (20d RS ↓)"
    elif rs_chg >= 1.0:
        emoji = "🟢"
        status = "Status: Crypto leading equities (20d RS ↑)"
    else:
        emoji = "🟠"
        status = "Status: Crypto in-line with equities (20d RS ~flat)"

    title = f"{emoji} **Crypto Relative Strength vs SPY (BTC, ETH)**"
    asof_line = ""
    if asof_label:
        asof_line = f"As-of {asof_label} ET" + (f" · stale {age_d}d" if isinstance(age_d, int) and age_d >= 2 else "")

    content_parts: list[str] = [title]
    if asof_line:
        content_parts.append(asof_line)
    content_parts.append(status)
    content_parts.append(str(_CRYPTO_RS_USE_LINE))
    content_parts.append(str(_CRYPTO_POLICY_LINE))
    content = "\n".join([p for p in content_parts if p and str(p).strip()])
    try:
        macro_line = await _get_macro_regime_line()
        if macro_line:
            content = content + "\n" + macro_line
            content = await _maybe_append_crypto_context_if_macro(content, macro_line, sep="\n")
    except Exception:
        pass

    try:
        await channel.send(content=content, files=[file])
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[TNT][CRYPTO_RS][SEND][ERROR] {exc}")
        except Exception:
            pass
        await _reply("Failed to post chart right now. Try again in ~30s.")
        return

    try:
        if isinstance(meta, dict):
            b20 = meta.get("btc_rs_20d_pct")
            e20 = meta.get("eth_rs_20d_pct")
            bits: list[str] = []
            if isinstance(b20, (int, float)):
                bits.append(f"BTC RS 20d: {float(b20):+.1f}%")
            if isinstance(e20, (int, float)):
                bits.append(f"ETH RS 20d: {float(e20):+.1f}%")
            if bits:
                await _reply("✅ Posted. " + " | ".join(bits))
            else:
                await _reply("✅ Posted crypto RS chart.")
        else:
            await _reply("✅ Posted crypto RS chart.")
    except Exception:
        await _reply("✅ Posted crypto RS chart.")


@bot.tree.command(name="crypto_weekend", description="Crypto weekend move (Fri 16:00 ET -> Sun 23:59 ET).")
async def crypto_weekend(interaction: discord.Interaction) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    # Soft per-user cooldown (macro bucket)
    try:
        cooldown = _cooldown_env_sec("macro")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="macro",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="crypto", bucket="macro", wait_s=wait_s)
            return
    except Exception:
        pass

    sym_btc = (os.getenv("TNT_CRYPTO_BTC_TICKER", "X:BTCUSD") or "X:BTCUSD").strip().upper()
    sym_eth = (os.getenv("TNT_CRYPTO_ETH_TICKER", "X:ETHUSD") or "X:ETHUSD").strip().upper()
    et_tz = delivery.ET_TZ or timezone.utc
    now_et = datetime.now(et_tz)
    today = now_et.date()
    days_since_sun = (today.weekday() - 6) % 7
    sunday = today - timedelta(days=days_since_sun)
    friday = sunday - timedelta(days=2)

    fri_close = datetime(friday.year, friday.month, friday.day, 16, 0, tzinfo=et_tz)
    sun_close = datetime(sunday.year, sunday.month, sunday.day, 23, 59, tzinfo=et_tz)
    if now_et < sun_close:
        sunday = sunday - timedelta(days=7)
        friday = sunday - timedelta(days=2)
        fri_close = datetime(friday.year, friday.month, friday.day, 16, 0, tzinfo=et_tz)
        sun_close = datetime(sunday.year, sunday.month, sunday.day, 23, 59, tzinfo=et_tz)

    try:
        from delivery.on_demand_data import polygon_aggs

        btc = _extract_aggs_close_series(polygon_aggs(sym_btc, multiplier=1, timespan="hour", days=10, cache_ttl_sec=60))
        eth = _extract_aggs_close_series(polygon_aggs(sym_eth, multiplier=1, timespan="hour", days=10, cache_ttl_sec=60))
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "POLYGON_API_KEY" in msg:
            await _reply("Crypto weekend unavailable: missing market-data API key (set it in `.env.local`).")
            return
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            await _reply(f"Crypto weekend unavailable: http_{status}.")
            return
        await _reply("Crypto weekend unavailable: bars_unavailable.")
        return

    btc_f = _value_at_or_before(btc, fri_close)
    btc_s = _value_at_or_before(btc, sun_close)
    eth_f = _value_at_or_before(eth, fri_close)
    eth_s = _value_at_or_before(eth, sun_close)
    bch = _pct(btc_s, btc_f)
    ech = _pct(eth_s, eth_f)

    end_ts = 0.0
    try:
        end_ts = float(sun_close.astimezone(timezone.utc).timestamp())
    except Exception:
        end_ts = 0.0

    asof_label = _format_et_mon_dd(end_ts, include_dow=True) if end_ts > 0 else None
    age_d = _age_days_et(end_ts) if end_ts > 0 else None
    asof_line = ""
    if asof_label:
        asof_line = f"As-of {asof_label} ET" + (f" · stale {age_d}d" if isinstance(age_d, int) and age_d >= 2 else "")

    status = "Status: Unavailable"
    if isinstance(bch, (int, float)) and isinstance(ech, (int, float)):
        b = float(bch)
        e = float(ech)
        if b > 0.5 and e > 0.5:
            status = "Status: Mild risk-on tone"
        elif b < -0.5 and e < -0.5:
            status = "Status: Mild risk-off tone"
        elif (b >= 0) != (e >= 0):
            status = "Status: Mixed tone"
        else:
            status = "Status: Flat/neutral"

    title = "🟠 **Crypto Weekend Move (Fri → Sun)**"
    lines: list[str] = [title]
    if asof_line:
        lines.append(asof_line)
    lines.append(status)
    if bch is None or btc_f is None or btc_s is None:
        lines.append(f"• {sym_btc}: unavailable")
    else:
        lines.append(f"• {sym_btc}: {float(bch):+.2f}%")
    if ech is None or eth_f is None or eth_s is None:
        lines.append(f"• {sym_eth}: unavailable")
    else:
        lines.append(f"• {sym_eth}: {float(ech):+.2f}%")
    lines.append(str(_CRYPTO_WEEKEND_USE_LINE))
    lines.append(str(_CRYPTO_POLICY_LINE))

    await _reply("\n".join(lines))


@bot.tree.command(name="crypto_vol", description="Crypto volatility proxy (14d realized vol trend; daily).")
async def crypto_vol(interaction: discord.Interaction) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    # Soft per-user cooldown (chart bucket)
    try:
        cooldown = _cooldown_env_sec("chart")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="chart",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="crypto", bucket="chart", wait_s=wait_s)
            return
    except Exception:
        pass

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    sym_btc = (os.getenv("TNT_CRYPTO_BTC_TICKER", "X:BTCUSD") or "X:BTCUSD").strip().upper()
    sym_eth = (os.getenv("TNT_CRYPTO_ETH_TICKER", "X:ETHUSD") or "X:ETHUSD").strip().upper()

    def _try_render() -> tuple[bytes | None, str | None, dict[str, object] | None]:
        try:
            import matplotlib

            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
        except Exception:
            return None, "matplotlib_missing", None

        try:
            from delivery.on_demand_data import polygon_aggs

            days = int(os.getenv("TNT_CRYPTO_VOL_DAYS", "120"))
            days = min(max(days, 60), 365)
            window = int(os.getenv("TNT_CRYPTO_VOL_WINDOW", "14"))
            window = min(max(window, 7), 60)
            btc_payload = polygon_aggs(sym_btc, multiplier=1, timespan="day", days=days + 7)
            eth_payload = polygon_aggs(sym_eth, multiplier=1, timespan="day", days=days + 7)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "POLYGON_API_KEY" in msg:
                return None, "polygon_key_missing", None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                return None, f"http_{status}", None
            return None, "bars_unavailable", None

        btc = _extract_aggs_close_series(btc_payload)
        eth = _extract_aggs_close_series(eth_payload)
        if len(btc) < (window + 10) or len(eth) < (window + 10):
            return None, "insufficient_data", None

        def _rv(series: list[tuple[datetime, float]]) -> tuple[list[datetime], list[float]]:
            ts = [t for t, _ in series]
            px = [float(p) for _, p in series]
            rets: list[float] = []
            for i in range(1, len(px)):
                if px[i - 1] > 0 and px[i] > 0:
                    rets.append(math.log(px[i] / px[i - 1]))
                else:
                    rets.append(0.0)
            out_x: list[datetime] = []
            out_y: list[float] = []
            for i in range(window, len(rets) + 1):
                w = rets[i - window : i]
                sd = _roll_std(w)
                if sd is None:
                    continue
                rv = float(sd) * math.sqrt(365.0) * 100.0
                out_x.append(ts[i])
                out_y.append(rv)
            return out_x, out_y

        x_b, y_b = _rv(btc)
        x_e, y_e = _rv(eth)
        if len(x_b) < 10 or len(x_e) < 10:
            return None, "insufficient_points", None

        fig, ax = plt.subplots(figsize=(11.25, 4.9))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        ax.plot(mdates.date2num(x_b), y_b, linewidth=1.35, label=f"{sym_btc} {window}d RV%")
        ax.plot(mdates.date2num(x_e), y_e, linewidth=1.35, label=f"{sym_eth} {window}d RV%")
        ax.grid(True, alpha=0.16, linestyle="--")
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")
        ax.set_ylabel("RV% (annualized)", color="#c9d1d9")
        ax.set_xlabel("ET", color="#c9d1d9")
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))
        ax.set_title(f"Crypto Realized Volatility Trend ({window}d)", color="#c9d1d9")
        try:
            ax.legend(loc="upper left", fontsize=9, framealpha=0.15)
        except Exception:
            pass

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, metadata=_tnt_png_metadata())
        plt.close(fig)

        try:
            asof_dt = x_b[-1] if x_b and x_e and x_b[-1] <= x_e[-1] else x_e[-1]
            asof_ts = float(asof_dt.astimezone(timezone.utc).timestamp())
        except Exception:
            asof_ts = 0.0

        def _prev_at(series: list[float], idx_from_end: int) -> float | None:
            try:
                if len(series) <= idx_from_end:
                    return None
                return float(series[-(idx_from_end + 1)])
            except Exception:
                return None

        # A simple, stable trend proxy: compare last RV% vs ~10 samples ago.
        b_prev = _prev_at(y_b, 10)
        e_prev = _prev_at(y_e, 10)
        meta = {
            "btc_rv_last": float(y_b[-1]),
            "eth_rv_last": float(y_e[-1]),
            "btc_rv_prev": float(b_prev) if isinstance(b_prev, (int, float)) else None,
            "eth_rv_prev": float(e_prev) if isinstance(e_prev, (int, float)) else None,
            "window": int(window),
            "asof_ts": asof_ts,
        }
        return buf.getvalue(), None, meta

    cache_key = f"crypto_vol_png:v1:{sym_btc}:{sym_eth}"
    png_bytes, png_err, meta = await _render_png_cached(
        cache_key=cache_key,
        ttl_sec=max(int(os.getenv("TNT_TTL_CRYPTO_VOL_PNG_SEC", "600")), 60),
        render_sync_fn=_try_render,
        family="crypto",
        interaction=interaction,
    )
    if not png_bytes:
        if png_err == "polygon_key_missing":
            await _reply("Crypto vol unavailable: missing market-data API key (set it in `.env.local`).")
        elif png_err == "busy_cached_only":
            await _reply(_busy_cached_only_banner())
        else:
            await _reply(f"Crypto vol unavailable: {png_err}.")
        return

    file = discord.File(fp=io.BytesIO(png_bytes), filename="crypto_vol.png")
    asof_ts = 0.0
    age_d: int | None = None
    brv_last: float | None = None
    erv_last: float | None = None
    brv_prev: float | None = None
    erv_prev: float | None = None
    try:
        if isinstance(meta, dict):
            asof_ts = float(meta.get("asof_ts") or 0.0)
            v = meta.get("btc_rv_last")
            brv_last = float(v) if isinstance(v, (int, float)) else None
            v = meta.get("eth_rv_last")
            erv_last = float(v) if isinstance(v, (int, float)) else None
            v = meta.get("btc_rv_prev")
            brv_prev = float(v) if isinstance(v, (int, float)) else None
            v = meta.get("eth_rv_prev")
            erv_prev = float(v) if isinstance(v, (int, float)) else None
    except Exception:
        asof_ts = 0.0

    asof_label = _format_et_mon_dd(asof_ts) if asof_ts > 0 else None
    if asof_ts > 0:
        age_d = _age_days_et(asof_ts)
    asof_line = ""
    if asof_label:
        asof_line = f"As-of {asof_label} ET" + (f" · stale {age_d}d" if isinstance(age_d, int) and age_d >= 2 else "")

    deltas: list[float] = []
    for last, prev in ((brv_last, brv_prev), (erv_last, erv_prev)):
        try:
            if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and float(prev) > 0:
                deltas.append((float(last) / float(prev) - 1.0) * 100.0)
        except Exception:
            continue
    dv = (sum(deltas) / float(len(deltas))) if deltas else None
    if dv is None:
        emoji = "🟠"
        status = "Status: Volatility trend unavailable"
    elif dv <= -5.0:
        emoji = "🟢"
        status = "Status: Volatility compressing (risk stable)"
    elif dv >= 5.0:
        emoji = "🟠"
        status = "Status: Volatility expanding (risk sensitivity rising)"
    else:
        emoji = "🟠"
        status = "Status: Volatility flat (risk neutral)"

    title = f"{emoji} **Crypto Volatility Trend (Realized RV%)**"
    content_parts: list[str] = [title]
    if asof_line:
        content_parts.append(asof_line)
    content_parts.append(status)
    content_parts.append(str(_CRYPTO_VOL_USE_LINE))
    content_parts.append(str(_CRYPTO_POLICY_LINE))
    content = "\n".join([p for p in content_parts if p and str(p).strip()])
    try:
        macro_line = await _get_macro_regime_line()
        if macro_line:
            content = content + "\n" + macro_line
            content = await _maybe_append_crypto_context_if_macro(content, macro_line, sep="\n")
    except Exception:
        pass

    try:
        await channel.send(content=content, files=[file])
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[TNT][CRYPTO_VOL][SEND][ERROR] {exc}")
        except Exception:
            pass
        await _reply("Failed to post chart right now. Try again in ~30s.")
        return

    try:
        if isinstance(meta, dict):
            brv = meta.get("btc_rv_last")
            erv = meta.get("eth_rv_last")
            win = meta.get("window")
            bits: list[str] = []
            if isinstance(win, int):
                bits.append(f"window={win}d")
            if isinstance(brv, (int, float)):
                bits.append(f"BTC RV%={float(brv):.1f}")
            if isinstance(erv, (int, float)):
                bits.append(f"ETH RV%={float(erv):.1f}")
            if bits:
                await _reply("✅ Posted. " + " | ".join(bits))
            else:
                await _reply("✅ Posted crypto vol chart.")
        else:
            await _reply("✅ Posted crypto vol chart.")
    except Exception:
        await _reply("✅ Posted crypto vol chart.")


@bot.tree.command(name="crypto_divergence", description="Crypto–equity divergence flag (5d returns vs SPY).")
async def crypto_divergence(interaction: discord.Interaction) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    # Soft per-user cooldown (macro bucket)
    try:
        cooldown = _cooldown_env_sec("macro")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="macro",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="crypto", bucket="macro", wait_s=wait_s)
            return
    except Exception:
        pass

    sym_spy = "SPY"
    sym_btc = (os.getenv("TNT_CRYPTO_BTC_TICKER", "X:BTCUSD") or "X:BTCUSD").strip().upper()
    sym_eth = (os.getenv("TNT_CRYPTO_ETH_TICKER", "X:ETHUSD") or "X:ETHUSD").strip().upper()
    lookback = int(os.getenv("TNT_CRYPTO_DIVERGENCE_DAYS", "5"))
    lookback = min(max(lookback, 3), 20)
    threshold = float(os.getenv("TNT_CRYPTO_DIVERGENCE_THRESHOLD", "0.05"))
    threshold = min(max(threshold, 0.01), 0.25)

    et_tz = delivery.ET_TZ or timezone.utc
    now_et = datetime.now(et_tz)
    today = now_et.date()

    try:
        from delivery.on_demand_data import polygon_aggs

        spy = _extract_aggs_close_series(polygon_aggs(sym_spy, multiplier=1, timespan="day", days=45))
        btc = _extract_aggs_close_series(polygon_aggs(sym_btc, multiplier=1, timespan="day", days=45))
        eth = _extract_aggs_close_series(polygon_aggs(sym_eth, multiplier=1, timespan="day", days=45))

        def _close_at_equity_close_et(symbol: str) -> float | None:
            """Best-effort close at 16:00 ET for `symbol` using 1-minute bars.

            Used as a provisional same-day point when daily aggregates lag.
            """

            try:
                mins = _extract_aggs_close_series(polygon_aggs(symbol, multiplier=1, timespan="minute", days=4))
            except Exception:
                return None
            if not mins:
                return None

            cutoff = datetime(today.year, today.month, today.day, 16, 0, tzinfo=et_tz)
            best_ts: datetime | None = None
            best_px: float | None = None
            for ts, px in mins:
                try:
                    ts_et = ts.astimezone(et_tz)
                    if ts_et.date() != today:
                        continue
                    if ts_et > cutoff:
                        continue
                    v = float(px)
                    if best_ts is None or ts_et > best_ts:
                        best_ts = ts_et
                        best_px = v
                except Exception:
                    continue
            return best_px
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "POLYGON_API_KEY" in msg:
            await _reply("Crypto divergence unavailable: missing market-data API key (set it in `.env.local`).")
            return
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            await _reply(f"Crypto divergence unavailable: http_{status}.")
            return
        await _reply("Crypto divergence unavailable: bars_unavailable.")
        return

    spy_by_d = {ts.date(): c for ts, c in spy}
    btc_by_d = {ts.date(): c for ts, c in btc}
    eth_by_d = {ts.date(): c for ts, c in eth}

    # If the market is closed but daily bars haven't updated yet, synthesize a same-day
    # point using the 16:00 ET close for SPY and matching 16:00 ET crypto prints.
    # This keeps the 5d window aligned to equity close (not 24/7 crypto drift).
    used_provisional_close = False
    try:
        is_weekday = int(today.weekday()) < 5
        after_close = (now_et.hour, now_et.minute) >= (16, 5)
        if is_weekday and after_close and today not in spy_by_d:
            spy_px = _close_at_equity_close_et(sym_spy)
            btc_px = _close_at_equity_close_et(sym_btc)
            eth_px = _close_at_equity_close_et(sym_eth)
            if isinstance(spy_px, (int, float)) and isinstance(btc_px, (int, float)) and isinstance(eth_px, (int, float)):
                spy_by_d[today] = float(spy_px)
                btc_by_d[today] = float(btc_px)
                eth_by_d[today] = float(eth_px)
                used_provisional_close = True
    except Exception:
        pass
    dates = sorted(set(spy_by_d).intersection(btc_by_d).intersection(eth_by_d))
    if len(dates) < (lookback + 2):
        await _reply("Crypto divergence unavailable: insufficient overlap.")
        return

    dates = dates[-(lookback + 1) :]
    d0 = dates[0]
    d1 = dates[-1]
    spy_ret = float(spy_by_d[d1]) / float(spy_by_d[d0]) - 1.0
    btc_ret = float(btc_by_d[d1]) / float(btc_by_d[d0]) - 1.0
    eth_ret = float(eth_by_d[d1]) / float(eth_by_d[d0]) - 1.0

    def _div_bits(cret: float) -> tuple[bool, bool, float]:
        diff = float(cret) - float(spy_ret)
        sign_div = (cret >= 0) != (spy_ret >= 0)
        mag_div = abs(diff) >= float(threshold)
        return bool(sign_div), bool(mag_div), float(diff)

    b_sign, b_mag, b_diff = _div_bits(btc_ret)
    e_sign, e_mag, e_diff = _div_bits(eth_ret)
    divergence = (b_sign and b_mag) or (e_sign and e_mag)

    # As-of line: show today's ET date but disclose the last available close date.
    # Use a weekday-only age so weekends don't inflate the "stale" count.
    try:
        data_date = d1
        asof_dt = datetime(data_date.year, data_date.month, data_date.day, 0, 0, tzinfo=et_tz)
        asof_ts = float(asof_dt.astimezone(timezone.utc).timestamp())

        today_dt = datetime(today.year, today.month, today.day, 0, 0, tzinfo=et_tz)
        today_ts = float(today_dt.astimezone(timezone.utc).timestamp())

        def _weekday_age_days(start_date, end_date) -> int:
            try:
                if end_date <= start_date:
                    return 0
                n = 0
                cur = start_date
                while cur < end_date:
                    cur = cur + timedelta(days=1)
                    if int(cur.weekday()) < 5:
                        n += 1
                return int(n)
            except Exception:
                return 0

        age_bd = _weekday_age_days(data_date, today)
    except Exception:
        asof_ts = 0.0
        today_ts = 0.0
        age_bd = None

    asof_label = _format_et_mon_dd(asof_ts) if asof_ts > 0 else None
    today_label = _format_et_mon_dd(today_ts) if today_ts > 0 else None
    asof_line = ""
    if today_label and asof_label:
        asof_line = f"Run {today_label} ET · data thru {asof_label}"
        if used_provisional_close and today_label == asof_label:
            asof_line += " (equity close)"
        if isinstance(age_bd, int) and age_bd >= 2:
            asof_line += f" · stale {age_bd}d"
    elif asof_label:
        asof_line = f"As-of {asof_label} ET"

    emoji = "🔴" if divergence else "🟢"
    title = f"{emoji} **Crypto–Equity Divergence ({lookback}d)**"

    status = "Status: Aligned"
    if divergence:
        if spy_ret > 0 and btc_ret < 0 and eth_ret < 0:
            status = "Status: Divergence detected (equities ↑, crypto ↓)"
        elif spy_ret < 0 and btc_ret > 0 and eth_ret > 0:
            status = "Status: Divergence detected (equities ↓, crypto ↑)"
        else:
            status = "Status: Divergence detected"
    interp = "Interpretation: Trend reliability reduced." if divergence else "Interpretation: No divergence signal."

    lines: list[str] = [title]
    if asof_line:
        lines.append(asof_line)
    lines.append(status)
    lines.append(f"• SPY {spy_ret*100:+.2f}%")
    lines.append(f"• BTC {btc_ret*100:+.2f}% ({b_diff*100:+.1f}pp)")
    lines.append(f"• ETH {eth_ret*100:+.2f}% ({e_diff*100:+.1f}pp)")
    lines.append(interp)
    lines.append(str(_CRYPTO_DIVERGENCE_USE_LINE))
    lines.append(str(_CRYPTO_POLICY_LINE))
    await _reply("\n".join(lines))


@bot.tree.command(name="risk_on_off", description="Risk-on / risk-off dashboard using ETF proxy ratios.")
async def risk_on_off(
    interaction: discord.Interaction,
) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    # Soft per-user cooldown (macro family; heavily cached)
    try:
        cooldown = _cooldown_env_sec("macro")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="macro",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="risk_on_off", bucket="macro", wait_s=wait_s)
            return
    except Exception:
        pass

    # Fixed proxy set (no new vendors):
    # ES proxy: SPY, NQ proxy: QQQ, RTY proxy: IWM, rates proxy: TLT/SHY, crude proxy: USO
    sym_spy = "SPY"
    sym_qqq = "QQQ"
    sym_iwm = "IWM"
    sym_tlt = "TLT"
    sym_shy = "SHY"
    sym_uso = "USO"

    def _try_render_risk_on_off_png() -> tuple[bytes | None, str | None, dict[str, Any] | None]:
        try:
            import matplotlib
            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except Exception:
            return None, "matplotlib_missing", None

        try:
            from delivery.on_demand_data import polygon_aggs

            window_days = 220  # ~10 months; enough context without being heavy.
            payloads = {
                sym_spy: polygon_aggs(sym_spy, multiplier=1, timespan="day", days=window_days),
                sym_qqq: polygon_aggs(sym_qqq, multiplier=1, timespan="day", days=window_days),
                sym_iwm: polygon_aggs(sym_iwm, multiplier=1, timespan="day", days=window_days),
                sym_tlt: polygon_aggs(sym_tlt, multiplier=1, timespan="day", days=window_days),
                sym_shy: polygon_aggs(sym_shy, multiplier=1, timespan="day", days=window_days),
                sym_uso: polygon_aggs(sym_uso, multiplier=1, timespan="day", days=window_days),
            }
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "POLYGON_API_KEY" in msg:
                return None, "polygon_key_missing", None
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None:
                return None, f"http_{status}", None
            return None, "bars_unavailable", None

        tz = delivery.ET_TZ or timezone.utc

        def _extract_close_map(payload: dict[str, Any]) -> dict[str, tuple[datetime, float]]:
            out: dict[str, tuple[datetime, float]] = {}
            for row in (payload.get("results") or []):
                try:
                    t_ms = float(row.get("t"))
                    c = float(row.get("c"))
                except Exception:
                    continue
                if c <= 0:
                    continue
                dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(tz)
                out[dt.date().isoformat()] = (dt, c)
            return out

        maps = {k: _extract_close_map(v) for k, v in payloads.items()}
        if any(len(m) < 30 for m in maps.values()):
            return None, "insufficient_history", {"counts": {k: len(v) for k, v in maps.items()}}

        def _ratio_series(num: str, den: str) -> tuple[list[datetime], list[float]]:
            a = maps.get(num) or {}
            b = maps.get(den) or {}
            common = sorted(set(a.keys()) & set(b.keys()))
            xs: list[datetime] = []
            ys: list[float] = []
            for k in common:
                dt_a, c_a = a[k]
                _, c_b = b[k]
                if c_b <= 0:
                    continue
                xs.append(dt_a)
                ys.append(float(c_a) / float(c_b))
            return xs, ys

        r1_x, r1_y = _ratio_series(sym_qqq, sym_spy)  # growth vs broad
        r2_x, r2_y = _ratio_series(sym_iwm, sym_spy)  # small caps vs broad
        r3_x, r3_y = _ratio_series(sym_tlt, sym_shy)  # duration vs cash
        r4_x, r4_y = _ratio_series(sym_uso, sym_spy)  # crude proxy vs broad

        if min(len(r1_y), len(r2_y), len(r3_y), len(r4_y)) < 30:
            return None, "insufficient_overlap", {
                "lens": {"QQQ/SPY": len(r1_y), "IWM/SPY": len(r2_y), "TLT/SHY": len(r3_y), "USO/SPY": len(r4_y)}
            }

        def _pct_change(values: list[float], lookback: int = 20) -> float:
            if len(values) < 2:
                return 0.0
            idx = max(0, len(values) - 1 - lookback)
            base = float(values[idx])
            last = float(values[-1])
            if base == 0:
                return 0.0
            return (last / base - 1.0) * 100.0

        def _pct_change_at(values: list[float], i: int, lookback: int = 20) -> float:
            # Percent change ending at index i.
            if not values or i <= 0:
                return 0.0
            j = max(0, i - lookback)
            base = float(values[j])
            last = float(values[i])
            if base == 0:
                return 0.0
            return (last / base - 1.0) * 100.0

        lookback = 20
        chg_qqq_spy = _pct_change(r1_y, lookback)
        chg_iwm_spy = _pct_change(r2_y, lookback)
        chg_tlt_shy = _pct_change(r3_y, lookback)
        chg_uso_spy = _pct_change(r4_y, lookback)

        def _sgn(x: float) -> int:
            if x > 0:
                return 1
            if x < 0:
                return -1
            return 0

        # Component directions (interpretation):
        # - QQQ/SPY up => growth leadership (risk-on)
        # - IWM/SPY up => breadth leadership (risk-on)
        # - TLT/SHY up => duration bid (risk-off)
        # - USO/SPY up => inflation/energy impulse (often risk-on early, risk-off later)
        dir_growth = _sgn(chg_qqq_spy)
        dir_breadth = _sgn(chg_iwm_spy)
        dir_duration = _sgn(chg_tlt_shy)
        dir_inflation = _sgn(chg_uso_spy)

        # Composite score: + means risk-on, - means risk-off.
        # For TLT/SHY, rising implies risk-off (duration bid), so subtract its sign.
        comp_signals = {
            "growth": dir_growth,
            "breadth": dir_breadth,
            "duration": -dir_duration,
            "inflation": dir_inflation,
        }
        score = int(sum(int(v) for v in comp_signals.values()))

        # Regime sign from score.
        if score > 0:
            regime_sign = 1
        elif score < 0:
            regime_sign = -1
        else:
            regime_sign = 0

        if regime_sign > 0:
            regime = "RISK-ON"
        elif regime_sign < 0:
            regime = "RISK-OFF"
        else:
            regime = "MIXED"

        # Regime confidence
        # Breadth of agreement: how many components agree with regime sign (0..4).
        aligned = 0
        if regime_sign != 0:
            for v in comp_signals.values():
                if int(v) == int(regime_sign):
                    aligned += 1

        # Strength: median z-score of abs 20D changes across ratios.
        def _strength_z(values: list[float]) -> float:
            if len(values) < (lookback + 25):
                return 0.0
            hist: list[float] = []
            # Build historical abs 20D changes.
            for i in range(lookback, len(values)):
                hist.append(abs(_pct_change_at(values, i, lookback)))
            if len(hist) < 30:
                return 0.0
            cur = abs(_pct_change(values, lookback))
            mu = float(sum(hist)) / float(len(hist))
            try:
                var = sum((x - mu) ** 2 for x in hist) / float(max(1, len(hist) - 1))
                sigma = math.sqrt(var)
            except Exception:
                sigma = 0.0
            if sigma <= 1e-9:
                return 0.0
            return (cur - mu) / sigma

        zs = [
            _strength_z(r1_y),
            _strength_z(r2_y),
            _strength_z(r3_y),
            _strength_z(r4_y),
        ]
        zs_sorted = sorted(float(z) for z in zs)
        strength_z = (zs_sorted[1] + zs_sorted[2]) / 2.0 if len(zs_sorted) >= 4 else (zs_sorted[len(zs_sorted) // 2])

        # Stability: streak length (days) that the composite sign persisted.
        def _streak_days() -> int:
            # Determine common length for series-based evaluation.
            n = min(len(r1_y), len(r2_y), len(r3_y), len(r4_y))
            if n < (lookback + 5):
                return 0

            def _sign_at(series: list[float], idx: int) -> int:
                return _sgn(_pct_change_at(series, idx, lookback))

            def _regime_sign_at(idx: int) -> int:
                g = _sign_at(r1_y, idx)
                b = _sign_at(r2_y, idx)
                d = _sign_at(r3_y, idx)
                inf = _sign_at(r4_y, idx)
                s = int(g + b - d + inf)
                if s > 0:
                    return 1
                if s < 0:
                    return -1
                return 0

            current = _regime_sign_at(n - 1)
            if current == 0:
                return 0
            streak = 0
            for idx in range(n - 1, lookback - 1, -1):
                if _regime_sign_at(idx) == current:
                    streak += 1
                else:
                    break
            return streak

        stability_days = _streak_days()

        # Conflict detector: proxies fighting each other.
        # Flag if regime is mixed or agreement is <= 2 out of 4.
        conflict = bool(regime_sign == 0 or aligned <= 2)

        # Conflict taxonomy + reason
        conflict_taxonomy = ""
        conflict_reason = ""
        if conflict:
            if dir_growth > 0 and dir_duration > 0:
                conflict_taxonomy = "GROWTH_vs_DURATION"
                conflict_reason = "Growth leadership yes, but duration bid = defensive flow."
            elif dir_growth > 0 and dir_breadth < 0:
                conflict_taxonomy = "SMALLCAP_LAG"
                conflict_reason = "Growth leading, but breadth weak (small caps lag)."
            elif dir_breadth < 0 and dir_inflation > 0:
                conflict_taxonomy = "COMMODITY_SPIKE"
                conflict_reason = "Small caps lagging, but crude impulse up = inflation risk."
            elif dir_duration > 0 and dir_growth <= 0 and dir_breadth <= 0:
                conflict_taxonomy = "DEFENSIVE_BID"
                conflict_reason = "Defensive bid: duration rising while risk proxies fail to confirm."
            else:
                conflict_taxonomy = "DEFENSIVE_BID"
                conflict_reason = "Proxies disagree — tighten risk / reduce conviction."

        # Regime Action: directly maps to posture.
        # Criteria: agreement>=3, strength>=0.7σ, stability>=3d.
        agreement_i = int(aligned)
        stability_i = int(stability_days)
        strength_f = float(strength_z)
        meets = bool(agreement_i >= 3 and strength_f >= 0.7 and stability_i >= 3)
        press_allowed = bool((not conflict) and (regime_sign > 0) and meets)
        defend_allowed = bool((not conflict) and (regime_sign < 0) and meets)

        if conflict or stability_i < 3:
            action = "REDUCE"
        elif press_allowed:
            action = "PRESS"
        elif defend_allowed:
            action = "DEFEND"
        else:
            action = "REDUCE"

        fig, axes = plt.subplots(2, 2, figsize=(12.2, 6.4))
        fig.patch.set_facecolor("#0b0f14")

        _add_tnt_watermark_fig(fig)

        def _style(ax: Any) -> None:
            ax.set_facecolor("#0b0f14")
            ax.tick_params(colors="#c9d1d9")
            for spine in ax.spines.values():
                spine.set_color("#2d333b")
            ax.grid(True, alpha=0.16, linestyle="--")
            ax.yaxis.tick_right()
            ax.yaxis.set_label_position("right")

        fmt = mdates.DateFormatter("%m/%d", tz=tz)
        loc = mdates.AutoDateLocator(minticks=6, maxticks=10, tz=tz)
        line_color = "#a5d6ff"

        panels = [
            (axes[0, 0], r1_x, r1_y, f"{sym_qqq}/{sym_spy}", chg_qqq_spy),
            (axes[0, 1], r2_x, r2_y, f"{sym_iwm}/{sym_spy}", chg_iwm_spy),
            (axes[1, 0], r3_x, r3_y, f"{sym_tlt}/{sym_shy}", chg_tlt_shy),
            (axes[1, 1], r4_x, r4_y, f"{sym_uso}/{sym_spy}", chg_uso_spy),
        ]

        for ax, xs, ys, title, chg in panels:
            _style(ax)
            xnums = mdates.date2num(xs)
            ax.plot(xnums, ys, linewidth=1.35, color=line_color)
            ax.xaxis.set_major_formatter(fmt)
            ax.xaxis.set_major_locator(loc)
            ax.set_title(f"{title}  (20D Δ {chg:+.2f}%)", color="#c9d1d9", fontsize=10)
            ax.set_ylabel("Ratio", color="#c9d1d9")

        conf_line = f"{regime} ({action}): {aligned}/4 aligned | Strength: {strength_z:.1f}σ | Stability: {stability_days}d"
        if conflict:
            conf_line = f"CONFLICTING REGIME — {conf_line}"
        fig.suptitle(f"Risk-on / Risk-off (ETF proxies) — {conf_line}", color="#c9d1d9")
        fig.tight_layout(rect=[0, 0, 1, 0.95])

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, metadata=_tnt_png_metadata())
        plt.close(fig)

        meta = {
            "score": int(score),
            "regime": regime,
            "regime_sign": int(regime_sign),
            "aligned": int(aligned),
            "agreement": int(aligned),
            "strength_z": float(strength_z),
            "strength_sigma": float(strength_z),
            "stability_days": int(stability_days),
            "conflict": bool(conflict),
            "conflict_flag": bool(conflict),
            "conflict_taxonomy": str(conflict_taxonomy),
            "conflict_reason": str(conflict_reason),
            "action": str(action),
            "qqq_spy": float(r1_y[-1]),
            "iwm_spy": float(r2_y[-1]),
            "tlt_shy": float(r3_y[-1]),
            "uso_spy": float(r4_y[-1]),
            "chg_qqq_spy_20d": float(chg_qqq_spy),
            "chg_iwm_spy_20d": float(chg_iwm_spy),
            "chg_tlt_shy_20d": float(chg_tlt_shy),
            "chg_uso_spy_20d": float(chg_uso_spy),
            "dir_growth": int(dir_growth),
            "dir_breadth": int(dir_breadth),
            "dir_duration": int(dir_duration),
            "dir_inflation": int(dir_inflation),
        }
        return buf.getvalue(), None, meta

    # Cache it seriously: shared key, long-ish TTL (15–30m).
    # This makes /risk_on_off effectively instantaneous under spam.
    try:
        ttl_raw = int(os.getenv("TNT_TTL_RISK_ON_OFF_PNG_SEC", "1200"))
    except Exception:
        ttl_raw = 1200
    ttl_sec = min(max(ttl_raw, 900), 1800)

    cache_key = "risk_on_off:v1"
    png_bytes, png_err, meta = await _render_png_cached(
        cache_key=cache_key,
        ttl_sec=ttl_sec,
        render_sync_fn=_try_render_risk_on_off_png,
        family="risk_on_off",
        interaction=interaction,
    )
    if not png_bytes:
        if png_err == "polygon_key_missing":
            await _reply("Chart unavailable: missing market-data API key (set it in `.env.local`).")
        elif png_err == "busy_cached_only":
            await _reply(_busy_cached_only_banner())
        else:
            await _reply(f"Chart unavailable: {png_err}.")
        return

    score = None
    regime = None
    regime_sign = None
    aligned = None
    strength_z = None
    stability_days = None
    conflict = None
    conflict_taxonomy = None
    conflict_reason = None
    action = None
    try:
        if isinstance(meta, dict):
            score = int(meta.get("score")) if meta.get("score") is not None else None
            regime = str(meta.get("regime")) if meta.get("regime") else None
            regime_sign = int(meta.get("regime_sign")) if meta.get("regime_sign") is not None else None
            aligned = int(meta.get("aligned")) if meta.get("aligned") is not None else None
            strength_z = float(meta.get("strength_z")) if meta.get("strength_z") is not None else None
            stability_days = int(meta.get("stability_days")) if meta.get("stability_days") is not None else None
            conflict = bool(meta.get("conflict")) if meta.get("conflict") is not None else None
            conflict_taxonomy = str(meta.get("conflict_taxonomy")) if meta.get("conflict_taxonomy") else None
            conflict_reason = str(meta.get("conflict_reason")) if meta.get("conflict_reason") else None
            action = str(meta.get("action")) if meta.get("action") else None
    except Exception:
        score = None
        regime = None
        regime_sign = None
        aligned = None
        strength_z = None
        stability_days = None
        conflict = None
        conflict_taxonomy = None
        conflict_reason = None
        action = None

    # Update shared macro regime state for other commands.
    try:
        if regime and action and aligned is not None and strength_z is not None and stability_days is not None:
            label = "Risk-On" if regime == "RISK-ON" else ("Risk-Off" if regime == "RISK-OFF" else "Mixed")
            await _set_macro_regime_state(
                {
                    "regime_sign": int(regime_sign) if regime_sign is not None else None,
                    "regime_label": label,
                    "agreement": int(aligned),
                    "strength_sigma": float(strength_z),
                    "stability_days": int(stability_days),
                    "conflict_flag": bool(conflict) if conflict is not None else False,
                    "action": str(action),
                }
            )
    except Exception:
        pass

    legend = "ES→SPY | NQ→QQQ | RTY→IWM | ZN→TLT | CL→USO"

    def _conflict_action_hint(taxonomy: str) -> str:
        t = (taxonomy or "").strip()
        mapping = {
            "GROWTH_vs_DURATION": "Expect whipsaw; favor defined-risk / avoid chasing",
            "SMALLCAP_LAG": "Favor quality/mega-cap; avoid breakout small-cap bets",
            "COMMODITY_SPIKE": "Watch inflation impulse; tighten risk",
            "DEFENSIVE_BID": "Lower trend reliability; prefer spreads",
        }
        return mapping.get(t, "Tighten risk; prefer defined-risk")

    if regime == "RISK-ON" and not conflict:
        so_what = "Risk-on improving: growth + breadth leading. Expect dips bought."
    elif regime == "RISK-OFF" and not conflict:
        so_what = "Risk-off: duration bid + breadth weak. Expect trend fragility."
    else:
        so_what = "Regime conflict: " + _conflict_action_hint(str(conflict_taxonomy or ""))

    headline = "📊 **Risk-on / Risk-off (ETF proxies)** — _Synthetic futures-style context from proxies._"
    if regime is not None and score is not None:
        extra = ""
        if aligned is not None and strength_z is not None and stability_days is not None:
            extra = f" | {aligned}/4 aligned | Strength {strength_z:.1f}σ | Stability {stability_days}d"
        action_tag = f" | ACTION: {action}" if action else ""
        if conflict:
            headline = f"📊 **Risk-on / Risk-off (ETF proxies)** — **CONFLICTING REGIME** (score {score:+d}){action_tag}{extra}"
        else:
            headline = f"📊 **Risk-on / Risk-off (ETF proxies)** — **{regime}** (score {score:+d}){action_tag}{extra}"

    # Add legend + one-line interpretation to footer.
    headline = headline + "\n" + legend + "\n" + so_what

    # Add macro regime line (same thing, normalized) so other charts can mirror it.
    try:
        macro_line = await _get_macro_regime_line()
        if macro_line:
            headline = headline + "\n" + macro_line
            headline = await _maybe_append_crypto_context_if_macro(headline, macro_line, sep="\n")
    except Exception:
        pass

    filename = "risk_on_off.png"
    try:
        await channel.send(content=headline, files=[discord.File(fp=io.BytesIO(png_bytes), filename=filename)])
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[TNT][RISK_ON_OFF][SEND][ERROR] {exc}")
        except Exception:
            pass
        await _reply("Failed to post chart right now. Try again in ~30s.")
        return

    # Compact follow-up with the ratios and 20D deltas.
    try:
        if isinstance(meta, dict):
            bits: list[str] = []
            bits.append(
                "✅ Posted. "
                f"QQQ/SPY {meta.get('qqq_spy'):.4f} (20D {meta.get('chg_qqq_spy_20d'):+.2f}%) | "
                f"IWM/SPY {meta.get('iwm_spy'):.4f} (20D {meta.get('chg_iwm_spy_20d'):+.2f}%) | "
                f"TLT/SHY {meta.get('tlt_shy'):.4f} (20D {meta.get('chg_tlt_shy_20d'):+.2f}%) | "
                f"USO/SPY {meta.get('uso_spy'):.4f} (20D {meta.get('chg_uso_spy_20d'):+.2f}%)."
            )

            aligned_v = meta.get("aligned")
            strength_v = meta.get("strength_z")
            stability_v = meta.get("stability_days")
            if aligned_v is not None and strength_v is not None and stability_v is not None:
                bits.append(f"RegimeConfidence: {int(aligned_v)}/4 aligned | Strength: {float(strength_v):.1f}σ | Stability: {int(stability_v)}d")

            action_v = meta.get("action")
            if action_v:
                bits.append(f"Regime Action: {str(action_v)}")

            if bool(meta.get("conflict")):
                tax = str(meta.get("conflict_taxonomy") or "").strip()
                reason = str(meta.get("conflict_reason") or "").strip()
                if tax and reason:
                    bits.append(f"CONFLICT ({tax}): {reason}")
                    bits.append("Action hint: " + _conflict_action_hint(tax))
                elif reason:
                    bits.append("CONFLICTING REGIME: " + reason)
                    bits.append("Action hint: " + _conflict_action_hint(tax))

            await _reply("\n".join(bits))
        else:
            await _reply("✅ Posted risk-on/off dashboard.")
    except Exception:
        await _reply("✅ Posted risk-on/off dashboard.")


@bot.tree.command(name="pcr", description="Put/Call ratio (rolling) from options volume totals.")
@app_commands.describe(
    symbol="Underlying ticker (e.g., SPY, QQQ)",
    expiration="Expiration YYYY-MM-DD (default: nearest/today per options contracts)",
)
async def pcr(
    interaction: discord.Interaction,
    symbol: str,
    expiration: str = "",
) -> None:
    ack_update = await _instant_ack_editor(interaction, initial="Working…", ephemeral=True)

    async def _reply(message: str) -> None:
        await _status(ack_update, interaction, message, ephemeral=True)

    sym = delivery._normalize_symbol_token(symbol)
    if not sym:
        await _reply("Invalid symbol. Try something like SPY or QQQ.")
        return

    # Soft per-user cooldown (options-heavy family)
    try:
        cooldown = _cooldown_effective_sec("heavy")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="heavy",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="pcr", bucket="heavy", wait_s=wait_s)
            return
    except Exception:
        pass

    exp = (expiration or "").strip()
    exp = exp[:10] if exp else ""

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    # Cache-first / busy-mode (avoid stampedes under load).
    cache_key_png = f"pcr_png:v1:{sym}:{(exp or 'auto')}"
    cache_key_png_full = _gprr_cache_key(cache_key_png)
    try:
        cached = await _png_cache_get(cache_key_png_full, family="pcr")
        if cached is not None:
            c_png, _c_err, _c_meta = cached
            if c_png:
                try:
                    await channel.send(
                        content=_append_gprr_banner(f"📊 **Put/Call Ratio (rolling)** — **{sym}** | _cached under load_"),
                        files=[discord.File(fp=io.BytesIO(c_png), filename=f"{sym.lower()}_pcr.png")],
                    )
                    await _reply("✅ Posted cached PCR chart.")
                    return
                except Exception as exc:  # noqa: BLE001
                    try:
                        print(f"[TNT][PCR][SEND][ERROR] {exc}")
                    except Exception:
                        pass
                    await _reply("Failed to post chart right now. Try again in ~30s.")
                    return
    except Exception:
        pass

    if _busy_cached_only_enabled():
        try:
            async with _RENDER_STATE_LOCK:
                cap = int(_MAX_CHART_RENDERS)
                active = int(_RENDER_ACTIVE)
                waiting = int(_RENDER_WAITING)
            if active >= cap and waiting >= _busy_queue_depth_threshold():
                stale = await _png_cache_get_stale_only(cache_key_png_full, family="pcr", stale_max_age_sec=_busy_stale_max_age_sec())
                if stale is not None:
                    s_png, _s_err, _s_meta = stale
                    if s_png:
                        try:
                            await channel.send(
                                content=_append_gprr_banner(f"📊 **Put/Call Ratio (rolling)** — **{sym}** | _stale cache under load_"),
                                files=[discord.File(fp=io.BytesIO(s_png), filename=f"{sym.lower()}_pcr.png")],
                            )
                            await _reply("✅ Posted cached PCR chart (stale under load).")
                            return
                        except Exception:
                            pass

                await _reply(_busy_cached_only_banner())
                return
        except Exception:
            pass

    def _db_path() -> str:
        return os.getenv("DB_PATH", "db/tnt.db")

    def _ensure_pcr_table(con: sqlite3.Connection) -> None:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS options_pcr (
                symbol TEXT NOT NULL,
                expiration TEXT NOT NULL,
                ts_utc TEXT NOT NULL,
                call_volume REAL,
                put_volume REAL,
                pcr REAL,
                PRIMARY KEY(symbol, expiration, ts_utc)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_options_pcr_sym_ts ON options_pcr(symbol, ts_utc)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_options_pcr_sym_exp_ts ON options_pcr(symbol, expiration, ts_utc)")
        con.commit()

    def _insert_pcr_point(*, symbol: str, expiration: str, ts_utc: str, call_vol: float, put_vol: float, pcr: float) -> None:
        con = sqlite3.connect(_db_path())
        try:
            _ensure_pcr_table(con)
            cur = con.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO options_pcr(symbol, expiration, ts_utc, call_volume, put_volume, pcr)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (symbol, expiration, ts_utc, float(call_vol), float(put_vol), float(pcr)),
            )
            con.commit()
        finally:
            con.close()

    def _load_pcr_series(*, symbol: str, expiration: str, limit: int = 300) -> list[tuple[str, float]]:
        con = sqlite3.connect(_db_path())
        try:
            _ensure_pcr_table(con)
            cur = con.cursor()
            if expiration:
                rows = cur.execute(
                    """
                    SELECT ts_utc, pcr FROM options_pcr
                    WHERE symbol=? AND expiration=?
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (symbol, expiration, int(limit)),
                ).fetchall()
            else:
                rows = cur.execute(
                    """
                    SELECT ts_utc, pcr FROM options_pcr
                    WHERE symbol=?
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (symbol, int(limit)),
                ).fetchall()
        finally:
            con.close()

        out: list[tuple[str, float]] = []
        for ts, val in reversed(rows or []):
            try:
                out.append((str(ts), float(val)))
            except Exception:
                continue
        return out

    def _rolling_sma(values: list[float], window: int) -> list[float]:
        """Simple moving average with min-periods=1.

        For the first few points (< window), we average over the available points
        so the chart doesn't look empty early on.
        """

        if window <= 1:
            return values[:]
        out = [math.nan] * len(values)
        csum = 0.0
        n = len(values)
        for i in range(n):
            v = float(values[i])
            csum += v
            if i >= window:
                csum -= float(values[i - window])
            denom = float(min(i + 1, window))
            out[i] = csum / denom
        return out

    async def _compute_and_store_point() -> tuple[float, float, float, str, str] | None:
        # Fetch a bounded chain; we disclose this in the message framing.
        df = await delivery._fetch_polygon_options_chain_df(
            sym,
            expiration_ymd=(exp or None),
            strike_window_pct=0.12,
            max_contracts=400,
            concurrency=10,
        )
        if df is None or df.empty:
            return None

        call_vol_day = 0.0
        put_vol_day = 0.0
        call_vol_prev = 0.0
        put_vol_prev = 0.0
        exp_used = exp or str(df.get("expiration").iloc[0] if "expiration" in df.columns and len(df) else "")
        for _, row in df.iterrows():
            try:
                opt_type = str(row.get("type") or "").lower()
                vol_day = row.get("volume")
                vol_prev = row.get("prev_volume")
                v_day = float(vol_day) if vol_day is not None else 0.0
                v_prev = float(vol_prev) if vol_prev is not None else 0.0
            except Exception:
                continue
            if opt_type == "call":
                if v_day > 0:
                    call_vol_day += v_day
                if v_prev > 0:
                    call_vol_prev += v_prev
            elif opt_type == "put":
                if v_day > 0:
                    put_vol_day += v_day
                if v_prev > 0:
                    put_vol_prev += v_prev

        # Prefer current-session day volume; fall back to prior-session volume for weekends/off-hours.
        source = "day"
        call_vol = call_vol_day
        put_vol = put_vol_day
        if call_vol <= 0 and put_vol <= 0:
            call_vol = call_vol_prev
            put_vol = put_vol_prev
            source = "prev"

        if call_vol <= 0 and put_vol <= 0:
            return None

        # PCR: puts / calls. Guard against divide-by-zero.
        pcr_val = float(put_vol) / float(call_vol) if call_vol > 0 else float("inf")

        ts_utc = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            _insert_pcr_point,
            symbol=sym,
            expiration=str(exp_used or "")[:10],
            ts_utc=ts_utc,
            call_vol=float(call_vol),
            put_vol=float(put_vol),
            pcr=float(pcr_val) if math.isfinite(pcr_val) else 9999.0,
        )
        return float(call_vol), float(put_vol), float(pcr_val), str(exp_used or "")[:10], source

    point = await _compute_and_store_point()
    if point is None:
        await _reply("No options volume returned (options snapshot). Try another symbol/expiration.")
        return

    call_vol, put_vol, pcr_val, exp_used, vol_source = point

    def _try_render_pcr_png(
        *,
        profile: RenderProfile | None = None,
        dpi: int | None = None,
        **_kwargs,
    ) -> tuple[bytes | None, str | None, dict[str, Any] | None]:
        try:
            import matplotlib
            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except Exception:
            return None, "matplotlib_missing", None

        series = _load_pcr_series(symbol=sym, expiration=exp_used, limit=320)
        if len(series) < 2:
            return None, "insufficient_history", {"points": len(series)}

        tz = delivery.ET_TZ or timezone.utc
        xs: list[datetime] = []
        ys: list[float] = []
        for ts_iso, val in series:
            try:
                dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                xs.append(dt.astimezone(tz))
                ys.append(float(val))
            except Exception:
                continue

        if len(xs) < 2:
            return None, "insufficient_history", {"points": len(xs)}

        # Rolling average over points (default: 10).
        roll_n = 10
        ys_roll = _rolling_sma(ys, roll_n)

        fig, ax = plt.subplots(figsize=(11.25, 4.9))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        xnums = mdates.date2num(xs)
        ratio_color = "#a5d6ff"
        roll_color = "#ffa657"
        show_markers = len(xs) <= 8
        ax.plot(
            xnums,
            ys,
            linewidth=1.25,
            color=ratio_color,
            label="Put/Call",
            marker="o" if show_markers else None,
            markersize=3.0 if show_markers else None,
            markeredgewidth=0.0 if show_markers else None,
        )
        ax.plot(xnums, ys_roll, linewidth=1.25, color=roll_color, alpha=0.95, label=f"Rolling ({roll_n})")
        ax.axhline(1.0, color="#c9d1d9", linewidth=0.9, alpha=0.28, linestyle="--")

        ax.grid(True, alpha=0.16, linestyle="--")
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")
        ax.set_ylabel("Ratio", color="#c9d1d9")
        ax.set_xlabel("ET", color="#c9d1d9")

        ax.xaxis_date()
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=7, maxticks=12, tz=tz))

        # Make early charts (e.g., 2 points) visible.
        try:
            if len(xnums) >= 2:
                span = float(max(xnums) - min(xnums))
                min_span = 1.0 / (24.0 * 60.0)  # 1 minute
                pad = (min_span * 2.0) if span < min_span else max(span * 0.05, min_span * 0.5)
                ax.set_xlim(float(min(xnums)) - pad, float(max(xnums)) + pad)
        except Exception:
            pass

        try:
            finite_ys = [float(y) for y in ys if isinstance(y, (int, float)) and math.isfinite(float(y))]
            if finite_ys:
                ymin = min(finite_ys + [1.0])
                ymax = max(finite_ys + [1.0])
                span = float(ymax - ymin)
                pad_y = max(span * 0.15, 0.05)
                lo = ymin - pad_y
                hi = ymax + pad_y
                # Keep ratio axis sane.
                if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
                    ax.set_ylim(max(0.0, lo), hi)
        except Exception:
            pass

        ax.set_title(f"{sym} — Put/Call Ratio (rolling) | exp {exp_used}", color="#c9d1d9")
        try:
            ax.legend(loc="upper left", fontsize=9, framealpha=0.15)
        except Exception:
            pass

        dpi_used = None
        try:
            if dpi is not None:
                dpi_used = int(dpi)
            elif profile is not None:
                dpi_used = int(profile.dpi)
        except Exception:
            dpi_used = None
        if dpi_used is None or dpi_used <= 0:
            dpi_used = 160

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=int(dpi_used), metadata=_tnt_png_metadata())
        plt.close(fig)

        meta = {"points": len(xs), "last": ys[-1] if ys else None}
        return buf.getvalue(), None, meta

    png_bytes, png_err, meta = await _render_png_cached(
        cache_key=cache_key_png,
        ttl_sec=max(int(os.getenv("TNT_TTL_PCR_PNG_SEC", "30")), 5),
        render_sync_fn=_try_render_pcr_png,
        family="pcr",
        interaction=interaction,
        meta={"cmd": "pcr", "symbol": sym, "heavy": True},
    )
    filename = f"{sym.lower()}_pcr.png"
    safe = "_Sentiment leaning defensive / speculative._"
    headline = _append_gprr_banner(f"📊 **Put/Call Ratio (rolling)** — **{sym}** (exp **{exp_used}**) | {safe}")

    # Add shared macro regime line if present.
    try:
        macro_line = await _get_macro_regime_line()
        if macro_line:
            headline = headline + "\n" + macro_line
            headline = await _maybe_append_crypto_context_if_macro(headline, macro_line, sep="\n")
    except Exception:
        pass

    # If we don't have enough stored points yet, still post a "single point" summary.
    if not png_bytes:
        await _reply(
            f"✅ PCR snapshot saved. Calls vol: **{int(call_vol):,}** | Puts vol: **{int(put_vol):,}** | PCR: **{pcr_val:.2f}**. "
            "(Chart appears after at least 2 samples; call /pcr again shortly.)"
        )
        return

    try:
        await channel.send(content=headline, files=[discord.File(fp=io.BytesIO(png_bytes), filename=filename)])
    except Exception as exc:  # noqa: BLE001
        try:
            print(f"[TNT][PCR][SEND][ERROR] {exc}")
        except Exception:
            pass
        await _reply("Failed to post chart right now. Try again in ~30s.")
        return

    # Note: the chain fetch is bounded near ATM; keep the follow-up compact.
    try:
        pts = int(meta.get("points")) if isinstance(meta, dict) and meta.get("points") is not None else None
    except Exception:
        pts = None
    note = "(Uses options snapshot volume; chain is ATM-bounded for speed.)"
    if vol_source == "prev":
        note = "(Uses options snapshot volume (prior session); chain is ATM-bounded for speed.)"
    if pts is not None:
        await _reply(
            f"✅ Posted. Last PCR: **{pcr_val:.2f}** | Calls: **{int(call_vol):,}** | Puts: **{int(put_vol):,}** | points: **{pts}**. {note}"
        )
    else:
        await _reply(f"✅ Posted. Last PCR: **{pcr_val:.2f}**. {note}")


@bot.tree.command(name="oi", description="Options open interest chart with IV overlay (52 supported).")
@app_commands.describe(
    symbol="Underlying ticker (e.g., SPY, SPX, QQQ)",
    by="Group by strike or expiration",
    expiration="Strike view: YYYY-MM-DD or {0dte|today|next|auto}",
    top="Strike view only: keep top-N OI buckets (0=all)",
    window="Strike window % around spot for chain fetch (default 7; set 10 for wider)",
)
@app_commands.choices(
    by=[
        app_commands.Choice(name="strike", value="strike"),
        app_commands.Choice(name="expiration", value="expiration"),
    ]
)
async def oi(
    interaction: discord.Interaction,
    symbol: str,
    by: app_commands.Choice[str] | None = None,
    expiration: str = "",
    top: int = 25,
    window: int = 7,
) -> None:
    responded = False
    try:
        # Acknowledge quickly; this avoids Discord's 3s timeout.
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.NotFound:
        # Interaction token invalid/expired (Discord won't accept any response).
        return
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        # Best-effort: ephemeral followup, then initial response, then channel message.
        # Never raise: avoid "application did not respond" due to an unhandled exception.
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except discord.NotFound:
            return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.NotFound:
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                try:
                    await interaction.followup.send(message, ephemeral=True)
                    responded = True
                    return
                except Exception:
                    return
            # Fallback to a normal channel message if possible.
            try:
                if interaction.channel is not None:
                    await interaction.channel.send(message)
            except Exception:
                pass
            return

    sym_norm = delivery._normalize_symbol_token(symbol)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like SPY, QQQ, or SPX.")
        return
    sym = sym_norm.upper().strip()

    try:
        print(f"[TNT][OI][REQ] raw_symbol={str(symbol or '').strip()} normalized_symbol={sym}")
    except Exception:
        pass

    if sym not in OI_UNIVERSE_SET:
        await _reply(f"Symbol not in TNT OI universe (52 supported). Try: {', '.join(OI_WARMED[:6])} ...")
        return

    # Demand telemetry
    try:
        if _gprr_enabled() and _GPRR is not None:
            _GPRR.note_command(cmd="oi", symbol=sym)
    except Exception:
        pass

    # Optional: promote-on-demand for extended symbols (helps cache hit rate during hype sessions).
    try:
        if sym not in OI_WARMED_SET:
            _oi_note_demand(sym)
    except Exception:
        pass

    # Soft per-user cooldown (options-heavy family)
    try:
        cooldown = _cooldown_effective_sec("heavy")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="heavy",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="oi", bucket="heavy", wait_s=wait_s)
            return
    except Exception:
        pass

    api_key, _base_url, provider = delivery._polygon_key_and_base()
    if not api_key:
        await _reply("Options chart unavailable: missing market-data API key (set it in `.env.local`).")
        return

    by_norm = (by.value if by is not None else "strike").strip().lower()
    if by_norm not in {"strike", "expiration"}:
        by_norm = "strike"

    exp_clean = (expiration or "").strip()
    exp_mode = exp_clean.lower().strip() if exp_clean else ""
    if exp_mode in {"0dte", "today", "next", "auto"}:
        # Resolve after we can query expirations.
        exp_clean = exp_mode
    elif exp_clean:
        if len(exp_clean) != 10 or exp_clean[4] != "-" or exp_clean[7] != "-":
            await _reply("Expiration must be `YYYY-MM-DD` or one of: `0dte`, `today`, `next`, `auto`.")
            return

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    # Default tighter window improves first-hit latency by reducing options fan-out.
    try:
        window_pct = int(window)
    except Exception:
        window_pct = 7
    window_pct = max(5, min(12, int(window_pct)))
    strike_window_pct = float(window_pct) / 100.0
    # Keep bounded: if spot lookup fails, the only remaining bound is this limit.
    try:
        max_contracts = int(os.getenv("TNT_OI_MAX_CONTRACTS", "120"))
    except Exception:
        max_contracts = 120
    max_contracts = max(50, min(500, int(max_contracts)))

    # Prefer absolute strike fan-out for OI: default ±$10.
    window_abs = None
    try:
        window_abs = float(os.getenv("TNT_OI_WINDOW_ABS", "10"))
    except Exception:
        window_abs = 10.0
    if not (isinstance(window_abs, (int, float)) and window_abs and window_abs > 0 and window_abs < 1_000_000):
        window_abs = None

    if window_abs is not None:
        w_abs = float(window_abs)
        w_abs_label = f"{int(w_abs)}" if abs(w_abs - float(int(w_abs))) < 1e-9 else f"{w_abs:g}"
        window_label = f"±${w_abs_label} Window"
        window_key = f"abs{int(round(w_abs * 100.0))}"
    else:
        window_label = f"±{int(round(float(strike_window_pct) * 100.0))}% Window"
        window_key = f"pct{int(round(float(strike_window_pct) * 100.0))}"

    async def _resolve_oi_expiration(*, requested: str) -> tuple[str, str]:
        """Resolve /oi strike-view expiration.

        Returns (expiration_ymd, label_for_display).
        """

        now_et = delivery._now_et()
        today = now_et.date().isoformat()
        req = (requested or "").strip().lower()

        # Explicit YYYY-MM-DD always wins.
        if req and len(req) == 10 and req[4] == "-" and req[7] == "-":
            return req, req

        # Default is auto.
        if not req:
            req = "auto"

        expiries = []
        try:
            expiries = await _oi_get_expiries_cached(sym, api_key=api_key, max_expiries=12)
        except Exception:
            expiries = []

        expiries = [str(x).strip()[:10] for x in (expiries or []) if str(x).strip()]
        expiries = sorted(set(expiries))
        if not expiries:
            # If we can't discover expirations, fall back to today.
            return today, today

        def _first_ge(x: str) -> str | None:
            for e in expiries:
                if e >= x:
                    return e
            return None

        def _first_gt(x: str) -> str | None:
            for e in expiries:
                if e > x:
                    return e
            return None

        if req in {"0dte", "today"}:
            if today in expiries:
                return today, f"{today} (0DTE)"
            nearest = _first_ge(today) or expiries[-1]
            raise RuntimeError(f"No 0DTE expirations today. Nearest is {nearest}.")

        if req == "next":
            nxt = _first_gt(today)
            if nxt:
                return nxt, nxt
            return expiries[-1], expiries[-1]

        # auto: prefer 0DTE, else nearest >= today.
        if today in expiries:
            return today, f"{today} (0DTE)"
        nearest = _first_ge(today) or expiries[-1]
        return nearest, nearest

    exp_for_fetch = ""
    exp_label_resolved = ""
    if by_norm == "strike":
        try:
            exp_for_fetch, exp_label_resolved = await _resolve_oi_expiration(requested=exp_clean)
        except Exception as exc:
            await _reply(str(exc))
            return

        try:
            print(f"[TNT][OI][EXP] requested={str(exp_clean or '')} resolved={str(exp_for_fetch)} label={str(exp_label_resolved)}")
        except Exception:
            pass

    try:
        top_n = int(top)
    except Exception:
        top_n = 25
    if top_n < 0:
        top_n = 0
    if top_n > 200:
        top_n = 200

    # Apply profile knob: cap strike fanout in busy modes.
    prof = _gprr_profile() if _gprr_enabled() else None
    try:
        if prof is not None and ProfileLevel(int(prof.level)) != ProfileLevel.NORMAL:
            cap_n = int(prof.top_strikes)
            if top_n <= 0:
                top_n = cap_n
            else:
                top_n = min(int(top_n), cap_n)
    except Exception:
        pass

    # Cache-first / busy-mode (avoid option-chain stampedes under load).
    # IMPORTANT: key uses resolved YYYY-MM-DD (not a selector token like "0dte").
    exp_for_key = exp_for_fetch or exp_clean or "auto"

    # IV overlay policy for /oi:
    # - Default: keep IV overlay ON (users expect /oi to be OI/IV).
    # - Optional: respect GPRR profile (DEGRADED/SURVIVAL may hide overlays) only if opted-in.
    try:
        respect_profile_iv = bool(_truthy_env("TNT_OI_RESPECT_PROFILE_IV_OVERLAY", default="0"))
    except Exception:
        respect_profile_iv = False
    if respect_profile_iv:
        try:
            include_iv_request = bool(getattr(prof, "include_iv_overlay", True)) if prof is not None else True
        except Exception:
            include_iv_request = True
    else:
        include_iv_request = True

    # IMPORTANT: include IV-overlay bit so degraded modes (IV hidden) never
    # serve a cached PNG rendered with IV overlay (and vice versa).
    try:
        prof_for_key = prof
        include_iv_key = bool(include_iv_request)
    except Exception:
        include_iv_key = True
    iv_key = "iv1" if include_iv_key else "iv0"
    # IMPORTANT: include a stamp-visibility bit so debug-stamped renders
    # never poison the default (no-visible-stamp) cache.
    truthy = {"1", "true", "yes", "y", "on"}
    allow_stamp = (os.getenv("TNT_RENDER_STAMP_ALLOW", "0") or "0").strip().lower()
    v_stamp = (os.getenv("TNT_RENDER_STAMP_VISIBLE", "0") or "0").strip().lower()
    stamp_key = "sv1" if (allow_stamp in truthy and v_stamp in truthy) else "sv0"

    cache_key_png = f"oi_png:v12:{sym}:{by_norm}:{exp_for_key}:{int(top_n)}:{window_key}:{int(max_contracts)}:{iv_key}:{stamp_key}"
    cache_key_png_full = _gprr_cache_key(cache_key_png)

    # Always print the cache key used for this request (helps debug stale cache vs fresh render).
    try:
        print(
            "[TNT][OI][KEY] "
            + f"symbol={sym} by={by_norm} exp={exp_for_key} top={int(top_n)} max_contracts={int(max_contracts)} "
            + f"window_key={window_key} window_abs={window_abs} iv_key={iv_key} stamp_key={stamp_key} "
            + f"key={cache_key_png_full}"
        )
    except Exception:
        pass

    async def _cache_get(_key: str):
        # Fresh cache hit.
        try:
            cached = await _png_cache_get(_key, family="oi")
        except Exception:
            cached = None
        if cached is not None:
            c_png, c_err, c_meta = cached
            if c_png and not c_err:
                try:
                    print(f"[TNT][OI][CACHE] hit key={_key}")
                except Exception:
                    pass
                cached_ts = None
                caption = None
                filename = None
                if isinstance(c_meta, dict):
                    cached_ts = c_meta.get("__tnt_cached_ts")
                    caption = c_meta.get("caption")
                    filename = c_meta.get("filename")
                if not filename:
                    filename = f"{sym.lower()}_oi_{by_norm}.png"
                if not caption:
                    exp_label_fallback = exp_for_fetch or exp_label_resolved or exp_clean or "auto"
                    caption = f"🧾 **Options OI/IV** — **{sym}** | by **{by_norm}** | exp **{exp_label_fallback}** | _cached_"
                caption = _append_oi_universe_hint(str(caption))
                return {"png_bytes": c_png, "caption": caption, "filename": filename, "cached_asof_ts": cached_ts, "meta": c_meta}

        # Legacy busy cached-only: allow stale cache serve when queue is deep.
        if _busy_cached_only_enabled():
            try:
                async with _RENDER_STATE_LOCK:
                    cap = int(_MAX_CHART_RENDERS)
                    active = int(_RENDER_ACTIVE)
                    waiting = int(_RENDER_WAITING)
                if active >= cap and waiting >= _busy_queue_depth_threshold():
                    stale = await _png_cache_get_stale_only(_key, family="oi", stale_max_age_sec=_busy_stale_max_age_sec())
                    if stale is not None:
                        s_png, s_err, s_meta = stale
                        if s_png and not s_err:
                            cached_ts = None
                            caption = None
                            filename = None
                            if isinstance(s_meta, dict):
                                cached_ts = s_meta.get("__tnt_cached_ts")
                                caption = s_meta.get("caption")
                                filename = s_meta.get("filename")
                            if not filename:
                                filename = f"{sym.lower()}_oi_{by_norm}.png"
                            if not caption:
                                exp_label_fallback = exp_for_fetch or exp_label_resolved or exp_clean or "auto"
                                caption = f"🧾 **Options OI/IV** — **{sym}** | by **{by_norm}** | exp **{exp_label_fallback}** | _stale cache under load_"
                            caption = _append_oi_universe_hint(str(caption))
                            return {"png_bytes": s_png, "caption": caption, "filename": filename, "cached_asof_ts": cached_ts, "meta": s_meta}
            except Exception:
                pass

        # Redis cache (short TTL) to collapse cross-process bursts.
        try:
            if time.time() < float(_REDIS_SF_DISABLE_UNTIL):
                return None
        except Exception:
            pass
        try:
            import base64

            from tnt_cache import cache_get as _tnt_cache_get
            from tnt_redis import rkey as _tnt_rkey

            redis_key = _tnt_rkey("oi", "png", str(_key))

            def _do_get():
                return _tnt_cache_get(redis_key)

            hit = await asyncio.wait_for(asyncio.to_thread(_do_get), timeout=0.75)
            if hit.hit and isinstance(hit.value, dict):
                d = hit.value
                b64 = d.get("png_b64")
                if b64:
                    try:
                        png_bytes = base64.b64decode(str(b64).encode("ascii"))
                    except Exception:
                        png_bytes = None
                    if isinstance(png_bytes, (bytes, bytearray)) and png_bytes:
                        c_meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
                        cached_ts = d.get("stored_at")
                        try:
                            ttl_mem = max(int(os.getenv("TNT_TTL_OI_PNG_SEC", "45")), 5)
                            await _png_cache_set(_key, (bytes(png_bytes), None, dict(c_meta) if isinstance(c_meta, dict) else {}), ttl_mem)
                        except Exception:
                            pass

                        caption = None
                        filename = None
                        if isinstance(c_meta, dict):
                            caption = c_meta.get("caption")
                            filename = c_meta.get("filename")
                        if not filename:
                            filename = str(d.get("filename") or "") or f"{sym.lower()}_oi_{by_norm}.png"
                        if not caption:
                            exp_label_fallback = exp_for_fetch or exp_label_resolved or exp_clean or "auto"
                            caption = f"🧾 **Options OI/IV** — **{sym}** | by **{by_norm}** | exp **{exp_label_fallback}** | _cached_"
                        caption = _append_oi_universe_hint(str(caption))
                        return {
                            "png_bytes": bytes(png_bytes),
                            "caption": caption,
                            "filename": filename,
                            "cached_asof_ts": float(cached_ts) if isinstance(cached_ts, (int, float)) else None,
                            "meta": c_meta,
                        }
        except Exception:
            try:
                globals()["_REDIS_SF_DISABLE_UNTIL"] = time.time() + 30.0
            except Exception:
                pass
            pass

        return None

    # Extended symbols are cache-only in Degraded/Survival.
    try:
        is_warmed = sym in OI_WARMED_SET
        if not is_warmed and _gprr_enabled():
            prof_gate = prof if prof is not None else _gprr_profile()
            lvl = ProfileLevel(int(getattr(prof_gate, "level", 0)))
            if lvl in {ProfileLevel.DEGRADED, ProfileLevel.SURVIVAL}:
                ack = await _instant_ack_editor(interaction)
                cached = await _cache_get(cache_key_png_full)
                if cached:
                    png_bytes = cached.get("png_bytes")
                    caption = cached.get("caption")
                    filename = cached.get("filename") or f"{sym.lower()}_oi_{by_norm}.png"
                    cached_ts = cached.get("cached_asof_ts")
                    if isinstance(png_bytes, (bytes, bytearray)) and png_bytes:
                        content = _append_gprr_banner_cached(str(caption or ""), cached_asof_ts=float(cached_ts) if cached_ts else None)
                        try:
                            if interaction.channel is not None:
                                await interaction.channel.send(content=content, files=[_file_from_png_bytes(bytes(png_bytes), filename=str(filename))])
                        except Exception:
                            pass
                        await ack.edit(content=content, attachments=[_file_from_png_bytes(bytes(png_bytes), filename=str(filename))])
                        return
                await ack.edit(content=_gprr_busy_banner(prof_gate))
                return
    except Exception:
        pass

    async def _cache_set(_key: str, result: dict[str, object]) -> None:
        try:
            png_bytes = result.get("png_bytes")
            png_err = result.get("png_err")
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            meta = dict(meta)
            meta.setdefault("caption", result.get("caption"))
            meta.setdefault("filename", result.get("filename"))
            ttl = int(result.get("ttl_sec") or max(int(os.getenv("TNT_TTL_OI_PNG_SEC", "45")), 5))
            await _png_cache_set(_key, (png_bytes, png_err, meta), ttl)
        except Exception:
            pass

        # Best-effort Redis mirror (short TTL) for cross-process cache hits.
        try:
            if time.time() < float(_REDIS_SF_DISABLE_UNTIL):
                return
        except Exception:
            pass
        try:
            if png_err:
                return
            if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes:
                return

            import base64
            import time

            from tnt_cache import cache_set as _tnt_cache_set
            from tnt_redis import rkey as _tnt_rkey

            redis_key = _tnt_rkey("oi", "png", str(_key))
            payload = {
                "png_b64": base64.b64encode(bytes(png_bytes)).decode("ascii"),
                "png_err": None,
                "meta": meta if isinstance(meta, dict) else {},
                "caption": result.get("caption"),
                "filename": result.get("filename"),
                "stored_at": float(time.time()),
            }

            def _do_set() -> None:
                _tnt_cache_set(redis_key, payload, ttl_s=120)

            await asyncio.wait_for(asyncio.to_thread(_do_set), timeout=0.75)
        except Exception:
            try:
                globals()["_REDIS_SF_DISABLE_UNTIL"] = time.time() + 30.0
            except Exception:
                pass
            pass

    async def _probe_polygon_options_access(
        *,
        exp: str,
    ) -> tuple[int | None, int | None, str | None]:
        """Return (contracts_status, snapshot_status, detail).

        Some API plans allow contracts lookup but forbid options snapshot/greeks,
        which is required for OI/IV.
        """
        import aiohttp

        underlying = delivery._map_underlying_for_options(sym)
        params_contracts: dict[str, object] = {
            "underlying_ticker": underlying,
            "expiration_date": exp,
            "limit": 1,
            "apiKey": api_key,
        }

        base_url = delivery._polygon_key_and_base()[1]
        url_contracts = f"{base_url}/v3/reference/options/contracts"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url_contracts, params=params_contracts) as resp:
                    contracts_status = int(resp.status)
                    payload = None
                    try:
                        payload = await resp.json()
                    except Exception:
                        payload = None

                    if contracts_status != 200:
                        detail = None
                        if isinstance(payload, dict):
                            detail = str(payload.get("error") or payload.get("message") or payload.get("status") or "")
                        return contracts_status, None, (detail or None)

                    ticker = None
                    if isinstance(payload, dict) and isinstance(payload.get("results"), list) and payload["results"]:
                        first = payload["results"][0]
                        if isinstance(first, dict):
                            ticker = str(first.get("ticker") or "").strip()

                    if not ticker:
                        return contracts_status, None, None

                # Probe snapshot for that one ticker.
                url_snap = f"{base_url}/v3/snapshot/options/{underlying}/{ticker}"
                async with session.get(url_snap, params={"apiKey": api_key}) as resp2:
                    snap_status = int(resp2.status)
                    if snap_status == 200:
                        return contracts_status, snap_status, None
                    detail = None
                    try:
                        snap_payload = await resp2.json()
                        if isinstance(snap_payload, dict):
                            detail = str(snap_payload.get("error") or snap_payload.get("message") or snap_payload.get("status") or "")
                    except Exception:
                        detail = None
                    return contracts_status, snap_status, (detail or None)
        except Exception:
            return None, None, None

    def _format_footer() -> str:
        footer = (str(FOOTER_DISCLAIMER) if FOOTER_DISCLAIMER is not None else "").replace("\r\n", " ").replace("\n", " ").strip()
        return footer

    def _infer_iv_percent(values: list[float]) -> list[float]:
        # Provider tends to return IV as a decimal (0.25 = 25%), but be defensive.
        clean = [v for v in values if isinstance(v, (int, float)) and not math.isnan(float(v))]
        if not clean:
            return values
        clean_sorted = sorted(float(v) for v in clean)
        median = clean_sorted[len(clean_sorted) // 2]
        if median > 3.0:
            # Likely already in percent units.
            return [float(v) if (isinstance(v, (int, float)) and not math.isnan(float(v))) else math.nan for v in values]
        return [float(v) * 100.0 if (isinstance(v, (int, float)) and not math.isnan(float(v))) else math.nan for v in values]

    def _bucket_oi_iv_by_strike(df) -> tuple[list[float], list[float], list[float], list[float]]:
        import pandas as pd

        if df is None or df.empty:
            return [], [], [], []

        work = df.copy()
        work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
        work["open_interest"] = pd.to_numeric(work.get("open_interest"), errors="coerce")
        work["iv"] = pd.to_numeric(work.get("iv"), errors="coerce")
        work = work.dropna(subset=["strike"]).copy()
        if work.empty:
            return [], [], [], []

        # Bucket strikes to reduce clutter.
        # Heuristic: SPX-style index strikes get wider buckets.
        sym_up = (sym or "").strip().upper()
        bucket = 5.0 if sym_up in {"SPX", "SPXW"} else 1.0
        try:
            median_strike = float(work["strike"].median())
            if median_strike >= 1000.0:
                bucket = max(bucket, 5.0)
        except Exception:
            pass

        work["type"] = work.get("type")
        work["type"] = work["type"].astype(str).str.lower()

        # Compute positive OI weights.
        oi_raw = work["open_interest"].fillna(0.0)
        work["oi_pos"] = oi_raw.where(oi_raw > 0.0, 0.0)

        # Round strikes into buckets.
        work["strike_bucket"] = (work["strike"] / float(bucket)).round() * float(bucket)
        work["strike_bucket"] = pd.to_numeric(work["strike_bucket"], errors="coerce")
        work = work.dropna(subset=["strike_bucket"]).copy()
        if work.empty:
            return [], [], [], []

        strikes: list[float] = []
        oi_calls: list[float] = []
        oi_puts: list[float] = []
        iv_avgs: list[float] = []

        for strike_b, grp in work.groupby("strike_bucket"):
            try:
                strike_f = float(strike_b)
            except Exception:
                continue

            call_mask = grp["type"].eq("call")
            put_mask = grp["type"].eq("put")

            oi_c = float(grp.loc[call_mask, "oi_pos"].sum()) if bool(call_mask.any()) else 0.0
            oi_p = float(grp.loc[put_mask, "oi_pos"].sum()) if bool(put_mask.any()) else 0.0

            iv = grp["iv"]
            w = grp["oi_pos"]
            mask = (~iv.isna()) & (w > 0.0)
            if bool(mask.any()):
                iv_avg = float((iv[mask] * w[mask]).sum() / w[mask].sum())
            else:
                try:
                    iv_avg = float(iv.mean())
                except Exception:
                    iv_avg = math.nan

            strikes.append(strike_f)
            oi_calls.append(oi_c)
            oi_puts.append(oi_p)
            iv_avgs.append(iv_avg)

        order = sorted(range(len(strikes)), key=lambda i: strikes[i])
        strikes = [strikes[i] for i in order]
        oi_calls = [oi_calls[i] for i in order]
        oi_puts = [oi_puts[i] for i in order]
        iv_avgs = [iv_avgs[i] for i in order]
        iv_pct = _infer_iv_percent(iv_avgs)
        return strikes, oi_calls, oi_puts, iv_pct

    def _bucket_oi_iv_by_expiration(rows: list[dict[str, object]]) -> tuple[list[str], list[float], list[float]]:
        exps: list[str] = []
        oi_sums: list[float] = []
        iv_avgs: list[float] = []
        for row in rows:
            exp = str(row.get("expiration") or "").strip()
            if not exp:
                continue
            exps.append(exp)
            try:
                oi_sums.append(float(row.get("oi_sum") or 0.0))
            except Exception:
                oi_sums.append(0.0)
            try:
                iv_avgs.append(float(row.get("iv_avg") or math.nan))
            except Exception:
                iv_avgs.append(math.nan)

        order = sorted(range(len(exps)), key=lambda i: exps[i])
        exps = [exps[i] for i in order]
        oi_sums = [oi_sums[i] for i in order]
        iv_avgs = [iv_avgs[i] for i in order]
        iv_pct = _infer_iv_percent(iv_avgs)
        return exps, oi_sums, iv_pct

    def _smooth_nan(values: list[float], window: int) -> list[float]:
        if window <= 1 or not values:
            return values
        n = len(values)
        out = [math.nan] * n
        half = window // 2
        for i in range(n):
            j0 = max(0, i - half)
            j1 = min(n, i + half + 1)
            nums = [float(v) for v in values[j0:j1] if isinstance(v, (int, float)) and not math.isnan(float(v))]
            if nums:
                out[i] = sum(nums) / float(len(nums))
        return out

    def _render_oi_iv_png(
        *,
        title: str,
        x_labels: list[str],
        iv_pct: list[float],
        x_label: str,
        oi_total: list[float] | None = None,
        oi_calls: list[float] | None = None,
        oi_puts: list[float] | None = None,
        spot_idx: int | None = None,
        spot_label: str | None = None,
        smooth_iv_window: int = 1,
        profile: RenderProfile | None = None,
        dpi: int | None = None,
        include_iv_overlay: bool = True,
        **_kwargs,
    ) -> bytes | None:
        # Prefer the shared premium renderer so warm-cache images match live /oi output.
        try:
            from delivery.oi_iv_render import render_oi_iv_png as _shared_render

            return _shared_render(
                title=str(title),
                x_labels=list(x_labels),
                iv_pct=list(iv_pct) if iv_pct else [],
                oi_calls=list(oi_calls),
                oi_puts=list(oi_puts),
                dpi=150,
                include_iv_overlay=bool(include_iv_overlay),
            )
        except Exception:
            # Fall back to a minimal local renderer if shared import fails.
            pass

        try:
            import matplotlib
            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
        except Exception:
            return None

        if not x_labels:
            return None

        n = len(x_labels)
        if oi_calls is None or oi_puts is None:
            if not oi_total:
                return None
        else:
            if len(oi_calls) != n or len(oi_puts) != n:
                return None
        if iv_pct and len(iv_pct) != n:
            return None

        xs = list(range(len(x_labels)))
        fig, ax = plt.subplots(figsize=(11.25, 5.4))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        call_color = "#4C78A8"  # TNT muted blue
        put_color = "#E45756"   # TNT muted red
        line_color = "#8b949e"  # muted gray

        if oi_calls is not None and oi_puts is not None:
            width = 0.38
            ax.bar([x - width / 2.0 for x in xs], oi_calls, width=width, color=call_color, alpha=0.55, label="Calls OI")
            ax.bar([x + width / 2.0 for x in xs], oi_puts, width=width, color=put_color, alpha=0.48, label="Puts OI")
        else:
            ax.bar(xs, oi_total or [], color=call_color, alpha=0.45, label="OI")
        ax.set_ylabel("Open interest", color="#c9d1d9")
        try:
            ax.set_ylim(bottom=0.0)
        except Exception:
            pass
        ax.grid(True, alpha=0.12, linestyle="--")
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")

        ax2 = None
        if bool(include_iv_overlay):
            ax2 = ax.twinx()
            ax2.set_facecolor("#0b0f14")
            ax2.tick_params(colors="#c9d1d9")
            for spine in ax2.spines.values():
                spine.set_color("#2d333b")
            iv_plot = list(iv_pct) if iv_pct else [math.nan] * n
            if smooth_iv_window > 1:
                iv_plot = _smooth_nan(iv_plot, smooth_iv_window)
            ax2.plot(xs, iv_plot, color=line_color, linewidth=1.25, alpha=0.60, linestyle="--", label="IV")
            ax2.set_ylabel("IV (%)", color="#c9d1d9")

        if isinstance(spot_idx, int) and 0 <= spot_idx < n:
            ax.axvline(spot_idx, color="#c9d1d9", linewidth=1.0, alpha=0.30, linestyle="--")
            if spot_label:
                try:
                    ymax = ax.get_ylim()[1]
                    ax.text(
                        spot_idx,
                        ymax,
                        spot_label,
                        color="#c9d1d9",
                        fontsize=8,
                        alpha=0.85,
                        ha="center",
                        va="bottom",
                    )
                except Exception:
                    pass

        ax.set_title(title, color="#c9d1d9")
        ax.set_xlabel(x_label, color="#c9d1d9")

        # One decisive takeaway (top-right).
        try:
            if oi_calls is not None and oi_puts is not None:
                put_i = int(max(range(n), key=lambda i: float(oi_puts[i]) if oi_puts else 0.0))
                call_i = int(max(range(n), key=lambda i: float(oi_calls[i]) if oi_calls else 0.0))
                put_max = float(oi_puts[put_i])
                call_max = float(oi_calls[call_i])
                takeaway = ""
                if put_max >= max(1.0, call_max) * 1.15:
                    takeaway = f"Put wall @ {x_labels[put_i]}"
                elif call_max >= max(1.0, put_max) * 1.15:
                    takeaway = f"Call wall @ {x_labels[call_i]}"
                else:
                    skew = None
                    try:
                        iv_clean = [float(v) for v in (iv_pct or []) if isinstance(v, (int, float)) and not math.isnan(float(v))]
                        if iv_clean and len(iv_clean) >= 6:
                            k = max(2, int(len(iv_clean) // 3))
                            iv_low = sum(iv_clean[:k]) / float(k)
                            iv_high = sum(iv_clean[-k:]) / float(k)
                            if (iv_low - iv_high) >= 2.0:
                                skew = "IV skew favors downside"
                            elif (iv_high - iv_low) >= 2.0:
                                skew = "IV skew favors upside"
                    except Exception:
                        skew = None
                    takeaway = skew or "Balanced OI"

                ax.text(
                    0.985,
                    0.92,
                    takeaway,
                    transform=ax.transAxes,
                    ha="right",
                    va="top",
                    fontsize=9,
                    color="#c9d1d9",
                    alpha=0.92,
                    bbox={"facecolor": "#0b0f14", "edgecolor": "#2d333b", "alpha": 0.75, "pad": 3.0},
                )
        except Exception:
            pass

        # Thin x tick labels so the chart stays readable.
        max_ticks = 18
        step = max(1, int(math.ceil(float(n) / float(max_ticks))))
        tick_idx = list(range(0, n, step))
        if (n - 1) not in tick_idx:
            tick_idx.append(n - 1)
        ax.set_xticks(tick_idx)
        ax.set_xticklabels([x_labels[i] for i in tick_idx], rotation=45, ha="right", color="#c9d1d9", fontsize=8)

        # Legend (bars + optional IV).
        try:
            h1, l1 = ax.get_legend_handles_labels()
            if ax2 is not None:
                h2, l2 = ax2.get_legend_handles_labels()
            else:
                h2, l2 = [], []
            if l1 or l2:
                ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, framealpha=0.15)
        except Exception:
            pass

        fig.tight_layout()
        dpi_used = None
        try:
            if dpi is not None:
                dpi_used = int(dpi)
            elif profile is not None:
                dpi_used = int(profile.dpi)
        except Exception:
            dpi_used = None
        if dpi_used is None or dpi_used <= 0:
            dpi_used = 150
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=int(dpi_used), metadata=_tnt_png_metadata())
        plt.close(fig)
        return buf.getvalue()

    async def _discover_expirations(max_expirations: int) -> list[str]:
        import aiohttp

        underlying = delivery._map_underlying_for_options(sym)
        now_et = delivery._now_et()
        start = now_et.date().isoformat()

        # Attempt to bound strikes to reduce contract count.
        underlying_px = None
        try:
            snap = delivery._get_last_price_snapshot(sym)
            if snap and snap.px is not None:
                underlying_px = float(snap.px)
        except Exception:
            underlying_px = None

        strike_min = strike_max = None
        if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
            strike_min = underlying_px * (1.0 - strike_window_pct)
            strike_max = underlying_px * (1.0 + strike_window_pct)

        params: dict[str, object] = {
            "underlying_ticker": underlying,
            "expiration_date.gte": start,
            "limit": 1000,
            "apiKey": api_key,
        }
        if strike_min is not None and strike_max is not None:
            params["strike_price.gte"] = f"{strike_min:.6f}"
            params["strike_price.lte"] = f"{strike_max:.6f}"

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"{delivery._polygon_key_and_base()[1]}/v3/reference/options/contracts"
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json()

        if not isinstance(payload, dict) or payload.get("status") != "OK":
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        expirations: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            exp = str(item.get("expiration_date") or "").strip()
            if exp:
                expirations.add(exp)
        return sorted(expirations)[: max(1, int(max_expirations))]

    if by_norm == "strike":
        async def _render_fn(*, profile: RenderProfile | None = None):
            import time

            render_mode = "local"
            t0 = time.perf_counter()

            tf0 = time.perf_counter()
            df = await delivery._fetch_polygon_options_chain_df(
                sym,
                expiration_ymd=(exp_for_fetch or None),
                strike_window_pct=strike_window_pct,
                max_contracts=max_contracts,
                concurrency=8,
            )
            try:
                rows = int(len(df)) if df is not None else 0
                print(f"[TNT][OI][FETCH] symbol={sym} fetch_s={time.perf_counter()-tf0:.2f} rows={rows} total_s={time.perf_counter()-t0:.2f}")
            except Exception:
                pass
            if df is None or getattr(df, "empty", True):
                exp_label0 = exp_for_fetch or delivery._now_et().date().isoformat()
                contracts_status, snap_status, detail = await _probe_polygon_options_access(exp=exp_label0)
                extra = f" ({detail})" if detail else ""
                if contracts_status in {401, 403}:
                    return {
                        "error": (
                            f"Options OI/IV unavailable for **{sym}**: options contracts endpoint returned HTTP **{contracts_status}**{extra}. "
                            "This usually means the API key plan doesn’t include options access."
                        ),
                        "ttl_sec": 3,
                    }
                if contracts_status == 429:
                    return {"error": f"Options OI/IV temporarily unavailable for **{sym}**: options contracts endpoint returned HTTP **429** (rate limited). Try again shortly.", "ttl_sec": 3}
                if snap_status in {401, 403}:
                    return {
                        "error": (
                            f"Options OI/IV unavailable for **{sym}**: options snapshot endpoint returned HTTP **{snap_status}**{extra}. "
                            "Contracts lookup may work, but OI/IV requires options snapshot/greeks access on your plan."
                        ),
                        "ttl_sec": 3,
                    }
                if snap_status == 429:
                    return {"error": f"Options OI/IV temporarily unavailable for **{sym}**: options snapshot endpoint returned HTTP **429** (rate limited). Try again shortly.", "ttl_sec": 3}
                return {"error": f"No options OI/IV data returned for **{sym}** ({provider}) on **{exp_label0}**.", "ttl_sec": 3}

            strikes, oi_calls, oi_puts, iv_pct = _bucket_oi_iv_by_strike(df)
            if not strikes:
                return {"error": f"No usable strikes found for **{sym}**.", "ttl_sec": 5}

            # Reduce clutter: keep only the top-N OI buckets (by total OI).
            strikes2 = list(strikes)
            oi_calls2 = list(oi_calls)
            oi_puts2 = list(oi_puts)
            iv_pct2 = list(iv_pct)
            if top_n > 0 and len(strikes2) > top_n:
                totals = [float(oi_calls2[i]) + float(oi_puts2[i]) for i in range(len(strikes2))]
                keep = sorted(range(len(strikes2)), key=lambda i: totals[i], reverse=True)[:top_n]
                keep_sorted = sorted(keep, key=lambda i: strikes2[i])
                strikes2 = [strikes2[i] for i in keep_sorted]
                oi_calls2 = [oi_calls2[i] for i in keep_sorted]
                oi_puts2 = [oi_puts2[i] for i in keep_sorted]
                iv_pct2 = [iv_pct2[i] for i in keep_sorted]

            labels = [f"{s:g}" for s in strikes2]

            # Final guardrail: enforce OI bars are finite and >= 0 before any render (worker or local).
            # This prevents baseline artifacts when negative/NaN values sneak in from upstream joins.
            def _pos(v: object) -> float:
                try:
                    x = float(v)
                except Exception:
                    return 0.0
                if not math.isfinite(x):
                    return 0.0
                return x if x > 0.0 else 0.0

            oi_calls2 = [_pos(v) for v in list(oi_calls2)]
            oi_puts2 = [_pos(v) for v in list(oi_puts2)]

            # Spot marker (best-effort) using underlying_price from snapshots.
            spot_idx = None
            spot_label = None
            spot_px = None
            try:
                import pandas as pd

                spot_series = pd.to_numeric(df.get("underlying_price"), errors="coerce")
                spot_vals = [float(v) for v in spot_series.dropna().tolist() if isinstance(v, (int, float))]
                if spot_vals:
                    spot_px = float(sorted(spot_vals)[len(spot_vals) // 2])
                    nearest_i = min(range(len(strikes2)), key=lambda i: abs(float(strikes2[i]) - float(spot_px)))
                    spot_idx = int(nearest_i)
                    spot_label = f"Spot {spot_px:.2f}"
            except Exception:
                spot_idx = None
                spot_label = None
                spot_px = None

            # Strong sanity check: chain-derived spot should roughly match our snapshot spot for the requested symbol.
            try:
                snap = delivery._get_last_price_snapshot(sym)
                snap_px = float(snap.px) if (snap is not None and snap.px is not None) else None
            except Exception:
                snap_px = None

            try:
                if isinstance(snap_px, (int, float)) and isinstance(spot_px, (int, float)) and snap_px and spot_px:
                    rel = abs(float(snap_px) - float(spot_px)) / max(1.0, abs(float(snap_px)))
                    if rel >= 0.15:
                        return {
                            "error": (
                                f"Symbol mismatch suspected for **{sym}** exp **{exp_for_fetch}**: "
                                f"snapshot spot {float(snap_px):.2f} vs chain spot {float(spot_px):.2f}. "
                                "Refusing to render."
                            ),
                            "ttl_sec": 5,
                        }
            except Exception:
                pass

            # Hard sanity check: a correct chain should bracket spot.
            try:
                if isinstance(spot_px, (int, float)) and spot_px and strikes2:
                    tol = 5.0 if sym in {"SPX", "SPXW"} else 1.0
                    if float(spot_px) < (float(min(strikes2)) - tol) or float(spot_px) > (float(max(strikes2)) + tol):
                        return {
                            "error": (
                                f"Symbol/expiry mismatch for **{sym}** exp **{exp_for_fetch}**: "
                                f"spot {float(spot_px):.2f} not bracketed by strikes [{float(min(strikes2)):.2f}, {float(max(strikes2)):.2f}]. "
                                "Refusing to render."
                            ),
                            "ttl_sec": 5,
                        }
            except Exception:
                pass

            exp_label0 = exp_label_resolved or exp_for_fetch or exp_for_key
            top_tag = f" | Top {top_n}" if top_n > 0 else ""
            include_iv = bool(include_iv_request)
            title = f"OI/IV — {sym} — exp {exp_label0} — {window_label}{top_tag}"
            if spot_label:
                title = f"{title} — {spot_label}"
            try:
                title = f"{title} — contracts {int(len(df))}"
            except Exception:
                pass

            # Preferred: worker renders from our deterministic payload (parity, no symbol ambiguity).
            try:
                if not (await _oi_worker_payload_renderer_ok()):
                    try:
                        reason = str(_OI_WORKER_OI_IV_PARITY.get("reason") or "")
                    except Exception:
                        reason = ""
                    reason = reason or "unknown"
                    raise RuntimeError(f"worker_parity_disabled:{reason}")
                tw0 = time.perf_counter()
                payload: dict[str, object] = {
                    "symbol": sym,
                    "strikes": [float(x) for x in strikes2],
                    "call_oi": [float(x) for x in oi_calls2],
                    "put_oi": [float(x) for x in oi_puts2],
                    "call_iv": [float(x) for x in (iv_pct2 if include_iv else [])],
                    "put_iv": None,
                    "title": title,
                    "include_iv_overlay": bool(include_iv),
                    "x_label": "Strike",
                }

                # Optional: renderer layout tuning for tick label clipping.
                # These are consumed by the parity worker (worker/worker_api_parity.py) and
                # are ignored by older workers.
                try:
                    raw_bottom = (os.getenv("TNT_OI_IV_LAYOUT_BOTTOM", "") or "").strip()
                    if raw_bottom:
                        bottom = float(raw_bottom)
                        if 0.02 <= bottom <= 0.30:
                            payload["layout_bottom"] = float(bottom)
                except Exception:
                    pass
                try:
                    truthy = {"1", "true", "yes", "y", "on"}
                    # Bulletproof default: in deterministic worker parity mode, always save with tight bbox
                    # to prevent strike tick label clipping across backends/Discord screenshots.
                    # In strict parity mode, only enable if explicitly requested (to avoid pixel mismatch).
                    if _oi_worker_parity_mode() == "deterministic":
                        payload["save_bbox_tight"] = True
                    elif (os.getenv("TNT_OI_IV_SAVE_BBOX_TIGHT", "0") or "0").strip().lower() in truthy:
                        payload["save_bbox_tight"] = True
                except Exception:
                    pass
                png0 = await _worker_render_oi_iv_payload_png(payload)
                render_mode = "worker"
                try:
                    print(f"[TNT][OI][PERF] render_mode=worker symbol={sym} worker_s={time.perf_counter()-tw0:.2f}")
                except Exception:
                    pass
                try:
                    worker = (os.getenv("TNT_WORKER_URL", "") or "").strip()
                    print(f"[TNT][OI][PROOF] render_mode=worker symbol={sym} worker={worker} key={cache_key_png_full}")
                except Exception:
                    pass

                footer = _format_footer()
                text = f"🧾 **Options OI/IV** — **{sym}** | by **strike** | exp **{exp_label0}** | {window_label}"
                if spot_label:
                    text = f"{text} | {spot_label}"
                if footer:
                    text = f"{text}\n_{footer}_"

                try:
                    macro_line = await _get_macro_regime_line()
                    if macro_line:
                        text = text + "\n" + macro_line
                        text = await _maybe_append_crypto_context_if_macro(text, macro_line, sep="\n")
                except Exception:
                    pass

                text = _append_oi_universe_hint(text)

                return {
                    "png_bytes": bytes(png0),
                    "png_err": None,
                    "meta": {
                        "mode": "strike",
                        "exp": str(exp_for_fetch or exp_label0),
                        "symbol": sym,
                        "render_mode": render_mode,
                        "include_iv_overlay": bool(include_iv),
                        "iv_key": str(iv_key),
                    },
                    "caption": text,
                    "filename": f"{sym.lower()}_oi_strike.png",
                    "ttl_sec": max(int(os.getenv("TNT_TTL_OI_PNG_SEC", "45")), 5),
                }
            except Exception as exc:
                _oi_log_worker_fallback(symbol=sym, reason=str(exc), key=cache_key_png_full)
                if _oi_worker_mode() == "required":
                    return {
                        "error": (
                            f"Worker required for **{sym}** /oi but worker render failed: {exc}. "
                            "(Set TNT_OI_WORKER_MODE=preferred to allow local fallback.)"
                        ),
                        "ttl_sec": 3,
                    }

            tr0 = time.perf_counter()
            def _render_local_shared() -> bytes | None:
                try:
                    from delivery.oi_iv_render import render_oi_iv_png as _shared_render
                except Exception:
                    return None

                dpi_used = 150
                try:
                    if profile is not None:
                        dpi_used = int(getattr(profile, "dpi", 150) or 150)
                except Exception:
                    dpi_used = 150

                return _shared_render(
                    title=title,
                    x_labels=labels,
                    iv_pct=(iv_pct2 if include_iv else []),
                    oi_calls=oi_calls2,
                    oi_puts=oi_puts2,
                    dpi=int(dpi_used),
                    include_iv_overlay=bool(include_iv),
                )

            png = await asyncio.to_thread(_render_local_shared)
            try:
                print(f"[TNT][OI][PERF] render_mode=local symbol={sym} mpl_s={time.perf_counter()-tr0:.2f}")
            except Exception:
                pass
            try:
                print(f"[TNT][OI][PROOF] render_mode=local symbol={sym} key={cache_key_png_full}")
            except Exception:
                pass
            if not png:
                return {"error": "Failed to render options chart (matplotlib missing or render error).", "ttl_sec": 5}

            footer = _format_footer()
            text = f"🧾 **Options OI/IV** — **{sym}** | by **strike** | exp **{exp_label0}** | {window_label}"
            if spot_label:
                text = f"{text} | {spot_label}"
            if footer:
                text = f"{text}\n_{footer}_"

            try:
                macro_line = await _get_macro_regime_line()
                if macro_line:
                    text = text + "\n" + macro_line
                    text = await _maybe_append_crypto_context_if_macro(text, macro_line, sep="\n")
            except Exception:
                pass

            text = _append_oi_universe_hint(text)

            return {
                "png_bytes": png,
                "png_err": None,
                "meta": {
                    "mode": "strike",
                    "exp": str(exp_label0),
                    "symbol": sym,
                    "render_mode": render_mode,
                    "include_iv_overlay": bool(include_iv),
                    "iv_key": str(iv_key),
                },
                "caption": text,
                "filename": f"{sym.lower()}_oi_strike.png",
                "ttl_sec": max(int(os.getenv("TNT_TTL_OI_PNG_SEC", "45")), 5),
            }

        await run_heavy_chart(
            interaction=interaction,
            cmd="oi",
            cache_key=cache_key_png_full,
            render_fn=_render_fn,
            cache_get=_cache_get,
            cache_set=_cache_set,
            profile_snapshot=prof,
            redis_sf_namespace="oi",
            redis_sf_work_key=cache_key_png_full,
            redis_sf_lock_ttl_s=60,
            redis_sf_wait_timeout_s=25,
            redis_sf_poll_ms=250,
        )
        return

    # by=expiration
    async def _render_fn_exp(*, profile: RenderProfile | None = None):
        expirations = await _oi_get_expiries_cached(sym, api_key=api_key, max_expiries=6)
        if not expirations:
            return {"error": f"Could not discover upcoming expirations for **{sym}**.", "ttl_sec": 8}

        summaries: list[dict[str, object]] = []
        for exp in expirations:
            df = await delivery._fetch_polygon_options_chain_df(
                sym,
                expiration_ymd=exp,
                strike_window_pct=strike_window_pct,
                max_contracts=120,
                concurrency=8,
            )
            if df is None or getattr(df, "empty", True):
                continue

            try:
                import pandas as pd

                work = df.copy()
                work["open_interest"] = pd.to_numeric(work.get("open_interest"), errors="coerce")
                work["iv"] = pd.to_numeric(work.get("iv"), errors="coerce")
                oi = work["open_interest"].fillna(0.0)
                oi = oi.where(oi > 0.0, 0.0)
                oi_sum = float(oi.sum())
                iv = work["iv"]
                mask = (~iv.isna()) & (oi > 0.0)
                if bool(mask.any()):
                    iv_avg = float((iv[mask] * oi[mask]).sum() / oi[mask].sum())
                else:
                    iv_avg = float(iv.mean())
            except Exception:
                continue

            summaries.append({"expiration": exp, "oi_sum": oi_sum, "iv_avg": iv_avg})

        if not summaries:
            return {"error": f"No usable options OI/IV data returned for **{sym}** across expirations ({provider}).", "ttl_sec": 8}

        exp_labels, oi_sums, iv_pct = _bucket_oi_iv_by_expiration(summaries)
        window_pct = int(strike_window_pct * 100)
        include_iv = bool(include_iv_request)
        title = f"{sym} Options — OI by Expiration + IV Overlay | Next {len(exp_labels)} Exps | {window_label}"
        if not include_iv:
            title = title.replace(" + IV Overlay", "") + " | IV hidden"
        png = await asyncio.to_thread(
            _render_oi_iv_png,
            title=title,
            x_labels=exp_labels,
            oi_total=oi_sums,
            iv_pct=iv_pct,
            x_label="Expiration",
            profile=profile,
            include_iv_overlay=include_iv,
        )
        if not png:
            return {"error": "Failed to render options chart (matplotlib missing or render error).", "ttl_sec": 5}

        footer = _format_footer()
        text = f"🧾 **Options OI/IV** — **{sym}** | by **expiration** | bounded chain ({window_label}, per-exp cap)"
        if footer:
            text = f"{text}\n_{footer}_"

        try:
            macro_line = await _get_macro_regime_line()
            if macro_line:
                text = text + "\n" + macro_line
                text = await _maybe_append_crypto_context_if_macro(text, macro_line, sep="\n")
        except Exception:
            pass

        text = _append_oi_universe_hint(text)

        return {
            "png_bytes": png,
            "png_err": None,
            "meta": {"mode": "expiration", "exp": "auto", "symbol": sym, "render_mode": "local"},
            "caption": text,
            "filename": f"{sym.lower()}_oi_expiration.png",
            "ttl_sec": max(int(os.getenv("TNT_TTL_OI_PNG_SEC", "45")), 5),
        }

    await run_heavy_chart(
        interaction=interaction,
        cmd="oi",
        cache_key=cache_key_png_full,
        render_fn=_render_fn_exp,
        cache_get=_cache_get,
        cache_set=_cache_set,
        profile_snapshot=prof,
    )


@oi.autocomplete("symbol")
async def _oi_symbol_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    cur = str(current or "").strip().upper()
    warmed = [s for s in OI_WARMED if (not cur or cur in s)]
    extended = [s for s in OI_EXTENDED if (not cur or cur in s)]
    picks = warmed + extended
    out: list[app_commands.Choice[str]] = []
    for s in picks[:25]:
        label = f"WARMED • {s}" if s in OI_WARMED_SET else f"EXT • {s}"
        out.append(app_commands.Choice(name=label, value=s))
    return out


async def _render_pressure_chart_result(
    *,
    sym: str,
    dte_min: int,
    dte_max: int,
    strike_window_pct: float,
    max_contracts_per_exp: int,
    max_expirations: int,
    ttl_sec: int,
    profile: RenderProfile | None = None,
) -> dict[str, object]:
    from datetime import date

    api_key, _base_url, provider = delivery._polygon_key_and_base()
    if not api_key:
        return {"error": "Pressure unavailable: missing market-data API key (set it in `.env.local`).", "ttl_sec": 6}

    def _fmt_money(val: float | None) -> str:
        if val is None:
            return "n/a"
        x = float(val)
        sign = "-" if x < 0 else ""
        ax = abs(x)
        if ax >= 1e9:
            return f"{sign}${ax/1e9:.2f}B"
        if ax >= 1e6:
            return f"{sign}${ax/1e6:.2f}M"
        if ax >= 1e3:
            return f"{sign}${ax/1e3:.2f}K"
        return f"{sign}${ax:.0f}"

    async def _discover_expirations_window(*, start_ymd: str, end_ymd: str) -> list[str]:
        import aiohttp

        underlying = delivery._map_underlying_for_options(sym)
        params: dict[str, object] = {
            "underlying_ticker": underlying,
            "expiration_date.gte": str(start_ymd)[:10],
            "expiration_date.lte": str(end_ymd)[:10],
            "limit": 1000,
            "apiKey": api_key,
        }

        # Attempt to bound strikes to reduce contract count.
        underlying_px = None
        try:
            snap = delivery._get_last_price_snapshot(sym)
            if snap and snap.px is not None:
                underlying_px = float(snap.px)
        except Exception:
            underlying_px = None

        if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
            strike_min = float(underlying_px) * (1.0 - float(strike_window_pct))
            strike_max = float(underlying_px) * (1.0 + float(strike_window_pct))
            params["strike_price.gte"] = f"{strike_min:.6f}"
            params["strike_price.lte"] = f"{strike_max:.6f}"

        base_url = delivery._polygon_key_and_base()[1]
        url = f"{base_url}/v3/reference/options/contracts"
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                if int(resp.status) != 200:
                    return []
                payload = await resp.json()

        if not isinstance(payload, dict) or payload.get("status") != "OK":
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        expirations: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            exp = str(item.get("expiration_date") or "").strip()
            if exp:
                expirations.add(exp[:10])
        return sorted(expirations)[: max(1, int(max_expirations))]

    now_et = delivery._now_et()
    start_ymd = (now_et.date() + timedelta(days=int(dte_min))).isoformat()
    end_ymd = (now_et.date() + timedelta(days=int(dte_max))).isoformat()

    expirations = await _discover_expirations_window(start_ymd=start_ymd, end_ymd=end_ymd)

    # Hard guardrail: enforce calendar-day DTE bounds (ET anchored).
    expirations_included: list[str] = []
    for exp in list(expirations or []):
        exp_s = str(exp or "")[:10]
        if not exp_s:
            continue
        try:
            exp_date = date.fromisoformat(exp_s)
        except Exception:
            continue
        dte = int((exp_date - now_et.date()).days)
        if dte < int(dte_min) or dte > int(dte_max):
            continue
        expirations_included.append(exp_s)

    expirations_included = sorted(set(expirations_included))

    frames = []
    if expirations_included:
        for exp in expirations_included:
            df = await delivery._fetch_polygon_options_chain_df(
                sym,
                expiration_ymd=exp,
                strike_window_pct=strike_window_pct,
                max_contracts=max_contracts_per_exp,
                concurrency=10,
            )
            if df is None or getattr(df, "empty", True):
                continue
            frames.append(df)

    chain = None
    options_ok = False
    if frames:
        options_ok = True
        try:
            import pandas as pd

            chain = pd.concat(frames, ignore_index=True)
        except Exception:
            chain = frames[0]

    spot = None
    try:
        snap = delivery._get_last_price_snapshot(sym)
        if snap and snap.px is not None:
            spot = float(snap.px)
    except Exception:
        spot = None

    # --- compute premium-weighted ladder (best-effort) ---
    call_prem: float | None = None
    put_prem: float | None = None
    net_pressure: float | None = None
    pressure_score: float | None = None
    pcr_prem: float | None = None
    gravity: list[float] = []
    step = 1.0
    strike_rows: list[tuple[float, float, float]] = []

    if options_ok and chain is not None and not getattr(chain, "empty", True):
        call_sum = 0.0
        put_sum = 0.0
        by_strike: dict[float, dict[str, float]] = {}

        # Estimate strike step (bucket size) from observed strikes
        try:
            strikes = sorted({float(x) for x in chain.get("strike").dropna().tolist()})
            diffs = [
                round(strikes[i + 1] - strikes[i], 6)
                for i in range(len(strikes) - 1)
                if (strikes[i + 1] - strikes[i]) > 0
            ]
            if diffs:
                diffs2 = sorted(diffs)
                step = float(diffs2[len(diffs2) // 2])
        except Exception:
            step = 1.0
        if step is None or not (step > 0):
            step = 1.0
        if step < 0.5:
            step = 0.5
        if step > 10.0:
            step = 5.0

        def _bucket_strike(raw: float) -> float:
            try:
                return round(round(float(raw) / float(step)) * float(step), 6)
            except Exception:
                return float(raw)

        try:
            for _, row in chain.iterrows():
                try:
                    opt_type = str(row.get("type") or "").lower()
                    strike_raw = row.get("strike")
                    if strike_raw is None:
                        continue
                    strike = float(strike_raw)
                    bid = row.get("bid")
                    ask = row.get("ask")
                    last = row.get("last")
                    vol = row.get("volume")
                    vol_prev = row.get("prev_volume")
                except Exception:
                    continue

                try:
                    v = float(vol) if vol is not None else 0.0
                except Exception:
                    v = 0.0
                if v <= 0:
                    try:
                        v = float(vol_prev) if vol_prev is not None else 0.0
                    except Exception:
                        v = 0.0
                if v <= 0:
                    continue

                px = None
                try:
                    b = float(bid) if bid is not None else None
                    a = float(ask) if ask is not None else None
                    if b is not None and a is not None and b > 0 and a > 0 and a >= b:
                        px = (b + a) / 2.0
                except Exception:
                    px = None
                if px is None:
                    try:
                        px = float(last) if last is not None else None
                    except Exception:
                        px = None
                if px is None or not (px > 0):
                    continue

                prem = float(px) * float(v) * 100.0
                s_bucket = float(_bucket_strike(strike))
                rec = by_strike.setdefault(s_bucket, {"call": 0.0, "put": 0.0, "total": 0.0})
                if opt_type == "call":
                    call_sum += prem
                    rec["call"] += prem
                    rec["total"] += prem
                elif opt_type == "put":
                    put_sum += prem
                    rec["put"] += prem
                    rec["total"] += prem
        except Exception:
            # Keep options_ok but treat as unusable.
            call_sum = 0.0
            put_sum = 0.0

        if call_sum > 0 or put_sum > 0:
            call_prem = float(call_sum)
            put_prem = float(put_sum)
            net_pressure = float(call_sum) - float(put_sum)
            denom = float(call_sum + put_sum) + 1e-9
            pressure_score = float(net_pressure) / denom
            pcr_prem = (float(put_sum) / float(call_sum)) if call_sum > 0 else float("inf")

            # Gravity zones: top premium strikes near spot.
            try:
                near_only = []
                for k, rec in by_strike.items():
                    tot = float(rec.get("total") or 0.0)
                    if tot <= 0:
                        continue
                    if isinstance(spot, (int, float)) and spot and spot > 0:
                        if abs(float(k) - float(spot)) / float(spot) > 0.06:
                            continue
                    near_only.append((float(tot), float(k)))
                near_only.sort(reverse=True)
                gravity = [k for _tot, k in near_only[:4]]
            except Exception:
                gravity = []

            # Choose strikes to draw
            try:
                for k, rec in by_strike.items():
                    tot = float(rec.get("total") or 0.0)
                    if tot <= 0:
                        continue
                    if isinstance(spot, (int, float)) and spot and spot > 0:
                        if abs(float(k) - float(spot)) / float(spot) > 0.08:
                            continue
                    net = float(rec.get("call") or 0.0) - float(rec.get("put") or 0.0)
                    strike_rows.append((tot, float(k), net))
                strike_rows.sort(reverse=True)
                strike_rows = strike_rows[:25]
                strike_rows.sort(key=lambda t: t[1])
            except Exception:
                strike_rows = []

    # --- price series + pivots (always best-effort) ---
    price_x: list[datetime] = []
    price_y: list[float] = []
    try:
        from delivery.on_demand_data import polygon_aggs

        payload = await asyncio.to_thread(polygon_aggs, sym, multiplier=15, timespan="minute", days=10)
        rows = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                t = r.get("t")
                c = r.get("c")
                if t is None or c is None:
                    continue
                try:
                    dt = datetime.fromtimestamp(float(t) / 1000.0, tz=timezone.utc).astimezone(delivery.ET_TZ or timezone.utc)
                    price_x.append(dt)
                    price_y.append(float(c))
                except Exception:
                    continue
    except Exception:
        pass

    # Keep the price panel tightly focused on "as-of" context so the x-axis
    # doesn't look like it refers to the expiration window (7–14 DTE).
    price_lookback_days = 3
    try:
        price_lookback_days = int(os.getenv("TNT_PRESSURE_PRICE_LOOKBACK_DAYS", "3"))
    except Exception:
        price_lookback_days = 3
    try:
        price_lookback_days = int(min(max(int(price_lookback_days), 1), 10))
    except Exception:
        price_lookback_days = 3
    try:
        if price_x and price_y and len(price_x) == len(price_y):
            cutoff = now_et - timedelta(days=int(price_lookback_days))
            clipped = [(x, y) for x, y in zip(price_x, price_y, strict=True) if x >= cutoff]
            # Only apply clip if we still have enough samples to draw a meaningful line.
            if len(clipped) >= 20:
                price_x = [x for x, _y in clipped]
                price_y = [float(y) for _x, y in clipped]
    except Exception:
        pass

    if (not price_y) and isinstance(spot, (int, float)):
        try:
            price_x = [now_et]
            price_y = [float(spot)]
        except Exception:
            price_x, price_y = [], []

    piv_info = None
    try:
        piv_info = delivery.get_latest_daily_pivots(sym)
    except Exception:
        piv_info = None

    piv = piv_info.get("piv") if isinstance(piv_info, dict) else None
    pivot_levels: dict[str, float] = {}
    if isinstance(piv, dict):
        for k in ("P", "R1", "R2", "S1", "S2"):
            v = piv.get(k)
            if v is None:
                continue
            try:
                pivot_levels[k] = float(v)
            except Exception:
                continue

    regime = "UNKNOWN"
    try:
        built = delivery.build_signal_payload(sym)
        if built:
            payload_sig, _prob, _gate = built
            rv = payload_sig.get("regime") if isinstance(payload_sig, dict) else None
            if rv:
                regime = str(rv).upper()
    except Exception:
        regime = "UNKNOWN"

    def _try_render_pressure_png():
        try:
            import matplotlib

            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except Exception:
            return None, "matplotlib_missing", None

        # --- expiry aggregates (best-effort; used for the "Expiry Ladder" panel) ---
        expiry_stats: list[dict[str, object]] = []
        try:
            if options_ok and chain is not None and not getattr(chain, "empty", True):
                exp_col = None
                for cand in ("expiration", "expiry", "expiration_date"):
                    try:
                        if cand in getattr(chain, "columns", []):
                            exp_col = cand
                            break
                    except Exception:
                        continue

                if exp_col:
                    per_exp: dict[str, dict[str, object]] = {}
                    for _, row in chain.iterrows():
                        try:
                            exp = str(row.get(exp_col) or "")[:10]
                            opt_type = str(row.get("type") or "").lower()
                            strike_raw = row.get("strike")
                            if not exp or strike_raw is None:
                                continue
                            strike = float(strike_raw)
                        except Exception:
                            continue

                        try:
                            v = float(row.get("volume")) if row.get("volume") is not None else 0.0
                        except Exception:
                            v = 0.0
                        if v <= 0:
                            try:
                                v = float(row.get("prev_volume")) if row.get("prev_volume") is not None else 0.0
                            except Exception:
                                v = 0.0
                        if v <= 0:
                            continue

                        px = None
                        try:
                            b = float(row.get("bid")) if row.get("bid") is not None else None
                            a = float(row.get("ask")) if row.get("ask") is not None else None
                            if b is not None and a is not None and b > 0 and a > 0 and a >= b:
                                px = (b + a) / 2.0
                        except Exception:
                            px = None
                        if px is None:
                            try:
                                px = float(row.get("last")) if row.get("last") is not None else None
                            except Exception:
                                px = None
                        if px is None or not (px > 0):
                            continue

                        prem = float(px) * float(v) * 100.0
                        is_call = opt_type == "call"

                        rec = per_exp.setdefault(
                            exp,
                            {
                                "exp": exp,
                                "call": 0.0,
                                "put": 0.0,
                                "net": 0.0,
                                "by_strike": {},
                            },
                        )

                        if is_call:
                            rec["call"] = float(rec.get("call") or 0.0) + prem
                            rec["net"] = float(rec.get("net") or 0.0) + prem
                        elif opt_type == "put":
                            rec["put"] = float(rec.get("put") or 0.0) + prem
                            rec["net"] = float(rec.get("net") or 0.0) - prem
                        else:
                            continue

                        try:
                            bys = rec.get("by_strike")
                            if isinstance(bys, dict):
                                bys[strike] = float(bys.get(strike) or 0.0) + (prem if is_call else -prem)
                        except Exception:
                            pass

                    # finalize
                    for _exp, rec in per_exp.items():
                        try:
                            call_v = float(rec.get("call") or 0.0)
                            put_v = float(rec.get("put") or 0.0)
                            net_v = float(rec.get("net") or 0.0)
                            denom = (call_v + put_v) + 1e-9
                            score_v = float(net_v) / float(denom)
                            pcr_v = (put_v / call_v) if call_v > 0 else float("inf")

                            top_strikes = []
                            bys = rec.get("by_strike")
                            if isinstance(bys, dict) and bys:
                                items = [(float(k), float(v)) for k, v in bys.items() if k is not None and v is not None]
                                items.sort(key=lambda t: abs(t[1]), reverse=True)
                                top_strikes = [f"{k:g}" for (k, _v) in items[:3]]

                            expiry_stats.append(
                                {
                                    "exp": str(rec.get("exp") or "")[:10],
                                    "call": call_v,
                                    "put": put_v,
                                    "net": net_v,
                                    "score": score_v,
                                    "pcr": pcr_v,
                                    "top": top_strikes,
                                }
                            )
                        except Exception:
                            continue

                    expiry_stats.sort(key=lambda d: str(d.get("exp") or ""))
        except Exception:
            expiry_stats = []

        fig = plt.figure(figsize=(12.2, 7.8))
        gs = fig.add_gridspec(3, 1, height_ratios=[2.2, 1.6, 0.9], hspace=0.18)
        ax_price = fig.add_subplot(gs[0, 0])
        ax_ladder = fig.add_subplot(gs[1, 0])
        ax_expiry = fig.add_subplot(gs[2, 0])
        fig.patch.set_facecolor("#0b0f14")
        for ax in (ax_price, ax_ladder, ax_expiry):
            ax.set_facecolor("#0b0f14")
            ax.tick_params(colors="#c9d1d9")
            for spine in ax.spines.values():
                spine.set_color("#2d333b")

        _add_tnt_watermark(ax_price)

        # Trust annotation: included expiries + as-of timestamp.
        try:
            def _fmt_md(ymd: str) -> str:
                try:
                    d = date.fromisoformat(str(ymd)[:10])
                    return d.strftime("%b %d").replace(" 0", " ")
                except Exception:
                    return str(ymd)[:10]

            if expirations_included:
                exp_min = expirations_included[0]
                exp_max = expirations_included[-1]
                exp_txt = f"{_fmt_md(exp_min)} – {_fmt_md(exp_max)}" if exp_min != exp_max else _fmt_md(exp_min)
            else:
                exp_txt = "(none)"

            asof_txt = now_et.strftime("%Y-%m-%d %H:%M ET")
            fig.text(
                0.012,
                0.988,
                f"Expiries included: {exp_txt} ({int(dte_min)}–{int(dte_max)} DTE)\nAs of: {asof_txt} | Price context: last {int(price_lookback_days)}d",
                ha="left",
                va="top",
                fontsize=8,
                color="#c9d1d9",
                alpha=0.80,
            )
        except Exception:
            pass

        # Required framing: this is NOT a forward-looking price path.
        try:
            fig.text(
                0.50,
                0.945,
                "Options Positioning Snapshot (7–14 DTE)\nShows where positioning pressure EXISTS, not future price paths",
                ha="center",
                va="top",
                fontsize=8,
                color="#c9d1d9",
                alpha=0.78,
            )
        except Exception:
            pass

        # --- Top: price + pivots ---
        if price_x and price_y:
            # Slightly dim price so the positioning ladder dominates.
            ax_price.plot(price_x, price_y, linewidth=1.0, color="#a5d6ff", alpha=0.60)
            ax_price.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            ax_price.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))

        piv_col = "#c9d1d9"
        for k, v in pivot_levels.items():
            ax_price.axhline(float(v), linewidth=0.9, alpha=0.45, color=piv_col)
            try:
                ax_price.text(
                    0.01,
                    float(v),
                    f" {k}",
                    color=piv_col,
                    alpha=0.7,
                    fontsize=8,
                    transform=ax_price.get_yaxis_transform(),
                    va="center",
                )
            except Exception:
                pass

        try:
            now_px = float(price_y[-1]) if price_y else (float(spot) if isinstance(spot, (int, float)) else None)
            if now_px is not None:
                ax_price.axhline(now_px, linewidth=0.9, alpha=0.22, color="#c9d1d9")
                ax_price.text(
                    0.99,
                    now_px,
                    " NOW ",
                    color="#0b0f14",
                    fontsize=8,
                    ha="right",
                    va="center",
                    transform=ax_price.get_yaxis_transform(),
                    bbox={"facecolor": "#c9d1d9", "edgecolor": "#c9d1d9", "alpha": 0.55, "pad": 1.6},
                )
        except Exception:
            pass

        ax_price.grid(True, alpha=0.12, linestyle="--")
        ax_price.set_title(f"{sym} — TNT Positioning Pressure ({dte_min}–{dte_max} DTE)", color="#c9d1d9")

        try:
            expected = "Context dependent"
            if str(regime).upper() == "RANGE":
                expected = "Edge pin / mean reversion"
            elif str(regime).upper() == "TREND":
                expected = "Pressure zones break"
            elif str(regime).upper() == "DO_NOTHING":
                expected = "Conflicting clusters above & below spot"

            ax_price.text(
                0.985,
                0.06,
                f"Regime: {str(regime)}\nExpected Behavior: {expected}",
                transform=ax_price.transAxes,
                ha="right",
                va="bottom",
                fontsize=9,
                color="#c9d1d9",
                bbox={"facecolor": "#0b0f14", "edgecolor": "#2d333b", "alpha": 0.85, "pad": 3.0},
            )
        except Exception:
            pass

        # --- Bottom: strike ladder (premium-weighted net) ---
        ax_ladder.axvline(0.0, linewidth=1.0, alpha=0.35, color="#c9d1d9")
        up_color = "#2ea043"
        down_color = "#f85149"

        ys = [r[1] for r in strike_rows]
        xs = [r[2] for r in strike_rows]
        colors = [up_color if v >= 0 else down_color for v in xs]
        if ys and xs:
            ax_ladder.barh(ys, xs, height=float(step) * 0.72, color=colors, alpha=0.65, linewidth=0.0)

        for g in gravity:
            try:
                ax_ladder.axhspan(
                    float(g) - float(step) / 2.0,
                    float(g) + float(step) / 2.0,
                    color="#c9d1d9",
                    alpha=0.10,
                )
            except Exception:
                continue

        # SPOT / Current Price Anchor on strike axis (horizontal line + ±0.5% band).
        try:
            if isinstance(spot, (int, float)) and float(spot) > 0:
                s = float(spot)
                lo = s * (1.0 - 0.005)
                hi = s * (1.0 + 0.005)
                ax_ladder.axhspan(lo, hi, color="#c9d1d9", alpha=0.08)
                ax_ladder.axhline(s, linewidth=2.2, alpha=0.60, color="#c9d1d9", linestyle="-")
                ax_ladder.text(
                    0.99,
                    s,
                    " Current Price Anchor ",
                    color="#0b0f14",
                    fontsize=8,
                    ha="right",
                    va="center",
                    transform=ax_ladder.get_yaxis_transform(),
                    bbox={"facecolor": "#c9d1d9", "edgecolor": "#c9d1d9", "alpha": 0.55, "pad": 1.6},
                )
        except Exception:
            pass

        ax_ladder.grid(True, axis="x", alpha=0.12, linestyle="--")
        ax_ladder.set_ylabel("Strike", color="#c9d1d9")
        ax_ladder.set_xlabel("$ premium-weighted volume (calls − puts)", color="#c9d1d9")

        # Keep strike axis tight around what we drew.
        try:
            if ys:
                ax_ladder.set_ylim(min(ys) - float(step), max(ys) + float(step))
        except Exception:
            pass

        # Summary line (or unavailable notice).
        try:
            if net_pressure is None or pressure_score is None or pcr_prem is None:
                ax_ladder.text(
                    0.01,
                    0.98,
                    "Options snapshot unavailable — showing price + pivots only.",
                    transform=ax_ladder.transAxes,
                    ha="left",
                    va="top",
                    fontsize=9,
                    color="#c9d1d9",
                )
            else:
                if isinstance(pcr_prem, (int, float)) and math.isfinite(float(pcr_prem)):
                    line = f"Net: {_fmt_money(net_pressure)} | Score: {float(pressure_score):+.2f} | PCR: {float(pcr_prem):.2f}"
                else:
                    line = f"Net: {_fmt_money(net_pressure)} | Score: {float(pressure_score):+.2f} | PCR: ∞"
                ax_ladder.text(
                    0.01,
                    0.98,
                    line,
                    transform=ax_ladder.transAxes,
                    ha="left",
                    va="top",
                    fontsize=9,
                    color="#c9d1d9",
                )
        except Exception:
            pass

        # --- Third: Expiry ladder (net premium by expiry) ---
        try:
            ax_expiry.axvline(0.0, linewidth=1.0, alpha=0.35, color="#c9d1d9")
            ax_expiry.grid(True, axis="x", alpha=0.12, linestyle="--")
            ax_expiry.set_title("Expiry ladder (net premium pressure)", color="#c9d1d9", fontsize=10)
            ax_expiry.set_xlabel("Net premium (calls − puts)", color="#c9d1d9")

            if not expiry_stats:
                ax_expiry.text(
                    0.01,
                    0.80,
                    "Expiry ladder unavailable — missing per-expiry snapshot.",
                    transform=ax_expiry.transAxes,
                    ha="left",
                    va="top",
                    fontsize=9,
                    color="#c9d1d9",
                )
                ax_expiry.set_yticks([])
            else:
                def _fmt_exp_short(ymd: str) -> str:
                    try:
                        d = date.fromisoformat(str(ymd)[:10])
                        return d.strftime("%b %d").replace(" 0", " ")
                    except Exception:
                        return str(ymd)[:10]

                labels = [_fmt_exp_short(str(x.get("exp") or "")) for x in expiry_stats]
                vals = [float(x.get("net") or 0.0) for x in expiry_stats]
                colors = [up_color if v >= 0 else down_color for v in vals]
                y = list(range(len(vals)))

                ax_expiry.barh(y, vals, color=colors, alpha=0.65, height=0.65, linewidth=0.0)
                ax_expiry.set_yticks(y)
                ax_expiry.set_yticklabels(labels, color="#c9d1d9")

                try:
                    max_abs = max([abs(v) for v in vals] + [1.0])
                    ax_expiry.set_xlim(-1.15 * max_abs, 1.15 * max_abs)
                except Exception:
                    pass

                for yi, st in enumerate(expiry_stats):
                    try:
                        pcr_v = float(st.get("pcr"))
                        score_v = float(st.get("score"))
                        top = st.get("top")
                        top_txt = ""
                        if isinstance(top, list) and top:
                            top_txt = " | Top " + ", ".join([str(s) for s in top[:3]])
                        if math.isfinite(pcr_v):
                            info = f"PCR {pcr_v:.2f} | Score {score_v:+.2f}{top_txt}"
                        else:
                            info = f"PCR ∞ | Score {score_v:+.2f}{top_txt}"
                        ax_expiry.text(
                            0.99,
                            yi,
                            info,
                            transform=ax_expiry.get_yaxis_transform(),
                            ha="right",
                            va="center",
                            fontsize=8,
                            color="#c9d1d9",
                            alpha=0.85,
                        )
                    except Exception:
                        continue
        except Exception:
            pass

        fig.tight_layout()
        buf = io.BytesIO()
        dpi_used = None
        try:
            if profile is not None:
                dpi_used = int(profile.dpi)
        except Exception:
            dpi_used = None
        if dpi_used is None or dpi_used <= 0:
            dpi_used = 170
        fig.savefig(buf, format="png", dpi=int(dpi_used), metadata=_tnt_png_metadata())
        plt.close(fig)

        meta = {
            "spot": spot,
            "regime": regime,
            "call_premium": float(call_prem) if call_prem is not None else None,
            "put_premium": float(put_prem) if put_prem is not None else None,
            "net_pressure": float(net_pressure) if net_pressure is not None else None,
            "pressure_score": float(pressure_score) if pressure_score is not None else None,
            "pcr_premium": float(pcr_prem) if (pcr_prem is not None and math.isfinite(float(pcr_prem))) else None,
            "gravity": gravity,
            "expirations": expirations,
            "provider": provider,
            "contracts": int(len(chain)) if (chain is not None and hasattr(chain, "__len__")) else None,
            "options_ok": bool(options_ok and call_prem is not None and put_prem is not None),
        }
        return buf.getvalue(), None, meta

    png_bytes, png_err, meta = await asyncio.to_thread(_try_render_pressure_png)
    if not png_bytes:
        return {"error": f"Pressure chart unavailable: {png_err}.", "ttl_sec": 6}

    gravity_txt = " | ".join([f"{g:.2f}" if isinstance(g, (int, float)) else str(g) for g in gravity]) if gravity else "(none)"
    if call_prem is None or put_prem is None or net_pressure is None or pressure_score is None or pcr_prem is None:
        text = (
            f"🧭 **TNT Positioning Pressure ({dte_min}–{dte_max} DTE)** — **{sym}**\n"
            f"Options snapshot unavailable right now ({provider}); showing price + pivots.\n"
            f"Gravity Zones: **{gravity_txt}**"
        )
    else:
        text = (
            f"🧭 **TNT Positioning Pressure ({dte_min}–{dte_max} DTE)** — **{sym}**\n"
            f"Call Prem: **{_fmt_money(call_prem)}** | Put Prem: **{_fmt_money(put_prem)}**\n"
            f"Net Pressure: **{_fmt_money(net_pressure)}** | Score: **{float(pressure_score):+.2f}** | Premium PCR: **{float(pcr_prem):.2f}**\n"
            f"Gravity Zones: **{gravity_txt}**"
        )

    text += "\nShows where near-term institutional positioning is concentrated."
    text += "\n• In TREND → pressure aligns = tailwind"
    text += "\n• In RANGE → clusters act as gravity / rejection zones"
    text += "\n• In DO_NOTHING → informational only (no permission)"

    footer = (str(FOOTER_DISCLAIMER) if FOOTER_DISCLAIMER is not None else "").replace("\r\n", " ").replace("\n", " ").strip()
    if footer:
        text = f"{text}\n_{footer}_"

    try:
        macro_line = await _get_macro_regime_line()
        if macro_line:
            text = text + "\n" + macro_line
            text = await _maybe_append_crypto_context_if_macro(text, macro_line, sep="\n")
    except Exception:
        pass

    return {
        "png_bytes": png_bytes,
        "png_err": None,
        "meta": meta if isinstance(meta, dict) else {},
        "caption": text,
        "filename": f"{sym.lower()}_pressure.png",
        "ttl_sec": int(ttl_sec),
    }


@bot.tree.command(name="pressure", description="Premium: TNT Positioning Pressure (7–14 DTE) chart.")
@app_commands.describe(
    symbol="Underlying ticker (e.g., SPY, SPX, QQQ)",
)
async def pressure(
    interaction: discord.Interaction,
    symbol: str,
) -> None:
    sym_norm = delivery._normalize_symbol_token(symbol)
    if not sym_norm:
        try:
            await interaction.response.send_message("Invalid symbol. Try something like SPY, QQQ, or SPX.", ephemeral=True)
        except Exception:
            pass
        return
    sym = sym_norm

    # Soft per-user cooldown (options-heavy family)
    try:
        cooldown = _cooldown_effective_sec("heavy")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="heavy",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="pressure", bucket="heavy", wait_s=wait_s)
            return
    except Exception:
        pass

    api_key, _base_url, provider = delivery._polygon_key_and_base()
    if not api_key:
        try:
            await interaction.response.send_message(
                "Pressure unavailable: missing market-data API key (set it in `.env.local`).",
                ephemeral=True,
            )
        except Exception:
            pass
        return

    dte_min = 7
    dte_max = 14
    strike_window_pct = 0.10
    max_contracts_per_exp = 350
    max_expirations = 6

    ttl_sec = max(int(os.getenv("TNT_TTL_PRESSURE_PNG_SEC", "45")), 8)
    cache_key_base = f"pressure_png:v1:{sym}:{dte_min}:{dte_max}:{int(strike_window_pct*100)}:{max_contracts_per_exp}"
    cache_key = _gprr_cache_key(cache_key_base)

    async def _cache_get(_key: str):
        cached = await _png_cache_get(_key, family="pressure")
        if cached is None:
            return None
        png_bytes, png_err, meta = cached
        if not png_bytes or png_err:
            return None
        cached_ts = None
        caption = None
        filename = None
        if isinstance(meta, dict):
            cached_ts = meta.get("__tnt_cached_ts")
            caption = meta.get("caption")
            filename = meta.get("filename")
        if not caption:
            caption = f"🧭 **TNT Positioning Pressure ({dte_min}–{dte_max} DTE)** — **{sym}** | _cached_"
        if not filename:
            filename = f"{sym.lower()}_pressure.png"
        return {"png_bytes": png_bytes, "caption": caption, "filename": filename, "cached_asof_ts": cached_ts, "meta": meta}

    async def _cache_set(_key: str, result: dict[str, object]) -> None:
        try:
            png_bytes = result.get("png_bytes")
            png_err = result.get("png_err")
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            meta = dict(meta)
            meta.setdefault("caption", result.get("caption"))
            meta.setdefault("filename", result.get("filename"))
            ttl = int(result.get("ttl_sec") or ttl_sec)
            await _png_cache_set(_key, (png_bytes, png_err, meta), ttl)
        except Exception:
            pass

    async def _discover_expirations_window(*, start_ymd: str, end_ymd: str) -> list[str]:
        import aiohttp

        underlying = delivery._map_underlying_for_options(sym)
        params: dict[str, object] = {
            "underlying_ticker": underlying,
            "expiration_date.gte": str(start_ymd)[:10],
            "expiration_date.lte": str(end_ymd)[:10],
            "limit": 1000,
            "apiKey": api_key,
        }

        # Attempt to bound strikes to reduce contract count.
        underlying_px = None
        try:
            snap = delivery._get_last_price_snapshot(sym)
            if snap and snap.px is not None:
                underlying_px = float(snap.px)
        except Exception:
            underlying_px = None

        if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
            strike_min = float(underlying_px) * (1.0 - float(strike_window_pct))
            strike_max = float(underlying_px) * (1.0 + float(strike_window_pct))
            params["strike_price.gte"] = f"{strike_min:.6f}"
            params["strike_price.lte"] = f"{strike_max:.6f}"

        base_url = delivery._polygon_key_and_base()[1]
        url = f"{base_url}/v3/reference/options/contracts"
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                if int(resp.status) != 200:
                    return []
                payload = await resp.json()

        if not isinstance(payload, dict) or payload.get("status") != "OK":
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        expirations: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            exp = str(item.get("expiration_date") or "").strip()
            if exp:
                expirations.add(exp[:10])
        return sorted(expirations)[: max(1, int(max_expirations))]

    def _fmt_money(val: float | None) -> str:
        if val is None:
            return "n/a"
        x = float(val)
        sign = "-" if x < 0 else ""
        ax = abs(x)
        if ax >= 1e9:
            return f"{sign}${ax/1e9:.2f}B"
        if ax >= 1e6:
            return f"{sign}${ax/1e6:.2f}M"
        if ax >= 1e3:
            return f"{sign}${ax/1e3:.2f}K"
        return f"{sign}${ax:.0f}"

    async def _render_fn(*, profile: RenderProfile | None = None):
        return await _render_pressure_chart_result(
            sym=sym,
            dte_min=int(dte_min),
            dte_max=int(dte_max),
            strike_window_pct=float(strike_window_pct),
            max_contracts_per_exp=int(max_contracts_per_exp),
            max_expirations=int(max_expirations),
            ttl_sec=int(ttl_sec),
            profile=profile,
        )

    await run_heavy_chart(
        interaction=interaction,
        cmd="pressure",
        cache_key=cache_key,
        render_fn=_render_fn,
        cache_get=_cache_get,
        cache_set=_cache_set,
        post_to_channel=True,
    )


@bot.tree.command(name="htf", description="Premium: TNT HTF Reversal Context (Daily/Weekly) chart.")
@app_commands.describe(
    symbol="Underlying ticker (e.g., SPY, QQQ, NVDA)",
)
async def htf(
    interaction: discord.Interaction,
    symbol: str,
) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                await interaction.followup.send(message, ephemeral=True)
                responded = True
                return
            raise

    sym_norm = delivery._normalize_symbol_token(symbol)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like SPY, QQQ, or NVDA.")
        return

    # Map common crypto shorthand to Polygon crypto tickers.
    # Without this, `/htf BTC` can resolve to an equity ticker instead of spot crypto.
    sym_display = sym_norm
    sym_fetch = sym_norm
    try:
        crypto_aliases = {"BTC", "ETH", "XRP", "DOGE", "LTC"}
        if sym_norm.startswith("X:"):
            sym_fetch = sym_norm
        elif sym_norm in crypto_aliases:
            if sym_norm == "BTC":
                env_sym = (os.getenv("TNT_CRYPTO_BTC_TICKER", "") or "").strip().upper()
                sym_fetch = env_sym if env_sym.startswith("X:") else _crypto_polygon_ticker("BTC")
            elif sym_norm == "ETH":
                env_sym = (os.getenv("TNT_CRYPTO_ETH_TICKER", "") or "").strip().upper()
                sym_fetch = env_sym if env_sym.startswith("X:") else _crypto_polygon_ticker("ETH")
            else:
                sym_fetch = _crypto_polygon_ticker(sym_norm)
    except Exception:
        sym_fetch = sym_norm

    sym = sym_display

    # Soft per-user cooldown (options-heavy family)
    try:
        cooldown = _cooldown_effective_sec("heavy")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="heavy",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="htf", bucket="heavy", wait_s=wait_s)
            return
    except Exception:
        pass

    ttl_sec = max(int(os.getenv("TNT_TTL_HTF_PNG_SEC", "60")), 10)
    cache_key_base = f"chart_htf_png:v3:{sym_fetch}:1d"
    cache_key = _gprr_cache_key(cache_key_base)

    async def _cache_get(_key: str):
        cached = await _png_cache_get(_key, family="htf")
        if cached is None:
            return None
        png_bytes, png_err, meta = cached
        if not png_bytes or png_err:
            return None
        cached_ts = None
        caption = None
        filename = None
        if isinstance(meta, dict):
            cached_ts = meta.get("__tnt_cached_ts")
            caption = meta.get("caption")
            filename = meta.get("filename")
        if not caption:
            caption = f"🧠 **TNT HTF Reversal Context (Daily/Weekly)** — **{sym}** | _cached_"
        if not filename:
            filename = f"{sym.lower()}_htf.png"
        return {
            "png_bytes": png_bytes,
            "caption": caption,
            "filename": filename,
            "cached_asof_ts": cached_ts,
            "meta": meta,
        }

    async def _cache_set(_key: str, result: dict[str, object]) -> None:
        try:
            png_bytes = result.get("png_bytes")
            png_err = result.get("png_err")
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            meta = dict(meta)
            meta.setdefault("caption", result.get("caption"))
            meta.setdefault("filename", result.get("filename"))
            ttl = int(result.get("ttl_sec") or ttl_sec)
            await _png_cache_set(_key, (png_bytes, png_err, meta), ttl)
        except Exception:
            pass

    def _sma(values: list[float], window: int) -> list[float]:
        n = len(values)
        out = [math.nan] * n
        if window <= 1 or n == 0:
            return values[:]
        csum = 0.0
        for i, v in enumerate(values):
            csum += v
            if i >= window:
                csum -= values[i - window]
            if i >= window - 1:
                out[i] = csum / float(window)
        return out

    def _rsi(closes: list[float], period: int = 14) -> list[float]:
        n = len(closes)
        out = [math.nan] * n
        if n < period + 1:
            return out
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, period + 1):
            change = closes[i] - closes[i - 1]
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        avg_gain = sum(gains) / float(period)
        avg_loss = sum(losses) / float(period)
        out[period] = 100.0 if avg_loss == 0 else (100.0 - (100.0 / (1.0 + (avg_gain / avg_loss))))
        for i in range(period + 1, n):
            change = closes[i] - closes[i - 1]
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            avg_gain = ((avg_gain * (period - 1)) + gain) / float(period)
            avg_loss = ((avg_loss * (period - 1)) + loss) / float(period)
            out[i] = 100.0 if avg_loss == 0 else (100.0 - (100.0 / (1.0 + (avg_gain / avg_loss))))
        return out

    def _stoch(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        k_period: int = 14,
        d_period: int = 3,
    ) -> tuple[list[float], list[float]]:
        n = len(closes)
        k = [math.nan] * n
        for i in range(n):
            if i < k_period - 1:
                continue
            window_high = max(highs[i - k_period + 1 : i + 1])
            window_low = min(lows[i - k_period + 1 : i + 1])
            denom = window_high - window_low
            if denom <= 0:
                k[i] = 0.0
            else:
                k[i] = 100.0 * (closes[i] - window_low) / denom
        d = _sma([0.0 if math.isnan(v) else v for v in k], d_period)
        for i in range(n):
            if i < (k_period - 1) + (d_period - 1):
                d[i] = math.nan
        return k, d

    def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
        n = len(closes)
        out = [math.nan] * n
        if n < period + 1:
            return out
        trs: list[float] = []
        for i in range(1, n):
            prev_c = float(closes[i - 1])
            tr = max(
                float(highs[i]) - float(lows[i]),
                abs(float(highs[i]) - prev_c),
                abs(float(lows[i]) - prev_c),
            )
            trs.append(float(tr))
        first = sum(trs[:period]) / float(period)
        out[period] = float(first)
        atr_val = float(first)
        for i in range(period + 1, n):
            tr = float(trs[i - 1])
            atr_val = ((atr_val * (period - 1)) + tr) / float(period)
            out[i] = atr_val
        return out

    async def _render_fn(*, profile: RenderProfile | None = None):
        tz = delivery.ET_TZ or timezone.utc
        results: list[dict[str, object]] = []
        db_rows: list[tuple] = []
        source = "polygon"
        try:
            from delivery.on_demand_data import polygon_aggs

            agg = await asyncio.to_thread(polygon_aggs, sym_fetch, multiplier=1, timespan="day", days=260)
            results = agg.get("results") or []
        except Exception as exc:  # noqa: BLE001
            source = "db"
            try:
                db_rows = await asyncio.to_thread(delivery.get_last_n_bars, sym_fetch, "1d", 320, with_volume=True)
            except Exception:
                db_rows = []
            if not db_rows or len(db_rows) < 30:
                msg = str(exc)
                if "POLYGON_API_KEY" in msg:
                    return {"error": "HTF unavailable: missing market-data API key (set it in `.env.local`).", "ttl_sec": 6}
                return {"error": f"HTF unavailable: {exc}", "ttl_sec": 6}

        if source == "polygon" and (not results or len(results) < 30):
            return {"error": f"HTF unavailable for **{sym}**: need ~30 daily bars.", "ttl_sec": 8}
        if source == "db" and (not db_rows or len(db_rows) < 30):
            return {"error": f"HTF unavailable for **{sym}**: need ~30 daily bars.", "ttl_sec": 8}

        xs: list[datetime] = []
        opens: list[float] = []
        closes: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        vols: list[float] = []
        if source == "db":
            for row in db_rows:
                try:
                    ts = str(row[0] or "")
                    o = float(row[1])
                    h = float(row[2])
                    l = float(row[3])
                    c = float(row[4])
                    v = float(row[5]) if len(row) >= 6 and row[5] is not None else 0.0
                except Exception:
                    continue
                if c <= 0:
                    continue
                dt_utc = None
                try:
                    dt_utc = delivery.parse_iso(ts)
                except Exception:
                    try:
                        dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        dt_utc = None
                if dt_utc is None:
                    continue
                dt = dt_utc.astimezone(tz)
                xs.append(dt)
                opens.append(o if o > 0 else c)
                closes.append(c)
                highs.append(h if h > 0 else c)
                lows.append(l if l > 0 else c)
                vols.append(max(v, 0.0))
        else:
            for row in results:
                try:
                    t_ms = float(row.get("t"))
                    o = float(row.get("o"))
                    c = float(row.get("c"))
                    h = float(row.get("h"))
                    l = float(row.get("l"))
                    v = float(row.get("v") or 0.0)
                except Exception:
                    continue
                if c <= 0:
                    continue
                dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(tz)
                xs.append(dt)
                opens.append(o if o > 0 else c)
                closes.append(c)
                highs.append(h if h > 0 else c)
                lows.append(l if l > 0 else c)
                vols.append(v)

        if len(xs) < 30:
            return {"error": f"HTF unavailable for **{sym}**: insufficient daily bars.", "ttl_sec": 8}

        look = 140
        if len(xs) > look:
            xs_p = xs[-look:]
            opens_p = opens[-look:]
            closes_p = closes[-look:]
            highs_p = highs[-look:]
            lows_p = lows[-look:]
            vols_p = vols[-look:]
        else:
            xs_p, opens_p, closes_p, highs_p, lows_p, vols_p = xs, opens, closes, highs, lows, vols

        rsi14 = _rsi(closes, 14)
        stoch_k, stoch_d = _stoch(highs, lows, closes, 14, 3)
        atr14 = _atr(highs, lows, closes, 14)

        last_close = float(closes[-1])
        last_atr = None
        try:
            if atr14 and not math.isnan(float(atr14[-1])):
                last_atr = float(atr14[-1])
        except Exception:
            last_atr = None
        atr_pct = (float(last_atr) / float(last_close)) if (last_atr is not None and last_close > 0) else None

        rvol = None
        try:
            if len(vols) >= 22:
                base = vols[-21:-1]
            else:
                base = vols[:-1]
            base = [float(v) for v in base if isinstance(v, (int, float)) and float(v) > 0]
            if base:
                avg20 = sum(base[-20:]) / float(len(base[-20:]))
                if avg20 > 0:
                    rvol = float(vols[-1]) / float(avg20)
        except Exception:
            rvol = None

        weekly_bias = "UNKNOWN"
        try:
            week_close: dict[tuple[int, int], float] = {}
            for dt, c in zip(xs, closes):
                iso = dt.isocalendar()
                key = (int(iso[0]), int(iso[1]))
                week_close[key] = float(c)
            wk = [week_close[k] for k in sorted(week_close.keys())]
            if len(wk) >= 12:
                sma10 = sum(wk[-10:]) / 10.0
                if wk[-1] > sma10 * 1.01:
                    weekly_bias = "UP"
                elif wk[-1] < sma10 * 0.99:
                    weekly_bias = "DOWN"
                else:
                    weekly_bias = "FLAT"
        except Exception:
            weekly_bias = "UNKNOWN"

        lvl_hi20 = None
        lvl_lo20 = None
        sma20 = None
        sma50 = None
        try:
            if len(closes) >= 55:
                sma20 = float(_sma(closes, 20)[-1])
                sma50 = float(_sma(closes, 50)[-1])
            if len(highs) >= 21:
                lvl_hi20 = max(highs[-20:])
                lvl_lo20 = min(lows[-20:])
        except Exception:
            pass

        def _near(px: float, lvl: float | None) -> bool:
            if lvl is None or lvl <= 0:
                return False
            if last_atr is not None and last_atr > 0:
                return abs(float(px) - float(lvl)) <= float(last_atr) * 0.65
            return abs(float(px) - float(lvl)) <= max(float(lvl) * 0.012, 1.0)

        near_key = _near(last_close, lvl_hi20) or _near(last_close, lvl_lo20) or _near(last_close, sma50)

        last_rsi = None
        try:
            if rsi14 and not math.isnan(float(rsi14[-1])):
                last_rsi = float(rsi14[-1])
        except Exception:
            last_rsi = None

        k0 = d0 = k1 = d1 = None
        try:
            if len(stoch_k) >= 2 and len(stoch_d) >= 2:
                k0 = float(stoch_k[-2]) if not math.isnan(float(stoch_k[-2])) else None
                d0 = float(stoch_d[-2]) if not math.isnan(float(stoch_d[-2])) else None
                k1 = float(stoch_k[-1]) if not math.isnan(float(stoch_k[-1])) else None
                d1 = float(stoch_d[-1]) if not math.isnan(float(stoch_d[-1])) else None
        except Exception:
            k0 = d0 = k1 = d1 = None

        cross_up = bool(k0 is not None and d0 is not None and k1 is not None and d1 is not None and (k0 < d0) and (k1 > d1))
        cross_dn = bool(k0 is not None and d0 is not None and k1 is not None and d1 is not None and (k0 > d0) and (k1 < d1))
        stoch_os = bool(k1 is not None and k1 <= 22.0)
        stoch_ob = bool(k1 is not None and k1 >= 78.0)
        rsi_os = bool(last_rsi is not None and last_rsi <= 38.0)
        rsi_ob = bool(last_rsi is not None and last_rsi >= 62.0)

        vol_rising = False
        try:
            atr_pcts = []
            for a, c in zip(atr14[-30:], closes[-30:]):
                if a is None:
                    continue
                try:
                    av = float(a)
                    cv = float(c)
                    if cv > 0 and not math.isnan(av):
                        atr_pcts.append(av / cv)
                except Exception:
                    continue
            if atr_pct is not None and atr_pcts:
                baseline = sum(atr_pcts[-20:]) / float(len(atr_pcts[-20:]))
                if baseline > 0:
                    vol_rising = float(atr_pct) >= float(baseline) * 1.15
        except Exception:
            vol_rising = False

        state = "OFF"
        direction = "NEUTRAL"
        posture = "n/a"

        if near_key or vol_rising or (rvol is not None and rvol >= 1.25) or rsi_os or rsi_ob or stoch_os or stoch_ob:
            state = "WATCH"
            posture = "reduce size; wait for confirmation"

        if (rvol is not None and rvol >= 1.50) and ((cross_up and (stoch_os or rsi_os)) or (cross_dn and (stoch_ob or rsi_ob))):
            state = "ON"
            if cross_up:
                direction = "BULL"
                posture = "bullish reversal posture (defined-risk longs)"
            elif cross_dn:
                direction = "BEAR"
                posture = "bearish reversal posture (defined-risk shorts)"

        strength = 0.0
        try:
            if rvol is not None and state in {"WATCH", "ON"}:
                base = max(0.0, min(1.0, (float(rvol) - 1.0) / 1.0))
                mult = 1.0 if state == "ON" else 0.55
                sign = 1.0 if direction == "BULL" else (-1.0 if direction == "BEAR" else 0.0)
                strength = float(sign) * float(base) * float(mult)
        except Exception:
            strength = 0.0

        try:
            strength_min = float(os.getenv("TNT_HTF_STRENGTH_LABEL_MIN", "0.15"))
        except Exception:
            strength_min = 0.15
        show_strength = abs(float(strength)) >= float(strength_min)

        def _try_render_htf_png():
            try:
                import matplotlib

                matplotlib.use("Agg")
                _configure_matplotlib_speed_flags(matplotlib)
                import matplotlib.dates as mdates
                import matplotlib.pyplot as plt
                from matplotlib.patches import Rectangle
            except Exception:
                return None, "matplotlib_missing", None

            if show_strength:
                fig, (ax, ax_strip) = plt.subplots(
                    nrows=2,
                    ncols=1,
                    sharex=True,
                    figsize=(11.6, 6.9),
                    gridspec_kw={"height_ratios": [6.0, 0.55]},
                )
                fig.patch.set_facecolor("#0b0f14")
                for a in (ax, ax_strip):
                    a.set_facecolor("#0b0f14")
                    a.tick_params(colors="#c9d1d9")
                    for spine in a.spines.values():
                        spine.set_color("#2d333b")
            else:
                fig, ax = plt.subplots(figsize=(11.6, 6.2))
                ax_strip = None
                fig.patch.set_facecolor("#0b0f14")
                ax.set_facecolor("#0b0f14")
                ax.tick_params(colors="#c9d1d9")
                for spine in ax.spines.values():
                    spine.set_color("#2d333b")

            # HTF watermark should whisper, not compete with price action.
            _add_tnt_watermark(ax, alpha=0.025, fontsize=52)

            # Plot arrays (may be filtered for egregious outliers that can blow out y-limits).
            xs_plot = list(xs_p)
            opens_plot = list(opens_p)
            closes_plot = list(closes_p)
            highs_plot = list(highs_p)
            lows_plot = list(lows_p)

            # Drop extreme outliers (e.g., a single bad bar) so y-limits don't get dragged
            # to absurd ranges like ~100 when price is ~700.
            try:
                closes_clean = [float(c) for c in closes_plot if isinstance(c, (int, float)) and float(c) > 0 and (not math.isnan(float(c)))]
                if closes_clean and len(closes_clean) >= 30:
                    c_sorted = sorted(closes_clean)
                    med = float(c_sorted[len(c_sorted) // 2])
                    if med > 0:
                        lo_bound = med / 4.0
                        hi_bound = med * 4.0
                        keep = []
                        for i in range(len(closes_plot)):
                            try:
                                c = float(closes_plot[i])
                                h = float(highs_plot[i])
                                l = float(lows_plot[i])
                                o = float(opens_plot[i])
                            except Exception:
                                continue
                            if any(math.isnan(v) for v in (c, h, l, o)):
                                continue
                            if c <= 0 or h <= 0 or l <= 0:
                                continue
                            if (c < lo_bound) or (c > hi_bound) or (h < lo_bound) or (h > hi_bound) or (l < lo_bound) or (l > hi_bound):
                                continue
                            keep.append(i)
                        if len(keep) >= 30 and len(keep) < len(closes_plot):
                            xs_plot = [xs_plot[i] for i in keep]
                            opens_plot = [opens_plot[i] for i in keep]
                            closes_plot = [closes_plot[i] for i in keep]
                            highs_plot = [highs_plot[i] for i in keep]
                            lows_plot = [lows_plot[i] for i in keep]
            except Exception:
                pass

            xnums = mdates.date2num(xs_plot)
            dx = (xnums[1] - xnums[0]) if len(xnums) > 1 else 1.0
            w = max(dx * 0.75, 1e-6)
            up = "#00ff66"
            down = "#ff3344"
            wick = "#c9d1d9"

            for i in range(len(xnums)):
                o = float(opens_plot[i])
                c = float(closes_plot[i])
                h = float(highs_plot[i])
                l = float(lows_plot[i])
                col = up if c >= o else down
                ax.vlines(xnums[i], l, h, color=wick, linewidth=0.6, alpha=0.9)
                body_low = min(o, c)
                body_h = abs(c - o)
                if body_h <= 0:
                    body_h = max((max(closes_plot) - min(closes_plot)) * 0.0002, 1e-6)
                ax.add_patch(
                    Rectangle(
                        (xnums[i] - w / 2.0, body_low),
                        w,
                        body_h,
                        facecolor=col,
                        edgecolor=col,
                        linewidth=0.0,
                        alpha=0.95,
                        antialiased=False,
                    )
                )

            try:
                if len(closes_plot) >= 25:
                    s20 = _sma(closes_plot, 20)
                    ax.plot(xnums, s20, linewidth=1.05, color="#ffa657", alpha=0.95, label="SMA20")
                if len(closes_plot) >= 55:
                    s50 = _sma(closes_plot, 50)
                    ax.plot(xnums, s50, linewidth=1.05, color="#a371f7", alpha=0.95, label="SMA50")
            except Exception:
                pass

            # Compute y-bounds from the visible plotted candles.
            y_bounds = None
            y_pad = None
            try:
                lows_clean = [float(v) for v in lows_plot if isinstance(v, (int, float)) and float(v) > 0 and (not math.isnan(float(v)))]
                highs_clean = [float(v) for v in highs_plot if isinstance(v, (int, float)) and float(v) > 0 and (not math.isnan(float(v)))]
                if lows_clean and highs_clean:
                    lo = min(lows_clean)
                    hi = max(highs_clean)
                    pad = (hi - lo) * 0.05 if hi > lo else (hi * 0.01)
                    ymin = (lo - pad) if pad > 0 else (lo * 0.995)
                    ymax = (hi + pad) if pad > 0 else (hi * 1.005)
                    if ymax > ymin:
                        y_bounds = (float(ymin), float(ymax))
                        y_pad = float(pad)
            except Exception:
                y_bounds = None
                y_pad = None

            # Compute guide levels from the plotted window (not the full history).
            lvl_hi20_plot = None
            lvl_lo20_plot = None
            sma50_plot = None
            try:
                if len(highs_plot) >= 21:
                    lvl_hi20_plot = max(float(v) for v in highs_plot[-20:])
                if len(lows_plot) >= 21:
                    lvl_lo20_plot = min(float(v) for v in lows_plot[-20:])
                if len(closes_plot) >= 55:
                    sma50_plot = float(_sma(closes_plot, 50)[-1])
            except Exception:
                lvl_hi20_plot = None
                lvl_lo20_plot = None
                sma50_plot = None

            def _level_in_view(lvl: float | None) -> bool:
                if lvl is None:
                    return False
                try:
                    lv = float(lvl)
                except Exception:
                    return False
                if y_bounds is None:
                    return True
                ymin, ymax = float(y_bounds[0]), float(y_bounds[1])
                pad = float(y_pad) if (y_pad is not None and y_pad > 0) else (ymax - ymin) * 0.05
                # Only draw if within the visible band (+ generous margin).
                return (lv >= (ymin - 3.0 * pad)) and (lv <= (ymax + 3.0 * pad))

            if state in {"WATCH", "ON"}:
                for lvl, name in ((lvl_hi20_plot, "HI20"), (lvl_lo20_plot, "LO20"), (sma50_plot, "SMA50")):
                    if lvl is None:
                        continue
                    if not _level_in_view(lvl):
                        continue
                    ax.axhline(float(lvl), linewidth=0.9, alpha=0.22, color="#c9d1d9")
                    try:
                        ax.text(
                            0.01,
                            float(lvl),
                            f" {name}",
                            color="#c9d1d9",
                            alpha=0.6,
                            fontsize=8,
                            transform=ax.get_yaxis_transform(),
                            va="center",
                        )
                    except Exception:
                        pass

            if state == "ON":
                try:
                    x_last = xnums[-1]
                    y_last = float(closes_plot[-1])
                    if direction == "BULL":
                        ax.scatter([x_last], [y_last], s=46, color=up, zorder=6)
                        ax.text(x_last, y_last, "  ▲", color=up, fontsize=12, va="center")
                    elif direction == "BEAR":
                        ax.scatter([x_last], [y_last], s=46, color=down, zorder=6)
                        ax.text(x_last, y_last, "  ▼", color=down, fontsize=12, va="center")
                except Exception:
                    pass

            # Apply tight y-axis bounds now (and again later after legends/layout).
            try:
                if y_bounds is not None:
                    ax.set_ylim(float(y_bounds[0]), float(y_bounds[1]))
            except Exception:
                pass

            ax.grid(True, alpha=0.14, linestyle="--")
            ax.yaxis.tick_right()
            ax.yaxis.set_label_position("right")
            ax.set_ylabel("Price", color="#c9d1d9")
            ax.set_title(f"{sym} — TNT HTF Reversal Context", color="#c9d1d9")

            try:
                # Reasoned state badge.
                badge_state = "ACTIVE" if state == "ON" else str(state)
                reason = ""
                if state == "WATCH":
                    if bool(vol_rising):
                        reason = "Range Expansion Risk"
                    elif bool(near_key):
                        reason = "Compression"
                    elif bool(rsi_os or rsi_ob or stoch_os or stoch_ob):
                        reason = "Momentum Extremes"
                    elif (rvol is not None and float(rvol) >= 1.25):
                        reason = "Volume Spike"
                    else:
                        reason = "Setup Forming"
                elif state == "ON":
                    reason = "Trend Resuming"

                badge = f"HTF: {badge_state}"
                if reason:
                    badge = badge + f" ({reason})"
                if state == "ON" and direction in {"BULL", "BEAR"}:
                    badge = badge + f" \u2022 {direction}"
                ax.text(
                    0.985,
                    0.03,
                    badge,
                    transform=ax.transAxes,
                    ha="right",
                    va="bottom",
                    fontsize=10,
                    color="#c9d1d9",
                    bbox={"facecolor": "#0b0f14", "edgecolor": "#2d333b", "alpha": 0.85, "pad": 3.0},
                )
            except Exception:
                pass

            if ax_strip is not None:
                ax_strip.set_ylim(0, 1)
                ax_strip.set_yticks([])
                ax_strip.grid(False)
                ax_strip.set_ylabel("", color="#c9d1d9")
                ax_strip.set_xlabel("ET", color="#c9d1d9")
                ax_strip.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
                ax_strip.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))
                try:
                    col = up if strength >= 0 else down
                    ax_strip.add_patch(
                        Rectangle(
                            (0.0, 0.0),
                            1.0,
                            1.0,
                            transform=ax_strip.transAxes,
                            facecolor=col,
                            edgecolor="none",
                            alpha=min(0.65, 0.10 + abs(float(strength)) * 0.65),
                            zorder=0.2,
                        )
                    )
                    ax_strip.text(
                        0.01,
                        0.5,
                        f"Strength: {strength:+.2f}",
                        transform=ax_strip.transAxes,
                        ha="left",
                        va="center",
                        fontsize=8,
                        color="#c9d1d9",
                    )
                except Exception:
                    pass
            else:
                ax.set_xlabel("ET", color="#c9d1d9")
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=10))

            handles, labels = ax.get_legend_handles_labels()
            if labels:
                ax.legend(loc="upper left", fontsize=8, framealpha=0.15, ncol=2)

            # Lock y-axis *after* all artists/legend to prevent any late autoscale from
            # off-window level lines causing massive dead space.
            try:
                if y_bounds is not None:
                    ax.set_autoscale_on(False)
                    ax.autoscale(enable=False, axis="y")
                    ax.set_ylim(float(y_bounds[0]), float(y_bounds[1]))
            except Exception:
                pass

            fig.tight_layout()
            buf = io.BytesIO()
            dpi_used = None
            try:
                if profile is not None:
                    dpi_used = int(profile.dpi)
            except Exception:
                dpi_used = None
            if dpi_used is None or dpi_used <= 0:
                dpi_used = 180
            fig.savefig(buf, format="png", dpi=int(dpi_used), facecolor=fig.get_facecolor(), metadata=_tnt_png_metadata())
            plt.close(fig)

            meta = {
                "state": state,
                "direction": direction,
                "posture": posture,
                "rvol": float(rvol) if rvol is not None else None,
                "atr_pct": float(atr_pct) if atr_pct is not None else None,
                "rsi": float(last_rsi) if last_rsi is not None else None,
                "stoch_k": float(k1) if k1 is not None else None,
                "stoch_d": float(d1) if d1 is not None else None,
                "weekly_bias": weekly_bias,
                "near_key": bool(near_key),
                "vol_rising": bool(vol_rising),
            }
            return buf.getvalue(), None, meta

        png_bytes, png_err, meta = await asyncio.to_thread(_try_render_htf_png)
        if not png_bytes:
            return {"error": f"HTF chart unavailable: {png_err}.", "ttl_sec": 6}

        bits: list[str] = []
        bits.append(f"🧠 **TNT HTF Reversal Context (Daily/Weekly)** — **{sym}**")
        bits.append(f"State: **{state}**{f' ({direction})' if state == 'ON' else ''} | Weekly: **{weekly_bias}**")
        if rvol is not None or atr_pct is not None:
            rv_txt = f"RVOL: **{float(rvol):.2f}**" if rvol is not None else "RVOL: n/a"
            atr_txt = f"ATR%: **{float(atr_pct)*100.0:.2f}%**" if atr_pct is not None else "ATR%: n/a"
            bits.append(f"{rv_txt} | {atr_txt}")
        if state in {"WATCH", "ON"}:
            bits.append(f"Posture: _{posture}_")

        footer = (str(FOOTER_DISCLAIMER) if FOOTER_DISCLAIMER is not None else "").replace("\r\n", " ").replace("\n", " ").strip()
        if footer:
            bits.append(f"_{footer}_")

        return {
            "png_bytes": png_bytes,
            "png_err": None,
            "meta": meta if isinstance(meta, dict) else {},
            "caption": "\n".join([b for b in bits if b and str(b).strip()]),
            "filename": f"{sym.lower()}_htf.png",
            "ttl_sec": int(ttl_sec),
        }

    await run_heavy_chart(
        interaction=interaction,
        cmd="htf",
        cache_key=cache_key,
        render_fn=_render_fn,
        cache_get=_cache_get,
        cache_set=_cache_set,
        post_to_channel=True,
    )


@bot.tree.command(name="gex", description="Premium: gamma exposure (GEX) chart with zero-gamma level.")
@app_commands.describe(
    symbol="Underlying ticker (e.g., SPY, SPX, QQQ)",
    expiration="Expiration YYYY-MM-DD (default: today ET)",
)
async def gex(
    interaction: discord.Interaction,
    symbol: str,
    expiration: str = "",
) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.NotFound:
        return
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except discord.NotFound:
            return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.NotFound:
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                try:
                    await interaction.followup.send(message, ephemeral=True)
                    responded = True
                    return
                except Exception:
                    return
            try:
                if interaction.channel is not None:
                    await interaction.channel.send(message)
            except Exception:
                pass
            return

    sym_norm = delivery._normalize_symbol_token(symbol)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like SPY, QQQ, or SPX.")
        return
    sym = sym_norm

    try:
        if _gprr_enabled() and _GPRR is not None:
            _GPRR.note_command(cmd="oi", symbol=sym)
    except Exception:
        pass

    # Soft per-user cooldown (options-heavy family)
    try:
        cooldown = _cooldown_effective_sec("heavy")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="heavy",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="gex", bucket="heavy", wait_s=wait_s)
            return
    except Exception:
        pass

    api_key, _base_url, provider = delivery._polygon_key_and_base()
    if not api_key:
        await _reply("GEX unavailable: missing market-data API key (set it in `.env.local`).")
        return

    exp_clean = (expiration or "").strip()
    if exp_clean:
        if len(exp_clean) != 10 or exp_clean[4] != "-" or exp_clean[7] != "-":
            await _reply("Expiration must be `YYYY-MM-DD` (example: `2025-12-27`).")
            return

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    strike_window_pct = 0.08
    max_contracts = 250
    exp_label = exp_clean or delivery._now_et().date().isoformat()

    async def _probe_options_access(*, exp: str) -> tuple[int | None, int | None, str | None]:
        import aiohttp

        underlying = delivery._map_underlying_for_options(sym)
        params_contracts: dict[str, object] = {
            "underlying_ticker": underlying,
            "expiration_date": exp,
            "limit": 1,
            "apiKey": api_key,
        }

        base_url = delivery._polygon_key_and_base()[1]
        url_contracts = f"{base_url}/v3/reference/options/contracts"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url_contracts, params=params_contracts) as resp:
                    contracts_status = int(resp.status)
                    payload = None
                    try:
                        payload = await resp.json()
                    except Exception:
                        payload = None

                    if contracts_status != 200:
                        detail = None
                        if isinstance(payload, dict):
                            detail = str(payload.get("error") or payload.get("message") or payload.get("status") or "")
                        return contracts_status, None, (detail or None)

                    ticker = None
                    if isinstance(payload, dict) and isinstance(payload.get("results"), list) and payload["results"]:
                        first = payload["results"][0]
                        if isinstance(first, dict):
                            ticker = str(first.get("ticker") or "").strip()

                    if not ticker:
                        return contracts_status, None, None

                url_snap = f"{base_url}/v3/snapshot/options/{underlying}/{ticker}"
                async with session.get(url_snap, params={"apiKey": api_key}) as resp2:
                    snap_status = int(resp2.status)
                    if snap_status == 200:
                        return contracts_status, snap_status, None
                    detail = None
                    try:
                        snap_payload = await resp2.json()
                        if isinstance(snap_payload, dict):
                            detail = str(snap_payload.get("error") or snap_payload.get("message") or snap_payload.get("status") or "")
                    except Exception:
                        detail = None
                    return contracts_status, snap_status, (detail or None)
        except Exception:
            return None, None, None

    def _format_footer() -> str:
        return (str(FOOTER_DISCLAIMER) if FOOTER_DISCLAIMER is not None else "").replace("\r\n", " ").replace("\n", " ").strip()

    def _compute_zero_gamma_level(strikes: list[float], net_gex: list[float]) -> float | None:
        if not strikes or not net_gex or len(strikes) != len(net_gex):
            return None
        try:
            cum: list[float] = []
            s = 0.0
            for v in net_gex:
                s += float(v)
                cum.append(s)
        except Exception:
            return None

        for i in range(1, len(cum)):
            a = float(cum[i - 1])
            b = float(cum[i])
            if a == 0.0:
                try:
                    return float(strikes[i - 1])
                except Exception:
                    return None
            if a * b < 0.0:
                try:
                    x0 = float(strikes[i - 1])
                    x1 = float(strikes[i])
                    if x1 == x0:
                        return x0
                    t = abs(a) / (abs(a) + abs(b))
                    return x0 + (x1 - x0) * float(t)
                except Exception:
                    return None
        return None

    def _try_render_gex_png(
        df,
        *,
        profile: RenderProfile | None = None,
        dpi: int | None = None,
        **_kwargs,
    ) -> tuple[bytes | None, str | None, dict[str, object] | None]:
        try:
            import math
            import pandas as pd

            import matplotlib

            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
        except Exception:
            return None, "render_error", None

        if df is None or getattr(df, "empty", True):
            return None, "no_chain", None

        work = df.copy()
        work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
        work["open_interest"] = pd.to_numeric(work.get("open_interest"), errors="coerce")
        work["gamma"] = pd.to_numeric(work.get("gamma"), errors="coerce")
        if "underlying_price" in work.columns:
            work["underlying_price"] = pd.to_numeric(work.get("underlying_price"), errors="coerce")

        work = work.dropna(subset=["strike"]).copy()
        if work.empty:
            return None, "no_strikes", None

        # Best-effort spot.
        spot_px = None
        try:
            if "underlying_price" in work.columns:
                vals = [float(v) for v in work["underlying_price"].dropna().tolist() if isinstance(v, (int, float))]
                if vals:
                    vals.sort()
                    spot_px = float(vals[len(vals) // 2])
        except Exception:
            spot_px = None

        # Bucket strikes to reduce clutter.
        sym_up = (sym or "").strip().upper()
        bucket = 5.0 if sym_up in {"SPX", "SPXW"} else 1.0
        try:
            median_strike = float(work["strike"].median())
            if median_strike >= 1000.0:
                bucket = max(bucket, 5.0)
        except Exception:
            pass

        work["type"] = work.get("type")
        work["type"] = work["type"].astype(str).str.lower()
        oi_raw = work["open_interest"].fillna(0.0)
        work["oi_pos"] = oi_raw.where(oi_raw > 0.0, 0.0)
        gamma_raw = work["gamma"].fillna(0.0)

        # Conventional scale to bring values into a readable range: per 1% move with 100x multiplier.
        scale = 1.0
        if isinstance(spot_px, (int, float)) and spot_px and spot_px > 0:
            scale = float(spot_px) * float(spot_px) * 0.01 * 100.0

        # Guard: if gamma is entirely missing/zero, call it out.
        try:
            if float(pd.to_numeric(work.get("gamma"), errors="coerce").fillna(0.0).abs().sum()) <= 0.0:
                return None, "gamma_missing", {"spot": spot_px, "bucket": bucket}
        except Exception:
            pass

        work["strike_bucket"] = (work["strike"] / float(bucket)).round() * float(bucket)
        work["strike_bucket"] = pd.to_numeric(work["strike_bucket"], errors="coerce")
        work = work.dropna(subset=["strike_bucket"]).copy()
        if work.empty:
            return None, "no_buckets", {"spot": spot_px, "bucket": bucket}

        work["gex_unit"] = (gamma_raw.abs() * work["oi_pos"]).astype(float) * float(scale)
        work["gex_signed"] = work["gex_unit"]
        work.loc[work["type"].eq("put"), "gex_signed"] = -work.loc[work["type"].eq("put"), "gex_unit"]
        work.loc[~work["type"].isin(["call", "put"]), "gex_signed"] = 0.0

        strikes: list[float] = []
        net: list[float] = []
        for strike_b, grp in work.groupby("strike_bucket"):
            try:
                strike_f = float(strike_b)
            except Exception:
                continue
            call_mask = grp["type"].eq("call")
            put_mask = grp["type"].eq("put")
            call_g = float(grp.loc[call_mask, "gex_unit"].sum()) if bool(call_mask.any()) else 0.0
            put_g = float(grp.loc[put_mask, "gex_unit"].sum()) if bool(put_mask.any()) else 0.0
            strikes.append(strike_f)
            net.append(call_g - put_g)

        if not strikes:
            return None, "no_data", {"spot": spot_px, "bucket": bucket}

        order = sorted(range(len(strikes)), key=lambda i: strikes[i])
        strikes = [strikes[i] for i in order]
        net = [net[i] for i in order]

        zero_gamma = _compute_zero_gamma_level(strikes, net)

        xs = list(range(len(strikes)))
        labels = [f"{s:g}" for s in strikes]
        colors = ["#00ff66" if v >= 0 else "#ff3344" for v in net]

        fig, ax = plt.subplots(figsize=(11.25, 5.4))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        ax.bar(xs, net, color=colors, alpha=0.48, label="Net GEX (calls - puts)")
        ax.axhline(0.0, color="#c9d1d9", linewidth=1.0, alpha=0.20)
        ax.set_ylabel("Net GEX (scaled)", color="#c9d1d9")
        ax.grid(True, alpha=0.12, linestyle="--")

        # Spot marker (nearest strike bucket).
        if isinstance(spot_px, (int, float)) and spot_px and strikes:
            try:
                spot_idx = int(min(range(len(strikes)), key=lambda i: abs(float(strikes[i]) - float(spot_px))))
                ax.axvline(spot_idx, color="#c9d1d9", linewidth=1.0, alpha=0.30, linestyle="--", label=f"Spot {spot_px:.2f}")
            except Exception:
                pass

        # Zero gamma marker (interpolated strike level).
        if isinstance(zero_gamma, (int, float)) and strikes:
            try:
                zg_idx = float(min(range(len(strikes)), key=lambda i: abs(float(strikes[i]) - float(zero_gamma))))
                ax.axvline(zg_idx, color="#ffa657", linewidth=1.2, alpha=0.70, linestyle="--", label=f"Zero γ {float(zero_gamma):.2f}")
            except Exception:
                pass

        window_pct = int(float(strike_window_pct) * 100)
        ax.set_title(f"{sym} — Gamma Exposure (GEX) | exp {exp_label} | ±{window_pct}% window", color="#c9d1d9")
        ax.set_xlabel("Strike", color="#c9d1d9")

        n = len(labels)
        max_ticks = 18
        step = max(1, int(math.ceil(float(n) / float(max_ticks))))
        tick_idx = list(range(0, n, step))
        if (n - 1) not in tick_idx:
            tick_idx.append(n - 1)
        ax.set_xticks(tick_idx)
        ax.set_xticklabels([labels[i] for i in tick_idx], rotation=45, ha="right", color="#c9d1d9", fontsize=8)

        try:
            ax.legend(loc="upper left", fontsize=8, framealpha=0.15)
        except Exception:
            pass

        dpi_used = None
        try:
            if dpi is not None:
                dpi_used = int(dpi)
            elif profile is not None:
                dpi_used = int(profile.dpi)
        except Exception:
            dpi_used = None
        if dpi_used is None or dpi_used <= 0:
            dpi_used = 150

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=int(dpi_used), metadata=_tnt_png_metadata())
        plt.close(fig)

        meta = {
            "spot": float(spot_px) if isinstance(spot_px, (int, float)) else None,
            "zero_gamma": float(zero_gamma) if isinstance(zero_gamma, (int, float)) else None,
            "bucket": float(bucket),
            "contracts": int(len(work)),
        }
        return buf.getvalue(), None, meta

    ttl_sec = max(int(os.getenv("TNT_TTL_GEX_PNG_SEC", "60")), 10)
    prof = _gprr_profile() if _gprr_enabled() else None
    cache_key_base = f"gex_png:v1:{sym}:{exp_label}:{int(strike_window_pct*100)}:{max_contracts}"
    cache_key = _gprr_cache_key(cache_key_base)

    async def _cache_get(_key: str):
        cached = await _png_cache_get(_key, family="gex")
        if cached is None:
            return None
        png_bytes, png_err, meta = cached
        if not png_bytes or png_err:
            return None
        cached_ts = None
        caption = None
        filename = None
        if isinstance(meta, dict):
            cached_ts = meta.get("__tnt_cached_ts")
            caption = meta.get("caption")
            filename = meta.get("filename")
        if not caption:
            caption = f"🧲 **Gamma Exposure (GEX)** — **{sym}** | exp **{exp_label}** | _cached_"
        if not filename:
            filename = f"{sym.lower()}_gex.png"
        return {"png_bytes": png_bytes, "caption": caption, "filename": filename, "cached_asof_ts": cached_ts, "meta": meta}

    async def _cache_set(_key: str, result: dict[str, object]) -> None:
        try:
            png_bytes = result.get("png_bytes")
            png_err = result.get("png_err")
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            meta = dict(meta)
            meta.setdefault("caption", result.get("caption"))
            meta.setdefault("filename", result.get("filename"))
            ttl = int(result.get("ttl_sec") or ttl_sec)
            await _png_cache_set(_key, (png_bytes, png_err, meta), ttl)
        except Exception:
            pass

    async def _render_fn(*, profile: RenderProfile | None = None):
        df = await delivery._fetch_polygon_options_chain_df(
            sym,
            expiration_ymd=exp_label,
            strike_window_pct=strike_window_pct,
            max_contracts=max_contracts,
            concurrency=8,
        )
        if df is None or getattr(df, "empty", True):
            contracts_status, snap_status, detail = await _probe_options_access(exp=exp_label)
            extra = f" ({detail})" if detail else ""
            if contracts_status in {401, 403}:
                return {
                    "error": (
                        f"GEX unavailable for **{sym}**: options contracts endpoint returned HTTP **{contracts_status}**{extra}. "
                        "This usually means the API key plan doesn’t include options access."
                    ),
                    "ttl_sec": 3,
                }
            if contracts_status == 429 or snap_status == 429:
                return {"error": f"GEX temporarily unavailable for **{sym}**: rate limited (HTTP **429**). Try again shortly.", "ttl_sec": 3}
            if snap_status in {401, 403}:
                return {
                    "error": (
                        f"GEX unavailable for **{sym}**: options snapshot/greeks endpoint returned HTTP **{snap_status}**{extra}. "
                        "Contracts lookup may work, but greeks require options snapshot access on your plan."
                    ),
                    "ttl_sec": 3,
                }
            return {"error": f"No options chain data returned for **{sym}** ({provider}) on **{exp_label}**.", "ttl_sec": 3}

        png_bytes, png_err, meta = await asyncio.to_thread(lambda: _try_render_gex_png(df, profile=profile))
        if not png_bytes:
            if png_err == "gamma_missing":
                return {
                    "error": f"GEX unavailable for **{sym}**: options greeks (gamma) not returned for exp **{exp_label}**.",
                    "ttl_sec": 8,
                }
            return {"error": f"GEX unavailable: {png_err}.", "ttl_sec": 5}

        footer = _format_footer()
        text = f"🧲 **Gamma Exposure (GEX)** — **{sym}** | exp **{exp_label}** | bounded chain (max {max_contracts})"
        if isinstance(meta, dict):
            try:
                spot = meta.get("spot")
                zg = meta.get("zero_gamma")
                if isinstance(spot, (int, float)):
                    text = text + f"\nSpot: **{float(spot):.2f}**"
                if isinstance(zg, (int, float)):
                    text = text + f" | Zero γ: **{float(zg):.2f}**"
            except Exception:
                pass
        if footer:
            text = f"{text}\n_{footer}_"

        try:
            macro_line = await _get_macro_regime_line()
            if macro_line:
                text = text + "\n" + macro_line
                text = await _maybe_append_crypto_context_if_macro(text, macro_line, sep="\n")
        except Exception:
            pass

        return {
            "png_bytes": png_bytes,
            "png_err": None,
            "meta": meta if isinstance(meta, dict) else {},
            "caption": text,
            "filename": f"{sym.lower()}_gex.png",
            "ttl_sec": int(ttl_sec),
        }

    await run_heavy_chart(
        interaction=interaction,
        cmd="gex",
        cache_key=cache_key,
        render_fn=_render_fn,
        cache_get=_cache_get,
        cache_set=_cache_set,
    )


@bot.tree.command(name="ddp", description="Dealer Delta Positioning: net hedge delta by strike (simplified).")
@app_commands.describe(
    symbol="Underlying ticker (e.g., SPY, SPX, QQQ)",
    expiration="Expiration YYYY-MM-DD (default: today ET)",
)
async def ddp(
    interaction: discord.Interaction,
    symbol: str,
    expiration: str = "",
) -> None:
    responded = False
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.NotFound:
        return
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if getattr(exc, "code", None) == 40060:
            responded = True
        else:
            responded = interaction.response.is_done()
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    async def _reply(message: str) -> None:
        nonlocal responded
        try:
            if responded or interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
                return
        except discord.NotFound:
            return
        except Exception:
            pass

        try:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True
            return
        except discord.NotFound:
            return
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            if getattr(exc, "code", None) == 40060:
                try:
                    await interaction.followup.send(message, ephemeral=True)
                    responded = True
                    return
                except Exception:
                    return
            try:
                if interaction.channel is not None:
                    await interaction.channel.send(message)
            except Exception:
                pass
            return

    sym_norm = delivery._normalize_symbol_token(symbol)
    if not sym_norm:
        await _reply("Invalid symbol. Try something like SPY, QQQ, or SPX.")
        return
    sym = sym_norm

    prof = _gprr_profile()
    try:
        if _GPRR is not None:
            _GPRR.note_command(cmd="ddp", symbol=sym, heavy=True)
    except Exception:
        pass

    # Soft per-user cooldown (options-heavy family)
    try:
        cooldown = _cooldown_effective_sec("heavy")
        ok, wait_s = await _check_user_cooldown(
            int(getattr(getattr(interaction, "user", None), "id", 0) or 0),
            bucket="heavy",
            cooldown_sec=cooldown,
        )
        if not ok:
            await _send_cooldown_notice(interaction, family="ddp", bucket="heavy", wait_s=wait_s)
            return
    except Exception:
        pass

    api_key, _base_url, provider = delivery._polygon_key_and_base()
    if not api_key:
        await _reply("DDP unavailable: missing market-data API key (set it in `.env.local`).")
        return

    exp_clean = (expiration or "").strip()
    if exp_clean:
        if len(exp_clean) != 10 or exp_clean[4] != "-" or exp_clean[7] != "-":
            await _reply("Expiration must be `YYYY-MM-DD` (example: `2025-12-27`).")
            return

    channel = interaction.channel
    if channel is None:
        await _reply("Could not resolve channel.")
        return

    strike_window_pct = 0.08
    max_contracts = 250
    exp_label = exp_clean or delivery._now_et().date().isoformat()

    async def _probe_options_access(*, exp: str) -> tuple[int | None, int | None, str | None]:
        import aiohttp

        underlying = delivery._map_underlying_for_options(sym)
        params_contracts: dict[str, object] = {
            "underlying_ticker": underlying,
            "expiration_date": exp,
            "limit": 1,
            "apiKey": api_key,
        }

        base_url = delivery._polygon_key_and_base()[1]
        url_contracts = f"{base_url}/v3/reference/options/contracts"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url_contracts, params=params_contracts) as resp:
                    contracts_status = int(resp.status)
                    payload = None
                    try:
                        payload = await resp.json()
                    except Exception:
                        payload = None

                    if contracts_status != 200:
                        detail = None
                        if isinstance(payload, dict):
                            detail = str(payload.get("error") or payload.get("message") or payload.get("status") or "")
                        return contracts_status, None, (detail or None)

                    ticker = None
                    if isinstance(payload, dict) and isinstance(payload.get("results"), list) and payload["results"]:
                        first = payload["results"][0]
                        if isinstance(first, dict):
                            ticker = str(first.get("ticker") or "").strip()

                    if not ticker:
                        return contracts_status, None, None

                url_snap = f"{base_url}/v3/snapshot/options/{underlying}/{ticker}"
                async with session.get(url_snap, params={"apiKey": api_key}) as resp2:
                    snap_status = int(resp2.status)
                    if snap_status == 200:
                        return contracts_status, snap_status, None
                    detail = None
                    try:
                        snap_payload = await resp2.json()
                        if isinstance(snap_payload, dict):
                            detail = str(snap_payload.get("error") or snap_payload.get("message") or snap_payload.get("status") or "")
                    except Exception:
                        detail = None
                    return contracts_status, snap_status, (detail or None)
        except Exception:
            return None, None, None

    def _format_footer() -> str:
        return (str(FOOTER_DISCLAIMER) if FOOTER_DISCLAIMER is not None else "").replace("\r\n", " ").replace("\n", " ").strip()

    def _compute_zero_cross_level(strikes: list[float], per_strike: list[float]) -> float | None:
        if not strikes or not per_strike or len(strikes) != len(per_strike):
            return None
        try:
            cum: list[float] = []
            s = 0.0
            for v in per_strike:
                s += float(v)
                cum.append(s)
        except Exception:
            return None

        for i in range(1, len(cum)):
            a = float(cum[i - 1])
            b = float(cum[i])
            if a == 0.0:
                try:
                    return float(strikes[i - 1])
                except Exception:
                    return None
            if a * b < 0.0:
                try:
                    x0 = float(strikes[i - 1])
                    x1 = float(strikes[i])
                    if x1 == x0:
                        return x0
                    t = abs(a) / (abs(a) + abs(b))
                    return x0 + (x1 - x0) * float(t)
                except Exception:
                    return None
        return None

    def _try_render_ddp_png(df) -> tuple[bytes | None, str | None, dict[str, object] | None]:
        try:
            import math
            import pandas as pd

            import matplotlib

            matplotlib.use("Agg")
            _configure_matplotlib_speed_flags(matplotlib)
            import matplotlib.pyplot as plt
        except Exception:
            return None, "render_error", None

        if df is None or getattr(df, "empty", True):
            return None, "no_chain", None

        work = df.copy()
        work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
        work["open_interest"] = pd.to_numeric(work.get("open_interest"), errors="coerce")
        work["delta"] = pd.to_numeric(work.get("delta"), errors="coerce")
        if "underlying_price" in work.columns:
            work["underlying_price"] = pd.to_numeric(work.get("underlying_price"), errors="coerce")

        work = work.dropna(subset=["strike"]).copy()
        if work.empty:
            return None, "no_strikes", None

        # Best-effort spot.
        spot_px = None
        try:
            if "underlying_price" in work.columns:
                vals = [float(v) for v in work["underlying_price"].dropna().tolist() if isinstance(v, (int, float))]
                if vals:
                    vals.sort()
                    spot_px = float(vals[len(vals) // 2])
        except Exception:
            spot_px = None

        # Bucket strikes to reduce clutter.
        sym_up = (sym or "").strip().upper()
        bucket = 5.0 if sym_up in {"SPX", "SPXW"} else 1.0
        try:
            median_strike = float(work["strike"].median())
            if median_strike >= 1000.0:
                bucket = max(bucket, 5.0)
        except Exception:
            pass

        work["type"] = work.get("type")
        work["type"] = work["type"].astype(str).str.lower()
        oi_raw = work["open_interest"].fillna(0.0)
        work["oi_pos"] = oi_raw.where(oi_raw > 0.0, 0.0)
        delta_raw = work["delta"].fillna(0.0)

        # Guard: if delta is entirely missing/zero, call it out.
        try:
            if float(pd.to_numeric(work.get("delta"), errors="coerce").fillna(0.0).abs().sum()) <= 0.0:
                return None, "delta_missing", {"spot": spot_px, "bucket": bucket}
        except Exception:
            pass

        work["strike_bucket"] = (work["strike"] / float(bucket)).round() * float(bucket)
        work["strike_bucket"] = pd.to_numeric(work["strike_bucket"], errors="coerce")
        work = work.dropna(subset=["strike_bucket"]).copy()
        if work.empty:
            return None, "no_buckets", {"spot": spot_px, "bucket": bucket}

        # Simplified convention:
        # - Option delta exposure (shares) ~= delta * OI * 100.
        # - Assume dealers are short customer options => dealer hedge delta ~= -exposure.
        work["delta_shares"] = (delta_raw * work["oi_pos"] * 100.0).astype(float)
        work["dealer_hedge_shares"] = (-1.0 * work["delta_shares"]).astype(float)

        strikes: list[float] = []
        hedge: list[float] = []
        for strike_b, grp in work.groupby("strike_bucket"):
            try:
                strike_f = float(strike_b)
            except Exception:
                continue
            v = float(grp["dealer_hedge_shares"].sum())
            strikes.append(strike_f)
            hedge.append(v)

        if not strikes:
            return None, "no_data", {"spot": spot_px, "bucket": bucket}

        order = sorted(range(len(strikes)), key=lambda i: strikes[i])
        strikes = [strikes[i] for i in order]
        hedge = [hedge[i] for i in order]

        # Cumulative hedge curve + approximate zero-cross strike.
        cum = []
        s = 0.0
        for v in hedge:
            s += float(v)
            cum.append(s)
        zero_delta = _compute_zero_cross_level(strikes, hedge)

        xs = list(range(len(strikes)))
        labels = [f"{s:g}" for s in strikes]
        colors = ["#00ff66" if v >= 0 else "#ff3344" for v in hedge]

        fig, ax = plt.subplots(figsize=(11.25, 5.4))
        fig.patch.set_facecolor("#0b0f14")
        ax.set_facecolor("#0b0f14")
        ax.tick_params(colors="#c9d1d9")
        for spine in ax.spines.values():
            spine.set_color("#2d333b")

        _add_tnt_watermark(ax)

        ax.bar(xs, hedge, color=colors, alpha=0.48, label="Dealer hedge Δ (shares, simplified)")
        ax.axhline(0.0, color="#c9d1d9", linewidth=1.0, alpha=0.20)
        ax.set_ylabel("Dealer hedge Δ (shares)", color="#c9d1d9")
        ax.grid(True, alpha=0.12, linestyle="--")
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")

        ax2 = ax.twinx()
        ax2.set_facecolor("#0b0f14")
        ax2.tick_params(colors="#c9d1d9")
        for spine in ax2.spines.values():
            spine.set_color("#2d333b")
        ax2.plot(xs, cum, color="#ffa657", linewidth=1.6, alpha=0.95, label="Cumulative hedge")
        ax2.set_ylabel("Cumulative hedge", color="#c9d1d9")

        # Spot marker (nearest strike bucket).
        if isinstance(spot_px, (int, float)) and spot_px and strikes:
            try:
                spot_idx = int(min(range(len(strikes)), key=lambda i: abs(float(strikes[i]) - float(spot_px))))
                ax.axvline(spot_idx, color="#c9d1d9", linewidth=1.0, alpha=0.30, linestyle="--", label=f"Spot {spot_px:.2f}")
            except Exception:
                pass

        # Zero-cross marker (approx magnet).
        if isinstance(zero_delta, (int, float)) and strikes:
            try:
                zd_idx = float(min(range(len(strikes)), key=lambda i: abs(float(strikes[i]) - float(zero_delta))))
                ax.axvline(zd_idx, color="#ffa657", linewidth=1.2, alpha=0.70, linestyle="--", label=f"Zero Δ {float(zero_delta):.2f}")
            except Exception:
                pass

        window_pct = int(float(strike_window_pct) * 100)
        ax.set_title(f"{sym} — Dealer Delta Positioning (DDP) | exp {exp_label} | ±{window_pct}% window", color="#c9d1d9")
        ax.set_xlabel("Strike", color="#c9d1d9")

        n = len(labels)
        max_ticks = 18
        step = max(1, int(math.ceil(float(n) / float(max_ticks))))
        tick_idx = list(range(0, n, step))
        if (n - 1) not in tick_idx:
            tick_idx.append(n - 1)
        ax.set_xticks(tick_idx)
        ax.set_xticklabels([labels[i] for i in tick_idx], rotation=45, ha="right", color="#c9d1d9", fontsize=8)

        try:
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            if l1 or l2:
                ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, framealpha=0.15)
        except Exception:
            pass

        fig.tight_layout()
        buf = io.BytesIO()
        dpi = 150
        try:
            if prof is not None:
                dpi = max(60, int(getattr(prof, "dpi", dpi) or dpi))
        except Exception:
            dpi = 150
        fig.savefig(buf, format="png", dpi=dpi, metadata=_tnt_png_metadata())
        plt.close(fig)

        meta = {
            "spot": float(spot_px) if isinstance(spot_px, (int, float)) else None,
            "zero_delta": float(zero_delta) if isinstance(zero_delta, (int, float)) else None,
            "bucket": float(bucket),
            "contracts": int(len(work)),
        }
        return buf.getvalue(), None, meta

    ttl_sec = max(int(os.getenv("TNT_TTL_DDP_PNG_SEC", "60")), 10)
    cache_key = f"ddp_png:v1:{sym}:{exp_label}:{int(strike_window_pct*100)}:{max_contracts}"

    cache_key_png_full = _gprr_cache_key(cache_key)

    async def _cache_get(_key: str):
        cached = await _png_cache_get(_key, family="ddp")
        if cached is None:
            return None
        png_bytes, png_err, meta = cached
        if not png_bytes or png_err:
            return None
        cached_ts = None
        caption = None
        filename = None
        if isinstance(meta, dict):
            cached_ts = meta.get("__tnt_cached_ts")
            caption = meta.get("caption")
            filename = meta.get("filename")
        if not caption:
            caption = f"🧭 **Dealer Delta Positioning (DDP)** — **{sym}** | exp **{exp_label}** | _cached_"
        if not filename:
            filename = f"{sym.lower()}_ddp.png"
        return {"png_bytes": png_bytes, "caption": caption, "filename": filename, "cached_asof_ts": cached_ts, "meta": meta}

    async def _cache_set(_key: str, result: dict[str, object]) -> None:
        try:
            png_bytes = result.get("png_bytes")
            png_err = result.get("png_err")
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            meta = dict(meta)
            meta.setdefault("caption", result.get("caption"))
            meta.setdefault("filename", result.get("filename"))
            ttl = int(result.get("ttl_sec") or ttl_sec)
            await _png_cache_set(_key, (png_bytes, png_err, meta), ttl)
        except Exception:
            pass

    async def _render_fn(*, profile: RenderProfile | None = None):
        df = await delivery._fetch_polygon_options_chain_df(
            sym,
            expiration_ymd=exp_label,
            strike_window_pct=strike_window_pct,
            max_contracts=max_contracts,
            concurrency=8,
        )
        if df is None or getattr(df, "empty", True):
            contracts_status, snap_status, detail = await _probe_options_access(exp=exp_label)
            extra = f" ({detail})" if detail else ""
            if contracts_status in {401, 403}:
                return {
                    "error": (
                        f"DDP unavailable for **{sym}**: options contracts endpoint returned HTTP **{contracts_status}**{extra}. "
                        "This usually means the API key plan doesn’t include options access."
                    ),
                    "ttl_sec": 3,
                }
            if contracts_status == 429 or snap_status == 429:
                return {"error": f"DDP temporarily unavailable for **{sym}**: rate limited (HTTP **429**). Try again shortly.", "ttl_sec": 3}
            if snap_status in {401, 403}:
                return {
                    "error": (
                        f"DDP unavailable for **{sym}**: options snapshot/greeks endpoint returned HTTP **{snap_status}**{extra}. "
                        "Contracts lookup may work, but greeks require options snapshot access on your plan."
                    ),
                    "ttl_sec": 3,
                }
            return {"error": f"No options chain data returned for **{sym}** ({provider}) on **{exp_label}**.", "ttl_sec": 3}

        t0 = _now_s()
        png_bytes, png_err, meta = await asyncio.to_thread(_try_render_ddp_png, df)
        try:
            dt_ms = (_now_s() - float(t0)) * 1000.0
            if _GPRR is not None:
                _GPRR.set_render_avg_ms(dt_ms)
        except Exception:
            pass

        if not png_bytes:
            if png_err == "delta_missing":
                return {
                    "error": f"DDP unavailable for **{sym}**: options greeks (delta) not returned for exp **{exp_label}**.",
                    "ttl_sec": 8,
                }
            return {"error": f"DDP unavailable: {png_err}.", "ttl_sec": 5}

        footer = _format_footer()
        text = f"🧭 **Dealer Delta Positioning (DDP)** — **{sym}** | exp **{exp_label}** | bounded chain (max {max_contracts})"
        if isinstance(meta, dict):
            try:
                spot = meta.get("spot")
                zd = meta.get("zero_delta")
                if isinstance(spot, (int, float)):
                    text = text + f"\nSpot: **{float(spot):.2f}**"
                if isinstance(zd, (int, float)):
                    text = text + f" | Zero Δ: **{float(zd):.2f}**"
            except Exception:
                pass
        text = text + "\n_(Simplified: dealer hedge Δ ≈ -Δ×OI×100; can help explain pinning/magnets.)_"
        if footer:
            text = f"{text}\n_{footer}_"

        try:
            macro_line = await _get_macro_regime_line()
            if macro_line:
                text = text + "\n" + macro_line
                text = await _maybe_append_crypto_context_if_macro(text, macro_line, sep="\n")
        except Exception:
            pass

        return {
            "png_bytes": png_bytes,
            "png_err": None,
            "meta": meta if isinstance(meta, dict) else {},
            "caption": text,
            "filename": f"{sym.lower()}_ddp.png",
            "ttl_sec": int(ttl_sec),
        }

    await run_heavy_chart(
        interaction=interaction,
        cmd="ddp",
        cache_key=cache_key_png_full,
        render_fn=_render_fn,
        cache_get=_cache_get,
        cache_set=_cache_set,
    )


# ------------------------------
# Stable render helpers (testable)
# ------------------------------

def _render__build_numeric_bias_pack(symbol: str, signal_payload: dict | None) -> str | None:
    """Best-effort Numeric Bias Pack formatter.

    Kept intentionally dependency-light and safe for tests.
    """

    if not symbol:
        symbol = "N/A"
    if not isinstance(signal_payload, dict):
        return None

    try:
        from delivery.numeric_bias_pack import format_numeric_bias_pack_from_signal_payload

        pack = format_numeric_bias_pack_from_signal_payload(symbol=symbol, signal_payload=signal_payload)
        return pack or None
    except Exception:
        return None


def _render__ensure_pack_first(*, title_line: str, symbol: str, signal_payload: dict | None, body: str) -> str:
    title = str(title_line or "").strip()
    body_txt = str(body or "")

    pack = _render__build_numeric_bias_pack(symbol, signal_payload)

    # If the body already begins with the same title line (common for /analyze),
    # avoid duplicating it and inject the pack immediately after the title if missing.
    first_nonempty = ""
    for ln in body_txt.splitlines():
        if ln.strip():
            first_nonempty = ln.strip()
            break

    if title and first_nonempty == title:
        if "Numeric Bias Pack" in body_txt:
            return body_txt.strip()

        if pack:
            parts = body_txt.splitlines()
            idx = 0
            for i, ln in enumerate(parts):
                if ln.strip():
                    idx = i
                    break
            injected = parts[: idx + 1] + ["", pack, ""] + parts[idx + 1 :]
            return "\n".join(injected).strip()

        return body_txt.strip()

    # Default: wrap with title + pack + body
    lines: list[str] = [title] if title else []
    if pack and "Numeric Bias Pack" not in body_txt:
        lines.append(pack)
    if body_txt.strip():
        lines.append(body_txt.strip())
    return "\n\n".join([ln for ln in lines if ln and str(ln).strip()])


def _render_ask_text(payload: dict) -> str:
    """Render a coach/ask message with Numeric Bias Pack first.

    Payload contract (minimal):
    - symbol: str
    - signal_payload: dict (optional)
    - body: str (optional)
    """

    symbol = str((payload or {}).get("symbol") or (payload or {}).get("display_symbol") or "SPY").strip() or "SPY"
    signal_payload = (payload or {}).get("signal_payload")
    body = str((payload or {}).get("body") or "").strip()
    return _render__ensure_pack_first(
        title_line=f"🧠 **Ask** — **{symbol.upper()}**",
        symbol=symbol,
        signal_payload=signal_payload if isinstance(signal_payload, dict) else None,
        body=body,
    )


def _render_analyze_text(payload: dict) -> str:
    """Render an on-demand analyze message with Numeric Bias Pack first."""

    symbol = str((payload or {}).get("symbol") or (payload or {}).get("display_symbol") or "SPY").strip() or "SPY"
    signal_payload = (payload or {}).get("signal_payload")
    body = str((payload or {}).get("body") or "").strip()
    return _render__ensure_pack_first(
        title_line=f"🔍 **On-Demand Analyze** — **{symbol.upper()}**",
        symbol=symbol,
        signal_payload=signal_payload if isinstance(signal_payload, dict) else None,
        body=body,
    )


def _render_trade_text(payload: dict) -> str:
    """Render a trade card message with Numeric Bias Pack first."""

    symbol = str((payload or {}).get("symbol") or (payload or {}).get("display_symbol") or "SPY").strip() or "SPY"
    signal_payload = (payload or {}).get("signal_payload")
    body = str((payload or {}).get("body") or "").strip()

    # Optional high-value safety: if TNT explicitly forbids trading, ensure
    # stand-down/no-trade language is present (pack order remains unchanged).
    no_trade = False
    try:
        sp = signal_payload if isinstance(signal_payload, dict) else None
        tnt = sp.get("tnt") if isinstance(sp, dict) else None
        perms = tnt.get("permissions") if isinstance(tnt, dict) else None
        no_trade = bool(perms.get("no_trade")) if isinstance(perms, dict) else False
    except Exception:
        no_trade = False

    if no_trade:
        body_upper = body.upper()
        if "STAND DOWN" not in body_upper and "NO TRADE" not in body_upper:
            prefix = "🛑 **STAND DOWN** — NO TRADE (permissions.no_trade)"
            body = prefix if not body else f"{prefix}\n\n{body}"
    return _render__ensure_pack_first(
        title_line=f"🧾 **Trade Card** — **{symbol.upper()}**",
        symbol=symbol,
        signal_payload=signal_payload if isinstance(signal_payload, dict) else None,
        body=body,
    )


@bot.tree.command(name="ask", description="Ask the TNT coach about a symbol")
@app_commands.describe(question="Trading question for the coach", symbol="Optional symbol override, e.g. TSLA")
async def ask(interaction: discord.Interaction, question: str, symbol: str = "") -> None:
    channel = interaction.channel
    responded = False

    async def _reply(message: str) -> None:
        nonlocal responded
        if responded:
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True

    if channel is None:
        await _reply("⚠️ Channel unavailable for ask.")
        return

    question_clean = (question or "").strip()
    if not question_clean:
        await _reply("⚠️ Provide a question for the coach.")
        return

    override_symbol = delivery._normalize_symbol_token(symbol) if symbol else None
    detected_symbol = delivery._extract_symbol_from_text(question_clean)
    chosen_symbol = override_symbol or detected_symbol or "SPY"


    def _is_directional_question(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False

        # Heuristic: route only questions that look like an entry/exit or direction decision.
        directional_markers = (
            "should i ",
            "do i ",
            "can i ",
            "buy ",
            "sell ",
            "long ",
            "short ",
            "calls",
            "puts",
            "call",
            "put",
            "enter",
            "entry",
            "exit",
            "stop",
            "target",
            "take profit",
            "tp",
            "sl",
            "breakout",
            "breakdown",
            "rip",
            "dip",
            "bullish",
            "bearish",
            "upside",
            "downside",
            "over ",
            "under ",
            "above ",
            "below ",
            "support",
            "resistance",
        )
        return any(m in t for m in directional_markers)


    async def _maybe_gate_directional_question() -> bool:
        """Return True if handled (gated response sent), else False."""

        if not _is_directional_question(question_clean):
            return False

        try:
            built = await asyncio.to_thread(delivery.build_signal_payload, chosen_symbol)
        except Exception:
            built = None

        if not built:
            return False

        payload, _prob_up, gate = built
        try:
            ok_tnt, reason_tnt = delivery.tnt_regime_gate(payload, output_mode="strict")
        except Exception:
            ok_tnt, reason_tnt = True, "OK"

        if ok_tnt:
            return False

        tnt = payload.get("tnt") if isinstance(payload.get("tnt"), dict) else {}
        regime = str(tnt.get("regime") or "UNKNOWN").upper()
        posture = str(tnt.get("posture") or "UNKNOWN").upper()
        confidence = tnt.get("confidence")
        conf_txt = "n/a"
        if isinstance(confidence, (int, float)):
            conf_txt = f"{float(confidence):.2f}"

        why = {}
        try:
            why = delivery.tnt_why_payload(payload, gate)
        except Exception:
            why = {}

        flips = {}
        try:
            flips = delivery.tnt_flip_triggers(payload)
        except Exception:
            flips = {}

        pivot = payload.get("pivot")
        s1 = payload.get("s1")
        r1 = payload.get("r1")

        def _fmt_level(v: object, fallback: str) -> str:
            try:
                if isinstance(v, (int, float)):
                    return str(delivery.fmt_money(float(v)))
            except Exception:
                pass
            return fallback

        lines: list[str] = []
        lines.append("🛡️ **TNT Discipline Gate: ON**")
        lines.append(
            f"**{chosen_symbol.upper()}** | Regime: **{regime}** | Posture: **{posture}** | Conf: **{conf_txt}**"
        )
        lines.append(f"Decision: **STAND DOWN** — {reason_tnt}")

        try:
            price_line = delivery.fmt_last_price(
                chosen_symbol,
                payload.get("last_price") if isinstance(payload, dict) else None,
                payload.get("last_price_ts") if isinstance(payload, dict) else None,
                tf=str(payload.get("price_tf") or "1m"),
            )
        except Exception:
            price_line = ""
        if price_line:
            lines.append(price_line)

        lines.append(
            "• Key Levels: "
            f"Pivot **{_fmt_level(pivot, 'Pivot')}** | "
            f"S1 **{_fmt_level(s1, 'S1')}** | "
            f"R1 **{_fmt_level(r1, 'R1')}**"
        )

        confirm = None
        try:
            confirm = why.get("confirmation") if isinstance(why, dict) else None
        except Exception:
            confirm = None
        if isinstance(confirm, dict):
            c_state = str(confirm.get("state") or "UNKNOWN").upper()
            vix_trend = str(confirm.get("vix_trend") or "unknown")
            sqqq_dir = str(confirm.get("sqqq_dir") or "unknown")
            lines.append(f"• Confirmation: **{c_state}** (VIX {vix_trend}; SQQQ {sqqq_dir})")

        dist = why.get("distance_to_pivot") if isinstance(why, dict) else None
        if isinstance(dist, str) and dist and dist != "n/a":
            lines.append(f"• Distance to pivot: {dist}")

        freshness = why.get("freshness") if isinstance(why, dict) else None
        if isinstance(freshness, str) and freshness:
            lines.append(f"• Data: {freshness}")

        if flips:
            lines.append("\nFlip Triggers:")
            bull = flips.get("bull")
            bear = flips.get("bear")
            neutral = flips.get("neutral")
            if bull:
                lines.append(f"• {bull}")
            if bear:
                lines.append(f"• {bear}")
            if neutral:
                lines.append(f"• {neutral}")

        lines.append("\nTry: `/trade` for the card, or `/regime tier:verbose` for details.")
        body = "\n".join(lines)
        gated = _render_ask_text({
            "symbol": chosen_symbol,
            "signal_payload": payload if isinstance(payload, dict) else None,
            "body": body,
        })
        await _reply(gated[:1800])
        return True

    try:
        await interaction.response.defer(thinking=True, ephemeral=True)
        responded = True
    except discord.HTTPException as exc:  # pragma: no cover - defensive
        if exc.code != 40060:
            raise
        responded = True  # interaction already acknowledged elsewhere
    except Exception:  # pragma: no cover - defensive
        responded = interaction.response.is_done()

    try:
        handled = await _maybe_gate_directional_question()
    except Exception:
        handled = False
    if handled:
        return

    try:
        coach_text, render, status, cache_hit, latency_ms = await delivery.run_analyze_then_coach(chosen_symbol, question_clean)
    except Exception as exc:  # noqa: BLE001
        print(f"[ASK][ERROR] {chosen_symbol.upper()}: {exc}")
        await _reply(delivery._build_coach_error_response(chosen_symbol, "Internal error"))
        return

    if not responded:
        await _reply("⚠️ Could not acknowledge the request. Try again.")
        return

    trimmed = coach_text[:delivery.AI_MAX_CHARS]
    send_target = interaction.followup
    message = None
    try:
        message = await delivery.safe_send_rate_limited(
            send_target,
            trimmed,
            requester_id=getattr(interaction.user, "id", None),
            request_kind="on_demand_text",
            client=getattr(interaction, "client", None),
            label="ask",
            kind="text",
            queue_mode="raise",
        )
    except delivery.PublishQueuedError as exc:
        await _reply(f"⏳ Queued for next window (~{int(round(exc.wait_seconds))}s).")
        # Message will be sent later by the burst queue pump.
        message = None

    if status in {"error", "format_error"}:
        render_text = render.text if isinstance(render, delivery.RenderedPost) else ""
        if render_text:
            fallback_intro = "Here’s the latest /analyze output while the coach is offline:"
            fallback = f"{fallback_intro}\n\n{render_text}"
        else:
            fallback = "Here’s the latest /analyze output while the coach is offline: (no render available)"

        fallback_trimmed = fallback[:delivery._ai_max_chars()]
        await delivery.safe_send(
            send_target,
            fallback_trimmed,
            kind="analysis",
            symbol=chosen_symbol,
            pivots=None,
            analysis_mode="coach",
            output_mode="coach-fallback",
            label=f"analyze_{chosen_symbol.lower()}",
        )

    message_id = getattr(message, "id", None) if message is not None else None
    try:
        delivery._write_ask_audit(
            question=question_clean,
            symbol=chosen_symbol,
            status=status,
            cache_hit=cache_hit,
            coach_text=trimmed,
            render=render,
            channel=channel,
            latency_ms=latency_ms,
            message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ASK][AUDIT][WARN] {chosen_symbol.upper()}: {exc}")


@bot.tree.command(name="trade", description="Regime-aware trade card (levels + confirmation + TNT gate)")
@app_commands.describe(symbol="Symbol, e.g. SPY", tier="default or pro")
async def trade(interaction: discord.Interaction, symbol: str = "SPY", tier: str = "default") -> None:
    responded = False

    async def _reply(message: str) -> None:
        nonlocal responded
        if responded:
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
            responded = True

    sym = delivery._normalize_symbol_token(symbol) if symbol else None
    chosen_symbol = sym or "SPY"
    tier_norm = (tier or "default").strip().lower()
    if tier_norm not in {"default", "pro", "verbose"}:
        tier_norm = "default"

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except Exception:
        responded = interaction.response.is_done()

    try:
        built = await asyncio.to_thread(delivery.build_signal_payload, chosen_symbol)
    except Exception as exc:  # noqa: BLE001
        await _reply(f"⚠️ trade failed for **{chosen_symbol.upper()}**: {exc}")
        return

    if not built:
        await _reply(f"⚠️ No signal payload available for **{chosen_symbol.upper()}**.")
        return

    payload, prob_up, gate = built
    tnt = payload.get("tnt") if isinstance(payload.get("tnt"), dict) else {}
    regime = str(tnt.get("regime") or "UNKNOWN").upper()
    posture = str(tnt.get("posture") or "UNKNOWN").upper()
    confidence = tnt.get("confidence")
    conf_txt = "n/a"
    if isinstance(confidence, (int, float)):
        conf_txt = f"{float(confidence):.2f}"

    try:
        ok_tnt, reason_tnt = delivery.tnt_regime_gate(payload, output_mode="strict")
    except Exception:
        ok_tnt, reason_tnt = True, "OK"

    lines: list[str] = []

    lines.append("🛡️ TNT Discipline Gate: **ON**")
    lines.append(f"Regime: **{regime}** | Posture: **{posture}** | Conf: **{conf_txt}**")

    try:
        lines.append(
            delivery.fmt_last_price(
                chosen_symbol,
                payload.get("last_price") if isinstance(payload, dict) else None,
                payload.get("last_price_ts") if isinstance(payload, dict) else None,
                tf=str(payload.get("price_tf") or "1m"),
            )
        )
    except Exception:
        pass

    # Tiny context summaries (cache-first; never triggers heavy compute).
    async def _pressure_summary_line_from_cache(sym_key: str) -> str | None:
        try:
            dte_min = 7
            dte_max = 14
            strike_window_pct = 0.10
            max_contracts_per_exp = 350
            cache_key_base = f"pressure_png:v1:{sym_key}:{dte_min}:{dte_max}:{int(strike_window_pct*100)}:{max_contracts_per_exp}"
            cache_key = _gprr_cache_key(cache_key_base)
            cached = await _png_cache_get(cache_key, family="pressure")
            if cached is None:
                # Back-compat: older cache key used by the previous `/chart pressure` variant.
                max_expirations = 6
                cache_key_base_old = (
                    f"chart_pressure_png:v1:{sym_key}:{dte_min}:{dte_max}:{int(strike_window_pct*100)}:{max_contracts_per_exp}:{max_expirations}"
                )
                cache_key_old = _gprr_cache_key(cache_key_base_old)
                cached = await _png_cache_get(cache_key_old, family="pressure")
        except Exception:
            cached = None
        if cached is None:
            return "🧭 Pressure: _n/a (run /pressure)_"
        _png, _err, meta = cached
        if not isinstance(meta, dict):
            return "🧭 Pressure: _n/a (run /pressure)_"
        if not bool(meta.get("options_ok")):
            return "🧭 Pressure: _snapshot unavailable_"

        score = meta.get("pressure_score")
        net = meta.get("net_pressure")
        pcr = meta.get("pcr_premium")

        def _fmt_money(val: object) -> str:
            try:
                x = float(val)
            except Exception:
                return "n/a"
            sign = "-" if x < 0 else ""
            ax = abs(x)
            if ax >= 1e9:
                return f"{sign}${ax/1e9:.2f}B"
            if ax >= 1e6:
                return f"{sign}${ax/1e6:.2f}M"
            if ax >= 1e3:
                return f"{sign}${ax/1e3:.2f}K"
            return f"{sign}${ax:.0f}"

        try:
            score_txt = f"{float(score):+.2f}" if isinstance(score, (int, float)) else "n/a"
        except Exception:
            score_txt = "n/a"
        try:
            pcr_txt = f"{float(pcr):.2f}" if isinstance(pcr, (int, float)) else "n/a"
        except Exception:
            pcr_txt = "n/a"

        return f"🧭 Pressure: score **{score_txt}** | net **{_fmt_money(net)}** | PCR **{pcr_txt}**"

    async def _htf_summary_line_from_cache(sym_key: str) -> str | None:
        try:
            cache_key_base = f"chart_htf_png:v3:{sym_key}:1d"
            cache_key = _gprr_cache_key(cache_key_base)
            cached = await _png_cache_get(cache_key, family="htf")
        except Exception:
            cached = None
        if cached is None:
            return "🧠 HTF: _n/a (run /htf)_"
        _png, _err, meta = cached
        if not isinstance(meta, dict):
            return "🧠 HTF: _n/a (run /htf)_"

        state = str(meta.get("state") or "n/a")
        direction = str(meta.get("direction") or "").strip()
        weekly = str(meta.get("weekly_bias") or "n/a")
        rvol = meta.get("rvol")
        atr_pct = meta.get("atr_pct")

        rv_txt = "n/a"
        try:
            if isinstance(rvol, (int, float)):
                rv_txt = f"{float(rvol):.2f}"
        except Exception:
            rv_txt = "n/a"
        atr_txt = "n/a"
        try:
            if isinstance(atr_pct, (int, float)):
                atr_txt = f"{float(atr_pct)*100.0:.2f}%"
        except Exception:
            atr_txt = "n/a"

        state_txt = state
        if state.upper() == "ON" and direction:
            state_txt = f"{state} ({direction})"
        return f"🧠 HTF: **{state_txt}** | Weekly **{weekly}** | RVOL **{rv_txt}** | ATR% **{atr_txt}**"

    try:
        pressure_line = await _pressure_summary_line_from_cache(chosen_symbol.upper())
        htf_line = await _htf_summary_line_from_cache(chosen_symbol.upper())
        if pressure_line:
            lines.append(pressure_line)
        if htf_line:
            lines.append(htf_line)
    except Exception:
        pass

    pivot = payload.get("pivot")
    s1 = payload.get("s1")
    r1 = payload.get("r1")

    def _fmt_level(v: object, fallback: str) -> str:
        try:
            if isinstance(v, (int, float)):
                return str(delivery.fmt_money(float(v)))
        except Exception:
            pass
        return fallback

    lines.append(
        "• Levels: "
        f"Pivot **{_fmt_level(pivot, 'Pivot')}** | "
        f"S1 **{_fmt_level(s1, 'S1')}** | "
        f"R1 **{_fmt_level(r1, 'R1')}**"
    )

    bias = str(payload.get("bias") or "NEUTRAL").upper()
    confirm = str(payload.get("bias_confirm") or "UNKNOWN").upper()
    vix_trend = str(payload.get("vix_trend") or "unknown")
    sqqq_dir = str(payload.get("sqqq_dir") or "unknown")
    lines.append(f"• Bias: **{bias}** (confirm **{confirm}**) | VIX {vix_trend} | SQQQ {sqqq_dir}")

    try:
        prob_txt = f"{float(prob_up):.3f}" if isinstance(prob_up, (int, float)) else "n/a"
    except Exception:
        prob_txt = "n/a"
    edge = payload.get("edge")
    edge_txt = "n/a"
    if isinstance(edge, (int, float)):
        edge_txt = f"{float(edge):.3f}"
    conviction = str(payload.get("conviction") or "UNKNOWN").upper()
    lines.append(f"• Model: prob_up {prob_txt} | edge {edge_txt} | conviction {conviction}")

    gate_mark = "✅" if ok_tnt else "🛑"
    lines.append(f"{gate_mark} TNT Gate: **{reason_tnt}**")

    try:
        flips = delivery.tnt_flip_triggers(payload)
    except Exception:
        flips = {}
    if flips:
        lines.append("\nFlip Triggers:")
        for key in ("bull", "bear", "neutral"):
            val = flips.get(key)
            if val:
                lines.append(f"• {val}")

    if tier_norm in {"pro", "verbose"}:
        try:
            why = delivery.tnt_why_payload(payload, gate)
        except Exception:
            why = {}
        if isinstance(why, dict):
            dist = why.get("distance_to_pivot")
            freshness = why.get("freshness")
            vix_gate = why.get("vix_gate")
            if dist:
                lines.append(f"\nWhy: pivot_dist {dist}")
            if isinstance(vix_gate, dict):
                mode = str(vix_gate.get("mode") or "").upper()
                reason = str(vix_gate.get("reason") or "").strip()
                if mode or reason:
                    lines.append(f"Why: VIX gate {mode} ({reason})")
            if isinstance(freshness, str) and freshness:
                lines.append(f"Why: data {freshness}")
            tnt_reasons = why.get("tnt_reasons")
            if isinstance(tnt_reasons, list) and tnt_reasons:
                lines.append("Why: " + "; ".join(str(x) for x in tnt_reasons[:4]))

    body = "\n".join(lines)
    out = _render_trade_text({
        "symbol": chosen_symbol,
        "signal_payload": payload if isinstance(payload, dict) else None,
        "body": body,
    })
    await _reply(out[:1900])


@bot.tree.command(name="status", description="Show autopost system status (owner only).")
async def status_command(interaction: discord.Interaction) -> None:
    responded = False

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
        responded = True
    except discord.NotFound:
        responded = False

    if OWNER_ID and interaction.user.id != OWNER_ID:
        if responded:
            await interaction.followup.send("Not authorized.", ephemeral=True)
        return

    try:
        report = await asyncio.to_thread(_build_status_report)
    except Exception as exc:  # noqa: BLE001
        if responded:
            await interaction.followup.send(f"❌ status failed: {exc}", ephemeral=True)
        elif interaction.channel:
            await interaction.channel.send(f"❌ status failed: {exc}")
        return

    if responded:
        await interaction.followup.send(report, ephemeral=True)
        return

    if interaction.channel is not None:
        await interaction.channel.send(report)


@bot.event
async def on_ready() -> None:
    def _config_value(key: str) -> str:
        raw = os.getenv(key)
        if raw and raw.strip():
            return raw.strip()
        for env_path in (PROJECT_ROOT / ".env.local", PROJECT_ROOT / ".env"):
            val = _dotenv_get_value(env_path, key)
            if val and val.strip():
                return val.strip()
        return ""

    guild_id_raw = _config_value("DISCORD_GUILD_ID")

    def _sync_scope() -> str:
        raw = _config_value("TNT_COMMAND_SYNC_SCOPE").strip().lower()
        if raw in {"global", "guild", "both"}:
            return raw
        # Default: in dev (guild id present) prefer guild-only for fast updates.
        return "guild" if guild_id_raw.isdigit() else "global"

    def _prune_other_enabled() -> bool:
        # Default prune = on when guild id is present (to remove duplicates from past global syncs).
        default = "1" if guild_id_raw.isdigit() else "0"
        raw = _config_value("TNT_COMMAND_SYNC_PRUNE_OTHER")
        if not raw:
            raw = default
        return _trueish(raw)

    async def _delete_remote_commands(*, guild: discord.Object | None) -> int:
        """Best-effort: delete all remote app commands for the given scope.

        - guild=None deletes global commands
        - guild=Object(...) deletes guild-scoped commands
        """

        deleted = 0
        try:
            fetch = getattr(bot.tree, "fetch_commands", None)
            delete = getattr(bot.tree, "delete_command", None)
            if fetch is None or delete is None:
                return 0
            remote_cmds = await fetch(guild=guild)
        except Exception:
            return 0

        for c in list(remote_cmds or []):
            try:
                cid = getattr(c, "id", None)
                if cid is not None:
                    await delete(cid, guild=guild)
                else:
                    await delete(c, guild=guild)
                deleted += 1
            except Exception:
                continue
        return int(deleted)

    # Startup verification: ensure TNT is reading the on-disk system prompt.
    # Logs path + first ~60 chars + sha256 so we can spot stale/embedded prompts.
    try:
        delivery.load_tnt_system_prompt(log=True)
    except Exception as exc:  # pragma: no cover - diagnostic
        print(f"[TNT][PROMPT][ERROR] Failed to load tnt_system_prompt.txt: {exc}")

    scope = _sync_scope()
    prune_other = _prune_other_enabled()

    # Retired commands: ensure they are not registered locally and get removed remotely.
    # These used to exist as slash commands but are now handled via mention routing.
    retired = {"ask", "analyze"}
    for name in retired:
        try:
            bot.tree.remove_command(name)
        except Exception:
            pass

    async def _delete_remote_commands_by_name(*, names: set[str], guild: discord.Object | None) -> int:
        deleted = 0
        try:
            fetch = getattr(bot.tree, "fetch_commands", None)
            delete = getattr(bot.tree, "delete_command", None)
            if fetch is None or delete is None:
                return 0
            remote_cmds = await fetch(guild=guild)
        except Exception:
            return 0

        for c in list(remote_cmds or []):
            try:
                cname = str(getattr(c, "name", "") or "").lower().strip()
                if cname not in names:
                    continue
                cid = getattr(c, "id", None)
                if cid is not None:
                    await delete(cid, guild=guild)
                else:
                    await delete(c, guild=guild)
                deleted += 1
            except Exception:
                continue
        return int(deleted)

    # Delete any lingering remote registrations regardless of chosen sync scope.
    try:
        n_global = await _delete_remote_commands_by_name(names=retired, guild=None)
        n_guild = 0
        if guild_id_raw.isdigit():
            n_guild = await _delete_remote_commands_by_name(names=retired, guild=discord.Object(id=int(guild_id_raw)))
        if n_global or n_guild:
            print(f"[TNT][SYNC] Deleted retired remote commands: global={n_global} guild={n_guild}")
    except Exception:
        pass

    if guild_id_raw.isdigit():
        try:
            gid = int(guild_id_raw)
            in_guild = any(getattr(g, "id", None) == gid for g in getattr(bot, "guilds", []) or [])
            print(f"[TNT][SYNC] Target guild {gid} in bot.guilds={in_guild} (connected_guilds={len(getattr(bot, 'guilds', []) or [])})")
        except Exception:
            pass

    if scope == "both":
        synced_global = await bot.tree.sync()
        print(f"Slash commands synced globally ({len(synced_global or [])} cmds).")
        if guild_id_raw.isdigit():
            try:
                guild = discord.Object(id=int(guild_id_raw))
                try:
                    bot.tree.copy_global_to(guild=guild)
                except Exception as exc:  # pragma: no cover - diagnostic
                    print(f"[TNT][SYNC] copy_global_to failed for {guild_id_raw}: {type(exc).__name__}: {exc}")
                synced_guild = await bot.tree.sync(guild=guild)
            except Exception as exc:  # pragma: no cover - diagnostic
                print(f"Slash commands guild sync failed for {guild_id_raw}: {exc}")
            else:
                print(f"Slash commands synced to guild {guild_id_raw} ({len(synced_guild or [])} cmds).")
    elif scope == "guild" and guild_id_raw.isdigit():
        guild = discord.Object(id=int(guild_id_raw))
        if prune_other:
            n = await _delete_remote_commands(guild=None)
            if n:
                print(f"Pruned {n} global commands to prevent duplicates.")
        try:
            try:
                bot.tree.copy_global_to(guild=guild)
            except Exception as exc:  # pragma: no cover - diagnostic
                print(f"[TNT][SYNC] copy_global_to failed for {guild_id_raw}: {type(exc).__name__}: {exc}")
            synced_guild = await bot.tree.sync(guild=guild)
        except Exception as exc:  # pragma: no cover - diagnostic
            print(f"Slash commands guild sync failed for {guild_id_raw}: {exc}")
        else:
            print(f"Slash commands synced to guild {guild_id_raw} ({len(synced_guild or [])} cmds).")
    else:
        # Global-only (optionally prune guild duplicates if a dev guild was previously used).
        if prune_other and guild_id_raw.isdigit():
            try:
                guild = discord.Object(id=int(guild_id_raw))
                n = await _delete_remote_commands(guild=guild)
                if n:
                    print(f"Pruned {n} guild commands to prevent duplicates.")
            except Exception:
                pass
        synced_global = await bot.tree.sync()
        print(f"Slash commands synced globally ({len(synced_global or [])} cmds).")

    # Ground-truth check: what does Discord report as registered right now?
    try:
        remote_global = await bot.tree.fetch_commands(guild=None)
        print("Remote global commands:", [getattr(c, "name", "?") for c in (remote_global or [])])
    except Exception as exc:  # pragma: no cover - diagnostic
        print(f"Remote global commands fetch failed: {type(exc).__name__}: {exc}")

    if guild_id_raw.isdigit():
        try:
            guild = discord.Object(id=int(guild_id_raw))
            remote_guild = await bot.tree.fetch_commands(guild=guild)
            print("Remote guild commands:", [getattr(c, "name", "?") for c in (remote_guild or [])])
        except Exception as exc:  # pragma: no cover - diagnostic
            print(f"Remote guild commands fetch failed for {guild_id_raw}: {type(exc).__name__}: {exc}")

    print("Registered commands:", [cmd.name for cmd in bot.tree.walk_commands()])

    if bot.user is not None:
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Worker health probe (ops visibility): if OI is worker-required and the worker is on an
    # older build, layout fixes will not appear even after restarting this bot.
    try:
        base = (os.getenv("TNT_WORKER_URL") or "").strip().rstrip("/")
        if base:
            import subprocess

            local_build = (os.getenv("TNT_BUILD") or "").strip()
            if not local_build:
                try:
                    local_build = (
                        subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(Path(__file__).resolve().parents[1]))
                        .decode("utf-8", errors="ignore")
                        .strip()
                    )
                except Exception:
                    local_build = ""

            import aiohttp

            async def _probe() -> None:
                try:
                    timeout = aiohttp.ClientTimeout(total=2.0)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(f"{base}/healthz") as resp:
                            d = await resp.json(content_type=None)
                    ok = bool(d.get("ok")) if isinstance(d, dict) else False
                    build = str(d.get("build") or "") if isinstance(d, dict) else ""
                    uptime_s = d.get("uptime_s") if isinstance(d, dict) else None
                    print(f"[TNT][WORKER][HEALTH] url={base} ok={ok} build={build} uptime_s={uptime_s}")
                    if local_build and build and build != local_build:
                        print(f"[TNT][WORKER][WARN] build mismatch: worker={build} local={local_build} (deploy/restart worker to pick up chart fixes)")
                except Exception as exc:
                    print(f"[TNT][WORKER][HEALTH][WARN] probe failed url={base} err={type(exc).__name__}:{exc}")

            asyncio.create_task(_probe())
    except Exception:
        pass

    # Heartbeat for watchdogs / ops.
    try:
        if not hasattr(bot, "_tnt_heartbeat_task") or getattr(bot, "_tnt_heartbeat_task") is None or getattr(bot, "_tnt_heartbeat_task").done():
            setattr(bot, "_tnt_heartbeat_task", asyncio.create_task(_heartbeat_loop()))
            print(f"[TNT] Heartbeat enabled: {_heartbeat_path()}")
    except Exception:
        pass

    # Legacy automation loops (earnings autopost, outlooks, heartbeat, etc).
    # Uses the env gates in delivery.discord_bot; safe to call even when disabled.
    try:
        if hasattr(delivery, "_ensure_tnt_automation_tasks"):
            delivery._ensure_tnt_automation_tasks()  # type: ignore[attr-defined]
    except Exception:
        pass

    # Alerts delivery loop (Redis -> Discord).
    # This is independent of earnings + /oi and is gated by TNT_ALERTS_DISCORD_DELIVERY_ENABLED.
    try:
        if (
            not hasattr(bot, "_tnt_alerts_delivery_task")
            or getattr(bot, "_tnt_alerts_delivery_task") is None
            or getattr(bot, "_tnt_alerts_delivery_task").done()
        ):
            setattr(bot, "_tnt_alerts_delivery_task", asyncio.create_task(_alerts_delivery_loop()))
    except Exception:
        pass


@bot.event
async def on_message(message: discord.Message) -> None:
    # Never respond to our own messages.
    if bot.user and message.author.id == bot.user.id:
        return

    # Default: ignore other bots. Optional: allow Concierge to react to alert-bot posts.
    if message.author.bot:
        if concierge_throttle is None or not concierge_throttle.allow_other_bot_messages_for_auto():
            return

    # Channel router: enforce channel purpose rules (paper trades ack, redirects, silence).
    try:
        if _router_enabled is not None and _router_enabled() and _decide_route is not None:
            ch_name = None
            try:
                ch_name = str(getattr(getattr(message, "channel", None), "name", None) or "")
            except Exception:
                ch_name = None

            decision = _decide_route(channel_id=_route_channel_id(message.channel), content=message.content or "", channel_name=ch_name)
            if decision.action == "silent":
                return
            if decision.action == "ack_trade_log":
                # Prefer reaction (quiet), fallback to reply.
                try:
                    await message.add_reaction("✅")
                except Exception:
                    if decision.reply_text:
                        try:
                            await message.reply(decision.reply_text, mention_author=False)
                        except Exception:
                            pass
                return
            if decision.action in {"redirect", "review_only"}:
                if decision.reply_text:
                    try:
                        await message.reply(decision.reply_text, mention_author=False)
                    except Exception:
                        pass
                return
    except Exception:
        pass

    # Deterministic #ask-tnt earnings embed (legacy-style, premium fields).
    # This keeps `/oi` working by allowing a single gateway session to serve both.
    try:
        if await delivery.maybe_handle_ask_tnt_earnings_embed(message):
            return
    except Exception:
        pass

    # Deterministic macro/econ calendar (CPI/FOMC/NFP/etc). Prefer CSV (rich) with Redis fallback (gating).
    try:
        content = (message.content or "").strip()
        if content and len(content) <= 220:
            reply = _macro_calendar_render_reply(query=content)
            if reply:
                await message.reply(reply, mention_author=False)
                return
    except Exception:
        pass

    # Deterministic VWAP distance reply for common asks (e.g., "how close is spy to vwap?").
    # Uses local DB 1m RTH bars (same pipeline source as other intraday features).
    try:
        content = (message.content or "").strip()
        t = content.lower()
        if content and len(content) <= 220 and ("vwap" in t):
            sym = None
            try:
                sym = delivery._extract_symbol_from_text(content)
            except Exception:
                sym = None
            if sym:
                try:
                    sym_norm = delivery._normalize_symbol_token(sym)
                except Exception:
                    sym_norm = None
                sym = sym_norm or sym
                # Skip crypto shorthand / non-equity tickers for this quick handler.
                if str(sym).upper().startswith("X:"):
                    sym = None

            if sym:
                try:
                    print(f"[TNT][ASK][VWAP] sym={sym} channel_id={_route_channel_id(message.channel)}")
                except Exception:
                    pass

                snap = None
                try:
                    snap = delivery._get_last_price_snapshot(sym)
                except Exception:
                    snap = None

                last_px = None
                if snap is not None and getattr(snap, "price", None) is not None:
                    try:
                        last_px = float(snap.price)
                    except Exception:
                        last_px = None

                # Prefer RTH VWAP from latest session bars (volume-weighted typical price).
                session = None
                try:
                    session = getattr(delivery, "_latest_session_bars")(sym)
                except Exception:
                    session = None

                vwap = None
                try:
                    bars = session.get("bars") if isinstance(session, dict) else None
                    if isinstance(bars, list) and bars:
                        num = 0.0
                        den = 0.0
                        for b in bars:
                            if not isinstance(b, dict):
                                continue
                            vol = float(b.get("volume") or 0.0)
                            if vol <= 0:
                                continue
                            h = float(b.get("high") or 0.0)
                            l = float(b.get("low") or 0.0)
                            c = float(b.get("close") or 0.0)
                            tp = (h + l + c) / 3.0
                            num += tp * vol
                            den += vol
                        if den > 0:
                            vwap = num / den
                except Exception:
                    vwap = None

                if last_px is None:
                    await message.reply(f"{sym} — price snapshot unavailable right now. Try `/oi {sym}`.", mention_author=False)
                    return

                if vwap is None or not math.isfinite(float(vwap)) or float(vwap) == 0.0:
                    await message.reply(
                        f"{sym} — VWAP unavailable right now (no fresh RTH 1m bars). Try `/vwap_range {sym}`.",
                        mention_author=False,
                    )
                    return

                delta = last_px - float(vwap)
                pct = (delta / float(vwap)) * 100.0
                side = "above" if delta >= 0 else "below"
                await message.reply(
                    f"{sym} VWAP (RTH): {float(vwap):.2f} | Last: {last_px:.2f} | Δ: {delta:+.2f} ({pct:+.2f}%) — {side}",
                    mention_author=False,
                )
                return
    except Exception:
        pass

    # Deterministic technical indicators for any detected ticker in #ask-tnt.
    # Examples: "mstr rsi", "rsi mstr", "mstr macd", "mstr sma 20", "mstr ema 9", "mstr technicals".
    # Uses local DB 1m RTH bars + last price snapshot.
    try:
        content = (message.content or "").strip()
        t = content.lower()
        full_pack = any(k in t for k in ("technicals", "technical", "indicators", "indicator", "ta"))
        # If user posts only a bare ticker (e.g. "MSTR"), treat it like a full-pack technicals ask.
        try:
            bare = content.strip()
            while bare.endswith("?") or bare.endswith("!") or bare.endswith("."):
                bare = bare[:-1]
            bare = bare.strip()
            if bare.startswith("$"):
                bare = bare[1:]
            symbol_only = (" " not in bare) and (1 <= len(bare) <= 6) and bare.isalpha()
        except Exception:
            symbol_only = False
        full_pack = full_pack or bool(symbol_only)

        if content and len(content) <= 220 and (full_pack or any(k in t for k in ("rsi", "macd", "sma", "ema"))):
            sym = None
            try:
                sym = delivery._extract_symbol_from_text(content)
            except Exception:
                sym = None
            if sym:
                try:
                    sym_norm = delivery._normalize_symbol_token(sym)
                except Exception:
                    sym_norm = None
                sym = sym_norm or sym
                if str(sym).upper().startswith("X:"):
                    sym = None

            if sym:
                snap = None
                try:
                    snap = delivery._get_last_price_snapshot(sym)
                except Exception:
                    snap = None

                last_px = None
                if snap is not None and getattr(snap, "price", None) is not None:
                    try:
                        last_px = float(snap.price)
                    except Exception:
                        last_px = None

                session = None
                try:
                    session = getattr(delivery, "_latest_session_bars")(sym)
                except Exception:
                    session = None

                bars = session.get("bars") if isinstance(session, dict) else None
                if not isinstance(bars, list) or not bars:
                    await message.reply(
                        f"{sym} — no fresh RTH 1m bars available right now. Try `/chart {sym}`.",
                        mention_author=False,
                    )
                    return

                closes: list[float] = []
                highs: list[float] = []
                lows: list[float] = []
                vols: list[float] = []
                for b in bars:
                    if not isinstance(b, dict):
                        continue
                    try:
                        c = float(b.get("close"))
                        h = float(b.get("high"))
                        l = float(b.get("low"))
                        v = float(b.get("volume") or 0.0)
                    except Exception:
                        continue
                    closes.append(c)
                    highs.append(h)
                    lows.append(l)
                    vols.append(v)

                if last_px is None and closes:
                    last_px = float(closes[-1])

                if last_px is None:
                    await message.reply(f"{sym} — price unavailable right now. Try `/oi {sym}`.", mention_author=False)
                    return

                # Parse optional periods (defaults chosen for intraday quick look).
                words = [w for w in (t.replace("?", " ").replace(",", " ").replace("/", " ").split()) if w]

                def _parse_period(keyword: str, default: int) -> int:
                    try:
                        for i, w in enumerate(words):
                            if w == keyword and i + 1 < len(words) and words[i + 1].isdigit():
                                return max(2, min(int(words[i + 1]), 500))
                            if w.startswith(keyword) and w[len(keyword) :].isdigit():
                                return max(2, min(int(w[len(keyword) :]), 500))
                        return int(default)
                    except Exception:
                        return int(default)

                rsi_n = _parse_period("rsi", 14)
                sma_n = _parse_period("sma", 20)
                ema_n = _parse_period("ema", 20)

                def _sma_last(vals: list[float], n: int) -> float | None:
                    if n <= 0 or len(vals) < n:
                        return None
                    return float(sum(vals[-n:]) / float(n))

                def _ema_last(vals: list[float], n: int) -> float | None:
                    if n <= 0 or len(vals) < n:
                        return None
                    alpha = 2.0 / (float(n) + 1.0)
                    ema_val = float(vals[0])
                    for v in vals[1:]:
                        ema_val = (float(v) * alpha) + (ema_val * (1.0 - alpha))
                    return float(ema_val)

                # RSI + MACD via shared indicator module (fast, lightweight).
                rsi_val = None
                macd_val = None
                macd_sig = None
                macd_hist = None
                try:
                    from analysis import indicators as _ind

                    rsi_val = _ind.rsi(closes, int(rsi_n))
                    macd_val, macd_sig, macd_hist = _ind.macd(closes, 12, 26, 9)
                except Exception:
                    rsi_val = None
                    macd_val = None
                    macd_sig = None
                    macd_hist = None

                sma_val = _sma_last(closes, int(sma_n))
                ema_val = _ema_last(closes, int(ema_n))

                # VWAP is sometimes asked as part of "technicals"; include if user mentioned it (or asked for full pack).
                vwap_val = None
                if full_pack or ("vwap" in t):
                    try:
                        num = 0.0
                        den = 0.0
                        for (h, l, c, v) in zip(highs, lows, closes, vols):
                            if v <= 0:
                                continue
                            tp = (float(h) + float(l) + float(c)) / 3.0
                            num += tp * float(v)
                            den += float(v)
                        if den > 0:
                            vwap_val = num / den
                    except Exception:
                        vwap_val = None

                parts: list[str] = []
                if full_pack or ("rsi" in t):
                    if isinstance(rsi_val, (int, float)):
                        tag = "neutral"
                        if float(rsi_val) >= 70:
                            tag = "overbought"
                        elif float(rsi_val) <= 30:
                            tag = "oversold"
                        parts.append(f"RSI({int(rsi_n)}): {float(rsi_val):.1f} ({tag})")
                    else:
                        parts.append(f"RSI({int(rsi_n)}): n/a")

                if full_pack or ("macd" in t):
                    if all(isinstance(x, (int, float)) for x in (macd_val, macd_sig, macd_hist)):
                        parts.append(f"MACD: {float(macd_val):+.3f} | Sig: {float(macd_sig):+.3f} | Hist: {float(macd_hist):+.3f}")
                    else:
                        parts.append("MACD: n/a")

                if full_pack or ("sma" in t):
                    if isinstance(sma_val, (int, float)):
                        d = float(last_px) - float(sma_val)
                        p = (d / float(sma_val) * 100.0) if float(sma_val) else 0.0
                        side = "above" if d >= 0 else "below"
                        parts.append(f"SMA({int(sma_n)}): {float(sma_val):.2f} (Δ {d:+.2f} {p:+.2f}%, {side})")
                    else:
                        parts.append(f"SMA({int(sma_n)}): n/a")

                if full_pack or ("ema" in t):
                    if isinstance(ema_val, (int, float)):
                        d = float(last_px) - float(ema_val)
                        p = (d / float(ema_val) * 100.0) if float(ema_val) else 0.0
                        side = "above" if d >= 0 else "below"
                        parts.append(f"EMA({int(ema_n)}): {float(ema_val):.2f} (Δ {d:+.2f} {p:+.2f}%, {side})")
                    else:
                        parts.append(f"EMA({int(ema_n)}): n/a")

                if vwap_val is not None and math.isfinite(float(vwap_val)) and float(vwap_val) != 0.0:
                    d = float(last_px) - float(vwap_val)
                    p = (d / float(vwap_val) * 100.0)
                    side = "above" if d >= 0 else "below"
                    parts.append(f"VWAP(RTH): {float(vwap_val):.2f} (Δ {d:+.2f} {p:+.2f}%, {side})")

                if parts:
                    await message.reply(f"{sym} | Last: {float(last_px):.2f} | " + " • ".join(parts), mention_author=False)
                    return
    except Exception:
        pass

    # Deterministic paper-trade summary (lightweight). Answers questions like "any paper trades today?".
    try:
        content = (message.content or "").strip()
        t = content.lower()
        if content and len(content) <= 160 and ("paper" in t) and ("trade" in t or "trades" in t) and ("today" in t):
            et_date = None
            try:
                et_date = delivery._now_et().date()
            except Exception:
                et_date = None

            # Canonical source of truth: SQLite paperdesk_trades.
            try:
                import sqlite3

                day = et_date.isoformat() if et_date else None
                db_path = (os.getenv("DB_PATH") or "db/tnt.db").strip() or "db/tnt.db"
                conn = sqlite3.connect(db_path)
                try:
                    if day:
                        row = conn.execute(
                            "SELECT "
                            "SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open_n, "
                            "SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) AS closed_n, "
                            "COALESCE(SUM(CASE WHEN status='CLOSED' THEN result_r ELSE 0 END), 0) AS net_r "
                            "FROM paperdesk_trades WHERE day=?",
                            (day,),
                        ).fetchone()
                        open_n = int(row[0] or 0) if row else 0
                        closed_n = int(row[1] or 0) if row else 0
                        net_r = float(row[2] or 0.0) if row else 0.0

                        if open_n or closed_n:
                            await message.reply(
                                f"Paper trades today ({day} ET): open={open_n} | closed={closed_n} | netR={net_r:+.2f}",
                                mention_author=False,
                            )
                            return

                    last = conn.execute("SELECT MAX(day) FROM paperdesk_trades").fetchone()
                    last_day = (str(last[0]) if last and last[0] else "")
                finally:
                    conn.close()

                if et_date and last_day:
                    await message.reply(
                        f"No paper trades recorded today ({et_date.isoformat()} ET). Last paperdesk trade day in DB: {last_day}.",
                        mention_author=False,
                    )
                elif et_date:
                    await message.reply(f"No paper trades recorded today ({et_date.isoformat()} ET).", mention_author=False)
                else:
                    await message.reply("No paper trades recorded today.", mention_author=False)
                return
            except Exception:
                # DB not available / schema missing. Fall back to the old log-based behavior.
                if et_date:
                    await message.reply(f"No paper trades recorded today ({et_date.isoformat()} ET).", mention_author=False)
                else:
                    await message.reply("No paper trades recorded today.", mention_author=False)
                return
    except Exception:
        pass

    # Deterministic SPY quick reply for plain-text asks (avoid AI routes / silence).
    # This is intentionally narrow to avoid chatter.
    try:
        content = (message.content or "").strip()
        t = content.lower()
        if content and len(content) <= 140 and ("earnings" not in t):
            # Only fire for lightweight "what is SPY doing" / "SPY price" style asks.
            mentions_spy = "spy" in t.split() or t.startswith("spy") or " spy" in t
            asks_pricey = any(k in t for k in ("price", "quote", "status", "doing", "today", "now", "at "))
            if mentions_spy and asks_pricey:
                try:
                    print(f"[TNT][ASK][SNAPSHOT] sym=SPY channel_id={_route_channel_id(message.channel)}")
                except Exception:
                    pass
                snap = None
                try:
                    snap = delivery._get_last_price_snapshot("SPY")
                except Exception:
                    snap = None

                # delivery._get_last_price_snapshot returns market_data.last_price.LastPrice.
                if snap is not None and getattr(snap, "price", None) is not None and getattr(snap, "asof_et", None) is not None:
                    try:
                        block = delivery.format_last_price_block(snap)
                    except Exception:
                        block = None
                    if block:
                        await message.reply(block, mention_author=False)
                    else:
                        await message.reply(f"SPY — {float(snap.price):.2f}", mention_author=False)
                else:
                    await message.reply("SPY — price snapshot unavailable right now. Try `/oi SPY`." , mention_author=False)
                return
    except Exception:
        pass

    # Concierge is additive; it must not block normal command handling.
    try:
        if schedule_concierge_nudge is not None:
            mentioned_user = bool(bot.user and bot.user in (message.mentions or []))
            schedule_concierge_nudge(
                message,
                triggered=bool(mentioned_user),
                now_et_fn=delivery._now_et,
                get_last_price_snapshot=delivery._get_last_price_snapshot,
                get_latest_signal_context=delivery.get_latest_signal_context,
                get_vix_context=delivery.get_vix_context,
                vix_gating_action=delivery.vix_gating_action,
                vix_gating_enabled=getattr(delivery, "VIX_GATING_ENABLED", False),
                safe_send=delivery.safe_send,
            )
    except Exception:
        pass

    await bot.process_commands(message)

    # GPRR (GPU Pressure Relief & Render Autopilot) V1a heuristic baseline.
    try:
        global _GPRR
        if _GPRR is None:
            _GPRR = GPRRManager(gather_telemetry=_gather_gprr_telemetry)
        if _GPRR.enabled():
            await _GPRR.start()
            print("[TNT][GPRR] enabled")
        else:
            print("[TNT][GPRR] disabled")
    except Exception as exc:  # pragma: no cover - defensive
        try:
            print(f"[TNT][GPRR][ERROR] failed to start: {exc}")
        except Exception:
            pass

    # Background cache warmer (best-effort).
    try:
        if not hasattr(bot, "_tnt_cache_warmer_task") or getattr(bot, "_tnt_cache_warmer_task") is None or getattr(bot, "_tnt_cache_warmer_task").done():
            setattr(bot, "_tnt_cache_warmer_task", asyncio.create_task(cache_warmer_loop()))
            print("[TNT] cache warmer enabled")
    except Exception:
        pass


if __name__ == "__main__":
    _BOT_SINGLETON_LOCK: object | None = None

    def _acquire_bot_singleton_lock() -> bool:
        """Best-effort single-instance lock for local/dev runs.

        Prevents two bot processes (same token) from simultaneously responding to
        the same interaction, which can manifest as duplicate channel posts.

        Override with `TNT_ALLOW_MULTIPLE_BOTS=1`.
        """

        allow_multi = str(os.getenv("TNT_ALLOW_MULTIPLE_BOTS", "") or "").strip().lower() in {"1", "true", "yes"}
        if allow_multi:
            return True

        try:
            lock_dir = Path("logs")
            lock_dir.mkdir(parents=True, exist_ok=True)
            safe_entry = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in _TNT_ENTRYPOINT)
            lock_path = lock_dir / f"tnt_discord_bot.{safe_entry}.lock"
            fh = open(lock_path, "a+", encoding="utf-8")

            # Ensure the lock handle is not inheritable. If a child process inherits
            # this handle, it can appear to "also" hold the singleton lock and we end
            # up with two running bot processes.
            try:
                os.set_inheritable(fh.fileno(), False)
            except Exception:
                pass

            try:
                # Windows: msvcrt lock (non-blocking)
                import msvcrt  # type: ignore

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    try:
                        fh.close()
                    except Exception:
                        pass
                    return False
            except Exception:
                # POSIX: fcntl flock (non-blocking)
                try:
                    import fcntl  # type: ignore

                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        try:
                            fh.close()
                        except Exception:
                            pass
                        return False
                except Exception:
                    # If locking isn't supported, don't block startup.
                    return True

            # Write PID stamp (informational).
            try:
                fh.seek(0)
                fh.truncate(0)
                fh.write(f"pid={os.getpid()}\n")
                fh.flush()
            except Exception:
                pass

            global _BOT_SINGLETON_LOCK
            _BOT_SINGLETON_LOCK = fh
            return True
        except Exception:
            return True

    if not _acquire_bot_singleton_lock():
        print("[FATAL] Another TNT Discord bot process is already running (singleton lock active).")
        print("        Stop the other process or set TNT_ALLOW_MULTIPLE_BOTS=1 to override.")
        raise SystemExit(2)

    # Load repo env files so feature gates work in dev/prod shells.
    # This is intentionally after singleton lock so duplicate runs don't thrash env parsing.
    try:
        _dotenv_load_into_environ()
    except Exception:
        pass

    _print_start_banner()
    _print_env_snapshot()
    try:
        _preflight_filesystem()
    except Exception as exc:
        print(f"[FATAL] Filesystem preflight failed: {exc}")
        raise SystemExit(2)

    token = _resolve_discord_token()
    if not token:
        print("[FATAL] DISCORD_BOT_TOKEN not set (env or .env.local/.env).")
        print("        Set DISCORD_BOT_TOKEN in your shell, or use run_discord_v1_beta.ps1 to manage .env.local.")
        raise SystemExit(1)
    try:
        bot.run(token)
    except KeyboardInterrupt:
        print("[STOP] Keyboard interrupt, shutting down bot")
        raise
    except Exception as exc:  # noqa: BLE001
        # Make the common failure modes actionable (invalid token, intents, gateway).
        print(f"[FATAL] Discord bot failed to start: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
