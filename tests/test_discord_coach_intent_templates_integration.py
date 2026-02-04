import json
import re
from dataclasses import dataclass
from typing import Any

import pytest

import delivery.discord_bot as bot


_BANNED = re.compile(r"\b(signal|call|entry|prediction|guaranteed|will)\b", re.IGNORECASE)
_HEADERS = (
    "Futures (ES/NQ/RTY)",
    "Market Context",
    "AI Option Structure Candidate",
    "Greeting —",
)


@dataclass
class _CachedOpt:
    packet: dict[str, Any]


class FakeRedis:
    def __init__(self, mapping: dict[str, Any]):
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


def _assert_locked_common(text: str) -> None:
    assert isinstance(text, str) and text.strip()
    assert text.splitlines()[0].startswith("Context:")
    assert "Bottom line:" in text
    assert "Triggers:" in text
    assert "Confidence:" in text
    assert "unknown" not in text.lower()

    # Hard bans
    assert "ctx:" not in text.lower()
    assert "missing required inputs" not in text.lower()
    assert not _BANNED.search(text)


def _assert_exactly_one_template(text: str, *, expected_header_prefix: str) -> None:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    assert len(lines) >= 2

    # The locked spec always starts with Context:, then the template header.
    header = lines[1]
    assert header.startswith(expected_header_prefix)

    # Guard against accidental double-responses.
    found = []
    for ln in lines[1:]:
        for h in _HEADERS:
            if ln.startswith(h):
                found.append(h)

    # Symbol template is dynamic; handle separately.
    if expected_header_prefix.endswith(" Context") or expected_header_prefix.endswith(" Context —"):
        # No extra market/futures/candidate headers allowed.
        assert "Market Context" not in "\n".join(lines[2:])
        assert "Futures (ES/NQ/RTY)" not in "\n".join(lines[2:])
        assert "AI Option Structure Candidate" not in "\n".join(lines[2:])
    else:
        # For fixed headers, ensure we only see the expected one.
        assert found.count(expected_header_prefix.rstrip(" —")) in (0, 1)
        others = [h for h in found if not header.startswith(h)]
        assert not others


@pytest.fixture()
def _stub_external(monkeypatch: pytest.MonkeyPatch):
    # Force ctx snapshots on.
    monkeypatch.setenv("CTX_SNAPSHOT_ENABLED", "1")
    try:
        import services.context.ctx_mode as ctx_mode

        monkeypatch.setattr(ctx_mode, "ctx_enabled", lambda: True)
    except Exception:
        pass

    # Avoid any external data calls (analyze, trend, options chain).
    async def _fake_run_analyze_for_symbol(symbol: str):
        # Keep it minimal; templates tolerate UNKNOWNs.
        payload = {
            "meta": {
                "signal_payload": {
                    "bias_confirm": "NEUTRAL",
                    "tnt": {"confidence": 0.5},
                }
            }
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
    monkeypatch.setattr(bot, "_get_cached_options_micro", lambda sym: _CachedOpt(packet={}))


@pytest.mark.asyncio
async def test_futures_overview_template_integration(_stub_external, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRedis(
        {
            "ctx:market": {
                "ts_utc": 1700000000,
                "sources_present": {"futures": True, "news": True},
                "futures": {
                    "bias": "BULLISH",
                    "regime": "RANGE",
                    "hb_age_sec": 10,
                    "updated_utc": 1700000000,
                    "degraded": False,
                },
            }
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach("SPY", "how do the futures look")
    _assert_locked_common(text)
    _assert_exactly_one_template(text, expected_header_prefix="Futures (ES/NQ/RTY)")


@pytest.mark.asyncio
async def test_market_context_template_integration(_stub_external, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRedis(
        {
            "ctx:market": {
                "ts_utc": 1700000000,
                "sources_present": {"futures": True, "news": False},
                "futures": {"hb_age_sec": 10, "degraded": False},
            }
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    # Do not provide a symbol: MARKET_CONTEXT should stay market-only.
    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach("", "what’s the market doing")
    _assert_locked_common(text)
    _assert_exactly_one_template(text, expected_header_prefix="Market Context")


@pytest.mark.asyncio
async def test_greeting_template_integration(_stub_external, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRedis(
        {
            "ctx:market": {
                "ts_utc": 1700000000,
                "sources_present": {"futures": True, "news": False},
                "futures": {"hb_age_sec": 10, "degraded": False},
            }
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach("", "hello")
    _assert_locked_common(text)
    _assert_exactly_one_template(text, expected_header_prefix="Greeting —")


@pytest.mark.asyncio
async def test_symbol_context_template_integration_stopwords_not_symbol(_stub_external, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRedis(
        {
            "ctx:market": {"ts_utc": 1700000000, "sources_present": {"futures": True, "news": True}},
            "ctx:sym:SPY": {"ts_utc": 1700000001, "candidates": {"status": "NONE"}},
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach("", "does spy look weak today")
    _assert_locked_common(text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    assert lines[1].startswith("SPY Context")
    assert "DOES Context" not in text

    # Extra guard: should not resolve stopwords as symbol.
    assert "HOW Context" not in text


@pytest.mark.asyncio
async def test_candidate_request_template_integration_approved(_stub_external, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRedis(
        {
            "ctx:market": {"ts_utc": 1700000000, "sources_present": {"futures": True, "news": True}},
            "ctx:sym:SPY": {"ts_utc": 1700000001},
            "ctx:sym:SPY.candidates": {
                "status": "APPROVED",
                "as_of_et": "2026-01-20 09:45 ET",
                "age_s": 12,
                "confidence": "MED",
                "top": {"strategy": "iron_condor", "direction": "NEUTRAL"},
            },
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach("SPY", "any candidates for $SPY")
    _assert_locked_common(text)
    _assert_exactly_one_template(text, expected_header_prefix="AI Option Structure Candidate")
    assert "this is a candidate, not a command." in text.lower()


@pytest.mark.asyncio
async def test_futures_stale_or_scores_missing_graceful_degrade(_stub_external, monkeypatch: pytest.MonkeyPatch):
    # scores missing (sources_present.futures=False) should degrade.
    fake = FakeRedis(
        {
            "ctx:market": {
                "ts_utc": 1700000000,
                "sources_present": {"futures": False, "news": True},
                "futures": {"hb_age_sec": 999, "degraded": True},
            }
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach("SPY", "how do the futures look")
    _assert_locked_common(text)
    assert "not available with confidence" in text.lower()


@pytest.mark.asyncio
async def test_symbol_snapshot_missing_graceful_degrade(_stub_external, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRedis(
        {
            "ctx:market": {"ts_utc": 1700000000, "sources_present": {"futures": True, "news": True}},
            # Intentionally omit ctx:sym:SPY
        }
    )
    monkeypatch.setattr(bot, "redis_client_for_ctx", lambda: fake)

    text, _render, _status, _cache_hit, _lat = await bot.run_analyze_then_coach("SPY", "SPY")
    _assert_locked_common(text)
    assert re.search(r"won.?t claim symbol positioning", text, flags=re.IGNORECASE)
    assert "ctx:" not in text.lower()
    assert "missing required inputs" not in text.lower()
