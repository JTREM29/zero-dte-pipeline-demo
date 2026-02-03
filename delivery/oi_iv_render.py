from __future__ import annotations

import io
import math
import os
from typing import Optional

from delivery.tnt_chart_style import add_tnt_watermark_ax, apply_tnt_dark_rcparams, style_tnt_dark_axes, style_tnt_dark_figure, tnt_png_metadata


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
) -> Optional[bytes]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        apply_tnt_dark_rcparams(matplotlib)
        import matplotlib.pyplot as plt
    except Exception:
        return None

    if not x_labels:
        return None

    n = len(x_labels)
    if len(oi_calls) != n or len(oi_puts) != n:
        return None
    if iv_pct and len(iv_pct) != n:
        return None

    xs = list(range(n))
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

    fig, ax = plt.subplots(figsize=figsize)
    style_tnt_dark_figure(fig)
    style_tnt_dark_axes(ax)

    # OI/IV should read "premium" and data-first: watermark off by default.
    try:
        truthy = {"1", "true", "yes", "y", "on"}
        if (os.getenv("TNT_OI_IV_WATERMARK", "0") or "0").strip().lower() in truthy:
            add_tnt_watermark_ax(ax, alpha=0.03)
    except Exception:
        pass

    call_color = "#58a6ff"  # blue
    put_color = "#ff7b72"   # salmon
    line_color = "#8b949e"  # muted gray

    # Side-by-side with a real gap so it can't be mistaken as stacked/overlapped.
    width = 0.34
    gap = 0.10
    delta = (width + gap) / 2.0
    ax.bar([x - delta for x in xs], oi_calls, width=width, color=call_color, alpha=0.55, label="Calls OI", zorder=2)
    ax.bar([x + delta for x in xs], oi_puts, width=width, color=put_color, alpha=0.50, label="Puts OI", zorder=2)
    ax.set_ylabel("Open interest", color="#c9d1d9")
    ax.grid(True, alpha=0.12, linestyle="--")
    # Keep OI axis conventional (left); IV axis will be right.
    try:
        ax.yaxis.tick_left()
        ax.yaxis.set_label_position("left")
    except Exception:
        pass

    ax2 = None
    if bool(include_iv_overlay):
        ax2 = ax.twinx()
        style_tnt_dark_axes(ax2, grid=False)
        try:
            ax2.tick_params(colors="#8b949e")
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

    ax.set_title(title, color="#c9d1d9")
    ax.set_xlabel("Strike", color="#c9d1d9")

    # One decisive takeaway (top-right).
    try:
        put_i = int(max(range(n), key=lambda i: float(oi_puts[i]) if oi_puts else 0.0))
        call_i = int(max(range(n), key=lambda i: float(oi_calls[i]) if oi_calls else 0.0))
        put_max = float(oi_puts[put_i])
        call_max = float(oi_calls[call_i])
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

        ax.text(
            0.985,
            0.92,
            takeaway,
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=9,
            color="#c9d1d9",
            alpha=0.92,
            bbox={"facecolor": "#0b0f14", "edgecolor": "#2d333b", "alpha": 0.75, "pad": 3.0},
        )
    except Exception:
        pass

    max_ticks = 18
    step = max(1, int(math.ceil(float(n) / float(max_ticks))))
    tick_idx = list(range(0, n, step))
    if (n - 1) not in tick_idx:
        tick_idx.append(n - 1)
    ax.set_xticks(tick_idx)
    ax.set_xticklabels([x_labels[i] for i in tick_idx], rotation=45, ha="right", color="#c9d1d9", fontsize=8)

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

    fig.tight_layout()
    dpi_used = int(dpi) if int(dpi) > 0 else 150
    buf = io.BytesIO()
    meta = dict(tnt_png_metadata())
    meta["tnt_oi_iv_layout"] = "v2"
    fig.savefig(buf, format="png", dpi=dpi_used, facecolor=fig.get_facecolor(), metadata=meta)
    plt.close(fig)
    return buf.getvalue()
