"""Tests for the lightweight morning report builder."""
from __future__ import annotations

from types import SimpleNamespace

from zero_dte_pipeline.reports import morning_report_simple as mr


def test_build_morning_report_dict(monkeypatch):
    """Ensure the builder returns dict output and posts to Discord when requested."""

    snapshot = mr.Snapshot(
        symbol="SPX",
        price=5000.0,
        open_price=4950.0,
        change_pct=1.0,
        expected_move=45.0,
        provenance="test",
    )
    context = mr.MorningReportContext(
        snapshot=snapshot,
        regime_label="TREND_UP",
        regime_note="test note",
        es_direction="BULLISH",
        news_headlines=[],
        headlines_summary="",
        candidates=[{"description": "test structure", "score": 0.9}],
    )

    monkeypatch.setattr(mr, "_build_context", lambda symbol, primary_expiration=None: context)
    monkeypatch.setattr(
        mr,
        "get_openai_client",
        lambda: SimpleNamespace(summarize_morning_brief=lambda text: "TLDR"),
    )

    posted = {}

    def _fake_post(content: str, username: str) -> bool:
        posted["content"] = content
        posted["username"] = username
        return True

    monkeypatch.setattr(mr, "post_discord_message", _fake_post)

    result = mr.build_morning_report("SPX", as_dict=True, post_to_discord=True)

    assert result["ok"] is True
    assert result["symbol"] == "SPX"
    assert result["tldr"] == "TLDR"
    assert posted["content"] == "TLDR"
    assert posted["username"] == "ZeroDTE Morning Bot"
    assert result["meta"]["candidate_cap"] == mr.config.max_morning_candidates
    assert result["text"].startswith("🔔 Morning Report")
