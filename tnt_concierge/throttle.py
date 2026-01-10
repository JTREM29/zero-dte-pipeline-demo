from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional


# --- In-process state (fast, DB-free) ---
_last_user_nudge_ts: dict[int, float] = {}
_last_global_nudge_ts: float = 0.0
_ticker_counts: dict[tuple[str, str], int] = {}


def enabled() -> bool:
    return os.getenv("TNT_CONCIERGE_ENABLED", "0") == "1"


def auto_enabled() -> bool:
    return os.getenv("TNT_CONCIERGE_AUTO", "0") == "1"


def allow_bot_messages_enabled() -> bool:
    return os.getenv("TNT_CONCIERGE_ALLOW_BOT_MESSAGES", "0") == "1"


def allow_other_bot_messages_for_auto() -> bool:
    # Safety default: off.
    return enabled() and auto_enabled() and allow_bot_messages_enabled()


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def _env_csv_set_lower(name: str, default: str = "") -> set[str]:
    raw = (os.getenv(name, default) or "").strip()
    if not raw:
        return set()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def allowed_channel_name(channel_name: Optional[str]) -> bool:
    allowed = _env_csv_set_lower(
        "TNT_CONCIERGE_ALLOWED_CHANNELS",
        "watchlist-alerts,charts,trading-floor",
    )
    if not allowed:
        return False
    if not channel_name:
        return False
    return channel_name.strip().lower() in allowed


_watchlist_cache: tuple[float, set[str]] | None = None
_user_watchlist_cache: tuple[float, dict[int, set[str]]] | None = None


def _watchlist_path() -> Path:
    p = os.getenv("TNT_CONCIERGE_WATCHLIST_PATH", str(Path("config") / "concierge_watchlist.json"))
    return Path(p)


def watchlist() -> set[str]:
    """Active watchlist (uppercased). Priority: file -> env."""
    global _watchlist_cache

    p = _watchlist_path()
    try:
        if p.exists():
            mtime = float(p.stat().st_mtime)
            if _watchlist_cache and _watchlist_cache[0] == mtime:
                return set(_watchlist_cache[1])

            raw = p.read_text(encoding="utf-8")
            payload = json.loads(raw) if raw.strip() else {}
            items: list[str] = []
            if isinstance(payload, list):
                items = [str(x) for x in payload]
            elif isinstance(payload, dict):
                wl = payload.get("watchlist")
                if isinstance(wl, (list, tuple)):
                    items = [str(x) for x in wl]

            out = {s.strip().upper() for s in items if isinstance(s, str) and s.strip()}
            _watchlist_cache = (mtime, out)
            return set(out)
    except Exception:
        # Fall back to env.
        pass

    raw = (os.getenv("TNT_CONCIERGE_WATCHLIST", "") or "").strip()
    if not raw:
        return set()
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


def _user_watchlist_path() -> Path:
    p = os.getenv("TNT_CONCIERGE_USER_WATCHLIST_PATH", str(Path("config") / "concierge_watchlist_users.json"))
    return Path(p)


def watchlist_for_user(user_id: Optional[int]) -> set[str]:
    """Effective watchlist for a user.

    If a user-specific watchlist exists, it is unioned with the global watchlist.
    File format: {"123456": ["SPY","QQQ"], ...}
    """
    global _user_watchlist_cache

    base = watchlist()
    if not user_id:
        return set(base)

    p = _user_watchlist_path()
    try:
        if p.exists():
            mtime = float(p.stat().st_mtime)
            if _user_watchlist_cache and _user_watchlist_cache[0] == mtime:
                user_map = _user_watchlist_cache[1]
            else:
                raw = p.read_text(encoding="utf-8")
                payload = json.loads(raw) if raw.strip() else {}
                user_map = {}
                if isinstance(payload, dict):
                    for k, v in payload.items():
                        try:
                            uid = int(str(k).strip())
                        except Exception:
                            continue
                        if isinstance(v, (list, tuple)):
                            syms = {str(x).strip().upper() for x in v if str(x).strip()}
                            if syms:
                                user_map[uid] = syms
                _user_watchlist_cache = (mtime, user_map)
            return set(base) | set(user_map.get(int(user_id), set()))
    except Exception:
        pass

    return set(base)


def extract_watchlist_ticker(text: str, *, user_id: Optional[int] = None) -> Optional[str]:
    if not text:
        return None
    wl = watchlist_for_user(user_id)
    if not wl:
        return None
    upper = text.upper()
    for sym in wl:
        # Conservative match to avoid false positives.
        if re.search(rf"\b{re.escape(sym)}\b", upper):
            return sym
    return None


def user_frustration(text: str) -> bool:
    t = (text or "").lower()
    return any(
        phrase in t
        for phrase in (
            "stopped out",
            "stop out",
            "stopout",
            "wtf",
            "chop",
            "choppy",
        )
    )


def allow_nudge(
    *,
    user_id: int,
    ticker: str,
    channel_name: Optional[str],
    triggered: bool,
    author_is_bot: bool,
) -> tuple[bool, str, str]:
    """Fast anti-spam gate.

    Returns: (ok, reason, mode)
    mode is "mention" or "auto" when ok, else "none".
    """
    if not enabled():
        return False, "disabled", "none"

    if author_is_bot and not allow_other_bot_messages_for_auto():
        return False, "ignore bot messages", "none"

    mode = "mention" if triggered else "auto"
    if mode == "auto" and not auto_enabled():
        return False, "auto disabled", "none"

    if not allowed_channel_name(channel_name):
        return False, "channel not allowed", "none"

    # Watchlist-only safety.
    if ticker.upper() not in watchlist():
        return False, "not watchlist", "none"

    now = float(time.time())
    global_cd = float(max(_env_int("TNT_CONCIERGE_GLOBAL_COOLDOWN_SEC", 60), 0))
    user_cd = float(max(_env_int("TNT_CONCIERGE_USER_COOLDOWN_SEC", 3600), 0))
    per_day_cap = int(max(_env_int("TNT_CONCIERGE_TICKER_MAX_PER_DAY", 2), 0))

    global _last_global_nudge_ts
    if global_cd > 0 and (now - _last_global_nudge_ts) < global_cd:
        return False, "global cooldown", "none"

    last_user = float(_last_user_nudge_ts.get(int(user_id), 0.0))
    if user_cd > 0 and (now - last_user) < user_cd:
        return False, "user cooldown", "none"

    day_key = time.strftime("%Y-%m-%d", time.localtime(now))
    if per_day_cap > 0:
        key = (ticker.upper(), day_key)
        used = int(_ticker_counts.get(key, 0))
        if used >= per_day_cap:
            return False, "ticker daily cap", "none"

    # Allow + commit
    _last_user_nudge_ts[int(user_id)] = now
    _last_global_nudge_ts = now
    if per_day_cap > 0:
        key = (ticker.upper(), day_key)
        _ticker_counts[key] = int(_ticker_counts.get(key, 0)) + 1

    return True, "ok", mode


def _reset_state_for_tests() -> None:
    global _last_global_nudge_ts
    _last_user_nudge_ts.clear()
    _ticker_counts.clear()
    _last_global_nudge_ts = 0.0
