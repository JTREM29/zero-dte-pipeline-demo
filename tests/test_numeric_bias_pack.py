from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from delivery.discord_bot import validate_analysis_message
from delivery.numeric_bias_pack import (
    format_numeric_bias_pack_from_analysis_payload,
    format_numeric_bias_pack_from_signal_payload,
)


def test_numeric_bias_pack_from_signal_payload_smoke() -> None:
    pack = format_numeric_bias_pack_from_signal_payload(
        symbol="SPY",
        signal_payload={
            "bias": "BULL",
            "bias_confirm": "MIXED",
            "conviction": "MED",
            "edge": 0.045,
            "regime": "RANGE",
            "session": "RTH",
            "tnt": {"regime": "RANGE", "posture": "NEUTRAL", "confidence": 0.72},
            "data_quality": {"technical_state": "FRESH"},
            "price_mode": "LIVE",
            "price_age_minutes": 0.6,
        },
    )
    assert "Numeric Bias Pack" in pack
    assert "Bias" in pack
    assert "Net" in pack


def test_numeric_bias_pack_from_analysis_payload_smoke() -> None:
    payload = {
        "type": "on_demand_analyze",
        "symbol": "SPY",
        "meta": {
            "data_quality": {"technical_state": "FRESH"},
            "signal_payload": {
                "bias": "BEAR",
                "bias_confirm": "BEARISH",
                "conviction": "HIGH",
                "edge": 0.08,
                "regime": "VOLATILE",
                "session": "RTH",
                "tnt": {"regime": "VOLATILE", "posture": "BEARISH", "confidence": 0.91},
            },
            "price_context": {"mode": "LIVE", "age_minutes": 0.2},
        },
    }

    pack = format_numeric_bias_pack_from_analysis_payload(analysis_payload=payload)
    assert pack is not None
    assert "Numeric Bias Pack" in pack


def test_validate_analysis_message_still_accepts_on_demand_with_bias_pack() -> None:
    # Bias pack should not break validation (last price + pivot must still parse).
    text = """🔍 **On-Demand Analyze** — **SPY**

🧮 **Numeric Bias Pack** — **SPY**
• Bias **BULL** | Net **+0.050** | Conv **MED** | Confirm **MIXED**
• Regime **RANGE** | TNT **RANGE/NEUTRAL** | Conf **0.72** | Data **FRESH** | Px LIVE | age 0.6m | RTH

🧾 Data Mode: OPEN (live snapshot)
💵 **Last Price**
• SPY: 500.25 (live snapshot | 2025-01-01 10:00 ET | OPEN)

📐 **Pivots (RTH)**
• R1 505.00 | P 499.00 | S1 493.00

🔕 TNT STATUS
Market conditions unstable / low-confidence.
No Trade Context issued.
"""

    ok, reason = validate_analysis_message(text, label="analyze_spy")
    assert ok, reason


@dataclass
class _FakePriceSnapshot:
    ok: bool = True
    reason: str | None = None


class _FakeRenderedPost:
    def __init__(self, *, text: str, agent_payload: dict) -> None:
        self.text = text
        self.agent_payload = agent_payload


class _FakeDeliveryModule:
    RenderedPost = _FakeRenderedPost

    @staticmethod
    def _now_et() -> datetime:
        return datetime(2025, 12, 31, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))

    @staticmethod
    def get_analysis_mode() -> str:
        return "strict"

    @staticmethod
    def _resolve_last_price(_symbol: str, *, now_et: datetime | None = None):
        _ = now_et
        return _FakePriceSnapshot(ok=True)

    @staticmethod
    def tnt_regime_gate(_signal_payload: dict, *, output_mode: str):
        _ = output_mode
        return True, "ok"

    @staticmethod
    def get_last_n_bars(*_args, **_kwargs):
        return []


def test_stand_down_render_places_pack_after_header() -> None:
    import delivery.on_demand_data as odd

    rp = odd._build_analyze_stand_down_render(
        _FakeDeliveryModule,
        symbol_display="SPY",
        symbol_meta="SPY",
        missing=["Model Signal"],
        reason="Data Not Ready",
        action_line="Action: retry",
        last_price_section=None,
        futures_block=None,
        price_context={"mode": "LIVE", "age_minutes": 0.1, "source": "test"},
        signal_payload={
            "bias": "BULL",
            "bias_confirm": "MIXED",
            "conviction": "MED",
            "edge": 0.05,
            "regime": "RANGE",
            "session": "RTH",
            "tnt": {"regime": "RANGE", "posture": "NEUTRAL", "confidence": 0.72},
        },
    )

    lines = (rp.text or "").splitlines()
    assert lines[0].startswith("⚠️ STAND DOWN"), rp.text
    # Stand-down line, blank line, then pack header.
    assert any("Numeric Bias Pack" in ln for ln in lines[:6]), rp.text
    assert rp.text.find("Numeric Bias Pack") < rp.text.find("Symbol:"), rp.text


def test_on_demand_analyze_render_places_pack_immediately_after_header(monkeypatch) -> None:
    import delivery.on_demand_data as odd

    # Avoid network/DB/side effects by patching all heavy dependencies.
    monkeypatch.setattr(odd, "_get_delivery_module", lambda: _FakeDeliveryModule)
    monkeypatch.setattr(odd, "_build_macro_futures_context_section", lambda *_a, **_k: None)
    monkeypatch.setattr(odd, "_build_spx_addon_block", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(
        odd,
        "_resolve_on_demand_price",
        lambda *_a, **_k: {
            "mode": "LIVE",
            "source": "test",
            "age_minutes": 0.1,
            "accepted": True,
            "last_price_ts": "2025-12-31T15:00:00+00:00",
            "last_price": 500.25,
            "snapshot": _FakePriceSnapshot(ok=True),
        },
    )
    monkeypatch.setattr(
        odd,
        "_build_last_price_section",
        lambda *_a, **_k: "💵 **Last Price**\n• SPY: 500.25 (test)",
    )
    monkeypatch.setattr(
        odd,
        "_build_pivots_section",
        lambda *_a, **_k: "📐 **Pivots (RTH)**\n• R1 505.00 | P 499.00 | S1 493.00",
    )
    monkeypatch.setattr(odd, "_build_regime_section", lambda *_a, **_k: None)
    monkeypatch.setattr(
        odd,
        "_build_signal_section",
        lambda *_a, **_k: (
            "📈 **Signal**\n• Bias: BULL",
            {
                "bias": "BULL",
                "bias_confirm": "MIXED",
                "conviction": "MED",
                "edge": 0.05,
                "regime": "RANGE",
                "session": "RTH",
                "pivot": 499.0,
                "r1": 505.0,
                "s1": 493.0,
                "tnt": {"regime": "RANGE", "posture": "NEUTRAL", "confidence": 0.72},
            },
            {"ok": True},
        ),
    )
    monkeypatch.setattr(odd, "_build_technical_package", lambda *_a, **_k: (None, None, "MISSING", "MISSING"))

    rp = odd.build_on_demand_analyze_render("SPY")
    text = rp.text or ""
    lines = text.splitlines()
    assert lines[0].startswith("🔍 **On-Demand Analyze**"), text
    assert any("Numeric Bias Pack" in ln for ln in lines[:8]), text
    # Pack should appear before any major sections like last price.
    assert text.find("Numeric Bias Pack") < text.find("💵 **Last Price**"), text
