from __future__ import annotations

import asyncio
import io
import os
import time

import discord

from services.futures.futures_store import FuturesStore


async def send_futures_panel(
    interaction: discord.Interaction,
    store: FuturesStore,
    *,
    lookback_min: int = 180,
    include_vwap: bool = True,
) -> None:
    # Lazy imports so the bot can start even if optional deps aren't installed.
    from services.futures.futures_embeds import build_futures_embed
    from services.futures.futures_chart import render_futures_chart_png_with_metrics

    mode = (os.getenv("FUTURES_PUBLISH_MODE", "quotes") or "quotes").strip().lower()
    if mode not in {"scores", "quotes", "full"}:
        mode = "quotes"

    quotes = await asyncio.to_thread(store.get_quotes_with_fallback, ["ES", "NQ", "RTY"])
    scores = await asyncio.to_thread(store.get_scores)
    status = await asyncio.to_thread(store.get_status)

    embed = build_futures_embed(quotes, scores, mode, status=status)  # type: ignore[arg-type]

    png, metrics = await asyncio.to_thread(
        render_futures_chart_png_with_metrics,
        lookback_min=int(lookback_min),
        include_vwap=bool(include_vwap),
    )

    # Premium header block (readable even when chart is small).
    header_lines = []
    for sym in ("ES", "NQ", "RTY"):
        m = (metrics or {}).get(sym) if isinstance(metrics, dict) else None
        label = (m or {}).get("label") if isinstance(m, dict) else None
        label_s = str(label) if label else sym

        trend_arrow = str((m or {}).get("trend_arrow") or "→") if isinstance(m, dict) else "→"
        vwap_side = str((m or {}).get("vwap_side") or "—") if isinstance(m, dict) else "—"

        # Prefer ingest scores if present; else use computed bps.
        impulse = None
        try:
            if scores is not None:
                impulse = scores.impulse.get(sym)
        except Exception:
            impulse = None
        if impulse is None and isinstance(m, dict):
            impulse = m.get("impulse_bps")

        if impulse is None:
            imp_s = "—"
        else:
            # If it's the score model, it will look like +/-0.xx; if bps, like +/-12.
            try:
                imp_s = f"{float(impulse):+.2f}" if abs(float(impulse)) < 5 else f"{float(impulse):+.0f}bps"
            except Exception:
                imp_s = str(impulse)

        header_lines.append(f"{label_s}: {trend_arrow}  •  VWAP {vwap_side}  •  Impulse {imp_s}")

    if header_lines:
        embed.add_field(name="Context Header", value="\n".join(header_lines), inline=False)

    filename = f"futures_{int(time.time())}.png"
    file = discord.File(fp=io.BytesIO(png), filename=filename)
    embed.set_image(url=f"attachment://{filename}")

    await interaction.followup.send(embed=embed, file=file)
