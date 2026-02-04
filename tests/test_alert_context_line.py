import json
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc

from services.alerts.alert_context_line import build_alert_context_line


class _FakeRedis:
    def __init__(self, mapping: dict[str, object]):
        self._m = dict(mapping)
        self._setex: dict[str, tuple[int, str]] = {}

    def get(self, key: str):
        if key in self._setex:
            # TTL not simulated; key exists.
            return self._setex[key][1]
        return self._m.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self._setex[key] = (int(ttl), str(value))


class _FakeStore:
    def __init__(self, mapping: dict[str, object]):
        self.r = _FakeRedis(mapping)


def test_context_line_builds_and_clips(monkeypatch) -> None:
    monkeypatch.setenv("ALERT_CONTEXT_LINE_ENABLED", "1")
    # Keep this high so we can assert all parts are present before clipping.
    monkeypatch.setenv("ALERT_CONTEXT_LINE_MAX_TOKENS", "240")
    monkeypatch.setenv("ALERT_CONTEXT_LINE_INCLUDE_NEWS", "1")
    monkeypatch.setenv("ALERT_CONTEXT_LINE_INCLUDE_EARNINGS", "1")
    monkeypatch.setenv("ALERT_CONTEXT_LINE_INCLUDE_FUTURES", "1")
    monkeypatch.setenv("EARNINGS_PREVIEW_STALE_HOURS", "48")

    sym = "SPY"

    # Futures: BEAR regime -> "ES bearish"
    fut_scores = {"regime": "BEARISH", "updated_utc": int(datetime.now(timezone.utc).timestamp())}

    # Earnings: tomorrow ET
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

    # News: very old -> quiet
    old_ts = int((now_utc - timedelta(hours=6)).timestamp())

    store = _FakeStore(
        {
            "fut:scores": json.dumps(fut_scores),
            f"cal:earnings:{sym}": json.dumps(ev),
            f"news:symbol:{sym}:last_ts": str(old_ts),
            "news:market:last_ts": str(old_ts),
        }
    )

    line = build_alert_context_line(store, sym, alert_id="A1")
    assert line is not None
    assert line.startswith("Context: ")
    assert "ES: bearish" in line
    assert "earnings" in line.lower()
    assert "tomorrow" in line.lower()
    assert "News: quiet" in line

    # Ensure it clips if forced short.
    monkeypatch.setenv("ALERT_CONTEXT_LINE_MAX_TOKENS", "60")
    line2 = build_alert_context_line(store, sym, alert_id="A2")
    assert line2 is not None
    assert len(line2) <= 60


def test_context_line_rate_limits_per_alert(monkeypatch) -> None:
    monkeypatch.setenv("ALERT_CONTEXT_LINE_ENABLED", "1")
    monkeypatch.setenv("ALERT_CONTEXT_LINE_COOLDOWN_SEC", "300")

    store = _FakeStore({"fut:scores": json.dumps({"regime": "BULL"})})

    a = build_alert_context_line(store, "SPY", alert_id="X")
    b = build_alert_context_line(store, "SPY", alert_id="X")

    assert a is not None
    assert b is None


def test_context_line_disabled(monkeypatch) -> None:
    monkeypatch.delenv("ALERT_CONTEXT_LINE_ENABLED", raising=False)
    store = _FakeStore({})
    assert build_alert_context_line(store, "SPY") is None
