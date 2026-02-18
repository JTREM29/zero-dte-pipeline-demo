from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .templates import ConciergeContext, LockState, concierge_reply
from . import throttle
from . import lock as decision_lock
from .memory import memory_hint


def _audit_path() -> Path:
    return Path(os.getenv("TNT_CONCIERGE_AUDIT_PATH", str(Path("logs") / "concierge_events.jsonl")))


def _append_audit_event(event: dict) -> None:
    try:
        p = _audit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        p.open("a", encoding="utf-8").write(line + "\n")
    except Exception:
        return


def _regime_from_trend_vol(*, trend: Optional[float], vol_z: Optional[float]) -> str:
    if trend is None or vol_z is None:
        return "UNKNOWN"
    try:
        tr = float(trend)
        vz = float(vol_z)
    except Exception:
        return "UNKNOWN"
    if vz >= 1.5:
        return "EXPANSION"
    if abs(tr) >= 0.004:
        return "TREND"
    if abs(tr) <= 0.0015 and vz <= 0.8:
        return "CHOP"
    return "UNKNOWN"


async def _send_concierge(
    *,
    message: Any,
    ticker: str,
    triggered: bool,
    mode: str,
    now_et_fn: Callable[[], datetime],
    get_last_price_snapshot: Callable[[str], Any],
    get_latest_signal_context: Callable[[str], tuple[list[str], Any]],
    get_vix_context: Callable[[str], Optional[dict]],
    vix_gating_action: Callable[[float], dict],
    vix_gating_enabled: bool,
    safe_send: Callable[..., Awaitable[Any]],
) -> None:
    try:
        sym = (ticker or "").strip().upper()
        if not sym:
            return

        snap = get_last_price_snapshot(sym)
        px = getattr(snap, "px", None)
        if px is None:
            px = 0.0

        trend_val: Optional[float] = None
        vol_z_val: Optional[float] = None
        try:
            cols, sig = get_latest_signal_context(sym)
            if sig:
                data = {col: sig[idx] for idx, col in enumerate(cols)}
                tr = data.get("trend")
                vz = data.get("vol_z")
                trend_val = float(tr) if tr is not None else None
                vol_z_val = float(vz) if vz is not None else None
        except Exception:
            pass

        regime = _regime_from_trend_vol(trend=trend_val, vol_z=vol_z_val)

        lock = LockState.SOFT
        lock_reason = ""
        try:
            vix = get_vix_context("1m")
            if vix and vix_gating_enabled:
                gate = vix_gating_action(float(vix["level"]))
                mode_vix = str(gate["mode"])
                lock_reason = str(gate.get("reason", "") or "")
                if mode_vix.upper() == "HARD":
                    lock = LockState.HARD
        except Exception:
            pass

        now_et = now_et_fn()
        late_day = bool(now_et.hour >= 15)

        ctx = ConciergeContext(
            ticker=sym,
            price=float(px),
            regime=regime,
            user_frustration=throttle.user_frustration(getattr(message, "content", "") or ""),
            late_day=late_day,
            decision_lock=lock,
        )

        tid, msg = concierge_reply(ctx)
        hint = memory_hint(ticker=sym)
        if hint:
            msg = msg + "\n\n_" + hint + "_"
        ch = getattr(message, "channel", None)
        ch_name = getattr(ch, "name", None)
        ch_name_s = ch_name if isinstance(ch_name, str) else "(unknown)"
        guild = getattr(message, "guild", None)

        # Persist Decision Lock for downstream (LLM) handling.
        # Conservative: only meaningful for HARD right now.
        if lock == LockState.HARD:
            decision_lock.set_lock(
                guild_id=getattr(guild, "id", None),
                channel_id=getattr(ch, "id", None),
                state=lock,
                reason=lock_reason or "Decision Lock: HARD",
            )

        _append_audit_event(
            {
                "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "template": tid.value,
                "ticker": sym,
                "mode": mode,
                "triggered": bool(triggered),
                "decision_lock": lock.value,
                "decision_lock_reason": lock_reason,
                "channel_id": getattr(ch, "id", None),
                "channel_name": ch_name_s,
                "guild_id": getattr(guild, "id", None),
                "user_id": getattr(getattr(message, "author", None), "id", None),
            }
        )
        print(
            f"[CONCIERGE] fired template={tid.value} ticker={sym} mode={mode} channel={ch_name_s} user={getattr(getattr(message, 'author', None), 'id', 'n/a')}"
        )

        if ch is None:
            return

        await safe_send(
            ch,
            msg,
            kind="text",
            symbol=sym,
            output_mode="strict",
            label=f"concierge_{tid.value}",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[CONCIERGE][ERROR] {type(exc).__name__}: {exc}")


def schedule_concierge_nudge(
    message: Any,
    *,
    triggered: bool,
    now_et_fn: Callable[[], datetime],
    get_last_price_snapshot: Callable[[str], Any],
    get_latest_signal_context: Callable[[str], tuple[list[str], Any]],
    get_vix_context: Callable[[str], Optional[dict]],
    vix_gating_action: Callable[[float], dict],
    vix_gating_enabled: bool,
    safe_send: Callable[..., Awaitable[Any]],
) -> bool:
    """Non-blocking Concierge hook.

    Returns True if a nudge was scheduled.
    """
    try:
        content = getattr(message, "content", "") or ""
        author = getattr(message, "author", None)
        user_id = int(getattr(author, "id", 0) or 0)
        ticker = throttle.extract_watchlist_ticker(content, user_id=user_id)
        if not ticker:
            return False

        ch = getattr(message, "channel", None)
        ch_name = getattr(ch, "name", None)

        author_is_bot = bool(getattr(author, "bot", False))

        ok, _reason, mode = throttle.allow_nudge(
            user_id=user_id,
            ticker=ticker,
            channel_name=ch_name if isinstance(ch_name, str) else None,
            triggered=bool(triggered),
            author_is_bot=author_is_bot,
        )
        if not ok:
            return False

        asyncio.create_task(
            _send_concierge(
                message=message,
                ticker=ticker,
                triggered=bool(triggered),
                mode=mode,
                now_et_fn=now_et_fn,
                get_last_price_snapshot=get_last_price_snapshot,
                get_latest_signal_context=get_latest_signal_context,
                get_vix_context=get_vix_context,
                vix_gating_action=vix_gating_action,
                vix_gating_enabled=bool(vix_gating_enabled),
                safe_send=safe_send,
            )
        )
        return True
    except Exception:
        return False
