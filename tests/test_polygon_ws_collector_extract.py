from __future__ import annotations


def test_polygon_ws_extract_trade_tick() -> None:
    from massive_service import polygon_ws_collector as ws

    tick = ws._extract_tick({"sym": "SPY", "p": 123.4, "t": 1735230000000})
    assert tick is not None
    assert tick.symbol == "SPY"
    assert tick.price == 123.4
    assert tick.source.startswith("polygon-ws")


def test_polygon_ws_extract_quote_midpoint() -> None:
    from massive_service import polygon_ws_collector as ws

    tick = ws._extract_tick({"sym": "SPY", "bp": 100.0, "ap": 102.0, "t": 1735230000000})
    assert tick is not None
    assert tick.price == 101.0
    assert tick.source == "polygon-ws:Q"


def test_polygon_ws_extract_index_value() -> None:
    from massive_service import polygon_ws_collector as ws

    tick = ws._extract_tick({"sym": "I:SPX", "v": 5678.0, "t": 1735230000000})
    assert tick is not None
    assert tick.symbol == "I:SPX"
    assert tick.price == 5678.0
    assert tick.source == "polygon-ws:V"
