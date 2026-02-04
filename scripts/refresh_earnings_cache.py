from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import redis

# Allow running this script from any working directory (e.g. from `scripts/`).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from delivery.discord_bot import _massive_key_and_base
from services.calendar.calendar_service import CalendarService
from services.calendar.calendar_keys import cal_earnings_last_refresh_key, cal_earnings_refreshed_key
from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings, pick_next_earnings


def _redis_client() -> redis.Redis:
    host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("TNT_REDIS_PORT", "6379"))
    db = int(os.getenv("TNT_REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


async def main_async(symbols: list[str], *, limit: int = 250, force: bool = False) -> None:
    api_key, base_url, _provider = _massive_key_and_base()
    if not api_key:
        raise RuntimeError("refresh_earnings_cache requires an external earnings feed (set ZERO_DTE_USE_MASSIVE=1 + MASSIVE_API_KEY)")

    try:
        from services.calendar.calendar_keys import earnings_warm_last_err_key, earnings_warm_last_ok_ts_key
    except Exception:
        earnings_warm_last_err_key = None  # type: ignore[assignment]
        earnings_warm_last_ok_ts_key = None  # type: ignore[assignment]

    now = datetime.now(timezone.utc)

    # Bound the query to a forward-looking window so we don't accidentally
    # cache extremely-far future dates when the upstream returns deep history.
    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone.utc

    try:
        lookahead_days = int(os.getenv("TNT_EARNINGS_LOOKAHEAD_DAYS", "180") or "180")
    except Exception:
        lookahead_days = 180
    lookahead_days = max(30, min(365, int(lookahead_days)))

    now_et = datetime.now(et)
    start_date = now_et.date().isoformat()
    end_date = (now_et.date() + timedelta(days=int(lookahead_days))).isoformat()

    records = await fetch_benzinga_earnings(
        base_url=base_url,
        api_key=api_key or "",
        tickers=symbols,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )

    r = _redis_client()
    cal = CalendarService(r)

    # Stamp refresh times even if a symbol has no upcoming earnings.
    # This makes /earnings_status reflect that we successfully polled.
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    try:
        r.set(cal_earnings_last_refresh_key(), str(now_epoch))
        if earnings_warm_last_ok_ts_key is not None:
            r.set(earnings_warm_last_ok_ts_key(), str(now_epoch))
        if earnings_warm_last_err_key is not None:
            r.delete(earnings_warm_last_err_key())
        for sym in symbols:
            s = str(sym or "").strip().upper()
            if s:
                r.set(cal_earnings_refreshed_key(s), str(now_epoch))
    except Exception:
        pass

    updated = 0
    cleared = 0
    for sym in symbols:
        nxt = pick_next_earnings(records, symbol=sym, now_utc=now)
        if nxt is None:
            if force:
                # If the feed returns no upcoming earnings for this symbol, clear stale cache.
                try:
                    from services.calendar.calendar_keys import cal_earnings_key

                    r.delete(cal_earnings_key(str(sym).strip().upper()))
                    cleared += 1
                except Exception:
                    pass
            continue
        cal.set_earnings(symbol=sym, ts_utc=nxt.ts_utc, confirmed=nxt.confirmed, source=nxt.source, ttl_sec=14 * 24 * 3600)
        updated += 1

    # Optional history: store everything we fetched per symbol.
    by_sym: dict[str, list[dict]] = {s: [] for s in symbols}
    for r0 in records:
        if r0.symbol in by_sym:
            by_sym[r0.symbol].append(r0.to_jsonable())
    for sym, batch in by_sym.items():
        if batch:
            cal.cache_earnings_history(symbol=sym, items=batch, ttl_sec=90 * 24 * 3600)

    print({"symbols": len(symbols), "updated": updated, "cleared": cleared, "total_records": len(records)})


def main() -> None:
    p = argparse.ArgumentParser(description="Refresh earnings cache via external feed")
    p.add_argument("--symbols", default=os.getenv("EARNINGS_AUTOPOST_SYMBOLS", "SPY,QQQ,IWM"), help="CSV tickers")
    p.add_argument("--symbol", default="", help="Single ticker (convenience; overrides --symbols)")
    p.add_argument("--force", action="store_true", help="Force overwrite/clear stale cache even if a record already exists")
    # Use a higher default so we can fetch enough events for the requested symbols,
    # even if an upstream ignores the filter (we also retry common param names).
    p.add_argument("--limit", type=int, default=int(os.getenv("TNT_EARNINGS_LIMIT", "2000")))
    args = p.parse_args()

    if str(args.symbol or "").strip():
        symbols = [str(args.symbol).strip().upper()]
    else:
        symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    asyncio.run(main_async(symbols, limit=int(args.limit), force=bool(args.force)))


if __name__ == "__main__":
    main()
