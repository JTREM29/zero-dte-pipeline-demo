from __future__ import annotations

from services.moderator.moderator_chart_caption import append_chart_moderator_line, render_chart_moderator_line


def test_chart_line_divergent_golden() -> None:
    now = 1_700_000_000
    fut_scores = {
        "regime": "RISK_ON",
        "updated_utc": now - 60,
        "trend": {"ES": 0.90},
        "impulse": {"NQ": -0.10},
    }

    line = render_chart_moderator_line(fut_scores, now_epoch=now)
    assert line == "🧭 Moderator: ⚠️ Futures diverge (Δ=1.00, age=60s)"


def test_chart_caption_appends_once_idempotent() -> None:
    now = 1_700_000_000
    fut_scores = {
        "regime": "RISK_ON",
        "updated_utc": now - 30,
        "trend": {"ES": 0.60},
        "impulse": {"NQ": 0.55},
    }

    base = "Chart caption"
    out1 = append_chart_moderator_line(base, fut_scores, now_epoch=now)
    out2 = append_chart_moderator_line(out1, fut_scores, now_epoch=now)

    assert out1 == "Chart caption\n🧭 Moderator: ✅ Futures aligned (RISK-ON) (Δ=0.05, age=30s)"
    assert out2 == out1
