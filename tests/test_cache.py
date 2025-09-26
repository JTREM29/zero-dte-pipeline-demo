"""Tests for in-memory cache abstraction."""
from __future__ import annotations
import time
from src.cache import InMemoryCache


def test_cache_set_get_and_expire():
    c = InMemoryCache()
    c.set("k","v", ttl=0.2)
    assert c.get("k") == "v"
    time.sleep(0.25)
    assert c.get("k") is None


def test_cache_purge():
    c = InMemoryCache()
    c.set("a",1, ttl=0.01)
    c.set("b",2, ttl=1.0)
    time.sleep(0.05)
    removed = c.purge()
    assert removed == 1
    assert c.get("a") is None and c.get("b") == 2
