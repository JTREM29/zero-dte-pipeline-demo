import json

from services.context import ctx_reader


class FakeRedis:
    def __init__(self, data: dict[str, str] | None = None, existing: set[str] | None = None):
        self._data = dict(data or {})
        self._existing = set(existing or set())

    def get(self, key: str):
        return self._data.get(key)

    def exists(self, key: str) -> int:
        if key in self._existing:
            return 1
        return 1 if key in self._data else 0


def test_futures_intent_stopword_never_symbol() -> None:
    r = FakeRedis(
        data={"ctx:market": json.dumps({"ts_utc": 1})},
        existing={"ctx:sym:SPY"},
    )
    spec = ctx_reader.resolve_intent_and_required_keys(message_text="How do the futures look?", r=r)
    assert spec.intent == "FUTURES_OVERVIEW"
    assert spec.symbol is None
    assert spec.required_keys == ["ctx:market"]


def test_futures_mentions_spy_still_market_only() -> None:
    r = FakeRedis(
        data={"ctx:market": json.dumps({"ts_utc": 1})},
        existing={"ctx:sym:SPY"},
    )
    spec = ctx_reader.resolve_intent_and_required_keys(message_text="What does spy futures look like?", r=r)
    assert spec.intent == "FUTURES_OVERVIEW"
    # Symbol may be detected, but must not be required.
    assert spec.required_keys == ["ctx:market"]


def test_symbol_context_spy() -> None:
    r = FakeRedis(
        data={"ctx:market": json.dumps({"ts_utc": 1}), "ctx:sym:SPY": json.dumps({"ts_utc": 1})},
        existing={"ctx:sym:SPY"},
    )
    spec = ctx_reader.resolve_intent_and_required_keys(message_text="spy", r=r)
    assert spec.intent == "SYMBOL_CONTEXT"
    assert spec.symbol == "SPY"
    assert spec.required_keys == ["ctx:market", "ctx:sym:SPY"]


def test_stopword_does_not_become_symbol() -> None:
    r = FakeRedis(existing={"ctx:sym:SPY"})
    spec = ctx_reader.resolve_intent_and_required_keys(message_text="does spy look weak today", r=r)
    assert spec.intent == "SYMBOL_CONTEXT"
    assert spec.symbol == "SPY"


def test_candidate_request_requires_market_and_symbol_only() -> None:
    r = FakeRedis(existing={"ctx:sym:SPY"})
    spec = ctx_reader.resolve_intent_and_required_keys(message_text="$SPY candidates", r=r)
    assert spec.intent == "CANDIDATE_REQUEST"
    assert spec.symbol == "SPY"
    assert spec.required_keys == ["ctx:market", "ctx:sym:SPY"]


def test_load_required_ctx_uses_spec_required_keys_only() -> None:
    r = FakeRedis(data={"ctx:market": json.dumps({"ok": True})})
    spec = ctx_reader.IntentSpec(intent="FUTURES_OVERVIEW", symbol=None, required_keys=["ctx:market"], why="test")
    mkt, sym, missing = ctx_reader.load_required_ctx(r=r, spec=spec)
    assert isinstance(mkt, dict)
    assert sym is None
    assert missing == []


def test_load_required_ctx_symbol_missing_reports_sym_only() -> None:
    r = FakeRedis(data={"ctx:market": json.dumps({"ok": True})})
    spec = ctx_reader.IntentSpec(intent="SYMBOL_CONTEXT", symbol="SPY", required_keys=["ctx:market", "ctx:sym:SPY"], why="test")
    mkt, sym, missing = ctx_reader.load_required_ctx(r=r, spec=spec)
    assert isinstance(mkt, dict)
    assert sym is None
    assert missing == ["ctx:sym:SPY"]
