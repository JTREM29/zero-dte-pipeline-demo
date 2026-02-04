import json
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc

from services.context.context_snapshot import build_market_context, build_symbol_context
from services.context.context_writer import write_context_once


class _FakeRedis:
    def __init__(self, mapping: dict[str, object] | None = None):
        self._m: dict[str, object] = dict(mapping or {})
        self._exp: dict[str, int] = {}

    def get(self, key: str):
        return self._m.get(key)

    def set(self, key: str, value: str):
        self._m[key] = value

    def expire(self, key: str, ttl: int):
        self._exp[key] = int(ttl)


class _FakeStore:
    def __init__(self, r: _FakeRedis):
        self.r = r


def test_market_snapshot_futures_and_news_state() -> None:
    now = datetime.now(timezone.utc)
    old_ts = int((now - timedelta(hours=6)).timestamp())

    r = _FakeRedis(
        {
            "fut:scores": json.dumps({"regime": "BEARISH"}),
            "news:market:last_ts": str(old_ts),
        }
    )

    store = _FakeStore(r)
    snap = build_market_context(store)
    assert snap.get("ctx_version") == 1
    assert isinstance(snap.get("ts_utc"), int)

    fut = snap.get("futures")
    assert isinstance(fut, dict)
    assert fut.get("bias") == "BEARISH"

    news = (snap.get("news") or {})
    assert news.get("market_state") == "quiet"


def test_symbol_snapshot_includes_fresh_earnings_near_note() -> None:
    sym = "SPY"

    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(_ET)
    ts_et = (now_et + timedelta(days=1)).replace(hour=16, minute=30, second=0, microsecond=0)
    ts_utc = ts_et.astimezone(timezone.utc)

    ev = {
        "symbol": sym,
        "ts_utc": ts_utc.isoformat().replace("+00:00", "Z"),
        "session": "AMC",
        "confirmed": True,
        "expected_move_pct": 4.2,
        "liquidity_risk": "HIGH",
        "front_iv": 68,
        "refreshed_utc": now_utc.isoformat(),
    }

    r = _FakeRedis({f"cal:earnings:{sym}": json.dumps(ev)})
    store = _FakeStore(r)
    snap = build_symbol_context(store, sym, stale_hours=48)

    assert snap.get("ctx_version") == 1
    assert snap.get("symbol") == sym

    e = snap.get("earnings")
    assert isinstance(e, dict)
    assert e.get("fresh") is True
    note = str(e.get("note") or "")
    assert note
    assert "earnings" in note.lower()

    cand = snap.get("candidates")
    assert isinstance(cand, dict)
    # Minimal schema presence
    assert cand.get("status") in {"APPROVED", "NONE", "STALE", "ERROR"}
    assert cand.get("eligibility") in {"ELIGIBLE", "NO_TRADE"}
    assert cand.get("confidence") in {"HIGH", "MED", "LOW"}


def test_symbol_snapshot_projects_candidates_from_cache() -> None:
    sym = "SPY"
    now = int(datetime.now(timezone.utc).timestamp())

    cached = {
        "ts_utc": now,
        "as_of_et": "2026-01-19 10:12 ET",
        "status": "APPROVED",
        "why": "OK",
        "eligibility": "ELIGIBLE",
        "confidence": "HIGH",
        "approved": [{"strategy": "call_spread", "direction": "BULLISH", "score": 0.9}],
        "top": {"strategy": "call_spread", "direction": "BULLISH", "score": 0.9},
    }

    r = _FakeRedis({f"tnt:candidates:{sym}": json.dumps(cached)})
    store = _FakeStore(r)
    snap = build_symbol_context(store, sym, stale_hours=48)

    cand = snap.get("candidates")
    assert isinstance(cand, dict)
    assert cand.get("status") == "APPROVED"
    assert isinstance(cand.get("approved"), list)
    assert isinstance(cand.get("top"), dict)
    assert isinstance(cand.get("age_s"), int)


def test_write_context_once_writes_keys() -> None:
    r = _FakeRedis({"fut:scores": json.dumps({"regime": "BULL"})})
    store = _FakeStore(r)

    out = write_context_once(store, symbols=["SPY", "QQQ"], stale_hours=48)
    assert out.get("symbols") == ["QQQ", "SPY"]

    assert "ctx:market" in r._m
    assert "ctx:hb" in r._m
    assert "ctx:sym:SPY" in r._m
    assert "ctx:sym:QQQ" in r._m

    # Ensure stored values are JSON.
    json.loads(str(r._m["ctx:market"]))
    json.loads(str(r._m["ctx:sym:SPY"]))
