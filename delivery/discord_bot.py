"""Discord bot for Zero DTE pipeline automation."""
from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _f(x, nd=2):
    try:
        if x is None:
            return None
        return round(float(x), nd)
    except Exception:
        return None


def _fmt(x, nd=2):
    val = _f(x, nd)
    return "n/a" if val is None else f"{val:.{nd}f}"


def _delta(a, b, nd=2):
    a_val = _f(a, nd)
    b_val = _f(b, nd)
    if a_val is None or b_val is None:
        return None
    return round(a_val - b_val, nd)


def _is_num(x):
    try:
        float(x)
        return True
    except Exception:
        return False


def detect_regime(price: float, P: float, R1: float, R2: float, S1: float, S2: float, eps: float = 1e-9) -> str:
    """
    Regime based on where price is vs pivot bands.
    """

    if price is None or P is None or R1 is None or S1 is None:
        return "UNKNOWN"
    if R2 is not None and price > R2 + eps:
        return "EXTENSION_UP"
    if S2 is not None and price < S2 - eps:
        return "EXTENSION_DOWN"
    if price >= R1 + eps:
        return "ABOVE_R1"
    if price <= S1 - eps:
        return "BELOW_S1"
    if price >= P + eps:
        return "ABOVE_P"
    if price <= P - eps:
        return "BELOW_P"
    return "RANGE"


def ladder_levels(price: float, levels: Dict[str, Optional[float]], nd: int = 2) -> Dict[str, List[Tuple[str, float]]]:
    """
    Returns only *valid forward* targets.
    - If price is below S1, do NOT show S1 as a downside target.
    - If price is above R1, do NOT show R1 as an upside target.
    """

    price_val = _f(price, nd)
    P = _f(levels.get("P"), nd)
    R1 = _f(levels.get("R1"), nd)
    R2 = _f(levels.get("R2"), nd)
    R3 = _f(levels.get("R3"), nd)
    S1 = _f(levels.get("S1"), nd)
    S2 = _f(levels.get("S2"), nd)
    S3 = _f(levels.get("S3"), nd)

    ups = [("P", P), ("R1", R1), ("R2", R2), ("R3", R3)]
    dns = [("S1", S1), ("S2", S2), ("S3", S3)]

    ups_valid = [
        (k, v)
        for (k, v) in ups
        if v is not None and price_val is not None and v > price_val
    ]
    dns_valid = [
        (k, v)
        for (k, v) in dns
        if v is not None and price_val is not None and v < price_val
    ]

    ups_valid.sort(key=lambda kv: kv[1])
    dns_valid.sort(key=lambda kv: kv[1], reverse=True)

    return {"up": ups_valid, "down": dns_valid}


def vix_sqqq_confirmation(vix_dir: Optional[str], sqqq_dir: Optional[str]) -> str:
    """
    vix_dir / sqqq_dir expected: "up" / "down" / None
    """

    v = (vix_dir or "").lower().strip()
    q = (sqqq_dir or "").lower().strip()
    if v == "up" and q == "up":
        return "BEARISH"
    if v == "down" and q == "down":
        return "BULLISH"
    if v in ("up", "down") and q in ("up", "down"):
        return "MIXED"
    return "UNKNOWN"


def fmt_last_price(symbol: str, last_price: Optional[float], ts_utc: Optional[str], tf: str = "1m") -> str:
    symbol = (symbol or "").upper()
    tf_label = f"{tf} close".strip() or "1m close"
    ts_clean = (ts_utc or "").strip()
    ts_display = "n/a"
    if ts_clean and ts_clean.lower() != "unknown":
        try:
            ts_dt = parse_iso(ts_clean)
            ts_display = ts_dt.astimezone(ET).strftime("%H:%M ET")
        except Exception:  # noqa: BLE001 - timestamp formatting must not fail
            ts_display = ts_clean

    if last_price is None:
        return f"💲 **Last Price:** **n/a** ({tf_label} | {ts_display})"

    try:
        price_val = float(last_price)
    except Exception:  # noqa: BLE001 - formatting must not fail
        return f"💲 **Last Price:** **n/a** ({tf_label} | {ts_display})"

    return f"💲 **Last Price:** **{price_val:.2f}** ({tf_label} | {ts_display})"


def _coerce_ai_string(value: Optional[object], *, max_len: int = 240) -> str:
    if value is None:
        return ""
    if level_ctx:
        lines.append(f"- Upside: {fmt_targets(level_ctx.get('upside_targets'))}")
        lines.append(f"- Downside: {fmt_targets(level_ctx.get('downside_targets'))}")

        if level_ctx.get("extension_mode") and last_price is not None:
            broken_levels = level_ctx.get("broken") if isinstance(level_ctx.get("broken"), list) else []
            try:
                price_val = float(last_price)
            except Exception:  # noqa: BLE001
                price_val = None

            if price_val is not None:
                pivot_regime = (payload.get("pivot_regime") or "").upper()
                if pivot_regime.startswith("ABOVE"):
                    broken_support = [item for item in broken_levels if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] < price_val]
                    if broken_support:
                        lines.append(f"- Support (broken): {fmt_targets(broken_support[:3])}")
                else:
                    broken_overhead = [item for item in broken_levels if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] > price_val]
                    if broken_overhead:
                        lines.append(f"- Overhead resistance (broken): {fmt_targets(broken_overhead[:3])}")
    else:
        lines.append("- Upside: n/a")
        lines.append("- Downside: n/a")
        lines.append("- (no level context available)")
        lines.append("")
        lines.append("🧠 How Pros Would Trade It:")
        lines.append("- Insufficient level data; defer to fresh signal or manual review")
        lines.append("")
        lines.append("🚫 Do Nothing If:")
        lines.append("- Levels unknown -> wait for updated data")
        lines.append("")
        lines.append("_Not financial advice._")
        return "\n".join(lines)

    if level_ctx:
        bull_targets = "-"  # keep placeholder if we craft plan below
    bull_targets_lines: list[str] = []
    bear_targets_lines: list[str] = []
    if level_ctx:
        bull_targets_lines.append(f"- Upside: {fmt_targets(level_ctx.get('upside_targets'))}")
        bear_targets_lines.append(f"- Downside: {fmt_targets(level_ctx.get('downside_targets'))}")

    lines.append("")
    lines.append("🧠 How Pros Would Trade It:")
    bull_plan = ["- Bull: enter on reclaim + hold above Pivot"]
    bear_plan = ["- Bear: enter on lose + hold below Pivot"]
    if level_ctx:
        bull_plan.append(f"- Targets: {fmt_targets(level_ctx.get('upside_targets'))}")
        bear_plan.append(f"- Targets: {fmt_targets(level_ctx.get('downside_targets'))}")
    else:
        bull_plan.append("- Targets: need updated pivots")
        bear_plan.append("- Targets: need updated pivots")
    bull_plan.append(f"- Invalidation: lose **{fmt_money(pivot)}**")
    bear_plan.append(f"- Invalidation: reclaim **{fmt_money(pivot)}**")
    lines.extend(bull_plan)
    lines.extend(bear_plan)
    symbol_norm = symbol.upper().strip()
    bias_norm = (bias or "NEUTRAL").upper().strip()
    conviction_norm = (conviction or "LOW").upper().strip()

    price_f = _f(last_price)
    P = _f(piv.get("P"))
    R1 = _f(piv.get("R1"))
    R2 = _f(piv.get("R2"))
    S1 = _f(piv.get("S1"))
    S2 = _f(piv.get("S2"))

    confirm = vix_sqqq_confirmation(vix_dir, sqqq_dir)

    regime = detect_regime(price_f, P, R1, R2, S1, S2)
    d_pivot = _delta(price_f, P) if P is not None else None
    if d_pivot is None:
        vs_pivot = "n/a"
    else:
        if d_pivot > 0:
            rel = "above"
        elif d_pivot < 0:
            rel = "below"
        else:
            rel = "at"
        vs_pivot = f"{d_pivot:+.2f} pts ({rel} P {_fmt(P)})"

    ladd = ladder_levels(price_f if price_f is not None else last_price, piv)
    up_targets = " → ".join([f"{k} {_fmt(v)}" for k, v in ladd["up"][:2]]) or "n/a"
    down_targets = " → ".join([f"{k} {_fmt(v)}" for k, v in ladd["down"][:2]]) or "n/a"

    if regime in ("BELOW_S1", "BELOW_P", "EXTENSION_DOWN"):
        primary = f"**Bear plan:** stay below **P {_fmt(P)}** → targets: {down_targets}"
        if S1 is not None and price_f is not None and price_f < S1:
            alternate = f"**Bull flip:** reclaim + hold **S1 {_fmt(S1)}** then **P {_fmt(P)}** → targets: {up_targets}"
        else:
            alternate = f"**Bull flip:** reclaim + hold **P {_fmt(P)}** → targets: {up_targets}"
    elif regime in ("ABOVE_P", "ABOVE_R1", "EXTENSION_UP"):
        primary = f"**Bull plan:** stay above **P {_fmt(P)}** → targets: {up_targets}"
        alternate = f"**Bear flip:** lose + hold below **P {_fmt(P)}** → targets: {down_targets}"
    else:
        primary = f"**Range plan:** trade edges only (S/R reactions). Targets: up {up_targets} | down {down_targets}"
        alternate = f"**Trend plan:** wait for break + retest of **P {_fmt(P)}** then follow direction."

    msg: List[str] = []
    msg.append(f"🚨 **{symbol_norm} — Trade Context**")
    msg.append(fmt_last_price(symbol_norm, last_price, last_price_ts_utc, last_price_tf))
    if P is not None:
        msg.append(f"📏 **vs Pivot:** {vs_pivot}")
    msg.append("")
    msg.append(
        f"**Bias:** **{bias_norm}** | **Confirmation:** **{confirm}** | **Regime:** **{regime}** | **Conviction:** **{conviction_norm}**"
    )
    msg.append("")
    msg.append("**Key Levels (RTH):**")
    msg.append(f"• P {_fmt(P)} | R1 {_fmt(R1)} | R2 {_fmt(R2)}")
    msg.append(f"• S1 {_fmt(S1)} | S2 {_fmt(S2)}")
    msg.append("")
    msg.append("**How Pros Would Trade It**")
    msg.append(f"• {primary}")
    msg.append(f"• {alternate}")
    msg.append("")
    msg.append("🚫 **Do Nothing If**")
    msg.append("• No break + retest confirmation (first spike only)")
    msg.append("• Choppy price around pivot (no direction)")
    msg.append("• Confirmation flips HARD against the plan")
    msg.append("")
    msg.append("_Not financial advice._")

    return "\n".join(msg)

import discord
import httpx
import requests
from discord.ext import commands
from dotenv import load_dotenv
from openai import OpenAI
from zoneinfo import ZoneInfo

from delivery.on_demand_data import (
    fetch_live_price,
    is_fresh as data_is_fresh,
    symbol_supported_polygon,
)
from scripts import polygon_ingest

load_dotenv()

def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        return int(default)


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


QUALITY_GATE_ENABLED = os.getenv("QUALITY_GATE_ENABLED", "1") == "1"
QUALITY_GATE_STRICT = os.getenv("QUALITY_GATE_STRICT", "1") == "1"
QUALITY_GATE_FALLBACK_ENABLED = os.getenv("QUALITY_GATE_FALLBACK_ENABLED", "1") == "1"
QUALITY_GATE_LOG_FAILS = os.getenv("QUALITY_GATE_LOG_FAILS", "1") == "1"
QUALITY_GATE_DEBUG = os.getenv("DEBUG_GATE", "0") == "1"

REQUIRED_SECTIONS = [
    "— Trade Context",
    "Key Levels",
    "How Pros Would Trade It",
    "Market Confirmation",
    "Do Nothing If",
]

INSIGHTS_REQUIRED = [
    "— Trade Context",
    "Last Price",
    "Key Levels",
    "How Pros Would Trade It",
    "Do Nothing If",
    "_Not financial advice._",
]

BANNED_SUBSTRINGS = [
    "[[",
    "]]",
    "{{",
    "}}",
    "todo",
    "tbd",
    "???",
    "fill me",
    "insert",
    "lorem ipsum",
]

WATCHLIST_EXTRA_COLUMNS = {
    "last_ready_ts": "TEXT",
    "last_attempt_ts": "TEXT",
    "last_error": "TEXT",
}

BOOTSTRAP_BACKFILL_DAYS = int(os.getenv("BOOTSTRAP_BACKFILL_DAYS", "3") or "3")

_STARTUP_DEFAULT = "SPY,QQQ,IWM"
STARTUP_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("STARTUP_SYMBOLS", _STARTUP_DEFAULT).split(",")
    if sym.strip()
]
if not STARTUP_SYMBOLS:
    STARTUP_SYMBOLS = [sym.strip().upper() for sym in _STARTUP_DEFAULT.split(",") if sym.strip()]
_STARTUP_SYMBOL_SET = set(STARTUP_SYMBOLS)


def _filter_startup_symbols(symbols: Iterable[str]) -> list[str]:
    if not _STARTUP_SYMBOL_SET:
        return [str(sym).upper() for sym in symbols if sym]
    filtered: list[str] = []
    for sym in symbols:
        if not sym:
            continue
        upper = str(sym).upper()
        if upper in _STARTUP_SYMBOL_SET:
            filtered.append(upper)
    return filtered


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def _extract_first_float(pattern: str, text: str) -> Optional[float]:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def validate_analysis_message(text: str) -> Tuple[bool, str]:
    """
    Returns (ok, reason).
    Blocks:
      - Missing last price
      - Missing pivot
      - Targets that are invalid (e.g., downside above current price)
      - Contradictory confirmation vs bias (hard contradictions only)
    """

    if not text or len(text.strip()) < 60:
        return False, "too short"

    required_substrings = [
        "Last Price",
        "Key Levels",
        "How Pros Would Trade It",
        "Do Nothing If",
    ]
    lowered = text.lower()

    banned_words = ["guaranteed", "sure thing", "100% win", "insider", "front-run"]
    for token in banned_words:
        if token in lowered:
            return False, f"banned:{token}"
    for token in required_substrings:
        if token.lower() not in lowered:
            return False, f"Missing section: {token}"

    last_px = _extract_first_float(r"(?:Last Price|Last price):\s*\*{0,2}\s*([0-9]+(?:\.[0-9]+)?)", text)
    pivot = _extract_first_float(r"\bP(?:ivot)?:\s*\*{0,2}\s*([0-9]+(?:\.[0-9]+)?)", text) or _extract_first_float(
        r"\bP\s*([0-9]+(?:\.[0-9]+)?)",
        text,
    )

    if last_px is None:
        return False, "Missing last price"
    if pivot is None:
        return False, "Missing pivot"

    s1 = _extract_first_float(r"\bS1\s*([0-9]+(?:\.[0-9]+)?)", text)
    s2 = _extract_first_float(r"\bS2\s*([0-9]+(?:\.[0-9]+)?)", text)
    r1 = _extract_first_float(r"\bR1\s*([0-9]+(?:\.[0-9]+)?)", text)
    r2 = _extract_first_float(r"\bR2\s*([0-9]+(?:\.[0-9]+)?)", text)

    if s1 is not None and last_px < s1:
        if re.search(r"Downside:.*S1", text, flags=re.IGNORECASE):
            return False, "Invalid downside targets: price already below S1"

    if r1 is not None and last_px > r1:
        if re.search(r"Upside:.*R1", text, flags=re.IGNORECASE):
            return False, "Invalid upside targets: price already above R1"

    bias = (
        "BULL"
        if re.search(r"\bBias:\s*\*\*BULL", text, re.IGNORECASE)
        else "BEAR"
        if re.search(r"\bBias:\s*\*\*BEAR", text, re.IGNORECASE)
        else "NEUTRAL"
    )

    confirm = (
        "BULLISH"
        if re.search(r"\bConfirmation:\s*\*\*BULLISH", text, re.IGNORECASE)
        else "BEARISH"
        if re.search(r"\bConfirmation:\s*\*\*BEARISH", text, re.IGNORECASE)
        else "MIXED"
        if re.search(r"\bConfirmation:\s*\*\*MIXED", text, re.IGNORECASE)
        else "UNKNOWN"
    )

    if bias == "BULL" and confirm == "BEARISH":
        return False, "Contradiction: Bias=BULL but Confirmation=BEARISH"
    if bias == "BEAR" and confirm == "BULLISH":
        return False, "Contradiction: Bias=BEAR but Confirmation=BULLISH"

    return True, "OK"


def validate_output(text: str, *, mode: str) -> tuple[bool, str]:
    return validate_analysis_message(text)


def _extract_first_number_after(label: str, text: str) -> Optional[float]:
    """Find the first float-like number after the provided label."""

    match = re.search(rf"{re.escape(label)}\s*[: ]\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:  # noqa: BLE001
        return None


def build_safe_fallback(symbol: str, pivots: Optional[dict], note: str = "") -> str:
    """Return a minimal, human-readable fallback when gating blocks analysis."""

    sym = (symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
    header: list[str] = [
        f"⚠️ **{sym} analysis unavailable**",
        "Automated response withheld. Minimal context below.",
    ]

    if note:
        header.append(f"Reason: {note}")

    def _coerce_float(val: Optional[object]) -> Optional[float]:
        try:
            return float(val) if val is not None else None
        except Exception:  # noqa: BLE001 - fallback must never raise
            return None

    last_px: Optional[float] = None
    last_ts: Optional[str] = None
    try:
        live_px, live_ts, _ = fetch_live_price(sym)
        if live_px is not None:
            last_px = float(live_px)
            last_ts = live_ts
    except Exception:  # noqa: BLE001 - network issues should not break fallback
        pass

    if last_px is None or last_ts is None:
        try:
            price_info = get_latest_price(sym)
        except Exception:  # noqa: BLE001
            price_info = None
        if price_info:
            last_px, last_ts = price_info

    pivots_dict = pivots if isinstance(pivots, dict) else {}
    payload = {
        "bias": "NEUTRAL",
        "bias_confirm": "UNKNOWN",
        "conviction": "LOW",
        "regime": "UNKNOWN",
        "tf_exec": "5m",
        "tf_struct": "60m",
        "tf_ctx": "1D",
        "pivot": _coerce_float(pivots_dict.get("P")),
        "r1": _coerce_float(pivots_dict.get("R1")),
        "r2": _coerce_float(pivots_dict.get("R2")),
        "s1": _coerce_float(pivots_dict.get("S1")),
        "s2": _coerce_float(pivots_dict.get("S2")),
        "vix_trend": "unknown",
        "sqqq_dir": "unknown",
        "last_price": last_px,
        "last_price_ts": last_ts,
        "price_tf": PRICE_TF_LABEL,
    }

    body = format_signal_clean(sym, payload, verbose=False)
    return "\n\n".join(["\n".join(header), body])


def build_deterministic_analysis(
    sym: str,
    last_price: Optional[float],
    last_ts: Optional[str],
    pivots: Optional[dict],
) -> str:
    """Produce a static analysis stub using only deterministic data."""

    symbol = (sym or "UNKNOWN").strip().upper() or "UNKNOWN"
    price_text = "n/a" if last_price is None else f"{last_price:.2f}"
    ts_text = last_ts or "n/a"

    pivot_line = "(no daily pivots yet)"
    if isinstance(pivots, dict) and pivots:
        parts: list[str] = []
        for key in ("P", "R1", "S1", "R2", "S2"):
            val = pivots.get(key)
            if val is None:
                parts.append(f"{key} n/a")
                continue
            try:
                parts.append(f"{key} {float(val):.2f}")
            except Exception:  # noqa: BLE001
                parts.append(f"{key} n/a")
        if parts:
            pivot_line = " | ".join(parts)

    lines = [
        f"📊 **{symbol} Analysis**",
        f"Last price: {price_text} ({ts_text})",
        "",
        "⏱ Timeframes:",
        "• Execution: 5m",
        "• Structure: 60m",
        "• Context: 1D",
        "",
        "Prev RTH pivots:",
        f"• {pivot_line}",
        "",
        "📐 Key Levels:",
        "• Use pivots above for immediate reference.",
        "",
        "🧭 Regime:",
        "• Needs fresh 1m data + signals to classify.",
        "",
        "🧠 How Pros Would Trade It:",
        "• Above Pivot tilt bullish toward R1.",
        "• Below Pivot tilt bearish toward S1.",
        "",
        "🚫 Do Nothing If:",
        "• Structure or data is stale; wait for fresh signal confirmation.",
        "",
        "_Not financial advice._",
    ]
    return "\n".join(lines)


def fmt_money(x):
    try:
        return f"{float(x):.2f}"
    except Exception:  # noqa: BLE001 - formatting must never raise
        return str(x)


def fmt_signed(x):
    try:
        val = float(x)
    except Exception:  # noqa: BLE001
        return str(x)
    sign = "-" if val < 0 else ""
    return f"{sign}{abs(val):.2f}"


def fmt_targets(pairs) -> str:
    if not pairs:
        return "n/a"

    parts: list[str] = []
    for item in pairs:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        name, val = item
        try:
            parts.append(f"{name} {fmt_money(val)}")
        except Exception:  # noqa: BLE001
            continue

    return " → ".join(parts) if parts else "n/a"


def format_signal_clean(sym: str, payload: dict, *, verbose: bool = False) -> str:
    """Render an easy-to-read trade context block for Discord."""

    bias = payload.get("bias", "NEUTRAL")
    bias_confirm = payload.get("bias_confirm", "UNKNOWN")
    conviction = payload.get("conviction", "LOW")
    regime = payload.get("regime", "UNKNOWN")
    tf_exec = payload.get("tf_exec", "5m")
    tf_struct = payload.get("tf_struct", "60m")
    tf_ctx = payload.get("tf_ctx", "1D")

    pivot = payload.get("pivot")
    r1 = payload.get("r1")
    r2 = payload.get("r2")
    s1 = payload.get("s1")
    s2 = payload.get("s2")

    vix_trend = payload.get("vix_trend", "unknown")
    sqqq_dir = payload.get("sqqq_dir", "unknown")

    edge = payload.get("edge")
    model = payload.get("model", payload.get("model_version", "unknown"))
    ts = payload.get("ts")

    bias_upper = str(bias).upper()
    bias_confirm_upper = str(bias_confirm).upper()
    bias_confirm_display = bias_confirm_upper if bias_confirm_upper else "UNKNOWN"

    bias_emoji = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "🟡"}.get(bias_upper, "🟡")
    confirm_emoji = {
        "BULLISH": "🟢",
        "BEARISH": "🔴",
        "UNKNOWN": "🟡",
        "NEUTRAL": "🟡",
    }.get(bias_confirm_upper, "🟡")

    lines: list[str] = []
    lines.append(f"🚨 **{sym} — Trade Context**")

    last_price = payload.get("last_price")
    last_price_ts = payload.get("last_price_ts")
    price_tf = payload.get("price_tf") or PRICE_TF_LABEL

    lines.append(fmt_last_price(sym, last_price, last_price_ts, price_tf))

    if payload.get("educational_only"):
        edge_meta = payload.get("edge")
        if isinstance(edge_meta, (int, float)):
            lines.append(f"⚠️ **LOW EDGE / EDUCATIONAL** (edge {edge_meta:.3f})")
        else:
            lines.append("⚠️ **LOW EDGE / EDUCATIONAL**")
        lines.append("")

    pivot_val = payload.get("pivot")
    distance_line = None
    if last_price is not None and pivot_val is not None:
        try:
            diff = float(last_price) - float(pivot_val)
            direction = "above" if diff >= 0 else "below"
            distance_line = (
                f"📏 **vs Pivot:** {fmt_signed(diff)} pts ({direction} {fmt_money(pivot_val)})"
            )
        except Exception:  # noqa: BLE001
            distance_line = None

    if distance_line:
        lines.append(distance_line)

    lines.append("")

    lines.append(
        f"**Bias:** {bias_emoji} {bias_upper} ({confirm_emoji} confirm: {bias_confirm_display})"
    )
    pivot_regime = (payload.get("pivot_regime") or regime or "UNKNOWN")
    pivot_regime_display = str(pivot_regime).upper()
    extension_mode = payload.get("pivot_extension_mode")
    mode_label = "EXTENSION MODE" if extension_mode else "NORMAL"
    lines.append(
        f"🧭 Regime: {pivot_regime_display} | Mode: {mode_label} | Conviction: {str(conviction).upper()}"
    )
    lines.append("")
    lines.append("⏱ Timeframes:")
    lines.append(f"- Execution: {tf_exec}")
    lines.append(f"- Structure: {tf_struct}")
    lines.append(f"- Context: {tf_ctx}")
    lines.append("")
    lines.append("📐 Key Levels (RTH):")

    level_ctx = payload.get("level_ctx") if isinstance(payload.get("level_ctx"), dict) else None

    if pivot is not None:
        lines.append(f"- Pivot: **{fmt_money(pivot)}**")
    else:
        lines.append("- Pivot: n/a")

    if last_price is not None:
        if pivot is not None:
            try:
                diff = float(last_price) - float(pivot)
                direction = "above" if diff >= 0 else "below"
                lines.append(
                    f"- Now: **{fmt_money(last_price)}** ({fmt_signed(diff)} pts, {direction} **{fmt_money(pivot)}**)")
            except Exception:  # noqa: BLE001
                lines.append(f"- Now: **{fmt_money(last_price)}**")
        else:
            lines.append(f"- Now: **{fmt_money(last_price)}**")

    if level_ctx:
        lines.append(f"- Upside: {fmt_targets(level_ctx.get('upside_targets'))}")
        lines.append(f"- Downside: {fmt_targets(level_ctx.get('downside_targets'))}")

        if level_ctx.get("extension_mode") and last_price is not None:
            broken_levels = level_ctx.get("broken") if isinstance(level_ctx.get("broken"), list) else []
            try:
                price_val = float(last_price)
            except Exception:  # noqa: BLE001
                price_val = None

            if price_val is not None:
                pivot_regime = (payload.get("pivot_regime") or "").upper()
                if pivot_regime.startswith("ABOVE"):
                    broken_support = [item for item in broken_levels if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] < price_val]
                    if broken_support:
                        lines.append(f"- Support (broken): {fmt_targets(broken_support[:3])}")
                else:
                    broken_overhead = [item for item in broken_levels if isinstance(item, (list, tuple)) and len(item) == 2 and item[1] > price_val]
                    if broken_overhead:
                        lines.append(f"- Overhead resistance (broken): {fmt_targets(broken_overhead[:3])}")
    else:
        lines.append("- Upside: n/a")
        lines.append("- Downside: n/a")
        lines.append("- (no level context available)")

    lines.append("")
    lines.append("🧠 How Pros Would Trade It:")

    bull_plan: list[str] = []
    bear_plan: list[str] = []

    bull_plan.append(f"- Bull: enter on reclaim + hold above **{fmt_money(pivot)}**" if pivot is not None else "- Bull: await clean reclaim of Pivot")
    bear_plan.append(f"- Bear: enter on lose + hold below **{fmt_money(pivot)}**" if pivot is not None else "- Bear: wait for decisive break under Pivot")

    if level_ctx:
        bull_plan.append(f"- Targets: {fmt_targets(level_ctx.get('upside_targets'))}")
        bear_plan.append(f"- Targets: {fmt_targets(level_ctx.get('downside_targets'))}")
    else:
        bull_plan.append("- Targets: need updated pivots/levels")
        bear_plan.append("- Targets: need updated pivots/levels")

    if pivot is not None:
        bull_plan.append(f"- Invalidation: lose **{fmt_money(pivot)}**")
        bear_plan.append(f"- Invalidation: reclaim **{fmt_money(pivot)}**")
    else:
        bull_plan.append("- Invalidation: intraday structure fails")
        bear_plan.append("- Invalidation: intraday structure fails")

    lines.extend(bull_plan)
    lines.extend(bear_plan)

    lines.append("")
    lines.append("### 🧭 **Market Confirmation**")
    lines.append(f"- VIX: **{vix_trend}**")
    lines.append(f"- SQQQ: **{sqqq_dir}**")
    lines.append("")
    lines.append("🚫 Do Nothing If:")
    lines.append("- No break + retest confirmation (first spike only)")
    lines.append("- Choppy price around pivot (no direction)")
    lines.append("- VIX gate flips hard against the bias")
    lines.append("")

    if verbose:
        lines.append("### 🔍 **Signal Details**")
        if ts:
            lines.append(f"- Signal ts: `{ts}`")
        if model:
            lines.append(f"- Model: `{model}`")
        if edge is not None:
            lines.append(f"- Edge: `{edge}`")
        gate_mode = payload.get("gate_mode")
        if gate_mode:
            gate_reason = payload.get("gate_reason")
            gate_line = f"- Gate: `{gate_mode}`"
            if gate_reason:
                gate_line += f" ({gate_reason})"
            lines.append(gate_line)
        vix_level = payload.get("vix_level")
        if vix_level is not None:
            try:
                vix_level_val = float(vix_level)
            except Exception:  # noqa: BLE001
                vix_level_val = None
            if vix_level_val is not None:
                lines.append(f"- VIX level: `{vix_level_val:.2f}`")
        lines.append("")

    lines.append("_Not financial advice._")
    return "\n".join(lines)


def build_signal_payload(sym: str) -> Optional[tuple[dict, float, Dict[str, object]]]:
    """Return structured signal context, probability, and gate info."""

    symbol = (sym or "").strip().upper()
    if not symbol:
        return None

    sig = get_latest_signal(symbol)
    if not sig:
        return None

    try:
        prob_up = float(sig.get("prob_up"))
    except (TypeError, ValueError):
        return None

    pivots_info = get_latest_daily_pivots(symbol)
    pivots = pivots_info.get("piv") if pivots_info and isinstance(pivots_info, dict) else None
    if not isinstance(pivots, dict):
        return None

    def _maybe_float(val):
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    pivot_val = _maybe_float(pivots.get("P"))
    r1 = _maybe_float(pivots.get("R1"))
    r2 = _maybe_float(pivots.get("R2"))
    s1 = _maybe_float(pivots.get("S1"))
    s2 = _maybe_float(pivots.get("S2"))

    if pivot_val is None or r1 is None or s1 is None:
        return None

    edge = _edge(prob_up)
    conviction = _conv(edge)
    bias = "BULL" if prob_up >= 0.53 else ("BEAR" if prob_up <= 0.47 else "NEUTRAL")

    regime = "UNKNOWN"
    tf_exec = "5m"
    tf_struct = "60m"
    tf_ctx = "1D"

    meta_raw = sig.get("meta")
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
            if isinstance(meta, dict):
                regime_val = meta.get("regime") or meta.get("market_regime")
                if regime_val:
                    regime = str(regime_val).upper()
                tf_exec = str(meta.get("tf_exec") or meta.get("tf_execution") or tf_exec)
                tf_struct = str(meta.get("tf_struct") or meta.get("tf_structure") or tf_struct)
                tf_ctx = str(meta.get("tf_ctx") or meta.get("tf_context") or tf_ctx)
        except Exception:  # noqa: BLE001
            pass

    bias_confirm, _confirm_note, confirm_detail = vix_sqqq_confirmation(tf=os.getenv("BIAS_TF", "5m"))
    vix_info = get_vix_context("1m")
    gate = vix_gating_action(float(vix_info["level"])) if vix_info else {"mode": "OK", "reason": "no vix"}

    payload = {
        "bias": bias,
        "bias_confirm": bias_confirm,
        "conviction": conviction,
        "regime": regime,
        "tf_exec": tf_exec,
        "tf_struct": tf_struct,
        "tf_ctx": tf_ctx,
        "pivot": pivot_val,
        "r1": r1,
        "r2": r2,
        "s1": s1,
        "s2": s2,
        "vix_trend": confirm_detail.get("vix_trend", "unknown"),
        "sqqq_dir": confirm_detail.get("sqqq_dir", "unknown"),
        "edge": edge,
        "model": sig.get("model_version"),
        "ts": sig.get("ts"),
        "gate_mode": gate.get("mode"),
        "gate_reason": gate.get("reason"),
        "analysis_mode": "db",
        "analysis_source": "model",
    }

    stale_limit = DATA_STALE_MAX_MIN if DATA_STALE_MAX_MIN > 0 else 3.0
    try:
        fresh = data_is_fresh(symbol, tf="1m", max_min=stale_limit)
    except Exception:  # noqa: BLE001 - if freshness check fails, assume fresh to avoid spam
        fresh = True

    last_px: Optional[float] = None
    last_ts: Optional[str] = None
    price_mode = "db"
    price_source = "db"
    price_tf_label = PRICE_TF_LABEL

    try:
        last_price_info = get_latest_price(symbol)
    except Exception:  # noqa: BLE001
        last_price_info = None

    if last_price_info:
        last_px, last_ts = last_price_info

    need_live = (last_px is None) or (not fresh)

    if need_live:
        polygon_ok = False
        try:
            polygon_ok = bool(symbol_supported_polygon(symbol))
        except Exception:  # noqa: BLE001
            polygon_ok = False

        if polygon_ok:
            live_px: Optional[float]
            live_ts: Optional[str]
            live_src: str
            try:
                live_px, live_ts, live_src = fetch_live_price(symbol)
            except Exception as exc:  # noqa: BLE001
                live_px = None
                live_ts = None
                live_src = f"error:{exc}"
            if live_px is not None and live_ts is not None:
                try:
                    last_px = float(live_px)
                except Exception:  # noqa: BLE001
                    last_px = None
                else:
                    last_ts = live_ts
                    price_mode = "live"
                    price_source = live_src or "polygon"
                    price_tf_label = "live"
                    fresh = True
                    print(f"[PRICE] using live snapshot for {symbol} ({price_source})")
        elif not fresh:
            print(f"[PRICE][WARN] {symbol} data stale and live snapshot unavailable; using cached bar")

    level_ctx = None
    if last_px is not None:
        payload["last_price"] = last_px
        payload["last_price_ts"] = last_ts
        payload["price_tf"] = price_tf_label
        payload["price_mode"] = price_mode
        if price_mode == "live":
            payload["price_source"] = price_source
            payload["analysis_mode"] = "on_demand"
        if isinstance(pivots, dict):
            try:
                level_ctx = build_level_context(last_px, pivots)
            except Exception:  # noqa: BLE001
                level_ctx = None

    if level_ctx:
        payload["level_ctx"] = level_ctx
        payload["pivot_regime"] = level_ctx.get("regime")
        payload["pivot_extension_mode"] = bool(level_ctx.get("extension_mode"))
        payload["upside_targets"] = level_ctx.get("upside_targets")
        payload["downside_targets"] = level_ctx.get("downside_targets")

    if vix_info:
        try:
            payload["vix_level"] = float(vix_info.get("level"))
        except Exception:  # noqa: BLE001
            pass

    return payload, prob_up, gate


def build_analysis_payload(sym: str) -> tuple[Optional[dict], Optional[str]]:
    built = build_signal_payload(sym)
    if not built:
        return None, "signal unavailable"

    payload, _prob, _gate = built

    ctx = payload.get("level_ctx") if isinstance(payload, dict) else None
    last_price = payload.get("last_price") if isinstance(payload, dict) else None

    if ctx and last_price is not None:
        ok, err = validate_targets_vs_price(
            float(last_price),
            ctx.get("upside_targets"),
            ctx.get("downside_targets"),
        )
        if not ok:
            return None, f"Quality gate: {err}"

    return payload, None


def build_analysis_packet(
    sym: str,
    payload: dict,
    *,
    analysis_mode: str,
    live_price: Optional[float],
    live_price_ts: Optional[str],
) -> tuple[dict, Optional[dict]]:
    """Compose the AI packet plus pivots used for gating."""

    level_ctx = payload.get("level_ctx") if isinstance(payload.get("level_ctx"), dict) else None
    if level_ctx and isinstance(level_ctx.get("levels"), dict):
        pivots_for_gate: Optional[dict] = dict(level_ctx.get("levels", {}))
    else:
        pivots_for_gate = {
            "P": payload.get("pivot"),
            "R1": payload.get("r1"),
            "R2": payload.get("r2"),
            "S1": payload.get("s1"),
            "S2": payload.get("s2"),
        }

    def _safe_float(val: object) -> Optional[float]:
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    price_source = "polygon_snapshot" if analysis_mode == "on_demand" else "db_1m"
    price_val = live_price if analysis_mode == "on_demand" and live_price is not None else payload.get("last_price")
    price_val = _safe_float(price_val)
    price_ts_val = live_price_ts if analysis_mode == "on_demand" and live_price_ts is not None else payload.get("last_price_ts")

    pivot_levels = {
        "P": _safe_float(payload.get("pivot")),
        "R1": _safe_float(payload.get("r1")),
        "R2": _safe_float(payload.get("r2")),
        "R3": _safe_float(payload.get("r3")),
        "S1": _safe_float(payload.get("s1")),
        "S2": _safe_float(payload.get("s2")),
        "S3": _safe_float(payload.get("s3")),
    }

    if level_ctx and isinstance(level_ctx.get("levels"), dict):
        for key, val in level_ctx["levels"].items():
            if key in pivot_levels and pivot_levels.get(key) is None:
                pivot_levels[key] = _safe_float(val)

    daily_pivots = get_latest_daily_pivots(sym)
    if daily_pivots and isinstance(daily_pivots.get("piv"), dict):
        for key in ("P", "R1", "R2", "R3", "S1", "S2", "S3"):
            if pivot_levels.get(key) is None:
                pivot_levels[key] = _safe_float(daily_pivots["piv"].get(key))

    data_staleness_s: Optional[float] = None
    if price_ts_val:
        try:
            ts_dt = parse_iso(price_ts_val)
            delta = datetime.now(timezone.utc) - ts_dt.astimezone(timezone.utc)
            data_staleness_s = max(delta.total_seconds(), 0.0)
        except Exception:  # noqa: BLE001
            data_staleness_s = None

    packet = {
        "symbol": sym,
        "current_price": price_val,
        "current_price_source": price_source,
        "current_price_ts": price_ts_val,
        "pivot": pivot_levels.get("P"),
        "r_levels": {
            "R1": pivot_levels.get("R1"),
            "R2": pivot_levels.get("R2"),
            "R3": pivot_levels.get("R3"),
        },
        "s_levels": {
            "S1": pivot_levels.get("S1"),
            "S2": pivot_levels.get("S2"),
            "S3": pivot_levels.get("S3"),
        },
        "bias": payload.get("bias"),
        "bias_confirm": payload.get("bias_confirm"),
        "regime": payload.get("pivot_regime") or payload.get("regime"),
        "conviction": payload.get("conviction"),
        "vix_trend": payload.get("vix_trend"),
        "sqqq_dir": payload.get("sqqq_dir"),
        "model": payload.get("model") or payload.get("model_version"),
        "signal_ts": payload.get("ts"),
        "notes": {
            "session": market_session_et(),
            "data_staleness_s": data_staleness_s,
        },
        "analysis_mode": analysis_mode,
    }

    return packet, pivots_for_gate


def build_autopost_payload():
    """
    Minimal safe autopost:
    - Posts daily prep for your watchlist during RTH
    - Returns None if nothing should be posted
    """

    if market_session_et() != "RTH":
        return None

    watchlist = os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",")
    watchlist = [s.strip().upper() for s in watchlist if s.strip()]
    if not watchlist:
        return None

    lines: list[str] = []
    lines.append("📌 **Autopost Check**")
    lines.append("Watchlist: " + ", ".join(watchlist))
    lines.append("")
    lines.append("⏱ Timeframes:")
    lines.append("• Execution: 5m")
    lines.append("• Structure: 60m")
    lines.append("• Context: 1D")
    lines.append("")
    lines.append("🧭 Regime:")
    lines.append("TRANSITION")
    lines.append("")
    lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    lines.append("")
    lines.append("📐 Key Levels:")
    lines.append("• (use !daily or Analyze <symbol> for full levels)")
    lines.append("")
    lines.append("🧠 How Pros Would Trade It:")
    lines.append("• Use commands for full detail; autopost is heartbeat for now")
    lines.append("")
    lines.append("🚫 Do Nothing If:")
    lines.append("• Data is stale or signals conflict")
    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


ET = ZoneInfo("America/New_York")
ET_TZ = ET

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "./db/tnt.db")
TF = os.getenv("TF", "1m")
MODEL_VERSION = os.getenv("MODEL_VERSION", "heuristic-v1")
HORIZON_MIN = int(os.getenv("MODEL_HORIZON_MIN", "5"))

CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or "0")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    try:
        migrate_settings_table(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] settings migration failed: {exc}")
    return conn


VALID_MODES = {"strict", "insights"}


def get_analysis_mode() -> str:
    try:
        conn = db_connect()
        mode = settings_get(conn, "analysis_mode", "insights").strip().lower()
        conn.close()
        return mode if mode in VALID_MODES else "insights"
    except Exception:  # noqa: BLE001
        return "insights"


def set_analysis_mode(mode: str) -> None:
    mode = (mode or "").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}")
    conn = db_connect()
    settings_set(conn, "analysis_mode", mode)
    conn.close()

_autopost_daily_task = None
_autopost_signal_task = None

DATA_STALE_MAX_MIN = float(os.getenv("DATA_STALE_MAX_MIN", "3") or "3")
DATA_STALE_WARN_CHANNEL_ID = int(os.getenv("DATA_STALE_WARN_CHANNEL", "0") or "0")
DATA_STALE_SYMBOL = (os.getenv("DATA_STALE_SYMBOL", "SPY") or "SPY").strip().upper()

AUTOPOST_EDGE_MIN_STRICT = _env_float("AUTOPOST_EDGE_MIN_STRICT", "0.04")
AUTOPOST_EDGE_MIN_INSIGHTS = _env_float("AUTOPOST_EDGE_MIN_INSIGHTS", "0.015")

_last_daily_post_et_date: Optional[date] = None
_last_signal_post_by_symbol: Dict[str, int] = {}

SignalState = Dict[str, object]
_last_signal_state: Dict[str, SignalState] = {}

_last_summary_date: Optional[date] = None
_last_morning_opt_date: Optional[date] = None
_last_vix_alert_level: Optional[float] = None
_last_vix_alert_ts: Optional[str] = None
_last_monday_date: Optional[date] = None

_data_stale_paused = False
_data_stale_lock: Optional[asyncio.Lock] = None

_bootstrap_tasks: dict[str, asyncio.Task] = {}

ANALYZE_BOOTSTRAP_TIMEOUT_S = float(os.getenv("ANALYZE_BOOTSTRAP_TIMEOUT_S", "8") or "8")

_TF_LABELS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
}
PRICE_TF_LABEL = _TF_LABELS.get(TF.lower() if isinstance(TF, str) else TF, str(TF))

_DEFAULT_FUTURES_CONTEXT_PATH = Path(__file__).resolve().parent.parent / "iqfeed_service" / "output" / "futures_context.json"
FUTURES_CONTEXT_PATH = os.getenv("FUTURES_CONTEXT_PATH") or str(_DEFAULT_FUTURES_CONTEXT_PATH)
FUTURES_CONTEXT_LABEL = os.getenv("FUTURES_CONTEXT_LABEL", "@ES#")
FUTURES_STALE_MINUTES_ACTIVE = int(os.getenv("FUTURES_STALE_MINUTES_ACTIVE", "20"))
FUTURES_STALE_MINUTES_OFFHOURS = int(os.getenv("FUTURES_STALE_MINUTES_OFFHOURS", str(12 * 60)))

OPT_MAX_LINES = int(os.getenv("OPT_MAX_LINES", "45"))
OPT_PLUS_MAX_LINES = int(os.getenv("OPT_PLUS_MAX_LINES", "120"))
OPT_NO_TRADE_MIN_EDGE = float(os.getenv("OPT_NO_TRADE_MIN_EDGE", "0.02"))
OPT_HIGH_CONV_EDGE = float(os.getenv("OPT_HIGH_CONV_EDGE", "0.05"))

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE_URL = (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")
MASSIVE_BASE_URL = (os.getenv("MASSIVE_BASE_URL") or "").rstrip("/")
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
USE_MASSIVE = os.getenv("ZERO_DTE_USE_MASSIVE", "0") == "1"
POLYGON_DISABLED = os.getenv("ZERO_DTE_DISABLE_POLYGON", "0") == "1"

STALE_MIN_RTH = int(os.getenv("STALE_MIN_RTH", "5"))
STALE_MIN_OFFHOURS = int(os.getenv("STALE_MIN_OFFHOURS", "60"))

VIX_ALERTS_ENABLED = os.getenv("VIX_ALERTS_ENABLED", "0") == "1"
VIX_ALERT_CHANNEL_ID = int(os.getenv("VIX_ALERT_CHANNEL_ID", "0") or "0")
VIX_ALERT_CHECK_SEC = int(os.getenv("VIX_ALERT_CHECK_SEC", "60"))
VIX_ALERT_ABS_MOVE = float(os.getenv("VIX_ALERT_ABS_MOVE", "1.00"))
VIX_ALERT_PCT_MOVE = float(os.getenv("VIX_ALERT_PCT_MOVE", "5.0"))
VIX_GATING_ENABLED = os.getenv("VIX_GATING_ENABLED", "0") == "1"
VIX_MAX_REGIME = (os.getenv("VIX_MAX_REGIME", "HIGH / STRESS") or "HIGH / STRESS").upper()
VIX_HARD_BLOCK_LEVEL = float(os.getenv("VIX_HARD_BLOCK_LEVEL", "0"))
VIX_SOFT_BLOCK_LEVEL = float(os.getenv("VIX_SOFT_BLOCK_LEVEL", "0"))

BIAS_CONFIRM_ENABLED = os.getenv("BIAS_CONFIRM_ENABLED", "1") == "1"
BIAS_TF = os.getenv("BIAS_TF", "5m")

_REGIME_ORDER = {
    "LOW": 0,
    "LOW VOL": 0,
    "NORMAL": 1,
    "NORMAL VOL": 1,
    "ELEVATED": 2,
    "ELEVATED VOL": 2,
    "HIGH": 3,
    "HIGH / STRESS": 3,
    "STRESS": 3,
}

MONDAY_PLAYBOOK_ENABLED = os.getenv("MONDAY_PLAYBOOK_ENABLED", "0") == "1"
MONDAY_PLAYBOOK_TIME_ET = os.getenv("MONDAY_PLAYBOOK_TIME_ET", "09:25")
MONDAY_PLAYBOOK_CHANNEL_ID = int(os.getenv("MONDAY_PLAYBOOK_CHANNEL_ID", "0") or "0")
MONDAY_PLAYBOOK_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("MONDAY_PLAYBOOK_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]

MORNING_BRIEF_ENABLED = os.getenv("MORNING_BRIEF_ENABLED", "0") == "1"
MORNING_BRIEF_CHANNEL_ID = int(os.getenv("MORNING_BRIEF_CHANNEL_ID", "0") or "0")
MORNING_BRIEF_TIME_ET = os.getenv("MORNING_BRIEF_TIME_ET", "08:00")
MORNING_BRIEF_DAYS = [
    d.strip().upper()
    for d in os.getenv("MORNING_BRIEF_DAYS", "MON,TUE,WED,THU,FRI").split(",")
]

MORNING_OPT_ENABLED = os.getenv("MORNING_OPT_ENABLED", "0") == "1"
MORNING_OPT_TIME_ET = os.getenv("MORNING_OPT_TIME_ET", "09:28")
MORNING_OPT_CHANNEL_ID = int(os.getenv("MORNING_OPT_CHANNEL_ID", "0") or "0")
MORNING_OPT_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("MORNING_OPT_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]

DAILY_SUMMARY_ENABLED = os.getenv("DAILY_SUMMARY_ENABLED", "0") == "1"
DAILY_SUMMARY_TIME_ET = os.getenv("DAILY_SUMMARY_TIME_ET", "09:31")
DAILY_SUMMARY_SYMBOLS = [
    sym.strip().upper()
    for sym in os.getenv("DAILY_SUMMARY_SYMBOLS", "SPY,QQQ,IWM").split(",")
    if sym.strip()
]
DAILY_SUMMARY_CHANNEL_ID = int(os.getenv("DAILY_SUMMARY_CHANNEL_ID", "0") or "0")

AI_ENABLED = os.getenv("DISCORD_AI_ENABLED", "0") == "1"
AI_MODE = (os.getenv("DISCORD_AI_MODE", "mention") or "mention").lower().strip()
AI_ROLE_ID = int(os.getenv("DISCORD_AI_ROLE_ID", "0") or "0")
AI_CHANNEL_ID = int(os.getenv("DISCORD_AI_CHANNEL_ID", "0") or "0")
AI_MODEL = os.getenv("DISCORD_AI_MODEL", "gpt-5-mini")
AI_MAX_CHARS = int(os.getenv("DISCORD_AI_MAX_CHARS", "1200") or "1200")
VERBOSE_DEFAULT = os.getenv("DISCORD_VERBOSE_DEFAULT", "0") == "1"

_openai: Optional[OpenAI] = OpenAI(api_key=os.getenv("OPENAI_API_KEY")) if AI_ENABLED else None


def _ai_enabled() -> bool:
    return os.getenv("DISCORD_AI_ENABLED", "0") in ("1", "true", "True")


def _ai_model() -> str:
    return os.getenv("DISCORD_AI_MODEL", "gpt-5-mini")


def _ai_max_chars() -> int:
    try:
        return int(os.getenv("DISCORD_AI_MAX_CHARS", "1800"))
    except Exception:  # noqa: BLE001
        return 1800


def _ai_timeout() -> float:
    try:
        return float(os.getenv("DISCORD_AI_TIMEOUT_S", "12"))
    except Exception:  # noqa: BLE001
        return 12.0


async def ai_render_trade_context(openai_client, packet: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, err) using packet + AI narrative (AI supplies only plan + notes)."""

    if not _ai_enabled() or openai_client is None:
        return None, "AI disabled"

    def _render(packet: dict, payload: dict) -> str:
        symbol = str(packet.get("symbol") or "?").upper()
        price_tf = PRICE_TF_LABEL
        price_val = packet.get("current_price")
        price_ts = packet.get("current_price_ts")
        pivot_val = packet.get("pivot")

        def _safe_float(val: object) -> Optional[float]:
            try:
                return float(val) if val is not None else None
            except Exception:  # noqa: BLE001
                return None

        price_float = _safe_float(price_val)
        pivot_float = _safe_float(pivot_val)

        bias_label = str(packet.get("bias") or "NEUTRAL").upper()
        confirm_label = str(packet.get("bias_confirm") or "UNKNOWN").upper()
        conviction_label = str(packet.get("conviction") or "LOW").upper()
        regime_label = str(packet.get("regime") or "UNKNOWN").upper()

        bias_note = _coerce_ai_string(payload.get("bias"))
        regime_note = _coerce_ai_string(payload.get("regime"))

        bull_plan = _coerce_ai_lines(payload.get("plan_bull"))
        bear_plan = _coerce_ai_lines(payload.get("plan_bear"))
        do_nothing = _coerce_ai_lines(payload.get("do_nothing"))

        lines: list[str] = []
        lines.append(f"🚨 **{symbol} — Trade Context**")
        lines.append(fmt_last_price(symbol, price_float, price_ts, price_tf))

        if packet.get("educational_only"):
            edge_meta = packet.get("edge")
            if isinstance(edge_meta, (int, float)):
                lines.append(f"⚠️ **LOW EDGE / EDUCATIONAL** (edge {edge_meta:.3f})")
            else:
                lines.append("⚠️ **LOW EDGE / EDUCATIONAL**")
            lines.append("")

        if price_float is not None and pivot_float is not None:
            diff = price_float - pivot_float
            direction = "above" if diff >= 0 else "below"
            lines.append(
                f"📏 **vs Pivot:** {fmt_signed(diff)} pts ({direction} {fmt_money(pivot_float)})"
            )
        else:
            lines.append("📏 **vs Pivot:** n/a")

        lines.append("")
        lines.append(
            f"**Bias:** **{bias_label}** | **Confirmation:** **{confirm_label}** | **Conviction:** **{conviction_label}**"
        )
        if bias_note:
            lines.append(f"• {bias_note}")
        lines.append(f"🧭 Regime: **{regime_label}**")
        if regime_note:
            lines.append(f"• {regime_note}")

        lines.append("")
        lines.append("📐 Key Levels (RTH):")
        if pivot_float is not None:
            lines.append(f"• Pivot: **{fmt_money(pivot_float)}**")
        else:
            lines.append("• Pivot: n/a")

        r_levels = packet.get("r_levels") if isinstance(packet.get("r_levels"), dict) else {}
        s_levels = packet.get("s_levels") if isinstance(packet.get("s_levels"), dict) else {}

        def _fmt_levels(label: str, lvl_dict: dict) -> Optional[str]:
            parts: list[str] = []
            for key in ("R1", "R2", "R3") if label == "Upside" else ("S1", "S2", "S3"):
                val = lvl_dict.get(key)
                if val is None:
                    continue
                try:
                    parts.append(f"{key} {fmt_money(val)}")
                except Exception:  # noqa: BLE001
                    continue
            if not parts:
                return None
            return f"• {label}: " + " | ".join(parts)

        r_line = _fmt_levels("Upside", r_levels)
        s_line = _fmt_levels("Downside", s_levels)
        if r_line:
            lines.append(r_line)
        else:
            lines.append("• Upside: n/a")
        if s_line:
            lines.append(s_line)
        else:
            lines.append("• Downside: n/a")

        lines.append("")
        lines.append("🧠 How Pros Would Trade It:")

        if bull_plan:
            for idx, item in enumerate(bull_plan):
                label = "Bull" if idx == 0 else "Bull cont."
                lines.append(f"• {label}: {item}")
        else:
            lines.append("• Bull: await clean reclaim of Pivot before risk on")

        if bear_plan:
            for idx, item in enumerate(bear_plan):
                label = "Bear" if idx == 0 else "Bear cont."
                lines.append(f"• {label}: {item}")
        else:
            lines.append("• Bear: wait for breakdown below Pivot with confirmation")

        lines.append("")
        lines.append("🧭 Market Confirmation:")
        lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))

        lines.append("")
        lines.append("🚫 Do Nothing If:")
        if do_nothing:
            for item in do_nothing:
                lines.append(f"• {item}")
        else:
            lines.append("• Confirmation flips hard against the bias or data turns stale")

        staleness = packet.get("notes", {}).get("data_staleness_s") if isinstance(packet.get("notes"), dict) else None
        if isinstance(staleness, (int, float)) and staleness > 90:
            lines.append("")
            lines.append(f"⚠️ Data staleness: last price {staleness/60:.1f} min old")

        lines.append("")
        lines.append("_Not financial advice._")
        return "\n".join(lines)

    sys = (
        "You are TNT Trading Bot. Draft ONLY the narrative fields for a trade context.\n"
        "Follow the schema exactly and never output prose outside JSON.\n"
        "Schema (all fields required):\n"
        '{"bias": string, "regime": string, "plan_bull": [string], "plan_bear": [string], "do_nothing": [string]}\n'
        "Rules:\n"
        "- Use ONLY facts from the provided packet. Do NOT invent prices, levels, times, or news.\n"
        "- Keep each string under 160 characters.\n"
        "- Focus on rationale and execution notes; reference named levels (Pivot, R1, S1) instead of creating new numbers.\n"
        "- Bullet strings should be actionable (e.g., trigger, target, invalidation).\n"
        "- Maintain a neutral, professional tone.\n"
        "Respond with valid JSON only."
    )

    user = "Packet:\n" + json.dumps(packet, ensure_ascii=False)

    try:
        resp = await asyncio.wait_for(
            openai_client.responses.create(  # type: ignore[call-arg]
                model=_ai_model(),
                input=user,
                instructions=sys,
                max_output_tokens=400,
                temperature=0.2,
            ),
            timeout=_ai_timeout(),
        )
        raw = getattr(resp, "output_text", "") or ""
    except Exception as exc:  # noqa: BLE001
        return None, f"AI error: {exc}"

    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.strip("`")
        raw = raw.strip()

    if "{" in raw and "}" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        raw = raw[start:end]

    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return None, f"AI parse error: {exc}"

    if not isinstance(payload, dict):
        return None, "AI response not dict"

    for required in ("bias", "regime", "plan_bull", "plan_bear", "do_nothing"):
        if required not in payload:
            return None, f"AI missing field: {required}"

    try:
        text = _render(packet, payload)
    except Exception as exc:  # noqa: BLE001
        return None, f"render error: {exc}"

    max_chars = _ai_max_chars()
    text = text.strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."

    return text, None

EARNINGS_PROVIDER = os.getenv("EARNINGS_PROVIDER", "earningsapi").lower()
EARNINGS_API_KEY = os.getenv("EARNINGS_API_KEY", "")
EARNINGS_WATCHLIST = [
    s.strip().upper()
    for s in os.getenv(
        "EARNINGS_WATCHLIST",
        "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA",
    ).split(",")
    if s.strip()
]

MACRO_EVENTS_FILE = os.getenv("MACRO_EVENTS_FILE", "data/macro_events.csv")

BRIEF_STATE_PATH = os.getenv("BRIEF_STATE_PATH", "db/brief_state.json")
BOT_ALERT_CHANNEL_ID = int(os.getenv("BOT_ALERT_CHANNEL_ID", "0") or "0")

ANALYZE_WORDS = ("analyze", "analysis", "setup", "thoughts", "outlook", "view")
TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
ANALYZE_SYNONYMS = {
    "TESLA": "TSLA",
    "GOOGLE": "GOOGL",
    "GOOG": "GOOGL",
    "ALPHABET": "GOOGL",
    "FACEBOOK": "META",
    "META": "META",
    "APPLE": "AAPL",
    "AMAZON": "AMZN",
    "MICROSOFT": "MSFT",
    "NVDA": "NVDA",
    "NVIDIA": "NVDA",
}


def timeframe_block_lines(*, execution: str, structure: str, context: str) -> list[str]:
    return [
        "⏱ Timeframes:",
        f"• Execution: {execution}",
        f"• Structure: {structure}",
        f"• Context: {context}",
    ]


def extract_analysis_request(text: str):
    t = (text or "").strip()
    if not t:
        return None
    tl = t.lower()
    if not any(w in tl for w in ANALYZE_WORDS):
        return None
    ticks = TICKER_RE.findall(t.upper())
    if not ticks:
        return None
    primary = ticks[0]
    return ANALYZE_SYNONYMS.get(primary, primary)


def build_mega_cap_analysis(symbol: str) -> str | None:
    sym = symbol.upper()

    dp = get_latest_daily_pivots(sym)
    if not dp or "piv" not in dp:
        return None

    piv = dp["piv"]
    P = float(piv["P"])
    R1 = float(piv["R1"])
    S1 = float(piv["S1"])

    sig = get_latest_signal(sym)
    v = get_vix_context("1m")
    hourly_state = _hourly_regime_state(sym)
    structure_stale = hourly_state is None

    vix_gate = None
    if v and "level" in v:
        try:
            vix_gate = vix_gating_action(float(v["level"]))
        except Exception:  # noqa: BLE001
            vix_gate = None
    if not vix_gate:
        vix_gate = {"mode": "OK", "reason": ""}

    has_signal = bool(sig)
    prob_up = float(sig["prob_up"]) if has_signal else None
    edge = abs(prob_up - 0.5) if prob_up is not None else 0.0
    bias_label = (
        "BULLISH"
        if prob_up is not None and prob_up >= 0.53
        else "BEARISH"
        if prob_up is not None and prob_up <= 0.47
        else "NEUTRAL"
    )
    bias_dir = 0
    if prob_up is not None:
        if prob_up >= 0.53:
            bias_dir = 1
        elif prob_up <= 0.47:
            bias_dir = -1

    regime_info = derive_regime(prob_up, hourly_state)
    regime_label = regime_info["label"]
    aligned = has_signal and regime_info.get("regime_dir") and regime_info.get("regime_dir") == bias_dir

    conviction, conviction_notes = determine_conviction(
        edge=edge,
        aligned=bool(aligned),
        structure_stale=structure_stale,
        has_signal=has_signal,
        gate_mode=vix_gate.get("mode", "OK"),
    )

    structure_label = "60m"
    if structure_stale:
        structure_label += " (STALE)"

    timeframe_lines = timeframe_block_lines(
        execution=PRICE_TF_LABEL,
        structure=structure_label,
        context="1D",
    )

    lines: list[str] = []
    lines.append(f"📊 Asset: {sym}")

    badge_emoji, badge_text = classify_regime_badge(regime_label, conviction, vix_gate.get("mode", "OK"))
    lines.append(f"{badge_emoji} {badge_text}")
    lines.extend(timeframe_lines)
    if BIAS_CONFIRM_ENABLED:
        lines.append("")
        lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    lines.append("")

    lines.append(f"🧭 Regime: {regime_label}")
    for reason in regime_info.get("reasons", []):
        lines.append(f"• {reason}")

    lines.append(f"🎯 Conviction: {conviction} (edge {edge:.3f})")
    for note in conviction_notes:
        lines.append(f"• {note}")

    if v and "level" in v:
        lvl = float(v["level"])
        gate_mode = vix_gate.get("mode", "OK").upper()
        gate_reason = vix_gate.get("reason", "")
        vix_line = f"VIX {lvl:.2f}"
        if gate_mode != "OK":
            vix_line += f" | gate {gate_mode}"
            if gate_reason:
                vix_line += f" ({gate_reason})"
        lines.append(vix_line)

    lines.append("")
    if has_signal and prob_up is not None:
        prob_down = 1.0 - prob_up
        lines.append(
            f"📈 Signal: Prob↑ {prob_up:.3f} | Prob↓ {prob_down:.3f} | Horizon {HORIZON_MIN}m | Model {sig['model_version']}"
        )
        lines.append(f"Signal ts: {sig['ts']}")
    else:
        lines.append("📈 Signal: unavailable (levels-only mode)")

    lines.append("")
    lines.append("📐 Key Levels:")
    lines.append(f"P {P:.2f} | R1 {R1:.2f} | S1 {S1:.2f}")

    daily_bias = compute_daily_bias(sym, dp)
    if daily_bias:
        lines.append("")
        lines.append(f"📊 Daily Bias: {daily_bias['label']} ({daily_bias['score']:+.2f})")
        lines.append(f"Confidence: {daily_bias['confidence']} (as of {daily_bias['as_of']} ET)")
        for item in daily_bias.get("context", []):
            lines.append(f"• {item}")

    weekly_structure = compute_weekly_structure(sym)
    if weekly_structure:
        lines.append("")
        lines.append("🧱 Weekly Structure")
        if weekly_structure.get("vwap") is not None:
            lines.append(f"• VWAP {weekly_structure['vwap']:.2f}")
        else:
            lines.append("• VWAP unavailable")
        if weekly_structure.get("prior_high") is not None and weekly_structure.get("prior_low") is not None:
            lines.append(f"• Prior High {weekly_structure['prior_high']:.2f}")
            lines.append(f"• Prior Low {weekly_structure['prior_low']:.2f}")
        else:
            lines.append("• Prior week levels unavailable")

    regime_lines = build_hourly_regime_block(sym, hourly_state)
    if regime_lines:
        lines.append("")
        lines.extend(regime_lines)

    if conviction == "LOW":
        playbook = {
            "selection": "NO-TRADE",
            "orientation": None,
            "reasons": ["Conviction LOW → wait for clarity"],
        }
    else:
        playbook = select_active_playbook(
            prob_up if prob_up is not None else 0.5,
            bias_label,
            conviction,
            hourly_state,
            vix_gate,
        )

    if playbook:
        lines.append("")
        orientation = playbook.get("orientation")
        if orientation:
            lines.append(f"🎯 Active Playbook: {playbook['selection']} ({orientation})")
        else:
            lines.append(f"🎯 Active Playbook: {playbook['selection']}")
        lines.append("Reason:")
        for reason in playbook.get("reasons", []):
            lines.append(f"• {reason}")

    target_level = f"R1 {R1:.2f}" if bias_dir > 0 else f"S1 {S1:.2f}" if bias_dir < 0 else f"Pivot {P:.2f}"
    if conviction == "LOW":
        do_nothing_clause = (
            f"🚫 Do nothing unless conviction improves (currently LOW) and price resolves beyond {target_level} with volume support."
        )
    else:
        do_nothing_clause = (
            f"🚫 Do nothing unless price breaks and retests {target_level} with confirming volume."
        )

    lines.append("")
    lines.append(do_nothing_clause)
    lines.append("")
    lines.append("_Not financial advice._")

    return "\n".join(lines)


def maybe_ai_analysis(symbol: str) -> Optional[str]:
    """Wrapper to build the preferred AI-style analysis, if data supports it."""

    try:
        return build_mega_cap_analysis(symbol)
    except Exception as exc:  # noqa: BLE001
        print(f"[ANALYZE] builder error for {symbol}: {exc}")
        return None


def safe_json_loads(data: object) -> dict:
    if isinstance(data, dict):
        return dict(data)
    if data is None:
        return {}
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001 - fall through to return {}
            return {}
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            try:
                return json.loads(text.replace("'", '"'))
            except Exception:  # noqa: BLE001 - tolerate malformed payloads
                return {}
    return {}


def edge_from_prob(prob_up: float) -> float:
    return float(math.fabs(prob_up - 0.5))


def conviction_from_edge(edge: float) -> str:
    if edge < OPT_NO_TRADE_MIN_EDGE:
        return "LOW"
    if edge < OPT_HIGH_CONV_EDGE:
        return "MED"
    return "HIGH"


def bias_from_prob(prob_up: float) -> str:
    if prob_up >= 0.53:
        return "BULL"
    if prob_up <= 0.47:
        return "BEAR"
    return "NEUTRAL"


def clamp_lines(lines: list[str], max_lines: int) -> str:
    limit = max(1, max_lines)
    if len(lines) <= limit:
        return "\n".join(lines)
    head = lines[: limit - 1]
    return "\n".join(head + ["…(trimmed)"])


def fmt_num(x, nd: int = 2) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except Exception:  # noqa: BLE001 - fall back if conversion fails
        return "n/a"


def load_brief_state() -> Dict[str, object]:
    try:
        if os.path.exists(BRIEF_STATE_PATH):
            with open(BRIEF_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_brief_state(state: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(BRIEF_STATE_PATH), exist_ok=True)
    with open(BRIEF_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


async def notify_ops(msg: str) -> None:
    try:
        if BOT_ALERT_CHANNEL_ID:
            ch = bot.get_channel(BOT_ALERT_CHANNEL_ID)
            if ch:
                await ch.send(f"⚠️ **BOT ALERT**\n{msg}")
    except Exception:  # noqa: BLE001
        pass


async def safe_send(
    channel,
    text: str,
    *,
    kind: str = "analysis",
    symbol: str = "",
    pivots: Optional[dict] = None,
    analysis_mode: str = "db",
    output_mode: str = "strict",
):

    if not QUALITY_GATE_ENABLED:
        return await channel.send(text)

    if kind == "analysis":
        ok, reason = validate_analysis_message(text)
    else:
        ok, reason = True, "ok"

    if ok:
        return await channel.send(text)

    if QUALITY_GATE_LOG_FAILS:
        print(f"[GATE] blocked {kind} {symbol}: {reason}")

    if QUALITY_GATE_DEBUG and kind == "analysis" and reason:
        try:
            await channel.send(f"Gate blocked {symbol or 'analysis'}: `{reason}`")
        except Exception:  # noqa: BLE001 - debugging aid only
            pass

    if QUALITY_GATE_FALLBACK_ENABLED and kind == "analysis":
        detail = reason or "Quality gate blocked an unvalidated response."
        fallback = build_safe_fallback(
            symbol or "UNKNOWN",
            pivots,
            note=detail,
        )
        return await channel.send(fallback)

    return None


MEGA_CAP = {"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"}

INDEX_HEAVY = {
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "JPM",
    "XOM",
    "UNH",
    "AVGO",
    "LLY",
    "COST",
    "AMD",
    "NFLX",
    "ADBE",
    "CRM",
}

HIGH_KEYWORDS = [
    "fomc",
    "rate decision",
    "press conference",
    "minutes",
    "cpi",
    "pce",
    "nonfarm",
    "payroll",
    "jobs report",
    "unemployment",
    "jolts",
    "gdp",
    "retail sales",
]

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
RTH_START = time(9, 31)
PM_OPEN = time(4, 0)
AH_CLOSE = time(20, 0)


def parse_iso(ts: str) -> datetime:
    """Parse an ISO8601 timestamp into a timezone-aware datetime."""
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def now_utc_iso() -> str:
    """Return the current UTC time as ISO8601 string."""
    return datetime.now(timezone.utc).isoformat()


def market_session_et(dt_utc: Optional[datetime] = None) -> str:
    """Classify the current US equity session in Eastern Time."""
    dt_utc = dt_utc or datetime.now(timezone.utc)
    dt_et = dt_utc.astimezone(ET)
    if dt_et.weekday() >= 5:
        return "WEEKEND"
    t = dt_et.time()
    if RTH_START <= t < RTH_CLOSE:
        return "RTH"
    if PM_OPEN <= t < RTH_START:
        return "PRE"
    if RTH_CLOSE <= t < AH_CLOSE:
        return "AH"
    return "CLOSED"


def pivots_from_hlc(high: float, low: float, close: float) -> Dict[str, float]:
    pivot = (high + low + close) / 3.0
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    r3 = high + 2 * (pivot - low)
    s3 = low - 2 * (high - pivot)
    return {"P": pivot, "R1": r1, "S1": s1, "R2": r2, "S2": s2, "R3": r3, "S3": s3}


def conviction_from_edge(edge: float) -> str:
    if edge >= 0.08:
        return "HIGH"
    if edge >= 0.04:
        return "MEDIUM"
    return "LOW"


def _pivot_expand(levels: dict) -> dict:
    """Return a copy with ladder extensions populated when possible."""

    expanded = dict(levels or {})

    P = expanded.get("P")
    R1 = expanded.get("R1")
    R2 = expanded.get("R2")
    S1 = expanded.get("S1")
    S2 = expanded.get("S2")

    if any(v is None for v in (P, R1, R2, S1, S2)):
        return expanded

    rng = (R2 - S2) / 2.0 if R2 is not None and S2 is not None else None
    if rng is None:
        return expanded

    if expanded.get("R3") is None:
        expanded["R3"] = P + 2.0 * rng
    if expanded.get("S3") is None:
        expanded["S3"] = P - 2.0 * rng
    if expanded.get("R4") is None:
        expanded["R4"] = expanded["R3"] + rng
    if expanded.get("S4") is None:
        expanded["S4"] = expanded["S3"] - rng

    return expanded


def classify_pivot_regime(price: float, levels: dict) -> str:
    """Tag regime based on current price relative to pivot ladder."""

    P = levels.get("P")
    R1 = levels.get("R1")
    R2 = levels.get("R2")
    S1 = levels.get("S1")
    S2 = levels.get("S2")

    if any(v is None for v in (P, R1, R2, S1, S2)):
        return "UNKNOWN"

    if price >= R2:
        return "ABOVE_R2"
    if price >= R1:
        return "ABOVE_R1"
    if price > P:
        return "ABOVE_P"
    if price <= S2:
        return "BELOW_S2"
    if price <= S1:
        return "BELOW_S1"
    return "BELOW_P"


def build_level_context(price: float, levels_in: dict) -> dict:
    """Return structured overhead/support context with broken level relabeling."""

    levels = _pivot_expand(levels_in or {})

    P = levels.get("P")
    R1 = levels.get("R1")
    R2 = levels.get("R2")
    R3 = levels.get("R3")
    R4 = levels.get("R4")
    S1 = levels.get("S1")
    S2 = levels.get("S2")
    S3 = levels.get("S3")
    S4 = levels.get("S4")

    regime = classify_pivot_regime(price, levels)

    overhead: list[tuple[str, float]] = []
    support: list[tuple[str, float]] = []
    broken: list[tuple[str, float]] = []

    def _add_if(name: str, val, arr: list[tuple[str, float]]):
        if val is None:
            return
        try:
            arr.append((name, float(val)))
        except Exception:  # noqa: BLE001
            return

    _add_if("R4", R4, overhead)
    _add_if("R3", R3, overhead)
    _add_if("R2", R2, overhead)
    _add_if("R1", R1, overhead)
    _add_if("P", P, overhead)
    _add_if("S1", S1, support)
    _add_if("S2", S2, support)
    _add_if("S3", S3, support)
    _add_if("S4", S4, support)

    overhead_now: list[tuple[str, float]] = []
    support_now: list[tuple[str, float]] = []

    for name, val in overhead:
        if val > price:
            overhead_now.append((name, val))
        else:
            broken.append((name, val))

    for name, val in support:
        if val < price:
            support_now.append((name, val))
        else:
            broken.append((name, val))

    overhead_now.sort(key=lambda x: x[1])
    support_now.sort(key=lambda x: -x[1])

    extension_mode = False
    if R2 is not None and price >= R2:
        extension_mode = True
    if S2 is not None and price <= S2:
        extension_mode = True

    upside_targets = overhead_now[:2]
    downside_targets = support_now[:2]

    overhead_from_broken = sorted(
        [(name, val) for (name, val) in broken if val > price],
        key=lambda x: x[1],
    )[:3]

    return {
        "levels": levels,
        "regime": regime,
        "extension_mode": extension_mode,
        "upside_targets": upside_targets,
        "downside_targets": downside_targets,
        "overhead": overhead_now,
        "support": support_now,
        "overhead_from_broken": overhead_from_broken,
        "broken": broken,
    }


def validate_targets_vs_price(price: float, upside_targets, downside_targets) -> tuple[bool, str]:
    for name, val in (upside_targets or []):
        if val <= price:
            return False, f"Invalid upside target {name}={val:.2f} at/under price {price:.2f}"

    for name, val in (downside_targets or []):
        if val >= price:
            return False, f"Invalid downside target {name}={val:.2f} at/over price {price:.2f}"

    return True, ""


def setup_score(prob_up: float, gate_mode: str, conviction: str) -> int:
    edge = abs(prob_up - 0.5)
    base = int(min(100, max(0, edge * 2000)))
    if gate_mode.upper() == "SOFT":
        base = int(base * 0.7)
    if gate_mode.upper() == "HARD":
        base = 0
    if conviction.upper().startswith("LOW"):
        base = int(base * 0.7)
    return base


def _format_ts(ts: Optional[str]) -> str:
    if not ts:
        return "unknown"
    try:
        return parse_iso(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts


def _format_ts_et_clock(ts: Optional[str]) -> str:
    if not ts:
        return "unknown"
    try:
        dt_et = parse_iso(ts).astimezone(ET)
        return dt_et.strftime("%H:%M ET")
    except Exception:  # noqa: BLE001
        return str(ts)


def _rows(conn: sqlite3.Connection, query: str, params: Iterable[object] = ()) -> list[tuple]:
    cur = conn.cursor()
    cur.execute(query, params)
    return cur.fetchall()


def get_latest_price(symbol: str) -> Optional[Tuple[float, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        try:
            row = _rows(
                conn,
                "SELECT close, ts FROM prices WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 1",
                (symbol.upper(), TF),
            )
        except sqlite3.OperationalError:
            row = _rows(
                conn,
                "SELECT close, ts FROM prices WHERE symbol=? ORDER BY ts DESC LIMIT 1",
                (symbol.upper(),),
            )
    if not row:
        return None
    close, ts = row[0]
    return float(close), str(ts)


def get_latest_bar(symbol: str, tf: str = "1m") -> Optional[Tuple[str, float, float, float, float]]:
    with sqlite3.connect(DB_PATH) as conn:
        row = _rows(
            conn,
            """
            SELECT ts, open, high, low, close
            FROM prices
            WHERE symbol=? AND tf=?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol.upper(), tf),
        )
    return tuple(row[0]) if row else None


def latest_bar_age_min(symbol: str = "SPY", tf: str = "1m") -> float | None:
    """Return the age in minutes of the most recent bar for symbol/tf."""

    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT max(ts) FROM prices WHERE symbol=? AND tf=?",
            (symbol.upper(), tf),
        ).fetchone()

    ts = row[0] if row else None
    if not ts:
        return None

    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 60.0


def get_latest_close(symbol: str, tf: str = "1m") -> Optional[Dict[str, float]]:
    bar = get_latest_bar(symbol, tf=tf)
    if not bar:
        return None
    ts, _open, _high, _low, close = bar
    return {"ts": ts, "close": float(close)}


def get_close_at_or_before(symbol: str, tf: str, ts_iso: str) -> Optional[Dict[str, float]]:
    with sqlite3.connect(DB_PATH) as conn:
        row = _rows(
            conn,
            """
            SELECT ts, close
            FROM prices
            WHERE symbol=? AND tf=? AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol.upper(), tf, ts_iso),
        )
    if not row:
        return None
    ts, close = row[0]
    return {"ts": ts, "close": float(close)}


def get_last_n_bars(symbol: str, tf: str = "1m", n: int = 2000, *, with_volume: bool = False) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        cols = "ts, open, high, low, close"
        if with_volume:
            cols += ", volume"
        rows = _rows(
            conn,
            f"""
            SELECT {cols}
            FROM prices
            WHERE symbol=? AND tf=?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (symbol.upper(), tf, n),
        )
    return list(reversed(rows))


def _latest_session_bars(symbol: str) -> Optional[Dict[str, object]]:
    rows = get_last_n_bars(symbol, tf="1m", n=1200, with_volume=True)
    if not rows:
        return None
    sessions: Dict[date, List[Dict[str, object]]] = {}
    for row in rows:
        if len(row) >= 6:
            ts, o, h, l, c, vol = row[:6]
        else:
            ts, o, h, l, c = row[:5]
            vol = 0.0
        try:
            dt_utc = parse_iso(ts)
        except Exception:  # noqa: BLE001
            continue
        dt_et = dt_utc.astimezone(ET)
        if dt_et.weekday() >= 5:
            continue
        if not (RTH_OPEN <= dt_et.time() <= RTH_CLOSE):
            continue
        key = dt_et.date()
        sessions.setdefault(key, []).append(
            {
                "ts": ts,
                "dt": dt_et,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(vol or 0.0),
            }
        )
    if not sessions:
        return None
    session_date = sorted(sessions.keys())[-1]
    bars = sorted(sessions[session_date], key=lambda item: item["dt"])
    if not bars:
        return None
    return {"date": session_date, "bars": bars}


def _session_history_bars(symbol: str, days: int = 3) -> list[Dict[str, object]]:
    rows = get_last_n_bars(symbol, tf="1m", n=4000, with_volume=True)
    if not rows:
        return []
    sessions: Dict[date, List[Dict[str, object]]] = {}
    for row in rows:
        if len(row) >= 6:
            ts, o, h, l, c, vol = row[:6]
        else:
            ts, o, h, l, c = row[:5]
            vol = 0.0
        try:
            dt_et = parse_iso(ts).astimezone(ET)
        except Exception:  # noqa: BLE001
            continue
        if dt_et.weekday() >= 5:
            continue
        if not (RTH_OPEN <= dt_et.time() <= RTH_CLOSE):
            continue
        key = dt_et.date()
        sessions.setdefault(key, []).append(
            {
                "ts": ts,
                "dt": dt_et,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(vol or 0.0),
            }
        )
    ordered = sorted(sessions.keys())
    selected = ordered[-days:]
    result: list[Dict[str, object]] = []
    for key in selected:
        bars = sorted(sessions[key], key=lambda item: item["dt"])
        if bars:
            result.append({"date": key, "bars": bars})
    return result


def compute_daily_bias(symbol: str, pivot_payload: Optional[Dict[str, object]]) -> Optional[Dict[str, object]]:
    """Blend session context into a weighted daily bias score with guardrail notes."""
    sessions = _session_history_bars(symbol, days=3)
    if not sessions:
        return None
    session = sessions[-1]
    bars: List[Dict[str, object]] = session["bars"]
    if len(bars) < 10:
        return None
    open_price = bars[0]["open"]
    last_price = bars[-1]["close"]
    last_dt: datetime = bars[-1]["dt"]
    high_price = max(b["high"] for b in bars)
    low_price = min(b["low"] for b in bars)
    if open_price <= 0 or high_price <= 0 or low_price <= 0:
        return None

    def _clamp(val: float, low: float, high: float) -> float:
        return max(low, min(high, val))

    context: List[str] = []
    score = 0.0

    piv = pivot_payload.get("piv") if pivot_payload else None
    if piv:
        P = float(piv.get("P", 0.0))
        R1 = float(piv.get("R1", P))
        S1 = float(piv.get("S1", P))
        if last_price >= R1:
            score += 0.45
            context.append("Holding above R1 (strong)")
        elif last_price >= P:
            score += 0.25
            context.append("Above pivot (constructive)")
        elif last_price <= S1:
            score -= 0.45
            context.append("Below S1 (pressure)")
        else:
            score -= 0.25
            context.append("Below pivot (headwind)")

    if len(sessions) >= 2:
        prev_bars = sessions[-2]["bars"]
        prev_close = prev_bars[-1]["close"] if prev_bars else None
    else:
        prev_close = None
    if prev_close and prev_close > 0:
        gap = (open_price - prev_close) / prev_close
        gap_adj = _clamp(gap * 4.0, -0.4, 0.4)
        score += gap_adj
        if gap > 0:
            context.append(f"Overnight gap up {gap:+.2%}")
        elif gap < 0:
            context.append(f"Overnight gap down {gap:+.2%}")
        else:
            context.append("No overnight gap")
    else:
        context.append("Previous close unavailable")

    vix = get_vix_context("1m")
    if vix and "regime" in vix:
        regime = vix["regime"]
        vix_weight = {
            "LOW VOL": 0.20,
            "NORMAL VOL": 0.0,
            "ELEVATED VOL": -0.20,
            "HIGH / STRESS": -0.35,
        }.get(regime, 0.0)
        score += vix_weight
        context.append(f"VIX regime {regime} ({vix_weight:+.2f} impact)")
    else:
        context.append("VIX regime unavailable")

    early_cutoff = time(10, 30)
    early_bars = [b for b in bars if b["dt"].time() <= early_cutoff]
    if len(early_bars) >= 5:
        early_close = early_bars[-1]["close"]
        early_ret = (early_close - open_price) / open_price
        early_adj = _clamp(early_ret * 5.0, -0.35, 0.35)
        score += early_adj
        context.append(f"Early session move {early_ret:+.2%}")
        early_range = max(b["high"] for b in early_bars) - min(b["low"] for b in early_bars)
        full_range = high_price - low_price if high_price > low_price else 0.0
        if full_range > 0:
            range_util = _clamp(early_range / full_range, 0.0, 1.0)
            context.append(f"Early range used {range_util:.0%} of session range")
    else:
        context.append("Early session data insufficient")

    score = _clamp(score, -1.0, 1.0)

    if score >= 0.6:
        label = "Bullish"
    elif score >= 0.25:
        label = "Slightly Bullish"
    elif score <= -0.6:
        label = "Bearish"
    elif score <= -0.25:
        label = "Slightly Bearish"
    else:
        label = "Neutral"

    abs_score = abs(score)
    if abs_score >= 0.65:
        confidence = "High"
    elif abs_score >= 0.35:
        confidence = "Medium"
    else:
        confidence = "Low"

    return {
        "score": float(score),
        "label": label,
        "confidence": confidence,
        "as_of": last_dt.strftime("%H:%M"),
        "context": context,
        "bars": len(bars),
        "open": open_price,
        "last": last_price,
    }


def compute_weekly_structure(symbol: str) -> Optional[Dict[str, object]]:
    """Derive weekly VWAP (current week) and prior-week extremes from 1m RTH bars."""
    rows = get_last_n_bars(symbol, tf="1m", n=6000, with_volume=True)
    if not rows:
        return None

    week_bars: Dict[date, List[Dict[str, float]]] = {}
    for row in rows:
        if len(row) >= 6:
            ts, _o, h, l, c, vol = row[:6]
        else:
            ts, _o, h, l, c = row[:5]
            vol = 0.0
        try:
            dt_et = parse_iso(ts).astimezone(ET)
        except Exception:  # noqa: BLE001
            continue
        if dt_et.weekday() >= 5:
            continue
        if not (RTH_OPEN <= dt_et.time() <= RTH_CLOSE):
            continue
        week_start = dt_et.date() - timedelta(days=dt_et.weekday())
        week_bars.setdefault(week_start, []).append(
            {
                "close": float(c),
                "high": float(h),
                "low": float(l),
                "volume": float(vol or 0.0),
                "typical": (float(h) + float(l) + float(c)) / 3.0,
            }
        )

    if not week_bars:
        return None

    sorted_weeks = sorted(week_bars.keys())
    current_week = sorted_weeks[-1]
    current = week_bars[current_week]

    vwap_num = 0.0
    vwap_den = 0.0
    for bar in current:
        vol = bar["volume"]
        if vol <= 0:
            continue
        vwap_num += bar["typical"] * vol
        vwap_den += vol
    vwap = (vwap_num / vwap_den) if vwap_den > 0 else None

    prior_high = None
    prior_low = None
    prior_week = None
    if len(sorted_weeks) >= 2:
        prior_week = sorted_weeks[-2]
        prior = week_bars[prior_week]
        if prior:
            prior_high = max(bar["high"] for bar in prior)
            prior_low = min(bar["low"] for bar in prior)

    return {
        "week_start": current_week,
        "vwap": vwap,
        "prior_start": prior_week,
        "prior_high": prior_high,
        "prior_low": prior_low,
    }


def derive_regime(prob_up: Optional[float], hourly_state: Optional[Dict[str, object]]) -> Dict[str, object]:
    reasons: List[str] = []
    if hourly_state is None:
        reasons.append("60m structure unavailable")
        return {
            "label": "TRANSITION",
            "reasons": reasons,
            "bias_dir": 0,
            "regime_dir": 0,
            "range_state": None,
            "conflict": False,
            "structure_available": False,
        }

    range_info = hourly_state.get("range_info")
    range_state = range_info[0] if range_info else None

    hourly_regime = str(hourly_state.get("regime", "NEUTRAL")).upper()
    if range_state == "expanding":
        regime_label = "VOL_EXPANSION"
        reasons.append("Range expanding vs 10-bar avg")
    elif range_state == "compressing":
        regime_label = "VOL_COMPRESSION"
        reasons.append("Range compressing vs 10-bar avg")
    else:
        if hourly_regime == "BULLISH":
            regime_label = "TREND_UP"
            reasons.append("60m structure trending higher")
        elif hourly_regime == "BEARISH":
            regime_label = "TREND_DOWN"
            reasons.append("60m structure trending lower")
        else:
            regime_label = "RANGE"
            reasons.append("60m structure flat")

    bias_dir = 0
    if prob_up is not None:
        if prob_up >= 0.53:
            bias_dir = 1
        elif prob_up <= 0.47:
            bias_dir = -1

    regime_dir = 0
    if regime_label == "TREND_UP":
        regime_dir = 1
    elif regime_label == "TREND_DOWN":
        regime_dir = -1

    conflict = False
    if regime_dir and bias_dir and regime_dir != bias_dir:
        conflict = True
        reasons.append("5m bias conflicts with 60m regime")
        regime_label = "TRANSITION"
        regime_dir = 0

    return {
        "label": regime_label,
        "reasons": reasons,
        "bias_dir": bias_dir,
        "regime_dir": regime_dir,
        "range_state": range_state,
        "conflict": conflict,
        "structure_available": True,
    }


def determine_conviction(
    *,
    edge: float,
    aligned: bool,
    structure_stale: bool,
    has_signal: bool,
    gate_mode: str,
) -> tuple[str, List[str]]:
    notes: List[str] = []
    gate_mode = (gate_mode or "OK").upper()

    if gate_mode == "HARD":
        notes.append("VIX gate HARD → stand down")
        return "LOW", notes

    if not has_signal:
        notes.append("Model signal unavailable → levels-only mode")
        return "LOW", notes

    if structure_stale:
        notes.append("60m structure stale → conviction capped")
        return "LOW", notes

    if aligned and edge >= 0.08:
        return "HIGH", notes

    if 0.04 <= edge < 0.08:
        if aligned:
            return "MEDIUM", notes
        notes.append("5m bias not aligned with 60m → conviction downgraded")
        return "LOW", notes

    notes.append("Edge < 0.04 → conviction LOW")
    return "LOW", notes


def classify_regime_badge(regime_label: str, conviction: str, gate_mode: str) -> tuple[str, str]:
    """Map regime and gating context to a quick-glance badge."""
    regime = (regime_label or "").upper()
    conviction = (conviction or "").upper()
    gate_mode = (gate_mode or "OK").upper()

    if gate_mode == "HARD" or conviction == "LOW":
        return "🔴", "NO TRADE"

    if regime in {"TREND_UP", "TREND_DOWN", "VOL_EXPANSION"}:
        return "🟢", "TREND"

    if regime in {"RANGE", "VOL_COMPRESSION"}:
        return "🟡", "RANGE"

    return "🔴", "NO TRADE"


def select_active_playbook(
    prob_up: float,
    bias_label: str,
    conviction: str,
    regime_state: Optional[Dict[str, object]],
    vix_gate: Optional[Dict[str, object]],
) -> Dict[str, object]:
    bias_label = (bias_label or "").upper()
    conviction = (conviction or "").upper()
    gate_mode = (vix_gate or {}).get("mode", "OK").upper()

    bias_dir = 1 if bias_label == "BULLISH" else -1 if bias_label == "BEARISH" else 0
    regime_label = "UNKNOWN"
    regime_dir = 0
    regime_unavailable = regime_state is None
    if regime_state:
        regime_label = str(regime_state.get("regime", "UNKNOWN"))
        if regime_label == "BULLISH":
            regime_dir = 1
        elif regime_label == "BEARISH":
            regime_dir = -1
        elif regime_label == "NEUTRAL":
            regime_dir = 0

    reasons: List[str] = []

    if gate_mode == "HARD":
        reasons.append("VIX gate HARD (volatility stress)")
        return {"selection": "NO-TRADE", "orientation": None, "reasons": reasons}

    if conviction == "LOW":
        reasons.append("Conviction LOW (edge limited)")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — size down / wait")
        return {"selection": "NO-TRADE", "orientation": None, "reasons": reasons}

    if bias_dir == 0:
        reasons.append(f"Bias neutral (prob_up {prob_up:.2f})")
        if regime_unavailable:
            reasons.append("60m regime unavailable")
        else:
            reasons.append(f"60m regime {regime_label.lower()}")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Neutral", "reasons": reasons}

    if regime_unavailable:
        reasons.append("60m regime unavailable — default to range setups")
        reasons.append(f"Bias {'bullish' if bias_dir > 0 else 'bearish'}")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Neutral", "reasons": reasons}

    if regime_dir == 0:
        reasons.append("60m regime neutral")
        reasons.append(f"Bias {'bullish' if bias_dir > 0 else 'bearish'}")
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Neutral", "reasons": reasons}

    if bias_dir != regime_dir:
        reasons.append("Bias and 60m regime conflict")
        reasons.append(
            f"Bias {'bullish' if bias_dir > 0 else 'bearish'} vs 60m {regime_label.lower()}"
        )
        if gate_mode == "SOFT":
            reasons.append("VIX gate SOFT — prefer spreads / smaller size")
        return {"selection": "RANGE", "orientation": "Mixed", "reasons": reasons}

    orientation = "Bullish" if bias_dir > 0 else "Bearish"
    reasons.append(
        f"Bias {'>' if bias_dir > 0 else '<'} 0 (prob_up {prob_up:.2f})"
    )
    reasons.append(f"60m regime {regime_label.lower()}")
    reasons.append(f"Conviction {conviction}")
    if gate_mode == "SOFT":
        reasons.append("VIX gate SOFT — favor defined-risk spreads")

    return {"selection": "TREND", "orientation": orientation, "reasons": reasons}


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    alpha = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period
    for val in values[period:]:
        ema_val = alpha * val + (1 - alpha) * ema_val
    return ema_val


def _resample_to_60m(bars: List[tuple]) -> List[Dict[str, float]]:
    buckets: List[Dict[str, float]] = []
    for row in bars:
        if len(row) >= 6:
            ts, o, h, l, c, vol = row[:6]
            vol = float(vol or 0.0)
        else:
            ts, o, h, l, c = row[:5]
            vol = 0.0
        dt = parse_iso(ts)
        bucket_key = dt.replace(minute=0, second=0, microsecond=0)
        if not buckets or buckets[-1]["hour"] != bucket_key:
            buckets.append(
                {
                    "hour": bucket_key,
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": vol,
                }
            )
        else:
            bucket = buckets[-1]
            bucket["high"] = max(bucket["high"], float(h))
            bucket["low"] = min(bucket["low"], float(l))
            bucket["close"] = float(c)
            bucket["volume"] += vol
    return buckets


def _higher_lows(bars: List[Dict[str, float]], span: int = 3) -> Optional[bool]:
    if len(bars) < span:
        return None
    lows = [bars[i]["low"] for i in range(-span, 0)]
    return all(x <= y for x, y in zip(lows, lows[1:]))


def _range_state(bars: List[Dict[str, float]], lookback: int = 10) -> Optional[Tuple[str, float]]:
    if len(bars) <= lookback:
        return None
    ranges = [max(b["high"] - b["low"], 0.0) for b in bars]
    last_range = ranges[-1]
    hist = [r for r in ranges[-(lookback + 1):-1] if r > 0]
    if not hist:
        return None
    avg_range = sum(hist) / len(hist)
    if avg_range <= 0:
        return None
    ratio = last_range / avg_range
    if ratio >= 1.2:
        state = "expanding"
    elif ratio <= 0.8:
        state = "compressing"
    else:
        state = "steady"
    return state, ratio


def _hourly_regime_state(symbol: str) -> Optional[Dict[str, object]]:
    raw = get_last_n_bars(symbol, tf="1m", n=3600, with_volume=True)
    if len(raw) < 120:
        return None
    hourly = _resample_to_60m(raw)
    if len(hourly) < 12:
        return None

    closes = [b["close"] for b in hourly]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    last_close = closes[-1]

    total_vol = sum(b["volume"] for b in hourly if b["volume"] > 0)
    vwap = None
    price_above_vwap = None
    distance_pct = None
    if total_vol > 0:
        pv = sum(b["close"] * b["volume"] for b in hourly)
        vwap = pv / total_vol if total_vol else None
        if vwap:
            price_above_vwap = last_close >= vwap
            if vwap != 0:
                distance_pct = (last_close - vwap) / vwap * 100.0

    higher_lows = _higher_lows(hourly)
    range_info = _range_state(hourly)

    score = 0.0
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            score += 1.0
        elif ema20 < ema50:
            score -= 1.0
    if price_above_vwap is True:
        score += 1.0
    elif price_above_vwap is False:
        score -= 1.0
    if higher_lows is True:
        score += 0.5
    elif higher_lows is False:
        score -= 0.5
    if range_info:
        state, _ratio = range_info
        if state == "expanding":
            score += 0.25
        elif state == "compressing":
            score -= 0.25

    regime = "NEUTRAL"
    emoji = "⚖️"
    if score >= 1.5:
        regime = "BULLISH"
        emoji = "📈"
    elif score <= -1.5:
        regime = "BEARISH"
        emoji = "📉"

    return {
        "regime": regime,
        "emoji": emoji,
        "score": score,
        "vwap": vwap,
        "price_above_vwap": price_above_vwap,
        "distance_vwap_pct": distance_pct,
        "ema20": ema20,
        "ema50": ema50,
        "higher_lows": higher_lows,
        "range_info": range_info,
    }


def build_hourly_regime_block(symbol: str, state: Optional[Dict[str, object]] = None) -> Optional[List[str]]:
    state = state or _hourly_regime_state(symbol)
    if not state:
        return None

    regime_raw = str(state.get("regime", "NEUTRAL")).upper()
    regime_map = {
        "BULLISH": "TREND_UP",
        "BEARISH": "TREND_DOWN",
        "NEUTRAL": "RANGE",
    }
    lines: List[str] = [f"{state['emoji']} 60-min Regime: {regime_map.get(regime_raw, regime_raw)}"]

    vwap = state.get("vwap")
    price_above_vwap = state.get("price_above_vwap")
    distance_pct = state.get("distance_vwap_pct")
    if vwap is not None and price_above_vwap is not None and distance_pct is not None:
        orientation = "above" if price_above_vwap else "below"
        lines.append(f"• Price {orientation} VWAP ({distance_pct:+.2f}%)")
    else:
        lines.append("• Price vs VWAP unavailable")

    ema20 = state.get("ema20")
    ema50 = state.get("ema50")
    if ema20 is not None and ema50 is not None:
        relation = ">" if ema20 > ema50 else "<" if ema20 < ema50 else "="
        lines.append(f"• EMA20 {relation} EMA50 ({ema20:.2f} vs {ema50:.2f})")
    else:
        lines.append("• EMA crossover unavailable")

    higher_lows = state.get("higher_lows")
    if higher_lows is True:
        lines.append("• Higher-lows intact")
    elif higher_lows is False:
        lines.append("• Higher-lows broken")
    else:
        lines.append("• Higher-lows signal unavailable")

    range_info = state.get("range_info")
    if range_info:
        state_label, ratio = range_info
        if state_label == "expanding":
            lines.append(f"• Range expanding vs 10-bar avg ({ratio:.2f}x)")
        elif state_label == "compressing":
            lines.append(f"• Range compressing vs 10-bar avg ({ratio:.2f}x)")
        else:
            lines.append(f"• Range steady vs 10-bar avg ({ratio:.2f}x)")
    else:
        lines.append("• Range signal unavailable")

    lines.append("")
    return lines


def get_latest_daily_bar(symbol: str) -> Optional[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        row = _rows(
            conn,
            """
            SELECT ts, open, high, low, close
            FROM prices
            WHERE symbol=? AND tf='1d'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        )
    return row[0] if row else None


def get_last_rth_session_hlc(symbol: str) -> Optional[Tuple[str, float, float, float, str]]:
    daily = get_latest_daily_bar(symbol)
    if daily:
        ts, _o, high, low, close = daily
        return ts[:10], float(high), float(low), float(close), str(ts)

    rows = get_last_n_bars(symbol, tf="1m")
    sessions: Dict[str, list[tuple[datetime, float, float, float, float]]] = {}
    for ts, _o, high, low, close in rows:
        dt_utc = parse_iso(ts)
        dt_et = dt_utc.astimezone(ET)
        if dt_et.weekday() >= 5:
            continue
        clock = dt_et.time()
        if not (RTH_OPEN <= clock < RTH_CLOSE):
            continue
        key = dt_et.date().isoformat()
        sessions.setdefault(key, []).append((dt_utc, float(high), float(low), float(close), float(close)))

    if not sessions:
        return None

    session_date = sorted(sessions.keys())[-1]
    bars = sorted(sessions[session_date], key=lambda item: item[0])
    highs = [b[1] for b in bars]
    lows = [b[2] for b in bars]
    close = bars[-1][3]
    last_ts = bars[-1][0].isoformat()
    return session_date, max(highs), min(lows), close, last_ts


def get_latest_daily_pivots(symbol: str) -> Optional[Dict[str, object]]:
    daily = get_latest_daily_bar(symbol)
    if daily:
        ts, _o, high, low, close = daily
        piv = pivots_from_hlc(float(high), float(low), float(close))
        return {"ts": ts, "H": float(high), "L": float(low), "C": float(close), "piv": piv}

    sess = get_last_rth_session_hlc(symbol)
    if not sess:
        return None
    session_date, high, low, close, last_ts = sess
    piv = pivots_from_hlc(high, low, close)
    return {"ts": last_ts, "session": session_date, "H": high, "L": low, "C": close, "piv": piv}


def get_latest_signal_full(symbol: str) -> Optional[Tuple[str, str, Optional[int], float, float, Optional[str], Optional[str]]]:
    query = (
        "SELECT symbol, ts, horizon_min, prob_up, prob_down, model_version, meta "
        "FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1"
    )
    with sqlite3.connect(DB_PATH) as conn:
        rows = _rows(conn, query, (symbol.upper(),))
    return tuple(rows[0]) if rows else None


def get_latest_signal(symbol: str):
    symbol = symbol.upper()
    conn = sqlite3.connect(os.getenv("DB_PATH", "db/tnt.db"))
    cur = conn.cursor()

    cur.execute(
        "SELECT ts, horizon_min, prob_up, prob_down, model_version, meta "
        "FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (symbol,)
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    ts, horizon_min, prob_up, prob_down, model_version, meta = row
    return {
        "ts": ts,
        "horizon_min": int(horizon_min),
        "prob_up": float(prob_up),
        "prob_down": float(prob_down),
        "model_version": model_version,
        "meta": meta or ""
    }


def classify_bias(prob_up: float) -> str:
    return bias_from_prob(prob_up)


def classify_conviction(prob_up: float) -> str:
    return conviction_from_edge(edge_from_prob(prob_up))


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def explain_no_trade(
    sym: str,
    prob_up: float | None,
    gate_mode: str,
    confirm_bias: str,
    confirm_note: str,
    mode: Optional[str] = None,
):
    reasons: list[str] = []

    mode_norm = (mode or "strict").strip().lower()
    edge_floor = _edge_threshold_for_mode(mode_norm)

    if prob_up is None:
        reasons.append("No model signal available yet for this symbol.")
        edge = None
    else:
        edge = abs(prob_up - 0.5)
        if edge < edge_floor:
            reasons.append(
                f"Edge below threshold ({edge:.3f} < {edge_floor:.3f}, mode {mode_norm})."
            )
        else:
            reasons.append(f"Edge meets threshold ({edge:.3f}).")

    if gate_mode == "HARD":
        reasons.append("Volatility gate is HARD (stand down).")
    elif gate_mode == "SOFT":
        reasons.append("Volatility gate is SOFT (reduce risk; prefer spreads).")
    else:
        reasons.append("Volatility gate OK.")

    if confirm_bias in ("UNKNOWN", "NEUTRAL"):
        reasons.append(f"Confirmation neutral/unknown: {confirm_note}.")
    else:
        reasons.append(f"Confirmation supports direction: {confirm_note}.")

    next_check = "Next check: after break + retest at Pivot/S1/R1 or in ~5–10 minutes."
    return reasons, next_check


def get_latest_signal_context(symbol: str) -> tuple[list[str], Optional[tuple]]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA table_info(signals)")
            table_cols = {info[1] for info in cur.fetchall()}
        except sqlite3.OperationalError:
            return [], None

        if "ts" not in table_cols or "prob_up" not in table_cols:
            return [], None

        select_cols = ["ts", "prob_up"]
        for name in ("prob_down", "model_version", "model", "edge", "ret_1m", "trend", "vol_z"):
            if name in table_cols:
                select_cols.append(name)

        sql = "SELECT {cols} FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT 1".format(
            cols=", ".join(select_cols)
        )

        try:
            cur.execute(sql, (symbol.upper(),))
            row = cur.fetchone()
        except sqlite3.OperationalError:
            return [], None

    if not row:
        return [], None
    return select_cols, row


def vix_regime(level: float) -> str:
    if level < 15:
        return "LOW VOL"
    if level < 20:
        return "NORMAL VOL"
    if level < 25:
        return "ELEVATED VOL"
    return "HIGH / STRESS"


def get_vix_context(tf: str = "1m") -> Optional[Dict[str, object]]:
    latest = get_latest_close("VIX", tf=tf)
    if not latest:
        return None
    level = float(latest["close"])
    return {"ts": latest["ts"], "level": level, "regime": vix_regime(level)}


def fetch_earnings_for_date(date_et: str) -> list[dict]:
    """Fetch earnings calendar rows for a given ET date."""
    if not EARNINGS_API_KEY:
        return []
    if EARNINGS_PROVIDER != "earningsapi":
        return []
    url = f"https://api.earningsapi.com/v1/calendar/{date_et}"
    try:
        resp = requests.get(url, params={"apikey": EARNINGS_API_KEY}, timeout=15)
        print(f"[EARNINGS] date={date_et} status={resp.status_code} bytes={len(resp.text)}")
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    rows: list[dict] = []
    for bucket, label in [("pre", "BMO"), ("after", "AMC"), ("notSupplied", "TAS")]:
        items = data.get(bucket) or []
        for entry in items:
            sym = (entry.get("symbol") or entry.get("ticker") or "").upper()
            if not sym:
                continue
            if EARNINGS_WATCHLIST and sym not in EARNINGS_WATCHLIST:
                continue
            rows.append({"symbol": sym, "when": label})

    return rows


def get_latest_two_closes(symbol: str, tf: str = "5m") -> Optional[Dict[str, object]]:
    symbol = symbol.upper()
    db_path = os.getenv("DB_PATH", DB_PATH)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ts, close FROM prices WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 2",
            (symbol, tf),
        )
        rows = cur.fetchall()
    if len(rows) < 2:
        return None
    (ts0, c0), (ts1, c1) = rows[0], rows[1]
    return {
        "ts_new": ts0,
        "c_new": float(c0),
        "ts_prev": ts1,
        "c_prev": float(c1),
    }


def _direction_from_two_closes(data: Optional[Dict[str, object]]) -> str:
    if not data:
        return "unknown"
    c_new = float(data["c_new"])
    c_prev = float(data["c_prev"])
    if c_new > c_prev:
        return "up"
    if c_new < c_prev:
        return "down"
    return "flat"


def vix_trend(tf: str = "5m") -> str:
    return _direction_from_two_closes(get_latest_two_closes("VIX", tf=tf))


def sqqq_dir(tf: str = "5m") -> str:
    return _direction_from_two_closes(get_latest_two_closes("SQQQ", tf=tf))


def vix_sqqq_confirmation(tf: str = "5m") -> Tuple[str, str, Dict[str, object]]:
    """Return directional confirmation bias info from VIX/SQQQ pairs."""

    vix_data = get_latest_two_closes("VIX", tf=tf)
    sqqq_data = get_latest_two_closes("SQQQ", tf=tf)
    vt = _direction_from_two_closes(vix_data)
    sd = _direction_from_two_closes(sqqq_data)

    detail: Dict[str, object] = {
        "tf": tf,
        "vix_trend": vt,
        "sqqq_dir": sd,
        "vix_ts_new": vix_data["ts_new"] if vix_data else None,
        "vix_ts_prev": vix_data["ts_prev"] if vix_data else None,
        "sqqq_ts_new": sqqq_data["ts_new"] if sqqq_data else None,
        "sqqq_ts_prev": sqqq_data["ts_prev"] if sqqq_data else None,
    }

    if vt == "unknown" or sd == "unknown":
        return "UNKNOWN", "insufficient VIX/SQQQ data", detail

    if vt == "up" and sd == "up":
        return "BEARISH", "VIX rising + SQQQ rising -> downside confirmed", detail

    if vt == "down" and sd == "down":
        return "BULLISH", "VIX falling + SQQQ falling -> upside confirmed", detail

    if vt == "up" and sd in ("flat", "down"):
        return "NEUTRAL", "VIX up but SQQQ not confirming -> volatility without direction", detail

    if vt in ("flat", "down") and sd == "up":
        return "NEUTRAL", "SQQQ up but VIX not confirming -> possible chop/hedging", detail

    return "NEUTRAL", "mixed signals -> treat as neutral / range rules", detail


def render_directional_confirmation_block(tf: str = "5m") -> str:
    bias, note, detail = vix_sqqq_confirmation(tf=tf)
    icon = "🟢" if bias == "BULLISH" else ("🔴" if bias == "BEARISH" else ("🟡" if bias == "NEUTRAL" else "🟠"))
    return (
        "🧭 **Directional Confirmation (VIX + SQQQ)**\n"
        f"• TF: {detail.get('tf', '?')} | VIX trend: **{detail.get('vix_trend', '?')}** | SQQQ dir: **{detail.get('sqqq_dir', '?')}**\n"
        f"• {icon} Bias confirm: **{bias}** — {note}"
    )


def earnings_within_window(symbol: str, now_et: datetime, window_minutes: int = 48 * 60) -> Optional[dict]:
    if not EARNINGS_API_KEY:
        return None
    symbol = symbol.upper()
    for offset in range(0, 3):
        target_date = now_et.date() + timedelta(days=offset)
        date_str = target_date.strftime("%Y-%m-%d")
        try:
            rows = fetch_earnings_for_date(date_str)
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            continue
        for row in rows:
            if (row.get("symbol") or "").upper() != symbol:
                continue
            when = (row.get("when") or "").upper()
            if when == "BMO":
                event_time = time(8, 30)
            elif when == "AMC":
                event_time = time(16, 30)
            else:
                event_time = time(12, 0)
            event_dt = datetime.combine(target_date, event_time, tzinfo=ET)
            delta_min = (event_dt - now_et).total_seconds() / 60.0
            if 0 <= delta_min <= window_minutes:
                label = "within 24h" if delta_min <= 24 * 60 else "within 48h"
                return {
                    "symbol": symbol,
                    "when": when,
                    "event_dt": event_dt,
                    "delta_min": delta_min,
                    "window": label,
                }
    return None


def load_macro_events_for_date(date_et: str) -> list[dict]:
    events: list[dict] = []
    if not os.path.exists(MACRO_EVENTS_FILE):
        return events
    try:
        with open(MACRO_EVENTS_FILE, "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if (row.get("date_et") or "").strip() != date_et:
                    continue
                events.append(
                    {
                        "time_et": (row.get("time_et") or "").strip(),
                        "title": (row.get("title") or "").strip(),
                        "impact": (row.get("impact") or "").strip().upper() or "MED",
                    }
                )
    except Exception:  # noqa: BLE001
        return []

    def _sort_key(event: dict) -> str:
        return event.get("time_et") or "99:99"

    return sorted(events, key=_sort_key)


def macro_within_minutes(events: Optional[list[dict]], minutes: int, now_et: datetime) -> Optional[dict]:
    if not events:
        return None
    for item in events:
        t = (item.get("time_et") or "").strip()
        if not t:
            continue
        try:
            hh, mm = [int(x) for x in t.split(":")]
            ev_dt = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            delta_min = (ev_dt - now_et).total_seconds() / 60.0
            if 0 <= delta_min <= minutes and (item.get("impact", "").upper() == "HIGH"):
                return item | {"delta_min": delta_min, "event_dt": ev_dt}
        except Exception:  # noqa: BLE001
            continue
    return None


def format_morning_brief(symbols: Optional[list[str]] = None) -> str:
    now_et = datetime.now(ET)
    today_et = now_et.strftime("%Y-%m-%d")
    target_date_et = next_market_date_et(now_et)
    sess = market_session_et(now_et)
    lines = [f"📈 *Morning Brief — {target_date_et}*"]
    if target_date_et != today_et:
        lines.append(f"🗓️ Note: using next market day (today is {today_et}).")
    lines.append("")
    lines.append(f"🕗 Session: {sess} (ET {now_et.strftime('%H:%M')})")

    vix = get_vix_context("1m")
    if vix:
        lines.append(f"🔥 VIX: {vix['level']:.2f} ({vix['regime']})")
        gate = vix_gating_action(float(vix["level"]))
        if gate["mode"] == "HARD":
            lines.append("Posture: **STAND DOWN**")
        elif gate["mode"] == "SOFT":
            lines.append("Posture: **CAUTION**")
        else:
            lines.append("Posture: **NORMAL**")

    macro = load_macro_events_for_date(target_date_et)

    banner = macro_risk_banner(macro, label=f"Macro Risk ({target_date_et} ET)")
    if banner:
        lines.append(banner)

    tomorrow_et = next_calendar_day_et(target_date_et)
    macro_tom = load_macro_events_for_date(tomorrow_et)
    banner_tom = macro_risk_banner(macro_tom, label=f"Tomorrow Risk ({tomorrow_et} ET)")
    if banner_tom:
        lines.append(banner_tom)
    lines.append("")
    lines.append("📅 **Macro Events (ET)**")
    if macro:
        for e in macro[:10]:
            lines.append(
                f"• {e.get('time_et','')} — {impact_icon(e.get('impact'))} {e.get('title','')}".strip()
            )
    else:
        lines.append("• (none listed)")

        earn = fetch_earnings_for_date(target_date_et)
        lines.append("")
        lines.append("💼 **Earnings**")

        if earn:
            bmo = sorted({x["symbol"] for x in earn if x.get("when") == "BMO"})
            amc = sorted({x["symbol"] for x in earn if x.get("when") == "AMC"})
            tas = sorted({x["symbol"] for x in earn if x.get("when") == "TAS"})

            if bmo:
                lines.append(f"• Before Market Open: {fmt_earnings_list(bmo)}")
            if amc:
                lines.append(f"• After Market Close: {fmt_earnings_list(amc)}")
            if tas:
                lines.append(f"• Time not supplied: {fmt_earnings_list(tas)}")

            if not (bmo or amc or tas):
                lines.append("• (none on watchlist)")
        else:
            if not EARNINGS_API_KEY:
                lines.append("• (earnings disabled: missing API key)")
            else:
                lines.append("• (none on watchlist / provider empty or rate-limited)")

    symbols = symbols or ["SPY", "QQQ", "IWM"]
    lines.append("")
    lines.append("🔎 **Levels / Plan**")
    for symbol in symbols:
        piv_data = get_latest_daily_pivots(symbol)
        if not piv_data:
            lines.append(f"{symbol}: (no pivots yet)")
            continue
        pivots = piv_data["piv"]
        lines.append(f"{symbol}: P {pivots['P']:.2f} | R1 {pivots['R1']:.2f} | S1 {pivots['S1']:.2f}")

    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


def next_weekday(d: date) -> date:
    # Mon-Fri only (MVP-safe; holidays not detected without an external calendar)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def next_market_date_et(now_et: datetime | None = None) -> str:
    now_et = now_et or datetime.now(ET)
    d = now_et.date()
    if d.weekday() >= 5:
        d = next_weekday(d)
    return d.strftime("%Y-%m-%d")


def next_calendar_day_et(d_str: str) -> str:
    # d_str = YYYY-MM-DD
    y, m, d = [int(x) for x in d_str.split("-")]
    dd = date(y, m, d) + timedelta(days=1)
    return dd.strftime("%Y-%m-%d")


def earnings_tag(sym: str) -> str:
    sym = sym.upper()
    tags: list[str] = []
    if sym in MEGA_CAP:
        tags.append("⭐")
    if sym in INDEX_HEAVY:
        tags.append("📌")
    return f" {''.join(tags)}" if tags else ""


def fmt_earnings_list(syms: list[str], limit: int = 18) -> str:
    syms = syms[:limit]
    return ", ".join([f"{s}{earnings_tag(s)}" for s in syms])


def is_high_event(e: dict) -> bool:
    title = (e.get("title") or "").lower()
    impact = (e.get("impact") or "").upper()
    if impact == "HIGH":
        return True
    return any(k in title for k in HIGH_KEYWORDS)


def macro_risk_banner(events: list[dict], label: str = "Macro Risk") -> str | None:
    if not events:
        return None

    hi = [e for e in events if is_high_event(e)]
    if not hi:
        return None

    hi = sorted(hi, key=lambda x: (x.get("time_et") or "99:99"))[:2]
    parts: list[str] = []
    for e in hi:
        t = (e.get("time_et") or "").strip()
        title = (e.get("title") or "").strip()
        parts.append(f"{impact_icon(e.get('impact'))} {t} {title}".strip())

    return f"🚨 {label}: " + " | ".join(parts)


def impact_icon(impact: str) -> str:
    impact = (impact or "").upper()
    if impact == "HIGH":
        return "🔴"
    if impact == "MED":
        return "🟠"
    if impact == "LOW":
        return "🟡"
    return "🟠"


def load_brief_state() -> Dict[str, object]:
    try:
        if os.path.exists(BRIEF_STATE_PATH):
            with open(BRIEF_STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                if isinstance(data, dict):
                    return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_brief_state(state: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(BRIEF_STATE_PATH), exist_ok=True)
    with open(BRIEF_STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


async def notify_ops(message: str) -> None:
    try:
        if BOT_ALERT_CHANNEL_ID:
            channel = bot.get_channel(BOT_ALERT_CHANNEL_ID)
            if channel:
                await channel.send(f"⚠️ **BOT ALERT**\n{message}")
    except Exception:  # noqa: BLE001
        pass


def options_structures(bias: str, vol_mode: str, conviction: str) -> list[str]:
    if vol_mode == "HARD":
        return ["Stand down (volatility stress). If trading: very small defined-risk only."]

    suggestions: list[str] = []
    bias = bias.upper()
    conviction = conviction.upper()
    vol_mode = vol_mode.upper()

    if bias == "BULL":
        suggestions.append("Bullish: Call debit spread (defined risk)")
        if vol_mode == "OK" and conviction in {"MED", "HIGH"}:
            suggestions.append("Alternative: Long call (only if conviction HIGH and levels confirm)")
    elif bias == "BEAR":
        suggestions.append("Bearish: Put debit spread (defined risk)")
        if vol_mode == "OK" and conviction in {"MED", "HIGH"}:
            suggestions.append("Alternative: Long put (only if conviction HIGH and levels confirm)")
    else:
        suggestions.append("Neutral: Iron condor / short premium *only* if range holds and VIX not elevated")
        suggestions.append("Neutral alt: Calendar/diagonal (if expecting IV change)")

    if vol_mode == "SOFT":
        suggestions.append("Vol elevated: favor spreads; avoid naked long premium unless strong edge")

    return suggestions


def build_playbooks(symbol: str, prob_up: float, piv: dict, vix_gate_mode: str, session: str) -> list[dict]:
    P = float(piv["P"])
    R1 = float(piv["R1"])
    S1 = float(piv["S1"])
    bias = classify_bias(prob_up)
    conv = classify_conviction(prob_up)

    if vix_gate_mode == "HARD":
        return [
            {
                "title": "STAND DOWN (volatility stress)",
                "body": [
                    "No new 0DTE trades recommended under HARD VIX gating.",
                    "If you must trade: defined-risk only, smallest size, strict time stop.",
                ],
                "structures": [
                    "0DTE: debit spreads only (small)",
                    "Weeklies: debit spreads / calendars (small)",
                ],
            }
        ]

    out: list[dict] = []

    trend_struct_0dte = "0DTE: debit spread (call spread if bull / put spread if bear)"
    trend_struct_wkly = "Weeklies: debit spread or diagonal (if you want more time)"

    trend_rules = [
        f"Trigger (bull): reclaim/hold above Pivot **P {P:.2f}** → target R1 **{R1:.2f}**.",
        f"Trigger (bear): lose/hold below Pivot **P {P:.2f}** → target S1 **{S1:.2f}**.",
        "Confirmation: wait for break + retest (avoid first spike).",
        "Stops: invalidation = return back through P after entry; add time-stop (e.g., 20–40 min).",
    ]
    if vix_gate_mode == "SOFT":
        trend_rules.append("VIX CAUTION: prefer spreads; avoid naked long premium unless conviction HIGH.")

    out.append(
        {
            "title": "PLAYBOOK 1 — TREND (best if breakout/breakdown)",
            "body": trend_rules,
            "structures": [trend_struct_0dte, trend_struct_wkly],
            "notes": f"Bias={bias}, Conviction={conv} (prob_up={prob_up:.2f})",
        }
    )

    range_struct_0dte = (
        "0DTE: defined-risk (iron condor) *only if range holds* OR quick debit scalps at edges"
    )
    range_struct_wkly = "Weeklies: calendars/diagonals (if expecting chop + IV shift)"

    range_rules = [
        f"Range zone: between **S1 {S1:.2f}** and **R1 {R1:.2f}** (use P as magnet).",
        "Long mean-reversion idea: reactions at S1/P/R1 — take only clean rejections, not mid-range noise.",
        "No-trade condition: if price is accelerating and closing outside S1/R1 → switch to TREND playbook.",
        "Risk: defined-risk only; cap max loss per play; add a time stop if chop persists.",
    ]
    if vix_gate_mode == "SOFT":
        range_rules.append("VIX CAUTION: range trades OK only if price action is orderly; reduce frequency.")

    out.append(
        {
            "title": "PLAYBOOK 2 — RANGE / MEAN REVERSION (best if chop)",
            "body": range_rules,
            "structures": [range_struct_0dte, range_struct_wkly],
            "notes": "Use when price is respecting levels and volatility is not exploding.",
        }
    )

    return out


def _day_ok(now_et: datetime) -> bool:
    day_names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    return day_names[now_et.weekday()] in MORNING_BRIEF_DAYS


async def morning_brief_loop() -> None:
    state = load_brief_state()

    while True:
        try:
            if not MORNING_BRIEF_ENABLED or not MORNING_BRIEF_CHANNEL_ID:
                await asyncio.sleep(60)
                continue

            now_et = datetime.now(ET)

            # weekday filter
            map_days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
            if map_days[now_et.weekday()] not in MORNING_BRIEF_DAYS:
                await asyncio.sleep(300)
                continue

            # target time
            hh, mm = [int(x) for x in MORNING_BRIEF_TIME_ET.split(":")]
            target = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)

            # one post per calendar day (persists across restarts)
            key = f"brief_posted_{now_et.date().isoformat()}"
            already = bool(state.get(key, False))

            if now_et >= target and not already:
                ch = bot.get_channel(MORNING_BRIEF_CHANNEL_ID)
                if not ch:
                    await notify_ops(
                        f"Morning brief channel not found: channel_id={MORNING_BRIEF_CHANNEL_ID}"
                    )
                else:
                    msg = format_morning_brief()
                    await ch.send(msg)

                    state[key] = True
                    state["last_post_ts"] = now_et.isoformat()
                    save_brief_state(state)

            await asyncio.sleep(20)

        except Exception as exc:  # noqa: BLE001
            await notify_ops(f"Morning brief loop exception: `{exc}`")
            await asyncio.sleep(60)


async def morning_opt_loop() -> None:
    global _last_morning_opt_date

    target_time = _parse_time_et(MORNING_OPT_TIME_ET, time(9, 28))

    while True:
        try:
            if not MORNING_OPT_ENABLED:
                await asyncio.sleep(60)
                continue

            channel_id = MORNING_OPT_CHANNEL_ID or CHANNEL_ID
            if not channel_id:
                await asyncio.sleep(60)
                continue

            now_et = datetime.now(ET)
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            target_dt = now_et.replace(
                hour=target_time.hour,
                minute=target_time.minute,
                second=0,
                microsecond=0,
            )

            if now_et < target_dt:
                await asyncio.sleep(30)
                continue

            if _last_morning_opt_date == now_et.date():
                await asyncio.sleep(60)
                continue

            vix = get_vix_context("1m")
            gate = vix_gating_action(float(vix["level"])) if vix else {"mode": "OK", "reason": ""}
            if gate.get("mode", "").upper() == "HARD":
                print(f"[OPT928] {now_et.date()}: skip (VIX gate HARD)")
                _last_morning_opt_date = now_et.date()
                await asyncio.sleep(60)
                continue

            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"[OPT928][WARN] cannot fetch channel {channel_id}: {exc}")
                    await asyncio.sleep(60)
                    continue

            header = "📣 **09:28 ET Options Toolkit**"
            if vix:
                header += f" | VIX {vix['level']:.2f} ({vix['regime']})"
            gate_note = gate.get("mode", "OK")
            if gate.get("reason"):
                header += f" | gate {gate_note} ({gate['reason']})"
            else:
                header += f" | gate {gate_note}"
            await channel.send(header)

            symbols = MORNING_OPT_SYMBOLS or ["SPY", "QQQ", "IWM"]
            for sym in symbols:
                payload, err = build_opt_payload(sym)
                if err:
                    await channel.send(err)
                    continue
                await channel.send(render_opt_clean(payload))

            _last_morning_opt_date = now_et.date()
            await asyncio.sleep(90)

        except Exception as exc:  # noqa: BLE001
            print(f"[OPT928][ERROR] {exc}")
            await asyncio.sleep(60)


def vix_gating_action(vix_level: float) -> Dict[str, object]:
    if VIX_HARD_BLOCK_LEVEL and vix_level >= VIX_HARD_BLOCK_LEVEL:
        return {"mode": "HARD", "mult": 0.0, "reason": f"level {vix_level:.2f} >= {VIX_HARD_BLOCK_LEVEL:.2f}"}
    if VIX_SOFT_BLOCK_LEVEL and vix_level >= VIX_SOFT_BLOCK_LEVEL:
        return {"mode": "SOFT", "mult": 0.5, "reason": f"level {vix_level:.2f} >= {VIX_SOFT_BLOCK_LEVEL:.2f}"}
    return {"mode": "OK", "mult": 1.0, "reason": "VIX normal"}


def vix_gating_decision(vix: Optional[Dict[str, object]]) -> tuple[str, Optional[str]]:
    if not VIX_GATING_ENABLED or not vix:
        return "ok", None
    level = float(vix["level"])
    regime = str(vix["regime"]).upper()
    regime_rank = _REGIME_ORDER.get(regime, 99)
    max_rank = _REGIME_ORDER.get(VIX_MAX_REGIME, 99)
    if regime_rank > max_rank:
        return "hard", f"regime {regime} > max {VIX_MAX_REGIME}"
    if VIX_HARD_BLOCK_LEVEL and level >= VIX_HARD_BLOCK_LEVEL:
        return "hard", f"level {level:.2f} >= hard block {VIX_HARD_BLOCK_LEVEL:.2f}"
    if VIX_SOFT_BLOCK_LEVEL and level >= VIX_SOFT_BLOCK_LEVEL:
        return "soft", f"level {level:.2f} >= soft block {VIX_SOFT_BLOCK_LEVEL:.2f}"
    return "ok", None


def nearest_levels(price: float, levels: Dict[str, float], k: int = 2) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    items = [(name, float(value)) for name, value in levels.items()]
    below = sorted((item for item in items if item[1] <= price), key=lambda item: price - item[1])[:k]
    above = sorted((item for item in items if item[1] >= price), key=lambda item: item[1] - price)[:k]
    return below, above


def _extract_meta(meta: Optional[str]) -> Dict[str, float]:
    if not meta:
        return {}
    try:
        parsed = json.loads(meta)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: float(v) for k, v in parsed.items() if isinstance(v, (int, float))}


def format_signal(signal_row: Tuple[str, str, Optional[int], float, float, Optional[str], Optional[str]], price_info: Optional[Tuple[float, str]]) -> str:
    symbol, ts, horizon_min, prob_up, prob_down, model_version, meta = signal_row
    horizon = horizon_min if horizon_min is not None else HORIZON_MIN
    edge = abs(prob_up - 0.5)
    arrow = "↑" if prob_up >= 0.5 else "↓"
    lines = [
        f"📊 **{symbol}** model `{model_version or MODEL_VERSION}`",
        f"• Horizon {horizon}m | Prob↑ {prob_up:.3f} {arrow} | Prob↓ {prob_down:.3f}",
        f"• Edge {edge:.3f} | Signal time {_format_ts(ts)}",
    ]
    if price_info:
        price, price_ts = price_info
        lines.append(f"• Last price {price:.2f} @ {_format_ts(price_ts)}")
    extras = _extract_meta(meta)
    if extras:
        lines.append("• Meta: " + " | ".join(f"{k}={v:.2f}" for k, v in extras.items()))
    return "\n".join(lines)


def accuracy_by_vix_regime(symbol: str, horizon_min: int, n: int = 200, tf: str = "1m") -> Optional[Dict[str, Dict[str, float]]]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT ts, prob_up FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol.upper(), n),
            )
        except sqlite3.OperationalError:
            return None
        sigs = cur.fetchall()

        buckets: Dict[str, list[int]] = {}
        for sig_ts, prob_up in sigs:
            vix = get_close_at_or_before("VIX", tf, sig_ts)
            if not vix:
                continue
            regime = vix_regime(float(vix["close"]))
            buckets.setdefault(regime, [0, 0])

            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, sig_ts),
            )
            start = cur.fetchone()
            if not start:
                continue
            start_ts, start_close = start
            target_ts = (parse_iso(start_ts) + timedelta(minutes=horizon_min)).isoformat()
            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, target_ts),
            )
            finish = cur.fetchone()
            if not finish:
                continue
            finish_close = float(finish[1])
            start_close = float(start_close)
            hit = finish_close > start_close
            pred_up = float(prob_up) >= 0.5
            buckets[regime][0] += 1 if hit == pred_up else 0
            buckets[regime][1] += 1

    if not buckets:
        return None
    out: Dict[str, Dict[str, float]] = {}
    for regime, (hits, total) in buckets.items():
        if total:
            out[regime] = {"acc": hits / total, "hits": hits, "total": total}
    return out


def rolling_accuracy(symbol: str, horizon_min: int, n: int = 50, tf: str = "1m") -> Optional[Dict[str, object]]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT ts, prob_up FROM signals WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                (symbol.upper(), n),
            )
        except sqlite3.OperationalError:
            return None
        sigs = cur.fetchall()

        hits = 0
        matured = 0
        for sig_ts, prob_up in sigs:
            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, sig_ts),
            )
            start = cur.fetchone()
            if not start:
                continue
            start_ts, start_close = start
            target_ts = (parse_iso(start_ts) + timedelta(minutes=horizon_min)).isoformat()
            cur.execute(
                """
                SELECT ts, close FROM prices
                WHERE symbol=? AND tf=? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), tf, target_ts),
            )
            finish = cur.fetchone()
            if not finish:
                continue
            finish_close = float(finish[1])
            start_close = float(start_close)
            matured += 1
            hit = finish_close > start_close
            pred_up = float(prob_up) >= 0.5
            hits += 1 if hit == pred_up else 0

    if matured == 0:
        return {"hits": 0, "matured": 0, "considered": len(sigs), "acc": None}
    return {"hits": hits, "matured": matured, "considered": len(sigs), "acc": hits / matured}


def format_daily_summary(symbols: list[str]) -> str:
    now_et = datetime.now(ET)
    lines = [
        "📌 **Daily Summary (signals + pivots)**",
        f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}",
    ]

    futures_block = _build_futures_section(post_type="daily_summary", now_ts=now_et.isoformat())
    if futures_block:
        lines.append("")
        lines.append(futures_block.rstrip("\n"))

    mode_setting = get_analysis_mode()
    for sym in symbols:
        sym = sym.upper()
        lines.append(f"\n**{sym}**")
        piv = get_latest_daily_pivots(sym)
        if piv:
            vals = piv["piv"]
            lines.append(
                "• Prev RTH pivots: P {P:.2f} | R1 {R1:.2f} | S1 {S1:.2f} | R2 {R2:.2f} | S2 {S2:.2f}".format(**vals)
            )
        else:
            lines.append("• (no daily pivots yet)")
        cols, sig = get_latest_signal_context(sym)
        have_signal = bool(sig)
        if have_signal:
            data = {col: sig[idx] for idx, col in enumerate(cols)}
            prob_up = float(data.get("prob_up", 0.5) or 0.5)
            edge_val = data.get("edge")
            edge = float(edge_val) if edge_val is not None else abs(prob_up - 0.5)
            conv = conviction_from_edge(edge)
            model_name = str(data.get("model_version") or data.get("model") or "unknown")
            lines.append(
                f"• Signal: prob_up {prob_up:.2f} | edge {edge:.2f} | conv {conv} | model {model_name}"
            )
        else:
            vix_ctx = get_vix_context("1m")
            vix_level = float(vix_ctx["level"]) if vix_ctx and "level" in vix_ctx else None
            gate_mode = vix_gating_action(vix_level)["mode"] if vix_level is not None else "OK"
            confirm_bias, confirm_note, _ = vix_sqqq_confirmation(tf=os.getenv("BIAS_TF", BIAS_TF))
            reasons, next_check = explain_no_trade(
                sym,
                None,
                gate_mode,
                confirm_bias,
                confirm_note,
                mode=mode_setting,
            )
            lines.append("• Signal status: **NO TRADE**")
            lines.append(f"• Last evaluated: {_utc_now_iso()}")
            if reasons:
                lines.append("• Reasons:")
                for reason in reasons[:3]:
                    lines.append(f"  - {reason}")
            lines.append(f"• {next_check}")

        if have_signal:
            acc = rolling_accuracy(sym, horizon_min=HORIZON_MIN, n=50, tf="1m")
            if acc and acc["acc"] is not None:
                lines.append(f"• Rolling acc: {acc['acc']*100:.1f}% ({acc['hits']}/{acc['matured']})")
            elif acc:
                lines.append(f"• Rolling acc: n/a (coverage {acc['matured']}/{acc['considered']})")
            else:
                lines.append("• Rolling acc: n/a")
    lines.append("\n_Not financial advice._")
    return "\n".join(lines)


def format_monday_playbook(symbols: list[str]) -> str:
    session = market_session_et()
    title = "Monday Open Playbook" if datetime.now(ET).weekday() == 0 else "Next RTH Playbook"
    lines = [f"📘 **{title} (levels + regimes + plan)**", f"ET: {datetime.now(ET).strftime('%Y-%m-%d %H:%M')} | Session={session}"]
    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, _confirm_note, _ = vix_sqqq_confirmation(tf=tf_bias)
    primary_sym = symbols[0] if symbols else "SPY"
    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig else None
    edge_val = _edge(prob_up) if prob_up is not None else 0.0
    min_edge = AUTOPOST_EDGE_MIN_STRICT
    strong_edge = prob_up is not None and edge_val >= min_edge
    market_status = "ACTIVE" if confirm_bias not in ("NEUTRAL", "UNKNOWN") and strong_edge else "WAIT MODE"
    lines.append(f"Market Status: **{market_status}**")
    lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    lines.append("")
    vix = get_vix_context("1m")
    if vix:
        lines.append(f"VIX: **{vix['level']:.2f}** ({vix['regime']})")
        gate = vix_gating_action(float(vix["level"]))
        if gate["mode"] == "HARD":
            lines.append("Posture: **STAND DOWN** (volatility stress)")
        elif gate["mode"] == "SOFT":
            lines.append("Posture: **CAUTION** (reduce size / be selective)")
        else:
            lines.append("Posture: **NORMAL**")
    for sym in symbols:
        sym = sym.upper()
        lines.append(f"\n**{sym}**")
        piv = get_latest_daily_pivots(sym)
        if piv:
            vals = piv["piv"]
            lines.append(
                f"Prev RTH pivots: P {vals['P']:.2f} | R1 {vals['R1']:.2f} | S1 {vals['S1']:.2f} | R2 {vals['R2']:.2f} | S2 {vals['S2']:.2f}"
            )
            lines.extend(
                [
                    "Plan:",
                    f"• Bull case: reclaim/hold above **P {vals['P']:.2f}** → target **R1 {vals['R1']:.2f}**",
                    f"• Bear case: lose/hold below **P {vals['P']:.2f}** → test **S1 {vals['S1']:.2f}**",
                    "• If inside S1-R1: treat as range until break + retest",
                ]
            )
        else:
            lines.append("• (no daily pivots yet)")
    lines.append("\n_Not financial advice._")
    return "\n".join(lines)


async def daily_summary_loop() -> None:
    global _last_summary_date
    while True:
        try:
            now_et = datetime.now(ET)
            try:
                hh, mm = [int(x) for x in DAILY_SUMMARY_TIME_ET.split(":", 1)]
            except ValueError:
                hh, mm = 9, 31
            target = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= target:
                if _last_summary_date != now_et.date():
                    channel_id = DAILY_SUMMARY_CHANNEL_ID
                    if channel_id:
                        channel = bot.get_channel(channel_id)
                        if channel:
                            await channel.send(format_daily_summary(DAILY_SUMMARY_SYMBOLS))
                            _last_summary_date = now_et.date()
                await asyncio.sleep(30)
            else:
                wait_seconds = max(1.0, min(30.0, (target - now_et).total_seconds()))
                await asyncio.sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"[DAILY_SUMMARY_ERROR] {exc}")
            await asyncio.sleep(30)


async def monday_playbook_loop() -> None:
    global _last_monday_date
    while True:
        try:
            now_et = datetime.now(ET)
            if now_et.weekday() != 0:
                _last_monday_date = None
                await asyncio.sleep(300)
                continue
            try:
                hh, mm = [int(x) for x in MONDAY_PLAYBOOK_TIME_ET.split(":", 1)]
            except ValueError:
                hh, mm = 9, 25
            target = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= target:
                if _last_monday_date != now_et.date():
                    channel = bot.get_channel(MONDAY_PLAYBOOK_CHANNEL_ID)
                    if channel is None:
                        try:
                            channel = await bot.fetch_channel(MONDAY_PLAYBOOK_CHANNEL_ID)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[MONDAY][WARN] Unable to fetch channel {MONDAY_PLAYBOOK_CHANNEL_ID}: {exc}")
                            await asyncio.sleep(60)
                            continue
                    if channel is None:
                        print(f"[MONDAY][WARN] Channel {MONDAY_PLAYBOOK_CHANNEL_ID} not accessible")
                        await asyncio.sleep(60)
                        continue
                    session = market_session_et()
                    if session == "CLOSED":
                        msg = (
                            "📘 **Monday Playbook**\nET: "
                            + datetime.now(ET).strftime("%Y-%m-%d %H:%M")
                            + "\n⚠️ Market session is CLOSED. Holiday or unexpected closure—verify market hours."
                        )
                        await channel.send(msg)
                    else:
                        symbols = MONDAY_PLAYBOOK_SYMBOLS or ["SPY", "QQQ", "IWM"]
                        await channel.send(format_monday_playbook(symbols))
                    _last_monday_date = now_et.date()
                    print(f"[MONDAY] Playbook posted for {_last_monday_date}")
                await asyncio.sleep(60)
            else:
                wait_seconds = max(5.0, min(300.0, (target - now_et).total_seconds()))
                await asyncio.sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001
            print(f"[MONDAY][ERROR] {exc}")
            await asyncio.sleep(60)


def is_rth_now() -> bool:
    return market_session_et() == "RTH"


def _now_et() -> datetime:
    if ET_TZ:
        return datetime.now(ET_TZ)
    # fallback: local time (still ok for MVP, but ET_TZ should exist on Py3.13)
    return datetime.now()


def _parse_hhmm(s: str, default: str = "09:25") -> tuple[int, int]:
    t = (s or "").strip() or default
    hh, mm = t.split(":")
    return int(hh), int(mm)


def _ensure_watchlist_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            added_ts TEXT NOT NULL
        )
        """
    )

    existing = {row[1] for row in conn.execute("PRAGMA table_info(watchlist)")}
    altered = False
    for column, ddl in WATCHLIST_EXTRA_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE watchlist ADD COLUMN {column} {ddl}")
            altered = True
    if altered:
        conn.commit()

def migrate_settings_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL,
            updated_ts TEXT NOT NULL
        )
        """
    )
    conn.commit()


def settings_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    cur = conn.cursor()
    row = cur.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def settings_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    from datetime import datetime, timezone

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings(k,v,updated_ts) VALUES(?,?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_ts=excluded.updated_ts",
        (key, value, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _watchlist_record_attempt(symbol: str, attempt_ts: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist(symbol, added_ts) VALUES (?, ?)",
            (symbol, attempt_ts),
        )
        conn.execute(
            "UPDATE watchlist SET last_attempt_ts=?, last_error=NULL WHERE symbol=?",
            (attempt_ts, symbol),
        )
        conn.commit()


def _watchlist_record_success(symbol: str, ready_ts: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        conn.execute(
            "UPDATE watchlist SET last_ready_ts=?, last_error=NULL WHERE symbol=?",
            (ready_ts, symbol),
        )
        conn.commit()


def _watchlist_record_error(symbol: str, attempt_ts: str, error: str) -> None:
    truncated = (error or "")[:240]
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist(symbol, added_ts) VALUES (?, ?)",
            (symbol, attempt_ts),
        )
        conn.execute(
            "UPDATE watchlist SET last_error=?, last_attempt_ts=? WHERE symbol=?",
            (truncated or None, attempt_ts, symbol),
        )
        conn.commit()


def _fetch_watchlist_symbols() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        _ensure_watchlist_schema(conn)
        migrate_settings_table(conn)
        cursor = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol ASC")
        rows = cursor.fetchall()
    raw = [row[0] for row in rows if row and row[0]]
    symbols = _filter_startup_symbols(raw)
    if symbols:
        return symbols
    if _STARTUP_SYMBOL_SET:
        return STARTUP_SYMBOLS
    return [str(row[0]).upper() for row in rows if row and row[0]]


def _fetch_watchlist_entries() -> list[dict]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure_watchlist_schema(conn)
            migrate_settings_table(conn)
            cursor = conn.execute(
                "SELECT symbol, added_ts, last_ready_ts, last_attempt_ts, last_error FROM watchlist ORDER BY symbol"
            )
            col_names = [desc[0] for desc in cursor.description or []]
            rows = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] fetch watchlist entries failed: {exc}")
        return []

    entries: list[dict] = []
    for row in rows:
        entry = {}
        for idx, name in enumerate(col_names):
            entry[name] = row[idx]
        entries.append(entry)
    if not _STARTUP_SYMBOL_SET:
        return entries
    filtered: list[dict] = []
    for entry in entries:
        sym = str(entry.get("symbol") or "").upper()
        if sym in _STARTUP_SYMBOL_SET:
            entry = dict(entry)
            entry["symbol"] = sym
            filtered.append(entry)
    return filtered


def _get_watchlist() -> list[str]:
    try:
        symbols = _fetch_watchlist_symbols()
        if symbols:
            return symbols
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] watchlist fetch failed: {exc}")

    fallback_env = os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",")
    fallback = _filter_startup_symbols(fallback_env)
    if fallback:
        return fallback
    return STARTUP_SYMBOLS


def _bootstrap_backfill_symbol(symbol: str, *, days: int = BOOTSTRAP_BACKFILL_DAYS) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    end = now + timedelta(minutes=1)
    api_symbol = polygon_ingest.SYMBOL_API_MAP.get(symbol, symbol)
    bars = polygon_ingest.polygon_aggs_1m(api_symbol, start, end)
    if not isinstance(bars, list):
        return 0, 0

    with sqlite3.connect(DB_PATH) as conn:
        inserted = polygon_ingest.upsert_bars(conn, symbol, bars, source="polygon_bootstrap")
    return len(bars), inserted


def _bootstrap_write_daily_bar(symbol: str) -> bool:
    session = _latest_session_bars(symbol)
    if not session:
        return False

    bars = session.get("bars", [])
    if not bars:
        return False

    open_val = float(bars[0]["open"])
    high_val = max(float(b["high"]) for b in bars)
    low_val = min(float(b["low"]) for b in bars)
    close_val = float(bars[-1]["close"])
    volume_val = sum(float(b.get("volume", 0.0)) for b in bars)

    session_date = session["date"]
    close_et = datetime.combine(session_date, RTH_CLOSE, tzinfo=ET)
    ts_utc = close_et.astimezone(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        price_columns = {row[1] for row in conn.execute("PRAGMA table_info(prices)")}
        columns = ["symbol", "tf", "ts", "open", "high", "low", "close"]
        values: list[object] = [symbol, "1d", ts_utc, open_val, high_val, low_val, close_val]

        if "volume" in price_columns:
            columns.append("volume")
            values.append(volume_val)
        if "source" in price_columns:
            columns.append("source")
            values.append("bootstrap")
        if "is_partial" in price_columns:
            columns.append("is_partial")
            values.append(0)

        placeholders = ",".join(["?"] * len(values))
        conn.execute(
            f"INSERT OR REPLACE INTO prices({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
    return True


def _bootstrap_run_model(symbol: str) -> None:
    from model import run_model as model_run

    backup_symbols = list(getattr(model_run, "SYMBOLS", []))
    try:
        model_run.SYMBOLS = [symbol]
        model_run.main()
    finally:
        model_run.SYMBOLS = backup_symbols


def _ensure_symbol_ready_sync(symbol: str) -> dict:
    sym = symbol.upper()
    attempt_ts = datetime.now(timezone.utc).isoformat()
    result: dict[str, object] = {
        "symbol": sym,
        "ok": False,
        "already_ready": False,
        "fetched_bars": 0,
        "inserted_bars": 0,
        "daily_written": False,
        "signal": None,
        "steps": [],
        "errors": [],
    }

    try:
        _watchlist_record_attempt(sym, attempt_ts)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"watchlist init failed: {exc}")
        return result

    freshness_limit = DATA_STALE_MAX_MIN if DATA_STALE_MAX_MIN > 0 else 3.0
    existing_signal = get_latest_signal(sym)
    fresh = data_is_fresh(sym, tf="1m", max_min=freshness_limit)

    if fresh and existing_signal:
        result["ok"] = True
        result["already_ready"] = True
        result["signal"] = existing_signal
        _watchlist_record_success(sym, attempt_ts)
        return result

    try:
        if not symbol_supported_polygon(sym):
            raise RuntimeError("Polygon does not report trades for this symbol")
    except Exception as exc:  # noqa: BLE001
        message = f"polygon availability failed: {exc}"
        result["errors"].append(message)
        _watchlist_record_error(sym, attempt_ts, message)
        return result

    try:
        fetched, inserted = _bootstrap_backfill_symbol(sym)
        result["fetched_bars"] = fetched
        result["inserted_bars"] = inserted
        result.setdefault("steps", []).append(f"backfill fetched {fetched} bars (inserted {inserted})")
    except Exception as exc:  # noqa: BLE001
        message = f"backfill failed: {exc}"
        result["errors"].append(message)
        _watchlist_record_error(sym, attempt_ts, message)
        return result

    try:
        daily_written = _bootstrap_write_daily_bar(sym)
        result["daily_written"] = daily_written
        if daily_written:
            result.setdefault("steps", []).append("daily bar updated")
    except Exception as exc:  # noqa: BLE001
        message = f"daily build failed: {exc}"
        result["errors"].append(message)

    before_ts = existing_signal.get("ts") if isinstance(existing_signal, dict) else None
    try:
        _bootstrap_run_model(sym)
    except Exception as exc:  # noqa: BLE001
        message = f"model run failed: {exc}"
        result["errors"].append(message)

    latest_signal = get_latest_signal(sym)
    if latest_signal:
        result["signal"] = latest_signal
        latest_ts = latest_signal.get("ts")
        if latest_ts and latest_ts != before_ts:
            result.setdefault("steps", []).append(f"new signal {latest_ts}")

    fresh_after = data_is_fresh(sym, tf="1m", max_min=freshness_limit)
    if latest_signal and fresh_after:
        result["ok"] = True
        _watchlist_record_success(sym, attempt_ts)
    else:
        if not fresh_after:
            result["errors"].append("data still stale after bootstrap")
        if not latest_signal:
            result["errors"].append("no signal available after bootstrap")
        _watchlist_record_error(sym, attempt_ts, "; ".join(str(e) for e in result["errors"]) or "bootstrap incomplete")

    print(
        "[BOOTSTRAP] {sym} ok={ok} fetched={fetched} inserted={inserted} errors={errors}".format(
            sym=sym,
            ok=result.get("ok"),
            fetched=result.get("fetched_bars"),
            inserted=result.get("inserted_bars"),
            errors=len(result.get("errors", [])),
        )
    )

    return result


async def ensure_symbol_ready(symbol: str) -> dict:
    return await asyncio.to_thread(_ensure_symbol_ready_sync, symbol)


def _format_bootstrap_result(result: dict) -> str:
    sym = result.get("symbol", "UNKNOWN")
    lines: list[str] = []

    if result.get("ok"):
        if result.get("already_ready"):
            lines.append(f"✅ **{sym}** already had fresh data and signal.")
        else:
            lines.append(f"✅ **{sym}** bootstrapped.")
            lines.append(
                "• Bars inserted: {inserted} (fetched {fetched})".format(
                    inserted=result.get("inserted_bars", 0),
                    fetched=result.get("fetched_bars", 0),
                )
            )
            if result.get("daily_written"):
                lines.append("• Daily bar updated from latest RTH session")
        signal = result.get("signal") or {}
        ts = signal.get("ts")
        prob_up = signal.get("prob_up")
        model_version = signal.get("model_version")
        if ts and prob_up is not None:
            try:
                prob_fmt = f"{float(prob_up):.3f}"
            except Exception:  # noqa: BLE001
                prob_fmt = str(prob_up)
            lines.append(f"• Signal: ts {ts} | prob_up {prob_fmt} | model {model_version}")
        elif ts:
            lines.append(f"• Signal: {ts}")
        else:
            lines.append("• Model has not produced a signal yet")
        if result.get("errors"):
            lines.append("⚠️ Notes:")
            for note in result["errors"][:3]:
                lines.append(f"• {note}")
    else:
        lines.append(f"⚠️ **{sym}** bootstrap failed.")
        errors = result.get("errors") or ["Unknown error"]
        for err in errors[:4]:
            lines.append(f"• {err}")

    return "\n".join(lines)


def _schedule_bootstrap(symbol: str, channel: discord.abc.MessageableChannel, *, announce: bool = False) -> bool:
    sym = symbol.upper()
    existing = _bootstrap_tasks.get(sym)
    if existing and not existing.done():
        return False

    task = asyncio.create_task(_run_bootstrap(sym, channel, announce=announce))
    _bootstrap_tasks[sym] = task
    task.add_done_callback(lambda _task, s=sym: _bootstrap_tasks.pop(s, None))
    return True


async def _run_bootstrap(symbol: str, channel: discord.abc.MessageableChannel, *, announce: bool = False) -> None:
    try:
        if announce:
            await safe_send(channel, f"🔄 Bootstrapping {symbol}…", kind="status", symbol=symbol, pivots=None)

        result = await ensure_symbol_ready(symbol)
        message = _format_bootstrap_result(result)
        if message:
            await safe_send(channel, message, kind="status", symbol=symbol, pivots=None)
    except Exception as exc:  # noqa: BLE001
        print(f"[BOOTSTRAP][ERROR] {symbol}: {exc}")
        try:
            await safe_send(
                channel,
                f"⚠️ Bootstrap for {symbol} crashed: {exc}",
                kind="status",
                symbol=symbol,
                pivots=None,
            )
        except Exception:  # noqa: BLE001
            pass

def _edge(prob_up: float) -> float:
    return abs(prob_up - 0.5)


def _conv(edge: float) -> str:
    if edge < 0.02:
        return "LOW"
    if edge < 0.05:
        return "MED"
    return "HIGH"


def _edge_threshold_for_mode(mode: Optional[str]) -> float:
    mode_norm = (mode or "strict").strip().lower()
    if mode_norm == "insights":
        return AUTOPOST_EDGE_MIN_INSIGHTS
    return AUTOPOST_EDGE_MIN_STRICT


def _extract_signal_state(payload: dict) -> Optional[SignalState]:
    if not isinstance(payload, dict):
        return None

    regime = str(payload.get("pivot_regime") or payload.get("regime") or "").strip().upper()
    if not regime:
        return None

    state: SignalState = {"regime": regime}
    state["extension_mode"] = bool(payload.get("pivot_extension_mode"))

    def _safe_float(val: object) -> Optional[float]:
        try:
            return float(val)
        except Exception:  # noqa: BLE001
            return None

    state["pivot"] = _safe_float(payload.get("pivot"))
    state["last_price"] = _safe_float(payload.get("last_price"))
    return state


def _record_signal_state(sym: str, state: Optional[SignalState]) -> None:
    if state:
        _last_signal_state[sym] = state
    else:
        _last_signal_state.pop(sym, None)


def _has_invalidation_break(prev_state: SignalState, state: SignalState) -> bool:
    pivot = state.get("pivot")
    if pivot is None:
        pivot = prev_state.get("pivot")
    if pivot is None:
        return False

    try:
        prev_price = float(prev_state.get("last_price"))
        curr_price = float(state.get("last_price"))
    except (TypeError, ValueError):
        return False

    if not math.isfinite(prev_price) or not math.isfinite(curr_price):
        return False

    def _side(price: float) -> str:
        if price > pivot:
            return "above"
        if price < pivot:
            return "below"
        return "at"

    prev_side = _side(prev_price)
    curr_side = _side(curr_price)
    if prev_side == curr_side:
        return False

    if prev_side == "above" and curr_side in {"below", "at"}:
        return True
    if prev_side == "below" and curr_side in {"above", "at"}:
        return True
    if prev_side == "at" and curr_side != "at":
        return True
    return False


def _should_post_signal(sym: str, prob_up: float, gate_mode: str, *, mode: str, state: Optional[SignalState]) -> tuple[bool, str]:
    mode_norm = (mode or "strict").strip().lower()
    e = _edge(prob_up)
    min_edge = _edge_threshold_for_mode(mode_norm)

    if gate_mode == "HARD":
        return False, "VIX gate HARD"

    if e < min_edge:
        return False, f"edge {e:.3f} < min {min_edge:.3f} ({mode_norm})"

    # cooldown per symbol
    cooldown = _env_int("AUTOPOST_SIGNAL_COOLDOWN_SEC", "600")
    now = int(datetime.now(timezone.utc).timestamp())
    last = _last_signal_post_by_symbol.get(sym, 0)
    if now - last < cooldown:
        return False, f"cooldown {cooldown}s"

    if state:
        regime = str(state.get("regime") or "").upper()
        if regime and regime != "UNKNOWN":
            prev_state = _last_signal_state.get(sym)
            if prev_state:
                prev_regime = str(prev_state.get("regime") or "").upper()
                same_regime = prev_regime == regime
                same_extension = bool(prev_state.get("extension_mode")) == bool(state.get("extension_mode"))
                if same_regime and same_extension and not _has_invalidation_break(prev_state, state):
                    return False, "regime unchanged"

    return True, "ok"


def _get_stale_lock() -> asyncio.Lock:
    global _data_stale_lock
    if _data_stale_lock is None:
        _data_stale_lock = asyncio.Lock()
    return _data_stale_lock


def _format_ts_et(ts_iso: Optional[str]) -> str:
    if not ts_iso:
        return "unknown"
    try:
        dt_et = parse_iso(ts_iso).astimezone(ET)
        return dt_et.strftime("%Y-%m-%d %H:%M:%S ET")
    except Exception:  # noqa: BLE001
        return str(ts_iso)


def _calc_data_stale(symbol: str = DATA_STALE_SYMBOL) -> tuple[bool, Optional[float], Optional[str]]:
    if DATA_STALE_MAX_MIN <= 0:
        return False, None, None
    bar = get_latest_bar(symbol, tf="1m")
    if not bar:
        return True, None, None
    ts_iso = bar[0]
    try:
        ts_dt = parse_iso(ts_iso)
    except Exception:  # noqa: BLE001
        return True, None, ts_iso
    age_min = (datetime.now(timezone.utc) - ts_dt).total_seconds() / 60.0
    return age_min > DATA_STALE_MAX_MIN, age_min, ts_iso


async def _fetch_warn_channel() -> Optional[discord.abc.MessageableChannel]:
    warn_channel_id = DATA_STALE_WARN_CHANNEL_ID or CHANNEL_ID
    if not warn_channel_id:
        return None
    channel = bot.get_channel(warn_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(warn_channel_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Unable to fetch warn channel {warn_channel_id}: {exc}")
            return None
    return channel


async def _send_stale_notification(message: str) -> None:
    channel = await _fetch_warn_channel()
    if channel is None:
        print(f"[WARN] {message}")
        return
    try:
        await safe_send(channel, message, kind="status", symbol="", pivots=None)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Failed to send stale notification: {exc}")


async def _update_data_stale_state() -> bool:
    if DATA_STALE_MAX_MIN <= 0:
        return False

    lock = _get_stale_lock()
    async with lock:
        global _data_stale_paused
        stale, age_min, ts_iso = _calc_data_stale()
        if stale:
            if not _data_stale_paused:
                _data_stale_paused = True
                age_text = "unknown"
                if age_min is not None:
                    age_text = f"{age_min:.1f} min"
                ts_text = _format_ts_et(ts_iso)
                await _send_stale_notification(
                    f"⚠️ **AutoPost Paused** — data feed stale. Latest {DATA_STALE_SYMBOL} 1m bar {ts_text} (age {age_text}, limit {DATA_STALE_MAX_MIN:.1f} min)."
                )
            return True

        if _data_stale_paused:
            _data_stale_paused = False
            age_text = "unknown"
            if age_min is not None:
                age_text = f"{age_min:.1f} min"
            ts_text = _format_ts_et(ts_iso)
            await _send_stale_notification(
                f"✅ **AutoPost Resumed** — data feed current. Latest {DATA_STALE_SYMBOL} 1m bar {ts_text} (age {age_text})."
            )
        return False


async def _autopost_stale_guard() -> bool:
    if DATA_STALE_MAX_MIN <= 0:
        return False
    return await _update_data_stale_state()


def _safe_float(val: Optional[object]) -> Optional[float]:
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _derive_futures_posture(ctx: dict) -> Optional[str]:
    on_high = _safe_float(ctx.get("on_high"))
    on_low = _safe_float(ctx.get("on_low"))
    prior_high = _safe_float(ctx.get("prior_rth_high"))
    prior_low = _safe_float(ctx.get("prior_rth_low"))

    if on_high is None or on_low is None or prior_high is None or prior_low is None:
        return None

    buffer_val = max(0.25, (prior_high - prior_low) * 0.02)
    if on_high > prior_high + buffer_val:
        return "• Posture: Overnight pressing above prior RTH high."
    if on_low < prior_low - buffer_val:
        return "• Posture: Overnight testing below prior RTH low."
    return "• Posture: Overnight holding inside prior RTH range."


def _load_futures_context_payload(now_ts: Optional[str] = None) -> dict:
    path = FUTURES_CONTEXT_PATH
    if not path:
        return {"ok": False, "reason": "path not configured"}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw_payload = json.load(handle)
    except FileNotFoundError:
        return {"ok": False, "reason": "missing"}
    except Exception as exc:  # noqa: BLE001
        print(f"[AUTOPOST] futures context read error: {exc}")
        return {"ok": False, "reason": "read error"}

    if not isinstance(raw_payload, dict):
        return {"ok": False, "reason": "invalid payload"}

    payload = dict(raw_payload)
    ctx_candidate = payload.get("context") if isinstance(raw_payload.get("context"), dict) else raw_payload
    ctx = dict(ctx_candidate) if isinstance(ctx_candidate, dict) else {}
    payload["context"] = ctx

    if now_ts:
        try:
            now_et = parse_iso(now_ts).astimezone(ET)
        except Exception:  # noqa: BLE001
            now_et = datetime.now(ET)
    else:
        now_et = datetime.now(ET)

    computed_ts = ctx.get("computed_ts") or payload.get("computed_ts")
    if not computed_ts:
        payload["ok"] = False
        payload["reason"] = "missing timestamp"
        return payload

    try:
        computed_dt = parse_iso(str(computed_ts)).astimezone(ET)
    except Exception:  # noqa: BLE001
        payload["ok"] = False
        payload["reason"] = "unparseable timestamp"
        return payload

    payload["computed_dt"] = computed_dt.isoformat()
    age_minutes = (now_et - computed_dt).total_seconds() / 60.0
    payload["age_minutes"] = age_minutes

    session = market_session_et(now_et.astimezone(timezone.utc))
    payload["session"] = session

    threshold_min = FUTURES_STALE_MINUTES_ACTIVE if session in {"PRE", "RTH"} else FUTURES_STALE_MINUTES_OFFHOURS
    if age_minutes > threshold_min:
        payload["ok"] = False
        payload["reason"] = f"stale ({int(age_minutes)} min old)"
        return payload

    payload["ok"] = bool(raw_payload.get("ok", True))
    payload["session_date"] = ctx.get("session_date") or payload.get("session_date")
    return payload


def _format_futures_context_lines(
    payload: dict,
    *,
    post_type: str = "default",
    show_reason: bool = False,
) -> list[str]:
    if not isinstance(payload, dict):
        return []

    if not payload.get("ok", True):
        reason = payload.get("reason")
        if show_reason and isinstance(reason, str) and reason:
            return [f"• Futures context unavailable: {reason}"]
        return []

    ctx = payload.get("context")
    if not isinstance(ctx, dict):
        return []

    lines: list[str] = []

    def _on_range_line() -> str:
        return (
            "• ON range:"
            f" H {_fmt(_safe_float(ctx.get('on_high')))}"
            f" / L {_fmt(_safe_float(ctx.get('on_low')))}"
            f" / VWAP {_fmt(_safe_float(ctx.get('on_vwap')))}"
        )

    def _intraday_summary_line() -> Optional[str]:
        on_vwap = _safe_float(ctx.get("on_vwap"))
        last_price = _safe_float(ctx.get("last_price") or ctx.get("last"))
        if on_vwap is None:
            return None
        if last_price is None:
            return f"• ON VWAP {_fmt(on_vwap)} (latest price pending)."
        relation = "holding above" if last_price >= on_vwap else "lost"
        return f"• ES {relation} ON VWAP ({_fmt(on_vwap)})."

    if post_type in {"daily_prep", "after_hours", "pre_market", "default"}:
        lines.append(_on_range_line())
        lines.append(
            "• RTH:"
            f" H {_fmt(_safe_float(ctx.get('rth_high')))}"
            f" / L {_fmt(_safe_float(ctx.get('rth_low')))}"
        )
        lines.append(
            "• Prior RTH:"
            f" H {_fmt(_safe_float(ctx.get('prior_rth_high')))}"
            f" / L {_fmt(_safe_float(ctx.get('prior_rth_low')))}"
            f" / Close {_fmt(_safe_float(ctx.get('prior_close')))}"
        )
    elif post_type == "daily_summary":
        lines.append(_on_range_line())
    elif post_type == "focus_list":
        lines.append(_on_range_line())
    elif post_type == "intraday_update":
        short_line = _intraday_summary_line()
        if short_line:
            lines.append(short_line)
        else:
            lines.append(_on_range_line())
    else:
        lines.append(_on_range_line())

    posture_line = _derive_futures_posture(ctx)
    if posture_line:
        if post_type in {"daily_summary", "focus_list"}:
            lines.append(posture_line.replace("Overnight", "ON"))
        elif post_type not in {"intraday_update"}:
            lines.append(posture_line)

    session_date = ctx.get("session_date") or payload.get("session_date")
    computed_ts = ctx.get("computed_ts") or payload.get("computed_dt") or payload.get("computed_ts")
    if post_type not in {"daily_summary", "intraday_update", "focus_list"}:
        timestamp_bits: list[str] = []
        if isinstance(session_date, str) and session_date:
            timestamp_bits.append(session_date)
        if isinstance(computed_ts, str) and computed_ts:
            try:
                timestamp_bits.append(_format_ts_et(computed_ts))
            except Exception:
                timestamp_bits.append(str(computed_ts))
        if timestamp_bits:
            lines.append("• Updated: " + " | ".join(timestamp_bits))

    return [line for line in lines if line.strip()]


def _build_futures_section(
    *,
    post_type: str = "default",
    now_ts: Optional[str] = None,
    show_reason: bool = False,
) -> str:
    payload = _load_futures_context_payload(now_ts)
    lines = _format_futures_context_lines(payload, post_type=post_type, show_reason=show_reason)
    if not lines:
        return ""
    return "\n".join([
        f"📊 Futures Context — **{FUTURES_CONTEXT_LABEL}**",
        *lines,
        "",
    ])


def _normalize_section_lines(raw: Optional[object], default: list[str], *, prefix: str = "• ") -> list[str]:
    if raw is None:
        return default

    candidates: list[str] = []
    if isinstance(raw, str):
        candidates.append(raw)
    elif isinstance(raw, dict):
        if isinstance(raw.get("lines"), (list, tuple)):
            candidates.extend(str(item) for item in raw["lines"])
        else:
            label = raw.get("label") or raw.get("title") or raw.get("name")
            note = raw.get("note") or raw.get("summary") or raw.get("text")
            if label and note:
                candidates.append(f"{label}: {note}")
            elif note:
                candidates.append(str(note))
            elif label:
                candidates.append(str(label))
            for key, value in raw.items():
                if key in {"label", "title", "name", "note", "summary", "text", "lines"}:
                    continue
                candidates.append(f"{key}: {value}")
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                label = item.get("label") or item.get("symbol") or item.get("name") or item.get("title")
                note = item.get("note") or item.get("summary") or item.get("text") or item.get("value")
                if label and note:
                    candidates.append(f"{label}: {note}")
                elif label:
                    candidates.append(str(label))
                elif note:
                    candidates.append(str(note))
                else:
                    candidates.append(str(item))
            else:
                candidates.append(str(item))
    else:
        candidates.append(str(raw))

    normalized: list[str] = []
    prefix_trim = prefix.strip()
    for candidate in candidates:
        clean = str(candidate).strip()
        if not clean:
            continue
        if prefix_trim and clean.startswith(prefix_trim):
            normalized.append(clean)
        else:
            normalized.append(f"{prefix}{clean}")

    return normalized or default


def _fetch_section_lines(fetcher_name: str, *, post_type: str, default: list[str]) -> list[str]:
    func = globals().get(fetcher_name)
    if not callable(func):
        return default
    try:
        result = func(post_type=post_type)
    except TypeError:
        result = func()
    except Exception:  # noqa: BLE001 - fall back to defaults when fixture fetch fails
        return default
    return _normalize_section_lines(result, default)


def _section_lines(
    explicit: Optional[object],
    fetcher_name: str,
    *,
    post_type: str,
    default: list[str],
    prefix: str = "• ",
) -> list[str]:
    if explicit is not None:
        return _normalize_section_lines(explicit, default, prefix=prefix)
    return _fetch_section_lines(fetcher_name, post_type=post_type, default=default)


def build_daily_prep_payload(symbols: list[str]) -> str:
    """Must pass the quality gate headings."""

    et = _now_et().strftime("%Y-%m-%d %H:%M ET")
    lines: list[str] = []
    lines.append("📌 **Daily Prep (RTH pivots + regime + plan)**")
    tf_bias = os.getenv("BIAS_TF", BIAS_TF)
    confirm_bias, _confirm_note, _ = vix_sqqq_confirmation(tf=tf_bias)
    primary_sym = symbols[0] if symbols else "SPY"
    sig = get_latest_signal(primary_sym)
    prob_up = float(sig["prob_up"]) if sig else None
    edge_val = _edge(prob_up) if prob_up is not None else 0.0
    min_edge = AUTOPOST_EDGE_MIN_STRICT
    strong_edge = prob_up is not None and edge_val >= min_edge
    market_status = "ACTIVE" if confirm_bias not in ("NEUTRAL", "UNKNOWN") and strong_edge else "WAIT MODE"
    lines.append(f"Market Status: **{market_status}**")
    lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    lines.append("")
    lines.append("⏱ Timeframes:")
    lines.append("• Execution: 5m")
    lines.append("• Structure: 60m")
    lines.append("• Context: 1D")
    lines.append("")
    lines.append("🧭 Regime:")
    # Keep conservative; daily prep is a plan, not a forecast
    lines.append("TRANSITION (confirm at key levels)")
    lines.append("")
    lines.append("")

    futures_block = _build_futures_section(post_type="daily_prep", now_ts=_now_et().isoformat(), show_reason=True)
    if futures_block:
        lines.append(futures_block.rstrip("\n"))
        lines.append("")

    lines.append("📐 Key Levels (RTH):")

    for sym in symbols:
        dp = get_latest_daily_pivots(sym)
        if not dp or "piv" not in dp:
            lines.append(f"• **{sym}**: (no pivots yet)")
            continue
        piv = dp["piv"]
        p_val = float(piv["P"])
        r1 = float(piv["R1"])
        s1 = float(piv["S1"])
        lines.append(f"• **{sym}**: P {p_val:.2f} | R1 {r1:.2f} | S1 {s1:.2f}")

    lines.append("")
    lines.append("🧠 How Pros Would Trade It:")
    lines.append("• Use TREND playbook only on break + retest at Pivot / S1 / R1")
    lines.append("• Use RANGE rules only if price holds inside S1–R1 (avoid mid-range)")
    lines.append("• Defined risk only; reduce size if conviction is LOW or confirmation is NEUTRAL")
    lines.append("")
    lines.append("🚫 Do Nothing If:")
    lines.append("• Directional confirmation is NEUTRAL/UNKNOWN and price chops around Pivot")
    lines.append("• High-impact macro window is imminent (if you add macro banners)")
    lines.append("")
    lines.append(f"ET: {et}")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


def build_after_hours_payload(
    symbols: Optional[list[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    now_et = _now_et()
    lines: list[str] = []
    lines.append("🌙 **After Hours Rundown**")
    lines.append(f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    futures_block = _build_futures_section(post_type="after_hours", now_ts=now_et.isoformat(), show_reason=True)
    if futures_block:
        lines.append(futures_block.rstrip("\n"))
        lines.append("")

    tracked = [sym.upper() for sym in (symbols or []) if sym]
    if tracked:
        lines.append("📊 Coverage:")
        lines.append(f"• Monitoring {', '.join(tracked)} into tomorrow.")
        lines.append("")

    lines.append("📈 Regime Snapshot:")
    lines.extend(
        _section_lines(
            regime,
            "get_regime_snapshot",
            post_type="after_hours",
            default=["• Regime snapshot pending."],
        )
    )
    lines.append("")

    lines.append("📐 Levels To Review:")
    lines.extend(
        _section_lines(
            levels,
            "get_levels_focus",
            post_type="after_hours",
            default=["• Levels fixture pending."],
        )
    )
    lines.append("")

    lines.append("📝 Options Flow:")
    lines.extend(
        _section_lines(
            options,
            "get_options_focus",
            post_type="after_hours",
            default=["• Options flow placeholder."],
        )
    )
    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


def build_pre_market_payload(
    symbols: Optional[list[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    now_et = _now_et()
    lines: list[str] = []
    lines.append("🌅 **Pre-Market Briefing**")
    lines.append(f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    futures_block = _build_futures_section(post_type="pre_market", now_ts=now_et.isoformat(), show_reason=True)
    if futures_block:
        lines.append(futures_block.rstrip("\n"))
        lines.append("")

    watch = [sym.upper() for sym in (symbols or []) if sym]
    if watch:
        lines.append("🎯 Opening Watch:")
        lines.append(f"• Primary symbols: {', '.join(watch)}")
        lines.append("")

    lines.append("📈 Regime Snapshot:")
    lines.extend(
        _section_lines(
            regime,
            "get_regime_snapshot",
            post_type="pre_market",
            default=["• Regime snapshot pending."],
        )
    )
    lines.append("")

    lines.append("📐 Key Levels For The Open:")
    lines.extend(
        _section_lines(
            levels,
            "get_levels_focus",
            post_type="pre_market",
            default=["• Levels fixture pending."],
        )
    )
    lines.append("")

    lines.append("📝 Options Flow Highlights:")
    lines.extend(
        _section_lines(
            options,
            "get_options_focus",
            post_type="pre_market",
            default=["• Options flow placeholder."],
        )
    )
    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


def build_focus_list_payload(
    symbols: Optional[list[str]] = None,
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    now_et = _now_et()
    lines: list[str] = []
    lines.append("🎯 **Focus List — Next RTH**")
    lines.append(f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    futures_block = _build_futures_section(post_type="focus_list", now_ts=now_et.isoformat())
    if futures_block:
        lines.append(futures_block.rstrip("\n"))
        lines.append("")

    lines.append("📈 Regime Snapshot:")
    lines.extend(
        _section_lines(
            regime,
            "get_regime_snapshot",
            post_type="focus_list",
            default=["• Regime snapshot pending."],
        )
    )
    lines.append("")

    focus_symbols = [sym.upper() for sym in (symbols or STARTUP_SYMBOLS) if sym]
    if focus_symbols:
        lines.append("🔭 Focus Symbols:")
        for sym in focus_symbols:
            lines.append(f"• {sym}: setup placeholder.")
        lines.append("")

    lines.append("📐 Levels In Play:")
    lines.extend(
        _section_lines(
            levels,
            "get_levels_focus",
            post_type="focus_list",
            default=["• Levels fixture pending."],
        )
    )
    lines.append("")

    lines.append("📝 Options Focus:")
    lines.extend(
        _section_lines(
            options,
            "get_options_focus",
            post_type="focus_list",
            default=["• Options focus placeholder."],
        )
    )
    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


def build_intraday_update_payload(
    *,
    regime: Optional[object] = None,
    levels: Optional[object] = None,
    options: Optional[object] = None,
) -> str:
    now_et = _now_et()
    lines: list[str] = []
    lines.append("⚡ **Intraday Update**")
    lines.append(f"ET date/time: {now_et.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    futures_block = _build_futures_section(post_type="intraday_update", now_ts=now_et.isoformat())
    if futures_block:
        lines.append(futures_block.rstrip("\n"))
        lines.append("")

    lines.append("📈 Regime Shift Watch:")
    lines.extend(
        _section_lines(
            regime,
            "get_regime_snapshot",
            post_type="intraday_update",
            default=["• Intraday regime placeholder."],
        )
    )
    lines.append("")

    lines.append("📐 Levels In Play:")
    lines.extend(
        _section_lines(
            levels,
            "get_levels_focus",
            post_type="intraday_update",
            default=["• Levels fixture pending."],
        )
    )
    lines.append("")

    lines.append("📝 Options Signals:")
    lines.extend(
        _section_lines(
            options,
            "get_options_focus",
            post_type="intraday_update",
            default=["• Options focus placeholder."],
        )
    )
    lines.append("")
    lines.append("_Not financial advice._")
    return "\n".join(lines)


async def build_signal_alert_payload(sym: str, *, mode: str) -> Optional[tuple[str, str]]:
    """Build a mode-aware autopost payload, returning (text, output_mode)."""

    built = build_signal_payload(sym)
    if not built:
        return None

    payload, prob_up, gate = built
    gate_mode = (gate.get("mode") or "OK").upper()

    mode_norm = (mode or "strict").strip().lower()
    mode_norm = mode_norm if mode_norm in VALID_MODES else "strict"

    edge_val = _edge(prob_up)
    payload["edge"] = edge_val
    payload["edge_threshold"] = _edge_threshold_for_mode(mode_norm)
    strict_floor = AUTOPOST_EDGE_MIN_STRICT
    insights_floor = AUTOPOST_EDGE_MIN_INSIGHTS
    payload["educational_only"] = (
        mode_norm == "insights"
        and edge_val < strict_floor
        and edge_val >= insights_floor
    )

    ctx = payload.get("level_ctx")
    last_price = payload.get("last_price")
    if ctx and last_price is not None:
        ok_targets, target_err = validate_targets_vs_price(
            float(last_price),
            ctx.get("upside_targets"),
            ctx.get("downside_targets"),
        )
        if not ok_targets:
            reason = f"Quality gate: {target_err or 'target validation failed'}"
            print(f"[AUTOPOST] skip {sym}: {reason}")
            return None

    state = _extract_signal_state(payload)
    ok, reason = _should_post_signal(sym, prob_up, gate_mode, mode=mode_norm, state=state)
    if not ok:
        if reason and "cooldown" not in reason.lower() and "regime unchanged" not in reason.lower():
            print(f"[AUTOPOST] skip {sym}: {reason}")
        return None

    analysis_mode = (payload.get("analysis_mode") or "db").strip().lower()
    if analysis_mode not in {"db", "on_demand"}:
        analysis_mode = "db"

    payload["analysis_mode"] = analysis_mode
    payload["analysis_source"] = payload.get("analysis_source") or "model"
    payload["analysis_output_mode"] = mode_norm

    live_price_override: Optional[float] = None
    live_price_ts: Optional[str] = None
    if analysis_mode == "on_demand":
        if isinstance(payload.get("last_price"), (int, float)):
            live_price_override = float(payload["last_price"])
        live_price_ts = payload.get("last_price_ts") if isinstance(payload.get("last_price_ts"), str) else None

    packet, _ = build_analysis_packet(
        sym,
        payload,
        analysis_mode=analysis_mode,
        live_price=live_price_override,
        live_price_ts=live_price_ts,
    )
    packet["analysis_output_mode"] = payload["analysis_output_mode"]
    packet["edge"] = payload.get("edge")
    packet["edge_threshold"] = payload.get("edge_threshold")
    packet["educational_only"] = payload.get("educational_only")
    payload["analysis_packet"] = packet

    if payload["analysis_output_mode"] == "insights":
        ai_text, ai_err = await ai_render_trade_context(_openai, packet)
        if ai_err:
            print(f"[AUTOPOST] insights fallback {sym}: {ai_err}")
        if ai_text:
            ok_ai, reason_ai = validate_output(ai_text, mode="insights")
            if ok_ai:
                _last_signal_post_by_symbol[sym] = int(datetime.now(timezone.utc).timestamp())
                _record_signal_state(sym, state)
                return ai_text, "insights"
            print(f"[AUTOPOST] insights blocked {sym}: {reason_ai}")

    fallback = format_signal_clean(sym, payload, verbose=False)
    ok_fb, reason_fb = validate_output(fallback, mode="strict")
    if ok_fb:
        _last_signal_post_by_symbol[sym] = int(datetime.now(timezone.utc).timestamp())
        _record_signal_state(sym, state)
        return fallback, "strict"

    print(f"[AUTOPOST] strict fallback blocked {sym}: {reason_fb}")
    return None


async def autopost_daily_loop(channel):
    await bot.wait_until_ready()
    global _last_daily_post_et_date

    hh, mm = _parse_hhmm(os.getenv("AUTOPOST_DAILY_TIME_ET", "09:25"))
    cooldown_hours = _env_int("AUTOPOST_DAILY_COOLDOWN_HOURS", "20")
    poll_sec = 30

    print(f"[OK] autopost_daily_loop running (time_et={hh:02d}:{mm:02d}, cooldown={cooldown_hours}h)")

    while not bot.is_closed():
        try:
            if not _env_bool("AUTOPOST_DAILY_ENABLED", "1"):
                await asyncio.sleep(poll_sec)
                continue

            max_min = float(os.getenv("DATA_STALE_MAX_MIN", "3"))
            age = latest_bar_age_min("SPY", "1m")
            if age is None or age > max_min:
                print(f"[WARN] data stale: SPY age_min={age}; autopost paused")
                await _autopost_stale_guard()
                await asyncio.sleep(poll_sec)
                continue

            if await _autopost_stale_guard():
                await asyncio.sleep(poll_sec)
                continue

            now_et = _now_et()
            # weekdays only by default
            if now_et.weekday() >= 5:
                await asyncio.sleep(60)
                continue

            due = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_et >= due and (_last_daily_post_et_date != now_et.date()):
                symbols = _get_watchlist()
                payload = build_daily_prep_payload(symbols)
                await safe_send(
                    channel,
                    payload,
                    kind="analysis",
                    symbol="",
                    pivots=None,
                    analysis_mode="db",
                    output_mode="strict",
                )

                _last_daily_post_et_date = now_et.date()
                print(f"[OK] daily prep posted for {now_et.date()}")

            await asyncio.sleep(poll_sec)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] autopost_daily_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll_sec)


async def autopost_signal_loop(channel):
    await bot.wait_until_ready()
    poll = _env_int("AUTOPOST_SIGNAL_POLL_SEC", "45")
    print(f"[OK] autopost_signal_loop running (poll={poll}s)")

    while not bot.is_closed():
        try:
            if not _env_bool("AUTOPOST_SIGNAL_ENABLED", "1"):
                await asyncio.sleep(poll)
                continue

            max_min = float(os.getenv("DATA_STALE_MAX_MIN", "3"))
            age = latest_bar_age_min("SPY", "1m")
            if age is None or age > max_min:
                print(f"[WARN] data stale: SPY age_min={age}; autopost paused")
                await _autopost_stale_guard()
                await asyncio.sleep(poll)
                continue

            if await _autopost_stale_guard():
                await asyncio.sleep(poll)
                continue

            symbols = _get_watchlist()
            mode_setting = get_analysis_mode()
            # Post at most 1 alert per poll cycle to avoid spam
            posted = False
            for sym in symbols:
                result = await build_signal_alert_payload(sym, mode=mode_setting)
                if result:
                    text, output_mode = result
                    await safe_send(
                        channel,
                        text,
                        kind="analysis",
                        symbol=sym,
                        pivots=None,
                        analysis_mode="db",
                        output_mode=output_mode,
                    )
                    posted = True
                    break

            await asyncio.sleep(poll)

        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] autopost_signal_loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(poll)


def _ensure_autopost_task():
    """Schedules BOTH daily + signal autopost loops if enabled. Idempotent."""
    global _autopost_daily_task, _autopost_signal_task

    if not _env_bool("AUTOPOST_ENABLED", "0"):
        print("[OK] autopost: disabled (AUTOPOST_ENABLED=0)")
        return

    channel_id = _env_int("AUTOPOST_CHANNEL_ID", "0")
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        print(f"[WARN] autopost: channel not found/invalid id={channel_id}; skipping tasks.")
        return

    if _env_bool("AUTOPOST_DAILY_ENABLED", "1"):
        if not _autopost_daily_task or _autopost_daily_task.done():
            _autopost_daily_task = bot.loop.create_task(autopost_daily_loop(channel))
            print("[OK] autopost daily task scheduled")

    if _env_bool("AUTOPOST_SIGNAL_ENABLED", "1"):
        if not _autopost_signal_task or _autopost_signal_task.done():
            _autopost_signal_task = bot.loop.create_task(autopost_signal_loop(channel))
            print("[OK] autopost signal task scheduled")


@bot.event
async def on_ready() -> None:
    user = bot.user
    guilds = ", ".join(g.name for g in bot.guilds) if bot.guilds else "n/a"
    print(f"[OK] Logged in as {user} (guilds: {guilds})")
    if VIX_ALERTS_ENABLED:
        fn = globals().get("vix_alert_loop")
        if callable(fn):
            bot.loop.create_task(fn())
            print("[OK] vix_alert_loop scheduled")
        else:
            print("[WARN] VIX alerts enabled but vix_alert_loop() is missing; skipping.")
    if MONDAY_PLAYBOOK_ENABLED and MONDAY_PLAYBOOK_CHANNEL_ID:
        bot.loop.create_task(monday_playbook_loop())
        print(
            f"[OK] Monday playbook enabled: {MONDAY_PLAYBOOK_TIME_ET} ET -> channel_id={MONDAY_PLAYBOOK_CHANNEL_ID}"
        )
    if MORNING_BRIEF_ENABLED and MORNING_BRIEF_CHANNEL_ID:
        bot.loop.create_task(morning_brief_loop())
        print(
            f"[OK] Morning brief enabled. time={MORNING_BRIEF_TIME_ET} ET channel_id={MORNING_BRIEF_CHANNEL_ID}"
        )
    if MORNING_OPT_ENABLED:
        bot.loop.create_task(morning_opt_loop())
        target_channel = MORNING_OPT_CHANNEL_ID or CHANNEL_ID
        print(f"[OK] Morning options toolkit enabled: {MORNING_OPT_TIME_ET} ET -> channel_id={target_channel}")

    try:
        fn = globals().get("_ensure_autopost_task")
        if callable(fn):
            fn()
        else:
            print("[WARN] _ensure_autopost_task missing; autopost disabled.")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] _ensure_autopost_task error: {exc}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    try:
        content = message.content or ""
        if content.strip().lower().startswith("!analyze"):
            sym = None
        else:
            sym = extract_analysis_request(content)
        if sym:
            dp = get_latest_daily_pivots(sym)
            pivots = dp.get("piv") if dp and isinstance(dp, dict) else None
            try:
                price_info = get_latest_price(sym)
            except Exception:  # noqa: BLE001
                price_info = None
            last_px, last_ts = price_info if price_info else (None, None)

            signal_bundle = build_signal_payload(sym)
            clean_payload = signal_bundle[0] if signal_bundle else None

            ai_text = maybe_ai_analysis(sym)
            if ai_text:
                ok, reason = validate_analysis_message(ai_text)
                if ok:
                    await message.channel.send(ai_text)
                else:
                    print(f"[ANALYZE] AI blocked {sym}: {reason}")
                    if QUALITY_GATE_DEBUG and reason:
                        try:
                            await message.channel.send(
                                f"Gate blocked AI response for {sym}: `{reason}`"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    if clean_payload:
                        fallback = format_signal_clean(sym, clean_payload, verbose=True)
                        if reason:
                            fallback += f"\n\n_Fallback reason: {reason}."
                    else:
                        note = reason or ""
                        fallback = build_safe_fallback(sym, pivots, note=note)
                    fb_ok, fb_reason = validate_analysis_message(fallback)
                    if not fb_ok:
                        print(f"[ANALYZE] fallback flagged {sym}: {fb_reason}")
                    await message.channel.send(fallback)
            else:
                if clean_payload:
                    fallback = format_signal_clean(sym, clean_payload, verbose=True)
                else:
                    fallback = build_safe_fallback(sym, pivots, note="no AI response")
                fb_ok, fb_reason = validate_analysis_message(fallback)
                if not fb_ok:
                    print(f"[ANALYZE] fallback flagged {sym}: {fb_reason}")
                    if QUALITY_GATE_DEBUG and fb_reason:
                        try:
                            await message.channel.send(
                                f"Gate blocked fallback for {sym}: `{fb_reason}`"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                await message.channel.send(fallback)
            await bot.process_commands(message)
            return
    except Exception as exc:  # noqa: BLE001
        await message.channel.send(f"⚠️ Analyze error: {type(exc).__name__}: {exc}")
        await bot.process_commands(message)
        return

    try:
        print(
            "[MSG] from={author} content={content!r} mentions={mentions} role_mentions={role_mentions}".format(
                author=message.author,
                content=message.content,
                mentions=[m.id for m in message.mentions],
                role_mentions=[r.id for r in message.role_mentions],
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[MSG][ERROR] {exc}")

    if not AI_ENABLED or _openai is None:
        await bot.process_commands(message)
        return
    if message.guild is None:
        await bot.process_commands(message)
        return
    if message.content.strip().startswith("!"):
        await bot.process_commands(message)
        return

    mentioned_user = bool(bot.user and bot.user in message.mentions)
    mentioned_role = bool(AI_ROLE_ID) and any(r.id == AI_ROLE_ID for r in message.role_mentions)
    triggered = mentioned_user or mentioned_role
    if AI_MODE == "mention" and not triggered:
        await bot.process_commands(message)
        return
    if AI_MODE == "channel" and message.channel.id != AI_CHANNEL_ID:
        await bot.process_commands(message)
        return

    user_text = message.content
    if bot.user:
        user_text = user_text.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "")
    if AI_ROLE_ID:
        user_text = user_text.replace(f"<@&{AI_ROLE_ID}>", "")
    user_text = user_text.strip()
    if not user_text:
        await message.reply("Ask me a question 🙂", mention_author=False)
        await bot.process_commands(message)
        return

    symbols = [
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "TSLA",
        "VIX",
    ]
    requested = None
    upper = user_text.upper()
    for sym in symbols:
        if sym in upper:
            requested = sym
            break

    market_context = ""
    pivots_for_gate: Optional[dict] = None
    if requested:
        last_close: Optional[float] = None
        session = market_session_et()
        market_context += f"Session (ET): {session}\n"
        if session != "RTH":
            market_context += "Note: Outside RTH, equity 1m bars may be sparse/stale.\n"
        bar = get_latest_bar(requested, tf="1m")
        if bar:
            ts, o, h, l, c = bar
            market_context += f"Latest {requested} 1m bar (DB): ts={ts}, O={o:.2f}, H={h:.2f}, L={l:.2f}, C={c:.2f}\n"
            last_close = float(c)
        dbar = get_latest_daily_bar(requested)
        if dbar:
            dts, _o, dh, dl, dc = dbar
            pivots = pivots_from_hlc(float(dh), float(dl), float(dc))
            pivots_for_gate = pivots
            below, above = nearest_levels(last_close or float(dc), pivots, k=2)
            market_context += (
                f"Daily pivots (from 1d bar @ {dts}): P={pivots['P']:.2f} R1={pivots['R1']:.2f} S1={pivots['S1']:.2f} "
                f"R2={pivots['R2']:.2f} S2={pivots['S2']:.2f}\n"
                f"Nearest supports: {', '.join([f'{n} {v:.2f}' for n, v in below]) or 'none'}\n"
                f"Nearest resistances: {', '.join([f'{n} {v:.2f}' for n, v in above]) or 'none'}\n"
            )
        cols, sig = get_latest_signal_context(requested)
        if sig:
            data = {col: sig[idx] for idx, col in enumerate(cols)}
            prob_up = float(data.get("prob_up", 0.5) or 0.5)
            prob_down = data.get("prob_down")
            prob_down_val = float(prob_down) if prob_down is not None else (1.0 - prob_up)
            edge_val = data.get("edge")
            edge = float(edge_val) if edge_val is not None else abs(prob_up - 0.5)
            model_name = str(data.get("model_version") or data.get("model") or "unknown")
            conv = conviction_from_edge(edge)
            sig_ts = data.get("ts") or (sig[0] if len(sig) > 0 else "n/a")
            market_context += (
                f"Latest signal: ts={sig_ts}, model={model_name}, prob_up={prob_up:.3f}, prob_down={prob_down_val:.3f}, "
                f"edge={edge:.3f}, conviction={conv}\n"
            )
            v = get_vix_context("1m")
            if v and VIX_GATING_ENABLED:
                gate = vix_gating_action(float(v["level"]))
                edge_adj = edge * float(gate["mult"])
                conv = conviction_from_edge(edge_adj)
                market_context += (
                    f"VIX gate: {gate['mode']} ({gate['reason']}); edge {edge:.3f} -> {edge_adj:.3f}; conviction={conv}\n"
                )
                if gate["mode"] == "HARD":
                    market_context += "Trading posture: STAND DOWN (wait for VIX to cool).\n"
            trend_raw = data.get("trend")
            vol_z_raw = data.get("vol_z")
            if trend_raw is not None and vol_z_raw is not None:
                trend = float(trend_raw)
                vol_z = float(vol_z_raw)
                market_context += (
                    f"Regime: {('VOL EXPANSION' if vol_z >= 1.5 else 'TREND' if abs(trend) >= 0.004 else 'RANGE / CHOP' if abs(trend) <= 0.0015 and vol_z <= 0.8 else 'MIXED')} "
                    f"(trend={trend:+.4f}, vol_z={vol_z:+.2f})\n"
                )
        acc = rolling_accuracy(requested, horizon_min=HORIZON_MIN, n=50, tf="1m")
        if acc and acc.get("acc") is not None:
            market_context += (
                f"Rolling accuracy: {acc['acc']*100:.1f}% over last {acc['matured']} matured of {acc['considered']} signals (@{HORIZON_MIN}m)\n"
            )
        vix = get_vix_context("1m")
        if vix:
            market_context += f"VIX: {vix['level']:.2f} ({vix['regime']}) as of {vix['ts']}\n"

    market_context = market_context.strip()
    print(f"[AI] responding to prompt={user_text!r}")
    prompt = (
        "You are TNT Trading Bot.\n"
        "Use the market context below. If timestamps look stale, say so.\n"
        "Be concise. No personalized financial advice.\n\n"
        f"User: {user_text}\n"
        f"{market_context}"
    )

    async with message.channel.typing():
        try:
            resp = await _call_openai(prompt)
            reply_text = (getattr(resp, "output_text", "") or "").strip() or _extract_ai_text(resp)
            if not reply_text:
                reply_text = "(No response text returned.)"
            trimmed = reply_text[:AI_MAX_CHARS]
            await safe_send(
                message.channel,
                trimmed,
                kind="analysis",
                symbol=requested or "",
                pivots=pivots_for_gate,
                output_mode="strict",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[AI ERROR] {exc}")
            await message.reply(f"[AI error] {exc}", mention_author=False)

    await bot.process_commands(message)


@bot.command(name="tf", aliases=["timeframe"])
async def timeframe_cmd(ctx: commands.Context) -> None:
    lines = timeframe_block_lines(
        execution=PRICE_TF_LABEL,
        structure="60m",
        context="1D",
    )
    lines.append(f"Signal horizon: {HORIZON_MIN}m probabilistic forward estimate")
    lines.append("Guardrail: Intraday only - not for swing, overnight, or earnings plays")
    await ctx.send("\n".join(lines))


@bot.command()
async def health(ctx: commands.Context) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        rows = _rows(conn, "SELECT symbol, max(ts) FROM prices GROUP BY symbol")
    lines = [f"🩺 Health Check (UTC {now})"]
    for sym, ts in rows:
        lines.append(f"- {sym}: {_format_ts(ts)}")
    await ctx.send("\n".join(lines))


@bot.command(name="analyze")
async def analyze_cmd(ctx: commands.Context, sym: str = "SPY", detail: str = "") -> None:
    sym = (sym or "SPY").upper().strip()
    detail_norm = (detail or "").lower().strip()
    if detail_norm in {"v", "verbose", "full"}:
        verbose = True
    elif detail_norm in {"b", "brief", "short"}:
        verbose = False
    else:
        verbose = VERBOSE_DEFAULT

    mode_setting = get_analysis_mode()

    max_age = float(os.getenv("DATA_STALE_MAX_MIN", "3") or "3")
    fresh = data_is_fresh(sym, tf="1m", max_min=max_age)
    live_px: Optional[float] = None
    live_ts: Optional[str] = None
    live_src: str = ""
    analysis_mode = "db"
    analysis_source = "model"

    if not fresh:
        try:
            if not symbol_supported_polygon(sym):
                await ctx.send(
                    f"⚠️ **{sym}** live snapshot unavailable from Polygon right now. Please try again in a moment."
                )
                return
        except Exception:
            await ctx.send(
                f"⚠️ **{sym}** Polygon availability check failed; try again later or pick another ticker."
            )
            return

        live_px, live_ts, live_src = fetch_live_price(sym)
        if live_px is None or live_ts is None:
            await ctx.send(
                f"⚠️ **{sym}** live price unavailable right now (source={live_src or 'polygon-live'})."
            )
            return
        analysis_mode = "on_demand"
        analysis_source = "polygon"

    bootstrap_result = None
    try:
        bootstrap_result = await asyncio.wait_for(
            ensure_symbol_ready(sym),
            timeout=ANALYZE_BOOTSTRAP_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        print(f"[BOOTSTRAP] ensure timeout for {sym} after {ANALYZE_BOOTSTRAP_TIMEOUT_S}s; scheduling background run")
        _schedule_bootstrap(sym, ctx.channel)
    except Exception as exc:  # noqa: BLE001
        print(f"[BOOTSTRAP] ensure error for {sym}: {exc}")
        _schedule_bootstrap(sym, ctx.channel)

    try:
        payload, err = build_analysis_payload(sym)
        if not payload:
            dp = get_latest_daily_pivots(sym)
            pivots = dp.get("piv") if dp and isinstance(dp, dict) else None

            if analysis_mode == "on_demand" and live_px is not None and live_ts is not None:
                snapshot = (
                    "\n".join(
                        [
                            f"📊 **{sym} — Live Snapshot (On-Demand)**",
                            "",
                            f"💲 **Last Price:** {fmt_money(live_px)}",
                            f"🕒 **Timestamp:** {live_ts} UTC",
                            f"📡 **Source:** {live_src or 'Polygon'}",
                            "",
                            "🧭 **Context**",
                            "• No stored model signal yet",
                            "• No historical pivots cached",
                            "• Live market snapshot only",
                            "",
                            "⚠️ This ticker is not yet ingested into TNT models.",
                            "Levels & regimes will improve once data accumulates.",
                            "",
                            "_Not financial advice._",
                        ]
                    ).strip()
                )
                await ctx.send(snapshot)
                _schedule_bootstrap(sym, ctx.channel)
                return

            note = err or "no signal data available"
            if err:
                print(f"[GATE] analyze {sym}: {err}")
            await ctx.send(build_safe_fallback(sym, pivots, note=note))
            return

        payload["analysis_mode"] = analysis_mode
        payload["analysis_source"] = analysis_source
        payload["analysis_output_mode"] = mode_setting
        if bootstrap_result is not None:
            payload["bootstrap_result"] = bootstrap_result

        packet, pivots_for_gate = build_analysis_packet(
            sym,
            payload,
            analysis_mode=analysis_mode,
            live_price=live_px,
            live_price_ts=live_ts,
        )
        if not packet:
            await ctx.send(f"⚠️ No data available for {sym} yet.")
            if analysis_mode == "on_demand":
                _schedule_bootstrap(sym, ctx.channel)
            return

        packet["analysis_output_mode"] = mode_setting
        payload["analysis_packet"] = packet

        if mode_setting == "insights":
            ai_text, ai_err = await ai_render_trade_context(_openai, packet)
            if ai_err:
                print(f"[AI] analyze {sym} fallback: {ai_err}")
            if ai_text:
                ok, reason = validate_output(ai_text, mode=mode_setting)
                if ok:
                    await safe_send(
                        ctx.channel,
                        ai_text,
                        kind="analysis",
                        symbol=sym,
                        pivots=pivots_for_gate,
                        analysis_mode=analysis_mode,
                        output_mode=mode_setting,
                    )
                    if analysis_mode == "on_demand":
                        _schedule_bootstrap(sym, ctx.channel)
                    return
                print(f"[INSIGHTS] blocked {sym}: {reason}")

        r_levels = packet.get("r_levels") or {}
        s_levels = packet.get("s_levels") or {}
        pivots_dict = {
            "P": packet.get("pivot"),
            "R1": r_levels.get("R1"),
            "R2": r_levels.get("R2"),
            "S1": s_levels.get("S1"),
            "S2": s_levels.get("S2"),
            "R3": r_levels.get("R3"),
            "S3": s_levels.get("S3"),
        }

        last_px = packet.get("current_price")
        last_ts = packet.get("current_price_ts")
        vix_dir = payload.get("vix_trend")
        sqqq_dir = payload.get("sqqq_dir")
        bias = payload.get("bias")
        conviction = payload.get("conviction")

        msg = format_trade_context(
            symbol=sym,
            last_price=last_px,
            last_price_ts_utc=last_ts,
            last_price_tf=PRICE_TF_LABEL,
            piv=pivots_dict,
            bias=bias or "NEUTRAL",
            conviction=conviction or "LOW",
            vix_dir=vix_dir,
            sqqq_dir=sqqq_dir,
            prefer_mode=mode_setting,
        )

        gate_ok, gate_reason = validate_analysis_message(msg)
        if not gate_ok:
            await ctx.send(f"⚠️ analysis blocked: {gate_reason}\nUse !daily or !status.")
            if analysis_mode == "on_demand":
                _schedule_bootstrap(sym, ctx.channel)
            return

        await safe_send(
            ctx.channel,
            msg,
            kind="analysis",
            symbol=sym,
            pivots=pivots_for_gate,
            analysis_mode=analysis_mode,
            output_mode="strict",
        )
        if analysis_mode == "on_demand":
            _schedule_bootstrap(sym, ctx.channel)
        return
    except Exception as exc:  # noqa: BLE001
        await ctx.send(f"⚠️ Analyze error: {type(exc).__name__}: {exc}")


@bot.command(name="mode")
async def mode_cmd(ctx: commands.Context, mode: str = "") -> None:
    mode = (mode or "").strip().lower()

    if not mode:
        cur = get_analysis_mode()
        await ctx.send(f"✅ Current analysis mode: **{cur.upper()}**")
        return

    if mode not in VALID_MODES:
        await ctx.send("⚠️ Usage: `!mode insights` or `!mode strict`")
        return

    try:
        set_analysis_mode(mode)
        await ctx.send(f"✅ Mode set: **{mode.upper()}**")
    except Exception as exc:  # noqa: BLE001
        await ctx.send(f"⚠️ Could not set mode: `{exc}`")


@bot.command(name="ensure", aliases=["bootstrap"])
async def ensure_cmd(ctx: commands.Context, symbol: str = "") -> None:
    sym = (symbol or "").strip().upper()
    if not sym:
        await ctx.send("Usage: !ensure <symbol>")
        return

    scheduled = _schedule_bootstrap(sym, ctx.channel, announce=True)
    if scheduled:
        await ctx.send(f"🔄 Bootstrapping **{sym}**. I'll report back here.")
    else:
        await ctx.send(f"⏳ Bootstrap already running for **{sym}**.")


@bot.command(name="watchlist")
async def watchlist_cmd(ctx: commands.Context) -> None:
    entries = _fetch_watchlist_entries()
    if not entries:
        symbols = _get_watchlist()
        if not symbols:
            await ctx.send("⚠️ Watchlist empty.")
            return
        await ctx.send("Watchlist: " + ", ".join(symbols))
        return

    lines: list[str] = ["📋 **Watchlist**"]
    for entry in entries[:20]:
        symbol = str(entry.get("symbol") or "?").upper()
        ready = entry.get("last_ready_ts") or "n/a"
        attempt = entry.get("last_attempt_ts") or "n/a"
        error = entry.get("last_error")
        line = f"• {symbol}: ready {ready} | attempt {attempt}"
        if error:
            err_preview = str(error)[:120]
            line += f" | error {err_preview}"
        lines.append(line)

    await ctx.send("\n".join(lines))


@bot.command(name="playbook")
async def playbook_cmd(ctx: commands.Context) -> None:
    symbols = MONDAY_PLAYBOOK_SYMBOLS or ["SPY", "QQQ", "IWM"]
    await ctx.send(format_monday_playbook(symbols))


@bot.command(name="daily")
async def daily_cmd(ctx: commands.Context) -> None:
    await ctx.send(format_daily_summary(DAILY_SUMMARY_SYMBOLS))


@bot.command(name="status")
async def status_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    sym = (symbol or "SPY").upper()

    mode_setting = get_analysis_mode()

    def latest_ts(s: str, tf: str) -> Optional[str]:
        db_path = os.getenv("DB_PATH", "db/tnt.db")
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            row = cur.execute("select max(ts) from prices where symbol=? and tf=?", (s, tf)).fetchone()
        return row[0] if row else None

    bias_tf = os.getenv("BIAS_TF", "5m")
    latest_1m = latest_ts(sym, "1m")
    latest_bias = latest_ts(sym, bias_tf)

    confirm_bias, confirm_note, _ = vix_sqqq_confirmation(tf=bias_tf)
    v = get_vix_context("1m")
    vix_level = float(v["level"]) if v and "level" in v else None
    gate_mode = vix_gating_action(vix_level)["mode"] if vix_level is not None else "OK"

    sig = get_latest_signal(sym)
    prob_up = float(sig["prob_up"]) if sig else None
    reasons, next_check = explain_no_trade(
        sym,
        prob_up,
        gate_mode,
        confirm_bias,
        confirm_note,
        mode=mode_setting,
    )

    lines: list[str] = []
    lines.append(f"🧪 **TNT Status: {sym}**")
    lines.append(f"• latest 1m ts: {latest_1m}")
    lines.append(f"• latest {bias_tf} ts: {latest_bias}")
    lines.append(render_directional_confirmation_block(tf=bias_tf))
    lines.append(f"• VIX gate: **{gate_mode}**")
    lines.append("")
    lines.append("Signal:")
    if sig and prob_up is not None:
        lines.append(f"• prob_up: {prob_up:.3f} | model `{sig['model_version']}` | ts {sig['ts']}")
    else:
        lines.append("• (none)")
    lines.append("")
    lines.append("Reasons / Next:")
    for reason in reasons[:4]:
        lines.append(f"• {reason}")
    lines.append(f"• {next_check}")
    lines.append("_Not financial advice._")

    await ctx.send("\n".join(lines))


@bot.command(name="brief")
async def brief_cmd(ctx: commands.Context) -> None:
    await ctx.send(format_morning_brief())


def build_opt_payload(sym: str) -> tuple[Optional[dict], Optional[str]]:
    sig = get_latest_signal(sym)
    if not sig:
        return None, f"🧩 **{sym} Options**\n• No signal found yet."

    dp = get_latest_daily_pivots(sym)
    if not dp or "piv" not in dp:
        return None, f"🧩 **{sym} Options**\n• No daily pivots found yet."

    meta = safe_json_loads(sig.get("meta"))

    prob_up = float(sig["prob_up"])
    edge = edge_from_prob(prob_up)
    conviction = conviction_from_edge(edge)
    bias = bias_from_prob(prob_up)

    now_et = datetime.now(ET)
    session = market_session_et(now_et)
    macro_events = load_macro_events_for_date(now_et.strftime("%Y-%m-%d"))
    macro_risk = macro_within_minutes(macro_events, 60, now_et)
    earnings_risk = earnings_within_window(sym, now_et)
    v = get_vix_context("1m")
    gate = {"mode": "OK", "reason": "", "mult": 1.0}
    vix_level = None
    if v and "level" in v:
        try:
            vix_level = float(v["level"])
            gate = vix_gating_action(vix_level)
        except Exception:  # noqa: BLE001
            vix_level = None

    piv = dp["piv"]
    P = float(piv["P"])
    R1 = float(piv["R1"])
    S1 = float(piv["S1"])
    R2 = float(piv.get("R2", R1))
    S2 = float(piv.get("S2", S1))

    no_trade: list[str] = []
    if gate["mode"] == "HARD":
        no_trade.append("VIX gate is HARD (vol stress) → stand down.")
    if conviction == "LOW":
        no_trade.append(f"Low conviction (edge {edge:.3f}) → wait for confirmation / reduce frequency.")

    price_ts = meta.get("price_ts") or meta.get("last_price_ts") or ""
    em_points = meta.get("em_points")
    regime = meta.get("regime") or "unknown"
    distP = meta.get("dist_to_P")
    distR1 = meta.get("dist_to_R1")
    distS1 = meta.get("dist_to_S1")

    score = setup_score(prob_up, str(gate.get("mode", "")).upper(), conviction)

    payload = {
        "sym": sym,
        "sig": sig,
        "prob_up": prob_up,
        "edge": edge,
        "conviction": conviction,
        "bias": bias,
        "session": session,
        "gate": gate,
        "vix_level": vix_level,
        "piv": {"P": P, "R1": R1, "S1": S1, "R2": R2, "S2": S2},
        "meta": meta,
        "price_ts": price_ts,
        "em_points": em_points,
        "regime": regime,
        "dists": {"P": distP, "R1": distR1, "S1": distS1},
        "no_trade": no_trade,
        "setup_score": score,
        "macro_risk": macro_risk,
        "earnings_risk": earnings_risk,
    }
    return payload, None


def render_opt_clean(p: dict) -> str:
    sym = p["sym"]
    piv = p["piv"]
    gate_mode = p["gate"].get("mode", "OK")
    vix_level = p.get("vix_level")
    prob_up = p["prob_up"]
    edge = p["edge"]
    bias = p["bias"]
    conv = p["conviction"]
    sig = p["sig"]
    price_ts = p.get("price_ts")
    session = p.get("session", "unknown")
    badge = freshness_badge(price_ts, session)

    P, R1, S1, R2, S2 = (piv[key] for key in ("P", "R1", "S1", "R2", "S2"))

    lines: list[str] = []
    lines.append(f"🎯 **{sym} Options Toolkit (0DTE + Weeklies)**")
    lines.append(f"Signal ts: {sig.get('ts', 'n/a')} | model `{sig.get('model_version', 'n/a')}`")
    lines.append(freshness_badge(p.get("price_ts", ""), p.get("session", "unknown")))
    if price_ts:
        lines.append(f"Price ts: {price_ts}")
    status_line = f"{badge} | Session: {session}"
    if session in {"CLOSED", "WEEKEND"}:
        status_line += " | Levels only (market closed)"
    lines.append(status_line)
    lines.append(
        "Prob↑ **{up}** | Prob↓ **{down}** | Edge **{edge}** | Conviction **{conv}**".format(
            up=fmt_num(prob_up, 3),
            down=fmt_num(1 - prob_up, 3),
            edge=fmt_num(edge, 3),
            conv=conv,
        )
    )
    score_val = setup_score(prob_up, gate_mode, conv)
    lines.append(f"Setup score: **{score_val}/100**")
    macro_risk = p.get("macro_risk")
    if macro_risk:
        lines.append(
            "⚠️ Macro risk: HIGH impact {title} at {time} ET (in {delta:.0f}m)".format(
                title=macro_risk.get("title", "event"),
                time=macro_risk.get("time_et", "?"),
                delta=float(macro_risk.get("delta_min", 0.0)),
            )
        )
    earnings_risk = p.get("earnings_risk")
    if earnings_risk:
        event_dt = earnings_risk.get("event_dt")
        if isinstance(event_dt, datetime):
            event_str = event_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        else:
            event_str = "soon"
        lines.append(
            "⚠️ Earnings risk: {sym} {window} ({when}) — {ts}".format(
                sym=sym,
                window=earnings_risk.get("window", "soon"),
                when=earnings_risk.get("when", ""),
                ts=event_str,
            )
        )
    if prob_up > 0.5:
        lean = "slight BULL lean"
    elif prob_up < 0.5:
        lean = "slight BEAR lean"
    else:
        lean = "flat"
    bias_line = f"Bias: **{bias}** ({lean}) | VIX mode: **{gate_mode}**"
    if vix_level is not None:
        bias_line += f" | VIX {fmt_num(vix_level)}"
    lines.append(bias_line)

    if BIAS_CONFIRM_ENABLED:
        lines.append("")
        lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))

    lines.append("")
    lines.append("**Key levels (RTH pivots)**")
    lines.append("• P **{P}** | R1 **{R1}** | S1 **{S1}**".format(P=fmt_num(P), R1=fmt_num(R1), S1=fmt_num(S1)))

    lines.append("")
    lines.append("🟦 **PLAYBOOK 1 — TREND (break + retest)**")
    lines.append(
        "• Bull trigger: reclaim/hold above **P {P}** → target **R1 {R1}** (then R2 {R2})".format(
            P=fmt_num(P), R1=fmt_num(R1), R2=fmt_num(R2)
        )
    )
    lines.append(
        "• Bear trigger: lose/hold below **P {P}** → target **S1 {S1}** (then S2 {S2})".format(
            P=fmt_num(P), S1=fmt_num(S1), S2=fmt_num(S2)
        )
    )
    lines.append("• Confirmation: break + retest (avoid first spike).")
    lines.append(
        "• Risk: defined-risk only; invalidation = clean return back through P; add a time-stop (20–45m)."
    )
    lines.append("• Structures:")
    lines.append("  - 0DTE: debit spread (call spread if bull / put spread if bear)")
    lines.append("  - Weeklies: debit spread or diagonal (more time, less decay)")

    lines.append("")
    lines.append("🟨 **PLAYBOOK 2 — RANGE (only if chop holds)**")
    lines.append(
        "• Range zone: **S1 {S1}** ↔ **R1 {R1}** (P as magnet).".format(
            S1=fmt_num(S1), R1=fmt_num(R1)
        )
    )
    lines.append("• Only trade edges (S1/P/R1 reactions); avoid mid-range noise.")
    lines.append("• No-trade switch: strong closes outside S1/R1 → revert to TREND playbook.")
    lines.append("• Structures:")
    lines.append("  - 0DTE: small defined-risk only (experienced traders); avoid if momentum is expanding")
    lines.append("  - Weeklies: calendars/diagonals if expecting continued chop + IV shift")

    if p.get("no_trade"):
        lines.append("")
        lines.append("🚫 **Caution / No-trade flags**")
        for note in p["no_trade"][:4]:
            lines.append(f"• {note}")

    lines.append("")
    lines.append("_Not financial advice. No strikes/entries/sizing provided._")
    return clamp_lines(lines, OPT_MAX_LINES)


def render_opt_pro(p: dict) -> str:
    sym = p["sym"]
    piv = p["piv"]
    meta = p.get("meta", {})
    sig = p["sig"]
    session = p.get("session", "unknown")
    badge = freshness_badge(p.get("price_ts"), session)

    lines: list[str] = []
    lines.append(f"🧠 **{sym} OPT+ (Dense Pro)**")
    lines.append(
        "session={session} vix_mode={mode} vix={vix}".format(
            session=p.get("session", "n/a"),
            mode=p["gate"].get("mode", "OK"),
            vix=fmt_num(p.get("vix_level")),
        )
    )
    levels_note = " | Levels only (market closed)" if session in {"CLOSED", "WEEKEND"} else ""
    lines.append(f"{badge}{levels_note}")
    lines.append(
        "signal_ts={ts} model={model} prob_up={prob_up} edge={edge} conv={conv} bias={bias}".format(
            ts=sig.get("ts", "n/a"),
            model=sig.get("model_version", "n/a"),
            prob_up=fmt_num(p["prob_up"], 4),
            edge=fmt_num(p["edge"], 4),
            conv=p.get("conviction", "n/a"),
            bias=p.get("bias", "n/a"),
        )
    )
    if p["prob_up"] > 0.5:
        lean = "slight_BULL"
    elif p["prob_up"] < 0.5:
        lean = "slight_BEAR"
    else:
        lean = "flat"
    lines.append(f"lean={lean}")
    if BIAS_CONFIRM_ENABLED:
        lines.append("")
        lines.append(render_directional_confirmation_block(tf=os.getenv("BIAS_TF", "5m")))
    macro_risk = p.get("macro_risk")
    if macro_risk:
        lines.append(
            "macro_risk=HIGH {title} @{time}ET ({delta:.0f}m)".format(
                title=macro_risk.get("title", "event"),
                time=macro_risk.get("time_et", "?"),
                delta=float(macro_risk.get("delta_min", 0.0)),
            )
        )
    earnings_risk = p.get("earnings_risk")
    if earnings_risk:
        event_dt = earnings_risk.get("event_dt")
        if isinstance(event_dt, datetime):
            event_str = event_dt.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        else:
            event_str = "soon"
        lines.append(
            "earnings_risk={sym} {window} ({when}) @ {ts}".format(
                sym=sym,
                window=earnings_risk.get("window", "soon"),
                when=earnings_risk.get("when", ""),
                ts=event_str,
            )
        )
    score_val = setup_score(p["prob_up"], p["gate"].get("mode", "OK"), p.get("conviction", "n/a"))
    lines.append(f"setup_score={score_val}/100")
    lines.append(
        "pivots: P={P} R1={R1} S1={S1} R2={R2} S2={S2}".format(
            P=fmt_num(piv["P"]),
            R1=fmt_num(piv["R1"]),
            S1=fmt_num(piv["S1"]),
            R2=fmt_num(piv["R2"]),
            S2=fmt_num(piv["S2"]),
        )
    )
    if p.get("price_ts"):
        lines.append(f"price_ts={p['price_ts']}")

    lines.append("")
    lines.append("meta (selected):")
    keys = [
        "regime",
        "dist_to_P",
        "dist_to_R1",
        "dist_to_S1",
        "trend_1m",
        "ret_1m",
        "ret_5m",
        "atr_1m",
        "atr_5m",
        "vol_z",
        "em_points",
        "macro_risk",
        "earnings_risk",
    ]
    for key in keys:
        if key not in meta:
            continue
        value = meta[key]
        if isinstance(value, (int, float)):
            lines.append(f"• {key}={fmt_num(value, 3)}")
        else:
            lines.append(f"• {key}={value}")

    P, R1, S1, R2, S2 = (piv[key] for key in ("P", "R1", "S1", "R2", "S2"))

    lines.append("")
    lines.append("PLAYBOOK 1: TREND")
    lines.append(f"- bull: reclaim P({fmt_num(P)}) + hold → R1({fmt_num(R1)}) → R2({fmt_num(R2)})")
    lines.append(f"- bear: lose P({fmt_num(P)}) + hold → S1({fmt_num(S1)}) → S2({fmt_num(S2)})")
    lines.append("- confirm: break + retest; avoid first impulse candle.")
    lines.append("- invalidation: clean cross back through P after entry.")
    lines.append("- time stop: 20–45m if no follow-through.")
    lines.append("- structures: 0DTE debit spreads default; weeklies debit/diagonal.")

    lines.append("")
    lines.append("PLAYBOOK 2: RANGE")
    lines.append(f"- zone: S1({fmt_num(S1)}) ↔ R1({fmt_num(R1)}); P({fmt_num(P)}) magnet.")
    lines.append("- entries: only at edges w/ rejection; no mid-range trades.")
    lines.append("- switch: strong closes outside S1/R1 → TREND playbook.")
    lines.append("- structures: 0DTE defined-risk only; weeklies calendars/diagonals.")

    if p.get("no_trade"):
        lines.append("")
        lines.append("NO-TRADE FLAGS:")
        for note in p["no_trade"]:
            lines.append(f"- {note}")

    lines.append("")
    lines.append("note: no strikes/expiry/entries; structures + triggers only. NFA.")
    return clamp_lines(lines, OPT_PLUS_MAX_LINES)


@bot.command(name="opt")
async def opt_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    sym = symbol.upper()
    payload, err = build_opt_payload(sym)
    if err:
        await ctx.send(err)
        return
    pivots = payload.get("piv") if payload else None
    await safe_send(
        ctx.channel,
        render_opt_clean(payload),
        kind="analysis",
        symbol=sym,
        pivots=pivots,
        output_mode="strict",
    )


@bot.command(name="optplus", aliases=["optp", "opt_plus"])
async def opt_plus_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    sym = symbol.upper()
    payload, err = build_opt_payload(sym)
    if err:
        await ctx.send(err)
        return
    pivots = payload.get("piv") if payload else None
    await safe_send(
        ctx.channel,
        render_opt_pro(payload),
        kind="analysis",
        symbol=sym,
        pivots=pivots,
        output_mode="strict",
    )


@bot.listen("on_message")
async def _opt_plus_alias_router(message: discord.Message) -> None:
    if message.author.bot:
        return
    content = (message.content or "").strip()
    if not content:
        return
    lowered = content.lower()
    if not any(lowered.startswith(prefix) for prefix in ("!opt+", "!opt_plus")):
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    command = bot.get_command("optplus")
    if not command:
        return

    ctx.command = command
    ctx.invoked_with = "opt+"
    await bot.invoke(ctx)


@bot.command(name="vix")
async def vix_cmd(ctx: commands.Context) -> None:
    v = get_vix_context("1m")
    if not v:
        await ctx.send("⚠️ No VIX data found in DB yet. (Check ingest symbol mapping.)")
        return
    await ctx.send(f"🧠 VIX: **{v['level']:.2f}** ({v['regime']}) as of {v['ts']}")


@bot.command(name="bias")
async def bias_cmd(ctx: commands.Context, tf: str | None = None) -> None:
    use_tf = (tf or BIAS_TF).lower()
    if use_tf not in ("1m", "5m", "15m", "60m"):
        use_tf = BIAS_TF

    bias, note, detail = vix_sqqq_confirmation(tf=use_tf)
    icon = "🟢" if bias == "BULLISH" else ("🔴" if bias == "BEARISH" else ("🟡" if bias == "NEUTRAL" else "🟠"))

    lines: list[str] = []
    lines.append("🧭 **Bias Explainer (VIX + SQQQ)**")
    lines.append(f"TF: **{use_tf}**")
    lines.append(f"• VIX trend: **{detail.get('vix_trend', '?')}**")
    lines.append(f"• SQQQ direction: **{detail.get('sqqq_dir', '?')}**")
    lines.append(f"{icon} Bias confirmation: **{bias}**")
    lines.append(f"Reason: {note}")
    lines.append("")
    lines.append("🚫 **Do Nothing If**")
    lines.append("• Bias confirmation is NEUTRAL/UNKNOWN")
    lines.append("• Price is chopping around pivot / mixed signals persist")
    lines.append("")
    lines.append("_Not financial advice._")
    await ctx.send("\n".join(lines))


@bot.command(name="vixtrend")
async def vixtrend_cmd(ctx: commands.Context, minutes: int = 30) -> None:
    now = get_latest_close("VIX", "1m")
    if not now:
        await ctx.send("⚠️ No VIX data found in DB yet.")
        return
    now_dt = parse_iso(now["ts"])
    past = get_close_at_or_before("VIX", "1m", (now_dt - timedelta(minutes=int(minutes))).isoformat())
    if not past:
        await ctx.send(f"⚠️ Not enough VIX history to compute {minutes}m trend.")
        return
    chg = now["close"] - past["close"]
    pct = (chg / past["close"]) * 100 if past["close"] else 0.0
    direction = "UP" if chg > 0 else ("DOWN" if chg < 0 else "FLAT")
    await ctx.send(
        f"📈 VIX trend ({minutes}m): **{direction}** | {past['close']:.2f} → {now['close']:.2f} "
        f"({chg:+.2f}, {pct:+.2f}%)"
    )


def _format_signal_details(symbol: str, signal: Dict[str, object]) -> list[str]:
    ts = signal.get("ts")
    prob_up = float(signal.get("prob_up", 0.5))
    prob_down = float(signal.get("prob_down", 0.5))
    model_version = str(signal.get("model_version") or MODEL_VERSION)
    edge = float(signal.get("edge") or abs(prob_up - 0.5))
    meta_numeric: Dict[str, float] = signal.get("meta_numeric") or {}
    arrow = "↑" if prob_up >= 0.5 else "↓"
    lines = [
        f"Signal: **{symbol}** {arrow} prob_up={prob_up:.3f} prob_down={prob_down:.3f}",
        f"Model `{model_version}` | Edge {edge:.3f} | Conv {conviction_from_edge(edge)}",
        f"Signal ts: {_format_ts(ts)}",
    ]
    if meta_numeric:
        lines.append("Meta: " + " | ".join(f"{k}={v:.2f}" for k, v in meta_numeric.items()))
    vix = get_vix_context("1m")
    if vix and VIX_GATING_ENABLED:
        state, reason = vix_gating_decision(vix)
        lines.append(f"VIX {vix['level']:.2f} ({vix['regime']}) -> gate {state}")
        if reason:
            lines.append(f"Reason: {reason}")
    return lines


@bot.command(name="last")
async def last_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    symbol = symbol.upper()
    latest = get_latest_signal(symbol)
    if not latest:
        await ctx.send(f"⚠️ No signal found for {symbol}.")
        return
    price_info = get_latest_price(symbol)
    lines = _format_signal_details(symbol, latest)
    if price_info:
        price, ts = price_info
        lines.append(f"Last price: {price:.2f} @ {_format_ts(ts)}")
    await ctx.send("\n".join(lines))


@bot.command(name="pivots")
async def pivots_cmd(ctx: commands.Context, symbol: str = "SPY") -> None:
    symbol = symbol.upper()
    piv = get_latest_daily_pivots(symbol)
    if not piv:
        await ctx.send(f"⚠️ No pivots available for {symbol}.")
        return
    lines = [f"🎯 {symbol} pivots"]
    if "session" in piv:
        lines.append(f"Source session: {piv['session']} (ts={_format_ts(piv['ts'])})")
    else:
        lines.append(f"Source daily bar ts={_format_ts(piv['ts'])}")
    vals = piv["piv"]
    lines.append(
        "P {P:.2f} | R1 {R1:.2f} | R2 {R2:.2f} | R3 {R3:.2f} | "
        "S1 {S1:.2f} | S2 {S2:.2f} | S3 {S3:.2f}".format(**vals)
    )
    await ctx.send("\n".join(lines))


@bot.command(name="gating")
async def gating_cmd(ctx: commands.Context) -> None:
    vix = get_vix_context("1m")
    if not vix:
        await ctx.send("⚠️ VIX context unavailable.")
        return
    mode, reason = vix_gating_decision(vix)
    msg = f"VIX {vix['level']:.2f} ({vix['regime']}) -> gate {mode}"
    if reason:
        msg += f" | {reason}"
    await ctx.send(msg)


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("[FATAL] DISCORD_BOT_TOKEN not set")
        sys.exit(1)
    try:
        bot.run(token)
    except KeyboardInterrupt:
        print("[STOP] Keyboard interrupt, shutting down bot")


if __name__ == "__main__":
    main()
