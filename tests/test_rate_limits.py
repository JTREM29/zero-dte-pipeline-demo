import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


class _FakeMsg:
    def __init__(self, mid=1):
        self.id = mid


class _OpsChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return _FakeMsg(999)


class _FakeClient:
    def __init__(self, ops_channel):
        self._ops = ops_channel

    def get_channel(self, channel_id):
        return self._ops


def test_rate_limit_queues_second_publish(monkeypatch):
    import delivery.discord_bot as bot

    # Prevent background pump task in unit tests.
    monkeypatch.setattr(bot, "start_burst_queue_loop", lambda: None)

    # Reset shared limiter state.
    bot._LAST_BY_LABEL.clear()
    bot._LAST_POSTURE_BY_SYMBOL.clear()
    bot._CHART_SEND_TS.clear()
    bot._LAST_USER_CHART_TS.clear()
    bot._LAST_USER_TEXT_TS.clear()
    bot._QUEUE_HEAP.clear()
    bot._QUEUE_BY_KEY.clear()

    # Small window so the second post must queue.
    monkeypatch.setattr(bot, "_RATE_LABEL_MIN_SEC", 60.0)
    monkeypatch.setattr(bot, "_RATE_QUEUE_ENABLED", True)

    now = {"t": 1000.0}
    monkeypatch.setattr(bot, "_now_ts", lambda: float(now["t"]))

    async def fake_safe_send(*args, **kwargs):
        return _FakeMsg(123)

    audits = []

    def fake_write_audit(**kwargs):
        audits.append(kwargs)
        return "audit.json"

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)
    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (False, None))

    render = bot.RenderedPost(text="ok", agent_payload={})
    dummy_channel = object()

    _run(
        bot._publish_autopost_render(
            channel=dummy_channel,
            render=render,
            builder="unit",
            label="daily_prep",
            symbol="SPY",
            request_kind="autopost",
            queue_mode="raise",
        )
    )

    now["t"] = 1001.0
    with pytest.raises(bot.PublishQueuedError):
        _run(
            bot._publish_autopost_render(
                channel=dummy_channel,
                render=render,
                builder="unit",
                label="daily_prep",
                symbol="SPY",
                request_kind="autopost",
                queue_mode="raise",
            )
        )

    # Confirm we recorded a queue audit.
    assert any(a.get("status") == "queued_rate_limit" for a in audits)
    # Queue should have at least one item.
    assert bot._QUEUE_BY_KEY


def test_burst_queue_executes_when_due(monkeypatch):
    import delivery.discord_bot as bot

    monkeypatch.setattr(bot, "start_burst_queue_loop", lambda: None)

    bot._LAST_BY_LABEL.clear()
    bot._LAST_POSTURE_BY_SYMBOL.clear()
    bot._CHART_SEND_TS.clear()
    bot._LAST_USER_CHART_TS.clear()
    bot._LAST_USER_TEXT_TS.clear()
    bot._QUEUE_HEAP.clear()
    bot._QUEUE_BY_KEY.clear()

    monkeypatch.setattr(bot, "_RATE_LABEL_MIN_SEC", 60.0)
    monkeypatch.setattr(bot, "_RATE_QUEUE_ENABLED", True)

    now = {"t": 2000.0}
    monkeypatch.setattr(bot, "_now_ts", lambda: float(now["t"]))

    sent = {"count": 0}

    async def fake_safe_send(*args, **kwargs):
        sent["count"] += 1
        return _FakeMsg(sent["count"])

    def fake_write_audit(**kwargs):
        return "audit.json"

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)
    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (False, None))

    render = bot.RenderedPost(text="ok", agent_payload={})
    dummy_channel = object()

    # First send
    _run(
        bot._publish_autopost_render(
            channel=dummy_channel,
            render=render,
            builder="unit",
            label="daily_prep",
            symbol="SPY",
            request_kind="autopost",
        )
    )

    # Second should queue
    now["t"] = 2001.0
    with pytest.raises(bot.PublishQueuedError):
        _run(
            bot._publish_autopost_render(
                channel=dummy_channel,
                render=render,
                builder="unit",
                label="daily_prep",
                symbol="SPY",
                request_kind="autopost",
                queue_mode="raise",
            )
        )

    assert sent["count"] == 1

    # Execute the queued publish manually (simulate queue pump when due)
    key = next(iter(bot._QUEUE_BY_KEY.keys()))
    qp = bot._QUEUE_BY_KEY[key]
    now["t"] = qp.due_ts + 1.0

    _run(qp.coro_factory())

    assert sent["count"] == 2


def test_quiet_ops_notified_on_block(monkeypatch):
    import delivery.discord_bot as bot

    monkeypatch.setattr(bot, "start_burst_queue_loop", lambda: None)
    bot._QUEUE_HEAP.clear()
    bot._QUEUE_BY_KEY.clear()

    ops = _OpsChannel()
    client = _FakeClient(ops)

    monkeypatch.setattr(bot, "_preflight_price_levels_conflict", lambda symbols: (False, None))

    async def fake_safe_send(*args, **kwargs):
        raise AssertionError("safe_send should not be called on strict block")

    def fake_write_audit(**kwargs):
        return "audit.json"

    monkeypatch.setattr(bot, "safe_send", fake_safe_send)
    monkeypatch.setattr(bot, "_write_autopost_audit", fake_write_audit)

    render = bot.RenderedPost(text="IQFeed", agent_payload={"meta": {"contracts": {"agent": "v1.0", "chart": "1.0"}}})

    with pytest.raises(bot.ContractViolationError):
        _run(
            bot._publish_autopost_render(
                channel=None,
                render=render,
                builder="unit",
                label="focus_list",
                symbol="SPY",
                request_kind="autopost",
                client=client,
            )
        )

    assert ops.sent
    msg = ops.sent[-1]
    assert msg.startswith("[blocked]")
    assert "label=focus_list" in msg
    assert "symbols=SPY" in msg
    assert "status=blocked" in msg
    assert "v(agent)=v1.0" in msg
    assert "v(chart)=1.0" in msg
    assert "audit=" in msg
