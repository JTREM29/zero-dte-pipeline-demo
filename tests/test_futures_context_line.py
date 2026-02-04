from __future__ import annotations

from services.futures.futures_context_line import (
    compute_context,
    render_confirmation_badge,
    render_futures_context_line,
)


def test_futures_context_missing_scores() -> None:
    ctx = compute_context(None)
    assert ctx.state == "MISSING"
    assert render_futures_context_line(None) is None


def test_futures_context_stale_scores_are_omitted() -> None:
    scores = {
        "trend": {"ES": 0.4},
        "impulse": {"NQ": 0.4},
        "vol_mult": 1.2,
        "breadth_bearish": 1,
        "breadth_total": 3,
        "regime": "RISK_ON",
        "updated_utc": 1,
    }
    # now_epoch far beyond updated_utc
    assert render_futures_context_line(scores, now_epoch=10_000, max_age_sec=120) is None


def test_futures_confirmation_badge_alignment_only_when_aligned() -> None:
    scores_risk_off = {
        "trend": {"ES": -0.4},
        "impulse": {"NQ": -0.4},
        "vol_mult": 1.3,
        "breadth_bearish": 3,
        "breadth_total": 3,
        "regime": "RISK_OFF",
        "updated_utc": 100,
    }

    ok_bear = render_confirmation_badge(scores_risk_off, direction="BEAR", now_epoch=150, max_age_sec=120)
    not_ok_bull = render_confirmation_badge(scores_risk_off, direction="BULL", now_epoch=150, max_age_sec=120)
    assert ok_bear == "✅ confirms BEAR"
    assert not_ok_bull is None


def test_futures_divergence_strong_warns_and_line_is_canonical() -> None:
    scores = {
        "trend": {"ES": 0.5},
        "impulse": {"NQ": -0.6},
        "vol_mult": 1.0,
        "breadth_bearish": 1,
        "breadth_total": 3,
        "regime": "RISK_ON",
        "updated_utc": 100,
    }

    line = render_futures_context_line(scores, now_epoch=150, max_age_sec=120)
    assert line is not None
    assert line.startswith("📊 Futures: ")
    assert "Δ=" in line
    assert "age=50s" in line

    badge = render_confirmation_badge(scores, direction="BULL", now_epoch=150, max_age_sec=120)
    assert badge == "⚠️ diverges"
