from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import math
import os

import pandas as pd


@dataclass
class Pivot:
    idx: int
    ts: pd.Timestamp
    value: float


@dataclass
class DivergenceResult:
    kind: str  # "bullish_rsi_divergence" | "bearish_rsi_divergence" | "none"
    timeframe: str
    rsi_period: int
    left_right: int
    lookback_bars: int
    strength: float
    price_pivots: List[Pivot]
    rsi_pivots: List[Pivot]
    summary: str
    details: Dict[str, Any]


def _fmt_oi_wall_line(symbol: str, last_px: float, walls) -> str:
    if not walls or (getattr(walls, "call_wall", None) is None and getattr(walls, "put_wall", None) is None):
        return "\n**OI Walls:** unavailable"

    def dist_strike(strike: float):
        pts = float(strike) - float(last_px)
        pct = (pts / float(last_px)) * 100.0 if float(last_px) else 0.0
        return pts, pct

    parts = ["\n**OI Walls:**"]
    call_wall = getattr(walls, "call_wall", None)
    put_wall = getattr(walls, "put_wall", None)

    if call_wall is not None:
        pts, pct = dist_strike(float(call_wall))
        parts.append(f"Call wall {float(call_wall):.0f} ({pts:+.2f}, {pct:+.2f}%)")
    if put_wall is not None:
        pts, pct = dist_strike(float(put_wall))
        parts.append(f"Put wall {float(put_wall):.0f} ({pts:+.2f}, {pct:+.2f}%)")

    meta = []
    exp = getattr(walls, "expiry", None)
    ts_et = getattr(walls, "ts_et", None)
    if exp:
        meta.append(f"exp {exp}")
    if ts_et:
        meta.append(f"asof {ts_et}")
    if meta:
        parts.append(f"({' | '.join(meta)})")

    return " | ".join(parts)


# Back-compat for older naming used in some prompts/tests.
def _fmt_oi_walls_line(symbol: str, last_px: float, walls) -> str:
    return _fmt_oi_wall_line(symbol, last_px, walls)


def _oi_tape_read(res_kind: str, last_px: float, walls) -> str:
    if not walls:
        return ""

    near_pct = 0.30  # 30 bps

    def near(strike: float | None) -> bool:
        if strike is None or not last_px:
            return False
        try:
            return abs((float(strike) - float(last_px)) / float(last_px) * 100.0) <= float(near_pct)
        except Exception:
            return False

    call_wall = getattr(walls, "call_wall", None)
    put_wall = getattr(walls, "put_wall", None)
    call_near = near(call_wall)
    put_near = near(put_wall)

    if res_kind == "bearish_rsi_divergence" and call_near and call_wall is not None and float(last_px) <= float(call_wall):
        return "\n**OI tape read:** bearish divergence **into call wall** → upside may be capped / rejection risk."
    if res_kind == "bullish_rsi_divergence" and put_near and put_wall is not None and float(last_px) >= float(put_wall):
        return "\n**OI tape read:** bullish divergence **into put wall** → downside may be supported / bounce risk."
    if res_kind != "none" and (call_near or put_near):
        return "\n**OI tape read:** OI wall nearby → expect pin/chop risk around that strike."
    return ""


# Back-compat for older naming used in some prompts/tests.
def _oi_wall_tape_read(kind: str, last_px: float, walls) -> str:
    return _oi_tape_read(kind, last_px, walls)


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Deterministic RSI (Wilder smoothing via EMA-like alpha=1/period)."""

    close = close.astype(float)
    delta = close.diff()

    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    alpha = 1.0 / float(period)
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0.0, math.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # backfill initial NaNs deterministically
    return rsi.bfill()


def _pivot_highs(series: pd.Series, left_right: int) -> List[int]:
    idxs: List[int] = []
    n = int(left_right)
    vals = series.values
    for i in range(n, len(series) - n):
        center = vals[i]
        if not math.isfinite(float(center)):
            continue
        left = vals[i - n : i]
        right = vals[i + 1 : i + 1 + n]
        if float(center) > float(left.max()) and float(center) >= float(right.max()):
            idxs.append(i)
    return idxs


def _pivot_lows(series: pd.Series, left_right: int) -> List[int]:
    idxs: List[int] = []
    n = int(left_right)
    vals = series.values
    for i in range(n, len(series) - n):
        center = vals[i]
        if not math.isfinite(float(center)):
            continue
        left = vals[i - n : i]
        right = vals[i + 1 : i + 1 + n]
        if float(center) < float(left.min()) and float(center) <= float(right.min()):
            idxs.append(i)
    return idxs


def _to_pivots(idxs: List[int], series: pd.Series, ts: pd.Series) -> List[Pivot]:
    pivs: List[Pivot] = []
    for i in idxs:
        pivs.append(Pivot(idx=int(i), ts=pd.Timestamp(ts.iloc[i]), value=float(series.iloc[i])))
    return pivs


def _latest_two_pivots(pivots: List[Pivot]) -> Optional[Tuple[Pivot, Pivot]]:
    if len(pivots) < 2:
        return None
    return pivots[-2], pivots[-1]


def _align_rsi_pivots_to_price_pivots(
    price_pair: Tuple[Pivot, Pivot],
    rsi_pivots: List[Pivot],
    max_bar_gap: int = 6,
) -> Optional[Tuple[Pivot, Pivot]]:
    """Align RSI pivots to the price pivot indices by nearest pivot within a small bar window."""

    p1, p2 = price_pair

    def nearest(target_idx: int) -> Optional[Pivot]:
        candidates = [p for p in rsi_pivots if abs(int(p.idx) - int(target_idx)) <= int(max_bar_gap)]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (abs(int(p.idx) - int(target_idx)), int(p.idx)))
        return candidates[0]

    r1 = nearest(p1.idx)
    r2 = nearest(p2.idx)
    if not r1 or not r2:
        return None
    if int(r1.idx) == int(r2.idx):
        return None
    return r1, r2


def _strength_score(price_delta: float, rsi_delta: float) -> float:
    """Deterministic strength heuristic in [0,1]."""

    pd_norm = min(1.0, abs(float(price_delta)) / (abs(float(price_delta)) + 1.0))
    rd_norm = min(1.0, abs(float(rsi_delta)) / 20.0)
    s = 0.35 * pd_norm + 0.65 * rd_norm
    return max(0.0, min(1.0, float(s)))


def detect_rsi_divergence(
    ohlc: pd.DataFrame,
    *,
    timeframe: str,
    rsi_period: int = 14,
    left_right: int = 3,
    lookback_bars: int = 150,
    max_bar_gap: int = 6,
) -> DivergenceResult:
    """Detect bullish/bearish RSI divergence using the last two swing pivots."""

    min_bars = max(int(lookback_bars), int(rsi_period) + 10)
    if ohlc is None or len(ohlc) < min_bars:
        return DivergenceResult(
            kind="none",
            timeframe=timeframe,
            rsi_period=int(rsi_period),
            left_right=int(left_right),
            lookback_bars=int(lookback_bars),
            strength=0.0,
            price_pivots=[],
            rsi_pivots=[],
            summary="Not enough data to evaluate divergence.",
            details={"reason": "insufficient_bars"},
        )

    df = ohlc.copy()

    if "ts" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index().rename(columns={"index": "ts"})
        else:
            raise ValueError("ohlc must include 'ts' column or a DatetimeIndex")

    for col in ("high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"ohlc missing required column: {col}")

    df = df.tail(int(lookback_bars)).reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=False, errors="coerce")

    rsi = compute_rsi(df["close"], period=int(rsi_period))

    hi_idxs = _pivot_highs(df["high"], left_right=int(left_right))
    lo_idxs = _pivot_lows(df["low"], left_right=int(left_right))
    price_highs = _to_pivots(hi_idxs, df["high"], df["ts"])
    price_lows = _to_pivots(lo_idxs, df["low"], df["ts"])

    rsi_hi_idxs = _pivot_highs(rsi, left_right=int(left_right))
    rsi_lo_idxs = _pivot_lows(rsi, left_right=int(left_right))
    rsi_highs = _to_pivots(rsi_hi_idxs, rsi, df["ts"])
    rsi_lows = _to_pivots(rsi_lo_idxs, rsi, df["ts"])

    bear_pair = _latest_two_pivots(price_highs)
    if bear_pair:
        aligned = _align_rsi_pivots_to_price_pivots(bear_pair, rsi_highs, max_bar_gap=int(max_bar_gap))
        if aligned:
            p1, p2 = bear_pair
            r1, r2 = aligned
            price_delta = float(p2.value) - float(p1.value)
            rsi_delta = float(r2.value) - float(r1.value)
            if price_delta > 0 and rsi_delta < 0:
                strength = _strength_score(price_delta, rsi_delta)
                summary = (
                    f"Bearish divergence ({timeframe} RSI{int(rsi_period)}): "
                    f"price HH {p1.value:.2f}→{p2.value:.2f} while RSI LH {r1.value:.1f}→{r2.value:.1f}."
                )
                return DivergenceResult(
                    kind="bearish_rsi_divergence",
                    timeframe=timeframe,
                    rsi_period=int(rsi_period),
                    left_right=int(left_right),
                    lookback_bars=int(lookback_bars),
                    strength=float(strength),
                    price_pivots=[p1, p2],
                    rsi_pivots=[r1, r2],
                    summary=summary,
                    details={
                        "price_delta": price_delta,
                        "rsi_delta": rsi_delta,
                        "price_pivot_ts": [str(p1.ts), str(p2.ts)],
                        "rsi_pivot_ts": [str(r1.ts), str(r2.ts)],
                        "max_bar_gap": int(max_bar_gap),
                    },
                )

    bull_pair = _latest_two_pivots(price_lows)
    if bull_pair:
        aligned = _align_rsi_pivots_to_price_pivots(bull_pair, rsi_lows, max_bar_gap=int(max_bar_gap))
        if aligned:
            p1, p2 = bull_pair
            r1, r2 = aligned
            price_delta = float(p2.value) - float(p1.value)
            rsi_delta = float(r2.value) - float(r1.value)
            if price_delta < 0 and rsi_delta > 0:
                strength = _strength_score(price_delta, rsi_delta)
                summary = (
                    f"Bullish divergence ({timeframe} RSI{int(rsi_period)}): "
                    f"price LL {p1.value:.2f}→{p2.value:.2f} while RSI HL {r1.value:.1f}→{r2.value:.1f}."
                )
                return DivergenceResult(
                    kind="bullish_rsi_divergence",
                    timeframe=timeframe,
                    rsi_period=int(rsi_period),
                    left_right=int(left_right),
                    lookback_bars=int(lookback_bars),
                    strength=float(strength),
                    price_pivots=[p1, p2],
                    rsi_pivots=[r1, r2],
                    summary=summary,
                    details={
                        "price_delta": price_delta,
                        "rsi_delta": rsi_delta,
                        "price_pivot_ts": [str(p1.ts), str(p2.ts)],
                        "rsi_pivot_ts": [str(r1.ts), str(r2.ts)],
                        "max_bar_gap": int(max_bar_gap),
                    },
                )

    return DivergenceResult(
        kind="none",
        timeframe=timeframe,
        rsi_period=int(rsi_period),
        left_right=int(left_right),
        lookback_bars=int(lookback_bars),
        strength=0.0,
        price_pivots=[],
        rsi_pivots=[],
        summary=f"No clean RSI divergence detected on {timeframe}.",
        details={
            "reason": "no_match",
            "price_high_pivots": int(len(price_highs)),
            "price_low_pivots": int(len(price_lows)),
            "rsi_high_pivots": int(len(rsi_highs)),
            "rsi_low_pivots": int(len(rsi_lows)),
        },
    )


def format_divergence_market_speak(res: DivergenceResult) -> str:
    """Market-language formatter anchored to computed pivots."""

    if res.kind == "none":
        return f"**Divergence:** None detected ({res.timeframe} RSI{res.rsi_period})."

    p1, p2 = res.price_pivots
    r1, r2 = res.rsi_pivots

    if res.kind == "bearish_rsi_divergence":
        return (
            f"**Bearish divergence** ({res.timeframe} RSI{res.rsi_period})\n"
            f"- Price: **higher high** {p1.value:.2f} → {p2.value:.2f}\n"
            f"- RSI: **lower high** {r1.value:.1f} → {r2.value:.1f}\n"
            f"- Read: momentum is **fading** into new highs (often precedes pullback or chop).\n"
            f"- Strength: {res.strength:.2f}\n"
        )

    if res.kind == "bullish_rsi_divergence":
        return (
            f"**Bullish divergence** ({res.timeframe} RSI{res.rsi_period})\n"
            f"- Price: **lower low** {p1.value:.2f} → {p2.value:.2f}\n"
            f"- RSI: **higher low** {r1.value:.1f} → {r2.value:.1f}\n"
            f"- Read: selling pressure is **waning** (often precedes bounce or basing).\n"
            f"- Strength: {res.strength:.2f}\n"
        )

    return f"**Divergence:** {res.kind} (unhandled formatter)."


def divergence_trade_plan(res: DivergenceResult, *, symbol: str, bars_df: pd.DataFrame | None = None) -> str:
    """Deterministic trigger/invalidation derived from detected pivots.

    No targets, no forecasting. Just a concrete setup you can risk-manage.
    """

    def get_futures_regime_safe() -> str | None:
        try:
            from services.futures.futures_store import FuturesStore
            import redis

            host = (os.getenv("TNT_REDIS_HOST") or "127.0.0.1").strip()
            port_s = (os.getenv("TNT_REDIS_PORT") or "6379").strip()
            db_s = (os.getenv("TNT_REDIS_DB") or "0").strip()
            try:
                port = int(port_s)
            except Exception:
                port = 6379
            try:
                db = int(db_s)
            except Exception:
                db = 0

            r = redis.Redis(
                host=host,
                port=port,
                db=db,
                decode_responses=True,
                socket_connect_timeout=0.35,
                socket_timeout=0.75,
            )

            scores = FuturesStore(r=r).get_scores()
            if not scores:
                return None
            reg = getattr(scores, "regime", None)
            if reg is None:
                return None
            reg_s = str(reg).strip().upper()
            return reg_s or None
        except Exception:
            return None

    def _append_context(
        plan_body: str,
        *,
        vwap_line: str = "",
        bias_line: str = "",
        oi_line: str = "",
        oi_read: str = "",
    ) -> str:
        base = plan_body
        if vwap_line:
            base = f"{base}{vwap_line}"
        if bias_line:
            base = f"{base}{bias_line}"
        if oi_line:
            base = f"{base}{oi_line}"
        if oi_read:
            base = f"{base}{oi_read}"

        regime = get_futures_regime_safe()
        if not regime:
            return f"{base}\n\n**Context:** Futures regime = UNKNOWN → context unavailable"

        if str(res.kind).startswith("bearish") and regime == "RISK_OFF":
            context = "aligned (risk-off tape favors fades)"
        elif str(res.kind).startswith("bullish") and regime == "RISK_ON":
            context = "aligned (risk-on tape favors dip buys)"
        else:
            context = "counter-trend (higher chop / fakeout risk)"

        return f"{base}\n\n**Context:** Futures regime = {regime} → {context}"

    # Best-effort last price (deterministic)
    last_px: float | None = None
    if bars_df is not None:
        try:
            last_px = float(pd.to_numeric(bars_df["close"], errors="coerce").dropna().iloc[-1])
        except Exception:
            last_px = None

    # VWAP enrichment (deterministic)
    vwap_line = ""
    bias_line = ""
    if bars_df is not None:
        try:
            from technicals.rth import filter_rth
            from technicals.vwap import compute_rth_vwap

            rth = filter_rth(bars_df)
            vwap = compute_rth_vwap(rth)
            if vwap is not None and float(vwap) > 0:
                if last_px is not None:
                    dist_pct = (float(last_px) - float(vwap)) / float(vwap) * 100.0
                    decision = abs(float(dist_pct)) <= 0.10  # within 10 bps

                    vwap_line = f"\n**VWAP (RTH):** {float(vwap):.2f} | Price {float(last_px):.2f} ({float(dist_pct):+.2f}%)"
                    if decision:
                        vwap_line += " → decision point"

                    if res.kind == "bearish_rsi_divergence" and float(last_px) >= float(vwap):
                        bias_line = "\n**Tape read:** bearish divergence **above VWAP** → fade risk (pullback/chop favored)."
                    elif res.kind == "bullish_rsi_divergence" and float(last_px) <= float(vwap):
                        bias_line = "\n**Tape read:** bullish divergence **below VWAP** → reclaim attempt (bounce/base favored)."
                    elif res.kind != "none":
                        bias_line = "\n**Tape read:** divergence present but VWAP positioning is mixed → chop / fakeout risk."
        except Exception:
            vwap_line = ""
            bias_line = ""

    # OI wall enrichment (deterministic, Redis-backed)
    oi_line = ""
    oi_read = ""
    if res is not None and res.kind != "none" and last_px is not None:
        try:
            from technicals.oi_walls import get_oi_walls

            walls = get_oi_walls(symbol)
            if walls is not None and (walls.call_wall is not None or walls.put_wall is not None):
                oi_line = _fmt_oi_wall_line(symbol, float(last_px), walls)
                oi_read = _oi_tape_read(str(res.kind), float(last_px), walls)
        except Exception:
            oi_line = ""
            oi_read = ""

    if not res or res.kind == "none":
        return _append_context(
            "Trade plan: no divergence setup right now.",
            vwap_line=vwap_line,
            bias_line=bias_line,
            oi_line=oi_line,
            oi_read=oi_read,
        )

    if len(res.price_pivots) < 2:
        return _append_context(
            "Trade plan: divergence detected but pivots were incomplete.",
            vwap_line=vwap_line,
            bias_line=bias_line,
            oi_line=oi_line,
            oi_read=oi_read,
        )

    p1, p2 = res.price_pivots[-2], res.price_pivots[-1]

    if res.kind == "bullish_rsi_divergence":
        trigger = float(p2.value)
        invalidation = float(min(p1.value, p2.value))
        body = (
            "Trade plan (bullish divergence)\n"
            f"- Trigger: reclaim/break above {trigger:.2f}\n"
            f"- Invalidation: lose {invalidation:.2f}\n"
            "- Watch next: follow-through + RSI holding above its prior pivot"
        )
        return _append_context(body, vwap_line=vwap_line, bias_line=bias_line, oi_line=oi_line, oi_read=oi_read)

    if res.kind == "bearish_rsi_divergence":
        trigger = float(p2.value)
        invalidation = float(max(p1.value, p2.value))
        body = (
            "Trade plan (bearish divergence)\n"
            f"- Trigger: lose/break below {trigger:.2f}\n"
            f"- Invalidation: reclaim {invalidation:.2f}\n"
            "- Watch next: rejection candles + RSI failing to regain its prior pivot"
        )
        return _append_context(body, vwap_line=vwap_line, bias_line=bias_line, oi_line=oi_line, oi_read=oi_read)

    return _append_context(
        "Trade plan: divergence detected, but kind was unknown.",
        vwap_line=vwap_line,
        bias_line=bias_line,
        oi_line=oi_line,
        oi_read=oi_read,
    )
