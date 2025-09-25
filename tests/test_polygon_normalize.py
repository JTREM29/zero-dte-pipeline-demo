"""Tests for normalization and caching logic of Polygon client (offline).

Network calls are not executed here— we simulate payloads.
"""
from __future__ import annotations
from src.datafeeds.polygon_client import PolygonClient, PolygonConfig
import pandas as pd


def _client():
    return PolygonClient(PolygonConfig(api_key="demo"))


def test_normalize_prev_agg_success():
    payload = {"results": [{"o":1,"h":2,"l":0.5,"c":1.5,"v":1000,"t":1234567890}]}
    df = _client().normalize_prev_agg(payload)
    assert df is not None
    assert set(["open","high","low","close","volume","timestamp"]).issubset(df.columns)


def test_normalize_prev_agg_empty():
    assert _client().normalize_prev_agg({}) is None


def test_cache_store_and_hit():
    c = _client()
    c._store_cache("spx_prev", {"a":1})  # type: ignore[attr-defined]
    res = c._cached("spx_prev")  # type: ignore[attr-defined]
    assert res == {"a":1}
