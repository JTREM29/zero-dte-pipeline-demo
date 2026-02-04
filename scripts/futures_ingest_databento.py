from __future__ import annotations

import json
import os
import sys
import time
import traceback
import datetime as dt
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

import redis

# Allow running this script from any working directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.observability.feed_heartbeat import write_feed_heartbeat

try:
    import databento as db  # type: ignore
except Exception as exc:
    raise SystemExit("Missing databento package. Install with: pip install databento") from exc


try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = dt.timezone.utc


_INGEST_LOCK: object | None = None


def _acquire_singleton_lock() -> bool:
    """Best-effort single-instance lock.

    Prevents accidentally running multiple ingest workers that write conflicting
    state into Redis.

    Override with DATABENTO_FUTURES_ALLOW_MULTI=1.
    """

    allow_multi = str(_get_setting("DATABENTO_FUTURES_ALLOW_MULTI", "0") or "0").strip().lower() in {"1", "true", "yes"}
    if allow_multi:
        return True

    try:
        lock_dir = Path("logs")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "tnt_futures_ingest.lock"
        fh = open(lock_path, "a+", encoding="utf-8")

        # IMPORTANT (Windows): msvcrt.locking() locks from the current file
        # position. Files opened with a+ start at EOF, so two processes can
        # accidentally lock different regions and both "succeed". Always lock
        # from the start of the file.
        try:
            fh.seek(0)
        except Exception:
            pass

        # Windows: msvcrt lock (non-blocking)
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
            # POSIX: fcntl flock (non-blocking)
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

        global _INGEST_LOCK
        _INGEST_LOCK = fh
        return True
    except Exception:
        return True


def _redis() -> redis.Redis:
    host = _get_setting("TNT_REDIS_HOST", "127.0.0.1")
    port = int(_get_setting("TNT_REDIS_PORT", "6379"))
    rdb = int(_get_setting("TNT_REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=rdb, decode_responses=True)


def _repo_root() -> Path:
    try:
        return Path(__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def _dotenv_get_value(path: Path, key: str) -> str | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        val = v.strip()
        if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
            val = val[1:-1]
        return val.strip()
    return None


def _get_setting(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw and raw.strip():
        return raw.strip()

    root = _repo_root()
    for env_path in (root / ".env.local", root / ".env"):
        val = _dotenv_get_value(env_path, key)
        if val and val.strip():
            return val.strip()
    return default


# Prefer shared key helpers if available.
try:
    from services.futures.futures_keys import fut_heartbeat_key as fut_hb_key
    from services.futures.futures_keys import fut_heartbeat_msg_key as fut_hb_msg_key
    from services.futures.futures_keys import fut_last_key, fut_scores_key
    from services.futures.futures_keys import fut_status_key
except Exception:  # pragma: no cover

    def fut_last_key(sym: str) -> str:
        return f"fut:{sym}:last"

    def fut_scores_key() -> str:
        return "fut:scores"

    def fut_hb_key() -> str:
        return "fut:hb"

    def fut_hb_msg_key() -> str:
        return "fut:hb_msg"

    def fut_status_key() -> str:
        return "fut:status"


@dataclass
class Quote:
    px: float
    ts_utc: int


@dataclass
class ScoreState:
    last_px: Dict[str, float]
    last_ts: Dict[str, int]
    ref_px: Dict[str, float]
    ref_session_id: Dict[str, int]
    ema_abs_ret: float
    ema_abs_ret_baseline: float
    updated_utc: int


def _session_id_et(epoch_utc: int | None = None) -> int:
    """Return a simple futures session id based on CME Globex rollover.

    We treat the trading "session day" as rolling over at 18:00 ET.
    Example: 2026-01-22 19:00 ET counts as session 20260123.
    """

    try:
        ts = int(epoch_utc or _now())
    except Exception:
        ts = _now()
    now_et = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(_ET)
    d = now_et.date()
    if int(now_et.hour) >= 18:
        d = d + dt.timedelta(days=1)
    return int(d.year) * 10000 + int(d.month) * 100 + int(d.day)


def _env_symbols() -> Tuple[str, ...]:
    raw = _get_setting("DATABENTO_SYMBOLS", "ES,NQ,RTY")
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def _root_from_symbol(symbol: str, roots: Tuple[str, ...]) -> str:
    s = (symbol or "").upper().strip()
    for r in roots:
        if s.startswith(r):
            return r
    # Common continuous contract form: ES.c.0
    if "." in s:
        head = s.split(".", 1)[0]
        for r in roots:
            if head == r:
                return r
    return ""


def _live_instrument_map(root: str, stype_in: str) -> str:
    sym = root.upper().strip()
    si = (stype_in or "").strip().lower()
    if si == "continuous":
        return f"{sym}.c.0"  # continuous, front month
    if si == "parent":
        return f"{sym}.FUT"  # per Databento Live docs for GLBX.MDP3
    return sym


def _now() -> int:
    return int(time.time())


def _write_hb(r: redis.Redis, msg: str) -> None:
    """Best-effort heartbeat write.

    Contract:
    - fut:hb is the primary heartbeat key consumed by context projection.
    - fut:hb:last_ts is a compatibility alias for ops/debug tooling.
    """

    try:
        ts = str(_now())
        r.set(fut_hb_key(), ts)
        r.set("fut:hb:last_ts", ts)
        r.set(fut_hb_msg_key(), (msg or "").strip() or "ok")
        # Unified feed heartbeat for ops/status.
        err = None
        if msg and str(msg).strip() and str(msg).strip().lower() not in {"ok", "boot", "awaiting_prints"}:
            err = str(msg).strip()
        write_feed_heartbeat("futures", last_error=err)
    except Exception:
        pass


_STATUS_REQUIRED_KEYS = (
    "state",
    "ts_utc",
    "updated_utc",
    "pid",
    "source",
    "note",
    "dataset",
    "schema",
    "stype_in",
    "symbols",
    "connected",
    "rec_counts",
)


def _safe_json_load_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_fut_status_guarded(r: redis.Redis, payload: dict) -> None:
    """Write fut:status without clobbering rich status fields.

    Tripwire behavior:
    - If a caller tries to write a minimal payload missing required keys, we log
      [FUT][STATUS][REFUSE_WRITE] and instead merge into the existing status.
    - The final write is always a merged payload containing the required keys.
    """

    if not isinstance(payload, dict):
        return

    now = _now()

    # Guardrail: refuse writing obviously-minimal status blobs.
    payload_missing = [k for k in _STATUS_REQUIRED_KEYS if k not in payload]
    refused_direct = bool(payload_missing)
    if refused_direct:
        try:
            print(f"[FUT][STATUS][REFUSE_WRITE] missing_keys={payload_missing}")
        except Exception:
            pass

    try:
        existing = _safe_json_load_dict(r.get(fut_status_key()))
    except Exception:
        existing = {}

    merged = dict(existing or {})
    merged.update(payload)

    # Fill required keys deterministically.
    merged.setdefault("pid", os.getpid())
    merged.setdefault("ts_utc", now)
    merged.setdefault("updated_utc", merged.get("ts_utc") or now)
    merged.setdefault("source", merged.get("source") or "databento")
    merged.setdefault("note", (merged.get("note") or "ok"))
    merged.setdefault("connected", merged.get("connected", None))
    merged.setdefault("rec_counts", merged.get("rec_counts") or {})

    # If stats.rec_counts exists, mirror it into top-level rec_counts.
    try:
        stats = merged.get("stats")
        if isinstance(stats, dict):
            rc = stats.get("rec_counts")
            if isinstance(rc, dict) and not merged.get("rec_counts"):
                merged["rec_counts"] = rc
    except Exception:
        pass

    missing_final = [k for k in _STATUS_REQUIRED_KEYS if k not in merged]
    if missing_final:
        try:
            print(f"[FUT][STATUS][REFUSE_WRITE] missing_final_keys={missing_final}")
        except Exception:
            pass
        return

    try:
        r.set(fut_status_key(), json.dumps(merged, separators=(",", ":"), ensure_ascii=False))
        if refused_direct:
            print("[FUT][STATUS][MERGE_WRITE] wrote_merged=1")
    except Exception:
        pass


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _compute_scores(quotes: Dict[str, Quote], state: ScoreState) -> dict:
    trend: Dict[str, float] = {}
    impulse: Dict[str, float] = {}

    abs_rets: list[float] = []
    bearish = 0

    for sym, q in quotes.items():
        lp = state.last_px.get(sym)
        if lp and lp > 0:
            ret = (q.px - lp) / lp
        else:
            ret = 0.0

        t = _clip(ret * 50.0, -1.0, 1.0)  # 2% move ~ 1.0
        trend[sym] = float(f"{t:.2f}")

        i = _clip(ret * 80.0, -1.0, 1.0)
        impulse[sym] = float(f"{i:.2f}")

        abs_rets.append(abs(ret))
        if t < -0.15:
            bearish += 1

    x = (sum(abs_rets) / max(1, len(abs_rets))) if abs_rets else 0.0

    alpha_fast = 0.15
    state.ema_abs_ret = (alpha_fast * x) + ((1 - alpha_fast) * state.ema_abs_ret)

    alpha_slow = 0.02
    state.ema_abs_ret_baseline = (alpha_slow * x) + ((1 - alpha_slow) * state.ema_abs_ret_baseline)

    base = max(1e-9, state.ema_abs_ret_baseline)
    vol_mult = _clip(state.ema_abs_ret / base, 0.25, 6.0)

    if bearish >= 2 and vol_mult >= 1.25:
        regime = "RISK_OFF"
    elif bearish == 0 and vol_mult <= 1.40:
        regime = "RISK_ON"
    elif vol_mult >= 1.60:
        regime = "CHOP"
    else:
        regime = "NEUTRAL"

    return {
        "trend": trend,
        "impulse": impulse,
        "vol_mult": float(f"{vol_mult:.2f}"),
        "breadth_bearish": int(bearish),
        "breadth_total": int(len(quotes)),
        "regime": regime,
        "updated_utc": _now(),
    }


def main() -> None:
    api_key = _get_setting("DATABENTO_API_KEY", "").strip()
    if not api_key.startswith("db-"):
        print("[FATAL] DATABENTO_API_KEY missing/invalid. Expected db-...")
        raise SystemExit(1)

    source = "databento"

    dataset = _get_setting("DATABENTO_DATASET", "GLBX.MDP3")
    schema = _get_setting("DATABENTO_LIVE_SCHEMA", _get_setting("DATABENTO_SCHEMA", "trades"))
    # Databento Live can require different symbology modes depending on entitlement and schema.
    # Support an AUTO mode which will try parent (ES.FUT) then continuous (ES.c.0) if we never
    # see a first print.
    live_stype_in_raw = (_get_setting("DATABENTO_LIVE_STYPE_IN", "") or "").strip().lower()
    live_stype_in = live_stype_in_raw if live_stype_in_raw else "auto"
    if live_stype_in not in {"auto", "parent", "continuous"}:
        live_stype_in = "auto"
    raw_live_start = (_get_setting("DATABENTO_LIVE_START", "") or "").strip()
    if raw_live_start in {"", "0"}:
        live_start = None
    else:
        try:
            live_start = int(raw_live_start)
        except Exception:
            live_start = raw_live_start

    live_snapshot = str(_get_setting("DATABENTO_LIVE_SNAPSHOT", "0") or "0").strip().lower() in {"1", "true", "yes"}
    hb_interval_sec = float(_get_setting("FUTURES_HEARTBEAT_INTERVAL_SEC", "10") or "10")
    hb_interval_sec = max(1.0, min(60.0, hb_interval_sec))

    log_level = (_get_setting("DATABENTO_LOG_LEVEL", "WARNING") or "WARNING").upper().strip()
    logging.basicConfig(level=getattr(logging, log_level, logging.WARNING))
    logging.getLogger("databento").setLevel(getattr(logging, log_level, logging.WARNING))

    syms = _env_symbols()

    def _db_symbols_for(stype: str) -> list[str]:
        return [_live_instrument_map(s, stype) for s in syms]

    # Boot watchdog: if we never see prints, we can retry with a different symbology mode.
    try:
        boot_timeout_sec = float(_get_setting("FUTURES_BOOT_TIMEOUT_SEC", "90") or "90")
    except Exception:
        boot_timeout_sec = 90.0
    boot_timeout_sec = max(10.0, min(10 * 60.0, boot_timeout_sec))

    r = _redis()

    # Observability: PID file + boot status (even if no trades print).
    try:
        Path("logs").mkdir(parents=True, exist_ok=True)
        (Path("logs") / "futures_ingest.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    boot_status = {
        "state": "running",
        "ts_utc": _now(),
        "updated_utc": _now(),
        "pid": os.getpid(),
        "source": source,
        "note": "waiting_for_first_trade",
        "dataset": dataset,
        "schema": schema,
        "stype_in": live_stype_in,
        "boot_timeout_sec": boot_timeout_sec,
        "symbols": list(syms),
        "connected": None,
        "rec_counts": {},
    }
    try:
        _write_fut_status_guarded(r, boot_status)
        _write_hb(r, "boot")
    except Exception:
        pass

    state = ScoreState(
        last_px={},
        last_ts={},
        ref_px={},
        ref_session_id={},
        ema_abs_ret=1e-6,
        ema_abs_ret_baseline=1e-6,
        updated_utc=_now(),
    )

    # Restore session reference prices so the panel's +/- isn't reset to zero on worker restart.
    try:
        cur_sess = _session_id_et()
        for s in syms:
            try:
                h = r.hgetall(fut_last_key(s))
            except Exception:
                h = None
            if not h:
                continue
            try:
                ref_px = float(h.get("ref_px", ""))
                ref_sess = int(float(h.get("session_id", "0")))
            except Exception:
                continue
            if ref_px > 0 and ref_sess == cur_sess:
                state.ref_px[s] = ref_px
                state.ref_session_id[s] = ref_sess
    except Exception:
        pass



    # Attempt order: try the configured mode first, then optionally fall back.
    # This is specifically to unblock the "awaiting_prints" bootstrap state when a
    # user configured stype_in but the chosen symbology doesn't yield prints.
    stype_fallback_raw = (_get_setting("FUTURES_STYPE_FALLBACK", "1") or "1").strip().lower()
    stype_fallback_enabled = stype_fallback_raw not in {"0", "false", "no", "off"}

    attempts: list[str]
    if live_stype_in == "auto":
        attempts = ["parent", "continuous"]
    else:
        attempts = [live_stype_in]
        if stype_fallback_enabled and live_stype_in in {"parent", "continuous"}:
            other = "continuous" if live_stype_in == "parent" else "parent"
            attempts.append(other)

    print("[futures_ingest] writing Redis keys: fut:{SYM}:last, fut:scores, fut:hb")

    # Shared state across attempts.
    last_write = 0.0
    write_interval = 0.5
    quotes: Dict[str, Quote] = {}
    instrument_id_to_root: Dict[int, str] = {}

    last_any_data_ts = time.time()
    last_any_data_lock = threading.Lock()

    rec_stats_lock = threading.Lock()
    rec_counts: Dict[str, int] = {}
    last_rec_type: str | None = None
    last_rec_symbol: str | None = None
    last_rec_instrument_id: int | None = None
    last_rec_price: float | None = None
    last_rec_price_scale: int | None = None
    last_drop_reason: str | None = None
    last_rec_seen_ts = time.time()

    on_err_count = 0
    seen_first_record = False

    def on_record(rec) -> None:
        nonlocal last_write, quotes, state
        nonlocal on_err_count
        nonlocal seen_first_record
        nonlocal last_any_data_ts
        nonlocal last_rec_type, last_rec_symbol, last_rec_instrument_id, last_rec_price, last_rec_price_scale
        nonlocal last_drop_reason, last_rec_seen_ts

        try:
            # Error/system/mapping records should be surfaced so we can diagnose.
            rec_type = type(rec).__name__

            if not seen_first_record:
                try:
                    sym0 = getattr(rec, "symbol", None)
                    iid0 = getattr(rec, "instrument_id", None)
                    seen_first_record = True
                    print(f"[futures_ingest] first_record type={rec_type} symbol={sym0!r} instrument_id={iid0!r}")
                except Exception:
                    pass

            try:
                sym_full_any = getattr(rec, "symbol", None)
                iid_any = getattr(rec, "instrument_id", None)
                px_any = getattr(rec, "price", None)
                pscale_any = getattr(rec, "price_scale", None)
                if pscale_any is None:
                    pscale_any = getattr(rec, "price_precision", None)
                with rec_stats_lock:
                    rec_counts[rec_type] = int(rec_counts.get(rec_type, 0)) + 1
                    last_rec_type = rec_type
                    last_rec_symbol = str(sym_full_any) if sym_full_any else None
                    try:
                        last_rec_instrument_id = int(iid_any) if iid_any is not None else None
                    except Exception:
                        last_rec_instrument_id = None
                    try:
                        last_rec_price = float(px_any) if px_any is not None else None
                    except Exception:
                        last_rec_price = None
                    try:
                        last_rec_price_scale = int(pscale_any) if pscale_any is not None else None
                    except Exception:
                        last_rec_price_scale = None
                    last_drop_reason = None
                    last_rec_seen_ts = time.time()
            except Exception:
                pass
            if rec_type == "ErrorMsg" or hasattr(rec, "err"):
                err = getattr(rec, "err", None)
                code = getattr(rec, "code", None)
                err_s = (str(err) if err is not None else "").strip()
                msg = "error"
                low = err_s.lower()
                if "rate" in low and "limit" in low:
                    msg = "rate_limited"
                elif "auth" in low or "unauthorized" in low or "forbidden" in low or "invalid" in low:
                    msg = "auth_error"
                elif "symbology" in low or "symbol" in low:
                    msg = "symbology_error"

                print(f"[futures_ingest] databento ErrorMsg code={code} err={err!r}")
                try:
                    _write_hb(r, msg)
                    st = {
                        "state": "error",
                        "ts_utc": _now(),
                        "updated_utc": _now(),
                        "pid": os.getpid(),
                        "source": source,
                        "note": msg,
                        "last_error": err_s[:400],
                        "last_error_code": str(code) if code is not None else None,
                    }
                    _write_fut_status_guarded(r, st)
                except Exception:
                    pass
                return
            if rec_type == "SystemMsg" or (hasattr(rec, "is_heartbeat") and callable(getattr(rec, "is_heartbeat"))):
                try:
                    if hasattr(rec, "is_heartbeat") and rec.is_heartbeat():
                        # Persist a liveness signal even if there are no trades.
                        try:
                            _write_hb(r, "ok")
                        except Exception:
                            pass
                        return
                except Exception:
                    pass

            if rec_type == "SymbolMappingMsg":
                iid = getattr(rec, "instrument_id", None)
                sym_out = (
                    getattr(rec, "symbol", "")
                    or getattr(rec, "symbol_out", "")
                    or getattr(rec, "stype_out_symbol", "")
                    or getattr(rec, "symbol_out_raw", "")
                    or ""
                )
                try:
                    iid_i = int(iid) if iid is not None else None
                except Exception:
                    iid_i = None
                if iid_i is not None:
                    root = _root_from_symbol(sym_out, syms)
                    if root:
                        instrument_id_to_root[iid_i] = root
                return

            sym_full = getattr(rec, "symbol", "") or ""
            root = _root_from_symbol(sym_full, syms) if sym_full else ""

            if root not in syms:
                iid = getattr(rec, "instrument_id", None)
                try:
                    iid_i = int(iid) if iid is not None else None
                except Exception:
                    iid_i = None
                if iid_i is not None and iid_i in instrument_id_to_root:
                    root = instrument_id_to_root[iid_i]
                else:
                    try:
                        with rec_stats_lock:
                            last_drop_reason = "unmapped_instrument"
                    except Exception:
                        pass
                    return

            # Databento trades often arrive in fixed-point integer form (e.g., nano price).
            # We keep this intentionally simple: for futures, raw trade prices are typically
            # enormous when unscaled; dividing by 1e9 restores human units.
            raw_px = getattr(rec, "price", 0.0) or 0.0
            px = float(raw_px)
            if px > 1e7:
                px = px / 1e9
            if px <= 0:
                try:
                    with rec_stats_lock:
                        last_drop_reason = "nonpositive_px"
                except Exception:
                    pass
                return

            try:
                with last_any_data_lock:
                    last_any_data_ts = time.time()
            except Exception:
                pass

            ts_utc = _now()
            quotes[root] = Quote(px=px, ts_utc=ts_utc)

            now = time.time()
            if now - last_write < write_interval:
                return
            last_write = now

            pipe = r.pipeline()
            for s, q in quotes.items():
                # Price change should reflect the *session move* (not the last-tick delta).
                sess_id = _session_id_et(q.ts_utc)
                ref_sess = state.ref_session_id.get(s)
                ref_px = state.ref_px.get(s)
                if (ref_sess is None) or (ref_sess != sess_id) or (ref_px is None) or (ref_px <= 0):
                    ref_sess = sess_id
                    ref_px = float(q.px)
                    state.ref_session_id[s] = int(ref_sess)
                    state.ref_px[s] = float(ref_px)

                chg = float(q.px) - float(ref_px)
                chg_pct = (chg / float(ref_px) * 100.0) if float(ref_px) > 0 else 0.0

                pipe.hset(
                    fut_last_key(s),
                    mapping={
                        "px": f"{q.px:.2f}",
                        "chg": f"{chg:.2f}",
                        "chg_pct": f"{chg_pct:.2f}",
                        "ref_px": f"{float(ref_px):.2f}",
                        "session_id": str(int(ref_sess)),
                        "ts_utc": str(q.ts_utc),
                        "source": source,
                    },
                )
                state.last_px[s] = q.px
                state.last_ts[s] = q.ts_utc

            scores = _compute_scores(quotes, state)
            pipe.set(fut_scores_key(), json.dumps(scores))
            # Heartbeat + alias.
            now_ts = str(_now())
            pipe.set(fut_hb_key(), now_ts)
            pipe.set("fut:hb:last_ts", now_ts)
            # Compatibility + ops: timestamp of last *data* update (not just heartbeat).
            pipe.set("fut:last_ts", now_ts)
            pipe.set(fut_hb_msg_key(), "ok")
            pipe.execute()

        except Exception as exc:
            # Avoid spamming logs on every record.
            on_err_count += 1
            if on_err_count <= 3:
                print(f"[futures_ingest] callback exception: {type(exc).__name__}: {exc}")
            return

    def _run_attempt(stype_in: str, *, break_on_boot_timeout: bool) -> None:
        nonlocal last_any_data_ts

        db_syms = _db_symbols_for(stype_in)
        print(
            f"[futures_ingest] dataset={dataset} schema={schema} stype_in={stype_in} symbols={syms} (db={db_syms}) start={live_start or 'REALTIME'}"
        )

        # Reset mapping per attempt.
        instrument_id_to_root.clear()

        client = db.Live(api_key, heartbeat_interval_s=10)

        # Subscribe (API may differ slightly across databento versions)
        sub_id = None
        if live_start is not None:
            sub_id = client.subscribe(
                dataset=dataset,
                schema=schema,
                symbols=db_syms,
                stype_in=stype_in,
                start=live_start,
                snapshot=live_snapshot,
            )
        else:
            sub_id = client.subscribe(dataset=dataset, schema=schema, symbols=db_syms, stype_in=stype_in, snapshot=live_snapshot)

        print(f"[futures_ingest] subscribed dataset={dataset} schema={schema} stype_in={stype_in} sub_id={sub_id}")

        # Prefer add_callback if present; fallback to callback arg if required.
        def on_cb_exc(exc: Exception) -> None:
            print(f"[futures_ingest] callback raised: {type(exc).__name__}: {exc}")

        add_cb = getattr(client, "add_callback", None)
        if callable(add_cb):
            add_cb(on_record, exception_callback=on_cb_exc)

        stop_event = threading.Event()
        block_exc: list[BaseException] = []

        def _blocker() -> None:
            try:
                client.block_for_close()
            except BaseException as exc:
                block_exc.append(exc)
            finally:
                stop_event.set()

        # Attempt metadata for ops.
        attempt_boot = {
            "state": "running",
            "ts_utc": _now(),
            "updated_utc": _now(),
            "pid": os.getpid(),
            "source": source,
            "note": "awaiting_prints",
            "dataset": dataset,
            "schema": schema,
            "stype_in": stype_in,
            "boot_timeout_sec": boot_timeout_sec,
            "attempts": list(attempts),
            "symbols": list(syms),
            "db_symbols": list(db_syms),
            "subscribe_id": sub_id,
            "connected": None,
            "rec_counts": {},
        }
        try:
            _write_fut_status_guarded(r, attempt_boot)
            _write_hb(r, "boot")
        except Exception:
            pass

        # Start and watch.
        client.start()
        print("[futures_ingest] started live session")
        threading.Thread(target=_blocker, daemon=True).start()

        boot_start = time.time()
        while not stop_event.is_set():
            try:
                with last_any_data_lock:
                    age = time.time() - last_any_data_ts
                note = "awaiting_prints" if age > hb_interval_sec else "ok"

                try:
                    try:
                        connected = bool(client.is_connected())
                    except Exception:
                        connected = None
                    try:
                        session_id = client.session_id()
                    except Exception:
                        session_id = None

                    with rec_stats_lock:
                        stats = {
                            "last_rec_type": last_rec_type,
                            "last_rec_symbol": last_rec_symbol,
                            "last_rec_instrument_id": last_rec_instrument_id,
                            "last_rec_price": last_rec_price,
                            "last_rec_price_scale": last_rec_price_scale,
                            "last_rec_age_s": round(time.time() - last_rec_seen_ts, 3),
                            "last_drop_reason": last_drop_reason,
                            # Keep this small; it's for ops/debug only.
                            "rec_counts": dict(list(rec_counts.items())[:12]),
                        }
                except Exception:
                    stats = None

                _write_fut_status_guarded(
                    r,
                    {
                        "state": "running",
                        "ts_utc": _now(),
                        "updated_utc": _now(),
                        "pid": os.getpid(),
                        "source": source,
                        "note": note,
                        "dataset": dataset,
                        "schema": schema,
                        "stype_in": stype_in,
                        "boot_timeout_sec": boot_timeout_sec,
                        "attempts": list(attempts),
                        "symbols": list(syms),
                        "db_symbols": list(db_syms),
                        "connected": connected,
                        "session_id": session_id,
                        "stats": stats,
                        "rec_counts": (stats or {}).get("rec_counts") if isinstance(stats, dict) else {},
                    },
                )
                _write_hb(r, note)
            except Exception:
                pass

            # If we never see prints, optionally return so caller can retry with another stype_in.
            if break_on_boot_timeout and (time.time() - boot_start) > boot_timeout_sec:
                try:
                    with last_any_data_lock:
                        no_data_age = time.time() - last_any_data_ts
                    if no_data_age >= boot_timeout_sec:
                        raise RuntimeError(f"no prints received after {int(boot_timeout_sec)}s")
                except Exception as exc:
                    try:
                        _write_fut_status_guarded(
                            r,
                            {
                                "state": "running",
                                "ts_utc": _now(),
                                "updated_utc": _now(),
                                "pid": os.getpid(),
                                "source": source,
                                "note": "awaiting_prints",
                                "dataset": dataset,
                                "schema": schema,
                                "stype_in": stype_in,
                                "boot_timeout_sec": boot_timeout_sec,
                                "attempts": list(attempts),
                                "symbols": list(syms),
                                "db_symbols": list(db_syms),
                                "last_error": str(exc)[:400],
                            },
                        )
                        _write_hb(r, "awaiting_prints")
                    except Exception:
                        pass
                    break

            time.sleep(hb_interval_sec)

        # Stop the attempt.
        try:
            client.stop()
        except Exception:
            pass

        if block_exc:
            raise block_exc[0]

    try:
        # Keep the worker alive indefinitely.
        while True:
            for i, stype in enumerate(attempts):
                try:
                    _run_attempt(stype, break_on_boot_timeout=(i < len(attempts) - 1))
                except Exception as exc:
                    print(f"[futures_ingest] attempt stype_in={stype} ended: {type(exc).__name__}: {exc}")
                    # Small backoff before trying the next attempt / reconnect.
                    time.sleep(2.0)
                    continue

                if i < len(attempts) - 1:
                    print(f"[futures_ingest] no prints; retrying with stype_in={attempts[i+1]}")
                    continue

            # If we got here, all attempts returned/failed (e.g., no prints + reconnect loop).
            time.sleep(2.0)
    except KeyboardInterrupt:
        pass
    except BaseException as exc:  # includes SystemExit
        print(f"[futures_ingest] fatal: {exc!r}")
        try:
            st = {
                "state": "error",
                "ts_utc": _now(),
                "updated_utc": _now(),
                "pid": os.getpid(),
                "source": source,
                "note": "fatal",
                "last_error": f"{type(exc).__name__}: {exc}"[:400],
            }
            _write_fut_status_guarded(r, st)
            _write_hb(r, "fatal")
        except Exception:
            pass
        traceback.print_exc()
        raise
        print("[futures_ingest] stopped")


if __name__ == "__main__":
    if not _acquire_singleton_lock():
        print("[FATAL] Another futures ingest process is already running (singleton lock active).")
        raise SystemExit(2)
    main()
