from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _to_epoch_s(ts_utc: datetime) -> int:
    return int(ts_utc.replace(tzinfo=timezone.utc).timestamp())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class NewsStamp:
    ts_utc: datetime


class NewsService:
    """Redis-backed minimal news staleness stamps.

    Keys:
    Broadcast/gate stamps:
    - news:symbol:{SYMBOL}:last_ts (int epoch seconds)
    - news:symbol:{SYMBOL}:last_title (string, optional)
    - news:market:last_ts (int epoch seconds)
    - news:market:last_title (string, optional)

    Ingest/cache stamps (for freshness/ops UI):
    - news:symbol:{SYMBOL}:last_seen_ts (int epoch seconds)
    - news:symbol:{SYMBOL}:last_seen_title (string, optional)
    - news:market:last_seen_ts (int epoch seconds)
    - news:market:last_seen_title (string, optional)

    Optional history layer (for debug/UI):
    - news:tick:{SYMBOL} (ZSET score=epoch seconds, member=news_id) TTL 24h
    - news:item:{news_id} (string json) TTL 24h

    Optional dedup layer (for broadcasts):
    - news:dedup:{hash} (string "1") TTL 24h
    """

    def __init__(self, redis_client: Any):
        self._r = redis_client

    @property
    def r(self) -> Any:
        return self._r

    def mark_symbol_headline(self, symbol: str, *, ts_utc: datetime | None = None) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        ts = ts_utc or _now_utc()
        self._r.set(f"news:symbol:{sym}:last_ts", str(_to_epoch_s(ts)))

    def mark_symbol_headline_with_title(self, symbol: str, *, ts_utc: datetime, title: str | None = None) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        self._r.set(f"news:symbol:{sym}:last_ts", str(_to_epoch_s(ts_utc)))
        if title and str(title).strip():
            self._r.set(f"news:symbol:{sym}:last_title", str(title).strip())

    def mark_market_headline(self, *, ts_utc: datetime | None = None) -> None:
        ts = ts_utc or _now_utc()
        self._r.set("news:market:last_ts", str(_to_epoch_s(ts)))

    def mark_market_headline_with_title(self, *, ts_utc: datetime, title: str | None = None) -> None:
        self._r.set("news:market:last_ts", str(_to_epoch_s(ts_utc)))
        if title and str(title).strip():
            self._r.set("news:market:last_title", str(title).strip())

    def mark_symbol_seen_with_title(self, symbol: str, *, ts_utc: datetime, title: str | None = None) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        self._r.set(f"news:symbol:{sym}:last_seen_ts", str(_to_epoch_s(ts_utc)))
        if title and str(title).strip():
            self._r.set(f"news:symbol:{sym}:last_seen_title", str(title).strip())

    def mark_market_seen_with_title(self, *, ts_utc: datetime, title: str | None = None) -> None:
        self._r.set("news:market:last_seen_ts", str(_to_epoch_s(ts_utc)))
        if title and str(title).strip():
            self._r.set("news:market:last_seen_title", str(title).strip())

    def cache_symbol_items(self, *, symbol: str, items: list[dict[str, Any]], ttl_sec: int = 86400) -> int:
        """Cache normalized news items for a symbol.

        Each item should include: id, published_utc (ISO), headline/title, tickers, url.
        """

        sym = str(symbol or "").strip().upper()
        if not sym or not items:
            return 0

        zkey = f"news:tick:{sym}"
        stored = 0
        latest_dt: datetime | None = None
        latest_title: str | None = None

        for it in items:
            if not isinstance(it, dict):
                continue
            nid = str(it.get("id") or "").strip()
            pub = str(it.get("published_utc") or "").strip()
            headline = str(it.get("headline") or it.get("title") or "").strip()
            if not nid or not pub:
                continue
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(timezone.utc)
            except Exception:
                continue

            score = float(dt.timestamp())
            # De-dupe by member id.
            try:
                self._r.zadd(zkey, {nid: score})
                self._r.set(f"news:item:{nid}", __import__("json").dumps(it, separators=(",", ":"), ensure_ascii=False))
                if ttl_sec and int(ttl_sec) > 0:
                    self._r.expire(zkey, int(ttl_sec))
                    self._r.expire(f"news:item:{nid}", int(ttl_sec))
                stored += 1
            except Exception:
                continue

            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
                latest_title = headline or None

        # Update ingest/cache stamp (NOT the broadcast/gate stamp).
        if latest_dt is not None:
            self.mark_symbol_seen_with_title(sym, ts_utc=latest_dt, title=latest_title)
        return stored

    def cache_market_items(self, *, items: list[dict[str, Any]], ttl_sec: int = 86400) -> int:
        """Cache marketwide items under a synthetic symbol bucket."""
        # Keep the cache-seen stamp authoritative; history is optional.
        stored = self.cache_symbol_items(symbol="MARKET", items=items, ttl_sec=ttl_sec)

        # Compatibility: also maintain explicit market cache stamps.
        # Some consumers expect these keys (see class docstring).
        try:
            last_seen = self.get_symbol_last_seen_epoch_s("MARKET")
            if last_seen is not None:
                self._r.set("news:market:last_seen_ts", str(int(last_seen)))
                title = self._r.get("news:symbol:MARKET:last_seen_title")
                if title and str(title).strip():
                    self._r.set("news:market:last_seen_title", str(title).strip())
        except Exception:
            pass
        return stored

    def mark_dedup_seen(self, *, dedup_hash: str, ttl_sec: int = 86400) -> bool:
        """Return True if this hash was not seen recently (and mark it)."""

        h = str(dedup_hash or "").strip()
        if not h:
            return True
        key = f"news:dedup:{h}"
        try:
            existing = self._r.get(key)
        except Exception:
            existing = None
        if existing:
            return False
        try:
            self._r.set(key, "1")
            if ttl_sec and int(ttl_sec) > 0:
                self._r.expire(key, int(ttl_sec))
        except Exception:
            pass
        return True

    def get_symbol_last_epoch_s(self, symbol: str) -> int | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        try:
            s = self._r.get(f"news:symbol:{sym}:last_ts")
        except Exception:
            return None
        if not s:
            return None
        return _safe_int(s)

    def get_symbol_last_seen_epoch_s(self, symbol: str) -> int | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        try:
            s = self._r.get(f"news:symbol:{sym}:last_seen_ts")
        except Exception:
            return None
        if not s:
            return None
        return _safe_int(s)

    def get_market_last_epoch_s(self) -> int | None:
        try:
            s = self._r.get("news:market:last_ts")
        except Exception:
            return None
        if not s:
            return None
        return _safe_int(s)

    def get_market_last_seen_epoch_s(self) -> int | None:
        try:
            s = self._r.get("news:market:last_seen_ts")
        except Exception:
            return None
        if not s:
            return None
        return _safe_int(s)

    def get_recent_items(self, *, symbol: str, limit: int = 3) -> list[dict[str, Any]]:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return []
        zkey = f"news:tick:{sym}"
        n = max(1, min(10, int(limit)))
        ids: list[str] = []
        try:
            raw = self._r.zrevrange(zkey, 0, n - 1)
            if isinstance(raw, list):
                ids = [x.decode("utf-8", errors="replace") if isinstance(x, (bytes, bytearray)) else str(x) for x in raw]
        except Exception:
            # Fallback: attempt zrange and reverse.
            try:
                raw = self._r.zrange(zkey, 0, -1)
                if isinstance(raw, list):
                    ids = [x.decode("utf-8", errors="replace") if isinstance(x, (bytes, bytearray)) else str(x) for x in raw]
                    ids = list(reversed(ids))[:n]
            except Exception:
                ids = []

        out: list[dict[str, Any]] = []
        for nid in ids:
            try:
                blob = self._r.get(f"news:item:{nid}")
            except Exception:
                blob = None
            if not blob:
                continue
            if isinstance(blob, (bytes, bytearray)):
                blob = blob.decode("utf-8", errors="replace")
            try:
                obj = __import__("json").loads(str(blob))
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out

    # --- Canonical context snapshot helpers (ctx:*) ---------------------------------

    def get_symbol_context_snapshot(self, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        try:
            raw = self._r.get(f"ctx:sym:{sym}")
        except Exception:
            return None
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            obj = __import__("json").loads(str(raw))
            if not isinstance(obj, dict):
                return None
            if int(obj.get("ctx_version") or 0) != 1:
                return None
            return obj
        except Exception:
            return None

    def get_market_context_snapshot(self) -> dict[str, Any] | None:
        try:
            raw = self._r.get("ctx:market")
        except Exception:
            return None
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            obj = __import__("json").loads(str(raw))
            if not isinstance(obj, dict):
                return None
            if int(obj.get("ctx_version") or 0) != 1:
                return None
            return obj
        except Exception:
            return None
