import json
import re

import pytest

import delivery.discord_bot as bot


_BANNED = re.compile(r"\b(signal|call|entry|prediction|guaranteed|will)\b", re.IGNORECASE)


class FakeRedis:
    def __init__(self, mapping: dict):
        # Store values as Redis would: strings.
        self._kv: dict[str, str] = {}
        for k, v in mapping.items():
            if isinstance(v, str):
                self._kv[k] = v
            else:
                self._kv[k] = json.dumps(v)

    def get(self, key: str):
        return self._kv.get(key)

    def exists(self, key: str) -> int:
        return 1 if key in self._kv else 0


@pytest.fixture()
def _stub_external(monkeypatch: pytest.MonkeyPatch):
    # Force ctx snapshots on.
    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "1")
    try:
        import services.context.ctx_mode as ctx_mode

        monkeypatch.setattr(ctx_mode, "ctx_enabled", lambda: True)
    except Exception:
        pass

    async def _fake_run_analyze_for_symbol(symbol: str):
        # Minimal analysis payload; TNT_STATE builder is robust.
        payload = {
            "symbol": symbol,
            "meta": {
                "signal_payload": {
                    "bias_confirm": "NEUTRAL",
                    "tnt": {"confidence": 0.5},
                },
                "price_context": {
                    "last_price": 100.0,
                    "last_price_ts": "2026-01-26T20:09:00+00:00",
                    "source": "stub",
                    "mode": "stub",
                },
            },
        }
        return "", payload

    async def _fake_recent_trend(symbol: str, sessions: int = 5):
        return None

    async def _fake_fetch_chain_df(symbol: str):
        return None

    monkeypatch.setattr(bot, "_run_analyze_for_symbol", _fake_run_analyze_for_symbol)
    monkeypatch.setattr(bot, "_compute_recent_daily_trend", _fake_recent_trend)
    monkeypatch.setattr(bot, "_fetch_polygon_options_chain_df", _fake_fetch_chain_df)

    # Make sure options micro cache doesn't cause surprises.
    monkeypatch.setattr(bot, "_get_cached_options_micro", lambda sym: None)


def _assert_gold(text: str, expected_header_prefix: str) -> None:
    assert isinstance(text, str) and text.strip()
    assert text.splitlines()[0].startswith("Context:")
    assert "Bottom line:" in text
    assert "Triggers:" in text
    assert "Confidence:" in text
    assert "unknown" not in text.lower()
    assert "ctx:" not in text.lower()
    assert not _BANNED.search(text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    assert len(lines) >= 2
    assert lines[1].startswith(expected_header_prefix)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provided_symbol, question, expected_header_prefix",
    [
        ("", "hello", "Greeting —"),
        ("", "gm", "Greeting —"),
        ("", "how do the futures look", "Futures (ES/NQ/RTY)"),
        ("", "futes update", "Futures (ES/NQ/RTY)"),
        ("", "what’s the market doing", "Market Context"),
        ("", "market context please", "Market Context"),
        ("", "What did spy do today?", "SPY Context"),
        ("", "what did qqq do today?", "QQQ Context"),
        ("", "does spy look weak today", "SPY Context"),
        ("SPY", "what happened today", "SPY Context"),
        ("SPY", "key levels?", "SPY Context"),
        ("", "any candidates for $SPY", "AI Option Structure Candidate"),
        ("", "What strike and expiry should I use on QQQ?", "Trade Actions —"),
        ("", "0dte structure for IWM", "Trade Actions —"),
        ("", "iron condor on SPY?", "AI Option Structure Candidate"),
        ("", "expiration for QQQ?", "Trade Actions —"),
        ("", "regime and bias?", "Market Context"),
        ("", "futures", "Futures (ES/NQ/RTY)"),
        ("", "qqq structure", "AI Option Structure Candidate"),
        ("", "spy candidates", "AI Option Structure Candidate"),
    ],
)
async def test_golden_questions_templates(_stub_external, monkeypatch: pytest.MonkeyPatch, provided_symbol: str, question: str, expected_header_prefix: str):
    fake = FakeRedis(
        {
            "ctx:market": {
                "ts_utc": 1700000000,
                "sources_present": {"futures": True, "news": True},
                "futures": {
                    "bias": "NEUTRAL",
                    "regime": "RANGE",
                    "hb_age_sec": 10,
                    "updated_utc": 1700000000,
                    "degraded": False,
                },
            },
            "ctx:sym:SPY": {"ts_utc": 1700000001},
            "ctx:sym:QQQ": {"ts_utc": 1700000001},
            "ctx:sym:IWM": {"ts_utc": 1700000001},
            "ctx:sym:SPY.candidates": {"status": "NONE", "as_of_et": "2026-01-26 15:09 ET", "age_s": 12},
            "ctx:sym:QQQ.candidates": {"status": "NONE", "as_of_et": "2026-01-26 15:09 ET", "age_s": 12},
            "ctx:sym:IWM.candidates": {"status": "NONE", "as_of_et": "2026-01-26 15:09 ET", "age_s": 12},
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach(provided_symbol, question)
    _assert_gold(text, expected_header_prefix=expected_header_prefix)
