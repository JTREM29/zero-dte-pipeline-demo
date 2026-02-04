from __future__ import annotations

from datetime import datetime, timezone
import time


def _patch_delivery_minimal(monkeypatch, mod) -> None:
    # Keep the status builder deterministic and lightweight.
    monkeypatch.setattr(mod.delivery, "_now_et", lambda: datetime(2026, 1, 25, 12, 0, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(mod.delivery, "market_session_et", lambda _dt: "RTH")
    monkeypatch.setattr(mod.delivery, "_calc_data_stale", lambda: (False, 0.0, None))
    monkeypatch.setattr(mod.delivery, "_format_ts_et", lambda _ts: "unknown")
    monkeypatch.setattr(mod.delivery, "_build_agent_payload", lambda *a, **k: None)
    monkeypatch.setattr(mod.delivery, "STARTUP_SYMBOLS", ["SPY"], raising=False)
    monkeypatch.setattr(mod.delivery, "STRICT_CONTRACTS", False, raising=False)
    monkeypatch.setattr(mod.delivery, "AUTOPOST_CONTRACT_VERSION", "v", raising=False)


def test_status_report_futures_down_when_hb_stale(monkeypatch) -> None:
    import cli.discord_bot as mod

    _patch_delivery_minimal(monkeypatch, mod)
    monkeypatch.setattr(time, "time", lambda: 1_000)
    monkeypatch.setenv("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", "90")
    monkeypatch.setenv("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120")

    class FakeScores:
        updated_utc = 0

    class FakeStore:
        def get_heartbeat(self):
            return (1_000 - 212, "ok")

        def get_status(self):
            return {"note": "ok"}

        def get_scores(self):
            return FakeScores()

    monkeypatch.setattr("services.futures.futures_store.FuturesStore", lambda: FakeStore())
    monkeypatch.setattr(mod, "CANARY_ID", 0, raising=False)

    report = mod._build_status_report()
    assert "Futures: DOWN (hb 212s stale)" in report


def test_status_report_futures_degraded_when_scores_missing(monkeypatch) -> None:
    import cli.discord_bot as mod

    _patch_delivery_minimal(monkeypatch, mod)
    monkeypatch.setattr(time, "time", lambda: 1_000)
    monkeypatch.setenv("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", "90")
    monkeypatch.setenv("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120")

    class FakeStore:
        def get_heartbeat(self):
            return (1_000 - 4, "ok")

        def get_status(self):
            return {"note": "ok"}

        def get_scores(self):
            return None

    monkeypatch.setattr("services.futures.futures_store.FuturesStore", lambda: FakeStore())
    monkeypatch.setattr(mod, "CANARY_ID", 0, raising=False)

    report = mod._build_status_report()
    assert "Futures: DEGRADED (hb 4s, scores missing)" in report


def test_status_report_futures_ok_when_hb_and_scores_fresh(monkeypatch) -> None:
    import cli.discord_bot as mod

    _patch_delivery_minimal(monkeypatch, mod)
    monkeypatch.setattr(time, "time", lambda: 1_000)
    monkeypatch.setenv("TNT_FUTURES_INGEST_HB_MAX_AGE_SEC", "90")
    monkeypatch.setenv("TNT_FUTURES_INGEST_SCORES_MAX_AGE_SEC", "120")

    class FakeScores:
        updated_utc = 1_000 - 14

    class FakeStore:
        def get_heartbeat(self):
            return (1_000 - 2, "ok")

        def get_status(self):
            return {"note": "ok"}

        def get_scores(self):
            return FakeScores()

    monkeypatch.setattr("services.futures.futures_store.FuturesStore", lambda: FakeStore())
    monkeypatch.setattr(mod, "CANARY_ID", 0, raising=False)

    report = mod._build_status_report()
    assert "Futures: OK (hb 2s, scores 14s)" in report
