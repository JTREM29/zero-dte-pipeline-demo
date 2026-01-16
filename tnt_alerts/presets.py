from __future__ import annotations

from typing import Callable

from .models import (
    AlertIntentV1,
    AlertSource,
    AlertTargets,
    ActionIncludeChart,
    ActionNotify,
    ConditionCross,
    ConfirmSpec,
    CooldownGate,
    ExpiresEOD,
    Gates,
    IndicatorVWAP,
    Lifecycle,
    MarketHoursGate,
    SeriesPrice,
)


def preset_spy_vwap_breakout(*, user_id: str, channel_id: str, request_text: str) -> AlertIntentV1:
    return AlertIntentV1(
        source=AlertSource(user_id=user_id, channel_id=channel_id, request_text=request_text),
        targets=AlertTargets(type="symbols", symbols=["SPY"], max_symbols=20),
        condition=ConditionCross(
            left=SeriesPrice(),
            op="crosses_above",
            right=IndicatorVWAP(),
            timeframe="5m",  # type: ignore[arg-type]
            confirm=ConfirmSpec(mode="close"),
        ),
        gates=Gates(
            market_hours=MarketHoursGate(session="RTH", time_window_et=None),
            cooldown=CooldownGate(seconds=300),
            max_triggers=3,
        ),
        lifecycle=Lifecycle(expires=ExpiresEOD()),
        actions=[ActionNotify(style="compact"), ActionIncludeChart(chart="execution")],
    )


_PRESETS: dict[str, Callable[..., AlertIntentV1]] = {
    "spy_vwap_breakout": preset_spy_vwap_breakout,
}


def list_presets() -> list[str]:
    return sorted(_PRESETS.keys())


def build_preset(name: str, *, user_id: str, channel_id: str, request_text: str) -> AlertIntentV1:
    key = (name or "").strip().lower()
    fn = _PRESETS.get(key)
    if not fn:
        raise KeyError(key)
    return fn(user_id=user_id, channel_id=channel_id, request_text=request_text)
