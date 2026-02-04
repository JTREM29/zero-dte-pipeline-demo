from __future__ import annotations

import time
from pathlib import Path

from delivery.tnt_prompt import load_tnt_system_prompt
from delivery.tnt_state import (
    build_tnt_state_from_analysis_payload,
    build_tnt_state_from_packet,
    validate_tnt_state,
)


def test_tnt_system_prompt_loader_reads_disk_and_detects_change(tmp_path: Path) -> None:
    prompt_path = tmp_path / "tnt_system_prompt.txt"
    prompt_path.write_text("alpha", encoding="utf-8")

    first = load_tnt_system_prompt(prompt_path)
    assert first.sha256
    assert first.chars == 5
    assert "alpha" in first.text

    # Ensure mtime changes on Windows filesystems.
    time.sleep(0.02)
    prompt_path.write_text("beta", encoding="utf-8")

    second = load_tnt_system_prompt(prompt_path)
    assert second.text == "beta"
    assert second.sha256 != first.sha256


def test_tnt_state_from_packet_validates() -> None:
    packet = {
        "symbol": "SPY",
        "current_price": 508.25,
        "current_price_source": "db_1m",
        "current_price_ts": "2025-12-24T14:41:12+00:00",
        "pivot": 507.2,
        "r_levels": {"R1": 509.1, "R2": 511.0, "R3": None},
        "s_levels": {"S1": 505.4, "S2": 503.6, "S3": None},
        "bias": "NEUTRAL",
        "conviction": "LOW",
        "regime": "COMPRESSION",
        "notes": {"session": "RTH", "data_staleness_s": 3.0},
        "trade_context_mode": "db",
    }

    result = build_tnt_state_from_packet(packet, freshness_ok_sec=9999)
    ok, err = validate_tnt_state(result.state)
    assert ok, err
    assert result.state["meta"]["schema_version"] == "1.0"


def test_tnt_state_from_analysis_payload_validates() -> None:
    analysis_payload = {
        "symbol": "SPY",
        "meta": {
            "display_symbol": "SPY",
            "price_context": {
                "mode": "LIVE",
                "source": "EXTERNAL",
                "last_price_ts": "2025-12-24T14:41:12+00:00",
                "last_price": 508.25,
            },
            "gate": {"mode": "OK"},
            "signal_payload": {
                "bias": "NEUTRAL",
                "conviction": "LOW",
                "regime": "COMPRESSION",
                "pivot": 507.2,
                "r1": 509.1,
                "r2": 511.0,
                "s1": 505.4,
                "s2": 503.6,
                "tf_exec": "5m",
                "tf_struct": "60m",
                "tf_ctx": "1D",
                "session": "RTH",
            },
        },
    }

    result = build_tnt_state_from_analysis_payload(analysis_payload, freshness_ok_sec=9999)
    ok, err = validate_tnt_state(result.state)
    assert ok, err


def test_tnt_state_data_integrity_conflict_forces_degraded_and_no_trade() -> None:
    packet = {
        "symbol": "SPY",
        "current_price": 679.0,
        "current_price_source": "live",
        "pivot": 4832.0,
        "r_levels": {"R1": 4845.0, "R2": None},
        "s_levels": {"S1": 4818.0, "S2": None},
        "bias": "NEUTRAL",
        "conviction": "LOW",
        "regime": "COMPRESSION",
        "notes": {"session": "RTH", "data_staleness_s": 0.0},
        "trade_context_mode": "db",
    }

    result = build_tnt_state_from_packet(packet, freshness_ok_sec=9999)
    assert result.state["meta"]["data_health"] == "DEGRADED"
    assert result.state["meta"].get("data_integrity", {}).get("ok") is False
    assert result.state["permissions"]["no_trade"] is True
    assert result.state["permissions"]["aggression"] == "PROHIBITED"
    assert "DATA_INTEGRITY_CONFLICT" in result.state["permissions"]["reasons"]
