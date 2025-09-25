"""Tests for the enhanced IQFeed Level1 parser."""
from __future__ import annotations
from src.datafeeds.iqfeed_client import parse_level1_line


def test_parse_level1_line_basic():
    line = "Q,SPY,500.12,500.15,100,200,500.13,50,09:35:10,100000,501.00,499.00"
    parsed = parse_level1_line(line)
    assert parsed is not None
    assert parsed["symbol"] == "SPY"
    assert parsed["bid"] == 500.12
    assert parsed["ask_size"] == 200
    assert parsed["day_high"] == 501.00
    assert parsed["day_low"] == 499.00


def test_parse_level1_line_rejects():
    assert parse_level1_line("") is None
    assert parse_level1_line("S,SERVER CONNECTED") is None
    assert parse_level1_line("X,SPY,blah") is None
