from __future__ import annotations

from services.moderator.moderator_paperdesk_line import render_paperdesk_moderator_line


def test_paperdesk_line_divergent_golden() -> None:
    now = 1_700_000_000
    fut_scores = {
        "regime": "RISK_ON",
        "updated_utc": now - 60,
        "trend": {"ES": 0.90},
        "impulse": {"NQ": -0.10},
        "vol_mult": 1.0,
        "breadth_bearish": 1,
        "breadth_total": 3,
    }

    line = render_paperdesk_moderator_line(direction="BULL", fut_scores=fut_scores, now_epoch=now)
    assert line == "⚠️ Futures diverge (Δ=1.00, age=60s) — Bottom line: DO NOTHING | Watch: Δ<0.20 + reconfirm"


def test_paperdesk_line_confirm_bull_golden() -> None:
    now = 1_700_000_000
    fut_scores = {
        "regime": "RISK_ON",
        "updated_utc": now - 30,
        "trend": {"ES": 0.60},
        "impulse": {"NQ": 0.55},
        "vol_mult": 1.0,
        "breadth_bearish": 0,
        "breadth_total": 3,
    }

    line = render_paperdesk_moderator_line(direction="BULL", fut_scores=fut_scores, now_epoch=now)
    assert line == "✅ Futures confirm BULL (Δ=0.05, age=30s) — Bottom line: SELECTIVE | Watch: keep Δ<0.20"


def test_paperdesk_line_stale_defaults_to_stand_aside() -> None:
    now = 1_700_000_000
    fut_scores = {
        "regime": "RISK_OFF",
        "updated_utc": now - 1_000,
        "trend": {"ES": -0.70},
        "impulse": {"NQ": -0.72},
    }

    line = render_paperdesk_moderator_line(direction="BEAR", fut_scores=fut_scores, now_epoch=now, max_age_sec=120)
    assert line == "🧊 Futures stale (Δ=0.02, age=1000s) — Bottom line: DO NOTHING | Watch: refresh (<120s)"


def test_paperdesk_line_mixed_or_transition_stand_aside() -> None:
    now = 1_700_000_000
    fut_scores = {
        "regime": "CHOP",
        "updated_utc": now - 10,
        "trend": {"ES": 0.10},
        "impulse": {"NQ": 0.12},
    }

    line = render_paperdesk_moderator_line(direction="BULL", fut_scores=fut_scores, tnt_confidence=0.95, now_epoch=now)
    assert line == "🧊 Futures mixed/transition (Δ=0.02, age=10s) — Bottom line: DO NOTHING | Watch: confirm BULL + Δ<0.20"
