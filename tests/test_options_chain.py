"""Tests for options_chain parsing and filtering."""
from __future__ import annotations
from datetime import datetime
from src.datafeeds.options_chain import parse_occ_symbol, filter_chain


def test_parse_occ_symbol_basic():
    sym = "SPXW250925C00045000"  # root SPXW, 2025-09-25, Call, strike 4500
    c = parse_occ_symbol(sym)
    assert c is not None
    assert c.root == "SPXW"
    assert c.call_put == "C"
    assert abs(c.strike - 4500) < 0.01
    assert c.expiry.year == 2025


def test_filter_chain_window():
    symbols = [
        "SPXW250925C00045000",
        "SPXW250925P00045500",
        "SPXW250925C00050000",
        "SPXW250925P00060000",
        "RUT250925C00020000",  # different root
    ]
    filtered = filter_chain(symbols, root="SPXW", center_strike=4550, width=100)
    strikes = sorted([c.strike for c in filtered])
    assert 4500 in strikes
    assert 4550 in strikes or 4550.0 in strikes
    assert 5000 not in strikes
