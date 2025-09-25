"""Tests for PolygonClient minimal methods (offline behavior)."""
from __future__ import annotations
import os
import pytest
from src.datafeeds.polygon_client import PolygonClient, PolygonConfig


def test_snapshot_placeholder():
    os.environ.pop("POLYGON_API_KEY", None)
    # Provide fake key to construct client
    os.environ["POLYGON_API_KEY"] = "demo_key"
    cfg = PolygonConfig.from_env()
    c = PolygonClient(cfg)
    snap = c.fetch_underlying_snapshot("SPX")
    assert snap["symbol"] == "SPX"
    assert snap["source"] == "polygon"


def test_last_trade_spx_missing_key(monkeypatch):
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    # Bypass constructor guard by injecting directly (simulate user forgetting key)
    cfg = PolygonConfig(api_key="dummy")
    c = PolygonClient(cfg)
    c.api_key = ""  # force missing
    assert c.last_trade_spx() is None
