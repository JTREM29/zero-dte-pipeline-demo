from __future__ import annotations

import pytest

from delivery.alert_embeds import build_alert_created_embed, build_alert_details_embed, build_alert_preview_embed


def _sample_intent() -> dict:
    return {
        "version": 1,
        "source": {"kind": "discord", "user_id": "discord:123", "channel_id": "456"},
        "targets": {"symbols": ["SPY"], "watchlist": None},
        "condition": {
            "type": "cross",
            "op": "crosses_above",
            "left": {"type": "price", "field": "last"},
            "right": {"type": "indicator", "name": "vwap"},
            "timeframe": "5m",
            "confirm": "close",
        },
        "gates": {
            "market_hours": {"session": "RTH"},
            "cooldown": {"seconds": 300},
            "max_triggers": {"count": 20},
        },
        "lifecycle": {
            "expires": {"type": "relative", "hours": 8},
        },
        "actions": {"discord": {"style": "compact"}},
    }


class _DummyStore:
    def next_id(self) -> str:
        return "A00000001"

    def create_alert(self, alert_id: str, intent_json: dict) -> None:
        return None

    def get_meta(self, alert_id: str) -> dict:
        return {"status": "active"}

    def pause(self, alert_id: str) -> None:
        return None

    def resume(self, alert_id: str) -> None:
        return None

    def set_alert_paused(self, alert_id: str, paused: bool) -> None:
        return None

    def update_alert(self, alert_id: str, intent_json: dict) -> None:
        return None

    def delete(self, alert_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_preview_embed_golden_shape() -> None:
    intent = _sample_intent()
    embed, view = build_alert_preview_embed(intent, warnings=[], store=_DummyStore())
    d = embed.to_dict()

    assert d.get("title") == "Alert Preview"
    assert "SPY" in (d.get("description") or "")
    assert "VWAP" in (d.get("description") or "")

    fields = d.get("fields") or []
    names = [f.get("name") for f in fields]
    assert "Rule" in names
    assert "Active window" in names
    assert "Guards" in names

    # Buttons + Bias dropdown exist.
    assert len(getattr(view, "children", [])) == 4


@pytest.mark.asyncio
async def test_created_embed_golden_shape() -> None:
    intent = _sample_intent()
    embed, view = build_alert_created_embed("A00000001", intent, store=_DummyStore())
    d = embed.to_dict()

    assert d.get("title") == "✅ Alert created — A00000001"
    assert "Watching:" in (d.get("description") or "")
    assert "SPY" in (d.get("description") or "")

    fields = d.get("fields") or []
    names = [f.get("name") for f in fields]
    assert "Status" in names

    # Buttons + Bias dropdown exist.
    assert len(getattr(view, "children", [])) == 5


def test_details_embed_golden_shape() -> None:
    intent = _sample_intent()
    embed = build_alert_details_embed("A00000001", intent, meta={"status": "active"})
    d = embed.to_dict()

    assert d.get("title") == "Details — A00000001"
    fields = d.get("fields") or []
    names = [f.get("name") for f in fields]
    assert "Condition" in names
    assert "Gates" in names
    assert "Redis" in names
