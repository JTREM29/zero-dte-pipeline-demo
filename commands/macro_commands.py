from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta

import discord

from embeds.macro_event_embeds import build_macro_event_embed
from embeds.macro_regime_embeds import build_macro_regime_embed
from jobs.macro_econ_jobs import ECON_KEYS, ECON_META_KEY, _redis_get_json, refresh_econ_cache
from services.macro_calendar import ET, get_next_macro_events, macro_blackout_state, macro_schedule_path_str, validate_macro_schedule
from services.macro_regime import compute_and_cache_macro_regime, get_cached_macro_regime


DEFAULT_CALENDAR_CHANNEL_ID = "1462948320229200065"


def _calendar_channel_id() -> int:
    raw = (os.getenv("TNT_CALENDAR_EARNINGS_CHANNEL_ID") or os.getenv("CALENDAR_EARNINGS_CHANNEL_ID") or "").strip()
    if not raw:
        raw = DEFAULT_CALENDAR_CHANNEL_ID
    try:
        return int(raw)
    except Exception:
        return 0


def _econ_age_min(econ_meta: object) -> int | None:
    try:
        if not isinstance(econ_meta, dict):
            return None
        updated_utc = econ_meta.get("updated_utc")
        if not (isinstance(updated_utc, str) and updated_utc):
            return None
        dt_u = datetime.fromisoformat(updated_utc.replace("Z", "+00:00"))
        if dt_u.tzinfo is None:
            return None
        age_s = max(0, (datetime.now(tz=dt_u.tzinfo) - dt_u).total_seconds())
        return int(age_s // 60)
    except Exception:
        return None


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

    @bot.tree.command(name="macro_validate", description="Validate macro schedule + autopost readiness.")
    async def macro_validate(interaction: discord.Interaction, days: int = 45) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            d = int(days)
        except Exception:
            d = 45
        d = max(7, min(180, d))

        now = datetime.now(tz=ET)
        report = validate_macro_schedule(now, horizon_days=d)

        # Keep response bounded.
        try:
            warn = list(report.get("warnings") or [])
            err = list(report.get("errors") or [])
            report["warnings"] = warn[:25]
            report["errors"] = err[:25]
            if len(warn) > 25:
                report["warnings_truncated"] = len(warn) - 25
            if len(err) > 25:
                report["errors_truncated"] = len(err) - 25
        except Exception:
            pass

        await interaction.followup.send(
            f"```json\n{json.dumps(report, separators=(',', ':'), sort_keys=True)}\n```",
            ephemeral=True,
        )

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
            schedule_label=os.path.basename(macro_schedule_path_str()),
            source_label="Massive + schedule",
        )

        channel_id = _calendar_channel_id()
        if not channel_id:
            await interaction.followup.send("Calendar channel id is not set/valid.", ephemeral=True)
            return

        try:
            ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await ch.send(embed=embed)
        except Exception as exc:
            await interaction.followup.send(f"Post failed: {type(exc).__name__}: {exc}", ephemeral=True)
            return

        await interaction.followup.send("Posted.", ephemeral=True)

    @bot.tree.command(name="macro", description="Show Macro Pulse (regime + next releases).")
    async def macro(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from services.redis_env import redis_client

            r = redis_client(timeout_s=2.0, decode_responses=True)
        except Exception as exc:
            await interaction.followup.send(f"Redis not available: {exc}", ephemeral=True)
            return

        now = datetime.now(tz=ET)
        econ_tre = _redis_get_json(r, ECON_KEYS["treasury_yields"])
        econ_inf = _redis_get_json(r, ECON_KEYS["inflation"])
        econ_iex = _redis_get_json(r, ECON_KEYS["inflation_expectations"])
        econ_lab = _redis_get_json(r, ECON_KEYS["labor_market"])
        econ_meta = _redis_get_json(r, ECON_META_KEY)

        econ_age = _econ_age_min(econ_meta)

        regime = get_cached_macro_regime(r)
        if not isinstance(regime, dict):
            regime = compute_and_cache_macro_regime(
                r,
                treasury_yields=econ_tre,
                inflation=econ_inf,
                inflation_expectations=econ_iex,
                labor_market=econ_lab,
                updated_utc=(econ_meta.get("updated_utc") if isinstance(econ_meta, dict) else None),
            )

        nxt: list[str] = []
        evs = get_next_macro_events(now, horizon_days=5)
        for ev in evs[:8]:
            if (ev.dt_et - now).total_seconds() <= 48 * 3600:
                nxt.append(f"• {ev.dt_et.strftime('%a %m/%d %H:%M')} — {ev.title} ({ev.importance})")

        embed = build_macro_regime_embed(
            regime=regime,
            now_et=now,
            next_events=nxt,
            blackout_state=macro_blackout_state(now),
            schedule_label=os.path.basename(macro_schedule_path_str()),
            econ_refreshed_age_min=econ_age,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="macro_pulse", description="Force-post the Macro Pulse to calendar channel (admin).")
    async def macro_pulse(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if int(os.getenv("DISCORD_OWNER_ID", "0") or "0") and interaction.user.id != int(os.getenv("DISCORD_OWNER_ID", "0") or "0"):
            await interaction.followup.send("Not authorized.", ephemeral=True)
            return

        channel_id = _calendar_channel_id()
        if not channel_id:
            await interaction.followup.send("Calendar channel id is not set/valid.", ephemeral=True)
            return

        try:
            from services.redis_env import redis_client

            r = redis_client(timeout_s=2.0, decode_responses=True)
        except Exception as exc:
            await interaction.followup.send(f"Redis not available: {exc}", ephemeral=True)
            return

        now = datetime.now(tz=ET)
        econ_tre = _redis_get_json(r, ECON_KEYS["treasury_yields"])
        econ_inf = _redis_get_json(r, ECON_KEYS["inflation"])
        econ_iex = _redis_get_json(r, ECON_KEYS["inflation_expectations"])
        econ_lab = _redis_get_json(r, ECON_KEYS["labor_market"])
        econ_meta = _redis_get_json(r, ECON_META_KEY)
        econ_age = _econ_age_min(econ_meta)

        regime = get_cached_macro_regime(r)
        if not isinstance(regime, dict):
            regime = compute_and_cache_macro_regime(
                r,
                treasury_yields=econ_tre,
                inflation=econ_inf,
                inflation_expectations=econ_iex,
                labor_market=econ_lab,
                updated_utc=(econ_meta.get("updated_utc") if isinstance(econ_meta, dict) else None),
            )

        evs = get_next_macro_events(now, horizon_days=5)
        nxt: list[str] = []
        for ev in evs[:8]:
            if (ev.dt_et - now).total_seconds() <= 48 * 3600:
                nxt.append(f"• {ev.dt_et.strftime('%a %m/%d %H:%M')} — {ev.title} ({ev.importance})")

        embed = build_macro_regime_embed(
            regime=regime,
            now_et=now,
            next_events=nxt,
            blackout_state=macro_blackout_state(now),
            schedule_label=os.path.basename(macro_schedule_path_str()),
            econ_refreshed_age_min=econ_age,
        )

        try:
            ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await ch.send(embed=embed)
            print(f"[TNT][MACRO_PULSE][OK] target_channel_id={channel_id}")
        except discord.Forbidden as exc:
            missing_bits: list[str] = []
            try:
                if isinstance(ch, discord.abc.GuildChannel) and ch.guild is not None:
                    me = ch.guild.get_member(getattr(bot.user, "id", 0) or 0)
                    if me is not None:
                        perms = ch.permissions_for(me)
                        required = {
                            "view_channel": "View Channel",
                            "send_messages": "Send Messages",
                            "embed_links": "Embed Links",
                        }
                        for attr, label in required.items():
                            if not bool(getattr(perms, attr, False)):
                                missing_bits.append(label)
            except Exception:
                missing_bits = []

            try:
                miss = ",".join(missing_bits) if missing_bits else "-"
                print(f"[TNT][MACRO_PULSE][FORBIDDEN] target_channel_id={channel_id} missing={miss}")
            except Exception:
                pass

            suffix = ""
            if missing_bits:
                suffix = f" Missing: {', '.join(missing_bits)}."
            await interaction.followup.send(
                f"Post failed: Forbidden (missing permissions) in <#{channel_id}>.{suffix}",
                ephemeral=True,
            )
            return
        except Exception as exc:
            try:
                print(f"[TNT][MACRO_PULSE][ERROR] target_channel_id={channel_id} err={type(exc).__name__}: {exc}")
            except Exception:
                pass
            await interaction.followup.send(f"Post failed: {type(exc).__name__}: {exc}", ephemeral=True)
            return

        await interaction.followup.send("Posted Macro Pulse.", ephemeral=True)
