from __future__ import annotations

from datetime import datetime, timezone

import delivery.discord_bot as bot


def _set_fixed_now(dt_utc: datetime) -> None:
    assert dt_utc.tzinfo is not None
    bot._set_now_provider(lambda: dt_utc)


def test_context_alert_state_change_only_and_limits(monkeypatch) -> None:
    # Reset module globals to avoid cross-test bleed.
    bot._last_context_post_by_symbol.clear()
    bot._last_context_state.clear()
    bot._context_global_post_ts.clear()

    # Safe defaults for launch.
    monkeypatch.setenv("AUTOPOST_CONTEXT_COOLDOWN_SEC", "600")
    monkeypatch.setenv("AUTOPOST_CONTEXT_GLOBAL_MAX_PER_HOUR", "2")
    monkeypatch.setenv("AUTOPOST_CONTEXT_POST_ON_SEED", "0")

    dt0 = datetime(2026, 1, 26, 15, 0, tzinfo=timezone.utc)
    _set_fixed_now(dt0)

    base = {
        "regime": "BALANCED",
        "extension_mode": False,
        "pivot": 100.0,
        "last_price": 100.0,
        "bias_confirm": "NEUTRAL",
        "tnt_posture": "DO_NOTHING",
    }

    # No previous state => state-change-only suppresses seed post.
    ok, reason = bot._should_post_context("SPY", dict(base))
    assert (ok, reason) == (False, "seed")

    # Seed the prior state.
    bot._last_context_state["SPY"] = dict(base)

    changed = dict(base)
    changed["regime"] = "BULLISH"
    ok, reason = bot._should_post_context("SPY", changed)
    assert ok is True
    assert reason == "regime changed"

    # Simulate that we posted now (cooldown blocks immediate repeats).
    now_s = int(bot._now_utc().timestamp())
    bot._last_context_post_by_symbol["SPY"] = now_s

    changed2 = dict(changed)
    changed2["regime"] = "BEARISH"
    ok, reason = bot._should_post_context("SPY", changed2)
    assert ok is False
    assert reason.startswith("cooldown ")

    # Global cap blocks once the hourly budget is used.
    # Advance time beyond cooldown.
    dt1 = datetime(2026, 1, 26, 15, 20, tzinfo=timezone.utc)
    _set_fixed_now(dt1)

    # Set prior state for another symbol so it can trigger.
    bot._last_context_state["QQQ"] = dict(base)
    qqq_changed = dict(base)
    qqq_changed["regime"] = "BULLISH"

    # Fill cap (2/hour).
    now1_s = int(bot._now_utc().timestamp())
    bot._context_global_post_ts.extend([now1_s - 10, now1_s - 5])

    ok, reason = bot._should_post_context("QQQ", qqq_changed)
    assert (ok, reason) == (False, "global cap 2/h")
