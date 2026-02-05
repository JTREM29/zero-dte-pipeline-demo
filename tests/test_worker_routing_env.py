import os

import delivery.worker_routing as wr


def test_pick_worker_url_spy_hits_xl(monkeypatch):
    monkeypatch.setenv("TNT_WORKER_URL_XL", "http://xl:8787/")
    monkeypatch.setenv("TNT_WORKER_URL_2", "http://w2:8787")
    assert wr.pick_worker_url("SPY", payload={"symbol": "SPY"}).startswith("http://xl")


def test_pick_worker_url_wide_fanout_hits_xl(monkeypatch):
    monkeypatch.setenv("TNT_WORKER_URL_XL", "http://xl:8787")
    monkeypatch.setenv("TNT_WORKER_URL_2", "http://w2:8787")

    url = wr.pick_worker_url("AAPL", payload={"symbol": "AAPL", "max_expiries": 12})
    assert url.startswith("http://xl")


def test_pick_worker_url_round_robin_when_secondary_present(monkeypatch):
    monkeypatch.setenv("TNT_WORKER_URL_XL", "http://xl:8787")
    monkeypatch.setenv("TNT_WORKER_URL_2", "http://w2:8787")

    # Force deterministic time for the modulo rule.
    monkeypatch.setattr(wr.time, "time", lambda: 100.0)
    assert wr.pick_worker_url("AAPL", payload={"symbol": "AAPL"}).startswith("http://w2")

    monkeypatch.setattr(wr.time, "time", lambda: 101.0)
    assert wr.pick_worker_url("AAPL", payload={"symbol": "AAPL"}).startswith("http://xl")


def test_pick_worker_url_falls_back_when_no_secondary(monkeypatch):
    monkeypatch.setenv("TNT_WORKER_URL_XL", "http://xl:8787")
    monkeypatch.delenv("TNT_WORKER_URL_2", raising=False)
    assert wr.pick_worker_url("AAPL", payload={"symbol": "AAPL"}).startswith("http://xl")


def test_pick_worker_url_legacy_fallback(monkeypatch):
    monkeypatch.delenv("TNT_WORKER_URL_XL", raising=False)
    monkeypatch.delenv("TNT_WORKER_URL_2", raising=False)
    monkeypatch.setenv("TNT_WORKER_URL", "http://legacy:8787")
    assert wr.pick_worker_url("AAPL", payload={"symbol": "AAPL"}).startswith("http://legacy")
