from __future__ import annotations

import datetime as dt
import io
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import redis

from delivery.tnt_chart_style import (
    TNT_FG,
    add_tnt_watermark_fig,
    apply_tnt_dark_rcparams,
    style_tnt_dark_axes,
    style_tnt_dark_figure,
    tnt_png_metadata,
)

# We import heavy deps lazily inside render() so the bot can start even when
# Databento/matplotlib aren't installed (or are CLX-only).

_ET = ZoneInfo("America/New_York")

_ETF_ANCHORS: dict[str, str] = {
    "ES": "SPY",
    "NQ": "QQQ",
    "RTY": "IWM",
}


def _repo_root() -> Path:
    # services/futures/futures_chart.py -> repo root is parents[2]
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()


def _dotenv_get_value(path: Path, key: str) -> str | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        val = v.strip()
        if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
            val = val[1:-1]
        return val.strip()
    return None


def _get_setting(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw and raw.strip():
        return raw.strip()

    root = _repo_root()
    for env_path in (root / ".env.local", root / ".env"):
        val = _dotenv_get_value(env_path, key)
        if val and val.strip():
            return val.strip()

    return default


def _redis() -> redis.Redis:
    host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("TNT_REDIS_PORT", "6379"))
    rdb = int(os.getenv("TNT_REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=rdb, decode_responses=True)


def _fut_last_key(sym: str) -> str:
    return f"fut:{sym}:last"


def _db_sym(root: str) -> str:
    return f"{root}.c.0"


@dataclass
class Bars:
    t: List[int]  # epoch seconds UTC
    o: List[float]
    h: List[float]
    l: List[float]
    c: List[float]
    v: List[float]


def _trend_arrow(x: float) -> str:
    if x > 0:
        return "↑"
    if x < 0:
        return "↓"
    return "→"


def _vwap_side(last_dist: float) -> str:
    if last_dist >= 0.0006:
        return "Above"
    if last_dist <= -0.0006:
        return "Below"
    return "Near"


def _impulse_bps(close: list[float]) -> float:
    # 5-minute impulse in bps (best-effort).
    if len(close) < 7:
        return 0.0
    base = float(close[-6])
    if base == 0.0:
        return 0.0
    return (float(close[-1]) - base) / base * 10000.0


def _trend_slope(close: list[float]) -> float:
    # 30-minute slope (best-effort): last minus 30 bars ago.
    if len(close) < 31:
        return 0.0
    return float(close[-1]) - float(close[-31])


def _now_utc() -> int:
    return int(time.time())


_RE_AVAILABLE_UP_TO = re.compile(r"available up to '([^']+)'", re.IGNORECASE)


def _parse_available_end_utc_from_exc(exc: Exception) -> int | None:
    """Best-effort: parse Databento 422 error text to find the dataset available end."""

    msg = str(exc) or ""
    m = _RE_AVAILABLE_UP_TO.search(msg)
    if not m:
        return None
    raw = (m.group(1) or "").strip()
    if not raw:
        return None
    try:
        # Example: "2026-01-19 01:30:00+00:00"
        dt_utc = dt.datetime.fromisoformat(raw)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=dt.timezone.utc)
        return int(dt_utc.timestamp())
    except Exception:
        return None


def _fetch_ohlcv_1m(
    *,
    client: object,
    dataset: str,
    symbol: str,
    lookback_min: int,
    stype_in: str,
) -> Bars:
    # Databento Historical datasets can lag. Use a small end-lag to avoid
    # "end_after_available_end" errors during live hours.
    try:
        lag_sec = int(os.getenv("DATABENTO_END_LAG_SEC", "120"))
    except Exception:
        lag_sec = 120
    lag_sec = max(0, min(lag_sec, 3600))

    end = _now_utc() - lag_sec
    start = end - int(lookback_min) * 60

    start_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start))
    end_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end))

    # Databento expects schema "ohlcv-1m" for 1m bars.
    ts = getattr(getattr(client, "timeseries", None), "get_range", None)
    if not callable(ts):
        return Bars([], [], [], [], [], [])

    def _call_range(cur_start: int, cur_end: int):
        cur_start_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cur_start))
        cur_end_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cur_end))
        return ts(
            dataset=dataset,
            schema="ohlcv-1m",
            symbols=[symbol],
            stype_in=stype_in,
            start=cur_start_s,
            end=cur_end_s,
        )

    def _clamp_to_available(exc: Exception, cur_start: int, cur_end: int) -> tuple[int, int] | None:
        avail_end = _parse_available_end_utc_from_exc(exc)
        if avail_end is None:
            return None
        new_end = min(int(cur_end), int(avail_end))
        new_start = min(int(cur_start), new_end - 60)
        new_start = max(0, new_start)
        return new_start, new_end

    try:
        data = _call_range(start, end)
    except Exception as exc:
        clamped = _clamp_to_available(exc, start, end)
        if clamped is None:
            raise
        start, end = clamped
        data = _call_range(start, end)

    to_df = getattr(data, "to_df", None)
    if not callable(to_df):
        return Bars([], [], [], [], [], [])

    try:
        df = to_df()
    except Exception as exc:
        # Some Databento client versions raise 422 errors during materialization.
        clamped = _clamp_to_available(exc, start, end)
        if clamped is None:
            raise
        start, end = clamped
        data = _call_range(start, end)
        to_df2 = getattr(data, "to_df", None)
        if not callable(to_df2):
            return Bars([], [], [], [], [], [])
        df = to_df2()
    if df is None or len(df) == 0:
        return Bars([], [], [], [], [], [])

    # Databento OHLCV often returns ts_event as the DatetimeIndex (not a column).
    import pandas as pd  # type: ignore

    if "ts_event" in df.columns:
        ts_values = df["ts_event"]
    else:
        ts_values = df.index

    ts_col = pd.to_datetime(ts_values, utc=True)
    t: List[int] = []
    for x in ts_col:
        try:
            t.append(int(x.value // 1_000_000_000))
            continue
        except Exception:
            pass
        try:
            t.append(int(x.timestamp()))
            continue
        except Exception:
            pass
        try:
            t.append(int(x))
            continue
        except Exception:
            t.append(0)

    return Bars(
        t=t,
        o=[float(x) for x in df["open"].tolist()],
        h=[float(x) for x in df["high"].tolist()],
        l=[float(x) for x in df["low"].tolist()],
        c=[float(x) for x in df["close"].tolist()],
        v=[float(x) for x in df["volume"].tolist()],
    )


def _vwap(b: Bars) -> Optional[List[float]]:
    if not b.t:
        return None

    cum_pv = 0.0
    cum_v = 0.0
    out: List[float] = []
    for h, l, c, v in zip(b.h, b.l, b.c, b.v):
        tp = (h + l + c) / 3.0
        cum_pv += tp * v
        cum_v += v
        out.append(cum_pv / cum_v if cum_v > 0 else c)
    return out


def _dist_pct(close: list[float], vwap: list[float]) -> list[float]:
    out: list[float] = []
    for c, v in zip(close, vwap):
        try:
            denom = float(v) if float(v) != 0.0 else float(c)
            out.append((float(c) - float(v)) / denom)
        except Exception:
            out.append(0.0)
    return out


def _label(root: str) -> str:
    anchor = _ETF_ANCHORS.get(root)
    if anchor:
        return f"{root} ({anchor})"
    return root


def _context_phrase(*, root: str, dist: list[float], dist_es: list[float] | None) -> str:
    if not dist:
        return "Context: —"

    last = float(dist[-1])
    recent = dist[-20:] if len(dist) >= 20 else dist
    rng = (max(recent) - min(recent)) if recent else 0.0
    crossed_recently = any((x > 0.0) for x in (dist[-15:] if len(dist) >= 15 else dist)) and any(
        (x < 0.0) for x in (dist[-15:] if len(dist) >= 15 else dist)
    )

    # Compression: hugging VWAP with low dispersion.
    if abs(last) <= 0.0006 and rng <= 0.0016:
        base = "Context: VWAP compression"
    elif last >= 0.0012:
        base = "Context: Above VWAP • Holding premium" if not crossed_recently else "Context: Above VWAP • Volatile"
    elif last <= -0.0012:
        base = "Context: Below VWAP • Weak reclaim" if crossed_recently else "Context: Below VWAP • Heavy"
    else:
        base = "Context: Near VWAP • Balanced"

    # Relative note vs ES for non-ES panels.
    if root != "ES" and dist_es:
        try:
            last_es = float(dist_es[-1])
            delta = last - last_es
            if delta <= -0.0010:
                base += "\nContext: Relative weakness vs ES"
            elif delta >= 0.0010:
                base += "\nContext: Relative strength vs ES"
        except Exception:
            pass

    return base


def render_futures_chart_png(
    *,
    roots: Tuple[str, ...] = ("ES", "NQ", "RTY"),
    lookback_min: int = 180,
    include_vwap: bool = True,
) -> bytes:
    try:
        import matplotlib

        matplotlib.use("Agg")
        apply_tnt_dark_rcparams(matplotlib)
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except Exception as exc:
        raise RuntimeError("matplotlib not installed") from exc

    try:
        import databento as db  # type: ignore
    except Exception as exc:
        raise RuntimeError("market data client not installed (required for futures charts)") from exc

    api_key = _get_setting("DATABENTO_API_KEY", "").strip()
    if not api_key.startswith("db-"):
        raise RuntimeError("DATABENTO_API_KEY missing/invalid")

    dataset = _get_setting("DATABENTO_DATASET", "GLBX.MDP3")

    hist = db.Historical(api_key)
    r = _redis()

    # Databento defaults stype_in=raw_symbol. Our futures symbols are continuous
    # by default (e.g., ES.c.0), so default to stype_in=continuous.
    stype_in = (_get_setting("DATABENTO_STYPE_IN", "continuous") or "continuous").strip()

    try:
        roots_s = ",".join(roots)
    except Exception:
        roots_s = "?"
    print(f"[futures_chart] dataset={dataset} stype_in={stype_in} roots={roots_s} lookback_min={int(lookback_min)}")

    bars_map: Dict[str, Bars] = {}
    stamps: Dict[str, Dict[str, str]] = {}

    vwap_map: Dict[str, List[float] | None] = {}
    dist_map: Dict[str, List[float]] = {}

    for root in roots:
        bars_map[root] = _fetch_ohlcv_1m(
            client=hist,
            dataset=dataset,
            symbol=_db_sym(root),
            lookback_min=int(lookback_min),
            stype_in=stype_in,
        )
        stamps[root] = r.hgetall(_fut_last_key(root)) or {}

        vw = _vwap(bars_map[root]) if include_vwap else None
        vwap_map[root] = vw
        dist_map[root] = _dist_pct(bars_map[root].c, vw) if (vw and bars_map[root].c) else []

    fig_h = 10.0
    fig_w = 16.0
    fig, axes = plt.subplots(
        nrows=len(roots),
        ncols=1,
        figsize=(fig_w, fig_h),
        sharex=True,
        gridspec_kw={"hspace": 0.42},
    )
    if len(roots) == 1:
        axes = [axes]

    style_tnt_dark_figure(fig)
    fig.suptitle(
        f"TNT Futures — Market Context (1m • {int(lookback_min)}m)",
        color=TNT_FG,
        fontweight="bold",
    )

    close_color = "#58a6ff"  # TNT dark theme: readable blue
    vwap_color = TNT_FG
    shade_green = "#00ff66"
    shade_red = "#ff3344"

    dist_es = dist_map.get("ES") or []

    for i, root in enumerate(roots):
        ax = axes[i]
        b = bars_map[root]
        if not b.t:
            style_tnt_dark_axes(ax, grid=False)
            ax.text(0.5, 0.5, f"{_label(root)}: no data", ha="center", va="center", color=TNT_FG)
            ax.set_axis_off()
            continue

        style_tnt_dark_axes(ax)

        x = [dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).astimezone(_ET) for ts in b.t]
        ax.plot(x, b.c, linewidth=0.95, color=close_color, label="Price")

        vw = vwap_map.get(root)
        if include_vwap and vw:
            ax.plot(x, vw, linewidth=2.1, color=vwap_color, label="VWAP")

            # Context shading: above VWAP = green, below = red.
            try:
                ax.fill_between(x, b.c, vw, where=[c >= v for c, v in zip(b.c, vw)], color=shade_green, alpha=0.06)
                ax.fill_between(x, b.c, vw, where=[c < v for c, v in zip(b.c, vw)], color=shade_red, alpha=0.06)
            except Exception:
                pass

        last = stamps.get(root, {})
        px = last.get("px")
        chg_pct = last.get("chg_pct")
        ts_utc = last.get("ts_utc")

        stamp = _label(root)
        if px is not None and chg_pct is not None:
            stamp += f"  •  {px}  ({chg_pct}%)"
        if ts_utc:
            try:
                age = _now_utc() - int(ts_utc)
                stamp += f"  •  age {age}s"
            except Exception:
                pass

        ax.set_title(stamp)
        ax.grid(True, alpha=0.12, linestyle="--")

        # Minimal framing.
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        # DST-correct ET axis.
        locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_ET))

        if i == len(roots) - 1:
            ax.set_xlabel("Time (ET)")

        # Rule-based context phrase (bottom-left).
        phrase = _context_phrase(root=root, dist=dist_map.get(root) or [], dist_es=dist_es)
        ax.text(
            0.01,
            0.03,
            phrase,
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=9,
            color="#8b949e",
        )

        # Keep legend subtle.
        ax.legend(loc="upper left", fontsize=9, framealpha=0.15)

    # One watermark for the whole multi-panel chart.
    add_tnt_watermark_fig(fig)

    buf = io.BytesIO()
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(buf, format="png", dpi=140, facecolor=fig.get_facecolor(), metadata=tnt_png_metadata())
    plt.close(fig)
    return buf.getvalue()


def render_futures_chart_png_with_metrics(
    *,
    roots: Tuple[str, ...] = ("ES", "NQ", "RTY"),
    lookback_min: int = 180,
    include_vwap: bool = True,
) -> tuple[bytes, dict[str, dict[str, object]]]:
    """Render chart PNG plus per-root metrics for the Discord header block."""

    # Leverage the existing renderer logic by re-running it here with a small
    # shared prefetch for metrics.
    try:
        import databento as db  # type: ignore
    except Exception:
        # If we can't compute metrics, still let the chart render path raise the right error.
        return render_futures_chart_png(roots=roots, lookback_min=lookback_min, include_vwap=include_vwap), {}

    api_key = _get_setting("DATABENTO_API_KEY", "").strip()
    if not api_key.startswith("db-"):
        return render_futures_chart_png(roots=roots, lookback_min=lookback_min, include_vwap=include_vwap), {}

    dataset = _get_setting("DATABENTO_DATASET", "GLBX.MDP3")
    stype_in = (_get_setting("DATABENTO_STYPE_IN", "continuous") or "continuous").strip()

    hist = db.Historical(api_key)

    bars_map: Dict[str, Bars] = {}
    vwap_map: Dict[str, List[float] | None] = {}
    dist_map: Dict[str, List[float]] = {}

    for root in roots:
        bars_map[root] = _fetch_ohlcv_1m(
            client=hist,
            dataset=dataset,
            symbol=_db_sym(root),
            lookback_min=int(lookback_min),
            stype_in=stype_in,
        )
        vw = _vwap(bars_map[root]) if include_vwap else None
        vwap_map[root] = vw
        dist_map[root] = _dist_pct(bars_map[root].c, vw) if (vw and bars_map[root].c) else []

    metrics: dict[str, dict[str, object]] = {}
    for root in roots:
        b = bars_map.get(root)
        if not b or not b.c:
            continue
        dist = dist_map.get(root) or []
        last_dist = float(dist[-1]) if dist else 0.0
        metrics[root] = {
            "label": _label(root),
            "trend_arrow": _trend_arrow(_trend_slope(b.c)),
            "vwap_side": _vwap_side(last_dist) if include_vwap else "—",
            "impulse_bps": float(f"{_impulse_bps(b.c):+.0f}"),
        }

    png = render_futures_chart_png(roots=roots, lookback_min=lookback_min, include_vwap=include_vwap)
    return png, metrics
