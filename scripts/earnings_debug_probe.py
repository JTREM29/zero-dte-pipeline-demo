from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone


# Ensure workspace root is importable when running as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _decode(b):
    if b is None:
        return None
    if isinstance(b, (bytes, bytearray)):
        return b.decode("utf-8", errors="replace")
    return str(b)


async def main() -> int:
    # Reuse the repo's Redis factory so env/config stays consistent.
    from services.redis_env import redis_client
    from delivery import discord_bot as delivery
    from services.calendar.calendar_keys import cal_earnings_key
    from services.calendar.calendar_service import CalendarService
    from services.calendar.earnings_options import compute_expected_move_from_chain_df
    from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings

    sym = (os.getenv("SYM") or os.getenv("SYMBOL") or "AAPL").strip().upper()

    r = redis_client(timeout_s=0.5, decode_responses=False)
    cal = CalendarService(r)

    print("=== earnings_debug_probe ===")
    print("sym", sym)

    # Cached earnings blob
    key = cal_earnings_key(sym)
    raw = r.get(key)
    print("cal_key", key, "exists", bool(raw))
    if raw:
        raw_s = _decode(raw) or ""
        try:
            obj = json.loads(raw_s)
        except Exception as exc:  # noqa: BLE001
            print("cal_blob_parse_error", type(exc).__name__, exc)
            obj = None

        if isinstance(obj, dict):
            print("ts_utc", obj.get("ts_utc"))
            print("confirmed", obj.get("confirmed"), "session", obj.get("session"), "source", obj.get("source"))
            print("refreshed_utc", obj.get("refreshed_utc"))
            em_pct = obj.get("expected_move_pct")
            em = obj.get("expected_move") if isinstance(obj.get("expected_move"), dict) else {}
            hist = obj.get("history") if isinstance(obj.get("history"), list) else []
            have_move = any(isinstance(x, dict) and x.get("move_pct") is not None for x in hist)
            print("expected_move_pct", em_pct)
            print("expected_move_bounds", em.get("lower"), em.get("upper"))
            print("history_n", len(hist), "has_move_pct", have_move)
        else:
            print("cal_blob_type", type(obj).__name__)

    # Earnings history keys
    zkey = f"earn:tick:{sym}"
    try:
        zcard = r.zcard(zkey)
    except Exception:
        zcard = None
    print("earn_history_zkey", zkey, "zcard", zcard)

    try:
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        ids = r.zrevrangebyscore(zkey, now_epoch, "-inf", start=0, num=5)
    except Exception:
        ids = []
    ids_s = [(_decode(x) or "") for x in (ids or [])]
    ids_s = [x for x in ids_s if x]
    if ids_s:
        print("earn_history_recent_ids", ",".join(ids_s))

    # Inspect the most recent earn:item payload (to see if move_pct/gap_pct are available there)
    if ids_s:
        eid0 = ids_s[0]
        try:
            raw_it = r.get(f"earn:item:{eid0}".encode("utf-8"))
        except Exception:
            raw_it = None
        if raw_it:
            try:
                s_it = _decode(raw_it) or ""
                obj_it = json.loads(s_it)
            except Exception as exc:  # noqa: BLE001
                print("earn_item_parse_error", type(exc).__name__, exc)
                obj_it = None
            if isinstance(obj_it, dict):
                print("earn_item_0_id", eid0)
                print("earn_item_0_ts_utc", obj_it.get("ts_utc"))
                print("earn_item_0_move_pct", obj_it.get("move_pct"), "gap_pct", obj_it.get("gap_pct"))
                try:
                    print("earn_item_0_keys", ",".join(sorted([str(k) for k in obj_it.keys()])[:30]))
                except Exception:
                    pass

    # ATM straddle probe
    api_key, _base_url, _provider = delivery._polygon_key_and_base()
    print("polygon_key_present", bool(api_key))
    if api_key:
        try:
            timeout_s = float(os.getenv("EARNINGS_OPTIONS_ENRICH_SINGLE_TIMEOUT", "8"))
        except Exception:
            timeout_s = 8.0

        try:
            df = await asyncio.wait_for(delivery._fetch_polygon_atm_straddle_df(sym), timeout=max(1.0, float(timeout_s)))
        except Exception as exc:  # noqa: BLE001
            print("atm_straddle_df_error", type(exc).__name__, exc)
            df = None

        print("atm_straddle_df_none", df is None)
        if df is not None:
            try:
                exp = str(df["expiration"].iloc[0]) if "expiration" in df.columns else "n/a"
            except Exception:
                exp = "n/a"
            print("atm_straddle_df_rows", len(df), "exp", exp)
            res = compute_expected_move_from_chain_df(df)
            print("atm_straddle_expected_move", res)

    # Benzinga backfill probe
    massive_key, base_url, _provider = delivery._massive_key_and_base()
    print("massive_key_present", bool(massive_key), "base_url_present", bool(base_url))
    if massive_key:
        now_utc = datetime.now(timezone.utc)
        days = 420
        try:
            days = int(os.getenv("EARNINGS_REACTIONS_LOOKBACK_DAYS", "420"))
        except Exception:
            days = 420
        days = max(120, min(900, int(days)))

        try:
            recs = await fetch_benzinga_earnings(
                base_url=base_url,
                api_key=massive_key or "",
                tickers=[sym],
                start_date=(now_utc - timedelta(days=days)).date().isoformat(),
                end_date=(now_utc + timedelta(days=30)).date().isoformat(),
                limit=120,
                timeout_s=10.0,
            )
        except Exception as exc:  # noqa: BLE001
            print("benzinga_backfill_error", type(exc).__name__, exc)
            recs = []

        print("benzinga_backfill_records", len(recs))
        if recs:
            print("benzinga_first", recs[0].symbol, recs[0].ts_utc.isoformat())
            print("benzinga_last", recs[-1].symbol, recs[-1].ts_utc.isoformat())

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
