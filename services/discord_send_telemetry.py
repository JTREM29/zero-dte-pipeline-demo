from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any


_KEY_LAST_SEND = "tnt:discord:last_send"
_KEY_LAST_ATTEMPT = "tnt:discord:last_attempt"

# Cache a redis client for best-effort, low-latency telemetry writes.
# We keep this extremely small and fail-open.
_CACHED_REDIS = None
_CACHED_REDIS_AT = 0.0
_CACHED_REDIS_TTL_S = 60.0


def _best_effort_git_short_sha() -> str:
    """Return a short git SHA without shelling out (best-effort).

    This is used purely for ops receipts; it must be fast and fail-open.
    """

    # 1) Explicit env override(s) win.
    for k in ("TNT_BOT_BUILD", "TNT_BUILD", "GIT_SHA", "GITHUB_SHA"):
        v = (os.getenv(k, "") or "").strip()
        if v:
            return v[:12]

    # 2) Try reading .git/HEAD and resolving refs.
    try:
        root = os.path.abspath(os.getcwd())
        git_dir = os.path.join(root, ".git")
        head_path = os.path.join(git_dir, "HEAD")
        if not os.path.isfile(head_path):
            return ""
        with open(head_path, "r", encoding="utf-8") as h:
            head = (h.read() or "").strip()
        if not head:
            return ""
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[-1].strip()
            ref_path = os.path.join(git_dir, ref.replace("/", os.sep))
            if os.path.isfile(ref_path):
                with open(ref_path, "r", encoding="utf-8") as rh:
                    sha = (rh.read() or "").strip()
                    return sha[:12] if sha else ""
            # Fallback: packed-refs
            packed = os.path.join(git_dir, "packed-refs")
            if os.path.isfile(packed):
                try:
                    with open(packed, "r", encoding="utf-8") as ph:
                        for line in ph:
                            ln = (line or "").strip()
                            if not ln or ln.startswith("#") or ln.startswith("^"):
                                continue
                            parts = ln.split(" ")
                            if len(parts) == 2 and parts[1].strip() == ref:
                                return parts[0].strip()[:12]
                except Exception:
                    return ""
            return ""
        # Detached HEAD: HEAD contains the sha.
        return head[:12]
    except Exception:
        return ""


# Cache build id once per process.
_BOT_BUILD = _best_effort_git_short_sha()


def _now_utc_iso_z() -> str:
    try:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _resolve_redis_client(redis_client):
    global _CACHED_REDIS, _CACHED_REDIS_AT

    if redis_client is not None:
        return redis_client

    now = time.time()
    if _CACHED_REDIS is not None and (now - float(_CACHED_REDIS_AT)) <= float(_CACHED_REDIS_TTL_S):
        return _CACHED_REDIS

    # Try TNT redis client first (common path).
    try:
        from tnt_redis import redis_client as _tnt_redis_client

        rc = _tnt_redis_client()
        _CACHED_REDIS = rc
        _CACHED_REDIS_AT = now
        return rc
    except Exception:
        pass

    # Fall back to services env helper.
    try:
        from services.redis_env import redis_client as _env_redis_client

        rc2 = _env_redis_client(timeout_s=1.0, decode_responses=True)
        _CACHED_REDIS = rc2
        _CACHED_REDIS_AT = now
        return rc2
    except Exception:
        return None


def record_discord_last_send(
    redis_client,
    *,
    kind: str,
    channel_id: int | str | None,
    guild_id: int | str | None = None,
    channel_name: str | None = None,
    msg_id: int | str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write the single source of truth for Discord send telemetry.

    Contract:
    - Call only after Discord confirms the send/edit succeeded.
    - Best-effort; never raises.
    """

    r = _resolve_redis_client(redis_client)
    if r is None:
        return

    payload: dict[str, Any] = {
        "ts_utc": _now_utc_iso_z(),
        "kind": str(kind or "").strip().upper() or "UNKNOWN",
        "channel_id": str(channel_id) if channel_id not in (None, "") else "",
        "guild_id": str(guild_id) if guild_id not in (None, "") else "",
        "channel_name": str(channel_name or "").strip() if channel_name else "",
        "msg_id": str(msg_id) if msg_id not in (None, "") else "",
    }
    if _BOT_BUILD:
        payload["bot_build"] = _BOT_BUILD
    if isinstance(extra, dict) and extra:
        # Keep small; consumers should tolerate unknown fields.
        payload["extra"] = extra

    try:
        r.set(_KEY_LAST_SEND, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    except Exception:
        return


def record_discord_last_attempt(
    redis_client,
    *,
    kind: str,
    channel_id: int | str | None,
    guild_id: int | str | None = None,
    channel_name: str | None = None,
    error_code: str | None = None,
    error: str | None = None,
    msg_id: int | str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write best-effort attempt telemetry for Discord sends.

    Contract:
    - This is for failures/forbidden paths (not success).
    - It must not mutate the success SSoT.
    - Best-effort; never raises.
    """

    r = _resolve_redis_client(redis_client)
    if r is None:
        return

    safe_err = (str(error) if error is not None else "").strip()
    if len(safe_err) > 220:
        safe_err = safe_err[:220]

    payload: dict[str, Any] = {
        "ts_utc": _now_utc_iso_z(),
        "kind": str(kind or "").strip().upper() or "UNKNOWN",
        "channel_id": str(channel_id) if channel_id not in (None, "") else "",
        "guild_id": str(guild_id) if guild_id not in (None, "") else "",
        "channel_name": str(channel_name or "").strip() if channel_name else "",
        "msg_id": str(msg_id) if msg_id not in (None, "") else "",
        "error_code": str(error_code or "").strip() or "",
        "error": safe_err,
    }
    if _BOT_BUILD:
        payload["bot_build"] = _BOT_BUILD
    if isinstance(extra, dict) and extra:
        payload["extra"] = extra

    try:
        r.set(_KEY_LAST_ATTEMPT, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    except Exception:
        return
