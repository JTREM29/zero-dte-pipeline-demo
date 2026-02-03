from __future__ import annotations

import os
from typing import Any, Iterable


TNT_DARK_BG = "#0b0f14"
TNT_FG = "#c9d1d9"
TNT_GRID = "#2d333b"


def tnt_render_stamp_visible() -> bool:
    """Whether to draw a visible per-render host stamp on charts.

    Default: off. Enable only for debugging.
    """

    truthy = {"1", "true", "yes", "y", "on"}
    # Hard gate: require explicit allow to prevent accidental leakage
    # from misconfigured worker environments.
    allow = (os.getenv("TNT_RENDER_STAMP_ALLOW", "0") or "0").strip().lower()
    if allow not in truthy:
        return False
    v = (os.getenv("TNT_RENDER_STAMP_VISIBLE", "0") or "0").strip().lower()
    return v in truthy


def tnt_png_metadata(*, build: str = "") -> dict[str, str]:
    """Return invisible PNG metadata for attribution/auditing."""

    meta: dict[str, str] = {}
    try:
        if tnt_render_stamp_visible():
            meta["tnt_debug"] = "1"
    except Exception:
        pass
    try:
        tag = _tnt_render_tag()
        if tag:
            meta["tnt_render_tag"] = str(tag)
    except Exception:
        pass

    b = (build or os.getenv("TNT_BUILD") or "").strip()
    if b:
        meta["tnt_build"] = str(b)
    return meta


def _tnt_render_tag() -> str:
    tag = (os.getenv("TNT_RENDER_TAG") or "").strip()
    if tag:
        return tag
    tag = (os.getenv("COMPUTERNAME") or "").strip()
    return tag or "TNT?"


def stamp_tnt_render_tag_fig(fig: Any, *, fontsize: int = 9, alpha: float = 0.70) -> None:
    """Stamp bottom-left tag so charts self-identify the render host.

    Uses env var TNT_RENDER_TAG; falls back to COMPUTERNAME.
    Idempotent per-figure.
    """

    try:
        if not tnt_render_stamp_visible():
            return
        if fig is None:
            return
        if bool(getattr(fig, "_tnt_render_tag_stamped", False)):
            return
        label = _tnt_render_tag()
        fig.text(
            0.01,
            0.01,
            str(label),
            transform=fig.transFigure,
            fontsize=int(fontsize),
            color=TNT_FG,
            alpha=float(alpha),
            ha="left",
            va="bottom",
            zorder=10,
        )
        setattr(fig, "_tnt_render_tag_stamped", True)
    except Exception:
        return


def apply_tnt_dark_rcparams(matplotlib: Any) -> None:
    """Apply TNT dark theme defaults via matplotlib rcParams.

    Best-effort + idempotent. Safe to call repeatedly.
    """

    try:
        rc = getattr(matplotlib, "rcParams", None)
        if rc is None:
            return

        rc.update(
            {
                "figure.facecolor": TNT_DARK_BG,
                "savefig.facecolor": TNT_DARK_BG,
                "axes.facecolor": TNT_DARK_BG,
                "axes.edgecolor": TNT_GRID,
                "axes.labelcolor": TNT_FG,
                "axes.titlecolor": TNT_FG,
                "xtick.color": TNT_FG,
                "ytick.color": TNT_FG,
                "text.color": TNT_FG,
                "grid.color": TNT_GRID,
                "grid.alpha": 0.12,
                "grid.linestyle": "--",
                "legend.framealpha": 0.15,
                "legend.facecolor": TNT_DARK_BG,
                "legend.edgecolor": TNT_GRID,
                "font.size": 10,
            }
        )
    except Exception:
        return


def style_tnt_dark_axes(ax: Any, *, grid: bool = True) -> None:
    """Apply TNT dark styling to a single matplotlib Axes."""

    try:
        ax.set_facecolor(TNT_DARK_BG)
        ax.tick_params(colors=TNT_FG)
        for spine in getattr(ax, "spines", {}).values():
            try:
                spine.set_color(TNT_GRID)
            except Exception:
                continue
        if grid:
            try:
                ax.grid(True, alpha=0.12, linestyle="--")
            except Exception:
                pass
    except Exception:
        return


def style_tnt_dark_figure(fig: Any) -> None:
    """Apply TNT dark styling to a matplotlib Figure."""

    try:
        fig.patch.set_facecolor(TNT_DARK_BG)
    except Exception:
        return


def style_tnt_dark(fig: Any, axes: Any | Iterable[Any] | None = None) -> None:
    """Convenience: style figure and provided axes (or fig.axes)."""

    style_tnt_dark_figure(fig)
    try:
        if axes is None:
            axes_list = list(getattr(fig, "axes", []) or [])
        elif isinstance(axes, (list, tuple)):
            axes_list = list(axes)
        else:
            axes_list = [axes]
        for ax in axes_list:
            style_tnt_dark_axes(ax)
    except Exception:
        return


def add_tnt_watermark_ax(ax: Any, *, text: str = "TNT", fontsize: int = 96, alpha: float = 0.06) -> None:
    """Add a subtle TNT watermark behind plotted data on a single Axes."""

    try:
        stamp_tnt_render_tag_fig(getattr(ax, "figure", None))

        try:
            ax.patch.set_zorder(0)
        except Exception:
            pass

        ax.text(
            0.5,
            0.5,
            str(text),
            transform=ax.transAxes,
            fontsize=int(fontsize),
            color="#ffffff",
            alpha=float(alpha),
            ha="center",
            va="center",
            weight="bold",
            zorder=0.5,
        )
    except Exception:
        return


def add_tnt_watermark_fig(fig: Any, *, text: str = "TNT", fontsize: int = 96, alpha: float = 0.045) -> None:
    """Centered watermark for multi-subplot charts, behind all axes."""

    try:
        stamp_tnt_render_tag_fig(fig)

        try:
            for ax in list(getattr(fig, "axes", []) or []):
                try:
                    ax.patch.set_zorder(0)
                except Exception:
                    continue
        except Exception:
            pass

        fig.text(
            0.5,
            0.5,
            str(text),
            transform=fig.transFigure,
            fontsize=int(fontsize),
            color="#ffffff",
            alpha=float(alpha),
            ha="center",
            va="center",
            weight="bold",
            zorder=0.5,
        )
    except Exception:
        return
