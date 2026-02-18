from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class AlertStore:
    """Minimal Redis-backed store for AlertIntent v1 envelopes.

    This is intentionally standalone and not wired into the default bot flow yet.
    """

    def __init__(self, redis_client):
        self.r = redis_client

    def next_id(self) -> str:
        n = int(self.r.incr("alerts:next_id"))
        return f"A{n:08d}"

    def create_alert(self, alert_id: str, intent_json: dict[str, Any]) -> None:
        tf = ((intent_json.get("condition") or {}).get("timeframe") or "").strip()
        src = intent_json.get("source") or {}
        user_id = str(src.get("user_id") or "")
        channel_id = str(src.get("channel_id") or "")

        self.r.set(f"alert:{alert_id}:intent", json.dumps(intent_json))
        self.r.hset(
            f"alert:{alert_id}:meta",
            mapping={
                "status": "active",
                "timeframe": tf,
                "owner_user_id": user_id,
                "channel_id": channel_id,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )

        pipe = self.r.pipeline()
        pipe.sadd("alerts:active", alert_id)
        if tf:
            pipe.sadd(f"alerts:tf:{tf}", alert_id)
        if user_id:
            pipe.sadd(f"alerts:user:{user_id}", alert_id)
        if channel_id:
            pipe.sadd(f"alerts:chan:{channel_id}", alert_id)
        pipe.execute()

    def get_intent(self, alert_id: str) -> dict[str, Any] | None:
        s = self.r.get(f"alert:{alert_id}:intent")
        if not s:
            return None
        if isinstance(s, bytes):
            s = s.decode("utf-8", errors="replace")
        return json.loads(s)

    def get_meta(self, alert_id: str) -> dict[str, str]:
        raw = self.r.hgetall(f"alert:{alert_id}:meta")
        out: dict[str, str] = {}
        for k, v in (raw or {}).items():
            kk = k.decode() if isinstance(k, (bytes, bytearray)) else str(k)
            vv = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            out[kk] = vv
        return out

    def list_user_alerts(self, user_id: str) -> list[str]:
        ids = self.r.smembers(f"alerts:user:{user_id}")
        out: list[str] = []
        for x in ids or []:
            out.append(x.decode() if isinstance(x, (bytes, bytearray)) else str(x))
        return out

    def list_tf_alerts(self, tf: str) -> list[str]:
        ids = self.r.smembers(f"alerts:tf:{tf}")
        out: list[str] = []
        for x in ids or []:
            out.append(x.decode() if isinstance(x, (bytes, bytearray)) else str(x))
        return out

    def pause(self, alert_id: str) -> None:
        meta = self.get_meta(alert_id)
        tf = meta.get("timeframe")
        self.r.hset(f"alert:{alert_id}:meta", "status", "paused")
        self.r.srem("alerts:active", alert_id)
        if tf:
            self.r.srem(f"alerts:tf:{tf}", alert_id)

    def resume(self, alert_id: str) -> None:
        meta = self.get_meta(alert_id)
        tf = meta.get("timeframe")
        self.r.hset(f"alert:{alert_id}:meta", "status", "active")
        self.r.sadd("alerts:active", alert_id)
        if tf:
            self.r.sadd(f"alerts:tf:{tf}", alert_id)

    def delete(self, alert_id: str) -> None:
        meta = self.get_meta(alert_id)
        tf = meta.get("timeframe")
        self.r.hset(f"alert:{alert_id}:meta", "status", "deleted")
        self.r.srem("alerts:active", alert_id)
        if tf:
            self.r.srem(f"alerts:tf:{tf}", alert_id)

    def get_state(self, alert_id: str, symbol: str) -> dict[str, Any]:
        s = self.r.get(f"alert:{alert_id}:state:{symbol}")
        if not s:
            return {}
        if isinstance(s, bytes):
            s = s.decode("utf-8", errors="replace")
        return json.loads(s)

    def set_state(self, alert_id: str, symbol: str, state: dict[str, Any]) -> None:
        self.r.set(f"alert:{alert_id}:state:{symbol}", json.dumps(state))
