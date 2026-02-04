from types import SimpleNamespace

from technicals.divergence import _fmt_oi_wall_line, _oi_tape_read


def test_oi_line_and_tape_read_bearish_into_call_wall():
    walls = SimpleNamespace(call_wall=485.0, put_wall=480.0, ts_et="10:00", expiry="2026-01-21")

    line = _fmt_oi_wall_line("SPY", 484.6, walls)
    assert "OI Walls" in line
    assert "Call wall" in line
    assert "Put wall" in line

    read = _oi_tape_read("bearish_rsi_divergence", 484.9, walls)
    assert "into call wall" in read


def test_oi_tape_read_far_walls_is_quiet():
    walls = SimpleNamespace(call_wall=520.0, put_wall=450.0)
    read = _oi_tape_read("bearish_rsi_divergence", 485.0, walls)
    assert read == ""


def test_oi_line_unavailable():
    line = _fmt_oi_wall_line("SPY", 485.0, None)
    assert "unavailable" in line.lower()
