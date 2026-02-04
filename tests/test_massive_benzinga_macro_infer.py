from __future__ import annotations

from datetime import datetime, timezone

from services.calendar.massive_benzinga_macro import infer_macro_type, _infer_impact, _infer_ts_utc


def test_infer_macro_type_core_events() -> None:
    assert infer_macro_type("CPI") == "CPI"
    assert infer_macro_type("Consumer Price Index") == "CPI"
    assert infer_macro_type("FOMC Rate Decision") == "FOMC"
    assert infer_macro_type("Nonfarm Payrolls") == "NFP"
    assert infer_macro_type("PCE Price Index") == "PCE"


def test_infer_impact_normalization() -> None:
    assert _infer_impact("HIGH") == "HIGH"
    assert _infer_impact("low") == "LOW"
    assert _infer_impact("medium") == "MED"
    assert _infer_impact(3) == "HIGH"
    assert _infer_impact(1) == "LOW"


def test_infer_ts_utc_prefers_explicit_iso() -> None:
    item = {"ts_utc": "2026-01-29T13:30:00+00:00"}
    dt = _infer_ts_utc(item)
    assert isinstance(dt, datetime)
    assert dt.tzinfo is not None
    assert dt.astimezone(timezone.utc).isoformat().startswith("2026-01-29T13:30:00")

