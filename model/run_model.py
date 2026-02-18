import json
import math
import os
import sqlite3
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import joblib
except Exception:  # noqa: BLE001
    joblib = None

DB_PATH = os.getenv("DB_PATH", "db/tnt.db")
TF = os.getenv("MODEL_TF", "1m")
_default_symbols = "SPY,QQQ,IWM,AAPL,MSFT,NVDA,AMZN,META,GOOGL,TSLA,VIX"
SYMBOLS = os.getenv("SYMBOLS", _default_symbols).split(",")
SYMBOLS = [s.strip().upper() for s in SYMBOLS if s.strip()]
HORIZON_MIN = int(os.getenv("MODEL_HORIZON_MIN", "5"))
MODEL_PATH = os.getenv("MODEL_PATH", "model/artifacts/logit.joblib")
MODEL_NAME = os.getenv("MODEL_NAME", "logit-v1")
LOOKBACK_ROWS = int(os.getenv("MODEL_LOOKBACK_ROWS", "1200"))

ET = ZoneInfo("America/New_York")
RTH_OPEN = dtime(9, 30)
RTH_START = dtime(9, 31)
RTH_CLOSE = dtime(16, 0)
PM_OPEN = dtime(4, 0)
AH_CLOSE = dtime(20, 0)
RTH_OPEN_MINUTES = RTH_OPEN.hour * 60 + RTH_OPEN.minute
RTH_CLOSE_MINUTES = RTH_CLOSE.hour * 60 + RTH_CLOSE.minute


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def market_session_et(dt_utc: datetime | None = None) -> str:
    dt_utc = dt_utc or datetime.now(timezone.utc)
    dt_et = dt_utc.astimezone(ET)
    if dt_et.weekday() >= 5:
        return "WEEKEND"
    t = dt_et.time()
    if RTH_START <= t < RTH_CLOSE:
        return "RTH"
    if PM_OPEN <= t < RTH_START:
        return "PRE"
    if RTH_CLOSE <= t < AH_CLOSE:
        return "AH"
    return "CLOSED"


def pivots_from_hlc(high: float, low: float, close: float) -> dict[str, float]:
    pivot = (high + low + close) / 3.0
    range_span = high - low
    return {
        "P": pivot,
        "R1": 2 * pivot - low,
        "S1": 2 * pivot - high,
        "R2": pivot + range_span,
        "S2": pivot - range_span,
    }


def get_latest_daily_bar(symbol: str) -> tuple[str, float, float, float] | None:
    query = (
        "SELECT ts, high, low, close "
        "FROM prices WHERE symbol=? AND tf='1d' ORDER BY ts DESC LIMIT 1"
    )
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(query, (symbol.upper(),)).fetchone()
    if not row:
        return None
    ts, high, low, close = row
    return str(ts), float(high), float(low), float(close)


def last_rth_session_from_df(df: pd.DataFrame) -> tuple[str, float, float, float] | None:
    if df.empty or "ts" not in df.columns:
        return None

    working = df.copy()
    ts_idx = pd.to_datetime(working["ts"], utc=True, errors="coerce")
    working = working.assign(ts_utc=ts_idx)
    working = working.dropna(subset=["ts_utc"])
    if working.empty:
        return None

    working = working.assign(ts_et=working["ts_utc"].dt.tz_convert(ET))
    weekdays = working["ts_et"].dt.weekday < 5
    minutes = working["ts_et"].dt.hour * 60 + working["ts_et"].dt.minute
    mask = weekdays & (minutes >= RTH_OPEN_MINUTES) & (minutes < RTH_CLOSE_MINUTES)
    rth = working.loc[mask]
    if rth.empty:
        return None

    last_date = rth["ts_et"].dt.date.iloc[-1]
    day = rth[rth["ts_et"].dt.date == last_date]
    high = float(day["high"].astype(float).max())
    low = float(day["low"].astype(float).min())
    close = float(day["close"].astype(float).iloc[-1])
    ts_str = str(day["ts"].iloc[-1])
    return ts_str, high, low, close


def get_pivot_context(symbol: str, df: pd.DataFrame) -> dict[str, object] | None:
    daily = get_latest_daily_bar(symbol)
    if daily:
        ts, high, low, close = daily
        pivots = pivots_from_hlc(high, low, close)
        return {
            "ts": ts,
            "high": high,
            "low": low,
            "close": close,
            "pivots": pivots,
            "range": high - low,
        }

    fallback = last_rth_session_from_df(df)
    if not fallback:
        return None
    ts, high, low, close = fallback
    pivots = pivots_from_hlc(high, low, close)
    return {
        "ts": ts,
        "high": high,
        "low": low,
        "close": close,
        "pivots": pivots,
        "range": high - low,
    }


def compute_intraday_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    if df.empty or len(df) < period + 1:
        return None

    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    closes = df["close"].astype(float)
    prev_close = closes.shift(1)

    tr = pd.concat(
        [
            highs - lows,
            (highs - prev_close).abs(),
            (lows - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_series = tr.rolling(period).mean()
    atr_val = atr_series.iloc[-1]
    if pd.isna(atr_val):
        return None
    return float(atr_val)


def classify_regime(trend: float, vol_z: float) -> str:
    trend_val = float(trend) if math.isfinite(trend) else 0.0
    vol_val = float(vol_z) if math.isfinite(vol_z) else 0.0

    if vol_val >= 1.5:
        return "vol_expansion"
    if abs(trend_val) >= 0.05:
        return "trend_up" if trend_val > 0 else "trend_down"
    if abs(trend_val) <= 0.03 and vol_val <= 1.2:
        return "range"
    return "mixed"


def enrich_meta(symbol: str, df: pd.DataFrame, base_meta: dict, last_price_ts: str) -> dict:
    meta = dict(base_meta or {})
    if df.empty:
        if last_price_ts:
            meta.setdefault("price_ts", str(last_price_ts))
        return meta

    closes = df["close"].astype(float)
    last_price = float(closes.iloc[-1])
    if last_price_ts:
        meta["price_ts"] = str(last_price_ts)

    ret_series = closes.pct_change()
    ret_1m = float(ret_series.iloc[-1]) if not ret_series.empty and math.isfinite(ret_series.iloc[-1]) else 0.0
    meta["ret_1m"] = ret_1m

    trend_1m = None
    if len(closes) >= 30:
        rolling_mean = float(closes.rolling(30).mean().iloc[-1])
        if math.isfinite(rolling_mean) and rolling_mean != 0:
            trend_1m = float(last_price / rolling_mean - 1.0)
    if trend_1m is None:
        existing_trend = meta.get("trend") or meta.get("trend_1m")
        if existing_trend is not None:
            try:
                trend_candidate = float(existing_trend)
            except (TypeError, ValueError):
                trend_candidate = 0.0
            trend_1m = trend_candidate if math.isfinite(trend_candidate) else 0.0
        else:
            trend_1m = 0.0
    meta["trend_1m"] = trend_1m
    meta["trend"] = trend_1m

    vol_z = meta.get("vol_z")
    if vol_z is not None:
        try:
            vol_z = float(vol_z)
        except (TypeError, ValueError):
            vol_z = None
    if len(ret_series) >= 60:
        absret = ret_series.abs()
        mu = float(absret.rolling(60).mean().iloc[-1])
        sd = float(absret.rolling(60).std().iloc[-1])
        if math.isfinite(mu) and math.isfinite(sd) and sd > 0:
            vol_z = float((abs(ret_1m) - mu) / (sd + 1e-9))
    if vol_z is None or not math.isfinite(vol_z):
        vol_z = 0.0
    meta["vol_z"] = vol_z

    atr_val = compute_intraday_atr(df)
    if atr_val is not None:
        meta["atr_1m"] = atr_val

    pivot_ctx = get_pivot_context(symbol, df)
    if pivot_ctx:
        pivots = pivot_ctx["pivots"]
        meta["pivots_ts"] = str(pivot_ctx["ts"])
        meta["dist_to_P"] = float(pivots["P"] - last_price)
        meta["dist_to_R1"] = float(pivots["R1"] - last_price)
        meta["dist_to_S1"] = float(pivots["S1"] - last_price)
        meta["em_points"] = float(pivot_ctx["range"])

    meta["regime"] = classify_regime(trend_1m, vol_z)
    return meta


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cur.fetchall()}
    return cols


def load_prices(symbol: str, tf: str = "1m", limit: int = 1200) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        query = (
            """
            SELECT ts, open, high, low, close
            FROM prices
            WHERE symbol=? AND tf=?
            ORDER BY ts DESC
            LIMIT ?
            """
        )
        df = pd.read_sql_query(query, conn, params=(symbol, tf, limit))

    if df.empty:
        return df
    df = df.sort_values("ts")
    for column in ("open", "high", "low", "close"):
        df[column] = df[column].astype(float)
    return df


def make_features(df: pd.DataFrame) -> pd.DataFrame:
    prices = df["close"]

    ret1 = prices.pct_change()
    ret5 = prices.pct_change(5)
    ret15 = prices.pct_change(15)

    vol10 = ret1.rolling(10).std()
    vol30 = ret1.rolling(30).std()

    mom10 = prices / prices.rolling(10).mean() - 1.0
    mom30 = prices / prices.rolling(30).mean() - 1.0

    up = ret1.clip(lower=0).rolling(14).mean()
    down = (-ret1.clip(upper=0)).rolling(14).mean()
    rs = up / (down + 1e-9)
    rsi = 100 - (100 / (1 + rs))

    features = pd.DataFrame(
        {
            "ret1": ret1,
            "ret5": ret5,
            "ret15": ret15,
            "vol10": vol10,
            "vol30": vol30,
            "mom10": mom10,
            "mom30": mom30,
            "rsi": rsi,
        }
    )

    return features


def heuristic_prob_up(df: pd.DataFrame) -> tuple[float, dict]:
    features = make_features(df)
    last = features.iloc[-1]
    if not np.isfinite(last.get("mom10", np.nan)):
        return 0.50, {"reason": "not_enough_bars"}

    mom = float(last["mom10"])
    vol = float(last["vol10"]) if np.isfinite(last.get("vol10", np.nan)) else 0.0

    prob = 0.5 + np.clip(mom * 2.0, -0.12, 0.12)
    prob = 0.5 + (prob - 0.5) * float(np.clip(0.03 / (vol + 1e-6), 0.5, 1.0))

    prob = float(np.clip(prob, 0.01, 0.99))
    meta = {"mom10": mom, "vol10": vol, "fallback": True}
    return prob, meta


def predict_prob_up(model, df: pd.DataFrame) -> tuple[float, dict]:
    features = make_features(df)
    last_row = features.iloc[-1:]
    if last_row.isna().any(axis=1).iloc[0]:
        prob, meta = heuristic_prob_up(df)
        meta.setdefault("reason", "nan_features")
        meta["fallback"] = True
        return prob, meta

    prob_up = float(model.predict_proba(last_row)[0][1])

    last = features.iloc[-1]
    ret_1m = float(last["ret1"]) if np.isfinite(last.get("ret1", np.nan)) else 0.0
    trend = float(last["mom30"]) if np.isfinite(last.get("mom30", np.nan)) else 0.0

    absret = df["close"].pct_change().abs()
    mu = absret.rolling(60).mean().iloc[-1]
    sd = absret.rolling(60).std().iloc[-1]
    if np.isfinite(mu) and np.isfinite(sd):
        vol_z = float((abs(ret_1m) - mu) / (sd + 1e-9))
    else:
        vol_z = 0.0

    meta = {"ret_1m": ret_1m, "trend": trend, "vol_z": vol_z, "fallback": False}
    return prob_up, meta


def write_signal(symbol: str, prob_up: float, last_price_ts: str, meta: dict, model_name: str):
    ts_now = now_utc_iso()
    prob_down = 1.0 - prob_up
    edge = abs(prob_up - 0.5)
    score = edge

    try:
        last_dt = parse_iso(last_price_ts)
    except Exception:  # noqa: BLE001
        last_dt = None
    meta = dict(meta)
    meta["session"] = market_session_et(last_dt)

    row = {
        "symbol": symbol,
        "ts": ts_now,
        "horizon_min": HORIZON_MIN,
        "prob_up": prob_up,
        "prob_down": prob_down,
        "model": model_name,
        "model_version": model_name,
        "last_price_ts": last_price_ts,
        "edge": edge,
        "score": score,
        "ret_1m": float(meta.get("ret_1m", 0.0)),
        "trend": float(meta.get("trend", 0.0)),
        "vol_z": float(meta.get("vol_z", 0.0)),
        "meta": str(meta),
        "meta_json": json.dumps(meta),
        "ok": 1,
        "duration_ms": None,
        "error": None,
    }

    start_time = time.time()
    with sqlite3.connect(DB_PATH) as conn:
        cols = table_columns(conn, "signals")
        insert_cols = [key for key in row if key in cols and row[key] is not None]
        values = [row[key] for key in insert_cols]

        if not insert_cols:
            raise RuntimeError("signals table has no matching columns for insert.")

        placeholders = ",".join(["?"] * len(insert_cols))
        sql = f"INSERT INTO signals ({','.join(insert_cols)}) VALUES ({placeholders})"
        conn.execute(sql, values)
        conn.commit()

    duration_ms = int((time.time() - start_time) * 1000)

    print(
        "[OK] {symbol}: wrote signal prob_up={prob_up:.3f} last_price_ts={last_price_ts} "
        "model={model_name} edge={edge:.3f} dur_ms={duration_ms}".format(
            symbol=symbol,
            prob_up=prob_up,
            last_price_ts=last_price_ts,
            model_name=model_name,
            edge=edge,
            duration_ms=duration_ms,
        )
    )


def main():
    model = None
    model_path = Path(MODEL_PATH)
    if model_path.exists() and joblib is not None:
        try:
            model = joblib.load(model_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] failed to load model artifact {MODEL_PATH}: {exc}. Falling back to heuristic.")
            model = None
    else:
        if not model_path.exists():
            print(f"[WARN] model artifact not found at {MODEL_PATH}. Falling back to heuristic.")
        if joblib is None:
            print("[WARN] joblib not available. Falling back to heuristic.")

    for symbol in SYMBOLS:
        df = load_prices(symbol, TF, LOOKBACK_ROWS)
        if df.empty or len(df) < 50:
            print(f"[WARN] {symbol}: not enough price rows for tf={TF}. Skipping.")
            continue

        last_price_ts = str(df["ts"].iloc[-1])
        try:
            if model is not None:
                prob_up, meta = predict_prob_up(model, df)
                model_name = MODEL_NAME
            else:
                prob_up, meta = heuristic_prob_up(df)
                model_name = "heuristic-v1"

            prob_up = float(np.clip(prob_up, 0.001, 0.999))
            meta = enrich_meta(symbol, df, meta, last_price_ts)
            write_signal(symbol, prob_up, last_price_ts, meta, model_name)

        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {symbol}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
