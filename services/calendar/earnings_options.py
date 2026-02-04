from __future__ import annotations

import math
from typing import Any


def _safe_float(x: Any) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def _mid(*, bid: Any, ask: Any, last: Any = None) -> float | None:
    b = _safe_float(bid)
    a = _safe_float(ask)
    if b is not None and a is not None and a >= b and (a + b) > 0:
        return 0.5 * (a + b)
    return _safe_float(last)


def _liquidity_risk_from_spread(rel_spread: float | None) -> str:
    if rel_spread is None:
        return "HIGH"
    # Conservative: earnings-time chains can get messy; prefer warning.
    if rel_spread >= 0.30:
        return "HIGH"
    if rel_spread >= 0.15:
        return "MED"
    return "LOW"


def compute_expected_move_from_chain_df(df) -> dict[str, Any] | None:
    """Compute a best-effort expected move from an options chain DataFrame.

    Uses an ATM straddle proxy: expected_move_$ ≈ call_mid + put_mid at strike nearest underlying.

    Returns a dict with:
    - expected_move_pct
    - liquidity_risk
    - front_iv (avg of call/put IV at ATM)
    - details (debug)

    Never raises; returns None if insufficient data.
    """

    if df is None or getattr(df, "empty", True):
        return None

    try:
        import pandas as pd

        data = df.copy()
        if not isinstance(data, pd.DataFrame) or data.empty:
            return None
    except Exception:
        return None

    required = {"type", "strike"}
    if not required.issubset(set(str(c) for c in data.columns)):
        return None

    # Underlying price.
    underlying = None
    if "underlying_price" in data.columns:
        try:
            series = data["underlying_price"]
            series = __import__("pandas").to_numeric(series, errors="coerce").dropna()
            underlying = float(series.iloc[-1]) if not series.empty else None
        except Exception:
            underlying = None

    if underlying is None or not (underlying > 0):
        return None

    try:
        data["strike"] = __import__("pandas").to_numeric(data["strike"], errors="coerce")
    except Exception:
        return None

    data = data.dropna(subset=["strike"])
    if data.empty:
        return None

    # Pick the strike closest to underlying.
    try:
        data["_dist"] = (data["strike"] - float(underlying)).abs()
        best_strike = float(data.sort_values("_dist", ascending=True).iloc[0]["strike"])
    except Exception:
        return None

    sl = data[data["strike"] == best_strike].copy()
    if sl.empty:
        return None

    def _pick(side: str):
        sub = sl[sl["type"].astype(str).str.lower() == side].copy() if "type" in sl.columns else sl.iloc[0:0]
        if sub.empty:
            return None
        # Prefer row with the most OI, then volume.
        for col in ("open_interest", "volume"):
            if col in sub.columns:
                try:
                    sub[col] = __import__("pandas").to_numeric(sub[col], errors="coerce").fillna(0.0)
                except Exception:
                    pass
        sort_cols = [c for c in ("open_interest", "volume") if c in sub.columns]
        if sort_cols:
            sub = sub.sort_values(sort_cols, ascending=False)
        return sub.iloc[0].to_dict()

    call = _pick("call")
    put = _pick("put")
    if not call or not put:
        return None

    call_mid = _mid(bid=call.get("bid"), ask=call.get("ask"), last=call.get("last"))
    put_mid = _mid(bid=put.get("bid"), ask=put.get("ask"), last=put.get("last"))
    if call_mid is None or put_mid is None:
        return None

    straddle = float(call_mid) + float(put_mid)
    if not (straddle > 0):
        return None

    expected_move_pct = (straddle / float(underlying)) * 100.0

    # Liquidity/spread risk: max relative spread across the two legs.
    def _rel_spread(row: dict[str, Any]) -> float | None:
        b = _safe_float(row.get("bid"))
        a = _safe_float(row.get("ask"))
        m = _mid(bid=b, ask=a, last=row.get("last"))
        if b is None or a is None or m is None or m <= 0 or a < b:
            return None
        return float(a - b) / float(m)

    rel = None
    try:
        rs_c = _rel_spread(call)
        rs_p = _rel_spread(put)
        vals = [x for x in (rs_c, rs_p) if x is not None]
        rel = max(vals) if vals else None
    except Exception:
        rel = None

    front_iv = None
    try:
        iv_c = _safe_float(call.get("iv"))
        iv_p = _safe_float(put.get("iv"))
        vals = [x for x in (iv_c, iv_p) if x is not None]
        front_iv = float(sum(vals) / len(vals)) if vals else None
    except Exception:
        front_iv = None

    return {
        "expected_move_pct": float(expected_move_pct),
        "liquidity_risk": _liquidity_risk_from_spread(rel),
        "front_iv": front_iv,
        "details": {
            "underlying_price": float(underlying),
            "atm_strike": float(best_strike),
            "straddle_mid": float(straddle),
            "rel_spread": rel,
        },
    }


def compute_wing_skew_score(*, atm_straddle: Any, up_cost: Any, down_cost: Any) -> float | None:
    """Compute a simple wing skew score.

    score = (up_cost - down_cost) / max(atm_straddle, 0.01)

    Returns None if inputs are invalid.
    """

    s = _safe_float(atm_straddle)
    u = _safe_float(up_cost)
    d = _safe_float(down_cost)
    if s is None or u is None or d is None:
        return None
    denom = max(float(s), 0.01)
    return float((float(u) - float(d)) / denom)


def classify_wing_risk_shape(*, score: Any, threshold: float = 0.10) -> str | None:
    """Classify risk shape from wing skew score.

    - score > +threshold => UPSIDE_SKEW
    - score < -threshold => DOWNSIDE_SKEW
    - else              => BALANCED
    """

    sc = _safe_float(score)
    if sc is None:
        return None
    thr = abs(float(threshold))
    if sc > thr:
        return "UPSIDE_SKEW"
    if sc < -thr:
        return "DOWNSIDE_SKEW"
    return "BALANCED"


def risk_shape_from_expected_move_snapshot(em_snap: Any) -> dict[str, Any] | None:
    """Best-effort risk-shape extraction/derivation from an earnings expected-move snapshot.

    Supports both the new wings schema and legacy shapes if already stored.
    """

    if not isinstance(em_snap, dict) or not em_snap:
        return None

    rs = em_snap.get("risk_shape")
    if isinstance(rs, dict) and rs.get("label"):
        return rs

    # New schema: expected_move.atm + expected_move.wings
    atm = em_snap.get("atm") if isinstance(em_snap.get("atm"), dict) else {}
    wings = em_snap.get("wings") if isinstance(em_snap.get("wings"), dict) else {}
    down = wings.get("down_5") if isinstance(wings.get("down_5"), dict) else {}
    up = wings.get("up_5") if isinstance(wings.get("up_5"), dict) else {}

    call_mid_atm = _safe_float(atm.get("call_mid"))
    put_mid_atm = _safe_float(atm.get("put_mid"))
    if call_mid_atm is None or put_mid_atm is None:
        # Legacy schema might store a top-level straddle.
        s = _safe_float(em_snap.get("straddle"))
    else:
        s = float(call_mid_atm) + float(put_mid_atm)

    # Require wing mids (both call+put) to compute a meaningful skew.
    up_call = _safe_float(up.get("call_mid"))
    up_put = _safe_float(up.get("put_mid"))
    dn_call = _safe_float(down.get("call_mid"))
    dn_put = _safe_float(down.get("put_mid"))
    if up_call is None or up_put is None or dn_call is None or dn_put is None:
        return None
    up_cost = float(up_call) + float(up_put)
    down_cost = float(dn_call) + float(dn_put)

    if s is None or up_cost is None or down_cost is None:
        return None

    score = compute_wing_skew_score(atm_straddle=s, up_cost=up_cost, down_cost=down_cost)
    if score is None:
        return None
    label = classify_wing_risk_shape(score=score)
    if label is None:
        return None
    return {"label": label, "score": float(score)}
