import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from delivery import discord_bot as delivery


class FakeChannel:
    def __init__(self, channel_id=123):
        self.id = channel_id
        self.name = "fake"

    async def send(self, text):
        print("send called with length", len(text))

        class Msg:
            id = 42

        return Msg()


async def main():
    render = delivery.build_focus_list_render(["SPY"])
    await delivery._publish_autopost_render(
        FakeChannel(),
        render,
        builder="build_focus_list_render",
        label="focus_list",
        symbol="SPY",
        analysis_mode="db",
        output_mode="strict",
    )


asyncio.run(main())
