from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# Ensure workspace root is importable when running as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _safe_float(x: object) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
        if not (v == v):
            return None
        return v
    except Exception:
        return None


def _decode(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _now_et() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc)


def _parse_iso_utc(ts: object) -> datetime | None:
    if ts is None:
        return None
    try:
        raw = str(ts).strip().replace("Z", "+00:00")
        if not raw:
            return None
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


@dataclass(frozen=True)
class EnrichResult:
    wrote: bool
    expected_move_written: bool
    reactions_written: bool
    notes: list[str]


def _fetch_recent_earnings_events(*, r: Any, symbol: str, limit: int = 12) -> list[dict[str, Any]]:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return []
    zkey = f"earn:tick:{sym}"
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    try:
        ids = r.zrevrangebyscore(zkey, now_epoch, "-inf", start=0, num=int(limit))
    except Exception:
        ids = []
    out: list[dict[str, Any]] = []
    for eid in ids or []:
        # When decode_responses=False, redis returns bytes for ids.
        try:
            if isinstance(eid, (bytes, bytearray)):
                eid_s = eid.decode("utf-8", errors="replace")
            else:
                eid_s = str(eid)
        except Exception:
            continue
        eid_s = str(eid_s or "").strip()
        if not eid_s:
            continue
        try:
            raw = r.get(f"earn:item:{eid_s}".encode("utf-8"))
        except Exception:
            raw = None
        if not raw:
            continue
        s = _decode(raw)
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if isinstance(obj, dict):
            # Preserve the Redis id so downstream reaction computation can write back
            # into the canonical history layer (earn:item:<id>).
            d = dict(obj)
            d.setdefault("id", eid_s)
            out.append(d)
    return out


def _compute_last4_reactions(*, symbol: str, events: list[dict[str, Any]], session_hint: str) -> list[dict[str, Any]]:
    if not events:
        return []

    # Pull daily bars (bounded) and compute simple gap + 1D move.
    try:
        from delivery.on_demand_data import polygon_aggs
    except Exception:
        return []

    days = 420
    try:
        days = int(os.getenv("EARNINGS_REACTIONS_LOOKBACK_DAYS", "420"))
    except Exception:
        days = 420
    days = max(120, min(900, int(days)))

    try:
        payload = polygon_aggs(symbol, multiplier=1, timespan="day", days=days, cache_ttl_sec=3600)
    except Exception:
        payload = {}

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return []

    tz_et = getattr(sys.modules.get("delivery.discord_bot"), "ET_TZ", None)
    if tz_et is None:
        try:
            from zoneinfo import ZoneInfo

            tz_et = ZoneInfo("America/New_York")
        except Exception:
            tz_et = timezone.utc

    def _infer_session_from_ts_utc(ts_utc: datetime) -> str:
        # Match CalendarService heuristics: <=11:00 ET => BMO, >=16:00 ET => AMC.
        try:
            ts_et = ts_utc.astimezone(tz_et)
            hhmm = ts_et.hour * 60 + ts_et.minute
            if hhmm <= (11 * 60):
                return "BMO"
            if hhmm >= (16 * 60):
                return "AMC"
        except Exception:
            pass
        return "DURING"

    bars: dict[str, dict[str, float]] = {}
    for b in results:
        if not isinstance(b, dict):
            continue
        try:
            t_ms = int(b.get("t"))
            o = float(b.get("o"))
            c = float(b.get("c"))
        except Exception:
            continue
        # Polygon daily aggregates use a UTC day-boundary timestamp for the bar start.
        # Converting that UTC midnight into ET can shift the date backward, breaking
        # alignment with earnings event dates. Key by the UTC *session date* instead.
        try:
            d_key = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).date().isoformat()
        except Exception:
            continue
        bars[d_key] = {"o": o, "c": c}

    if not bars:
        return []

    bar_dates = sorted(bars.keys())

    def _prev_trading_date(d: str) -> str | None:
        try:
            import bisect

            i = bisect.bisect_left(bar_dates, d)
            if i <= 0:
                return None
            return bar_dates[i - 1]
        except Exception:
            return None

    def _next_trading_date(d: str) -> str | None:
        try:
            import bisect

            i = bisect.bisect_right(bar_dates, d)
            if i >= len(bar_dates):
                return None
            return bar_dates[i]
        except Exception:
            return None

    def _tag(*, gap_pct: float, move_pct: float) -> str:
        if abs(gap_pct) < 1.0 and abs(move_pct) < 1.0:
            return "inside range"
        same_dir = (gap_pct >= 0) == (move_pct >= 0)
        if not same_dir:
            return "gap, fade"
        if abs(move_pct) >= max(1.0, abs(gap_pct) * 0.7):
            return "gap, continuation"
        return "gap, chop"

    out: list[dict[str, Any]] = []
    # Take up to 4 most recent events.
    for ev0 in (events or [])[:8]:
        eid = str(ev0.get("id") or "").strip()
        ts_raw = str(ev0.get("ts_utc") or "")
        if not ts_raw:
            continue
        try:
            dtu = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if dtu.tzinfo is None:
                dtu = dtu.replace(tzinfo=timezone.utc)
            dtu = dtu.astimezone(timezone.utc)
        except Exception:
            continue

        # Session-date key must match the bar map key (UTC session date).
        d_key = dtu.date().isoformat()

        session = str(ev0.get("session") or "").strip().upper()
        if not session:
            # Many cached history items only have ts_utc; infer session from the timestamp.
            session = _infer_session_from_ts_utc(dtu)
        if not session and session_hint:
            session = str(session_hint).strip().upper()
        if session not in {"BMO", "AMC", "DURING"}:
            session = "DURING"

        # Select which bars define the reaction.
        base_date = d_key
        prev_date = _prev_trading_date(base_date)
        next_date = _next_trading_date(base_date)
        if session == "AMC":
            base_close_date = base_date
            open_date = next_date
            close_date = next_date
        else:
            base_close_date = prev_date
            open_date = base_date
            close_date = base_date

        if not base_close_date or not open_date or not close_date:
            continue
        if base_close_date not in bars or open_date not in bars or close_date not in bars:
            continue

        base_close = _safe_float(bars[base_close_date].get("c"))
        open_px = _safe_float(bars[open_date].get("o"))
        close_px = _safe_float(bars[close_date].get("c"))
        if base_close is None or open_px is None or close_px is None or base_close <= 0:
            continue

        gap_pct = (float(open_px) - float(base_close)) / float(base_close) * 100.0
        move_pct = (float(close_px) - float(base_close)) / float(base_close) * 100.0
        out.append(
            {
                "id": eid or None,
                "ts_utc": dtu.isoformat(),
                "session": session,
                "gap_pct": float(gap_pct),
                "move_pct": float(move_pct),
                "tag": _tag(gap_pct=float(gap_pct), move_pct=float(move_pct)),
            }
        )
        if len(out) >= 4:
            break

    return out


async def enrich_symbol(*, symbol: str, write: bool, compute_expected_move: bool = True, compute_reactions: bool = True) -> EnrichResult:
    from services.redis_env import redis_client
    from delivery import discord_bot as delivery
    from services.calendar.calendar_service import CalendarService

    sym = str(symbol or "").strip().upper()
    r = redis_client(timeout_s=0.5, decode_responses=False)
    cal = CalendarService(r)

    notes: list[str] = []

    payload = cal.get_earnings(symbol=sym) or {}
    if not payload:
        return EnrichResult(wrote=False, expected_move_written=False, reactions_written=False, notes=["no cal earnings payload"])

    expected_move_written = False
    reactions_written = False

    # TTL-based refresh (weekday hotlist) so expected move doesn't go stale.
    try:
        em_ttl_hours = float(os.getenv("EARNINGS_EXPECTED_MOVE_TTL_HOURS", "6") or "6")
    except Exception:
        em_ttl_hours = 6.0
    em_ttl_hours = max(0.25, min(72.0, float(em_ttl_hours)))

    em_refreshed = _parse_iso_utc(payload.get("expected_move_refreshed_utc"))
    em_age_h = None if em_refreshed is None else (datetime.now(timezone.utc) - em_refreshed).total_seconds() / 3600.0
    em_stale = (payload.get("expected_move_pct") is None) or (em_age_h is None) or (em_age_h > em_ttl_hours)

    if compute_expected_move and em_stale:
        api_key, _base_url, _provider = delivery._polygon_key_and_base()
        if not api_key:
            notes.append("no polygon key; skipping expected move")
        else:
            try:
                timeout_s = float(os.getenv("EARNINGS_OPTIONS_ENRICH_SINGLE_TIMEOUT", "8"))
            except Exception:
                timeout_s = 8.0

            try:
                df = await asyncio.wait_for(delivery._fetch_polygon_atm_straddle_df(sym), timeout=max(1.0, float(timeout_s)))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"atm_straddle_df failed: {type(exc).__name__}")
                df = None

            if df is None:
                notes.append("atm_straddle_df=None")
            else:
                from services.calendar.earnings_options import compute_expected_move_from_chain_df

                res = compute_expected_move_from_chain_df(df)
                if isinstance(res, dict) and res.get("expected_move_pct") is not None:
                    payload["expected_move_pct"] = float(res["expected_move_pct"])
                    payload["liquidity_risk"] = str(res.get("liquidity_risk") or payload.get("liquidity_risk") or "MED")
                    payload["expected_move_source"] = "options_chain_atm_straddle"

                    details = res.get("details") if isinstance(res.get("details"), dict) else {}
                    underlying = _safe_float(details.get("underlying_price"))
                    straddle = _safe_float(details.get("straddle_mid"))
                    if underlying and straddle and underlying > 0 and straddle > 0:
                        payload["expected_move"] = {
                            "underlying": float(underlying),
                            "straddle": float(straddle),
                            "pct": float(res["expected_move_pct"]),
                            "upper": float(underlying + straddle),
                            "lower": float(max(0.0, underlying - straddle)),
                            "atm_strike": _safe_float(details.get("atm_strike")),
                        }

                    iv_state = payload.get("iv_state") if isinstance(payload.get("iv_state"), dict) else {}
                    if res.get("front_iv") is not None and iv_state.get("front_iv") is None:
                        iv_state["front_iv"] = float(res["front_iv"])
                    payload["iv_state"] = iv_state

                    payload["expected_move_refreshed_utc"] = datetime.now(timezone.utc).isoformat()

                    expected_move_written = True
                else:
                    notes.append("compute_expected_move_from_chain_df returned None")

    # Reactions from existing earn:tick history.
    have_moves = False
    if compute_reactions:
        hist0 = payload.get("history") if isinstance(payload.get("history"), list) else []
        for it in hist0:
            if isinstance(it, dict) and it.get("move_pct") is not None:
                have_moves = True
                break

    if compute_reactions and (not have_moves):
        raw_events = _fetch_recent_earnings_events(r=r, symbol=sym, limit=12)

        # Treat "too old" as empty so we trigger a backfill and get usable events.
        days = 420
        try:
            days = int(os.getenv("EARNINGS_REACTIONS_LOOKBACK_DAYS", "420"))
        except Exception:
            days = 420
        days = max(120, min(900, int(days)))

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days) + 10)).astimezone(timezone.utc)
            recent = []
            for ev0 in raw_events or []:
                ts_raw = str((ev0 or {}).get("ts_utc") or "")
                if not ts_raw:
                    continue
                try:
                    dtu = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if dtu.tzinfo is None:
                        dtu = dtu.replace(tzinfo=timezone.utc)
                    dtu = dtu.astimezone(timezone.utc)
                except Exception:
                    continue
                if dtu >= cutoff:
                    recent.append(ev0)
            if raw_events and not recent:
                raw_events = []
        except Exception:
            pass

        if not raw_events:
            # Best-effort backfill: fetch and cache earnings history so reactions can be computed.
            try:
                from services.calendar.massive_benzinga_earnings import fetch_benzinga_earnings

                api_key, base_url, _provider = delivery._massive_key_and_base()
                if api_key:
                    now_utc = datetime.now(timezone.utc)
                    recs = await fetch_benzinga_earnings(
                        base_url=base_url,
                        api_key=api_key or "",
                        tickers=[sym],
                        start_date=(now_utc - timedelta(days=days)).date().isoformat(),
                        end_date=(now_utc + timedelta(days=30)).date().isoformat(),
                        limit=400,
                        timeout_s=10.0,
                    )
                    if recs:
                        cal.cache_earnings_history(symbol=sym, items=[x.to_jsonable() for x in recs], ttl_sec=90 * 24 * 3600)
                        raw_events = _fetch_recent_earnings_events(r=r, symbol=sym, limit=12)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"backfill_failed: {type(exc).__name__}")

        if not raw_events:
            notes.append("no raw_events in earn:tick")
        else:
            reactions = _compute_last4_reactions(symbol=sym, events=raw_events, session_hint=str(payload.get("session") or "UNKNOWN"))
            if reactions:
                payload["history"] = reactions
                reactions_written = True

                # Persist computed fields back into the history layer so downstream
                # readers (e.g. /earnings_debug) can confirm write-back durability.
                persisted = 0
                attempted = 0
                for it in reactions:
                    if not isinstance(it, dict):
                        continue
                    eid = str(it.get("id") or "").strip()
                    if not eid:
                        continue
                    attempted += 1
                    key = f"earn:item:{eid}"
                    try:
                        raw = r.get(key)
                    except Exception:
                        raw = None
                    if not raw:
                        continue
                    try:
                        obj = json.loads(_decode(raw) or "{}")
                    except Exception:
                        obj = {}
                    if not isinstance(obj, dict):
                        obj = {}

                    obj["gap_pct"] = _safe_float(it.get("gap_pct"))
                    obj["move_pct"] = _safe_float(it.get("move_pct"))
                    obj["tag"] = str(it.get("tag") or "") or None
                    obj["session"] = str(it.get("session") or "") or None
                    if it.get("ts_utc"):
                        obj["ts_utc"] = str(it.get("ts_utc"))
                    obj["reactions_computed_utc"] = datetime.now(timezone.utc).isoformat()

                    try:
                        r.set(key, json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
                        persisted += 1
                    except Exception:
                        continue
                notes.append(f"reactions_persisted={persisted}/{attempted}")

                # Cache stamp (internal): makes it obvious when reactions were last updated
                # and which path produced them. This is not shown in the public embed.
                payload["_reactions_cache"] = {
                    "source": "earnings_enrich_cache",
                    "updated_utc": datetime.now(timezone.utc).isoformat(),
                    "persisted": int(persisted),
                    "attempted": int(attempted),
                }
            else:
                notes.append("computed reactions empty")

    wrote = False
    if write and (expected_move_written or reactions_written):
        cal.set_earnings_blob(symbol=sym, blob=payload, ttl_sec=14 * 24 * 3600)
        wrote = True

    return EnrichResult(wrote=wrote, expected_move_written=expected_move_written, reactions_written=reactions_written, notes=notes)


async def _amain() -> int:
    p = argparse.ArgumentParser(description="Enrich cal:earnings:{SYM} with expected move + last-4 reactions")
    p.add_argument("--symbol", default="AAPL", help="Symbol to enrich")
    p.add_argument("--write", action="store_true", help="Write back to Redis")
    p.add_argument("--expected-move", action="store_true", help="Compute/refresh expected move (default: on)")
    p.add_argument("--no-expected-move", action="store_true", help="Disable expected move enrichment")
    p.add_argument("--reactions", action="store_true", help="Compute/refresh last-4 reactions (default: on)")
    p.add_argument("--no-reactions", action="store_true", help="Disable reactions enrichment")
    args = p.parse_args()

    sym = str(args.symbol or "").strip().upper()
    if not sym:
        print("symbol required")
        return 2

    # Defaults: both enabled unless explicitly disabled.
    enrich_expected_move = not bool(args.no_expected_move)
    enrich_reactions = not bool(args.no_reactions)
    res = await enrich_symbol(
        symbol=sym,
        write=bool(args.write),
        compute_expected_move=bool(enrich_expected_move),
        compute_reactions=bool(enrich_reactions),
    )
    print("=== earnings_enrich_cache ===")
    print("symbol", sym)
    print("expected_move_written", res.expected_move_written)
    print("reactions_written", res.reactions_written)
    print("wrote", res.wrote)
    if res.notes:
        print("notes", "; ".join(res.notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
