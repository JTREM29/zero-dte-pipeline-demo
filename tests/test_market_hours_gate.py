from __future__ import annotations

from datetime import datetime, timezone

from tnt_alerts.eval_engine import is_in_custom_window_et, is_in_rth


def _dt(iso_utc: str) -> datetime:
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_is_in_rth_true_during_rth_weekday() -> None:
    # 2026-01-22 is a Thursday; ET is UTC-5.
    assert is_in_rth(_dt("2026-01-22T15:00:00Z")) is True  # 10:00 ET


def test_is_in_rth_false_before_open_and_after_close() -> None:
    assert is_in_rth(_dt("2026-01-22T14:00:00Z")) is False  # 09:00 ET
    assert is_in_rth(_dt("2026-01-22T21:00:00Z")) is False  # 16:00 ET


def test_is_in_rth_false_on_weekend() -> None:
    # Saturday
    assert is_in_rth(_dt("2026-01-24T15:00:00Z")) is False


def test_custom_window_et_inclusive_start_exclusive_end() -> None:
    # Window 09:35-09:40 ET => 14:35-14:40 UTC in winter.
    assert is_in_custom_window_et(_dt("2026-01-22T14:34:00Z"), ["09:35", "09:40"]) is False
    assert is_in_custom_window_et(_dt("2026-01-22T14:35:00Z"), ["09:35", "09:40"]) is True
    assert is_in_custom_window_et(_dt("2026-01-22T14:39:00Z"), ["09:35", "09:40"]) is True
    assert is_in_custom_window_et(_dt("2026-01-22T14:40:00Z"), ["09:35", "09:40"]) is False
