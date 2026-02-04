from __future__ import annotations

from services.calendar.earnings_embeds import build_earnings_embed
from services.calendar.earnings_miss_gate import should_show_retrieving_once


class _FakeRedis:
    def __init__(self):
        self._kv: dict[str, bytes] = {}

    def get(self, key: str):
        return self._kv.get(key)

    def setex(self, key: str, ttl: int, value: str):
        self._kv[key] = str(value).encode("utf-8")
        return True


def test_first_miss_gate_only_once() -> None:
    r = _FakeRedis()
    assert should_show_retrieving_once(r, "SOFI", ttl_sec=999) is True
    assert should_show_retrieving_once(r, "SOFI", ttl_sec=999) is False


def test_build_earnings_embed_sparse_mode_is_stable() -> None:
    e = build_earnings_embed("SOFI", None, refreshed_utc=None, price_line="Price: unavailable (no recent price)", missing_mode="sparse")
    assert "Earnings" in (e.title or "")
    fields = {f.name: f.value for f in (e.fields or [])}
    assert "Next earnings" in fields
    assert "Not yet announced" in str(fields.get("Next earnings"))
    assert "Latest News" in fields
    assert "Last 4 earnings reactions" in fields
