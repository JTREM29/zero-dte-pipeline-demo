import time

import pytest

from delivery.fastqa_state import (
    FASTQA_NEVER_CALLS_HEAVY,
    estimate_fastqa_capacity_per_minute,
    fastqa_throttle_rate,
    format_fastqa_throttle_message,
    get_fastqa_slo_snapshot,
    record_fastqa_event,
    take_pending_fastqa_canary,
    _reset_fastqa_state_for_tests,
)


def test_throttle_template_never_mentions_queue() -> None:
    _reset_fastqa_state_for_tests()
    msg = format_fastqa_throttle_message(7)
    assert "queue" not in msg.lower()
    assert "try again" in msg.lower()


def test_slo_state_transitions_emit_canary() -> None:
    _reset_fastqa_state_for_tests()
    # Start with many OK events.
    base = time.time()
    for i in range(180):
        record_fastqa_event(ts_utc=base + (i * 0.01), decision="FAST_QA", latency_ms=100.0, output_text="ok")
    state, p95, n = get_fastqa_slo_snapshot()
    assert n > 0
    # Might be UNKNOWN early, but should settle to OK with enough data.
    assert state in {"OK", "UNKNOWN"}

    # Add enough slow events to push p95 > 800ms.
    for j in range(20):
        record_fastqa_event(ts_utc=base + 10 + (j * 0.01), decision="FAST_QA", latency_ms=900.0, output_text="ok")

    state2, p95_2, _ = get_fastqa_slo_snapshot()
    assert state2 in {"WARN", "BAD"}

    notice = take_pending_fastqa_canary()
    assert notice is not None
    assert notice.get("state") in {"WARN", "BAD"}


def test_queued_substring_triggers_bad_immediately() -> None:
    _reset_fastqa_state_for_tests()
    record_fastqa_event(decision="FAST_QA", latency_ms=10.0, output_text="Queued... please wait")
    state, _, _ = get_fastqa_slo_snapshot()
    assert state == "BAD"
    notice = take_pending_fastqa_canary()
    assert notice is not None
    assert notice.get("reason") == "queued_substring_detected"


def test_capacity_and_throttle_rate_have_sane_shapes() -> None:
    _reset_fastqa_state_for_tests()
    # Add a couple of events in the last few seconds.
    now = time.time()
    record_fastqa_event(ts_utc=now - 1, decision="FAST_QA", latency_ms=50.0, output_text="ok")
    record_fastqa_event(ts_utc=now - 1, decision="THROTTLED", latency_ms=1.0, output_text="throttle")

    cap = estimate_fastqa_capacity_per_minute(window_s=60)
    assert cap >= 0.0

    rate = fastqa_throttle_rate(window_s=300)
    assert rate is None or (0.0 <= rate <= 1.0)
