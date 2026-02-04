def fut_last_key(sym: str) -> str:
    return f"fut:{sym}:last"          # hash: px, chg, chg_pct, ts_utc, source


def fut_bar_key(sym: str, tf: str) -> str:
    return f"fut:{sym}:bars:{tf}"     # list or zset (implementation dependent)


def fut_intel_key() -> str:
    return "fut:intel"                # hash/json: regime, breadth, vol_mult, updated_utc


def fut_scores_key() -> str:
    return "fut:scores"               # hash/json: ES trend, NQ impulse, etc.


def fut_heartbeat_key() -> str:
    return "fut:hb"                   # string/int: last update epoch


def fut_heartbeat_msg_key() -> str:
    return "fut:hb_msg"               # string: last heartbeat status message


def fut_status_key() -> str:
    return "fut:status"               # string/json: ingest status + last error
