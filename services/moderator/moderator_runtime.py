from __future__ import annotations

import os
from typing import Any

from services.context.ctx_mode import get_setting
from services.moderator.moderator_policy import Action, FuturesSignal, ModeratorEnv, decide_policy
from services.moderator.moderator_templates import render_reply


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _strictness_from_env(raw: str) -> str:
    v = str(raw or "").strip().lower()
    if v in {"1", "true", "yes", "on", "strict", "enabled"}:
        return "on"
    if v in {"shadow", "warn", "telemetry"}:
        return "shadow"
    return "off"


def get_moderator_env() -> ModeratorEnv:
    primary = _truthy(get_setting("TNT_PRIMARY_MODERATOR", "0"))
    owner_avail = _truthy(get_setting("TNT_OWNER_AVAILABLE", "0"))
    strictness = _strictness_from_env(get_setting("TNT_MODERATOR_STRICTNESS", "0"))
    return ModeratorEnv(primary_moderator=primary, owner_available=owner_avail, strictness=strictness)  # type: ignore[arg-type]


def _safe_one_line(s: str, *, max_len: int = 220) -> str:
    t = str(s or "").replace("\n", " ").replace("\r", " ").strip()
    if len(t) <= max_len:
        return t
    return t[: max(0, int(max_len) - 1)].rstrip() + "…"


def try_get_futures_signal(*, redis_client: Any) -> FuturesSignal | None:
    """Best-effort futures context fetch for gating; returns None on errors.

    Must remain cache-first and deterministic.
    """

    if redis_client is None:
        return None

    try:
        from services.futures.futures_context_line import compute_context
        from services.futures.futures_store import FuturesStore

        try:
            max_age = int(os.getenv("FUTURES_CONTEXT_MAX_AGE_SEC", "120") or "120")
        except Exception:
            max_age = 120

        now_s = int(__import__("time").time())
        scores = FuturesStore(redis_client).get_scores()
        if not scores:
            return FuturesSignal(state="MISSING")

        ctx = compute_context(scores, now_epoch=now_s, max_age_sec=max_age)
        # ctx.state is already canonical in this repo (OK|AGING|STALE|MISSING)
        state = str(getattr(ctx, "state", "MISSING") or "MISSING").upper()
        if state not in {"OK", "AGING", "STALE", "MISSING"}:
            state = "MISSING"

        return FuturesSignal(
            state=state,  # type: ignore[arg-type]
            trend=str(getattr(ctx, "trend", None) or "") or None,
            mode=str(getattr(ctx, "mode", None) or "") or None,
            divergence_level=str(getattr(ctx, "divergence_level", None) or "") or None,
        )
    except Exception:
        return None


async def maybe_handle_moderator_message(*, message: Any, user_text: str, redis_client: Any) -> bool:
    """If Moderator Mode chooses to reply, send deterministic template and return True.

    Shadow mode logs but does not reply.
    """

    env = get_moderator_env()
    if not env.primary_moderator or env.strictness == "off":
        return False

    futures = try_get_futures_signal(redis_client=redis_client)
    decision = decide_policy(user_text=str(user_text or ""), env=env, futures=futures)

    if decision.action != Action.REPLY_TEMPLATE or decision.template_id is None:
        return False

    if env.strictness == "shadow":
        try:
            print(
                "[TNT][MOD][shadow] bucket={bucket} template={tpl} reason={reason} text={text}".format(
                    bucket=str(decision.bucket),
                    tpl=str(decision.template_id),
                    reason=str(decision.reason),
                    text=_safe_one_line(user_text),
                )
            )
        except Exception:
            pass
        return False

    reply = render_reply(template_id=decision.template_id, env=env)

    try:
        await message.reply(str(reply.text)[:1800], mention_author=False)
    except Exception:
        return False
    return True
