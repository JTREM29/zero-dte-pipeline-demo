from __future__ import annotations

import io
import math
import os
from typing import Optional

from delivery.tnt_chart_style import apply_tnt_dark_rcparams, style_tnt_dark_axes, style_tnt_dark_figure, tnt_png_metadata


# === TNT OI/IV STYLE CONTRACT ===
TNT_CALL_COLOR = "#22C55E"  # TNT green
TNT_PUT_COLOR = "#EF4444"  # TNT red
TNT_BAR_ALPHA = 0.90
TNT_GRID_ALPHA = 0.12
TNT_WATERMARK = True


def infer_iv_percent_units(values: list[float]) -> list[float]:
    clean = [v for v in values if isinstance(v, (int, float)) and not math.isnan(float(v))]
    if not clean:
        return values
    clean_sorted = sorted(float(v) for v in clean)
    median = clean_sorted[len(clean_sorted) // 2]
    if median > 3.0:
        return [float(v) if (isinstance(v, (int, float)) and not math.isnan(float(v))) else math.nan for v in values]
    return [float(v) * 100.0 if (isinstance(v, (int, float)) and not math.isnan(float(v))) else math.nan for v in values]


def bucket_oi_iv_by_strike(df, *, sym: str) -> tuple[list[float], list[float], list[float], list[float]]:
    try:
        import pandas as pd
    except Exception:
        return [], [], [], []

    if df is None or getattr(df, "empty", True):
        return [], [], [], []

    work = df.copy()
    work["strike"] = pd.to_numeric(work.get("strike"), errors="coerce")
    work["open_interest"] = pd.to_numeric(work.get("open_interest"), errors="coerce")
    work["iv"] = pd.to_numeric(work.get("iv"), errors="coerce")
    work = work.dropna(subset=["strike"]).copy()
    if work.empty:
        return [], [], [], []

    sym_up = (sym or "").strip().upper()
    bucket = 5.0 if sym_up in {"SPX", "SPXW"} else 1.0
    try:
        median_strike = float(work["strike"].median())
        if median_strike >= 1000.0:
            bucket = max(bucket, 5.0)
    except Exception:
        pass

    work["type"] = work.get("type")
    work["type"] = work["type"].astype(str).str.lower()

    oi_raw = work["open_interest"].fillna(0.0)
    work["oi_pos"] = oi_raw.where(oi_raw > 0.0, 0.0)

    work["strike_bucket"] = (work["strike"] / float(bucket)).round() * float(bucket)
    work["strike_bucket"] = pd.to_numeric(work["strike_bucket"], errors="coerce")
    work = work.dropna(subset=["strike_bucket"]).copy()
    if work.empty:
        return [], [], [], []

    strikes: list[float] = []
    oi_calls: list[float] = []
    oi_puts: list[float] = []
    iv_avgs: list[float] = []

    for strike_b, grp in work.groupby("strike_bucket"):
        try:
            strike_f = float(strike_b)
        except Exception:
            continue

        call_mask = grp["type"].eq("call")
        put_mask = grp["type"].eq("put")

        oi_c = float(grp.loc[call_mask, "oi_pos"].sum()) if bool(call_mask.any()) else 0.0
        oi_p = float(grp.loc[put_mask, "oi_pos"].sum()) if bool(put_mask.any()) else 0.0

        iv = grp["iv"]
        w = grp["oi_pos"]
        mask = (~iv.isna()) & (w > 0.0)
        if bool(mask.any()):
            iv_avg = float((iv[mask] * w[mask]).sum() / w[mask].sum())
        else:
            try:
                iv_avg = float(iv.mean())
            except Exception:
                iv_avg = math.nan

        strikes.append(strike_f)
        oi_calls.append(oi_c)
        oi_puts.append(oi_p)
        iv_avgs.append(iv_avg)

    order = sorted(range(len(strikes)), key=lambda i: strikes[i])
    strikes = [strikes[i] for i in order]
    oi_calls = [oi_calls[i] for i in order]
    oi_puts = [oi_puts[i] for i in order]
    iv_avgs = [iv_avgs[i] for i in order]
    iv_pct = infer_iv_percent_units(iv_avgs)
    return strikes, oi_calls, oi_puts, iv_pct


def render_oi_iv_png(
    *,
    title: str,
    x_labels: list[str],
    iv_pct: list[float],
    oi_calls: list[float],
    oi_puts: list[float],
    dpi: int = 150,
    include_iv_overlay: bool = True,
    figsize: tuple[float, float] | None = None,
    watermark: bool | None = None,
) -> Optional[bytes]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        apply_tnt_dark_rcparams(matplotlib)
        import matplotlib.pyplot as plt

        # Guardrails: keep this chart consistent even if other rcparams drift.
        try:
            plt.rcParams.update(
                {
                    "axes.facecolor": "#0B0F14",
                    "figure.facecolor": "#0B0F14",
                    "axes.edgecolor": "#2A2E35",
                    "axes.labelcolor": "#E6E6E6",
                    "xtick.color": "#C7CBD1",
                    "ytick.color": "#C7CBD1",
                    "text.color": "#E6E6E6",
                }
            )
        except Exception:
            pass
    except Exception:
        return None

    if not x_labels:
        return None

    n = len(x_labels)
    if len(oi_calls) != n or len(oi_puts) != n:
        return None
    if iv_pct and len(iv_pct) != n:
        return None

    # Allow aligning render geometry across machines/workers.
    # Env format: "W,H" (inches), e.g. "10,6".
    if figsize is None:
        raw = (os.getenv("TNT_OI_IV_FIGSIZE", "") or "").strip()
        if raw:
            try:
                parts = [p.strip() for p in raw.split(",")]
                if len(parts) == 2:
                    w_in = float(parts[0])
                    h_in = float(parts[1])
                    if w_in > 0.0 and h_in > 0.0:
                        figsize = (w_in, h_in)
            except Exception:
                figsize = None
    if figsize is None:
        figsize = (11.25, 5.4)

    # Deterministically cap strike count based on available width to avoid cramming.
    # Prefer a center window so the picture stays balanced.
    try:
        width_in = float(figsize[0])
    except Exception:
        width_in = 11.25
    max_cols = 18 if width_in < 15.0 else 25
    if n > max_cols:
        start = max(0, int(n // 2 - max_cols // 2))
        end = min(n, start + max_cols)
        start = max(0, end - max_cols)
        x_labels = list(x_labels[start:end])
        oi_calls = list(oi_calls[start:end])
        oi_puts = list(oi_puts[start:end])
        if iv_pct:
            iv_pct = list(iv_pct[start:end])
        n = len(x_labels)

    xs = list(range(n))

    fig, ax = plt.subplots(figsize=figsize)
    style_tnt_dark_figure(fig)
    style_tnt_dark_axes(ax)

    call_color = TNT_CALL_COLOR
    put_color = TNT_PUT_COLOR
    line_color = "#8b949e"  # muted gray

    # Guardrail: some feeds/joins can yield non-finite or negative OI.
    # Negative bars clipped by ylim(bottom=0) can appear as tiny baseline slivers in screenshots.
    # Enforce the product rule: OI bars are always finite and >= 0.
    def _pos(v: object) -> float:
        try:
            x = float(v)
        except Exception:
            return 0.0
        if not math.isfinite(x):
            return 0.0
        return x if x > 0.0 else 0.0

    oi_calls_pos = [_pos(v) for v in list(oi_calls)]
    oi_puts_pos = [_pos(v) for v in list(oi_puts)]

    # Bars (must be identical style): same alpha, z-order, width.
    w = 0.38
    ax.bar(
        [x - w / 2.0 for x in xs],
        oi_calls_pos,
        width=w,
        color=call_color,
        alpha=TNT_BAR_ALPHA,
        label="Calls OI",
        zorder=3,
    )
    ax.bar(
        [x + w / 2.0 for x in xs],
        oi_puts_pos,
        width=w,
        color=put_color,
        alpha=TNT_BAR_ALPHA,
        label="Puts OI",
        zorder=3,
    )
    ax.set_ylabel("Open interest", color="#c9d1d9")

    # Grid + Y-axis (lock this so it never breaks again).
    ax.set_ylim(bottom=0.0)
    try:
        ax.grid(axis="y", alpha=TNT_GRID_ALPHA)
        ax.grid(axis="x", visible=False)
    except Exception:
        try:
            ax.grid(True, axis="y", alpha=TNT_GRID_ALPHA)
            ax.grid(False, axis="x")
        except Exception:
            ax.grid(True, alpha=0.12)

    # TNT watermark (center, subtle, never intrusive) — always on.
    # NOTE: `watermark` is retained for backward compatibility but intentionally ignored
    # so the visual contract cannot drift across runtimes.
    try:
        if bool(TNT_WATERMARK):
            ax.text(
                0.5,
                0.52,
                "TNT",
                transform=ax.transAxes,
                fontsize=72,
                color="white",
                alpha=0.05,
                ha="center",
                va="center",
                zorder=1,
                weight="bold",
            )
    except Exception:
        pass

    # Hide top/right spines for a cleaner composition.
    try:
        ax.spines["top"].set_alpha(0.0)
        ax.spines["right"].set_alpha(0.0)
    except Exception:
        pass
    # Keep OI axis conventional (left); IV axis will be right.
    try:
        ax.yaxis.tick_left()
        ax.yaxis.set_label_position("left")
    except Exception:
        pass

    ax2 = None
    wall_callout_drawn = False
    if bool(include_iv_overlay):
        ax2 = ax.twinx()
        style_tnt_dark_axes(ax2, grid=False)
        try:
            ax2.tick_params(colors="#8b949e")
        except Exception:
            pass
        # twinx() can create "ghost" x ticks/labels unless explicitly disabled.
        try:
            ax2.set_xticks([])
            ax2.set_xticklabels([])
            ax2.tick_params(axis="x", which="both", bottom=False, labelbottom=False, top=False, labeltop=False)
            ax2.minorticks_off()
        except Exception:
            pass
        ax2.plot(
            xs,
            list(iv_pct) if iv_pct else [math.nan] * n,
            color=line_color,
            linewidth=1.05,
            alpha=0.42,
            linestyle=":",
            label="IV",
        )
        ax2.set_ylabel("IV (%)", color="#8b949e")

    # Soft-cap Y so a single wall doesn't flatten everything else.
    try:
        import numpy as np

        y = np.asarray([float(v) for v in list(oi_calls_pos) + list(oi_puts_pos)], dtype=float)
        y = y[np.isfinite(y)]
        y = y[y >= 0.0]
        if y.size:
            soft = float(np.percentile(y, 95)) * 1.15
            hard = float(y.max()) * 1.05
            y_top = max(1.0, min(hard, soft))
            ax.set_ylim(0.0, y_top)

            # If the max bar is clipped by the soft cap, add a callout.
            max_call = float(max(oi_calls_pos)) if oi_calls_pos else 0.0
            max_put = float(max(oi_puts_pos)) if oi_puts_pos else 0.0
            if max(max_call, max_put) > (y_top * 1.02):
                if max_call >= max_put:
                    i = int(max(range(n), key=lambda j: float(oi_calls_pos[j]) if oi_calls_pos else 0.0))
                    label = f"Call wall @ {x_labels[i]}"
                    val = float(oi_calls_pos[i])
                else:
                    i = int(max(range(n), key=lambda j: float(oi_puts_pos[j]) if oi_puts_pos else 0.0))
                    label = f"Put wall @ {x_labels[i]}"
                    val = float(oi_puts_pos[i])

                ax.annotate(
                    f"{label}\n{val:,.0f}",
                    xy=(xs[i], y_top),
                    xytext=(xs[i] + 0.25, y_top * 0.92),
                    textcoords="data",
                    ha="left",
                    va="top",
                    fontsize=8,
                    color="#c9d1d9",
                    alpha=0.92,
                    arrowprops={"arrowstyle": "-|>", "color": "#8b949e", "alpha": 0.55, "lw": 0.9},
                    bbox={"facecolor": "#0b0f14", "edgecolor": "#2d333b", "alpha": 0.65, "pad": 2.5},
                    zorder=5,
                )
                wall_callout_drawn = True
    except Exception:
        pass

    # Title smaller/tighter, with a light subheader.
    # Place these slightly above the axes so they don't cover the IV overlay.
    ax.set_title(title, color="#c9d1d9", fontsize=11, pad=0, loc="left", y=1.08)
    try:
        sub = f"{n} strikes  •  IV overlay {'on' if include_iv_overlay else 'off'}"
        ax.text(
            0.0,
            1.04,
            sub,
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=8.5,
            color="#8b949e",
            alpha=0.85,
            clip_on=False,
        )
    except Exception:
        pass
    ax.set_xlabel("Strike", color="#c9d1d9")

    # One decisive takeaway (top-right).
    try:
        # If a wall callout is already present (soft-cap), avoid duplicating the same message.
        if bool(wall_callout_drawn):
            raise RuntimeError("skip_takeaway_when_wall_callout")
        put_i = int(max(range(n), key=lambda i: float(oi_puts_pos[i]) if oi_puts_pos else 0.0))
        call_i = int(max(range(n), key=lambda i: float(oi_calls_pos[i]) if oi_calls_pos else 0.0))
        put_max = float(oi_puts_pos[put_i])
        call_max = float(oi_calls_pos[call_i])
        takeaway = ""
        if put_max >= max(1.0, call_max) * 1.15:
            takeaway = f"Put wall @ {x_labels[put_i]}"
        elif call_max >= max(1.0, put_max) * 1.15:
            takeaway = f"Call wall @ {x_labels[call_i]}"
        else:
            skew = None
            try:
                iv_clean = [float(v) for v in (iv_pct or []) if isinstance(v, (int, float)) and not math.isnan(float(v))]
                if iv_clean and len(iv_clean) >= 6:
                    k = max(2, int(len(iv_clean) // 3))
                    iv_low = sum(iv_clean[:k]) / float(k)
                    iv_high = sum(iv_clean[-k:]) / float(k)
                    if (iv_low - iv_high) >= 2.0:
                        skew = "IV skew favors downside"
                    elif (iv_high - iv_low) >= 2.0:
                        skew = "IV skew favors upside"
            except Exception:
                skew = None
            takeaway = skew or "Balanced OI"

        if str(takeaway).strip() == "Balanced OI":
            ax.text(
                0.985,
                0.92,
                "Balanced OI",
                transform=ax.transAxes,
                ha="right",
                va="top",
                fontsize=10,
                color="#E6E6E6",
                bbox=dict(
                    boxstyle="round,pad=0.25",
                    fc=(0.15, 0.15, 0.15, 0.85),
                    ec=(1, 1, 1, 0.15),
                ),
            )
        else:
            ax.text(
                0.985,
                0.92,
                takeaway,
                transform=ax.transAxes,
                ha="right",
                va="top",
                fontsize=9,
                color="#E6E6E6",
                alpha=0.92,
                bbox={"facecolor": "#0b0f14", "edgecolor": "#2d333b", "alpha": 0.75, "pad": 3.0},
            )
    except Exception:
        pass

    # --- X axis: deterministic ticks (no ghost labels) ---
    try:
        from matplotlib.ticker import NullFormatter, NullLocator

        ax.minorticks_off()
        ax.xaxis.set_minor_locator(NullLocator())
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.tick_params(axis="x", which="minor", bottom=False, top=False)
    except Exception:
        try:
            ax.minorticks_off()
            ax.tick_params(axis="x", which="minor", bottom=False, top=False)
        except Exception:
            pass

    ax.set_xticks(xs)
    try:
        tick_labels = [str(int(round(float(s)))) for s in x_labels]
    except Exception:
        tick_labels = [str(s) for s in x_labels]
    ax.set_xticklabels(tick_labels, rotation=40, ha="right", rotation_mode="anchor", color="#c9d1d9", fontsize=8)
    # Prevent strike tick labels from clipping into the plot area (seen as tiny baseline fragments).
    # Make tick labels unclippable and give the first label a left anchor.
    try:
        labs = list(ax.get_xticklabels() or [])
        for lab in labs:
            try:
                lab.set_clip_on(False)
            except Exception:
                pass
        if labs:
            try:
                labs[0].set_ha("left")
            except Exception:
                pass
    except Exception:
        pass
    try:
        # Hard disable top ticks + duplicate labels.
        ax.tick_params(axis="x", which="both", top=False, labeltop=False, bottom=True, labelbottom=True, pad=6)
    except Exception:
        pass

    # Give edge labels a bit of breathing room so the leftmost rotated label can't clip.
    try:
        ax.margins(x=0.03)
        ax.set_xlim(-0.85, float(n) - 0.15)
    except Exception:
        pass


    try:
        h1, l1 = ax.get_legend_handles_labels()
        if ax2 is not None:
            h2, l2 = ax2.get_legend_handles_labels()
        else:
            h2, l2 = [], []
        if l1 or l2:
            ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8, framealpha=0.15)
    except Exception:
        pass

    try:
        # Some matplotlib versions/font backends still clip rotated x tick labels.
        # Default more generous for Discord screenshots; allow ops override.
        bottom = 0.18
        try:
            raw_bottom = (os.getenv("TNT_OI_IV_LAYOUT_BOTTOM", "") or "").strip()
            if raw_bottom:
                bottom = float(raw_bottom)
        except Exception:
            bottom = 0.18
        bottom = max(0.02, min(0.30, float(bottom)))
        # Leave extra headroom for title/subheader placed above the axes.
        fig.tight_layout(rect=(0.06, bottom, 0.985, 0.92))
    except Exception:
        pass
    dpi_used = int(dpi) if int(dpi) > 0 else 150
    buf = io.BytesIO()
    meta = dict(tnt_png_metadata())
    meta["tnt_oi_iv_layout"] = "v2"
    # Audit flags for stability rails / quality gates.
    meta["tnt_oi_iv_bar_calls"] = "2"
    meta["tnt_oi_iv_ylim_bottom"] = "0"
    # Style contract rails (avoid drift across worker/local renders).
    meta["tnt_oi_iv_palette"] = "option_b"
    meta["tnt_oi_iv_watermark"] = "1" if bool(TNT_WATERMARK) else "0"
    save_kwargs: dict[str, object] = {}
    try:
        truthy = {"1", "true", "yes", "y", "on"}
        if (os.getenv("TNT_OI_IV_SAVE_BBOX_TIGHT", "0") or "0").strip().lower() in truthy:
            save_kwargs["bbox_inches"] = "tight"
            save_kwargs["pad_inches"] = 0.08
    except Exception:
        pass

    # Auto-safety: if rotated tick labels are still clipped, fall back to tight bbox.
    # This is deterministic within a given runtime/font backend and fixes the "6." artifacts.
    try:
        if "bbox_inches" not in save_kwargs:
            canvas = getattr(fig, "canvas", None)
            if canvas is not None:
                canvas.draw()
                renderer = canvas.get_renderer()
                fig_bbox = fig.get_window_extent(renderer=renderer)
                clipped = False
                for lab in ax.get_xticklabels() or []:
                    try:
                        bb = lab.get_window_extent(renderer=renderer)
                        if bb.x0 < fig_bbox.x0 or bb.x1 > fig_bbox.x1 or bb.y0 < fig_bbox.y0:
                            clipped = True
                            break
                    except Exception:
                        continue
                if clipped:
                    save_kwargs["bbox_inches"] = "tight"
                    save_kwargs["pad_inches"] = 0.08
    except Exception:
        pass

    # Final lock: even after any late layout changes, keep OI at/above zero.
    try:
        ax.set_ylim(bottom=0.0)
    except Exception:
        pass
    fig.savefig(
        buf,
        format="png",
        dpi=dpi_used,
        facecolor=fig.get_facecolor(),
        metadata=meta,
        **save_kwargs,
    )
    plt.close(fig)
    return buf.getvalue()
