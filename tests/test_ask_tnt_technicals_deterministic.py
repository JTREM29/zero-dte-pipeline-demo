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
        self._reactions: list[str] = []

    @property
    def replies(self) -> list[str]:
        return list(self._replies)

    async def reply(self, content: str, *, mention_author: bool = False) -> None:  # noqa: ARG002
        self._replies.append(str(content))

    async def add_reaction(self, emoji: str) -> None:
        self._reactions.append(str(emoji))


def _mk_session_bars(n: int = 60) -> dict:
    # Deterministic synthetic bars: gently trending upward with non-zero volume.
    bars = []
    px = 100.0
    for i in range(int(n)):
        px = px + 0.05 + (0.005 * (i % 5))
        bars.append(
            {
                "open": px - 0.03,
                "high": px + 0.08,
                "low": px - 0.10,
                "close": px,
                "volume": 1000 + (i * 10),
            }
        )
    return {"bars": bars}


@pytest.mark.asyncio
async def test_ask_tnt_rsi_only_reply_is_deterministic(monkeypatch):
    import cli.discord_bot as botmod

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)
    monkeypatch.setattr(botmod.delivery, "_latest_session_bars", lambda _sym: _mk_session_bars(80))
    monkeypatch.setattr(botmod.delivery, "_get_last_price_snapshot", lambda _sym: type("S", (), {"price": 123.45})())

    msg = _FakeMessage("mstr rsi")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    out = msg.replies[0]
    assert out.startswith("MSTR | Last: ")
    assert "RSI(" in out
    assert "MACD:" not in out  # rsi-only query should not include macd block


@pytest.mark.asyncio
async def test_ask_tnt_rsi_symbol_after_keyword(monkeypatch):
    import cli.discord_bot as botmod

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)
    monkeypatch.setattr(botmod.delivery, "_latest_session_bars", lambda _sym: _mk_session_bars(80))
    monkeypatch.setattr(botmod.delivery, "_get_last_price_snapshot", lambda _sym: type("S", (), {"price": 123.45})())

    msg = _FakeMessage("rsi mstr")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    out = msg.replies[0]
    assert out.startswith("MSTR | Last: ")
    assert "RSI(" in out
    assert "MACD:" not in out


@pytest.mark.asyncio
async def test_ask_tnt_full_pack_on_technicals_keyword(monkeypatch):
    import cli.discord_bot as botmod

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)
    monkeypatch.setattr(botmod.delivery, "_latest_session_bars", lambda _sym: _mk_session_bars(120))
    monkeypatch.setattr(botmod.delivery, "_get_last_price_snapshot", lambda _sym: type("S", (), {"price": 200.0})())

    msg = _FakeMessage("MSTR technicals")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    out = msg.replies[0]
    assert out.startswith("MSTR | Last: ")
    assert "RSI(" in out
    assert "MACD:" in out
    assert "SMA(" in out
    assert "EMA(" in out
    assert "VWAP(RTH):" in out


@pytest.mark.asyncio
async def test_ask_tnt_full_pack_on_bare_ticker(monkeypatch):
    import cli.discord_bot as botmod

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)
    monkeypatch.setattr(botmod.delivery, "_latest_session_bars", lambda _sym: _mk_session_bars(80))
    monkeypatch.setattr(botmod.delivery, "_get_last_price_snapshot", lambda _sym: type("S", (), {"price": 150.0})())

    msg = _FakeMessage("MSTR")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    out = msg.replies[0]
    assert out.startswith("MSTR | Last: ")
    assert "RSI(" in out
    assert "MACD:" in out
    assert "SMA(" in out
    assert "EMA(" in out
    assert "VWAP(RTH):" in out


@pytest.mark.asyncio
async def test_ask_tnt_parses_ema_period(monkeypatch):
    import cli.discord_bot as botmod

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)
    monkeypatch.setattr(botmod.delivery, "_latest_session_bars", lambda _sym: _mk_session_bars(120))
    monkeypatch.setattr(botmod.delivery, "_get_last_price_snapshot", lambda _sym: type("S", (), {"price": 150.0})())

    msg = _FakeMessage("mstr ema 9")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    out = msg.replies[0]
    assert "EMA(9):" in out
    assert "RSI(" not in out  # ema-only query should not include RSI block


@pytest.mark.asyncio
async def test_ask_tnt_no_bars_fallback(monkeypatch):
    import cli.discord_bot as botmod

    async def _no_earnings(_message):
        return False

    monkeypatch.setattr(botmod, "_router_enabled", lambda: False)
    monkeypatch.setattr(botmod.delivery, "maybe_handle_ask_tnt_earnings_embed", _no_earnings)
    monkeypatch.setattr(botmod.delivery, "_latest_session_bars", lambda _sym: {"bars": []})
    monkeypatch.setattr(botmod.delivery, "_get_last_price_snapshot", lambda _sym: type("S", (), {"price": 150.0})())

    msg = _FakeMessage("mstr rsi")
    await botmod.on_message(msg)

    assert len(msg.replies) == 1
    assert "no fresh RTH 1m bars" in msg.replies[0]
