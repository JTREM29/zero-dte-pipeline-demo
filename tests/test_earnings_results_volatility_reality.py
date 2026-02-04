import json
from datetime import datetime

from delivery import discord_bot as delivery


class _FakeRedis:
    def __init__(self, mapping: dict[str, object] | None = None):
        self._m: dict[str, object] = dict(mapping or {})

    def get(self, key: str):
        return self._m.get(key)

    def mget(self, keys: list[str]):
        return [self._m.get(k) for k in keys]


def test_results_today_includes_volatility_reality_check(monkeypatch) -> None:
    target = "2026-01-27"

    # Earnings results payload (watchlist filtered upstream in real fetch).
    monkeypatch.setattr(
        delivery,
        "fetch_earnings_for_date",
        lambda _date_et: [
            {
                "symbol": "SPY",
                "when": "BMO",
                "eps_actual": 1.20,
                "eps_est": 1.00,
                "rev_actual": None,
                "rev_est": None,
                "confirmed": True,
            }
        ],
    )

    # Make render think earnings are configured and fetch was fine.
    monkeypatch.setattr(delivery, "_resolve_earnings_api_key", lambda: "x")
    monkeypatch.setattr(delivery, "_earnings_provider_runtime", lambda: "massive_benzinga")
    monkeypatch.setattr(
        delivery,
        "earnings_calendar_last_fetch",
        lambda: {"date_et": target, "error": "", "http_status": None, "rows": 1},
    )

    # Cache-first implied vs realized source.
    r = _FakeRedis(
        {
            "cal:earnings:SPY": json.dumps(
                {
                    "symbol": "SPY",
                    "expected_move_pct": 2.0,
                    "history": [{"move_pct": 4.0}],
                }
            )
        }
    )
    monkeypatch.setattr(delivery, "redis_client_for_ctx", lambda: r)

    now_et = datetime(2026, 1, 27, 12, 0, tzinfo=delivery.ET)
    render = delivery.build_earnings_results_today_render(target_date_et=target, now_et=now_et)

    text = render.text or ""
    assert "📊 Volatility Reality Check" in text
    assert "UNDERPRICED" in text
    assert "Ratio 2.00x" in text
