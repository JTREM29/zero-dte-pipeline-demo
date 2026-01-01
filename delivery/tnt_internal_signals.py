"""Internal signals (indicator logic) -> policy outputs.

Core rule:
- Indicators may be used internally to *decide whether* action is permitted.
- Indicators must NOT be shown to users (no RSI/MACD/EMA/VWAP numbers/plots).

This module defines the *exact* internal signal thresholds, but returns only:
- gates
- permissions
- directive labels

No trading advice is produced; only behavior constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Literal, Optional, Sequence

from analysis.indicators import macd as _macd
from analysis.indicators import rsi as _rsi


Aggression = Literal["PERMITTED", "REDUCED", "PROHIBITED"]
MomentumSide = Literal["BULL", "BEAR"]


@dataclass(frozen=True)
class InternalSignals:
    rsi_14: Optional[float]
    macd_hist: Optional[float]
    macd_hist_pct: Optional[float]


@dataclass(frozen=True)
class PolicyOutputs:
    aggression: Aggression
    no_trade: bool
    momentum_only: bool
    gates: tuple[str, ...]
    directive: str


def _safe_pct(numer: Optional[float], denom: Optional[float]) -> Optional[float]:
    if numer is None or denom is None:
        return None
    try:
        if denom == 0:
            return None
        return float(numer) / float(denom) * 100.0
    except Exception:  # noqa: BLE001
        return None


def compute_internal_signals(
    closes: Sequence[float],
    *,
    last_price: Optional[float] = None,
) -> InternalSignals:
    """Compute internal indicator signals from close series.

    - RSI(14) using simple average gains/losses.
    - MACD histogram (12,26,9) using simple EMA.

    Returns raw internal values for downstream gating (not for display).
    """

    values = [float(x) for x in closes if x is not None]
    rsi_14 = _rsi(values, 14)
    _macd_val, _sig, hist = _macd(values, 12, 26, 9)
    hist_pct = _safe_pct(abs(hist) if hist is not None else None, last_price)
    return InternalSignals(rsi_14=rsi_14, macd_hist=hist, macd_hist_pct=hist_pct)


def derive_policy_outputs(
    signals: InternalSignals,
    *,
    range_state: Optional[str] = None,
    range_ratio: Optional[float] = None,
) -> PolicyOutputs:
    """Convert internal signals into explicit gates/permissions.

    Exact thresholds (v1):
    - Countertrend risk gate:
      - RSI <= 30 or RSI >= 70

    - Compression gate (NO-TRADE):
      - range_state == 'compressing'
      - AND 45 <= RSI <= 55
      - AND MACD histogram magnitude small vs price: macd_hist_pct <= 0.02
        (i.e., <= 0.02% of price)

    - Momentum confirmation:
      - Bull momentum: RSI >= 55 AND MACD hist > 0 AND macd_hist_pct >= 0.02
      - Bear momentum: RSI <= 45 AND MACD hist < 0 AND macd_hist_pct >= 0.02

    Outputs:
    - aggression: PERMITTED/REDUCED/PROHIBITED
    - no_trade: True/False
    - momentum_only: True/False
    - gates: explicit policy gates
    """

    gates: list[str] = []

    rsi_14 = signals.rsi_14
    hist = signals.macd_hist
    hist_pct = signals.macd_hist_pct

    countertrend_risk = rsi_14 is not None and (rsi_14 <= 30.0 or rsi_14 >= 70.0)
    if countertrend_risk:
        gates.append("COUNTERTREND_RISK")

    compression = (
        (range_state or "").lower() == "compressing"
        and rsi_14 is not None
        and 45.0 <= rsi_14 <= 55.0
        and hist is not None
        and hist_pct is not None
        and hist_pct <= 0.02
    )
    if compression:
        gates.append("COMPRESSION")

    momentum_bull = (
        rsi_14 is not None
        and hist is not None
        and hist_pct is not None
        and rsi_14 >= 55.0
        and hist > 0
        and hist_pct >= 0.02
    )
    momentum_bear = (
        rsi_14 is not None
        and hist is not None
        and hist_pct is not None
        and rsi_14 <= 45.0
        and hist < 0
        and hist_pct >= 0.02
    )
    momentum_confirmed = momentum_bull or momentum_bear

    if compression:
        return PolicyOutputs(
            aggression="PROHIBITED",
            no_trade=True,
            momentum_only=True,
            gates=tuple(gates),
            directive="AGGRESSION PROHIBITED",
        )

    if momentum_confirmed:
        # Momentum confirmed: allow activity, but only with continuation-style behavior.
        return PolicyOutputs(
            aggression="PERMITTED",
            no_trade=False,
            momentum_only=True,
            gates=tuple(gates),
            directive="AGGRESSION PERMITTED (momentum-only)",
        )

    # Default: reduce activity; if countertrend risk is present, reduce further.
    if countertrend_risk:
        return PolicyOutputs(
            aggression="REDUCED",
            no_trade=False,
            momentum_only=True,
            gates=tuple(gates),
            directive="AGGRESSION REDUCED",
        )

    # Range_ratio is optional; if it suggests compression even without full criteria, reduce.
    if isinstance(range_ratio, (int, float)) and range_ratio <= 0.85:
        gates.append("TIGHT_RANGE")
        return PolicyOutputs(
            aggression="REDUCED",
            no_trade=False,
            momentum_only=True,
            gates=tuple(gates),
            directive="AGGRESSION REDUCED",
        )

    return PolicyOutputs(
        aggression="REDUCED",
        no_trade=False,
        momentum_only=False,
        gates=tuple(gates),
        directive="AGGRESSION REDUCED",
    )


def format_policy_lines(
    *,
    directive: str,
    gates: Iterable[str],
    momentum_only: bool,
) -> list[str]:
    gate_list = [g for g in gates if g]
    gates_text = ", ".join(gate_list) if gate_list else "NONE"
    lines = [f"• Permission: {directive}", f"• Gates: {gates_text}"]
    if momentum_only:
        lines.append("• Constraint: MOMENTUM-ONLY (no countertrend attempts)")
    return lines
