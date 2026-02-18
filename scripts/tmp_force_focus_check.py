import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import types
from types import SimpleNamespace

import discord

from cli import discord_bot as cli_bot


class DummyResponse:
    async def defer(self, *, ephemeral=True, thinking=True):
        print("defer called", ephemeral, thinking)


class DummyFollowup:
    async def send(self, message, *, ephemeral=False):
        print("followup", "(ephemeral)" if ephemeral else "", message)


class DummyChannel:
    def __init__(self):
        self.id = 123

    async def send(self, message):
        print("channel send", message)


class DummyClient:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel if channel_id else None


class DummyInteraction:
    def __init__(self):
        self.user = SimpleNamespace(id=cli_bot.OWNER_ID or 0)
        self.channel = DummyChannel()
        self.client = DummyClient(self.channel)
        self.response = DummyResponse()
        self.followup = DummyFollowup()


async def main():
    interaction = DummyInteraction()
    await cli_bot.force_focus.callback(interaction)
    await cli_bot.force_intraday.callback(interaction)
    await cli_bot.force_daily.callback(interaction)
    await cli_bot.status_command.callback(interaction)


if __name__ == "__main__":
    asyncio.run(main())
