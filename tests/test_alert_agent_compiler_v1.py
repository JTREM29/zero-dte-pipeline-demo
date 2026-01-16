from __future__ import annotations

import pytest

from tnt_alerts.compiler import compile_request
from tnt_alerts.pretty import intent_to_dsl


def _compile(text: str):
    out = compile_request(request_text=text, user_id="discord:123", channel_id="discord:456")
    dsls = [intent_to_dsl(i) for i in out.intents]
    return out, dsls


def test_01_simple_vwap_cross() -> None:
    out, dsls = _compile("Notify me when SPY crosses above VWAP on 5m")
    assert len(out.intents) == 1
    assert dsls[0].startswith("SPY WHEN crosses_above(price,vwap)")
    assert " ON 5m" in dsls[0]
    assert " DURING RTH" in dsls[0]


def test_02_multi_symbol_vwap_cross() -> None:
    out, dsls = _compile("Ping me when SPY, QQQ, IWM cross below VWAP 5m")
    assert len(out.intents) == 1
    assert dsls[0].startswith("SPY,QQQ,IWM WHEN crosses_below(price,vwap)")


def test_03_yesterday_high_break() -> None:
    out, dsls = _compile("Alert when QQQ breaks yesterday high on 5m")
    assert "QQQ WHEN breaks_above(y_high)" in dsls[0]


def test_04_yesterday_low_break_with_cooldown_defaults() -> None:
    out, dsls = _compile("QQQ alert if it breaks yesterday low; don't spam me")
    assert "QQQ WHEN breaks_below(y_low)" in dsls[0]
    assert "cooldown" in dsls[0]
    assert "max_triggers" in dsls[0]


def test_05_daily_sma_cross_defaults_to_1d_and_30d() -> None:
    out, dsls = _compile("Notify me when SPY crosses below its 200-day SMA")
    assert "SPY WHEN crosses_below(price,sma(200))" in dsls[0]
    assert " ON 1D" in dsls[0]
    assert " UNTIL 30d" in dsls[0]


def test_06_opening_range_high_break_infers_1m() -> None:
    out, dsls = _compile("Alert when SPY breaks the 15-minute opening range high")
    assert "SPY WHEN breaks_above(or_high(15))" in dsls[0]
    assert " ON 1m" in dsls[0]


def test_07_or_low_with_time_window() -> None:
    out, dsls = _compile("Between 10:00 and 11:30, alert if SPY breaks opening range low")
    assert "DURING 10:00-11:30 ET" in dsls[0]


def test_08_touch_pivot_intrabar_confirm() -> None:
    out, dsls = _compile("Let me know if SPY touches R1 today")
    assert "touches(price,pivot(R1))" in dsls[0]
    assert "CONFIRM intrabar" in dsls[0]


def test_09_watchlist_new_10_day_low() -> None:
    out, dsls = _compile("watchlist:PORTFOLIO when new_low(10d) on 1D during RTH until 14d")
    assert dsls[0].startswith("watchlist:PORTFOLIO WHEN new_low(10d)")


def test_10_rvol_spike() -> None:
    out, dsls = _compile("Alert if TSLA has RVOL over 2 today on 15m")
    assert "TSLA WHEN rvol(20) > 2" in dsls[0]
    assert " ON 15m" in dsls[0]


def test_11_regime_gate_bullish_only() -> None:
    out, dsls = _compile("Alert when SPY breaks VWAP on 5m but only in bullish regime")
    assert "regime IN {BULLISH}" in dsls[0]


def test_12_confidence_threshold() -> None:
    out, dsls = _compile("Notify only if confidence is above 0.7 when QQQ breaks yesterday high")
    assert "confidence >= 0.70" in dsls[0]


def test_13_exclude_transition() -> None:
    out, dsls = _compile("SPY VWAP cross alerts, but not during transition")
    assert "regime IN {BULLISH,NEUTRAL,BEARISH}" in dsls[0]


def test_14_multiple_actions() -> None:
    out, dsls = _compile("When IWM breaks yesterday low on 5m, ping and include chart")
    assert "THEN notify(compact) AND include_chart(execution)" in dsls[0]


def test_15_expiry_relative_days() -> None:
    out, dsls = _compile("Set this for the next 3 days: SPY cross above VWAP on 5m")
    assert " UNTIL 3d" in dsls[0]


def test_16_expiry_date() -> None:
    out, dsls = _compile("Alert until 2026-02-01 when SPY crosses below 200 SMA")
    assert " UNTIL 2026-02-01" in dsls[0]


def test_17_missing_timeframe_infers_5m() -> None:
    out, dsls = _compile("Alert when SPY breaks VWAP")
    assert " ON 5m" in dsls[0]


def test_18_ambiguous_either_way_creates_two_intents() -> None:
    out, dsls = _compile("Alert when SPY breaks VWAP either way")
    assert len(out.intents) == 2
    assert any("crosses_above(price,vwap)" in d for d in dsls)
    assert any("crosses_below(price,vwap)" in d for d in dsls)


def test_19_price_staleness_gate() -> None:
    out, dsls = _compile("Only alert if data is fresh — SPY VWAP cross 5m")
    assert "price_age <= 60s" in dsls[0]


def test_20_too_many_symbols_rejects() -> None:
    many = ",".join([f"SYM{i}" for i in range(30)])
    with pytest.raises(Exception) as exc:
        _compile(f"Create this alert for {many} when SPY crosses above VWAP on 5m")
    # We raise a typed validation error with code
    assert "too many symbols" in str(exc.value).lower() or "ERR_TOO_MANY_SYMBOLS" in str(exc.value)
