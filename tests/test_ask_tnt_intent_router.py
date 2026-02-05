import json
import re


class _FakeRedis:
    def __init__(self, *, existing=None, values=None, hashes=None, scan_keys=None):
        self._existing = set(existing or [])
        self._values = dict(values or {})
        self._hashes = dict(hashes or {})
        self._scan_keys = list(scan_keys or [])

    def exists(self, key: str) -> int:
        return 1 if str(key) in self._existing else 0

    def get(self, key: str):
        return self._values.get(str(key))

    def hgetall(self, key: str):
        return dict(self._hashes.get(str(key)) or {})

    def scan(self, cursor: int = 0, match: str | None = None, count: int = 10):  # noqa: ARG002
        # Minimal scan: return all keys once.
        if cursor and int(cursor) != 0:
            return 0, []
        if match is None:
            return 0, list(self._scan_keys)
        # naive wildcard: only supports '*' anywhere
        pat = str(match)
        if "*" not in pat:
            return 0, [k for k in self._scan_keys if k == pat]
        parts = pat.split("*")
        out = []
        for k in self._scan_keys:
            s = str(k)
            ok = True
            idx = 0
            for p in parts:
                if not p:
                    continue
                j = s.find(p, idx)
                if j < 0:
                    ok = False
                    break
                idx = j + len(p)
            if ok:
                out.append(k)
        return 0, out

    def setex(self, key: str, ttl: int, value: str) -> bool:  # noqa: ARG002
        self._values[str(key)] = str(value)
        return True


def test_detect_intent_help_and_status_and_futures():
    from services.ask_tnt_router import detect_intent

    assert detect_intent("help").intent == "help"
    assert detect_intent("status").intent == "status"
    assert detect_intent("futures").intent == "futures_overview"


def test_detect_intent_macro_variants():
    from services.ask_tnt_router import detect_intent

    assert detect_intent("next cpi").intent == "macro_events"
    assert detect_intent("macro posture?", redis_client=None).intent == "macro_posture"
    assert detect_intent("posture", redis_client=None).intent == "macro_posture"
    assert detect_intent("should we stand down?", redis_client=None).intent == "macro_posture"
    assert detect_intent("stand down?", redis_client=None).intent == "macro_posture"
    assert detect_intent("blackout?", redis_client=None).intent == "macro_posture"
    assert detect_intent("rates / curve").intent == "rates"
    assert detect_intent("inflation snapshot").intent == "inflation"
    assert detect_intent("labor market").intent == "labor"


def test_detect_intent_symbol_context_uses_redis_validation():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:AAPL"})
    res = detect_intent("does AAPL look weak", redis_client=r)
    assert res.intent == "symbol_context"
    assert res.slots.get("symbol") == "AAPL"


def test_detect_intent_spx_proxies_to_spy_when_index_ctx_missing():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:SPY"})
    res = detect_intent("SPX", redis_client=r)
    assert res.intent == "symbol_context"
    assert res.slots.get("symbol") == "SPY"
    assert res.slots.get("proxy_from") == "SPX"
    assert res.slots.get("proxy_to") == "SPY"


def test_detect_intent_market_snapshot_when_user_asks_whats_spy_doing():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:SPY"})
    res = detect_intent("what's SPY doing today", redis_client=r)
    assert res.intent == "market_snapshot"
    assert res.slots.get("symbol") == "SPY"


def test_detect_intent_blackout_with_symbol_and_today_still_routes_to_market_snapshot():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:SPY"})
    res = detect_intent("SPY blackout today?", redis_client=r)
    assert res.intent == "market_snapshot"
    assert res.slots.get("symbol") == "SPY"


def test_detect_intent_market_snapshot_beats_macro_when_both_present():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:SPY"})
    res = detect_intent("what is SPX doing today rates", redis_client=r)
    assert res.intent == "market_snapshot"
    assert res.slots.get("symbol") in {"SPX", "SPY"}


def test_detect_intent_market_snapshot_for_spx_doing_today_without_macro_tokens():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:SPY"})
    res = detect_intent("what is SPX doing today?", redis_client=r)
    assert res.intent == "market_snapshot"
    assert res.slots.get("symbol") in {"SPX", "SPY"}


def test_detect_intent_help_is_explicit_only_and_still_works():
    from services.ask_tnt_router import detect_intent

    assert detect_intent("next cpi?").intent == "macro_events"
    assert detect_intent("next cpi?").intent != "macro_posture"
    assert detect_intent("how do i use tnt?").intent == "help"


def test_detect_intent_oi_snapshot_defaults_to_spy_and_marks_defaulted():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:SPY"})
    res = detect_intent("put wall chart?", redis_client=r)
    assert res.intent == "oi_snapshot"
    assert res.slots.get("symbol") == "SPY"
    assert bool(res.slots.get("defaulted_symbol")) is True
    assert bool(res.slots.get("want_chart")) is True


def test_detect_intent_candidates_defaults_to_spy():
    from services.ask_tnt_router import detect_intent

    r = _FakeRedis(existing={"ctx:sym:SPY"})
    res = detect_intent("any trades today?", redis_client=r)
    assert res.intent == "candidates"
    assert res.slots.get("symbol") == "SPY"


def test_build_ask_reply_unknown_falls_back_to_help_and_has_data_used():
    from services.ask_tnt_router import build_ask_reply

    out = build_ask_reply(None, "banana")
    assert out.startswith("**Ask-TNT help**")
    assert "Data used:" in out
    assert "Freshness:" in out


def test_build_ask_reply_symbol_context_includes_data_used_and_proxy_note_once():
    from services.ask_tnt_router import build_ask_reply

    ctx_market = {"ctx_version": 1, "ts_utc": 1000, "futures": {"bias": "BULLISH", "degraded": False}, "news": {"market_state": "quiet"}}
    ctx_spy = {
        "ctx_version": 1,
        "ts_utc": 1000,
        "symbol": "SPY",
        "futures": {"bias": "BULLISH", "fresh": True},
        "earnings": {"fresh": False, "note": None},
        "news": {"sym_state": "quiet", "market_state": "quiet"},
    }

    r = _FakeRedis(
        existing={"ctx:sym:SPY"},
        values={
            "ctx:market": json.dumps(ctx_market),
            "ctx:sym:SPY": json.dumps(ctx_spy),
        },
    )

    out1 = build_ask_reply(r, "SPX", user_id=123)
    assert out1.startswith("**SPY context**")
    assert "using SPY proxy" in out1
    assert "Data used:" in out1

    # Second call within TTL should suppress the repeated proxy note (last-intent cache).
    out2 = build_ask_reply(r, "SPX", user_id=123)
    assert out2.startswith("**SPY context**")
    assert "proxy" not in out2.lower()


def test_build_ask_response_candidates_includes_not_a_signal_and_do_nothing_when_none():
    from services.ask_tnt_router import build_ask_response

    ctx_spy = {
        "ctx_version": 1,
        "ts_utc": 1000,
        "symbol": "SPY",
        "candidates": {"status": "NONE", "why": "NO_CANDIDATE_CACHE", "age_s": 0, "approved": []},
    }
    r = _FakeRedis(existing={"ctx:sym:SPY"}, values={"ctx:sym:SPY": json.dumps(ctx_spy)})
    resp = build_ask_response(r, "any trades today", user_id=1)
    assert resp.text.startswith("**SPY candidates")
    assert "Do nothing" in resp.text
    assert "Not a signal" in resp.text
    assert "Data used:" in resp.text


def test_build_ask_response_oi_chart_not_cached_does_not_attach():
    from services.ask_tnt_router import build_ask_response

    r = _FakeRedis(
        existing={"ctx:sym:SPY"},
        hashes={"oi:walls:SPY": {"put_wall": "500", "call_wall": "510", "ts_et": "2026-01-01T10:00:00"}},
        values={},
        scan_keys=[],
    )
    resp = build_ask_response(r, "SPY put wall chart", user_id=7)
    assert resp.text.startswith("**SPY OI snapshot**")
    assert "Chart: not cached" in resp.text
    assert resp.attachments == []


def test_market_snapshot_spx_price_proxies_to_spy_price_only_when_spx_db_missing(monkeypatch):
    from datetime import datetime

    import services.ask_tnt_router as mod
    from services.ask_tnt_router import build_ask_reply

    def _fake_db_last_close_record(symbol: str, tf: str = "1m"):
        sym = (symbol or "").strip().upper()
        if sym == "SPX":
            return None, None, None
        if sym == "SPY" and tf == "1m":
            return 500.0, datetime(2026, 2, 4, 10, 0, tzinfo=mod.ET), "2026-02-04T15:00:00Z"
        if sym == "SPY" and tf == "1d":
            return 495.0, datetime(2026, 2, 3, 16, 0, tzinfo=mod.ET), "2026-02-03T21:00:00Z"
        return None, None, None

    monkeypatch.setattr(mod, "_db_last_close_record", _fake_db_last_close_record)
    monkeypatch.setattr(mod, "_now_et", lambda: datetime(2026, 2, 4, 10, 1, tzinfo=mod.ET))

    ctx_spx = {
        "ctx_version": 1,
        "ts_utc": 1000,
        "symbol": "SPX",
        "futures": {"bias": "BULLISH", "fresh": True},
        "news": {"sym_state": "quiet"},
        "earnings": {"fresh": False, "note": None},
    }

    r = _FakeRedis(
        existing={"ctx:sym:SPX"},
        values={
            "ctx:sym:SPX": json.dumps(ctx_spx),
        },
    )

    out = build_ask_reply(r, "what is SPX doing today?", user_id=42)
    assert out.startswith("**SPX market snapshot**")
    assert "Price proxy: SPX price not cached; using SPY price cache for snapshot price only." in out
    assert "src=db_prices:SPY" in out


def test_market_snapshot_spx_stays_unavailable_if_spy_price_missing_too(monkeypatch):
    import services.ask_tnt_router as mod
    from services.ask_tnt_router import build_ask_reply

    def _fake_db_last_close_record(symbol: str, tf: str = "1m"):
        return None, None, None

    monkeypatch.setattr(mod, "_db_last_close_record", _fake_db_last_close_record)

    ctx_spx = {"ctx_version": 1, "ts_utc": 1000, "symbol": "SPX"}
    r = _FakeRedis(existing={"ctx:sym:SPX"}, values={"ctx:sym:SPX": json.dumps(ctx_spx)})

    out = build_ask_reply(r, "what is SPX doing today?", user_id=7)
    assert out.startswith("**SPX market snapshot**")
    assert "Snapshot unavailable" in out
    assert "Price proxy:" not in out


def test_market_snapshot_spy_today_stale_refuses_and_shows_no_market_numbers(monkeypatch):
    from datetime import datetime

    import services.ask_tnt_router as mod
    from services.ask_tnt_router import build_ask_reply

    monkeypatch.setenv("TNT_ASK_MAX_PRICE_AGE_SEC", "300")
    monkeypatch.setenv("TNT_ASK_MAX_PRICE_AGE_SEC_SOFT", "1800")

    now = datetime(2026, 2, 4, 10, 0, tzinfo=mod.ET)
    monkeypatch.setattr(mod, "_now_et", lambda: now)

    def _fake_rec(symbol: str, tf: str = "1m"):
        sym = (symbol or "").strip().upper()
        if sym == "SPY" and tf == "1m":
            # 2 hours stale
            ts = datetime(2026, 2, 4, 8, 0, tzinfo=mod.ET)
            return 500.0, ts, "2026-02-04T13:00:00Z"
        if sym == "SPY" and tf == "1d":
            ts = datetime(2026, 2, 3, 16, 0, tzinfo=mod.ET)
            return 495.0, ts, "2026-02-03T21:00:00Z"
        return None, None, None

    monkeypatch.setattr(mod, "_db_last_close_record", _fake_rec)

    r = _FakeRedis(existing={"ctx:sym:SPY"}, values={"ctx:sym:SPY": json.dumps({"ctx_version": 1, "ts_utc": 1000, "symbol": "SPY"}), "ctx:market": json.dumps({"ctx_version": 1, "ts_utc": 1000})})

    out = build_ask_reply(r, "what is SPY doing today?", user_id=1)
    assert out.splitlines()[0].startswith("Snapshot unavailable (price cache stale)")
    assert "price cache stale" in out.lower()
    assert "Last:" not in out
    assert "Δ" not in out
    assert re.search(r"\bprev\s+close\b", out, re.I) is None


def test_market_snapshot_spy_today_fresh_prints_price_line(monkeypatch):
    from datetime import datetime

    import services.ask_tnt_router as mod
    from services.ask_tnt_router import build_ask_reply

    monkeypatch.setenv("TNT_ASK_MAX_PRICE_AGE_SEC", "300")
    now = datetime(2026, 2, 4, 10, 0, tzinfo=mod.ET)
    monkeypatch.setattr(mod, "_now_et", lambda: now)

    def _fake_rec(symbol: str, tf: str = "1m"):
        sym = (symbol or "").strip().upper()
        if sym == "SPY" and tf == "1m":
            ts = datetime(2026, 2, 4, 9, 59, tzinfo=mod.ET)
            return 500.0, ts, "2026-02-04T14:59:00Z"
        if sym == "SPY" and tf == "1d":
            ts = datetime(2026, 2, 3, 16, 0, tzinfo=mod.ET)
            return 495.0, ts, "2026-02-03T21:00:00Z"
        return None, None, None

    monkeypatch.setattr(mod, "_db_last_close_record", _fake_rec)

    r = _FakeRedis(existing={"ctx:sym:SPY"}, values={"ctx:sym:SPY": json.dumps({"ctx_version": 1, "ts_utc": 1000, "symbol": "SPY"}), "ctx:market": json.dumps({"ctx_version": 1, "ts_utc": 1000})})

    out = build_ask_reply(r, "what is SPY doing today?", user_id=1)
    assert "Snapshot unavailable" not in out
    assert "Last:" in out
    assert "src=db_prices:SPY" in out


def test_market_snapshot_spx_today_uses_proxy_and_has_truthful_src_and_proxy_freshness(monkeypatch):
    from datetime import datetime

    import services.ask_tnt_router as mod
    from services.ask_tnt_router import build_ask_reply

    now = datetime(2026, 2, 4, 10, 0, tzinfo=mod.ET)
    monkeypatch.setattr(mod, "_now_et", lambda: now)
    monkeypatch.setenv("TNT_ASK_MAX_PRICE_AGE_SEC", "300")

    def _fake_rec(symbol: str, tf: str = "1m"):
        sym = (symbol or "").strip().upper()
        if sym == "SPX":
            return None, None, None
        if sym == "SPY" and tf == "1m":
            ts = datetime(2026, 2, 4, 9, 59, tzinfo=mod.ET)
            return 500.0, ts, "2026-02-04T14:59:00Z"
        if sym == "SPY" and tf == "1d":
            ts = datetime(2026, 2, 3, 16, 0, tzinfo=mod.ET)
            return 495.0, ts, "2026-02-03T21:00:00Z"
        return None, None, None

    monkeypatch.setattr(mod, "_db_last_close_record", _fake_rec)

    r = _FakeRedis(existing={"ctx:sym:SPX"}, values={"ctx:sym:SPX": json.dumps({"ctx_version": 1, "ts_utc": 1000, "symbol": "SPX"}), "ctx:market": json.dumps({"ctx_version": 1, "ts_utc": 1000})})

    out = build_ask_reply(r, "what is SPX doing today?", user_id=2)
    assert "Price proxy: SPX price not cached; using SPY price cache for snapshot price only." in out
    assert "src=db_prices:SPY" in out
    assert re.search(r"Freshness:.*price_proxy", out) is not None
