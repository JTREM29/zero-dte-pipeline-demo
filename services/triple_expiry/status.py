from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from services.triple_expiry.keys import manifest_key, pack_key


def _et_tz():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone.utc


def now_et_iso() -> str:
    return datetime.now(_et_tz()).isoformat()


def today_et_ymd() -> str:
    return datetime.now(_et_tz()).date().isoformat()


def _json_loads(s: Any) -> Any:
    if not isinstance(s, (str, bytes, bytearray)):
        return None
    try:
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="replace")
        return json.loads(str(s))
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _age_seconds(iso_ts: Any, now_utc: datetime) -> int | None:
    dt = _parse_dt(iso_ts)
    if dt is None:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((now_utc - dt.astimezone(timezone.utc)).total_seconds()))
    except Exception:
        return None


def read_manifest_keys(*, r: Any, date_et_ymd: str) -> list[str]:
    raw = r.get(manifest_key(date_et_ymd))
    obj = _json_loads(raw)
    if not isinstance(obj, dict):
        return []
    keys = obj.get("keys")
    if not isinstance(keys, list):
        return []
    out: list[str] = []
    for k in keys:
        s = str(k or "").strip()
        if s:
            out.append(s)
    return out


def load_pack_for_symbol(*, r: Any, symbol: str, date_et_ymd: str) -> dict[str, Any] | None:
    """Load the best-effort pack for a symbol for the ET date.

    Uses the symbol+date pointer key first; if it contains an expiry, loads the
    canonical symbol+expiry key.
    """

    sym = str(symbol or "").strip().upper()
    if not sym:
        return None

    raw0 = r.get(pack_key(sym, date_et_ymd))
    obj0 = _json_loads(raw0)
    if not isinstance(obj0, dict):
        return None

    exp = str(obj0.get("expiry") or "").strip()
    if not exp:
        return obj0

    raw = r.get(pack_key(sym, exp))
    obj = _json_loads(raw)
    return obj if isinstance(obj, dict) else obj0


def build_status_rows(
    *,
    r: Any,
    date_et_ymd: str,
    symbols: Iterable[str],
    per_symbol_cooldown_s: int,
) -> list[dict[str, Any]]:
    now_utc = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []

    for s in symbols:
        sym = str(s or "").strip().upper()
        if not sym:
            continue

        obj = load_pack_for_symbol(r=r, symbol=sym, date_et_ymd=date_et_ymd)
        if not isinstance(obj, dict):
            out.append(
                {
                    "symbol": sym,
                    "resolver": "pack_missing",
                    "expiry": None,
                    "pack_age_s": None,
                    "px_age_min": None,
                    "artifact_present": False,
                    "last_post_utc": None,
                    "cooldown_remaining_s": None,
                }
            )
            continue

        resolver = str(obj.get("reason") or "") or ("ok" if obj.get("ok") else "unknown")
        expiry = str(obj.get("expiry") or "").strip() or None

        pack_age_s = _age_seconds(obj.get("created_utc"), now_utc)

        price = obj.get("price") if isinstance(obj.get("price"), dict) else {}
        px_age_min = price.get("age_minutes") if isinstance(price.get("age_minutes"), (int, float)) else None

        r_oi = obj.get("renders") if isinstance(obj.get("renders"), dict) else {}
        r_oi = r_oi.get("oi_iv") if isinstance(r_oi, dict) else None
        url = str(r_oi.get("artifact_url") or "").strip() if isinstance(r_oi, dict) else ""
        path = str(r_oi.get("out_path") or "").strip() if isinstance(r_oi, dict) else ""
        artifact_present = bool(url or path)

        # Last post + cooldown remaining.
        last_key = f"triple_expiry:last_post:{sym}"
        last_raw = r.get(last_key)
        last_ts = None
        try:
            last_ts = int(str(last_raw or "") or "0")
        except Exception:
            last_ts = None

        cooldown_remaining = None
        last_iso = None
        if last_ts:
            last_iso = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
            try:
                cooldown_remaining = max(0, int(per_symbol_cooldown_s) - int((now_utc.timestamp() - last_ts)))
            except Exception:
                cooldown_remaining = None

        out.append(
            {
                "symbol": sym,
                "resolver": resolver,
                "expiry": expiry,
                "pack_age_s": pack_age_s,
                "px_age_min": px_age_min,
                "artifact_present": artifact_present,
                "last_post_utc": last_iso,
                "cooldown_remaining_s": cooldown_remaining,
                "pack_ok": bool(obj.get("ok")),
            }
        )

    return out
