def make_signal_payload(symbol: str = "SPY", *, no_trade: bool = False) -> dict:
    sym = (symbol or "SPY").strip().upper() or "SPY"
    return {
        # Core fields used by Numeric Bias Pack
        "bias": "NEUTRAL",
        "bias_confirm": "MIXED",
        "conviction": "MED",
        "edge": 0.01,
        "regime": "RANGE",
        "session": "RTH",
        "pivot": 499.0,
        "r1": 505.0,
        "s1": 493.0,
        "tnt": {
            "regime": "RANGE",
            "posture": "NEUTRAL",
            "confidence": 0.55,
            "permissions": {"no_trade": bool(no_trade)},
        },
        # Optional fields the pack may surface when present
        "price_mode": "LIVE",
        "price_age_minutes": 0.5,
        "data_quality": {"technical_state": "FRESH"},
        # Helpful for downstream renderers
        "symbol": sym,
    }
