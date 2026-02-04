import os


def test_paperdesk_enabled_legacy_alias(monkeypatch):
    # Ensure canonical flag is absent so alias is used.
    monkeypatch.delenv("TNT_PAPER_DESK_ENABLED", raising=False)
    monkeypatch.delenv("PAPER_DESK_ENABLED", raising=False)

    monkeypatch.setenv("PAPERDESK_ENABLED", "1")

    from delivery import discord_bot as delivery_bot

    assert delivery_bot._paper_desk_enabled() is True


def test_paperdesk_enabled_legacy_alias_off(monkeypatch):
    monkeypatch.delenv("TNT_PAPER_DESK_ENABLED", raising=False)
    monkeypatch.delenv("PAPER_DESK_ENABLED", raising=False)

    monkeypatch.setenv("PAPERDESK_ENABLED", "0")

    from delivery import discord_bot as delivery_bot

    assert delivery_bot._paper_desk_enabled() is False
