from __future__ import annotations


def cal_earnings_key(sym: str) -> str:
    return f"cal:earnings:{str(sym or '').strip().upper()}"


def cal_earnings_refreshed_key(sym: str) -> str:
    return f"cal:earnings:refreshed_utc:{str(sym or '').strip().upper()}"


def cal_earnings_last_refresh_key() -> str:
    return "cal:earnings:last_refresh_utc"


def earnings_warm_last_ok_ts_key() -> str:
    """Last successful earnings warm (unix ts, UTC)."""

    return "earnings:warm:last_ok_ts"


def earnings_warm_last_err_key() -> str:
    """Last earnings warm error (short string)."""

    return "earnings:warm:last_err"


def earnings_warm_lock_key() -> str:
    """Best-effort distributed lock to prevent concurrent warms."""

    return "earnings:warm:lock"


def earnings_qcount_24h_zset_key() -> str:
    """Rolling last-24h-ish earnings query counts.

    NOTE: This is an approximate 24h window implemented via a single ZSET
    with a TTL that is refreshed on writes.
    """

    return "earnings:qcount:24h"
