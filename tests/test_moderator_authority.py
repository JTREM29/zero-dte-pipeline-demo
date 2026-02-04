import time

import services.moderator.moderator_authority as ma


def _fixed_now(monkeypatch, now_s: int = 1_700_000_000) -> int:
    monkeypatch.setattr(time, "time", lambda: float(now_s))
    return now_s


def test_trade_action_futures_only_refuses_specifics_but_answers(monkeypatch):
    now_s = _fixed_now(monkeypatch)

    monkeypatch.setattr(
        ma,
        "_futures_snapshot",
        lambda **_: ma.FuturesSnapshot(
            ok=True,
            trend="RISK-ON",
            mode="trend",
            divergence=0.10,
            divergence_level="aligned",
            age_s=12,
        ),
    )

    ctx_market = {"ts_utc": now_s}
    decision = ma.decide_reply(
        question="What strike and expiry should I use on QQQ?",
        symbol="QQQ",
        ctx_market=ctx_market,
        ctx_sym=None,
        redis_client=None,
    )
    assert decision is not None
    assert decision.intent == ma.Intent.TRADE_ACTION
    assert decision.state == ma.DataState.FUTURES_ONLY

    text = decision.reply.lower()
    assert "pick strike" in text
    # Copy may prefer explicitly calling out stale/missing symbol context.
    assert ("futures" in text) or ("symbol context" in text)
    assert "stand" in text  # stand-aside guidance


def test_risk_structure_answers_without_symbol_snapshot(monkeypatch):
    now_s = _fixed_now(monkeypatch)

    monkeypatch.setattr(
        ma,
        "_futures_snapshot",
        lambda **_: ma.FuturesSnapshot(
            ok=True,
            trend="RISK-OFF",
            mode="trend",
            divergence=0.25,
            divergence_level="mixed",
            age_s=30,
        ),
    )

    ctx_market = {"ts_utc": now_s}
    decision = ma.decide_reply(
        question="What's the risk if ES breaks overnight lows?",
        symbol="ES",
        ctx_market=ctx_market,
        ctx_sym=None,
        redis_client=None,
    )
    assert decision is not None
    assert decision.intent == ma.Intent.RISK_STRUCTURE
    assert "gap" in decision.reply.lower()
    assert "liquidity" in decision.reply.lower()


def test_data_diagnostic_explains_missing_snapshots(monkeypatch):
    _fixed_now(monkeypatch)

    monkeypatch.setattr(ma, "_futures_snapshot", lambda **_: ma.FuturesSnapshot(ok=False))

    decision = ma.decide_reply(
        question="Why is it unavailable?",
        symbol="QQQ",
        ctx_market=None,
        ctx_sym=None,
        redis_client=None,
    )
    assert decision is not None
    assert decision.intent == ma.Intent.DATA_DIAGNOSTIC

    text = decision.reply.lower()
    assert "context writer" in text
    assert "missing" in text
