from __future__ import annotations

import math


def fmt_px(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.2f}"


def fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def fmt_chg(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:,.2f}"


def score_word(v: float) -> str:
    a = abs(v)
    if a >= 0.75:
        strength = "strong"
    elif a >= 0.45:
        strength = "moderate"
    elif a >= 0.20:
        strength = "light"
    else:
        strength = "flat"

    if v > 0.10:
        return f"{strength} up"
    if v < -0.10:
        return f"{strength} down"
    return "flat"


def arrow(pct: float) -> str:
    return "▲" if pct >= 0 else "▼"
