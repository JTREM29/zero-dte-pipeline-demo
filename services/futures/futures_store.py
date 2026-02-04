from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import redis

from .futures_keys import fut_heartbeat_key, fut_heartbeat_msg_key, fut_intel_key, fut_last_key, fut_scores_key, fut_status_key
from .futures_models import FutQuote, FutScores


def _repo_root() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
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


def _redis() -> redis.Redis:
    host = _get_setting("TNT_REDIS_HOST", "127.0.0.1")
    port = int(_get_setting("TNT_REDIS_PORT", "6379"))
    db = int(_get_setting("TNT_REDIS_DB", "0"))
    return redis.Redis(host=host, port=port, db=db, decode_responses=True)


class FuturesStore:
    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or _redis()

    def get_quotes(self, syms: List[str]) -> Dict[str, FutQuote]:
        out: Dict[str, FutQuote] = {}
        for sym in syms:
            k = fut_last_key(sym)
            h = self.r.hgetall(k)
            if not h:
                continue
            try:
                out[sym] = FutQuote(
                    sym=sym,
                    px=float(h.get("px", "nan")),
                    chg=float(h.get("chg", "0")),
                    chg_pct=float(h.get("chg_pct", "0")),
                    ts_utc=int(float(h.get("ts_utc", "0"))),
                    source=h.get("source", "unknown"),
                )
            except Exception:
                continue
        return out

    def get_quotes_with_fallback(self, syms: List[str]) -> Dict[str, FutQuote]:
        """Get quotes from Redis; if missing, try Databento Historical last bar.

        This keeps /futures useful on holidays/weekends when Live has no trades
        but Historical has a last known close.
        """

        quotes = self.get_quotes(syms)
        missing = [s for s in syms if s not in quotes]
        if not missing:
            return quotes

        enabled = (_get_setting("FUTURES_HIST_FALLBACK_ENABLED", "1") or "1").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return quotes

        try:
            hist_quotes = _fetch_hist_lastbar_quotes(missing)
        except Exception:
            hist_quotes = {}

        quotes.update(hist_quotes)
        return quotes

    def get_scores(self) -> Optional[FutScores]:
        raw = self.r.get(fut_scores_key())
        if not raw:
            raw = self.r.get(fut_intel_key())
        if not raw:
            return None
        try:
            obj = json.loads(raw)
            return FutScores(
                trend=obj.get("trend", {}) or {},
                impulse=obj.get("impulse", {}) or {},
                vol_mult=float(obj.get("vol_mult", 1.0)),
                breadth_bearish=int(obj.get("breadth_bearish", 0)),
                breadth_total=int(obj.get("breadth_total", 3)),
                regime=str(obj.get("regime", "NEUTRAL")),
                updated_utc=int(obj.get("updated_utc", int(time.time()))),
            )
        except Exception:
            return None

    def get_status(self) -> dict | None:
        """Return fut:status JSON as a dict (or None)."""

        try:
            raw = self.r.get(fut_status_key())
        except Exception:
            raw = None
        if not raw:
            return None
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def get_heartbeat(self) -> tuple[int | None, str | None]:
        """Return (hb_ts_utc_epoch, hb_msg)."""

        hb = None
        msg = None
        try:
            raw = self.r.get(fut_heartbeat_key())
            if raw:
                hb = int(float(raw))
        except Exception:
            hb = None
        try:
            msg = self.r.get(fut_heartbeat_msg_key())
        except Exception:
            msg = None
        return hb, msg


def _db_symbol(root: str, stype_in: str) -> str:
    r = root.upper().strip()
    si = (stype_in or "").strip().lower()
    if si == "continuous":
        return f"{r}.c.0"
    if si == "parent":
        return f"{r}.FUT"
    return r


def _fetch_hist_lastbar_quotes(roots: List[str]) -> Dict[str, FutQuote]:
    api_key = _get_setting("DATABENTO_API_KEY", "").strip()
    if not api_key:
        return {}

    # Lazy import to keep bot startup light.
    import databento as db  # type: ignore
    import pandas as pd  # type: ignore

    dataset = _get_setting("DATABENTO_DATASET", "GLBX.MDP3")
    stype_in = (_get_setting("DATABENTO_STYPE_IN", "continuous") or "continuous").strip().lower()

    try:
        lag_sec = int(_get_setting("DATABENTO_END_LAG_SEC", "120"))
    except Exception:
        lag_sec = 120
    lag_sec = max(0, min(lag_sec, 3600))

    end = int(time.time()) - lag_sec
    start = end - (6 * 60 * 60)  # last 6 hours of bars is usually enough for a last close

    start_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start))
    end_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end))

    symbols = [_db_symbol(r, stype_in) for r in roots]
    hist = db.Historical(api_key)
    data = hist.timeseries.get_range(
        dataset=dataset,
        schema="ohlcv-1m",
        symbols=symbols,
        stype_in=stype_in,
        start=start_s,
        end=end_s,
    )

    df = data.to_df()
    if df is None or len(df) == 0:
        return {}

    out: Dict[str, FutQuote] = {}

    def _epoch_from_ts(ts_val) -> int:
        try:
            x = pd.to_datetime(ts_val, utc=True)
            return int(x.value // 1_000_000_000)
        except Exception:
            try:
                return int(getattr(ts_val, "timestamp")())
            except Exception:
                return int(time.time())

    # Databento OHLCV may be MultiIndex; try to normalize with a symbol column.
    symbol_col = "symbol" if "symbol" in df.columns else None
    if symbol_col is None and hasattr(df.index, "names"):
        # common: MultiIndex with first level "symbol"
        if "symbol" in list(df.index.names):
            df = df.reset_index()
            symbol_col = "symbol"

    ts_col = "ts_event" if "ts_event" in df.columns else None
    if ts_col is None and ("ts_event" in getattr(df.index, "names", []) or df.index.name == "ts_event"):
        df = df.reset_index()
        ts_col = "ts_event"

    for root in roots:
        db_sym = _db_symbol(root, stype_in)
        try:
            sub = df[df[symbol_col] == db_sym] if symbol_col else df
        except Exception:
            sub = df
        if sub is None or len(sub) == 0:
            continue

        last = sub.iloc[-1]
        prev = sub.iloc[-2] if len(sub) >= 2 else None

        try:
            px = float(last["close"])
        except Exception:
            continue

        ts_utc = _epoch_from_ts(last[ts_col]) if ts_col else int(time.time())

        chg = 0.0
        chg_pct = 0.0
        if prev is not None:
            try:
                prev_px = float(prev["close"])
                if prev_px:
                    chg = px - prev_px
                    chg_pct = (chg / prev_px) * 100.0
            except Exception:
                pass

        out[root] = FutQuote(
            sym=root,
            px=px,
            chg=chg,
            chg_pct=chg_pct,
            ts_utc=ts_utc,
            source="databento_hist",
        )

    return out
