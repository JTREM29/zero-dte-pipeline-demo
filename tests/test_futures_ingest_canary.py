from __future__ import annotations

from services.observability.futures_ingest_canary import (
    FuturesIngestCanary,
    evaluate_futures_ingest_state,
)


def test_evaluate_down_when_hb_missing() -> None:
    state, snap = evaluate_futures_ingest_state(
        now_epoch=1_000,
        hb_ts=None,
        hb_msg=None,
        scores_updated_utc=None,
        scores_regime=None,
        status_note=None,
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert state == "DOWN"
    assert snap.hb_ts is None
    assert snap.scores_present is False


def test_evaluate_down_when_hb_stale() -> None:
    state, snap = evaluate_futures_ingest_state(
        now_epoch=1_000,
        hb_ts=1_000 - 200,
        hb_msg="ok",
        scores_updated_utc=1_000 - 10,
        scores_regime="NEUTRAL",
        status_note="ok",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert state == "DOWN"
    assert snap.hb_age_s == 200


def test_evaluate_degraded_when_scores_missing_but_hb_fresh() -> None:
    state, snap = evaluate_futures_ingest_state(
        now_epoch=1_000,
        hb_ts=1_000 - 10,
        hb_msg="ok",
        scores_updated_utc=None,
        scores_regime=None,
        status_note="ok",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert state == "DEGRADED"
    assert snap.scores_present is False


def test_evaluate_degraded_when_scores_too_old() -> None:
    state, snap = evaluate_futures_ingest_state(
        now_epoch=1_000,
        hb_ts=1_000 - 10,
        hb_msg="ok",
        scores_updated_utc=1_000 - 999,
        scores_regime="NEUTRAL",
        status_note="ok",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert state == "DEGRADED"
    assert snap.scores_age_s == 999


def test_evaluate_ok_when_hb_and_scores_fresh() -> None:
    state, snap = evaluate_futures_ingest_state(
        now_epoch=1_000,
        hb_ts=1_000 - 10,
        hb_msg="ok",
        scores_updated_utc=1_000 - 30,
        scores_regime="NEUTRAL",
        status_note="ok",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert state == "OK"
    assert snap.hb_age_s == 10
    assert snap.scores_age_s == 30


def test_evaluate_degraded_when_note_or_msg_non_ok() -> None:
    state1, _ = evaluate_futures_ingest_state(
        now_epoch=1_000,
        hb_ts=1_000 - 10,
        hb_msg="warn",
        scores_updated_utc=1_000 - 30,
        scores_regime="NEUTRAL",
        status_note="ok",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert state1 == "DEGRADED"

    state2, _ = evaluate_futures_ingest_state(
        now_epoch=1_000,
        hb_ts=1_000 - 10,
        hb_msg="ok",
        scores_updated_utc=1_000 - 30,
        scores_regime="NEUTRAL",
        status_note="degraded",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert state2 == "DEGRADED"


def test_canary_transition_only_and_no_initial_ok() -> None:
    c = FuturesIngestCanary(min_post_sec=300)

    # initial OK -> no message
    assert (
        c.maybe_message(
            now_epoch=1_000,
            hb_ts=1_000 - 10,
            hb_msg="ok",
            scores_updated_utc=1_000 - 30,
            scores_regime="NEUTRAL",
            status_note="ok",
            hb_max_age_sec=90,
            scores_max_age_sec=120,
        )
        is None
    )

    # OK -> DEGRADED should post
    msg = c.maybe_message(
        now_epoch=1_030,
        hb_ts=1_030 - 10,
        hb_msg="ok",
        scores_updated_utc=None,
        scores_regime=None,
        status_note="ok",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert msg is not None
    assert "DEGRADED" in msg

    # DEGRADED -> DEGRADED -> no message
    assert (
        c.maybe_message(
            now_epoch=1_060,
            hb_ts=1_060 - 10,
            hb_msg="ok",
            scores_updated_utc=None,
            scores_regime=None,
            status_note="ok",
            hb_max_age_sec=90,
            scores_max_age_sec=120,
        )
        is None
    )


def test_canary_always_posts_on_worsening_even_with_cooldown() -> None:
    c = FuturesIngestCanary(min_post_sec=9999)

    # start DEGRADED -> should post initial non-OK
    first = c.maybe_message(
        now_epoch=1_000,
        hb_ts=1_000 - 10,
        hb_msg="ok",
        scores_updated_utc=None,
        scores_regime=None,
        status_note="ok",
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert first is not None
    assert "DEGRADED" in first

    # worsen to DOWN quickly -> must post despite cooldown
    second = c.maybe_message(
        now_epoch=1_010,
        hb_ts=None,
        hb_msg=None,
        scores_updated_utc=None,
        scores_regime=None,
        status_note=None,
        hb_max_age_sec=90,
        scores_max_age_sec=120,
    )
    assert second is not None
    assert "DOWN" in second
