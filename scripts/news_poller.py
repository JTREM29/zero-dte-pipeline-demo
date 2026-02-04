from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import redis

from dotenv import load_dotenv

from delivery.discord_bot import _massive_key_and_base
from services.news.massive_benzinga_news import fetch_benzinga_news
from services.news.news_rules import classify_news
from services.news.news_service import NewsService
from services.observability.feed_heartbeat import write_feed_heartbeat


_POLLER_LOCK: object | None = None


def _maybe_load_repo_dotenv() -> None:
    """Load .env/.env.local relative to repo root.

    This script is commonly started outside the bot's runners, so we need to
    ensure TNT_REDIS_* and Massive/Benzinga keys are present.
    """

    if str(os.getenv("TNT_DOTENV_DISABLE", "0") or "0").strip().lower() in {"1", "true", "yes"}:
        return

    try:
        repo_root = Path(__file__).resolve().parents[1]
    except Exception:
        repo_root = Path.cwd()

    env_path = repo_root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    env_local_path = repo_root / ".env.local"
    if env_local_path.exists():
        load_dotenv(env_local_path, override=True)


def _acquire_singleton_lock() -> bool:
    """Best-effort single-instance lock.

    Prevents accidentally running multiple news pollers that stamp competing
    cursors and cache freshness keys.

    Override with NEWS_POLLER_ALLOW_MULTI=1.
    """

    allow_multi = str(os.getenv("NEWS_POLLER_ALLOW_MULTI", "0") or "0").strip().lower() in {"1", "true", "yes"}
    if allow_multi:
        return True

    try:
        lock_dir = Path("logs")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "tnt_news_poller.lock"
        fh = open(lock_path, "a+", encoding="utf-8")

        try:
            fh.seek(0)
        except Exception:
            pass

        try:
            import msvcrt  # type: ignore

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                try:
                    fh.close()
                except Exception:
                    pass
                return False
        except Exception:
            try:
                import fcntl  # type: ignore

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    try:
                        fh.close()
                    except Exception:
                        pass
                    return False
            except Exception:
                return True

        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(f"pid={os.getpid()}\n")
            fh.flush()
        except Exception:
            pass

        global _POLLER_LOCK
        _POLLER_LOCK = fh
        return True
    except Exception:
        return True


def _redis_client() -> redis.Redis:
    host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("TNT_REDIS_PORT", "6379"))
    db = int(os.getenv("TNT_REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


async def _tick(*, symbols: list[str], market: bool, limit: int) -> tuple[int, int, int, list[str], int]:
    api_key, base_url, _provider = _massive_key_and_base()
    if not api_key:
        raise RuntimeError(
            "news_poller requires a news feed integration (set ZERO_DTE_USE_MASSIVE=1 and MASSIVE_API_KEY; POLYGON_API_KEY may be used as fallback if Massive shares the key)"
        )

    r = _redis_client()
    svc = NewsService(r)

    classified_n = 0
    material_n = 0

    def _decorate(js: dict) -> dict:
        if not isinstance(js, dict):
            return js
        try:
            tags, severity = classify_news(js)
            js = dict(js)
            did_classify = False
            if tags and "tags" not in js:
                js["tags"] = tags
                did_classify = True
            if severity and "severity" not in js:
                js["severity"] = severity
                did_classify = True

            nonlocal classified_n, material_n
            if did_classify:
                classified_n += 1
            if tags or (severity == "HIGH"):
                material_n += 1
            return js
        except Exception:
            return js

    # Cursor is a best-effort optimization.
    cursor_key = "news:last_cursor_ts"
    cursor_s = None
    try:
        raw = r.get(cursor_key)
        cursor_s = int(raw) if raw else None
    except Exception:
        cursor_s = None

    since = None
    if cursor_s is not None:
        since = datetime.fromtimestamp(max(cursor_s - 5, 0), tz=timezone.utc)

    stored_total = 0
    market_items_written = 0
    symbols_written: set[str] = set()

    if market:
        items = await fetch_benzinga_news(
            base_url=base_url,
            api_key=api_key or "",
            tickers=None,
            published_since_utc=since,
            limit=limit,
        )
        market_batch = [_decorate(x.to_jsonable()) for x in items]
        wrote = svc.cache_market_items(items=market_batch, ttl_sec=24 * 3600)
        market_items_written += int(wrote or 0)
        stored_total += int(wrote or 0)

        # Also populate per-symbol history from market-wide items for the watchlist.
        # This prevents "no headlines" on earnings nights when the ticker-filtered
        # endpoint is sparse or delayed.
        if symbols and market_batch:
            want = set(s.upper() for s in symbols)
            by_sym: dict[str, list[dict]] = {s: [] for s in want}
            try:
                max_per_sym = int(os.getenv("TNT_NEWS_MARKET_SPLIT_MAX_PER_SYM", "25") or "25")
            except Exception:
                max_per_sym = 25
            max_per_sym = max(5, min(100, int(max_per_sym)))

            for js in market_batch:
                if not isinstance(js, dict):
                    continue
                tickers = js.get("tickers")
                tickers_list: list[str] = []
                if isinstance(tickers, list):
                    tickers_list = [str(t).strip().upper() for t in tickers if t and str(t).strip()]
                elif isinstance(tickers, str):
                    tickers_list = [t.strip().upper() for t in tickers.split(",") if t and t.strip()]
                for t in set(tickers_list):
                    if t in want and len(by_sym[t]) < max_per_sym:
                        by_sym[t].append(js)

            for sym, batch in by_sym.items():
                if batch:
                    wrote = svc.cache_symbol_items(symbol=sym, items=batch, ttl_sec=24 * 3600)
                    stored_total += int(wrote or 0)
                    if int(wrote or 0) > 0:
                        symbols_written.add(str(sym).strip().upper())

    if symbols:
        items = await fetch_benzinga_news(
            base_url=base_url,
            api_key=api_key or "",
            tickers=symbols,
            published_since_utc=since,
            limit=limit,
        )
        # Store per-symbol using the tickers field.
        want = set(s.upper() for s in symbols)
        by_sym: dict[str, list[dict]] = {s: [] for s in want}
        for it in items:
            js = _decorate(it.to_jsonable())
            for t in set(it.tickers or []):
                tt = str(t).strip().upper()
                if tt and tt in want:
                    by_sym[tt].append(js)
        for sym, batch in by_sym.items():
            wrote = svc.cache_symbol_items(symbol=sym, items=batch, ttl_sec=24 * 3600)
            stored_total += int(wrote or 0)
            if int(wrote or 0) > 0:
                symbols_written.add(str(sym).strip().upper())

    # Advance cursor
    max_ts = None
    try:
        # Prefer market stamp if present; else pick the newest from any symbol.
        max_ts = svc.get_market_last_epoch_s()
        if max_ts is None:
            for sym in symbols:
                v = svc.get_symbol_last_epoch_s(sym)
                if v is not None and (max_ts is None or v > max_ts):
                    max_ts = v
    except Exception:
        max_ts = None

    if max_ts is not None:
        try:
            r.set(cursor_key, str(int(max_ts)))
            r.expire(cursor_key, 48 * 3600)
        except Exception:
            pass

    return stored_total, classified_n, material_n, sorted(symbols_written), int(market_items_written)


async def _run(args: argparse.Namespace) -> None:
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    fail_n = 0
    write_feed_heartbeat("news")

    # 5-min heartbeat for ops trust.
    try:
        hb_every_s = float(os.getenv("TNT_NEWS_HEARTBEAT_EVERY_SEC", "300") or "300")
    except Exception:
        hb_every_s = 300.0
    hb_every_s = max(60.0, float(hb_every_s))
    hb_last_ts = 0.0
    hb_market_items = 0
    hb_symbols_written: set[str] = set()

    try:
        r_hb = _redis_client()
        ck = getattr(getattr(r_hb, "connection_pool", None), "connection_kwargs", {}) or {}
        hb_db = ck.get("db")
        hb_host = ck.get("host")
    except Exception:
        hb_db = None
        hb_host = None

    while True:
        try:
            stored, classified_n, material_n, wrote_syms, wrote_market = await _tick(
                symbols=symbols,
                market=bool(args.market),
                limit=int(args.limit),
            )
            fail_n = 0
            now = datetime.now(timezone.utc).isoformat()
            print(
                f"[{now}] stored={stored} classified={classified_n} material={material_n} market={bool(args.market)} symbols={len(symbols)}"
            )
            write_feed_heartbeat("news")

            # Heartbeat (prints at most once per interval, independent of poll rate).
            try:
                hb_market_items += int(wrote_market or 0)
                if isinstance(wrote_syms, list):
                    hb_symbols_written.update({str(s).strip().upper() for s in wrote_syms if str(s).strip()})

                now_ts = datetime.now(timezone.utc).timestamp()
                if (now_ts - hb_last_ts) >= hb_every_s:
                    hb_last_ts = now_ts
                    print(
                        f"[NEWS][HEARTBEAT] db={hb_db} host={hb_host} "
                        f"wrote_symbol_keys={len(hb_symbols_written)} wrote_market_items={hb_market_items}"
                    )

                    # Shared on-disk heartbeat for diagnosing Redis DB mismatches.
                    try:
                        hb_payload = {
                            "ts_utc": datetime.now(timezone.utc).isoformat(),
                            "db": hb_db,
                            "host": hb_host,
                            "wrote_symbol_keys": int(len(hb_symbols_written)),
                            "wrote_market_items": int(hb_market_items),
                        }
                        logs_dir = Path("logs")
                        logs_dir.mkdir(parents=True, exist_ok=True)
                        out_path = logs_dir / "news_poller_heartbeat.json"
                        tmp_path = logs_dir / "news_poller_heartbeat.json.tmp"
                        tmp_path.write_text(json.dumps(hb_payload, ensure_ascii=False), encoding="utf-8")
                        tmp_path.replace(out_path)
                    except Exception:
                        pass

                    hb_market_items = 0
                    hb_symbols_written.clear()
            except Exception:
                pass

            await asyncio.sleep(float(args.every_sec))
        except Exception as exc:
            fail_n += 1
            base = float(args.every_sec)
            backoff = min(60.0, max(base, base * (2.0 ** min(fail_n, 6))))
            # Small jitter to avoid synchronized retries.
            backoff = backoff * (0.85 + 0.3 * random.random())
            now = datetime.now(timezone.utc).isoformat()
            print(f"[{now}] error={type(exc).__name__}: {exc} backoff_s={backoff:.1f} fail_n={fail_n}")
            write_feed_heartbeat("news", last_error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(backoff)


def main() -> None:
    _maybe_load_repo_dotenv()

    if not _acquire_singleton_lock():
        print("[FATAL] Another news poller process is already running (singleton lock active).")
        raise SystemExit(2)

    p = argparse.ArgumentParser(description="Poll external news feed and cache into Redis")
    p.add_argument("--symbols", default=os.getenv("TNT_NEWS_SYMBOLS", "SPY,QQQ,IWM"), help="CSV tickers")
    p.add_argument("--market", action="store_true", help="Also poll market-wide news")
    p.add_argument("--every-sec", type=float, default=float(os.getenv("TNT_NEWS_EVERY_SEC", "45")))
    p.add_argument("--limit", type=int, default=int(os.getenv("TNT_NEWS_LIMIT", "50")))
    args = p.parse_args()

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
