import pytest


class _FakeGuild:
    def __init__(self, *, guild_id: int = 777) -> None:
        self.id = int(guild_id)


class _FakeAuthor:
    def __init__(self, *, user_id: int = 123, is_bot: bool = False) -> None:
        self.id = int(user_id)
        self.bot = bool(is_bot)


class _FakeChannel:
    def __init__(self, *, channel_id: int = 999, name: str = "ask-tnt") -> None:
        self.id = int(channel_id)
        self.name = str(name)

    async def send(self, *, embed=None):  # noqa: ANN001
        # Fallback path used when reply() fails.
        self._sent_embed = embed


class _FakeMessage:
    def __init__(
        self,
        content: str,
        *,
        channel: _FakeChannel | None = None,
        author: _FakeAuthor | None = None,
        guild: _FakeGuild | None = None,
    ) -> None:
        self.content = content
        self.channel = channel or _FakeChannel()
        self.author = author or _FakeAuthor()
        self.guild = guild or _FakeGuild()
        self._replied_embeds = []

    @property
    def replied_embeds(self):
        return list(self._replied_embeds)

    async def reply(self, *, embed=None, mention_author: bool = False):  # noqa: ANN001,ARG002
        self._replied_embeds.append(embed)


@pytest.mark.asyncio
async def test_ask_tnt_earnings_does_not_become_ticker(monkeypatch):
    import delivery.discord_bot as d

    # Ensure we take the fallback extraction path.
    monkeypatch.setattr(d, "extract_analysis_request", lambda _text: None)

    # Route must allow this handler.
    monkeypatch.setattr(d, "decide_route", lambda **_kwargs: type("D", (), {"action": "allow_full"})())

    # Avoid real redis/network.
    monkeypatch.setattr(d, "redis_client_for_ctx", lambda: None)

    # Guardrail: the old buggy implementation used re.search to grab the first 1-6 letter token.
    def _boom(*_args, **_kwargs):
        raise AssertionError("re.search() should not be used for earnings symbol fallback")

    monkeypatch.setattr(d.re, "search", _boom)

    msg = _FakeMessage("Does AMD have earnings tomorrow?")
    handled = await d.maybe_handle_ask_tnt_earnings_embed(msg)

    assert handled is True
    assert len(msg.replied_embeds) == 1
    e = msg.replied_embeds[0]
    assert e is not None
    assert "Earnings — AMD" in str(getattr(e, "title", ""))
