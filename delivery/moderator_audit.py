from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping


def _utc_date_str(ts: float | None = None) -> str:
    ts0 = time.time() if ts is None else float(ts)
    return time.strftime("%Y-%m-%d", time.gmtime(ts0))


def _safe_text(s: str, max_len: int = 280) -> str:
    t = (s or "").replace("\n", " ").replace("\r", " ").strip()
    if len(t) <= int(max_len):
        return t
    return t[: max(0, int(max_len) - 3)] + "..."


def _age_s_from_ts(now: float, ts: Any) -> int | None:
    """Best-effort age calculator for epoch seconds or ISO strings."""

    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return max(0, int(float(now) - float(ts)))
        if isinstance(ts, str):
            s = ts.strip()
            if not s:
                return None
            if s.isdigit():
                return max(0, int(float(now) - float(int(s))))
            try:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0, int(float(now) - float(dt.timestamp())))
            except Exception:
                return None
    except Exception:
        return None
    return None


class ModeratorAudit:
    """Append-only JSONL audit log for moderator decisions.

    Env:
      TNT_MOD_AUDIT_ENABLED=1
      TNT_MOD_AUDIT_DIR=logs/moderator_audit
      TNT_MOD_AUDIT_INCLUDE_TEXT=0/1  (default 0)
    """

    def __init__(
        self,
        *,
        enabled: bool,
        root_dir: str = "logs/moderator_audit",
        include_text: bool = False,
    ):
        self.enabled = bool(enabled)
        self.root_dir = Path(str(root_dir or "logs/moderator_audit"))
        self.include_text = bool(include_text)

    @classmethod
    def from_env(cls) -> "ModeratorAudit":
        enabled = (os.getenv("TNT_MOD_AUDIT_ENABLED", "0") or "0").strip() == "1"
        root_dir = (os.getenv("TNT_MOD_AUDIT_DIR", "logs/moderator_audit") or "logs/moderator_audit").strip()
        include_text = (os.getenv("TNT_MOD_AUDIT_INCLUDE_TEXT", "0") or "0").strip() == "1"
        return cls(enabled=enabled, root_dir=root_dir, include_text=include_text)

    def log(
        self,
        *,
        decision: Any,
        source: str,
        user_id: int | None = None,
        channel_id: int | None = None,
        is_admin: bool | None = None,
        owner_available: bool | None = None,
        draining: bool | None = None,
        safe_mode: bool | None = None,
        live_hours: bool | None = None,
        text: str | None = None,
        ctx_market: Mapping[str, Any] | None = None,
        ctx_sym: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return

        try:
            now = time.time()
            day = _utc_date_str(now)
            out_dir = self.root_dir / day
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / "moderator.jsonl"

            rec: dict[str, Any] = {
                "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "ts_epoch": int(now),
                "source": str(source or ""),
                "user_id": user_id,
                "channel_id": channel_id,
                "is_admin": bool(is_admin) if is_admin is not None else None,
                "owner_available": bool(owner_available) if owner_available is not None else None,
                "draining": bool(draining) if draining is not None else None,
                "safe_mode": bool(safe_mode) if safe_mode is not None else None,
                "live_hours": bool(live_hours) if live_hours is not None else None,
            }

            # Decision payload (support dataclass or dict-like).
            dec_obj: Any
            if is_dataclass(decision):
                dec_obj = asdict(decision)
            elif isinstance(decision, dict):
                dec_obj = dict(decision)
            else:
                # Best-effort for namedtuple-like.
                dec_obj = {
                    "intent": getattr(decision, "intent", None),
                    "state": getattr(decision, "state", None),
                    "confidence": getattr(decision, "confidence", None),
                    "reason": getattr(decision, "reason", None),
                }

            # Normalize enums to their values (json.dumps would do str(Enum), but be explicit).
            for k in ("intent", "state"):
                v = dec_obj.get(k)
                if hasattr(v, "value"):
                    dec_obj[k] = getattr(v, "value")

            rec.update(
                {
                    "intent": dec_obj.get("intent"),
                    "state": dec_obj.get("state"),
                    "confidence": dec_obj.get("confidence"),
                    "reason": dec_obj.get("reason"),
                }
            )

            # Compatibility: some consumers prefer `rule`.
            rec["rule"] = rec.get("reason")

            # Snapshot presence only (keep it privacy-safe).
            rec["market_ok"] = bool(isinstance(ctx_market, Mapping))
            rec["symbol_ok"] = bool(isinstance(ctx_sym, Mapping))

            # Additional freshness fields for ops display.
            try:
                rec["market_age_s"] = _age_s_from_ts(now, (ctx_market or {}).get("ts_utc")) if ctx_market else None
            except Exception:
                rec["market_age_s"] = None

            try:
                sym_ts = (ctx_sym or {}).get("ts_utc") if ctx_sym else None
                sym_age = _age_s_from_ts(now, sym_ts)
                rec["symbol_age_s"] = sym_age
                rec["snapshot_ok"] = rec["symbol_ok"]
                rec["snapshot_age_s"] = sym_age
            except Exception:
                rec["symbol_age_s"] = None
                rec["snapshot_ok"] = rec["symbol_ok"]
                rec["snapshot_age_s"] = None

            # Futures freshness (best-effort; derived from ctx:market futures block).
            futures_ok = False
            futures_age_s = None
            futures_label = None
            try:
                fut = (ctx_market or {}).get("futures") if isinstance(ctx_market, Mapping) else None
                if isinstance(fut, Mapping):
                    futures_age_s = fut.get("hb_age_sec")
                    futures_label = fut.get("regime") or fut.get("bias")
                    degraded = bool(fut.get("degraded", False))
                    bias = fut.get("bias")
                    futures_ok = bool(bias is not None and not degraded)
            except Exception:
                futures_ok = False
                futures_age_s = None
                futures_label = None

            rec["futures_ok"] = bool(futures_ok)
            rec["futures_age_s"] = futures_age_s
            rec["futures_label"] = futures_label

            if self.include_text and text is not None:
                rec["text"] = _safe_text(text)

            if extra:
                rec["extra"] = dict(extra)

            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            # Audit should never break the user-facing flow.
            return
