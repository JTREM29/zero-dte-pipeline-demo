from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import discord

from embeds.macro_event_embeds import build_macro_event_embed
from jobs.macro_econ_jobs import ECON_KEYS, _redis_get_json, refresh_econ_cache
from services.macro_calendar import ET, get_next_macro_events, macro_blackout_state


def register_macro_commands(bot: discord.Client) -> None:
    """Register /macro_* commands on the provided bot instance."""

    @bot.tree.command(name="macro_next", description="Show next macro events (with timers).")
    async def macro_next(interaction: discord.Interaction) -> None:
        now = datetime.now(tz=ET)
        events = get_next_macro_events(now, horizon_days=14)
        if not events:
            await interaction.response.send_message("No macro events found in the next 14 days.", ephemeral=True)
            return

        lines = []
        for e in events[:8]:
            lines.append(f"• **{e.title}** — {e.dt_et.strftime('%a %b %d %I:%M %p ET')} — {e.importance} — `{e.id}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @bot.tree.command(name="macro_now", description="Show current macro blackout gate state.")
    async def macro_now(interaction: discord.Interaction) -> None:
        now = datetime.now(tz=ET)
        state = macro_blackout_state(now)
        await interaction.response.send_message(f"```json\n{json.dumps(state, separators=(',', ':'), sort_keys=True)}\n```", ephemeral=True)

    @bot.tree.command(name="macro_refresh", description="Refresh Massive econ cache now.")
    async def macro_refresh(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from services.redis_env import redis_client

            r = redis_client(timeout_s=2.0, decode_responses=True)
        except Exception as exc:
            await interaction.followup.send(f"Redis not available: {exc}", ephemeral=True)
            return

        meta = await asyncio.to_thread(refresh_econ_cache, r)
        await interaction.followup.send(f"```json\n{json.dumps(meta, separators=(',', ':'), sort_keys=True)}\n```", ephemeral=True)

    @bot.tree.command(name="macro_post", description="Force-post a macro card by event id (admin).")
    async def macro_post(interaction: discord.Interaction, event_id: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if int(os.getenv("DISCORD_OWNER_ID", "0") or "0") and interaction.user.id != int(os.getenv("DISCORD_OWNER_ID", "0") or "0"):
            await interaction.followup.send("Not authorized.", ephemeral=True)
            return

        now = datetime.now(tz=ET)
        events = get_next_macro_events(now - timedelta(days=30), horizon_days=90)
        ev = next((x for x in events if x.id == event_id), None)
        if not ev:
            await interaction.followup.send(f"Event id not found: {event_id}", ephemeral=True)
            return

        try:
            from services.redis_env import redis_client

            r = redis_client(timeout_s=2.0, decode_responses=True)
        except Exception:
            r = None

        econ = _redis_get_json(r, ECON_KEYS["inflation"]) if r is not None else None
        embed = build_macro_event_embed(
            event_title=f"{ev.title} (Manual)",
            event_id=ev.id,
            dt_et=ev.dt_et,
            importance=ev.importance,
            blackout_before_min=ev.blackout_before_min,
            blackout_after_min=ev.blackout_after_min,
            now_et=now,
            econ_latest=econ,
        )

        channel_id = int(os.getenv("TNT_CALENDAR_EARNINGS_CHANNEL_ID", "0") or "0")
        if not channel_id:
            await interaction.followup.send("TNT_CALENDAR_EARNINGS_CHANNEL_ID is not set.", ephemeral=True)
            return

        try:
            ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await ch.send(embed=embed)
        except Exception as exc:
            await interaction.followup.send(f"Post failed: {type(exc).__name__}: {exc}", ephemeral=True)
            return

        await interaction.followup.send("Posted.", ephemeral=True)
