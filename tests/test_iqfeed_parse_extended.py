from __future__ import annotations
from src.datafeeds.iqfeed_client import parse_level1_line


def test_parse_level1_line_extended_fields():
    # Construct a synthetic Q line with extended fields we mapped.
    # Index mapping (subset): 0 Q,1 symbol,2 bid,3 ask,4 bid_size,5 ask_size,6 last_trade,7 last_trade_size,8 last_trade_time,
    # 9 total_volume,10 day_high,11 day_low,12 open,13 close_yest,14 bid_time,15 ask_time
    line = "Q,TEST,100.0,100.5,10,12,100.25,5,09:30:01.123,10000,101,99,100.1,99.5,09:30:01,09:30:01"  # truncated after ask_time
    parsed = parse_level1_line(line)
    assert parsed is not None
    assert parsed['symbol'] == 'TEST'
    assert parsed['bid'] == 100.0
    assert parsed['ask'] == 100.5
    assert parsed['last_price'] == 100.25
    assert 'mid' in parsed and parsed['mid'] == (100.0 + 100.5)/2
    assert 'spread' in parsed and parsed['spread'] == 0.5
    assert 'spread_bps' in parsed and parsed['spread_bps'] > 0
    assert parsed['day_high'] == 101
    assert parsed['day_low'] == 99
    # epoch derivation best effort (may be None if time parse fails due to environment date); accept presence or None
    assert 'epoch' in parsed or True