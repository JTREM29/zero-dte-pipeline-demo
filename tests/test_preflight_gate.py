import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


def test_preflight_integrity_mismatch_forces_standdown_only(monkeypatch, tmp_path):
    import delivery.discord_bot as bot

    sent = {}

    async def fake_safe_send(*args, **kwargs):
        # args: (channel, content, ...)
        sent["content"] = args[1]

        class _Msg:
            id = 123

        return _Msg()

    def fake_write_audit(**kwargs):
        sent["audit"] = kwargs

    # Force integrity conflict without relying on market data.
    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (True, "forced"))
    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)

    render = bot.RenderedPost(
        text="THIS SHOULD NEVER POST",
        agent_payload={"meta": {"data_health": "OK"}},
    )

    _run(
        bot._publish_autopost_render(
            channel=None,
            render=render,
            builder="unit_test",
            label="focus_list",
            symbol="SPY",
            context_mode="live",
            context_output_mode="discord",
        )
    )

    assert sent["content"] == bot.PREFLIGHT_INTEGRITY_STANDDOWN_TEXT
    assert sent["audit"]["status"].startswith("stand_down_integrity")
    assert "DATA_INTEGRITY_CONFLICT" in sent["audit"]["violations"]


def test_preflight_forbidden_vendor_label_blocks_publish(monkeypatch):
    import delivery.discord_bot as bot

    called = {"send": 0, "audit": 0}

    async def fake_safe_send(*args, **kwargs):
        called["send"] += 1

        class _Msg:
            id = 1

        return _Msg()

    def fake_write_audit(**kwargs):
        called["audit"] += 1

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)
    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (False, None))

    # Trigger forbidden substring list (legacy/vendor label).
    render = bot.RenderedPost(text="Smart Market IQ", agent_payload={})

    try:
        _run(
            bot._publish_autopost_render(
                channel=None,
                render=render,
                builder="unit_test",
                label="focus_list",
                symbol="SPY",
                context_mode="live",
                context_output_mode="discord",
            )
        )
    except Exception:
        # Expected: should be blocked by violations.
        pass

    assert called["send"] == 0
    # Audit may or may not be written on hard-fail depending on existing behavior;
    # but it must never send.


def test_preflight_blocks_iqfeed_word(monkeypatch):
    import delivery.discord_bot as bot

    called = {"send": 0, "audit": None}

    async def fake_safe_send(*args, **kwargs):
        called["send"] += 1

    def fake_write_audit(**kwargs):
        called["audit"] = kwargs

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)
    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (False, None))

    render = bot.RenderedPost(text="Source: IQFeed\n_Not financial advice._", agent_payload={"meta": {"symbols": ["SPY"]}})

    with pytest.raises(bot.ContractViolationError):
        _run(
            bot._publish_autopost_render(
                channel=None,
                render=render,
                builder="unit_test",
                label="focus_list",
                symbol="SPY",
                context_mode="live",
                context_output_mode="discord",
            )
        )

    assert called["send"] == 0
    assert called["audit"] is not None
    assert any(str(v).startswith("FORBIDDEN_TEXT:iqfeed") for v in called["audit"]["violations"])


def test_preflight_blocks_options_contract_pattern(monkeypatch):
    import delivery.discord_bot as bot

    called = {"send": 0, "audit": None}

    async def fake_safe_send(*args, **kwargs):
        called["send"] += 1

    def fake_write_audit(**kwargs):
        called["audit"] = kwargs

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)
    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (False, None))

    render = bot.RenderedPost(text="SPX 4800c looks active\n_Not financial advice._", agent_payload={"meta": {"symbols": ["SPY"]}})

    with pytest.raises(bot.ContractViolationError):
        _run(
            bot._publish_autopost_render(
                channel=None,
                render=render,
                builder="unit_test",
                label="focus_list",
                symbol="SPY",
                context_mode="live",
                context_output_mode="discord",
            )
        )

    assert called["send"] == 0
    assert called["audit"] is not None
    assert "FORBIDDEN_TEXT:OPTIONS_CONTRACT_PATTERN" in called["audit"]["violations"]


def test_preflight_blocks_chart_label_buy_here(monkeypatch):
    import delivery.discord_bot as bot

    called = {"send": 0, "audit": None}

    async def fake_safe_send(*args, **kwargs):
        called["send"] += 1

    def fake_write_audit(**kwargs):
        called["audit"] = kwargs

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)
    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (False, None))

    chart_spec = {
        "template": "DECISION_ZONE",
        "timeframe": "1D",
        "zones": [
            {"label": "Support zone", "price": 100.0},
            {"label": "Buy here", "price": 101.0},
        ],
        "gates": {
            "bull": {"label": "Bullish posture permitted only above acceptance"},
        },
    }

    render = bot.RenderedPost(
        text="Chart attached\n_Not financial advice._",
        agent_payload={"meta": {"symbols": ["SPY"]}, "chart_spec": chart_spec},
    )

    with pytest.raises(bot.ContractViolationError):
        _run(
            bot._publish_autopost_render(
                channel=None,
                render=render,
                builder="unit_test",
                label="intraday_update",
                symbol="SPY",
                context_mode="live",
                context_output_mode="discord",
            )
        )

    assert called["send"] == 0
    assert called["audit"] is not None
    assert any(str(v).startswith("CHART_SPEC:FORBIDDEN_LABEL_TOKEN:buy") for v in called["audit"]["violations"])
    assert any(str(v).startswith("CHART_SPEC:INVALID_LABEL:Buy here") for v in called["audit"]["violations"])


def test_integrity_mismatch_real_path_forces_standdown_only(monkeypatch):
    import delivery.discord_bot as bot
    from types import SimpleNamespace

    sent = {}

    async def fake_safe_send(*args, **kwargs):
        sent["content"] = args[1]

        class _Msg:
            id = 999

        return _Msg()

    def fake_write_audit(**kwargs):
        sent["audit"] = kwargs

    # Drive the real mismatch detector by stubbing its data sources.
    monkeypatch.setattr(bot, "_get_last_price_snapshot", lambda sym: SimpleNamespace(px=678.0))
    monkeypatch.setattr(bot, "get_latest_daily_pivots", lambda sym: {"piv": {"P": 4832.0}})
    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)

    render = bot.RenderedPost(
        text="Whatever\n_Not financial advice._",
        agent_payload={"meta": {"symbols": ["SPY"]}},
    )

    _run(
        bot._publish_autopost_render(
            channel=None,
            render=render,
            builder="unit_test",
            label="daily_prep",
            symbol="SPY",
            context_mode="live",
            context_output_mode="discord",
        )
    )

    assert sent["content"] == bot.PREFLIGHT_INTEGRITY_STANDDOWN_TEXT
    assert str(sent["audit"]["status"]).startswith("stand_down_integrity")
    assert "DATA_INTEGRITY_CONFLICT" in sent["audit"]["violations"]
