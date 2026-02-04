from __future__ import annotations

from services.calendar.earnings_embeds import build_earnings_embed


def test_embed_renders_risk_shape_when_present() -> None:
    ev = {
        "ts_utc": "2026-01-31T21:00:00Z",
        "confirmed": True,
        "session": "AMC",
        "expected_move_pct": 6.7,
        "liquidity_risk": "MED",
        "expected_move": {
            "underlying": 657.68,
            "straddle": 44.02,
            "upper": 702.02,
            "lower": 613.98,
            "wings": {
                "down_5": {"strike": 655, "call_mid": 23.4, "put_mid": 20.1},
                "up_5": {"strike": 665, "call_mid": 19.2, "put_mid": 25.0},
            },
            "risk_shape": {"label": "UPSIDE_SKEW", "score": 0.14},
        },
    }

    e = build_earnings_embed("SPY", ev, refreshed_utc=None)
    impact = next((f for f in e.fields if f.name == "Impact"), None)
    assert impact is not None
    assert "Risk shape" in impact.value
    assert "Skewed upside" in impact.value


def test_embed_renders_risk_shape_unavailable_when_missing_wings() -> None:
    ev = {
        "ts_utc": "2026-01-31T21:00:00Z",
        "confirmed": True,
        "session": "AMC",
        "expected_move_pct": 6.7,
        "liquidity_risk": "MED",
        "expected_move": {
            "underlying": 657.68,
            "straddle": 44.02,
            "upper": 702.02,
            "lower": 613.98,
            "wings": {},
        },
    }

    e = build_earnings_embed("SPY", ev, refreshed_utc=None)
    impact = next((f for f in e.fields if f.name == "Impact"), None)
    assert impact is not None
    assert "Risk shape: unavailable" in impact.value
