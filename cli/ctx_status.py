"""ctx visibility + Redis config status builder for Discord ops.

Goal: prove (from inside the bot runtime) which Redis host/port/db is being used
and whether the canonical ctx keys exist and are fresh.

Intentionally dependency-light.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from services.redis_env import redis_client, redis_env


def _utc_iso(ts_utc: Optional[float]) -> str:
    if ts_utc is None:
        return "(none)"
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts_utc)))
    except Exception:
        return "(bad_ts)"


def _safe_json_load(s: Any) -> Optional[dict]:
    if s is None:
        return None
    if isinstance(s, (bytes, bytearray)):
        try:
            s = s.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if not isinstance(s, str):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _ts_age_s(ts_utc: Any) -> Optional[int]:
    try:
        ts_i = int(float(ts_utc))
    except Exception:
        return None
    try:
        now = int(time.time())
        return max(0, now - int(ts_i))
    except Exception:
        return None


def probe_ctx(*, symbol: str, timeout_s: float = 0.25, count_keys: bool = False, scan_cap: int = 5000) -> Dict[str, Any]:
    sym = (symbol or "").strip().upper() or "SPY"
    env = redis_env()

    out: Dict[str, Any] = {
        "redis": {"host": env.host, "port": env.port, "db": env.db},
        "ping_ok": False,
    }

    try:
        r = redis_client(timeout_s=timeout_s, decode_responses=True)
    except Exception as exc:
        out["error"] = f"redis_client_failed:{type(exc).__name__}"
        return out

    try:
        out["ping_ok"] = bool(r.ping())
    except Exception as exc:
        out["ping_error"] = f"{type(exc).__name__}"

    # Fingerprint Redis instance: server run_id (short) + dbsize.
    try:
        info = r.info("server")
        run_id = ""
        if isinstance(info, dict):
            run_id = str(info.get("run_id") or "")
        out["redis_run_id"] = (run_id[:8] if run_id else "")
    except Exception as exc:
        out["redis_run_id_error"] = f"{type(exc).__name__}"

    try:
        out["redis_dbsize"] = int(r.dbsize())
    except Exception as exc:
        out["redis_dbsize_error"] = f"{type(exc).__name__}"

    def _read_ctx_key(key: str) -> Dict[str, Any]:
        try:
            raw = r.get(key)
        except Exception as exc:
            return {"key": key, "exists": False, "error": f"get_failed:{type(exc).__name__}"}

        if raw is None:
            return {"key": key, "exists": False}

        doc = _safe_json_load(raw)
        if not isinstance(doc, dict):
            return {"key": key, "exists": True, "bad_json": True}

        ts_utc = doc.get("ts_utc")
        age_s = _ts_age_s(ts_utc)
        return {
            "key": key,
            "exists": True,
            "bad_json": False,
            "ts_utc": ts_utc,
            "age_s": age_s,
        }

    out["ctx_market"] = _read_ctx_key("ctx:market")
    out["ctx_sym"] = _read_ctx_key(f"ctx:sym:{sym}")

    # Writer metadata keys (best-effort).
    meta_keys = [
        "ctx:last_write_ts",
        "ctx:last_write_count",
        "ctx:last_write_seq",
        "ctx:last_writer_host",
        "ctx:last_writer_pid",
    ]
    meta: Dict[str, Any] = {}
    for k in meta_keys:
        try:
            meta[k] = r.get(k)
        except Exception:
            meta[k] = None
    out["ctx_meta"] = meta

    if bool(count_keys):
        n = 0
        capped = False
        try:
            # scan_iter avoids blocking the server with KEYS.
            for _k in r.scan_iter(match="ctx:*", count=250):
                n += 1
                if n >= int(scan_cap):
                    capped = True
                    break
        except Exception as exc:
            out["ctx_key_count_error"] = f"scan_failed:{type(exc).__name__}"
        else:
            out["ctx_key_count"] = int(n)
            out["ctx_key_count_capped"] = bool(capped)

    return out


def build_ctx_status_message(*, symbol: str, count_keys: bool = False, timeout_s: float = 0.25) -> str:
    sym = (symbol or "").strip().upper() or "SPY"
    p = probe_ctx(symbol=sym, timeout_s=timeout_s, count_keys=bool(count_keys))

    r_cfg = p.get("redis") if isinstance(p.get("redis"), dict) else {}
    host = str(r_cfg.get("host") or "")
    port = r_cfg.get("port")
    db = r_cfg.get("db")

    lines: list[str] = []
    rid = str(p.get("redis_run_id") or "")
    rid_txt = (rid if rid else "unknown")
    dbsize = p.get("redis_dbsize")
    dbsize_txt = "unknown" if dbsize is None else str(dbsize)
    lines.append(
        f"Redis (bot runtime): host={host or '(unset)'} port={port} db={db} run_id={rid_txt} dbsize={dbsize_txt}"
    )
    if p.get("error"):
        lines.append(f"Redis init error: {p.get('error')}")
        return "\n".join(lines)

    ping_ok = bool(p.get("ping_ok"))
    ping_err = p.get("ping_error")
    lines.append("Ping: " + ("ok" if ping_ok else f"fail ({ping_err or 'unknown'})"))

    def _fmt_ctx_block(label: str, d: Any) -> None:
        if not isinstance(d, dict):
            lines.append(f"{label}: (unavailable)")
            return
        exists = bool(d.get("exists"))
        if not exists:
            err = d.get("error")
            lines.append(f"{label}: exists=False" + (f" error={err}" if err else ""))
            return
        if bool(d.get("bad_json")):
            lines.append(f"{label}: exists=True bad_json=True")
            return
        ts_utc = d.get("ts_utc")
        age_s = d.get("age_s")
        age_lbl = "unknown" if age_s is None else f"{int(age_s)}s"
        lines.append(f"{label}: exists=True ts_utc={_utc_iso(ts_utc)} age_s={age_lbl}")

    _fmt_ctx_block("ctx:market", p.get("ctx_market"))
    _fmt_ctx_block(f"ctx:sym:{sym}", p.get("ctx_sym"))

    meta = p.get("ctx_meta") if isinstance(p.get("ctx_meta"), dict) else {}
    if meta:
        last_ts = meta.get("ctx:last_write_ts")
        last_age = _ts_age_s(last_ts)
        last_age_lbl = "unknown" if last_age is None else f"{int(last_age)}s"
        lines.append(
            "writer_meta: "
            + f"last_write_ts={_utc_iso(last_ts)} age_s={last_age_lbl} "
            + f"count={meta.get('ctx:last_write_count') or '(none)'} "
            + f"seq={meta.get('ctx:last_write_seq') or '(none)'} "
            + f"host={meta.get('ctx:last_writer_host') or '(none)'} "
            + f"pid={meta.get('ctx:last_writer_pid') or '(none)'}"
        )

    if bool(count_keys):
        if p.get("ctx_key_count_error"):
            lines.append(f"ctx:* key_count: error={p.get('ctx_key_count_error')}")
        else:
            n = p.get("ctx_key_count")
            capped = bool(p.get("ctx_key_count_capped"))
            lines.append(f"ctx:* key_count: {n}{' (capped)' if capped else ''}")

    return "\n".join(lines)
