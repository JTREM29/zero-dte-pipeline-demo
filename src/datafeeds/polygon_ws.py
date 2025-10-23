from __future__ import annotations
import os, json, threading, time, traceback, queue
from typing import Dict, Any, Iterable, Optional, List, Tuple
from collections import defaultdict
from urllib import request as _urlrequest
from urllib import parse as _urlparse
from datetime import datetime, timedelta, timezone

try:
    import websocket  # type: ignore  # pip install websocket-client
except Exception as e:
    raise RuntimeError("Install dependency: pip install websocket-client") from e

POLY_KEY = os.getenv("POLYGON_API_KEY", "")
WS_STOCKS  = "wss://socket.polygon.io/stocks"
WS_OPTIONS = "wss://socket.polygon.io/options"
WS_INDICES = "wss://socket.polygon.io/indices"
REST_BASE  = "https://api.polygon.io"

# --------- Simple thread-safe cache ---------
class QuoteCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._q: Dict[str, Dict[str, Any]] = {}  # symbol -> {bid,ask,bs,asz,ts}

    def update(self, sym: str, bid: float, ask: float, bs: int, asz: int, ts: int):
        with self._lock:
            self._q[sym] = {"bid": bid, "ask": ask, "bs": bs, "asz": asz, "ts": ts}

    def get(self, sym: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._q.get(sym)

    def mids(self, *symbols: str) -> Dict[str, float]:
        out = {}
        with self._lock:
            for s in symbols:
                q = self._q.get(s)
                if q and q["bid"] > 0 and q["ask"] > 0 and q["ask"] >= q["bid"]:
                    out[s] = 0.5 * (q["bid"] + q["ask"])
        return out

# --------- Simple thread-safe index snapshot cache ---------
class IndexCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._d: Dict[str, Dict[str, Any]] = {}  # symbol -> {value, ts}

    def update(self, sym: str, value: float, ts: int):
        with self._lock:
            self._d[sym] = {"value": value, "ts": ts}

    def get(self, sym: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._d.get(sym)

# --------- Base WS runner with backoff & resubscribe ---------
class _WSRunner(threading.Thread):
    def __init__(self, url: str, key: str, subs: Iterable[str], cache: QuoteCache, name: str):
        super().__init__(daemon=True, name=name)
        self.url, self.key, self.cache = url, key, cache
        self._subs = list(subs)
        self._stop = threading.Event()
        self._status = "INIT"
        self._last_msg_ts = 0
        self._outbound = queue.Queue()  # additional subscriptions at runtime

    def stop(self):
        self._stop.set()

    def alive(self) -> bool:
        return self._status == "RUN" and (time.time() - self._last_msg_ts) < 10

    def add_subscriptions(self, topics: Iterable[str]):
        for t in topics:
            self._outbound.put(str(t))

    # --- parse a single message into cache (override if you want more) ---
    def _handle(self, obj: Dict[str, Any]):
        """
        Polygon v3 messages:
          Q.: NBBO quotes
          For stocks: ev='Q', sym='SPY', bp, ap, bs, as, t
          For options: ev='Q', sym='O:SPYYYYY...'
          For indices: ev='Q', sym='I:SPX' (NBBO-like snapshot)
        """
        ev = obj.get("ev") or obj.get("event_type")
        if ev != "Q":  # only care about NBBO quotes
            return
        sym = obj.get("sym") or obj.get("symbol")
        bp  = float(obj.get("bp", 0) or 0)
        ap  = float(obj.get("ap", 0) or 0)
        bs  = int(obj.get("bs", 0) or 0)
        aS  = int(obj.get("as", 0) or 0)
        ts  = int(obj.get("t", 0) or 0)
        if sym:
            self.cache.update(sym, bp, ap, bs, aS, ts)

class _WSRunnerIndices(_WSRunner):
    """Index channel runner that parses index value snapshots.

    Polygon indices messages typically come with ev='I' and include a value.
    We'll try common keys for the value and timestamp to be robust across minor variations.
    """

    def __init__(self, url: str, key: str, subs: Iterable[str], cache: IndexCache, name: str):
        # type: ignore[arg-type] - parent expects QuoteCache but we pass IndexCache to reuse connection logic
        super().__init__(url, key, subs, cache, name)  # type: ignore[arg-type]  # reuse connection logic
        self._index_cache = cache

    def _handle(self, obj: Dict[str, Any]):
        ev = obj.get("ev") or obj.get("event_type")
        if ev not in ("I", "index", "IX", "i"):
            return
        sym = obj.get("sym") or obj.get("symbol")
        # Try multiple potential fields for the index value
        val = None
        for k in ("v", "value", "p", "price", "iv", "indexValue"):
            if k in obj and obj[k] is not None:
                try:
                    val = float(obj[k])
                    break
                except Exception:
                    pass
        ts = 0
        for tk in ("t", "timestamp", "ts"):
            if tk in obj and obj[tk] is not None:
                try:
                    ts = int(obj[tk])
                    break
                except Exception:
                    pass
        if sym and val is not None:
            self._index_cache.update(sym, val, ts)

    def run(self):
        if not self.key:
            raise RuntimeError("POLYGON_API_KEY not set")
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                self._status = "CONNECTING"
                ws = websocket.create_connection(self.url, timeout=8)
                # auth
                ws.send(json.dumps({"action": "auth", "params": self.key}))
                # initial subscribe
                if self._subs:
                    ws.send(json.dumps({"action": "subscribe", "params": ",".join(self._subs)}))
                self._status = "RUN"
                self._last_msg_ts = time.time()
                backoff = 1.0  # reset on success

                # ping loop + outbound subs
                last_ping = 0.0
                while not self._stop.is_set():
                    now = time.time()
                    # send any queued subs
                    try:
                        while True:
                            topic = self._outbound.get_nowait()
                            ws.send(json.dumps({"action": "subscribe", "params": topic}))
                        # falls through only if queue emptied
                    except queue.Empty:
                        pass

                    # heartbeat ping every 15s
                    if now - last_ping > 15:
                        try:
                            ws.send(json.dumps({"action": "ping"}))
                        except Exception:
                            break
                        last_ping = now

                    # read message (non-blocking-ish)
                    ws.settimeout(5)
                    raw = ws.recv()
                    if not raw:
                        continue
                    self._last_msg_ts = time.time()
                    # Ensure text; websocket-client may yield bytes
                    if isinstance(raw, (bytes, bytearray, memoryview)):
                        try:
                            raw = bytes(raw).decode("utf-8", "ignore")
                        except Exception:
                            continue
                    # Polygon batches frames as JSON array sometimes
                    if raw.startswith("["):
                        arr = json.loads(raw)
                        for obj in arr:
                            self._handle(obj)
                    else:
                        obj = json.loads(raw)
                        if isinstance(obj, list):
                            for o in obj: self._handle(o)
                        else:
                            self._handle(obj)

                # graceful stop: break outer while
                if self._stop.is_set():
                    break

            except Exception:
                traceback.print_exc()
            finally:
                self._status = "DOWN"
                try:
                    if ws: ws.close()
                except Exception:
                    pass
                # backoff before reconnect
                time.sleep(backoff)
                backoff = min(30.0, backoff * 1.7)

# --------- Public facade you will use ---------
class PolygonWS:
    """
    Three lightweight WS connections: stocks, indices, options.
    You can start any subset; all feed a shared QuoteCache.
    """
    def __init__(self, key: str | None = None):
        key = key or POLY_KEY
        if not key:
            raise RuntimeError("POLYGON_API_KEY not set")
        self.cache = QuoteCache()
        self.index_cache = IndexCache()
        self._stocks = None
        self._indices = None
        self._options = None
        self._key = key

    @staticmethod
    def _topics_quotes(prefix: str, tickers: Iterable[str]) -> List[str]:
        # Polygon v3: subscribe strings are like "Q.SPY", "Q.O:SPY...", "Q.I:SPX"
        return [f"Q.{t}" for t in tickers]

    def start_stocks(self, tickers: Iterable[str]):
        subs = self._topics_quotes("Q", tickers)
        self._stocks = _WSRunner(WS_STOCKS, self._key, subs, self.cache, name="poly-stocks")
        self._stocks.start()

    def start_indices(self, tickers: Iterable[str]):
        # Indices channel uses "I." prefix for subscriptions
        subs = [f"I.{t}" for t in tickers]
        self._indices = _WSRunnerIndices(WS_INDICES, self._key, subs, self.index_cache, name="poly-indices")
        self._indices.start()

    def start_options(self, occ_symbols: Iterable[str]):
        subs = self._topics_quotes("Q", occ_symbols)
        self._options = _WSRunner(WS_OPTIONS, self._key, subs, self.cache, name="poly-options")
        self._options.start()

    def subscribe_options(self, occ_symbols: Iterable[str]):
        if self._options:
            self._options.add_subscriptions(self._topics_quotes("Q", occ_symbols))

    # ---------- Options chain helpers (REST + batched WS subscribe) ----------
    def _rest_get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Minimal REST GET using urllib with apiKey query parameter and JSON parse.

        Raises on HTTP errors; returns parsed JSON dict.
        """
        if not self._key:
            raise RuntimeError("POLYGON_API_KEY not set")
        # Include apiKey as query param to avoid header auth complexity
        p = dict(params or {})
        p["apiKey"] = self._key
        qs = _urlparse.urlencode({k: v for k, v in p.items() if v is not None})
        url = f"{REST_BASE}{path}?{qs}"
        req = _urlrequest.Request(url)
        with _urlrequest.urlopen(req, timeout=15) as resp:
            data = resp.read()
        try:
            return json.loads(data.decode("utf-8", "ignore"))
        except Exception as e:
            raise RuntimeError(f"Failed decoding JSON from {url}") from e

    def _rest_get_abs(self, url: str) -> Dict[str, Any]:
        """GET an absolute URL (used for Polygon next_url pagination)."""
        req = _urlrequest.Request(url)
        with _urlrequest.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8", "ignore"))

    def _get_underlying_price(self, ticker: str) -> Optional[float]:
        """Try to fetch an approximate underlying price (last trade, then prev close)."""
        try:
            data = self._rest_get(f"/v2/last/trade/{ticker}", {})
            r = data.get("results") or {}
            p = r.get("p") or r.get("price")
            if p is not None:
                return float(p)
        except Exception:
            pass
        try:
            data = self._rest_get(f"/v2/aggs/ticker/{ticker}/prev", {})
            res = data.get("results") or []
            if res:
                c = res[0].get("c")
                if c is not None:
                    return float(c)
        except Exception:
            pass
        return None

    def _list_option_contracts_single(self, underlying: str, *, days_ahead: int = 7,
                                      contract_types: Optional[Iterable[str]] = None,
                                      active_only: bool = True,
                                      limit_per_page: int = 1000,
                                      max_pages: int = 10) -> List[str]:
        """Fetch OCC symbols for one underlying via /v3/reference/options/contracts.

        Returns a list like ["O:SPY241018C00450000", ...]. Applies simple pagination and
        expiration window [today, today+days_ahead]. When contract_types is provided, it's
        an iterable of {"call", "put"}; otherwise both are returned.
        """
        symbols: List[str] = []
        # Build base params
        now = datetime.now(timezone.utc).date()
        gte = now.isoformat()
        lte = (now + timedelta(days=max(0, int(days_ahead)))).isoformat()

        def fetch_page(next_url: Optional[str], extra: Dict[str, Any]) -> Tuple[List[str], Optional[str]]:
            if next_url:
                # next_url already includes apiKey; honor as-is
                data = self._rest_get_abs(next_url)
            else:
                params = {
                    "underlying_ticker": underlying,
                    "expired": "false" if active_only else "true",
                    "limit": limit_per_page,
                    "sort": "expiration_date",
                    "order": "asc",
                    "expiration_date.gte": gte,
                    "expiration_date.lte": lte,
                }
                params.update(extra)
                data = self._rest_get("/v3/reference/options/contracts", params)
            res = data.get("results") or []
            out = []
            for it in res:
                t = it.get("ticker") or it.get("symbol")
                if isinstance(t, str) and t.startswith("O:"):
                    out.append(t)
            return out, data.get("next_url")

        # Iterate per contract_type to improve coverage and reduce server-side filtering edge cases
        types = list(contract_types) if contract_types else [None]
        page_guard = 0
        for ctype in types:
            extra = {}
            if ctype:
                extra["contract_type"] = ctype
            next_url: Optional[str] = None
            while page_guard < max_pages:
                page_guard += 1
                try:
                    page_syms, next_url = fetch_page(next_url, extra)
                except Exception:
                    break
                if not page_syms:
                    break
                symbols.extend(page_syms)
                if not next_url:
                    break
                # be gentle with API
                time.sleep(0.15)
        # Deduplicate while preserving order
        seen = set()
        uniq: List[str] = []
        for s in symbols:
            if s not in seen:
                uniq.append(s)
                seen.add(s)
        return uniq

    def _list_option_contracts_single_meta(self, underlying: str, *, days_ahead: int = 7,
                                            contract_types: Optional[Iterable[str]] = None,
                                            active_only: bool = True,
                                            limit_per_page: int = 1000,
                                            max_pages: int = 15) -> List[Dict[str, Any]]:
        """Fetch option contract metadata for one underlying (keeps strike/expiry/type fields)."""
        rows: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc).date()
        gte = now.isoformat()
        lte = (now + timedelta(days=max(0, int(days_ahead)))).isoformat()

        def fetch_page(next_url: Optional[str], extra: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
            if next_url:
                data = self._rest_get_abs(next_url)
            else:
                params = {
                    "underlying_ticker": underlying,
                    "expired": "false" if active_only else "true",
                    "limit": limit_per_page,
                    "sort": "expiration_date",
                    "order": "asc",
                    "expiration_date.gte": gte,
                    "expiration_date.lte": lte,
                }
                params.update(extra)
                data = self._rest_get("/v3/reference/options/contracts", params)
            res = data.get("results") or []
            out: List[Dict[str, Any]] = []
            for it in res:
                t = it.get("ticker") or it.get("symbol")
                if not (isinstance(t, str) and t.startswith("O:")):
                    continue
                # keep minimal fields we need
                out.append({
                    "ticker": t,
                    "expiration_date": it.get("expiration_date"),
                    "strike_price": it.get("strike_price"),
                    "contract_type": it.get("contract_type"),
                })
            return out, data.get("next_url")

        types = list(contract_types) if contract_types else [None]
        page_guard = 0
        for ctype in types:
            extra = {}
            if ctype:
                extra["contract_type"] = ctype
            next_url: Optional[str] = None
            while page_guard < max_pages:
                page_guard += 1
                try:
                    page_rows, next_url = fetch_page(next_url, extra)
                except Exception:
                    break
                if not page_rows:
                    break
                rows.extend(page_rows)
                if not next_url:
                    break
                time.sleep(0.12)
        return rows

    def list_option_symbols_atm_slice(self, underlyings: Iterable[str], *, days_ahead: int = 2,
                                      max_expirations: int = 1,
                                      per_type_strikes: int = 8,
                                      contract_types: Optional[Iterable[str]] = ("call", "put"),
                                      active_only: bool = True,
                                      max_total: int = 4000) -> List[str]:
        """Return a lighter slice of options: nearest-ATM strikes for earliest expirations.

        For each underlying, we select up to `max_expirations` within the window and for each
        expiration pick `per_type_strikes` nearest to ATM per contract type.
        """
        out: List[str] = []
        for u in underlyings:
            try:
                price = self._get_underlying_price(u)
            except Exception:
                price = None
            try:
                rows = self._list_option_contracts_single_meta(
                    u,
                    days_ahead=days_ahead,
                    contract_types=contract_types,
                    active_only=active_only,
                )
            except Exception:
                rows = []
            if not rows:
                continue
            # group by expiration
            by_exp: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                exp = r.get("expiration_date") or ""
                by_exp.setdefault(exp, []).append(r)
            # choose earliest expirations
            exps_sorted = sorted(e for e in by_exp.keys() if e)
            for exp in exps_sorted[: max(1, int(max_expirations))]:
                bucket = by_exp.get(exp, [])
                # split by type
                for ctype in (contract_types or ("call", "put")):
                    # collect rows for this type
                    sub = [r for r in bucket if r.get("contract_type") == ctype]
                    if not sub:
                        continue
                    # score by distance to ATM
                    def _dist(row: Dict[str, Any]) -> float:
                        sp = row.get("strike_price")
                        try:
                            spf = float(sp) if sp is not None else float("nan")
                        except Exception:
                            spf = float("nan")
                        if price is None or not (spf == spf):  # NaN check
                            return float("inf")
                        return abs(spf - float(price))
                    sub.sort(key=_dist)
                    # fallback if price unknown: pick mid-range by strike
                    if price is None:
                        try:
                            sub.sort(key=lambda r: float(r.get("strike_price") or 0.0))
                            mid = len(sub) // 2
                            half = max(1, int(per_type_strikes // 2))
                            sub = sub[max(0, mid - half): mid + half]
                        except Exception:
                            sub = sub[: per_type_strikes]
                    picks = sub[: per_type_strikes]
                    for row in picks:
                        t = row.get("ticker")
                        if isinstance(t, str):
                            out.append(t)
                if len(out) >= max_total:
                    break
            if len(out) >= max_total:
                break
        # dedupe/cap
        seen = set()
        uniq: List[str] = []
        for s in out:
            if s not in seen:
                uniq.append(s)
                seen.add(s)
            if len(uniq) >= max_total:
                break
        return uniq

    def start_options_atm_slice(self, underlyings: Iterable[str], *, days_ahead: int = 2,
                                 max_expirations: int = 1,
                                 per_type_strikes: int = 8,
                                 contract_types: Optional[Iterable[str]] = ("call", "put"),
                                 batch_size: int = 400,
                                 sleep_between: float = 0.15,
                                 active_only: bool = True,
                                 max_total: int = 4000) -> int:
        """Start options WS (if needed) and subscribe to an ATM slice of contracts."""
        if not self._options:
            self.start_options([])
            time.sleep(0.25)
        occ_symbols = self.list_option_symbols_atm_slice(
            underlyings,
            days_ahead=days_ahead,
            max_expirations=max_expirations,
            per_type_strikes=per_type_strikes,
            contract_types=contract_types,
            active_only=active_only,
            max_total=max_total,
        )
        n = len(occ_symbols)
        if n == 0:
            return 0
        bs = max(50, int(batch_size))
        for i in range(0, n, bs):
            chunk = occ_symbols[i:i+bs]
            try:
                self.subscribe_options(chunk)
            except Exception:
                traceback.print_exc()
            time.sleep(max(0.05, float(sleep_between)))
        return n

    def list_option_symbols(self, underlyings: Iterable[str], *, days_ahead: int = 7,
                             contract_types: Optional[Iterable[str]] = ("call", "put"),
                             active_only: bool = True,
                             limit_per_page: int = 1000,
                             max_pages: int = 10,
                             max_total: int = 8000) -> List[str]:
        """Collect OCC option symbols across underlyings using Polygon REST.

        Parameters allow scoping by expiration window and type. Results are capped by
        max_total for safety.
        """
        out: List[str] = []
        for u in underlyings:
            try:
                syms = self._list_option_contracts_single(
                    u,
                    days_ahead=days_ahead,
                    contract_types=contract_types,
                    active_only=active_only,
                    limit_per_page=limit_per_page,
                    max_pages=max_pages,
                )
                out.extend(syms)
            except Exception:
                # continue on per-underlying failure
                traceback.print_exc()
            if len(out) >= max_total:
                break
        # Cap and dedupe again
        seen = set()
        uniq: List[str] = []
        for s in out:
            if s not in seen:
                uniq.append(s)
                seen.add(s)
            if len(uniq) >= max_total:
                break
        return uniq

    def start_options_all(self, underlyings: Iterable[str], *, days_ahead: int = 7,
                           contract_types: Optional[Iterable[str]] = ("call", "put"),
                           batch_size: int = 400,
                           sleep_between: float = 0.2,
                           active_only: bool = True,
                           max_total: int = 8000) -> int:
        """Start the options WS (if not running) and subscribe to ALL contracts for underlyings.

        Returns the number of contracts requested for subscription. Subscriptions are sent in
        batches to avoid exceeding message size and rate limits.
        """
        # Ensure the options runner is alive
        if not self._options:
            self.start_options([])
            # small delay to ensure connection authenticated before sending many subs
            time.sleep(0.25)

        occ_symbols = self.list_option_symbols(
            underlyings,
            days_ahead=days_ahead,
            contract_types=contract_types,
            active_only=active_only,
            max_total=max_total,
        )
        n = len(occ_symbols)
        if n == 0:
            return 0
        # Batch subscribe
        bs = max(50, int(batch_size))
        for i in range(0, n, bs):
            chunk = occ_symbols[i:i+bs]
            try:
                self.subscribe_options(chunk)
            except Exception:
                traceback.print_exc()
            time.sleep(max(0.05, float(sleep_between)))
        return n

    def stop_all(self):
        for r in (self._stocks, self._indices, self._options):
            try:
                if r: r.stop()
            except Exception:
                pass

    # Convenience getters
    def nbbo(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.cache.get(symbol)

    def mid(self, symbol: str) -> float:
        q = self.cache.get(symbol) or {}
        bid, ask = q.get("bid", 0.0), q.get("ask", 0.0)
        if bid > 0 and ask > 0 and ask >= bid:
            return 0.5 * (bid + ask)
        return 0.0

    def alive(self) -> Dict[str, bool]:
        return {
            "stocks":  bool(self._stocks and self._stocks.alive()),
            "indices": bool(self._indices and self._indices.alive()),
            "options": bool(self._options and self._options.alive()),
        }

    # --- Convenience: wait for a valid NBBO to arrive (bid>0, ask>=bid) ---
    def wait_nbbo(self, symbol: str, *, timeout: float = 2.0, interval: float = 0.05) -> Optional[Dict[str, Any]]:
        """Poll cache for up to `timeout` seconds for a valid NBBO.

        Returns the quote dict or None if not received in time.
        """
        end = time.time() + max(0.0, float(timeout))
        iv = max(0.01, float(interval))
        while time.time() < end:
            q = self.nbbo(symbol)
            try:
                if q:
                    b = float(q.get("bid", 0.0) or 0.0)
                    a = float(q.get("ask", 0.0) or 0.0)
                    if b > 0.0 and a > 0.0 and a >= b:
                        return q
            except Exception:
                pass
            time.sleep(iv)
        return None

    # --- Wait for an index value snapshot to arrive ---
    def index_value(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.index_cache.get(symbol)

    def wait_index_value(self, symbol: str, *, timeout: float = 5.0, interval: float = 0.05) -> Optional[Dict[str, Any]]:
        end = time.time() + max(0.0, float(timeout))
        iv = max(0.01, float(interval))
        while time.time() < end:
            snap = self.index_cache.get(symbol)
            if snap and isinstance(snap.get("value"), (int, float)):
                return snap
            time.sleep(iv)
        return None

    def subscribe_and_wait_options(self, occ_symbols: Iterable[str], *, timeout: float = 2.0, interval: float = 0.05) -> Dict[str, Optional[Dict[str, Any]]]:
        """Subscribe to option OCC symbols and wait briefly for first valid quotes.

        Returns mapping symbol -> quote or None if not received.
        """
        self.start_options(occ_symbols)
        out: Dict[str, Optional[Dict[str, Any]]] = {}
        for occ in occ_symbols:
            try:
                out[occ] = self.wait_nbbo(occ, timeout=timeout, interval=interval)
            except Exception:
                out[occ] = None
        return out
