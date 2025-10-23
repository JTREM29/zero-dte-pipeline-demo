"""Polygon.io client abstraction.

Contains minimal REST helper for previous aggregate close (SPX) and snapshot placeholder.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import os
import requests
import time
from functools import lru_cache
import pandas as pd
from datetime import datetime, timedelta, date

from src.utils.logging_setup import get_logger


@dataclass
class PolygonConfig:
    api_key: str

    @classmethod
    def from_env(cls) -> "PolygonConfig":
        key = os.getenv("POLYGON_API_KEY", "")
        if not key:
            raise RuntimeError("POLYGON_API_KEY not set in environment")
        return cls(api_key=key)


class PolygonClient:
    def __init__(self, cfg: PolygonConfig):
        self._cfg = cfg
        self.log = get_logger("polygon")
        self.base = "https://api.polygon.io"
        self.api_key = cfg.api_key
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.cache_ttl = 30.0  # seconds

    def _cached(self, key: str) -> Optional[dict[str, Any]]:
        now = time.time()
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, payload = entry
        if now - ts > self.cache_ttl:
            self._cache.pop(key, None)
            return None
        return payload

    def _store_cache(self, key: str, payload: dict[str, Any]) -> None:
        self._cache[key] = (time.time(), payload)

    def fetch_underlying_snapshot(self, symbol: str) -> dict[str, Any]:
        # Placeholder snapshot; real endpoint would call /v2/snapshot...
        return {"symbol": symbol, "lastPrice": 0.0, "source": "polygon", "_demo": True}

    def _request(self, url: str, params: dict[str, Any], retries: int = 3, backoff: float = 0.5) -> Optional[dict[str, Any]]:
        for attempt in range(1, retries + 1):
            try:
                r = requests.get(url, params=params, timeout=10)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("Attempt %s failed: %s", attempt, exc)
                time.sleep(backoff * attempt)
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as exc:  # noqa: BLE001
                    self.log.error("JSON decode error: %s", exc)
                    return None
            else:
                self.log.warning("Non-200 (%s) attempt %s: %s", r.status_code, attempt, r.text[:200])
                time.sleep(backoff * attempt)
        return None

    def last_trade_spx(self, use_cache: bool = True) -> Optional[dict[str, Any]]:
        """Fetch previous aggregate bar for the S&P 500 index using Polygon index ticker I:SPX.

        Polygon uses the I: prefix for index tickers. This calls /v2/aggs/ticker/I:SPX/prev.
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        cache_key = "spx_prev"
        if use_cache and (cached := self._cached(cache_key)):
            return cached
        url = f"{self.base}/v2/aggs/ticker/I:SPX/prev"
        params = {"adjusted": "true", "apiKey": self.api_key}
        data = self._request(url, params)
        if data:
            self._store_cache(cache_key, data)
        return data

    def last_trade_symbol(self, symbol: str, use_cache: bool = True) -> Optional[dict[str, Any]]:
        """Fetch previous aggregate bar for an arbitrary ticker symbol (e.g., SPY)."""
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        cache_key = f"prev_{symbol}"
        if use_cache and (cached := self._cached(cache_key)):
            return cached
        url = f"{self.base}/v2/aggs/ticker/{symbol}/prev"
        params = {"adjusted": "true", "apiKey": self.api_key}
        data = self._request(url, params)
        if data:
            self._store_cache(cache_key, data)
        return data

    def fetch_index_options_contracts(
        self,
        underlying: str = "I:SPX",
        expiration_date: Optional[str] = None,
        limit: int = 1000,
        max_pages: int = 30,
    ) -> Optional[dict[str, Any]]:
        """Fetch list of options contracts for an index underlying (e.g., I:SPX) from Polygon v3 reference.

        Uses /v3/reference/options/contracts with pagination via next_url.
        Returns a dict with keys: results (raw list), symbols (tickers list), next_url (last), count.
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None

        base_url = f"{self.base}/v3/reference/options/contracts"
        params: dict[str, Any] = {"underlying_ticker": underlying, "limit": limit, "apiKey": self.api_key}
        if expiration_date:
            params["expiration_date"] = expiration_date
        results: list[dict[str, Any]] = []

        url = base_url
        pages = 0
        while url and pages < max_pages:
            pages += 1
            data = self._request(url, params if url == base_url else {"apiKey": self.api_key})
            if not data or not isinstance(data, dict):
                break
            items = data.get("results")
            if isinstance(items, list):
                results.extend(items)
            next_url = data.get("next_url")
            if isinstance(next_url, str) and next_url:
                url = next_url
                params = {"apiKey": self.api_key}
            else:
                break

        symbols: list[str] = []
        for it in results:
            t = it.get("ticker") if isinstance(it, dict) else None
            if isinstance(t, str):
                symbols.append(t)
        payload = {"count": len(symbols), "symbols": symbols, "results": results}
        return payload

    @staticmethod
    def normalize_prev_agg(payload: dict[str, Any]) -> Optional[pd.DataFrame]:
        # Expected structure: { results: [ { "o":..., "h":..., "l":..., "c":..., "v":..., "t": ... } ], ... }
        results = payload.get("results") if isinstance(payload, dict) else None
        if not results or not isinstance(results, list):
            return None
        df = pd.DataFrame(results)
        # Rename columns to readable names
        rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"}
        df = df.rename(columns=rename_map)
        return df

    # ----- Options snapshots and index aggregates -----
    def fetch_option_snapshot(self, option_ticker: str) -> Optional[dict[str, Any]]:
        """Fetch Polygon v3 option snapshot for a single option ticker (e.g., O:SPXW251001C06000000)."""
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        url = f"{self.base}/v3/snapshot/options/{option_ticker}"
        params = {"apiKey": self.api_key}
        return self._request(url, params)

    def fetch_index_daily_aggs(self, ticker: str = "I:SPX", years: int = 10) -> Optional[pd.DataFrame]:
        """Fetch daily aggregates for an index ticker for the past N years."""
        if not self.api_key:
            return None
        end = datetime.utcnow().date()
        start = end - timedelta(days=365 * max(1, years) + 7)
        url = f"{self.base}/v2/aggs/ticker/{ticker}/range/1/day/{start:%Y-%m-%d}/{end:%Y-%m-%d}"
        params = {"adjusted": "true", "limit": 50000, "apiKey": self.api_key}
        data = self._request(url, params)
        if not data or not isinstance(data, dict):
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        df = pd.DataFrame(results)
        rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"}
        df = df.rename(columns=rename_map)
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(None).dt.date
        df["ret"] = df["close"].pct_change()
        df["dow"] = pd.to_datetime(df["date"]).dt.dayofweek
        df["month"] = pd.to_datetime(df["date"]).dt.month
        return df

    def fetch_equity_daily_aggs(self, ticker: str = "SPY", years: int = 10) -> Optional[pd.DataFrame]:
        """Fetch daily aggregates for an equity ticker as a proxy for seasonality (e.g., SPY)."""
        if not self.api_key:
            return None
        end = datetime.utcnow().date()
        start = end - timedelta(days=365 * max(1, years) + 7)
        url = f"{self.base}/v2/aggs/ticker/{ticker}/range/1/day/{start:%Y-%m-%d}/{end:%Y-%m-%d}"
        params = {"adjusted": "true", "limit": 50000, "apiKey": self.api_key}
        data = self._request(url, params)
        if not data or not isinstance(data, dict):
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        df = pd.DataFrame(results)
        rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"}
        df = df.rename(columns=rename_map)
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(None).dt.date
        df["ret"] = df["close"].pct_change()
        df["dow"] = pd.to_datetime(df["date"]).dt.dayofweek
        df["month"] = pd.to_datetime(df["date"]).dt.month
        return df

    def fetch_intraday_minute_aggs_day(self, ticker: str, day: date) -> Optional[pd.DataFrame]:
        """Fetch intraday 1-minute aggregates for a single day for any ticker (equity or index).

        Endpoint: /v2/aggs/ticker/{ticker}/range/1/minute/{day}/{day}
        Returns DataFrame with columns [timestamp, open, high, low, close, volume].
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        # Use 'minute' (not 'min') for the timespan spec
        url = f"{self.base}/v2/aggs/ticker/{ticker}/range/1/minute/{day:%Y-%m-%d}/{day:%Y-%m-%d}"
        params: dict[str, Any] = {"adjusted": "true", "limit": 50000, "sort": "asc", "apiKey": self.api_key}
        data = self._request(url, params)
        if not data or not isinstance(data, dict):
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        df = pd.DataFrame(results)
        rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"}
        df = df.rename(columns=rename_map)
        return df

    # ----- Option aggregates (historical minute/second bars) -----
    def fetch_option_aggs(
        self,
        option_ticker: str,
        start: datetime,
        end: datetime,
        timespan: str = "minute",
        mult: int = 1,
        adjusted: bool = True,
        limit: int = 50000,
        sort: str = "asc",
    ) -> Optional[pd.DataFrame]:
        """Fetch historical aggregates for a single option ticker (e.g., O:SPXW250101C06000000).

        Uses /v2/aggs/ticker/{ticker}/range/{mult}/{timespan}/{from}/{to}
        Returns a DataFrame with columns [timestamp, open, high, low, close, volume].
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        # Ensure ticker has O: prefix
        tkr = option_ticker if option_ticker.startswith("O:") else f"O:{option_ticker}"
        url = f"{self.base}/v2/aggs/ticker/{tkr}/range/{int(mult)}/{timespan}/{start:%Y-%m-%d}/{end:%Y-%m-%d}"
        params: dict[str, Any] = {
            "adjusted": str(adjusted).lower(),
            "limit": int(limit),
            "sort": sort,
            "apiKey": self.api_key,
        }
        frames: list[pd.DataFrame] = []
        cursor = None
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            data = self._request(url, p)
            if not data or not isinstance(data, dict):
                break
            results = data.get("results")
            if isinstance(results, list) and results:
                df = pd.DataFrame(results)
                # rename
                rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"}
                df = df.rename(columns=rename_map)
                frames.append(df)
            # pagination via next_url or next_url/cursor token
            cursor = data.get("next_url") or data.get("next_url_params", {}).get("cursor") or data.get("next_page_token") or data.get("next_cursor") or None
            # Some responses include next_url like ".../range/...?...&cursor=abc"; extract token if present
            if isinstance(cursor, str) and cursor.startswith("http"):
                import urllib.parse as _up
                try:
                    q = _up.urlparse(cursor).query
                    qs = _up.parse_qs(q)
                    cursor = (qs.get("cursor") or [None])[0]
                except Exception:
                    cursor = None
            if not cursor:
                break
        if not frames:
            return None
        out = pd.concat(frames, ignore_index=True)
        # convert ms to UTC datetime-like seconds (leave as ms if needed by caller)
        # Caller will handle window filtering to RTH
        return out

    # ----- Option historical quotes (NBBO) -----
    def fetch_option_quotes(
        self,
        option_ticker: str,
        start: datetime,
        end: datetime,
        limit: int = 50000,
        sort: str = "asc",
    ) -> Optional[pd.DataFrame]:
        """Fetch historical quotes for a single option ticker via Polygon v3.

        Endpoint: /v3/quotes/{optionsTicker}
        Filters by timestamp.gte / timestamp.lt in NANOSECONDS since epoch.
        Returns DataFrame with columns: [timestamp, bid, ask, bid_size, ask_size].
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        tkr = option_ticker if option_ticker.startswith("O:") else f"O:{option_ticker}"
        url = f"{self.base}/v3/quotes/{tkr}"
        def _to_ns(dt: datetime) -> int:
            try:
                return int(dt.timestamp() * 1_000_000_000)
            except Exception:
                return 0
        params: dict[str, Any] = {
            "timestamp.gte": _to_ns(start),
            "timestamp.lt": _to_ns(end),
            "limit": int(limit),
            "sort": sort,
            "apiKey": self.api_key,
        }
        frames: list[pd.DataFrame] = []
        next_url: Optional[str] = None
        while True:
            p = params if next_url is None else {"apiKey": self.api_key}
            data = self._request(next_url or url, p)
            if not data or not isinstance(data, dict):
                break
            results = data.get("results")
            if isinstance(results, list) and results:
                # Map known fields
                recs = []
                for q in results:
                    if not isinstance(q, dict):
                        continue
                    # Prefer SIP timestamp if present
                    ts_raw = q.get("sip_timestamp") or q.get("t") or q.get("timestamp")
                    try:
                        ts = float(ts_raw or 0)
                    except Exception:
                        ts = 0.0
                    # Normalize to seconds
                    if ts > 1e15:  # ns
                        ts /= 1e9
                    elif ts > 1e12:  # ms
                        ts /= 1e3
                    bid = q.get("bid_price") or q.get("bp") or 0.0
                    ask = q.get("ask_price") or q.get("ap") or 0.0
                    bs = q.get("bid_size") or q.get("bs") or 0
                    az = q.get("ask_size") or q.get("as") or 0
                    try:
                        recs.append({
                            "timestamp": float(ts),
                            "bid": float(bid or 0),
                            "ask": float(ask or 0),
                            "bid_size": float(bs or 0),
                            "ask_size": float(az or 0),
                        })
                    except Exception:
                        continue
                if recs:
                    frames.append(pd.DataFrame.from_records(recs))
            # pagination: v3 returns next_url
            nu = data.get("next_url")
            next_url = nu if isinstance(nu, str) and nu else None
            if next_url is None:
                break
        if not frames:
            return None
        out = pd.concat(frames, ignore_index=True)
        return out

    # ----- Option historical trades -----
    def fetch_option_trades(
        self,
        option_ticker: str,
        start: datetime,
        end: datetime,
        limit: int = 50000,
        sort: str = "asc",
    ) -> Optional[pd.DataFrame]:
        """Fetch historical trades for a single option ticker via Polygon v3.

        Endpoint: /v3/trades/{optionsTicker}
        Filters by timestamp.gte / timestamp.lt in NANOSECONDS since epoch.
        Returns DataFrame with columns: [timestamp, price, size, conditions?].
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        tkr = option_ticker if option_ticker.startswith("O:") else f"O:{option_ticker}"
        url = f"{self.base}/v3/trades/{tkr}"
        def _to_ns(dt: datetime) -> int:
            try:
                return int(dt.timestamp() * 1_000_000_000)
            except Exception:
                return 0
        params: dict[str, Any] = {
            "timestamp.gte": _to_ns(start),
            "timestamp.lt": _to_ns(end),
            "limit": int(limit),
            "sort": sort,
            "apiKey": self.api_key,
        }
        frames: list[pd.DataFrame] = []
        next_url: Optional[str] = None
        while True:
            p = params if next_url is None else {"apiKey": self.api_key}
            data = self._request(next_url or url, p)
            if not data or not isinstance(data, dict):
                break
            results = data.get("results")
            if isinstance(results, list) and results:
                recs = []
                for tr in results:
                    if not isinstance(tr, dict):
                        continue
                    ts_raw = tr.get("sip_timestamp") or tr.get("t") or tr.get("timestamp")
                    try:
                        ts = float(ts_raw or 0)
                    except Exception:
                        ts = 0.0
                    if ts > 1e15:  # ns
                        ts /= 1e9
                    elif ts > 1e12:  # ms
                        ts /= 1e3
                    px = tr.get("price") or tr.get("p") or 0.0
                    sz = tr.get("size") or tr.get("s") or 0
                    try:
                        recs.append({
                            "timestamp": float(ts),
                            "price": float(px or 0),
                            "size": float(sz or 0),
                        })
                    except Exception:
                        continue
                if recs:
                    frames.append(pd.DataFrame.from_records(recs))
            nu = data.get("next_url")
            next_url = nu if isinstance(nu, str) and nu else None
            if next_url is None:
                break
        if not frames:
            return None
        out = pd.concat(frames, ignore_index=True)
        return out

    # ----- Intraday indicators (computed locally from Polygon minute bars) -----
    def fetch_index_intraday_aggs(
        self,
        ticker: str = "I:SPX",
        timespan: str = "minute",
        mult: int = 1,
        lookback_days: int = 2,
        limit: int = 50000,
    ) -> Optional[pd.DataFrame]:
        """Fetch recent intraday aggregates for an index ticker.

        We query the last couple of days of minute bars and let callers filter to today/RTH.
        """
        if not self.api_key:
            return None
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, int(lookback_days)))
        url = f"{self.base}/v2/aggs/ticker/{ticker}/range/{int(mult)}/{timespan}/{start:%Y-%m-%d}/{end:%Y-%m-%d}"
        params = {"adjusted": "true", "limit": int(limit), "sort": "asc", "apiKey": self.api_key}
        data = self._request(url, params)
        if not data or not isinstance(data, dict):
            return None
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return None
        df = pd.DataFrame(results)
        rename_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "t": "timestamp"}
        df = df.rename(columns=rename_map)
        # Ensure timestamp is pandas datetime (UTC)
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df

    @staticmethod
    def compute_indicators(df: pd.DataFrame, *, sma_win: int = 20, ema_win: int = 20,
                           macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9,
                           rsi_win: int = 14) -> Optional[dict[str, float]]:
        """Compute SMA, EMA, MACD, RSI on a minute-bar DataFrame with a 'close' column.

        Returns a dict with latest values or None if insufficient data.
        """
        try:
            s = df["close"].astype(float).copy()
        except Exception:
            return None
        if len(s) < max(ema_win, macd_slow + macd_signal + 5, rsi_win + 5):
            return None
        # SMA/EMA
        sma = s.rolling(window=int(sma_win), min_periods=int(sma_win)).mean()
        ema = s.ewm(span=int(ema_win), adjust=False).mean()
        # MACD
        ema_fast = s.ewm(span=int(macd_fast), adjust=False).mean()
        ema_slow = s.ewm(span=int(macd_slow), adjust=False).mean()
        macd = ema_fast - ema_slow
        macd_sig = macd.ewm(span=int(macd_signal), adjust=False).mean()
        macd_hist = macd - macd_sig
        # RSI (Wilder)
        delta = s.diff()
        gain = (delta.clip(lower=0)).ewm(alpha=1.0 / rsi_win, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / rsi_win, adjust=False).mean()
        rs = gain / loss.replace({0.0: float('nan')})
        rsi = 100 - (100 / (1 + rs))
        vals: dict[str, float] = {}
        def _maybe_put(k: str, v: float | None):
            if v is None:
                return
            try:
                vals[k] = float(v)
            except Exception:
                pass
        _maybe_put("sma", float(sma.iloc[-1]) if pd.notna(sma.iloc[-1]) else None)
        _maybe_put("ema", float(ema.iloc[-1]) if pd.notna(ema.iloc[-1]) else None)
        _maybe_put("macd", float(macd.iloc[-1]) if pd.notna(macd.iloc[-1]) else None)
        _maybe_put("macd_signal", float(macd_sig.iloc[-1]) if pd.notna(macd_sig.iloc[-1]) else None)
        _maybe_put("macd_hist", float(macd_hist.iloc[-1]) if pd.notna(macd_hist.iloc[-1]) else None)
        _maybe_put("rsi", float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None)
        _maybe_put("close", float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else None)
        return vals if vals else None

    def latest_indicator_snapshot(self, ticker: str = "I:SPX") -> Optional[dict[str, float]]:
        """Convenience: fetch recent minute bars and compute indicators; return latest values.

        Intended for light enrichment in agent summaries.
        """
        try:
            df = self.fetch_index_intraday_aggs(ticker=ticker, timespan="minute", mult=1, lookback_days=2)
            if df is None or df.empty:
                return None
            # Filter to today RTH (09:30-16:00 ET) but tolerate premarket if present
            import pytz
            from datetime import time as _time_cls
            et = pytz.timezone("America/New_York")
            df["et"] = df["dt"].dt.tz_convert(et)
            d0 = df["et"].dt.date.iloc[-1]
            start_t = _time_cls(9, 30)
            end_t = _time_cls(16, 0)
            df_today = df[(df["et"].dt.date == d0) & (df["et"].dt.time >= start_t) & (df["et"].dt.time <= end_t)]
            base = df_today if len(df_today) >= 50 else df
            return self.compute_indicators(base)
        except Exception:
            return None

    # ----- Reference helpers: earnings and economic events (best-effort) -----
    def _collect_paginated(self, base_url: str, params0: dict[str, Any], next_keys: tuple[str, ...] = ("next_url", "next")) -> list[dict[str, Any]]:
        url = base_url
        params = dict(params0)
        results: list[dict[str, Any]] = []
        pages = 0
        while url and pages < 50:
            pages += 1
            data = self._request(url, params if url == base_url else {"apiKey": self.api_key})
            if not data or not isinstance(data, dict):
                break
            items = data.get("results") or data.get("data")
            if isinstance(items, list):
                results.extend(x for x in items if isinstance(x, dict))
            nx = None
            for k in next_keys:
                v = data.get(k)
                if isinstance(v, str) and v:
                    nx = v
                    break
            if nx:
                url = nx
                params = {"apiKey": self.api_key}
            else:
                break
        return results

    def fetch_earnings_range(self, start_date: date, end_date: date, limit: int = 1000) -> Optional[list[dict[str, Any]]]:
        """Fetch earnings in a date range via Polygon reference endpoints if available.

        Returns a list of dicts; shape is API-dependent. Empty list if unavailable.
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        s = f"{start_date:%Y-%m-%d}"
        e = f"{end_date:%Y-%m-%d}"
        # Try vX then v3
        try:
            base_vx = f"{self.base}/vX/reference/earnings"
            params_vx = {"apiKey": self.api_key, "limit": limit, "report_date.gte": s, "report_date.lte": e}
            res = self._collect_paginated(base_vx, params_vx)
            if res:
                return res
        except Exception:
            pass
        try:
            base_v3 = f"{self.base}/v3/reference/earnings"
            params_v3 = {"apiKey": self.api_key, "limit": limit, "report_date.gte": s, "report_date.lte": e}
            res = self._collect_paginated(base_v3, params_v3)
            return res or []
        except Exception:
            return []

    def fetch_economic_events_range(self, start_date: date, end_date: date, limit: int = 1000) -> Optional[list[dict[str, Any]]]:
        """Fetch economic events in a date range via Polygon reference endpoint if available.

        Returns a list of dicts; empty list if unavailable.
        """
        if not self.api_key:
            self.log.warning("Polygon API key missing.")
            return None
        s = f"{start_date:%Y-%m-%d}"
        e = f"{end_date:%Y-%m-%d}"
        try:
            base_vx = f"{self.base}/vX/reference/economic"
            params_vx = {"apiKey": self.api_key, "limit": limit, "date.gte": s, "date.lte": e}
            res = self._collect_paginated(base_vx, params_vx)
            return res or []
        except Exception:
            return []
