from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _regime_key() -> str:
    try:
        from tnt_redis import rkey as _rkey

        return _rkey("macro", "regime", "v1")
    except Exception:
        return "macro:regime:v1"


def _safe_float(x: object) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, bool):
            return None
        return float(x)
    except Exception:
        return None


def _get_val(d: Dict[str, Any], keys: list[str]) -> Optional[float]:
    for k in keys:
        if k in d:
            v = _safe_float(d.get(k))
            if v is not None:
                return v
    return None


def _latest_and_prev(payload: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if not isinstance(payload, dict):
        return {}, None
    latest = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    prev = None
    hist = payload.get("history")
    if isinstance(hist, list) and len(hist) >= 2 and isinstance(hist[1], dict):
        prev = hist[1]
    return latest if isinstance(latest, dict) else {}, prev


def compute_macro_regime(
    *,
    treasury_yields: Optional[Dict[str, Any]] = None,
    inflation: Optional[Dict[str, Any]] = None,
    inflation_expectations: Optional[Dict[str, Any]] = None,
    labor_market: Optional[Dict[str, Any]] = None,
    now_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute a simple, explainable macro regime snapshot.

    This is meant to *gate posture* (risk/behavior), not forecast direction.
    """

    yv, y_prev = _latest_and_prev(treasury_yields)
    iv, i_prev = _latest_and_prev(inflation)
    ev, e_prev = _latest_and_prev(inflation_expectations)
    lv, l_prev = _latest_and_prev(labor_market)

    y2 = _get_val(yv, ["yield_2_year", "yield_2yr", "two_year", "y2", "2y", "two_year_yield"])  # type: ignore[list-item]
    y10 = _get_val(yv, ["yield_10_year", "yield_10yr", "ten_year", "y10", "10y", "ten_year_yield"])  # type: ignore[list-item]

    spread_2s10s = (y10 - y2) if (y10 is not None and y2 is not None) else None

    if y10 is None:
        rates_level = "unknown"
    elif y10 < 3.0:
        rates_level = "low"
    elif y10 < 4.5:
        rates_level = "mid"
    else:
        rates_level = "high"

    if spread_2s10s is None:
        curve_shape = "unknown"
    elif spread_2s10s < -0.10:
        curve_shape = "inverted"
    elif abs(spread_2s10s) <= 0.10:
        curve_shape = "flat"
    elif spread_2s10s >= 0.50:
        curve_shape = "steep"
    else:
        curve_shape = "normal"

    cpi_yoy = _get_val(iv, ["cpi_year_over_year", "cpi_yoy", "cpi_yoy_pct"])
    core = _get_val(iv, ["cpi_core", "core_cpi", "core_cpi_yoy", "core_cpi_year_over_year"])

    prev_cpi_yoy = _get_val(i_prev, ["cpi_year_over_year", "cpi_yoy", "cpi_yoy_pct"]) if isinstance(i_prev, dict) else None
    prev_core = _get_val(i_prev, ["cpi_core", "core_cpi", "core_cpi_yoy", "core_cpi_year_over_year"]) if isinstance(i_prev, dict) else None

    def _trend(cur: Optional[float], prev: Optional[float]) -> str:
        if cur is None or prev is None:
            return "unknown"
        d = cur - prev
        if d >= 0.10:
            return "rising"
        if d <= -0.10:
            return "falling"
        return "sticky"

    inflation_trend = _trend(cpi_yoy, prev_cpi_yoy)
    core_trend = _trend(core, prev_core)

    exp_5y5y = _get_val(ev, ["inflation_5y5y", "five_year_five_year_forward", "5y5y_forward", "five_year_five_year", "infl_5y5y"])  # type: ignore[list-item]
    prev_exp_5y5y = _get_val(e_prev, ["inflation_5y5y", "five_year_five_year_forward", "5y5y_forward", "five_year_five_year", "infl_5y5y"]) if isinstance(e_prev, dict) else None

    if exp_5y5y is None:
        infl_expectations = "unknown"
    elif exp_5y5y >= 2.8:
        infl_expectations = "unanchoring"
    elif exp_5y5y <= 2.6:
        infl_expectations = "anchored"
    else:
        infl_expectations = "watch"

    unemp = _get_val(lv, ["unemployment_rate", "unemployment", "u_rate", "unemp_rate"])
    prev_unemp = _get_val(l_prev, ["unemployment_rate", "unemployment", "u_rate", "unemp_rate"]) if isinstance(l_prev, dict) else None

    if unemp is None:
        labor_tightness = "unknown"
    elif unemp < 4.0:
        labor_tightness = "tight"
    elif unemp < 4.5:
        labor_tightness = "cooling"
    else:
        labor_tightness = "loose"

    # Simple combined score (0..6) -> LOW/MED/HIGH.
    score = 0
    if rates_level == "high":
        score += 2
    elif rates_level == "mid":
        score += 1

    if curve_shape == "inverted":
        score += 2
    elif curve_shape == "flat":
        score += 1

    if inflation_trend in {"rising", "sticky"}:
        score += 1
    if infl_expectations in {"unanchoring", "watch"}:
        score += 1

    if labor_tightness == "tight":
        score += 1

    if score >= 6:
        macro_risk = "HIGH"
    elif score >= 3:
        macro_risk = "MED"
    else:
        macro_risk = "LOW"

    posture = "NORMAL"
    if macro_risk == "HIGH":
        posture = "STAND DOWN"
    elif macro_risk == "MED":
        posture = "CAUTIOUS"

    if not now_utc:
        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "ok": True,
        "updated_utc": str(now_utc),
        "rates_level": rates_level,
        "curve_shape": curve_shape,
        "spread_2s10s": spread_2s10s,
        "inflation_trend": inflation_trend,
        "core_trend": core_trend,
        "infl_expectations": infl_expectations,
        "labor_tightness": labor_tightness,
        "macro_risk": macro_risk,
        "score": int(score),
        "posture": posture,
        "levels": {
            "yield_2y": y2,
            "yield_10y": y10,
            "cpi_yoy": cpi_yoy,
            "core_cpi": core,
            "exp_5y5y": exp_5y5y,
            "unemployment_rate": unemp,
        },
        "deltas": {
            "cpi_yoy": (cpi_yoy - prev_cpi_yoy) if (cpi_yoy is not None and prev_cpi_yoy is not None) else None,
            "core_cpi": (core - prev_core) if (core is not None and prev_core is not None) else None,
            "exp_5y5y": (exp_5y5y - prev_exp_5y5y) if (exp_5y5y is not None and prev_exp_5y5y is not None) else None,
            "unemployment_rate": (unemp - prev_unemp) if (unemp is not None and prev_unemp is not None) else None,
        },
        "notes": ["Gates posture only; not a directional forecast."],
    }


def get_cached_macro_regime(redis_client) -> Optional[Dict[str, Any]]:
    try:
        raw = redis_client.get(_regime_key())
    except Exception:
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def set_cached_macro_regime(redis_client, regime: Dict[str, Any], *, ttl_s: int = 60 * 60 * 48) -> None:
    s = json.dumps(regime, separators=(",", ":"), sort_keys=True)
    if ttl_s and ttl_s > 0:
        redis_client.setex(_regime_key(), int(ttl_s), s)
    else:
        redis_client.set(_regime_key(), s)


def compute_and_cache_macro_regime(
    redis_client,
    *,
    treasury_yields: Optional[Dict[str, Any]] = None,
    inflation: Optional[Dict[str, Any]] = None,
    inflation_expectations: Optional[Dict[str, Any]] = None,
    labor_market: Optional[Dict[str, Any]] = None,
    updated_utc: Optional[str] = None,
) -> Dict[str, Any]:
    regime = compute_macro_regime(
        treasury_yields=treasury_yields,
        inflation=inflation,
        inflation_expectations=inflation_expectations,
        labor_market=labor_market,
        now_utc=updated_utc,
    )
    try:
        set_cached_macro_regime(redis_client, regime)
    except Exception:
        pass
    return regime
