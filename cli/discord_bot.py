"""Discord bot for serving morning briefs on demand."""
from __future__ import annotations

import os

import discord
from discord.ext import commands

from scripts.morning_brief_agent import build_morning_brief


DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.command(name="morning")
async def morning_cmd(ctx: commands.Context, symbol: str = "SPX"):
    await ctx.trigger_typing()
    result = build_morning_brief(symbol=symbol)
    brief = result["brief_markdown"]

    if len(brief) > 1900:
        brief = brief[:1900] + "\n\n*(truncated)*"

    await ctx.send(f"📈 Morning Brief for **{symbol.upper()}**\n\n{brief}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
