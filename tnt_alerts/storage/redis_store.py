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
        # Normalize market-hours semantics before persisting.
        # If a time window is present, treat it as CUSTOM regardless of session.
        try:
            gates = intent_json.get("gates") if isinstance(intent_json.get("gates"), dict) else None
            if isinstance(gates, dict):
                mh = gates.get("market_hours") if isinstance(gates.get("market_hours"), dict) else None
                if isinstance(mh, dict):
                    win = mh.get("time_window_et")
                    if isinstance(win, (list, tuple)) and len(win) == 2:
                        mh2 = dict(mh)
                        mh2["time_window_et"] = [str(win[0]), str(win[1])]
                        mh2["session"] = "CUSTOM"
                        gates2 = dict(gates)
                        gates2["market_hours"] = mh2
                        intent_json = dict(intent_json)
                        intent_json["gates"] = gates2
        except Exception:
            pass

        # Ensure created_at_utc exists to satisfy the strict contract.
        try:
            src = intent_json.get("source") if isinstance(intent_json.get("source"), dict) else None
            if isinstance(src, dict) and not (src.get("created_at_utc") or "").strip():
                src2 = dict(src)
                src2["created_at_utc"] = datetime.now(timezone.utc).isoformat()
                intent_json = dict(intent_json)
                intent_json["source"] = src2
        except Exception:
            pass

        # Enforce strict schema at persistence time.
        try:
            from tnt_alerts.llm_compiler.validate import validate_intent

            intent_obj = validate_intent(intent_json)
            intent_json = intent_obj.model_dump(mode="json")
        except Exception as exc:
            raise ValueError(f"Intent invalid; refusing to store: {type(exc).__name__}: {exc}") from exc

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
        self.set_alert_paused(alert_id, True)

    def resume(self, alert_id: str) -> None:
        self.set_alert_paused(alert_id, False)

    def set_alert_paused(self, alert_id: str, paused: bool) -> None:
        """Pause/resume with consistent routing invariants.

        Invariant: paused alerts are removed from routing sets used for evaluation
        (`alerts:active` and `alerts:tf:<tf>`), but remain discoverable via
        owner/channel sets.
        """
        meta = self.get_meta(alert_id)
        tf = (meta.get("timeframe") or "").strip()
        pipe = self.r.pipeline()
        pipe.hset(f"alert:{alert_id}:meta", mapping={"status": "paused" if paused else "active"})
        if paused:
            pipe.srem("alerts:active", alert_id)
            if tf:
                pipe.srem(f"alerts:tf:{tf}", alert_id)
        else:
            pipe.sadd("alerts:active", alert_id)
            if tf:
                pipe.sadd(f"alerts:tf:{tf}", alert_id)
        pipe.execute()

    def update_alert(self, alert_id: str, intent_json: dict[str, Any]) -> None:
        """Update an existing alert in-place (keeps the same alert_id)."""
        # Keep the same normalization invariant as create_alert.
        try:
            gates = intent_json.get("gates") if isinstance(intent_json.get("gates"), dict) else None
            if isinstance(gates, dict):
                mh = gates.get("market_hours") if isinstance(gates.get("market_hours"), dict) else None
                if isinstance(mh, dict):
                    win = mh.get("time_window_et")
                    if isinstance(win, (list, tuple)) and len(win) == 2:
                        mh2 = dict(mh)
                        mh2["time_window_et"] = [str(win[0]), str(win[1])]
                        mh2["session"] = "CUSTOM"
                        gates2 = dict(gates)
                        gates2["market_hours"] = mh2
                        intent_json = dict(intent_json)
                        intent_json["gates"] = gates2
        except Exception:
            pass

        # Ensure created_at_utc exists to satisfy the strict contract.
        try:
            src = intent_json.get("source") if isinstance(intent_json.get("source"), dict) else None
            if isinstance(src, dict) and not (src.get("created_at_utc") or "").strip():
                src2 = dict(src)
                src2["created_at_utc"] = datetime.now(timezone.utc).isoformat()
                intent_json = dict(intent_json)
                intent_json["source"] = src2
        except Exception:
            pass

        # Enforce strict schema at persistence time.
        try:
            from tnt_alerts.llm_compiler.validate import validate_intent

            intent_obj = validate_intent(intent_json)
            intent_json = intent_obj.model_dump(mode="json")
        except Exception as exc:
            raise ValueError(f"Intent invalid; refusing to store: {type(exc).__name__}: {exc}") from exc

        old_meta = self.get_meta(alert_id)
        old_tf = (old_meta.get("timeframe") or "").strip()
        old_user_id = (old_meta.get("owner_user_id") or "").strip()
        old_channel_id = (old_meta.get("channel_id") or "").strip()
        status = (old_meta.get("status") or "active").strip().lower() or "active"

        new_tf = ((intent_json.get("condition") or {}).get("timeframe") or "").strip()
        src = intent_json.get("source") or {}
        new_user_id = str(src.get("user_id") or "")
        new_channel_id = str(src.get("channel_id") or "")

        pipe = self.r.pipeline()
        pipe.set(f"alert:{alert_id}:intent", json.dumps(intent_json))

        meta_updates: dict[str, str] = {
            "timeframe": new_tf,
            "owner_user_id": new_user_id,
            "channel_id": new_channel_id,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        pipe.hset(f"alert:{alert_id}:meta", mapping=meta_updates)

        # Update discoverability sets.
        if old_user_id and old_user_id != new_user_id:
            pipe.srem(f"alerts:user:{old_user_id}", alert_id)
        if new_user_id:
            pipe.sadd(f"alerts:user:{new_user_id}", alert_id)

        if old_channel_id and old_channel_id != new_channel_id:
            pipe.srem(f"alerts:chan:{old_channel_id}", alert_id)
        if new_channel_id:
            pipe.sadd(f"alerts:chan:{new_channel_id}", alert_id)

        # Update timeframe routing set membership.
        if old_tf and old_tf != new_tf:
            pipe.srem(f"alerts:tf:{old_tf}", alert_id)
        if new_tf:
            if status == "active":
                pipe.sadd(f"alerts:tf:{new_tf}", alert_id)
            else:
                pipe.srem(f"alerts:tf:{new_tf}", alert_id)

        # Active routing set depends on current status.
        if status == "active":
            pipe.sadd("alerts:active", alert_id)
        else:
            pipe.srem("alerts:active", alert_id)

        pipe.execute()

    def expire_alert(self, alert_id: str) -> None:
        """Mark an alert as expired and remove it from routing sets."""
        meta = self.get_meta(alert_id)
        tf = (meta.get("timeframe") or "").strip()
        pipe = self.r.pipeline()
        pipe.hset(f"alert:{alert_id}:meta", mapping={"status": "expired", "expired_at_utc": datetime.now(timezone.utc).isoformat()})
        pipe.srem("alerts:active", alert_id)
        if tf:
            pipe.srem(f"alerts:tf:{tf}", alert_id)
        pipe.execute()

    def delete(self, alert_id: str) -> None:
        meta = self.get_meta(alert_id)
        tf = meta.get("timeframe")
        user_id = meta.get("owner_user_id")
        channel_id = meta.get("channel_id")

        # Remove from routing sets first.
        pipe = self.r.pipeline()
        pipe.srem("alerts:active", alert_id)
        if tf:
            pipe.srem(f"alerts:tf:{tf}", alert_id)
        if user_id:
            pipe.srem(f"alerts:user:{user_id}", alert_id)
        if channel_id:
            pipe.srem(f"alerts:chan:{channel_id}", alert_id)

        # Mark deleted for audit, then hard-delete keys.
        pipe.hset(f"alert:{alert_id}:meta", "status", "deleted")
        pipe.delete(f"alert:{alert_id}:intent")
        pipe.delete(f"alert:{alert_id}:meta")

        # Delete any per-symbol state keys.
        cursor = 0
        pattern = f"alert:{alert_id}:state:*"
        while True:
            cursor, keys = self.r.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                pipe.delete(*keys)
            if int(cursor) == 0:
                break

        pipe.execute()

    def get_state(self, alert_id: str, symbol: str) -> dict[str, Any]:
        s = self.r.get(f"alert:{alert_id}:state:{symbol}")
        if not s:
            return {}
        if isinstance(s, bytes):
            s = s.decode("utf-8", errors="replace")
        return json.loads(s)

    def set_state(self, alert_id: str, symbol: str, state: dict[str, Any]) -> None:
        self.r.set(f"alert:{alert_id}:state:{symbol}", json.dumps(state))
