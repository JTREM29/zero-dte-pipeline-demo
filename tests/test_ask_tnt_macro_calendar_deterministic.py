import pytest


class _FakeAuthor:
    def __init__(self, *, user_id: int = 123, is_bot: bool = False) -> None:
        self.id = int(user_id)
        self.bot = bool(is_bot)


class _FakeChannel:
    def __init__(self, *, channel_id: int = 1, name: str = "ask-tnt") -> None:
        self.id = int(channel_id)
        self.name = str(name)


class _FakeMessage:
    def __init__(self, content: str, *, channel: _FakeChannel | None = None, author: _FakeAuthor | None = None) -> None:
        self.content = content
        self.channel = channel or _FakeChannel()
        self.author = author or _FakeAuthor()
        self._replies: list[str] = []

    @property
    def replies(self) -> list[str]:
        return list(self._replies)

    async def reply(self, content: str, *, mention_author: bool = False) -> None:  # noqa: ARG002
        self._replies.append(str(content))


@pytest.mark.asyncio
async def test_ask_tnt_next_cpi_reply_is_deterministic(monkeypatch):
    # Exercise the channel-only Ask-TNT router deterministically.
    monkeypatch.setenv("TNT_ASK_DEBUG", "0")

    import cli.discord_bot as botmod

    # Ensure the Ask router is enabled for our fake channel id, regardless of any
    # local dev env (.env/.env.local) configuration.
    from services import ask_tnt_router

    monkeypatch.setattr(
        ask_tnt_router,
        "load_ask_config",
        lambda: ask_tnt_router.AskConfig(ask_channel_id=1, cooldown_sec=0, enabled=True),
    )

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)

    from datetime import datetime, timezone

    now = datetime(2026, 1, 29, 14, 0, tzinfo=timezone.utc)
    items = [
        {
            "ts_utc": datetime(2026, 1, 29, 13, 30, tzinfo=timezone.utc),
            "type": "CPI",
            "title": "CPI (YoY)",
            "impact": "HIGH",
            "source": "csv",
        },
        {
            "ts_utc": datetime(2026, 1, 30, 19, 0, tzinfo=timezone.utc),
            "type": "FOMC",
            "title": "FOMC Rate Decision",
            "impact": "HIGH",
            "source": "csv",
        },
    ]

    # Patch the actual source used by the Ask router.
    from services import macro_calendar
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    def _fake_events(_now_et, horizon_days=14):  # noqa: ARG001
        def _mk(ts_utc, title, impact):
            dt_et = ts_utc.astimezone(ET)
            return type(
                "Evt",
                (),
                {"dt_et": dt_et, "title": title, "importance": impact, "type": "macro", "source": "test"},
            )()

        return [
            _mk(items[0]["ts_utc"], items[0]["title"], items[0]["impact"]),
            _mk(items[1]["ts_utc"], items[1]["title"], items[1]["impact"]),
        ]

    monkeypatch.setattr(macro_calendar, "get_next_macro_events", _fake_events)
    monkeypatch.setattr(macro_calendar, "macro_blackout_state", lambda _now_et: {"active": False})

    msg = _FakeMessage("next cpi")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    out = msg.replies[0]
    assert out.startswith("**Next macro catalysts (ET)**")
    assert "CPI" in out
    assert "FOMC" in out


@pytest.mark.asyncio
async def test_ask_tnt_macro_list_reply(monkeypatch):
    # Tests should not depend on local dev routing env vars.
    monkeypatch.setenv("TNT_ASK_CHANNEL_ONLY_ENABLED", "0")
    monkeypatch.delenv("TNT_ASK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("ASK_TNT_CHANNEL_ID", raising=False)

    import cli.discord_bot as botmod

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)

    from datetime import datetime, timezone

    now = datetime(2026, 1, 29, 14, 0, tzinfo=timezone.utc)
    items = [
        {
            "ts_utc": datetime(2026, 1, 29, 15, 30, tzinfo=timezone.utc),
            "type": "JOBLESS",
            "title": "Initial Jobless Claims",
            "impact": "HIGH",
            "source": "csv",
        },
        {
            "ts_utc": datetime(2026, 1, 30, 19, 0, tzinfo=timezone.utc),
            "type": "FOMC",
            "title": "FOMC Rate Decision",
            "impact": "HIGH",
            "source": "csv",
        },
    ]

    monkeypatch.setattr(botmod, "_macro_calendar_load_upcoming", lambda **_kw: items)
    monkeypatch.setattr(botmod, "datetime", type("DT", (), {"now": staticmethod(lambda tz=None: now)})())

    msg = _FakeMessage("macro this week")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    out = msg.replies[0]
    assert out.startswith("📅 Macro calendar")
    assert "FOMC" in out
    assert "Initial Jobless Claims" in out
