from __future__ import annotations

from services.moderator.moderator_policy import Action, Bucket, FuturesSignal, ModeratorEnv, TemplateId, classify_bucket, decide_policy
from services.moderator.moderator_templates import render_reply


def test_classify_bucket_trade_entry() -> None:
    assert classify_bucket("should I buy 0dte calls") == Bucket.TRADE_ENTRY
    assert classify_bucket("what strike should I take") == Bucket.TRADE_ENTRY


def test_classify_bucket_sizing() -> None:
    assert classify_bucket("how many contracts") == Bucket.SIZING


def test_classify_bucket_prediction() -> None:
    assert classify_bucket("will SPY go up today") == Bucket.PREDICTION


def test_policy_disabled_when_not_primary() -> None:
    env = ModeratorEnv(primary_moderator=False, owner_available=False, strictness="on")
    d = decide_policy(user_text="should I buy calls", env=env, futures=None)
    assert d.action == Action.PASS_THROUGH
    assert d.template_id is None


def test_policy_disabled_when_strictness_off() -> None:
    env = ModeratorEnv(primary_moderator=True, owner_available=False, strictness="off")
    d = decide_policy(user_text="how many contracts", env=env, futures=None)
    assert d.action == Action.PASS_THROUGH
    assert d.template_id is None


def test_policy_blocks_entries_in_on_mode() -> None:
    env = ModeratorEnv(primary_moderator=True, owner_available=False, strictness="on")
    fut = FuturesSignal(state="OK", trend="RISK-ON", mode="normal", divergence_level="aligned")
    d = decide_policy(user_text="buy puts now?", env=env, futures=fut)
    assert d.action == Action.REPLY_TEMPLATE
    assert d.template_id == TemplateId.NO_ENTRIES


def test_template_no_entries_golden_owner_unavailable() -> None:
    env = ModeratorEnv(primary_moderator=True, owner_available=False, strictness="on")
    r = render_reply(template_id=TemplateId.NO_ENTRIES, env=env)
    assert r.text == (
        "I can’t provide trade entries/strikes/expirations.\n"
        "Safer next step: share your thesis + level(s) you’re watching, and I’ll help you frame confirmation/invalidations."
    )


def test_template_no_entries_golden_owner_available() -> None:
    env = ModeratorEnv(primary_moderator=True, owner_available=True, strictness="on")
    r = render_reply(template_id=TemplateId.NO_ENTRIES, env=env)
    assert r.text == (
        "I can’t provide trade entries/strikes/expirations.\n"
        "Safer next step: share your thesis + level(s) you’re watching, and I’ll help you frame confirmation/invalidations."
        "\n\nIf you need a second set of eyes, tag the owner."
    )
