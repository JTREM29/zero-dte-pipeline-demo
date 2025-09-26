from __future__ import annotations
import time
from src.datafeeds.iqfeed_client import should_reconnect


def test_should_reconnect_basic():
    now = time.time()
    assert not should_reconnect(now - 5, now, 20)
    assert should_reconnect(now - 25, now, 20)


def test_should_reconnect_edge():
    now = time.time()
    # Exactly threshold -> True
    assert should_reconnect(now - 10, now, 10)