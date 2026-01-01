"""Options chain summarization for TNT_STATE.

This module intentionally keeps logic deterministic and bounded in size.
It is used to inject an options snapshot into TNT_STATE for coach answers.

Note: Unlike `delivery.market_data_adapter`, this summary may include
option contract identifiers (ticker/strike/expiration) by user request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional

import pandas as pd


@dataclass(frozen=True)
class OptionsChainSummary:
    payload: Dict[str, Any]


def _coerce_float(val: object) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:  # noqa: BLE001
        return None


def _coerce_int(val: object) -> int:
    try:
        if val is None:
            return 0
        return int(float(val))
    except Exception:  # noqa: BLE001
        return 0


def _fmt_exp(val: object) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date().isoformat()
    text = str(val).strip()
    if not text:
        return None
    # Best effort: keep YYYY-MM-DD if present.
    return text[:10]


def _row_contract(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "ticker": str(row.get("symbol") or row.get("ticker") or "").strip() or None,
        "type": str(row.get("type") or "").strip().lower() or None,
        "strike": _coerce_float(row.get("strike")),
        "expiration": _fmt_exp(row.get("expiration")),
        "bid": _coerce_float(row.get("bid")),
        "ask": _coerce_float(row.get("ask")),
        "last": _coerce_float(row.get("last")),
        "volume": _coerce_int(row.get("volume")),
        "open_interest": _coerce_int(row.get("open_interest")),
        "iv": _coerce_float(row.get("iv")),
        "delta": _coerce_float(row.get("delta")),
        "gamma": _coerce_float(row.get("gamma")),
        "theta": _coerce_float(row.get("theta")),
        "vega": _coerce_float(row.get("vega")),
    }


def _top_n(df: pd.DataFrame, *, side: str, by: str, n: int) -> list[Dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    if "type" not in df.columns:
        return []
    slice_df = df[df["type"].astype(str).str.lower() == side].copy()
    if slice_df.empty:
        return []

    metric = by
    if metric not in slice_df.columns:
        return []

    try:
        slice_df[metric] = pd.to_numeric(slice_df[metric], errors="coerce").fillna(0.0)
    except Exception:  # noqa: BLE001
        return []

    # Prefer nearby/valid strikes; if missing, still allow.
    if "strike" in slice_df.columns:
        slice_df["strike"] = pd.to_numeric(slice_df["strike"], errors="coerce")

    best = slice_df.sort_values(metric, ascending=False).head(max(int(n), 0))

    out: list[Dict[str, Any]] = []
    for _, row in best.iterrows():
        out.append(_row_contract(row.to_dict()))
    return out


def summarize_options_chain(
    df: Optional[pd.DataFrame],
    *,
    underlying: str,
    asof_et: str,
    provider: str,
    max_per_side: int = 8,
) -> OptionsChainSummary:
    """Summarize a chain into a small TNT_STATE payload.

    Includes contract identifiers (ticker/strike/expiration) by request.
    """

    sym = (underlying or "").strip().upper() or "SPY"
    if df is None or getattr(df, "empty", True):
        return OptionsChainSummary(
            payload={
                "underlying": sym,
                "asof_et": asof_et,
                "provider": provider,
                "status": "empty",
            }
        )

    data = df.copy()
    # Normalize required columns.
    if "symbol" not in data.columns and "ticker" in data.columns:
        data["symbol"] = data["ticker"]

    for col in ("volume", "open_interest"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce").fillna(0.0).astype(int)

    if "gamma" in data.columns:
        data["gamma"] = pd.to_numeric(data["gamma"], errors="coerce")

    if "strike" in data.columns:
        data["strike"] = pd.to_numeric(data["strike"], errors="coerce")

    # Underlying price (best effort).
    underlying_price = None
    if "underlying_price" in data.columns:
        try:
            series = pd.to_numeric(data["underlying_price"], errors="coerce").dropna()
            underlying_price = float(series.iloc[-1]) if not series.empty else None
        except Exception:  # noqa: BLE001
            underlying_price = None

    calls = data[data.get("type", "").astype(str).str.lower() == "call"] if "type" in data.columns else pd.DataFrame()
    puts = data[data.get("type", "").astype(str).str.lower() == "put"] if "type" in data.columns else pd.DataFrame()

    call_oi = int(calls["open_interest"].sum()) if not calls.empty and "open_interest" in calls.columns else 0
    put_oi = int(puts["open_interest"].sum()) if not puts.empty and "open_interest" in puts.columns else 0
    call_vol = int(calls["volume"].sum()) if not calls.empty and "volume" in calls.columns else 0
    put_vol = int(puts["volume"].sum()) if not puts.empty and "volume" in puts.columns else 0

    def _ratio(a: int, b: int) -> Optional[float]:
        if b <= 0:
            return None
        try:
            return float(a) / float(b)
        except Exception:  # noqa: BLE001
            return None

    # Walls (max OI per side).
    call_wall = None
    put_wall = None
    if not calls.empty and "open_interest" in calls.columns:
        best = calls.sort_values("open_interest", ascending=False).head(1)
        if not best.empty:
            call_wall = _row_contract(best.iloc[0].to_dict())
    if not puts.empty and "open_interest" in puts.columns:
        best = puts.sort_values("open_interest", ascending=False).head(1)
        if not best.empty:
            put_wall = _row_contract(best.iloc[0].to_dict())

    # Gamma peak proxy: max abs(gamma) * OI.
    gamma_peak = None
    if "gamma" in data.columns and "open_interest" in data.columns:
        try:
            data["gamma_oi"] = (data["gamma"].abs().fillna(0.0) * data["open_interest"].fillna(0.0)).astype(float)
            best = data.sort_values("gamma_oi", ascending=False).head(1)
            if not best.empty:
                row = best.iloc[0].to_dict()
                gamma_peak = _row_contract(row)
                gamma_peak["gamma_oi"] = _coerce_float(row.get("gamma_oi"))
        except Exception:  # noqa: BLE001
            gamma_peak = None

    # Expiration(s) present (keep only the most common one to stay small).
    exp = None
    if "expiration" in data.columns:
        try:
            exp_series = data["expiration"].map(_fmt_exp).dropna()
            exp = str(exp_series.mode().iloc[0]) if not exp_series.empty else None
        except Exception:  # noqa: BLE001
            exp = None

    payload: Dict[str, Any] = {
        "underlying": sym,
        "expiration": exp,
        "asof_et": asof_et,
        "provider": provider,
        "status": "ok",
        "underlying_price": underlying_price,
        "metrics": {
            "contracts_total": int(len(data)),
            "calls": int(len(calls)) if not calls.empty else 0,
            "puts": int(len(puts)) if not puts.empty else 0,
            "oi_total": int(call_oi + put_oi),
            "vol_total": int(call_vol + put_vol),
            "call_put_oi_ratio": _ratio(call_oi, put_oi),
            "call_put_vol_ratio": _ratio(call_vol, put_vol),
            "call_wall": call_wall,
            "put_wall": put_wall,
            "gamma_peak": gamma_peak,
        },
        "top": {
            "calls_by_open_interest": _top_n(data, side="call", by="open_interest", n=max_per_side),
            "puts_by_open_interest": _top_n(data, side="put", by="open_interest", n=max_per_side),
            "calls_by_volume": _top_n(data, side="call", by="volume", n=max_per_side),
            "puts_by_volume": _top_n(data, side="put", by="volume", n=max_per_side),
        },
    }

    return OptionsChainSummary(payload=payload)
