from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .calendar_keys import cal_earnings_key, cal_earnings_last_refresh_key, cal_earnings_refreshed_key
from .earnings_enricher import enrich_earnings_blob
from .earnings_schema import normalize_earnings_blob


def _to_epoch_s(ts_utc: datetime) -> float:
    return float(ts_utc.replace(tzinfo=timezone.utc).timestamp())


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        raw = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _infer_session_from_ts_utc(ts_utc: datetime) -> str:
    """Best-effort earnings session inference from event timestamp.

    - <= 11:00 ET => BMO
    - >= 16:00 ET => AMC
    - else => DURING
    """

    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
    except Exception:
        et = timezone.utc

    ts_et = ts_utc.astimezone(et)
    hhmm = ts_et.hour * 60 + ts_et.minute
    if hhmm <= (11 * 60):
        return "BMO"
    if hhmm >= (16 * 60):
        return "AMC"
    return "DURING"


@dataclass(frozen=True)
class MacroEvent:
    type: str
    ts_utc: datetime

    def to_payload(self) -> dict[str, Any]:
        return {"type": self.type, "ts_utc": self.ts_utc.isoformat()}


class CalendarService:
    """Redis-backed calendar primitives.

    Keys:
    - cal:macro:upcoming (ZSET) score=epoch_s member=json payload
    - cal:earnings:{SYMBOL} (string json) {ts_utc, confirmed}
    """

    def __init__(self, redis_client: Any):
        self._r = redis_client

    @property
    def r(self) -> Any:
        return self._r

    def set_macro_upcoming(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        mapping: dict[str, float] = {}
        for ev in events:
            if not isinstance(ev, dict):
                continue
            typ = str(ev.get("type") or "").strip().upper()
            ts = _parse_iso(str(ev.get("ts_utc") or ""))
            if not typ or ts is None:
                continue
            payload = {"type": typ, "ts_utc": ts.isoformat()}
            mapping[json.dumps(payload, separators=(",", ":"), ensure_ascii=False)] = _to_epoch_s(ts)

        if mapping:
            # zadd(name, mapping={member: score})
            self._r.zadd("cal:macro:upcoming", mapping)

    def add_macro_event(self, *, event_type: str, ts_utc: datetime) -> None:
        payload = {"type": str(event_type or "").strip().upper(), "ts_utc": ts_utc.isoformat()}
        if not payload["type"]:
            return
        self._r.zadd("cal:macro:upcoming", {json.dumps(payload, separators=(",", ":"), ensure_ascii=False): _to_epoch_s(ts_utc)})

    def macro_events_between(self, *, start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]]:
        try:
            raw = self._r.zrangebyscore("cal:macro:upcoming", _to_epoch_s(start_utc), _to_epoch_s(end_utc))
        except Exception:
            raw = []
        out: list[dict[str, Any]] = []
        for s in raw or []:
            try:
                if isinstance(s, (bytes, bytearray)):
                    s = s.decode("utf-8", errors="replace")
                obj = json.loads(str(s))
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out

    def set_earnings(
        self,
        *,
        symbol: str,
        ts_utc: datetime,
        confirmed: bool = True,
        source: str | None = None,
        ttl_sec: int = 14 * 24 * 3600,
        session: str | None = None,
        blackout: dict[str, int] | None = None,
    ) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        payload: dict[str, Any] = {
            "ts_utc": ts_utc.isoformat(),
            "confirmed": bool(confirmed),
            "source": str(source).strip() if source and str(source).strip() else "cache",
            "session": str(session).strip().upper() if session and str(session).strip() else _infer_session_from_ts_utc(ts_utc),
            "blackout": blackout if isinstance(blackout, dict) else {"pre_min": 45, "post_min": 30},
            "refreshed_utc": datetime.now(timezone.utc).isoformat(),
        }

        # Preserve enriched fields (expected move, reactions, iv_state, etc.) from the
        # existing canonical blob so periodic refreshes don't wipe premium UX fields.
        try:
            existing = self.get_earnings(symbol=sym) or {}
        except Exception:
            existing = {}
        if isinstance(existing, dict) and existing:
            for k in (
                "expected_move_pct",
                "expected_move",
                "expected_move_refreshed_utc",
                "expected_move_source",
                "liquidity_risk",
                "iv_state",
                "history",
                "_reactions_cache",
            ):
                if k in existing and payload.get(k) is None:
                    payload[k] = existing.get(k)

        # Canonical blob normalization + best-effort enrichment.
        blob = normalize_earnings_blob(sym, payload)
        blob = enrich_earnings_blob(blob)

        key = cal_earnings_key(sym)
        self._r.set(key, json.dumps(blob, separators=(",", ":"), ensure_ascii=False))

        # Freshness keys (epoch seconds). These are used for premium UX + fleet-wide staleness checks.
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        try:
            self._r.set(cal_earnings_refreshed_key(sym), str(now_epoch))
            self._r.set(cal_earnings_last_refresh_key(), str(now_epoch))
        except Exception:
            pass
        if ttl_sec and int(ttl_sec) > 0:
            try:
                self._r.expire(key, int(ttl_sec))
            except Exception:
                pass

    def cache_earnings_history(
        self,
        *,
        symbol: str,
        items: list[dict[str, Any]],
        ttl_sec: int = 90 * 24 * 3600,
    ) -> int:
        """Optional history layer: earn:tick:{SYM} zset + earn:item:{id} json."""
        sym = str(symbol or "").strip().upper()
        if not sym or not items:
            return 0
        zkey = f"earn:tick:{sym}"
        stored = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            eid = str(it.get("id") or "").strip()
            ts = _parse_iso(str(it.get("ts_utc") or ""))
            if not eid or ts is None:
                continue
            score = _to_epoch_s(ts)
            try:
                self._r.zadd(zkey, {eid: score})
                self._r.set(f"earn:item:{eid}", json.dumps(it, separators=(",", ":"), ensure_ascii=False))
                if ttl_sec and int(ttl_sec) > 0:
                    self._r.expire(zkey, int(ttl_sec))
                    self._r.expire(f"earn:item:{eid}", int(ttl_sec))
                stored += 1
            except Exception:
                continue
        return stored

    def get_earnings_history(self, *, symbol: str, limit: int = 4) -> list[dict[str, Any]]:
        """Load recent earnings history items for a symbol.

        Storage layout:
        - earn:tick:{SYM} (ZSET) member=eid score=epoch_s
        - earn:item:{eid} (string json)

        Returns newest-first, best-effort, never raises.
        """

        sym = str(symbol or "").strip().upper()
        if not sym:
            return []
        try:
            lim = int(limit)
        except Exception:
            lim = 4
        lim = max(0, min(50, lim))
        if lim <= 0:
            return []

        zkey = f"earn:tick:{sym}"
        try:
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            ids = self._r.zrevrangebyscore(zkey, now_epoch, "-inf", start=0, num=lim)
        except Exception:
            ids = []

        out: list[dict[str, Any]] = []
        for eid in ids or []:
            try:
                if isinstance(eid, (bytes, bytearray)):
                    eid_s = eid.decode("utf-8", errors="replace")
                else:
                    eid_s = str(eid)
                eid_s = str(eid_s or "").strip()
                if not eid_s:
                    continue
                # When decode_responses=False, pass bytes keys.
                raw = self._r.get(f"earn:item:{eid_s}".encode("utf-8"))
            except Exception:
                continue
            if not raw:
                continue
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                obj = json.loads(str(raw))
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue

        return out

    def get_earnings(self, *, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        try:
            s = self._r.get(cal_earnings_key(sym))
        except Exception:
            return None
        if not s:
            return None
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="replace")
        try:
            obj = json.loads(str(s))
            if not isinstance(obj, dict):
                return None
            # Normalize legacy blobs on read (do not write-back here).
            return normalize_earnings_blob(sym, obj)
        except Exception:
            return None

    def get_cached_earnings(self, sym: str) -> dict[str, Any] | None:
        """Alias for reading the cached earnings blob.

        Kept as a simple wrapper so gate code can use a non-keyword signature.
        """

        return self.get_earnings(symbol=sym)

    def get_symbol_context_snapshot(self, sym: str) -> dict[str, Any] | None:
        """Read canonical ctx snapshot for a symbol (ctx:sym:{SYM}).

        Consumers may use this to stay aligned with previews/gates without re-reading
        raw cache keys.
        """

        symbol = str(sym or "").strip().upper()
        if not symbol:
            return None
        try:
            raw = self._r.get(f"ctx:sym:{symbol}")
        except Exception:
            return None
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            obj = json.loads(str(raw))
            if not isinstance(obj, dict):
                return None
            if int(obj.get("ctx_version") or 0) != 1:
                return None
            return obj
        except Exception:
            return None

    def set_earnings_blob(
        self,
        *,
        symbol: str,
        blob: dict[str, Any],
        ttl_sec: int = 14 * 24 * 3600,
    ) -> None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return
        normalized = normalize_earnings_blob(sym, blob)
        normalized = enrich_earnings_blob(normalized)
        key = cal_earnings_key(sym)
        self._r.set(key, json.dumps(normalized, separators=(",", ":"), ensure_ascii=False))

        now_epoch = int(datetime.now(timezone.utc).timestamp())
        try:
            self._r.set(cal_earnings_refreshed_key(sym), str(now_epoch))
            self._r.set(cal_earnings_last_refresh_key(), str(now_epoch))
        except Exception:
            pass

        if ttl_sec and int(ttl_sec) > 0:
            try:
                self._r.expire(key, int(ttl_sec))
            except Exception:
                pass

    def get_earnings_refreshed_utc(self, *, symbol: str) -> int | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        try:
            v = self._r.get(cal_earnings_refreshed_key(sym))
        except Exception:
            return None
        if not v:
            return None
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", errors="replace")
        try:
            return int(str(v).strip())
        except Exception:
            return None

    def get_earnings_last_refresh_utc(self) -> int | None:
        try:
            v = self._r.get(cal_earnings_last_refresh_key())
        except Exception:
            return None
        if not v:
            return None
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", errors="replace")
        try:
            return int(str(v).strip())
        except Exception:
            return None
