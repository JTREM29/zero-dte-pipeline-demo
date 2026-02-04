import json
import os
import time
import types

from delivery.fastqa_state import FASTQA_NEVER_CALLS_HEAVY, update_fastqa_last


def test_fastqa_status_module_is_dependency_light() -> None:
    # Guardrail: the status builder must not pull in heavy runtime modules.
    # (String-based so it's robust even if other tests imported things already.)
    import cli.fastqa_status as mod

    path = mod.__file__
    assert path
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    forbidden = [
        "import delivery.discord_bot",
        "from delivery import discord_bot",
        "import controller.worker_pool",
        "import controller.worker_health",
        "enqueue_job",
        "smb",
    ]
    for token in forbidden:
        assert token not in src


def test_fastqa_status_includes_hard_assertion_and_last_receipt(monkeypatch) -> None:
    monkeypatch.setenv("TNT_ASK_TNT_FREE_TEXT_ENABLED", "1")
    monkeypatch.setenv("TNT_FAST_QA_DEBUG", "1")

    update_fastqa_last(
        ts_utc=time.time(),
        channel_id=123,
        user_id=987654321,
        mode="slash_ask",
        decision="FAST_QA",
        reason="intent=MARKET_CONTEXT",
        latency_ms=12.3,
        ctx_ok=True,
        ctx_age_s=3,
        futures_ok=True,
        futures_age_s=2,
    )

    from cli.fastqa_status import build_fastqa_status_message

    msg = build_fastqa_status_message(check=False)
    assert f"fastqa_never_calls_heavy={FASTQA_NEVER_CALLS_HEAVY}" in msg
    assert "Last receipt:" in msg
    assert "decision=FAST_QA" in msg


def test_fastqa_status_live_check_uses_fast_lane(monkeypatch) -> None:
    # Stub redis so no network calls happen.
    class _DummyRedis:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, key):
            assert key == "ctx:market"
            return json.dumps(
                {
                    "ts_utc": int(time.time()),
                    "futures": {"hb_age_sec": 1, "degraded": False},
                }
            )

    dummy = types.SimpleNamespace(Redis=_DummyRedis)
    monkeypatch.setitem(__import__("sys").modules, "redis", dummy)

    from cli.fastqa_status import build_fastqa_status_message

    msg = build_fastqa_status_message(check=True)
    assert "Live check:" in msg
    assert "futures_ok=True" in msg
