from __future__ import annotations

from services.observability.uncertainty_canary import UncertaintyCanary, evaluate_uncertainty


def test_evaluate_uncertainty_divergent_elevated() -> None:
    now = 1_700_000_000
    fut_scores = {
        "regime": "RISK_ON",
        "updated_utc": now - 30,
        "trend": {"ES": 0.90},
        "impulse": {"NQ": -0.10},
    }

    d = evaluate_uncertainty(fut_scores, now_epoch=now, max_age_sec=120)
    assert d.level == "ELEVATED"
    assert "divergent" in d.key
    assert d.message == "🛟 Canary: elevated uncertainty (divergent) — Futures RISK-ON/trend Δ=1.00 age=30s"


def test_uncertainty_canary_state_change_only_and_rate_limited() -> None:
    canary = UncertaintyCanary(min_post_sec=300)

    t0 = 1_700_000_000
    divergent = {
        "regime": "RISK_ON",
        "updated_utc": t0 - 10,
        "trend": {"ES": 0.90},
        "impulse": {"NQ": -0.10},
    }

    msg1 = canary.maybe_message(divergent, now_epoch=t0, max_age_sec=120)
    assert msg1 == "🛟 Canary: elevated uncertainty (divergent) — Futures RISK-ON/trend Δ=1.00 age=10s"

    # While still elevated: no repeat (state-change-only).
    assert canary.maybe_message(divergent, now_epoch=t0 + 100, max_age_sec=120) is None

    # Return to NORMAL resets the state.
    normal = {
        "regime": "RISK_ON",
        "updated_utc": t0 + 50 - 5,
        "trend": {"ES": 0.20},
        "impulse": {"NQ": 0.22},
    }
    assert canary.maybe_message(normal, now_epoch=t0 + 50, max_age_sec=120) is None

    # Different elevated key, but within rate limit: suppressed.
    transition_early = {
        "regime": "NEUTRAL",
        "updated_utc": t0 + 50 - 10,
        "trend": {"ES": 0.00},
        "impulse": {"NQ": 0.30},
    }
    assert canary.maybe_message(transition_early, now_epoch=t0 + 50, max_age_sec=120) is None

    # After rate limit: allow the new state.
    transition_late = {
        "regime": "NEUTRAL",
        "updated_utc": t0 + 301 - 10,
        "trend": {"ES": 0.00},
        "impulse": {"NQ": 0.30},
    }
    msg2 = canary.maybe_message(transition_late, now_epoch=t0 + 301, max_age_sec=120)
    assert msg2 == "🛟 Canary: elevated uncertainty (transition) — Futures MIXED/transition Δ=0.30 age=10s"
