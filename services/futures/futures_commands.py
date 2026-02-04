from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timezone

import discord
from discord import app_commands

from .futures_embeds import build_futures_embed
from .futures_context_line import MAX_AGE_S_DEFAULT, WARN_AGE_S_DEFAULT, scores_age_sec, render_futures_context_line
from .futures_models import PublishMode
from .futures_store import FuturesStore
from services.observability.futures_ingest_health import classify_futures_ingest


def _mode() -> PublishMode:
    m = (os.getenv("FUTURES_PUBLISH_MODE", "quotes") or "quotes").strip().lower()
    if m not in ("scores", "quotes", "full"):
        m = "quotes"
    return m  # type: ignore[return-value]


def _fmt_ts_utc(epoch: int | None) -> str:
    if epoch is None:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return "unknown"


def _build_id() -> str:
    for k in ("GIT_SHA", "GITHUB_SHA"):
        v = (os.getenv(k) or "").strip()
        if v:
            return v[:12]
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True)
        return (result.stdout or "").strip()[:12] or "unknown"
    except Exception:
        return "unknown"


def register_futures_commands(tree: app_commands.CommandTree, store: FuturesStore) -> None:
    def _ingest_health_snapshot() -> tuple[str, str, int | None, int | None, str, str]:
        """Return (state, reason, hb_age_s, scores_age_s, note, hb_msg) using the canonical classifier."""

        hb_ts, hb_msg = store.get_heartbeat()
        status = store.get_status() or {}
        scores = store.get_scores()

        now = int(__import__("time").time())

        hb_age_s: int | None = None
        if hb_ts is not None:
            try:
                hb_age_s = max(0, now - int(hb_ts))
            except Exception:
                hb_age_s = None

        try:
            hb_max_age = int(os.getenv("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", "90") or "90")
        except Exception:
            hb_max_age = 90
        try:
            scores_max_age = int(os.getenv("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120") or "120")
        except Exception:
            scores_max_age = 120

        scores_present = scores is not None
        scores_age_s: int | None = None
        if scores is not None:
            try:
                scores_age_s = max(0, now - int(scores.updated_utc))
            except Exception:
                scores_age_s = None

        st_note = str((status or {}).get("note") or "").strip()

        ingest_state, ingest_reason = classify_futures_ingest(
            hb_age_s=hb_age_s,
            scores_age_s=scores_age_s,
            scores_present=bool(scores_present),
            note=st_note,
            hb_msg=hb_msg,
            hb_max_age=int(hb_max_age),
            scores_max_age=int(scores_max_age),
        )

        return ingest_state, ingest_reason, hb_age_s, scores_age_s, st_note, (hb_msg or "")

    @tree.command(name="futures", description="Show premium futures snapshot + intelligence (ES/NQ/RTY).")
    async def futures(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        mode = _mode()

        ingest_state, ingest_reason, hb_age_s, scores_age_s, st_note, hb_msg = await asyncio.to_thread(
            _ingest_health_snapshot
        )
        if ingest_state != "OK":
            reason = ingest_reason or ingest_state
            hb_bits = f"hb {hb_age_s}s" if isinstance(hb_age_s, int) else "hb missing"
            scores_bits = (
                f"scores {scores_age_s}s" if isinstance(scores_age_s, int) else "scores missing"
            )
            extra_bits: list[str] = []
            if st_note and st_note.lower() not in {"ok"}:
                extra_bits.append(f"note={st_note}")
            if hb_msg and hb_msg.strip().lower() not in {"ok"}:
                extra_bits.append(f"hb_msg={hb_msg.strip()}")
            extra_line = ("; " + "; ".join(extra_bits)) if extra_bits else ""
            embed = discord.Embed(
                title="Futures: DEGRADED",
                description=(
                    "Futures ingest is not OK, so futures-dependent output is suppressed to avoid ghost signals.\n"
                    f"• {ingest_state}: {reason}\n"
                    f"• {hb_bits} | {scores_bits}{extra_line}\n\n"
                    "Use `/futures_status` for details."
                )[:3900],
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed)
            return

        quotes = await asyncio.to_thread(store.get_quotes_with_fallback, ["ES", "NQ", "RTY"])
        scores = await asyncio.to_thread(store.get_scores)
        status = await asyncio.to_thread(store.get_status)
        embed = build_futures_embed(quotes, scores, mode, status=status)

        await interaction.followup.send(embed=embed)

    @tree.command(name="futures_status", description="Futures ingest status: heartbeat, per-symbol age, last error")
    @app_commands.describe(
        check="Force a live quote check (may be slower)",
        public="Post the status publicly in-channel (admin-only)",
        force="Required when public=true to avoid accidental announces",
    )
    async def futures_status(
        interaction: discord.Interaction,
        check: bool = False,
        public: bool = False,
        force: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        def _is_admin() -> bool:
            try:
                owner_id = int((os.getenv("DISCORD_OWNER_ID") or "").strip() or "0")
                if owner_id and int(getattr(getattr(interaction, "user", None), "id", 0) or 0) == owner_id:
                    return True
            except Exception:
                pass
            try:
                perms = getattr(getattr(interaction, "user", None), "guild_permissions", None)
                if perms is None:
                    return False
                return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))
            except Exception:
                return False

        if public and not _is_admin():
            await interaction.followup.send("Not authorized to post publicly.", ephemeral=True)
            return

        if public and not force:
            await interaction.followup.send("Refusing to post publicly without `force:true`.", ephemeral=True)
            return

        hb_ts, hb_msg = await asyncio.to_thread(store.get_heartbeat)
        status = await asyncio.to_thread(store.get_status)
        if check:
            live_quotes = await asyncio.to_thread(store.get_quotes_with_fallback, ["ES", "NQ", "RTY"])
        else:
            live_quotes = await asyncio.to_thread(store.get_quotes, ["ES", "NQ", "RTY"])
        scores = await asyncio.to_thread(store.get_scores)

        now = int(__import__("time").time())
        hb_age_s: int | None = None
        if hb_ts is not None:
            hb_age_s = max(0, now - int(hb_ts))
        hb_age = f"{hb_age_s}s" if isinstance(hb_age_s, int) else "unknown"

        st_state = str((status or {}).get("state") or "unknown")
        st_note = str((status or {}).get("note") or "").strip()
        last_err = str((status or {}).get("last_error") or "").strip()

        try:
            hb_max_age = int(os.getenv("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", "90") or "90")
        except Exception:
            hb_max_age = 90
        try:
            scores_max_age = int(os.getenv("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120") or "120")
        except Exception:
            scores_max_age = 120

        scores_present = scores is not None
        scores_age_s: int | None = None
        if scores is not None:
            try:
                scores_age_s = max(0, now - int(scores.updated_utc))
            except Exception:
                scores_age_s = None

        ingest_state, ingest_reason = classify_futures_ingest(
            hb_age_s=hb_age_s,
            scores_age_s=scores_age_s,
            scores_present=bool(scores_present),
            note=st_note,
            hb_msg=hb_msg,
            hb_max_age=int(hb_max_age),
            scores_max_age=int(scores_max_age),
        )

        embed = discord.Embed(title="Futures Status")
        # Canonical, operator-facing ingest health label (same as canary).
        try:
            if hb_age_s is None:
                ingest_bits = [f"{ingest_state}", "hb missing"]
            else:
                if ingest_state == "DOWN" and hb_age_s > int(hb_max_age):
                    ingest_bits = [f"{ingest_state}", f"hb {hb_age_s}s stale"]
                else:
                    ingest_bits = [f"{ingest_state}", f"hb {hb_age_s}s"]
                    if scores_present:
                        if scores_age_s is None:
                            ingest_bits.append("scores stale")
                        else:
                            ingest_bits.append(f"scores {scores_age_s}s")
                    else:
                        ingest_bits.append("scores missing")
            if ingest_state == "DEGRADED" and ingest_reason and ingest_reason not in {"scores missing", "scores stale"}:
                ingest_bits.append(ingest_reason)
            embed.add_field(name="Ingest health", value=" | ".join(ingest_bits)[:900], inline=False)
        except Exception:
            pass
        embed.add_field(name="Heartbeat", value=f"age={hb_age}\nmsg={hb_msg or '—'}", inline=True)
        embed.add_field(name="Worker", value=f"state={st_state}\nnote={st_note or '—'}", inline=True)

        scores_line = "missing"
        if scores is not None:
            try:
                age_s = max(0, now - int(scores.updated_utc))
                scores_line = f"present (age={age_s}s)\nregime={scores.regime} vol_mult={scores.vol_mult:.2f}"
            except Exception:
                scores_line = "present"
        embed.add_field(name="Scores", value=scores_line, inline=True)

        # Source-of-truth panel for operators.
        try:
            scores_age = scores_age_sec(scores, now_epoch=now)
        except Exception:
            scores_age = None
        scores_ts = None
        try:
            scores_ts = int(scores.updated_utc) if scores is not None else None
        except Exception:
            scores_ts = None

        sot_lines = [
            f"build_id={_build_id()}",
            f"hb_ts_utc={_fmt_ts_utc(hb_ts)} (age={hb_age})",
            f"scores_ts_utc={_fmt_ts_utc(scores_ts)} (age={str(scores_age) + 's' if isinstance(scores_age, int) else 'unknown'})",
        ]
        embed.add_field(name="Source of truth", value="\n".join(sot_lines), inline=False)

        # Micro context panel: stale-quiet for the system generally, but operator panel can show "aging".
        try:
            max_age = int(os.getenv("FUTURES_CONTEXT_MAX_AGE_SEC", str(MAX_AGE_S_DEFAULT)) or str(MAX_AGE_S_DEFAULT))
        except Exception:
            max_age = MAX_AGE_S_DEFAULT
        try:
            warn_age = int(os.getenv("FUTURES_CONTEXT_WARN_AGE_SEC", str(WARN_AGE_S_DEFAULT)) or str(WARN_AGE_S_DEFAULT))
        except Exception:
            warn_age = WARN_AGE_S_DEFAULT
        try:
            ctx_line = render_futures_context_line(scores, now_epoch=now, max_age_sec=max_age, warn_age_sec=warn_age)
        except Exception:
            ctx_line = None
        if ctx_line:
            embed.add_field(name="Context", value=ctx_line[:900], inline=False)
        elif scores is None:
            embed.add_field(name="Context", value="no prints yet (futures scores missing)", inline=False)

        lines = []
        for sym in ("ES", "NQ", "RTY"):
            q = live_quotes.get(sym)
            if not q:
                lines.append(f"{sym}: —")
                continue
            age = max(0, now - int(q.ts_utc or 0))
            lines.append(f"{sym}: {age}s ({q.source})")
        embed.add_field(name="Last update age", value="\n".join(lines), inline=False)

        if last_err:
            embed.add_field(name="Last error", value=last_err[:900], inline=False)

        if public and force and interaction.channel is not None:
            await interaction.channel.send(embed=embed)
            await interaction.followup.send("✅ Posted futures status publicly.", ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)
