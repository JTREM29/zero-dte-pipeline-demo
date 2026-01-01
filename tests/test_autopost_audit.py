from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import delivery.discord_bot as bot


@pytest.mark.asyncio
async def test_publish_autopost_render_writes_audit(tmp_path, monkeypatch):
    audit_root = tmp_path / "audit"
    monkeypatch.setattr(bot, "AUTOPOST_AUDIT_ROOT", audit_root)
    monkeypatch.setattr(bot, "_LAST_AUDIT_PRUNE", None, raising=False)
    monkeypatch.setattr(bot, "AUTOPOST_AUDIT_RETENTION_DAYS", 30, raising=False)

    monkeypatch.setattr(bot, "_git_sha", lambda: "test-sha")
    monkeypatch.setattr(bot, "_GIT_SHA_CACHE", "test-sha", raising=False)
    monkeypatch.setattr(bot, "_contract_violations_for_render", lambda *_args, **_kwargs: [])

    tzinfo = bot.ET_TZ or ZoneInfo("America/New_York")
    now_et = datetime(2025, 12, 17, 15, 23, tzinfo=tzinfo)
    monkeypatch.setattr(bot, "_now_et", lambda: now_et)

    async def fake_safe_send(channel, text, **_kwargs):
        return SimpleNamespace(id=456, text=text, channel=channel)

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)

    render = bot.RenderedPost(
        text="Test autopost payload\n_Not financial advice._",
        agent_payload={"meta": {"symbols": ["SPY"], "stand_down": False}},
    )

    class DummyChannel:
        id = 123
        name = "canary"

    await bot._publish_autopost_render(
        DummyChannel(),
        render,
        builder="unit_test_builder",
        label="daily_prep",
        symbol="SPY",
        analysis_mode="db",
        output_mode="strict",
    )

    files = list(audit_root.rglob("*.json"))
    assert len(files) == 1

    payload = json.loads(files[0].read_text(encoding="utf-8"))
    required_keys = {"text", "agent_payload", "git_sha", "contract_version", "metadata"}
    assert required_keys.issubset(payload)
    assert payload["git_sha"] == "test-sha"
    assert payload["contract_version"] == bot.AUTOPOST_CONTRACT_VERSION
    assert payload["metadata"]["symbol_list"] == ["SPY"]
    assert "scenario_flags" in payload["metadata"]