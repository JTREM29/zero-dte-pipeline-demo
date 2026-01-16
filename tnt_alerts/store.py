from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import AlertIntentV1, StoredAlert, StoredAlertState
from .pretty import intent_to_dsl


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")


def alerts_root() -> Path:
    raw = (os.getenv("TNT_ALERTS_ROOT") or "").strip()
    return Path(raw) if raw else Path("alerts")


@dataclass(frozen=True)
class ListItem:
    id: str
    status: str
    summary: str
    created_at_utc: str
    updated_at_utc: str


class AlertStore:
    def __init__(self, root: Path | None = None):
        self.root = root or alerts_root()

    def _day_dir(self, day_utc: str | None = None) -> Path:
        if not day_utc:
            day_utc = time.strftime("%Y-%m-%d", time.gmtime())
        return self.root / day_utc

    def _alert_path(self, alert_id: str, *, day_utc: str | None = None) -> Path:
        return self._day_dir(day_utc) / f"{alert_id}.json"

    def _events_path(self, alert_id: str, *, day_utc: str | None = None) -> Path:
        return self._day_dir(day_utc) / f"{alert_id}_events.jsonl"

    def new_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def create(self, *, intent: AlertIntentV1, alert_id: str | None = None) -> StoredAlert:
        aid = alert_id or self.new_id()
        now = _now_utc_iso()
        stored = StoredAlert(
            id=aid,
            intent=intent,
            state=StoredAlertState(status="active", created_at_utc=now, updated_at_utc=now),
        )
        p = self._alert_path(aid)
        _atomic_write(p, json.dumps(stored.model_dump(mode="json"), indent=2, ensure_ascii=False))
        self.append_event(aid, {"ts": now, "type": "created", "status": "active"})
        return stored

    def load(self, alert_id: str) -> StoredAlert | None:
        # Scan last ~14 days (simple v1 impl; avoids needing an index).
        base = self.root
        if not base.exists():
            return None
        day_dirs = sorted([p for p in base.glob("*") if p.is_dir()], reverse=True)
        for d in day_dirs[:14]:
            p = d / f"{alert_id}.json"
            if p.exists():
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                    return StoredAlert.model_validate(obj)
                except Exception:
                    return None
        return None

    def update_status(self, alert_id: str, *, status: str) -> bool:
        stored = self.load(alert_id)
        if not stored:
            return False

        # Rewrite in-place (same day dir it currently lives in)
        # We locate exact file path by scanning.
        base = self.root
        day_dirs = sorted([p for p in base.glob("*") if p.is_dir()], reverse=True)
        found_path: Path | None = None
        for d in day_dirs[:30]:
            p = d / f"{alert_id}.json"
            if p.exists():
                found_path = p
                break
        if not found_path:
            return False

        now = _now_utc_iso()
        stored.state.status = status  # type: ignore[misc]
        stored.state.updated_at_utc = now  # type: ignore[misc]
        _atomic_write(found_path, json.dumps(stored.model_dump(mode="json"), indent=2, ensure_ascii=False))
        self.append_event(alert_id, {"ts": now, "type": "status", "status": status})
        return True

    def append_event(self, alert_id: str, event: dict[str, Any]) -> None:
        # Events live alongside the alert JSON in its creation-day folder.
        # For v1, we attempt to append to the first matching day dir.
        base = self.root
        if not base.exists():
            # No alerts yet; create today's dir to capture event.
            p = self._events_path(alert_id)
            _append_jsonl(p, event)
            return

        day_dirs = sorted([p for p in base.glob("*") if p.is_dir()], reverse=True)
        for d in day_dirs[:30]:
            if (d / f"{alert_id}.json").exists():
                _append_jsonl(d / f"{alert_id}_events.jsonl", event)
                return

        # Fallback
        _append_jsonl(self._events_path(alert_id), event)

    def list_for_user(self, *, user_id: str, limit: int = 20) -> list[ListItem]:
        base = self.root
        if not base.exists():
            return []
        out: list[ListItem] = []
        day_dirs = sorted([p for p in base.glob("*") if p.is_dir()], reverse=True)
        for d in day_dirs[:30]:
            for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
                if p.name.endswith("_events.jsonl"):
                    continue
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                    stored = StoredAlert.model_validate(obj)
                except Exception:
                    continue
                if stored.intent.source.user_id != user_id:
                    continue
                summary = intent_to_dsl(stored.intent)
                out.append(
                    ListItem(
                        id=stored.id,
                        status=stored.state.status,
                        summary=summary,
                        created_at_utc=stored.state.created_at_utc,
                        updated_at_utc=stored.state.updated_at_utc,
                    )
                )
                if len(out) >= int(limit):
                    return out
        return out


def expand_watchlist_symbols(*, watchlist_name: str, user_id_int: int | None) -> list[str]:
    # Leverage existing TNT concierge watchlist files if present.
    try:
        from tnt_concierge.throttle import watchlist_for_user

        wl = watchlist_for_user(user_id_int)
        return sorted({s.strip().upper() for s in wl if s and s.strip()})
    except Exception:
        return []


def plan_intents(intent: AlertIntentV1, *, user_id_int: int | None = None) -> list[AlertIntentV1]:
    # Expand watchlist -> per-symbol intents (fan-out).
    if intent.targets.type == "symbols":
        return [intent]

    name = (intent.targets.watchlist or "default").strip().lower() or "default"
    if name not in {"default", "user"}:
        raise ValueError("named watchlists not supported yet; use watchlist:default")

    syms = expand_watchlist_symbols(watchlist_name=name, user_id_int=user_id_int)
    if not syms:
        raise ValueError("watchlist is empty (configure config/concierge_watchlist.json)")

    out: list[AlertIntentV1] = []
    for s in syms[: int(intent.targets.max_symbols or 20)]:
        clone = intent.model_copy(deep=True)
        # Pydantic models are mutable unless frozen; we keep it simple here.
        clone.targets.type = "symbols"  # type: ignore[assignment]
        clone.targets.watchlist = None  # type: ignore[assignment]
        clone.targets.symbols = [s]  # type: ignore[assignment]
        out.append(clone)
    return out
