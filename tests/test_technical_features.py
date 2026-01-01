import pandas as pd

from zero_dte_pipeline.tech import technical_features as tf
from zero_dte_pipeline.tech import contracts


def _make_df(periods: int, minutes: int) -> pd.DataFrame:
    ts = pd.date_range("2024-01-01", periods=periods, freq=f"{minutes}min", tz="UTC")
    base = pd.Series(range(periods), dtype=float) + 100.0
    data = pd.DataFrame(
        {
            "ts": ts,
            "o": base,
            "h": base + 1.0,
            "l": base - 1.0,
            "c": base + 0.5,
            "v": 1_000.0,
        }
    )
    return data


def test_compute_timeframe_state_emas_present() -> None:
    df = _make_df(periods=200, minutes=1)
    state = tf.compute_timeframe_state(df, "1m", include_vwap=True)
    ema_map = state.get("ema")
    assert isinstance(ema_map, dict)
    for key in ("9", "20", "50"):
        assert key in ema_map
        assert isinstance(ema_map[key], float)


def test_context_has_indicator_detects_ema() -> None:
    bars = {
        "1m": _make_df(periods=200, minutes=1),
        "60m": _make_df(periods=200, minutes=60),
    }
    tech_state = tf.build_technical_state("AMD", bars)
    context = {"technical_state": {"AMD": tech_state}}
    assert contracts._context_has_indicator(context, "ema")
