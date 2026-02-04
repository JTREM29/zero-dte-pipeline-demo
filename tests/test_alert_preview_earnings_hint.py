import json
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc

from delivery.alert_embeds import build_earnings_preview_footer


class _FakeRedis:
    def __init__(self, mapping: dict[str, str]):
        self._m = dict(mapping)

    def get(self, key: str):
        return self._m.get(key)


class _FakeStore:
    def __init__(self, mapping: dict[str, str]):
        self.r = _FakeRedis(mapping)


def test_preview_footer_only_today_or_tomorrow() -> None:
    sym = "AAPL"
    ev = {
        "symbol": sym,
        "ts_utc": "2099-01-20T21:00:00Z",
        "session": "AMC",
        "confirmed": True,
        "expected_move_pct": 4.2,
        "liquidity_risk": "HIGH",
        "front_iv": 68,
        "refreshed_utc": datetime.now(timezone.utc).isoformat(),
    }
    store = _FakeStore({f"cal:earnings:{sym}": json.dumps(ev)})
    # Not today/tomorrow -> no footer.
    assert build_earnings_preview_footer(store, sym) is None


def test_preview_footer_emits_for_tomorrow_et() -> None:
    sym = "AAPL"
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(_ET)

    # Tomorrow at 16:30 ET (AMC-ish) so overlay tags "tomorrow".
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
    store = _FakeStore({f"cal:earnings:{sym}": json.dumps(ev)})

    msg = build_earnings_preview_footer(store, sym)
    assert msg is not None
    assert "⚠" in msg
    assert "Earnings" in msg
    assert "tomorrow" in msg
    assert "expected move" in msg


def test_preview_footer_skips_missing_or_stale_refresh() -> None:
    sym = "AAPL"

    ev_missing = {
        "symbol": sym,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "session": "AMC",
        "confirmed": True,
    }
    store_missing = _FakeStore({f"cal:earnings:{sym}": json.dumps(ev_missing)})
    assert build_earnings_preview_footer(store_missing, sym) is None

    ev_stale = {
        "symbol": sym,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "session": "AMC",
        "confirmed": True,
        "refreshed_utc": "2000-01-01T00:00:00+00:00",
    }
    store_stale = _FakeStore({f"cal:earnings:{sym}": json.dumps(ev_stale)})
    assert build_earnings_preview_footer(store_stale, sym) is None
