from __future__ import annotations

import json
from datetime import date

import pytest


class FakeRedis:
    def __init__(self, kv=None):
        self.kv = dict(kv or {})
        self.expires = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = value
        return True

    def expire(self, key, ttl):
        self.expires[key] = int(ttl)
        return True


def test_post_noops_when_artifact_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import triple_expiry_fanout as fanout

    # Force enabled so we exercise completeness gate.
    monkeypatch.setenv("TNT_TRIPLE_EXPIRY_ENABLED", "1")

    called = {"n": 0}

    def _fake_post_message(*_a, **_k):
        called["n"] += 1
        return True

    # Patch the webhook module import target.
    monkeypatch.setattr("zero_dte_pipeline.discord.webhook.post_message", _fake_post_message, raising=False)

    d = date(2026, 1, 23).isoformat()
    sym = "NVDA"
    exp = d

    pack_key = f"pack:triple:{sym}:{exp}"
    pack = {
        "schema": "triple_expiry_pack_v1",
        "symbol": sym,
        "date_et": d,
        "ok": True,
        "reason": "ok",
        "expiry": exp,
        "price": {"ok": True, "px": 100.0},
        "renders": {"oi_iv": {"ok": True, "artifact_url": None, "out_path": None}},
    }

    r = FakeRedis({pack_key: json.dumps(pack)})

    out = fanout.post(r=r, date_et=date.fromisoformat(d), symbols=[sym], log=fanout._log_path(d))
    assert out.get("ok") is False
    assert out.get("reason") in {"no_eligible_symbols", "disabled", "quiet_hours", "global_cap"}
    assert called["n"] == 0
