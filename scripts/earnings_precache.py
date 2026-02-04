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
from services.calendar.calendar_keys import earnings_warm_last_err_key, earnings_warm_last_ok_ts_key, earnings_warm_lock_key
from services.calendar.calendar_service import CalendarService
from services.calendar.earnings_hotlist import top_earnings_queries
from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings, pick_next_earnings
from services.news.massive_benzinga_news import fetch_benzinga_news
from services.news.news_service import NewsService


def _truthy_env(name: str, default: str = "0") -> bool:
    try:
        v = (os.getenv(name, default) or default).strip().lower()
        return v in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return float(default)


def _parse_csv_symbols(csv: str, *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in (csv or "").split(","):
        sym = str(raw or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


def _redis_client() -> redis.Redis:
    host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
    port = int(os.getenv("TNT_REDIS_PORT", "6379"))
    db = int(os.getenv("TNT_REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


def _acquire_lock(r: redis.Redis, *, ttl_sec: int = 20 * 60) -> bool:
    key = earnings_warm_lock_key()
    try:
        ok = r.set(key, str(int(datetime.now(timezone.utc).timestamp())), nx=True, ex=int(ttl_sec))
        return bool(ok)
    except Exception:
        return True  # best-effort


def _release_lock(r: redis.Redis) -> None:
    try:
        r.delete(earnings_warm_lock_key())
    except Exception:
        pass


def _stable_universe(*, max_symbols: int) -> list[str]:
    # Prefer explicit stable list.
    raw = (os.getenv("EARNINGS_UNIVERSE_STABLE", "") or "").strip()
    if raw:
        return _parse_csv_symbols(raw, limit=max_symbols)

    # Otherwise build from autopost + a small mega-cap set.
    autopost = (os.getenv("EARNINGS_AUTOPOST_SYMBOLS", "SPY,QQQ,IWM") or "").strip()
    extra = (os.getenv("EARNINGS_UNIVERSE_STABLE_EXTRA", "") or "").strip()

    # Keep this list conservative: common high-traffic US names.
    mega = "AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,NFLX,AMD,INTC,AVGO,COST,JPM,V,MA,UNH,LLY,GOOG"

    combined = ",".join([autopost, extra, mega])
    return _parse_csv_symbols(combined, limit=max_symbols)


def _always_hot(*, limit: int = 50) -> list[str]:
    raw = (os.getenv("EARNINGS_ALWAYS_HOT_SYMBOLS", "") or "").strip()
    if raw:
        return _parse_csv_symbols(raw, limit=limit)
    return _parse_csv_symbols("SPY,QQQ,IWM,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL", limit=limit)


def _parse_iso_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


async def _refresh_earnings_metadata(
    *,
    r: redis.Redis,
    symbols: list[str],
    limit: int,
    ttl_sec: int,
    force_clear: bool,
) -> dict[str, int]:
    api_key, base_url, _provider = _massive_key_and_base()
    if not api_key:
        return {"symbols": len(symbols), "updated": 0, "cleared": 0, "total_records": 0}

    now = datetime.now(timezone.utc)
    records = await fetch_benzinga_earnings(base_url=base_url, api_key=api_key or "", tickers=symbols, limit=limit)

    cal = CalendarService(r)

    updated = 0
    cleared = 0
    for sym in symbols:
        nxt = pick_next_earnings(records, symbol=sym, now_utc=now)
        if nxt is None:
            if force_clear:
                try:
                    from services.calendar.calendar_keys import cal_earnings_key

                    r.delete(cal_earnings_key(str(sym).strip().upper()))
                    cleared += 1
                except Exception:
                    pass
            continue
        cal.set_earnings(symbol=sym, ts_utc=nxt.ts_utc, confirmed=nxt.confirmed, source=nxt.source, ttl_sec=ttl_sec)
        updated += 1

    # Optional history layer from everything we fetched per symbol.
    by_sym: dict[str, list[dict]] = {s: [] for s in symbols}
    for rec in records:
        if rec.symbol in by_sym:
            by_sym[rec.symbol].append(rec.to_jsonable())
    for sym, batch in by_sym.items():
        if batch:
            cal.cache_earnings_history(symbol=sym, items=batch, ttl_sec=90 * 24 * 3600)

    return {"symbols": len(symbols), "updated": updated, "cleared": cleared, "total_records": len(records)}


async def _backfill_history_one(
    *,
    symbol: str,
    lookback_days: int,
    timeout_s: float,
) -> dict[str, object]:
    # Use the existing single-symbol bounded backfill behavior.
    from scripts.earnings_enrich_cache import enrich_symbol

    # enrich_symbol already does a best-effort history backfill when reactions need it.
    # Here, we call it with reactions enabled and expected move disabled.
    try:
        res = await asyncio.wait_for(
            enrich_symbol(symbol=symbol, write=True, compute_expected_move=False, compute_reactions=True),
            timeout=max(1.0, float(timeout_s)),
        )
        return {
            "symbol": symbol,
            "expected_move_written": bool(res.expected_move_written),
            "reactions_written": bool(res.reactions_written),
            "wrote": bool(res.wrote),
            "notes": list(res.notes or []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "error": type(exc).__name__}


async def _refresh_expected_move_one(*, symbol: str, timeout_s: float) -> dict[str, object]:
    from scripts.earnings_enrich_cache import enrich_symbol

    try:
        res = await asyncio.wait_for(
            enrich_symbol(symbol=symbol, write=True, compute_expected_move=True, compute_reactions=False),
            timeout=max(1.0, float(timeout_s)),
        )
        return {
            "symbol": symbol,
            "expected_move_written": bool(res.expected_move_written),
            "reactions_written": bool(res.reactions_written),
            "wrote": bool(res.wrote),
            "notes": list(res.notes or []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "error": type(exc).__name__}


async def _refresh_news_one(
    *,
    r: redis.Redis,
    symbol: str,
    timeout_s: float,
    lookback_hours: int,
    limit: int,
    ttl_sec: int,
) -> dict[str, object]:
    api_key, base_url, _provider = _massive_key_and_base()
    if not api_key:
        return {"symbol": symbol, "skipped": "no_massive_key"}

    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))

    try:
        items = await asyncio.wait_for(
            fetch_benzinga_news(
                base_url=base_url,
                api_key=api_key or "",
                tickers=[symbol],
                published_since_utc=since,
                limit=int(limit),
                timeout_s=max(2.0, float(timeout_s)),
            ),
            timeout=max(3.0, float(timeout_s) + 2.0),
        )
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "error": f"news_fetch:{type(exc).__name__}"}

    svc = NewsService(r)
    try:
        stored = svc.cache_symbol_items(symbol=symbol, items=[x.to_jsonable() for x in items], ttl_sec=int(ttl_sec))
    except Exception:
        stored = 0

    return {"symbol": symbol, "stored": int(stored), "fetched": int(len(items))}


async def _run_bounded(symbols: list[str], *, concurrency: int, fn, timeout_s: float) -> list[dict[str, object]]:
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def _one(sym: str) -> dict[str, object]:
        async with sem:
            return await fn(sym)

    tasks = [asyncio.create_task(_one(s)) for s in symbols]
    out: list[dict[str, object]] = []
    for t in asyncio.as_completed(tasks):
        try:
            out.append(await asyncio.wait_for(t, timeout=max(1.0, float(timeout_s) + 5.0)))
        except Exception as exc:  # noqa: BLE001
            out.append({"error": type(exc).__name__})
    return out


def _hotlist(
    *,
    r: redis.Redis,
    cal: CalendarService,
    stable: list[str],
    max_symbols: int,
) -> list[str]:
    # 1) Always-hot pins.
    out: list[str] = []
    seen: set[str] = set()

    def _add(sym: str) -> None:
        s = str(sym or "").strip().upper()
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    for s in _always_hot(limit=50):
        _add(s)

    # 2) Usage-based hotlist.
    min_q = _env_int("EARNINGS_HOTLIST_MIN_QCOUNT", 3)
    top_n = _env_int("EARNINGS_HOTLIST_TOPN", 25)
    for sym, cnt in top_earnings_queries(r, limit=top_n, min_count=min_q):
        _add(sym)

    # 3) Upcoming within window (from cached earnings blobs).
    days = _env_int("EARNINGS_HOTLIST_EARNINGS_DAYS", 10)
    end_utc = datetime.now(timezone.utc) + timedelta(days=max(1, int(days)))

    for s in stable:
        payload = cal.get_earnings(symbol=s)
        if not payload:
            continue
        ts = _parse_iso_utc(str(payload.get("ts_utc") or ""))
        if ts is None:
            continue
        if datetime.now(timezone.utc) <= ts <= end_utc:
            _add(s)

    return out[: max(1, int(max_symbols))]


async def main_async(mode: str, *, dry_run: bool) -> int:
    mode0 = str(mode or "").strip().lower()
    if mode0 not in {"sunday", "weekday"}:
        print("mode must be sunday|weekday")
        return 2

    r = _redis_client()
    cal = CalendarService(r)

    if not _acquire_lock(r, ttl_sec=_env_int("EARNINGS_WARM_LOCK_TTL_SEC", 20 * 60)):
        print({"status": "skipped", "reason": "lock_held"})
        return 0

    try:
        if mode0 == "sunday":
            if not _truthy_env("EARNINGS_WARM_SUNDAY_ENABLE", "1"):
                print({"status": "disabled"})
                return 0

            max_syms = _env_int("EARNINGS_WARM_SUNDAY_MAX_SYMBOLS", 150)
            concurrency = _env_int("EARNINGS_WARM_SUNDAY_CONCURRENCY", 4)
            timeout_s = _env_float("EARNINGS_WARM_SUNDAY_TIMEOUT_SEC", 6.0)

            symbols = _stable_universe(max_symbols=max_syms)

            if dry_run:
                print({"mode": "sunday", "symbols": symbols, "count": len(symbols)})
                return 0

            # Step 1: metadata/schedule poll (batch)
            meta_limit = _env_int("TNT_EARNINGS_LIMIT", 2000)
            meta_ttl = _env_int("EARNINGS_WARM_SUNDAY_TTL_SEC", 7 * 24 * 3600)
            meta = await _refresh_earnings_metadata(r=r, symbols=symbols, limit=meta_limit, ttl_sec=meta_ttl, force_clear=False)

            # Step 2+3: bounded backfill + reactions compute via enrich script (per symbol)
            lookback_days = _env_int("EARNINGS_REACTIONS_LOOKBACK_DAYS", 420)

            async def _one(sym: str) -> dict[str, object]:
                return await _backfill_history_one(symbol=sym, lookback_days=lookback_days, timeout_s=float(timeout_s))

            results = await _run_bounded(symbols, concurrency=concurrency, fn=_one, timeout_s=float(timeout_s))

            # Optional Sunday news seed (off by default)
            news_seed = _truthy_env("EARNINGS_WARM_SUNDAY_NEWS_ENABLE", "0")
            news_results: list[dict[str, object]] = []
            if news_seed:
                news_timeout = _env_float("EARNINGS_WARM_SUNDAY_NEWS_TIMEOUT_SEC", 8.0)
                news_limit = _env_int("EARNINGS_WARM_SUNDAY_NEWS_LIMIT", 20)
                news_ttl = _env_int("EARNINGS_WARM_SUNDAY_NEWS_TTL_SEC", 24 * 3600)

                async def _news_one(sym: str) -> dict[str, object]:
                    return await _refresh_news_one(
                        r=r,
                        symbol=sym,
                        timeout_s=float(news_timeout),
                        lookback_hours=24,
                        limit=int(news_limit),
                        ttl_sec=int(news_ttl),
                    )

                news_results = await _run_bounded(symbols, concurrency=concurrency, fn=_news_one, timeout_s=float(news_timeout))

            # Stamps
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            try:
                r.set(earnings_warm_last_ok_ts_key(), str(now_epoch))
                r.delete(earnings_warm_last_err_key())
            except Exception:
                pass

            print({"mode": "sunday", "meta": meta, "symbols": len(symbols), "enrich_results": len(results), "news_seed": news_seed, "news_results": len(news_results)})
            return 0

        # weekday
        if not _truthy_env("EARNINGS_REFRESH_WEEKDAY_ENABLE", "1"):
            print({"status": "disabled"})
            return 0

        max_syms = _env_int("EARNINGS_REFRESH_WEEKDAY_MAX_SYMBOLS", 40)
        concurrency = _env_int("EARNINGS_REFRESH_WEEKDAY_CONCURRENCY", 4)
        timeout_s = _env_float("EARNINGS_REFRESH_WEEKDAY_TIMEOUT_SEC", 3.0)

        stable = _stable_universe(max_symbols=_env_int("EARNINGS_WARM_SUNDAY_MAX_SYMBOLS", 150))
        hot = _hotlist(r=r, cal=cal, stable=stable, max_symbols=max_syms)

        if dry_run:
            print({"mode": "weekday", "hotlist": hot, "count": len(hot)})
            return 0

        # Optional metadata refresh for hotlist (cheap; keeps next earnings date from drifting)
        meta_enable = _truthy_env("EARNINGS_REFRESH_META_ENABLE", "1")
        meta = None
        if meta_enable:
            meta_limit = _env_int("TNT_EARNINGS_LIMIT", 2000)
            meta_ttl = _env_int("EARNINGS_WARM_SUNDAY_TTL_SEC", 7 * 24 * 3600)
            meta = await _refresh_earnings_metadata(r=r, symbols=hot, limit=meta_limit, ttl_sec=meta_ttl, force_clear=False)

        # Step 1: expected move refresh
        expected_move_enable = _truthy_env("EARNINGS_REFRESH_EXPECTED_MOVE_ENABLE", "1")
        em_results: list[dict[str, object]] = []
        if expected_move_enable:

            async def _em_one(sym: str) -> dict[str, object]:
                return await _refresh_expected_move_one(symbol=sym, timeout_s=float(timeout_s))

            em_results = await _run_bounded(hot, concurrency=concurrency, fn=_em_one, timeout_s=float(timeout_s))

        # Step 2: news refresh
        news_enable = _truthy_env("EARNINGS_REFRESH_NEWS_ENABLE", "1")
        news_results: list[dict[str, object]] = []
        if news_enable:
            news_timeout = _env_float("EARNINGS_REFRESH_NEWS_TIMEOUT_SEC", 8.0)
            news_limit = _env_int("EARNINGS_REFRESH_NEWS_LIMIT", 30)
            news_ttl = _env_int("EARNINGS_REFRESH_NEWS_TTL_SEC", 6 * 3600)

            async def _news_one(sym: str) -> dict[str, object]:
                return await _refresh_news_one(
                    r=r,
                    symbol=sym,
                    timeout_s=float(news_timeout),
                    lookback_hours=24,
                    limit=int(news_limit),
                    ttl_sec=int(news_ttl),
                )

            news_results = await _run_bounded(hot, concurrency=concurrency, fn=_news_one, timeout_s=float(news_timeout))

        now_epoch = int(datetime.now(timezone.utc).timestamp())
        try:
            r.set(earnings_warm_last_ok_ts_key(), str(now_epoch))
            r.delete(earnings_warm_last_err_key())
        except Exception:
            pass

        print(
            {
                "mode": "weekday",
                "hotlist": len(hot),
                "meta": meta,
                "expected_move_enable": expected_move_enable,
                "expected_move_results": len(em_results),
                "news_enable": news_enable,
                "news_results": len(news_results),
            }
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            r.set(earnings_warm_last_err_key(), f"{type(exc).__name__}")
        except Exception:
            pass
        raise
    finally:
        _release_lock(r)


def main() -> None:
    p = argparse.ArgumentParser(description="Earnings pre-cache warmers: sunday stable warm + weekday light refresh")
    p.add_argument("--mode", required=True, choices=["sunday", "weekday"], help="Which run profile to execute")
    p.add_argument("--dry-run", action="store_true", help="Print derived symbol list(s) and exit")
    args = p.parse_args()

    raise SystemExit(asyncio.run(main_async(args.mode, dry_run=bool(args.dry_run))))


if __name__ == "__main__":
    main()
