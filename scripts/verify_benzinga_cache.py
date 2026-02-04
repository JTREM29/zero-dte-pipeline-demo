from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

import redis

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = timezone.utc


def _redis_client() -> redis.Redis:
    host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("TNT_REDIS_PORT", "6379"))
    db = int(os.getenv("TNT_REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _fmt_epoch(ts: str | None) -> str:
    if not ts:
        return "(none)"
    try:
        t = int(float(ts))
    except Exception:
        return str(ts)
    dt_utc = datetime.fromtimestamp(t, tz=timezone.utc)
    dt_et = dt_utc.astimezone(ET)
    age_s = int((datetime.now(timezone.utc) - dt_utc).total_seconds())
    return f"{t} | utc={dt_utc.isoformat()} | et={dt_et.strftime('%Y-%m-%d %H:%M:%S')} | age_s={age_s}"


def main() -> None:
    p = argparse.ArgumentParser(description="Verify external news/earnings cache keys in Redis")
    p.add_argument("--symbol", default="SPY", help="Underlying symbol, e.g. SPY")
    p.add_argument("--show-latest-item", action="store_true", help="Also print the latest news item JSON")
    args = p.parse_args()

    sym = (args.symbol or "").strip().upper()
    if not sym:
        raise SystemExit("--symbol required")

    r = _redis_client()

    news_last_ts = r.get(f"news:symbol:{sym}:last_ts")
    news_last_title = r.get(f"news:symbol:{sym}:last_title")
    news_zset = f"news:tick:{sym}"
    news_zcard = r.zcard(news_zset)

    market_last_ts = r.get("news:market:last_ts")
    market_last_title = r.get("news:market:last_title")

    earnings_key = f"cal:earnings:{sym}"
    earnings_raw = r.get(earnings_key)

    print(f"redis host={r.connection_pool.connection_kwargs.get('host')} port={r.connection_pool.connection_kwargs.get('port')} db={r.connection_pool.connection_kwargs.get('db')}")

    print(f"news last ts ({sym})", _fmt_epoch(news_last_ts))
    print(f"news last title ({sym})", news_last_title or "(none)")
    print(f"news zset size ({sym})", news_zcard)

    if args.show_latest_item:
        latest_id = None
        try:
            rows = r.zrevrange(news_zset, 0, 0)
            latest_id = rows[0] if rows else None
        except Exception:
            latest_id = None
        if latest_id:
            blob = r.get(f"news:item:{latest_id}")
            try:
                obj = json.loads(blob) if blob else None
            except Exception:
                obj = blob
            print("news latest id", latest_id)
            print("news latest item", json.dumps(obj, indent=2) if isinstance(obj, dict) else obj)

    print("market news last ts", _fmt_epoch(market_last_ts))
    print("market news last title", market_last_title or "(none)")

    print(f"earnings raw ({sym})", earnings_raw or "(none)")
    if earnings_raw:
        try:
            obj = json.loads(earnings_raw)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            ts = str(obj.get("ts_utc") or "")
            confirmed = obj.get("confirmed")
            source = obj.get("source")
            print("earnings parsed ts_utc", ts or "(none)")
            print("earnings parsed confirmed", confirmed)
            src = str(source or "")
            if any(tok in src.lower() for tok in ("benzinga", "massive", "polygon", "databento")):
                src = "EXTERNAL"
            print("earnings parsed source", src or "(none)")


if __name__ == "__main__":
    main()
