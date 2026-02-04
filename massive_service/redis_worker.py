from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delivery import discord_bot as delivery
from delivery.oi_iv_render import bucket_oi_iv_by_strike, render_oi_iv_png


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _now_s() -> float:
    return float(time.time())


def _safe_upper_symbol(value: str) -> str:
    return (value or "").strip().upper()


def _ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def _env_str(name: str, default: str = "") -> str:
    try:
        return str(os.getenv(name, default) or default)
    except Exception:
        return str(default)


def _truthy_env(name: str, default: str = "0") -> bool:
    try:
        raw = (os.getenv(name, default) or default).strip().lower()
    except Exception:
        raw = str(default).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _artifact_url_for(job_id: str, *, ext: str = "png") -> str | None:
    base = (_env_str("TNT_ARTIFACT_HTTP_BASE_URL") or _env_str("TNT_ARTIFACTS_BASE_URL") or "").strip()
    if not base:
        return None
    base = base.rstrip("/")
    safe_ext = (ext or "").lstrip(".") or "png"
    artifact_id = _artifact_basename(job_id)
    return f"{base}/artifacts/{artifact_id}.{safe_ext}"


def _artifact_basename(job_id: str) -> str:
    """Return a filesystem-safe basename for artifacts derived from job_id.

    Windows forbids characters like : * ? " < > | and control chars in filenames.
    We also append a short hash when sanitization changes the string to preserve uniqueness.
    """

    raw = str(job_id or "")
    # Replace Windows-illegal filename characters + control chars with underscores.
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", raw)
    # Collapse whitespace and repeated separators.
    safe = re.sub(r"\s+", "_", safe)
    safe = re.sub(r"_+", "_", safe)
    # Windows also disallows trailing dots/spaces.
    safe = safe.strip(" ._-")
    if not safe:
        safe = "job"

    if safe != raw:
        digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
        safe = f"{safe}_{digest}"

    # Avoid very long filenames (path length constraints).
    return safe[:180]


def _join_path(folder: str, filename: str) -> str:
    # Works for UNC and local paths.
    folder2 = (folder or "").rstrip("\\/")
    if not folder2:
        return filename
    return str(Path(folder2) / filename)


def _parse_job(raw: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _set_hb(r: object, *, key: str, ttl_sec: int, message: str) -> None:
    try:
        # redis-py client supports setex(name, time, value)
        getattr(r, "setex")(key, int(ttl_sec), str(message))
    except Exception:
        return


def _bucket_5m(now_s: float | None = None) -> int:
    """Return a stable UTC bucket id for 5-minute windows."""

    t = float(now_s if now_s is not None else time.time())
    return int(t // 300)


def _incr_bucket(r: object, *, key_prefix: str, bucket: int, ttl_sec: int = 6 * 3600) -> None:
    """Best-effort per-bucket counter for ops heartbeats."""

    try:
        k = f"{key_prefix}{int(bucket)}"
        getattr(r, "incr")(k)
        # Keep bucket counters around for a few hours for debugging.
        getattr(r, "expire")(k, int(ttl_sec))
    except Exception:
        return


def _probe_options_access(
    *,
    symbol: str,
    expiry: str | None,
    strike_window_pct: float,
    max_contracts: int,
) -> dict[str, Any]:
    """Return diagnostic info for why options chain fetch might be empty.

    delivery._fetch_polygon_options_chain_df intentionally returns None on any
    non-200 to keep callers simple; the worker needs a more actionable reason.
    """

    diag: dict[str, Any] = {
        "symbol": (symbol or "").strip().upper(),
        "expiry": (expiry or ""),
        "provider": None,
        "base_url": None,
        "contracts_status": None,
        "contracts_detail": None,
        "snapshot_status": None,
        "snapshot_detail": None,
    }

    try:
        api_key, base_url, provider = delivery._polygon_key_and_base()
        diag["provider"] = provider
        diag["base_url"] = base_url
    except Exception as exc:
        diag["reason"] = f"key_and_base_failed:{type(exc).__name__}:{exc}"
        return diag

    if not api_key:
        diag["reason"] = "missing_api_key"
        return diag

    sym = (symbol or "").strip().upper()
    if not sym:
        diag["reason"] = "missing_symbol"
        return diag

    try:
        import httpx
    except Exception as exc:
        diag["reason"] = f"httpx_missing:{type(exc).__name__}:{exc}"
        return diag

    try:
        underlying = delivery._map_underlying_for_options(sym)
    except Exception:
        underlying = sym

    # Determine expiration default same as delivery._fetch_polygon_options_chain_df.
    try:
        exp = (expiry or delivery._now_et().date().isoformat())[:10]
    except Exception:
        exp = (expiry or "")[:10]
    diag["expiry"] = exp

    # Strike bounds: best-effort only.
    strike_min = strike_max = None
    try:
        snap = delivery._get_last_price_snapshot(sym)
        underlying_px = float(snap.px) if snap and snap.px is not None else None
        if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
            window = max(float(strike_window_pct), 0.0)
            strike_min = underlying_px * (1.0 - window)
            strike_max = underlying_px * (1.0 + window)
    except Exception:
        strike_min = strike_max = None

    url_contracts = f"{str(base_url).rstrip('/')}/v3/reference/options/contracts"
    params: dict[str, Any] = {
        "underlying_ticker": underlying,
        "expiration_date": exp,
        "limit": int(max_contracts),
        "apiKey": api_key,
    }
    if strike_min is not None and strike_max is not None:
        params["strike_price.gte"] = f"{strike_min:.6f}"
        params["strike_price.lte"] = f"{strike_max:.6f}"

    ticker: str | None = None
    try:
        resp = httpx.get(url_contracts, params=params, timeout=12.0)
        diag["contracts_status"] = int(getattr(resp, "status_code", 0) or 0)
        if int(diag["contracts_status"] or 0) != 200:
            try:
                j = resp.json()
                if isinstance(j, dict):
                    diag["contracts_detail"] = j.get("error") or j.get("message") or j.get("status")
            except Exception:
                diag["contracts_detail"] = (getattr(resp, "text", "") or "")[:250]
            diag["reason"] = f"contracts_http_{diag['contracts_status']}"
            return diag

        j = resp.json()
        if not isinstance(j, dict) or j.get("status") != "OK":
            diag["contracts_detail"] = j.get("error") if isinstance(j, dict) else None
            diag["reason"] = "contracts_not_ok"
            return diag

        results = j.get("results")
        if not isinstance(results, list) or not results:
            diag["reason"] = "contracts_empty"
            return diag

        first = results[0] if isinstance(results[0], dict) else None
        ticker = str(first.get("ticker") or "").strip() if isinstance(first, dict) else None
        if not ticker:
            diag["reason"] = "contracts_missing_ticker"
            return diag
    except Exception as exc:
        diag["reason"] = f"contracts_fetch_failed:{type(exc).__name__}:{exc}"
        return diag

    url_snap = f"{str(base_url).rstrip('/')}/v3/snapshot/options/{underlying}/{ticker}"
    try:
        resp2 = httpx.get(url_snap, params={"apiKey": api_key}, timeout=12.0)
        diag["snapshot_status"] = int(getattr(resp2, "status_code", 0) or 0)
        if int(diag["snapshot_status"] or 0) != 200:
            try:
                j2 = resp2.json()
                if isinstance(j2, dict):
                    diag["snapshot_detail"] = j2.get("error") or j2.get("message") or j2.get("status")
            except Exception:
                diag["snapshot_detail"] = (getattr(resp2, "text", "") or "")[:250]
            diag["reason"] = f"snapshot_http_{diag['snapshot_status']}"
            return diag
        diag["reason"] = "ok"
        return diag
    except Exception as exc:
        diag["reason"] = f"snapshot_fetch_failed:{type(exc).__name__}:{exc}"
        return diag


@dataclass(frozen=True)
class JobResult:
    job_id: str
    ok: bool
    type: str
    symbol: str
    out_path: str | None
    artifact_url: str | None
    meta: dict[str, Any] | None
    error: str | None
    took_ms: int

    def to_json(self) -> str:
        status = "succeeded" if bool(self.ok) else "failed"
        artifact_obj: dict[str, str] | None = None
        if self.out_path or self.artifact_url:
            artifact_obj = {}
            if self.out_path:
                artifact_obj["path"] = self.out_path
            if self.artifact_url:
                artifact_obj["url"] = self.artifact_url

        ext = ""
        try:
            if self.out_path:
                ext = str(Path(self.out_path).suffix or "").lower()
            elif self.artifact_url:
                ext = "." + str(self.artifact_url).split("?")[0].split("#")[0].rsplit(".", 1)[-1].lower()
        except Exception:
            ext = ""

        artifacts: dict[str, object] = {}
        if artifact_obj:
            if ext == ".json":
                artifacts = {"json": artifact_obj}
            else:
                artifacts = {"png": artifact_obj}
        payload = {
            "job_id": self.job_id,
            "status": status,
            "ok": bool(self.ok),
            "type": self.type,
            "symbol": self.symbol,
            "out_path": self.out_path,
            # Backwards-compatible: keep out_path, but prefer structured artifacts.png for new clients.
            "artifact_url": self.artifact_url,
            "artifacts": artifacts,
            "meta": self.meta,
            "error": self.error,
            "took_ms": int(self.took_ms),
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "worker_pid": int(os.getpid()),
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _render_oi_job(job: dict[str, Any], *, r: object | None = None, hb_key: str | None = None, hb_ttl_sec: int = 300) -> JobResult:
    t0 = _now_s()
    job_id = str(job.get("job_id") or str(uuid.uuid4()))
    typ = str(job.get("type") or "")
    symbol = _safe_upper_symbol(str(job.get("symbol") or ""))

    if r is not None and hb_key:
        _set_hb(r, key=hb_key, ttl_sec=hb_ttl_sec, message=f"{job_id}: started")

    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    expiry = str(params.get("expiry") or "").strip() or None
    top_n = int(params.get("top") or 18)
    dpi = int(params.get("dpi") or 130)

    strike_window_pct = 0.07
    max_contracts = 250

    reply = job.get("reply") if isinstance(job.get("reply"), dict) else {}
    out_folder = str(reply.get("path") or "").strip()
    if not out_folder:
        # New mode: no shared folder required. Write locally and (optionally) serve over HTTP.
        out_folder = (_env_str("TNT_ARTIFACTS_DIR", "artifacts") or "artifacts").strip()

    if not symbol:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, artifact_url=None, meta=None, error="missing symbol", took_ms=took)

    try:
        if r is not None and hb_key:
            _set_hb(r, key=hb_key, ttl_sec=hb_ttl_sec, message=f"{job_id}: fetching chain")
        df = asyncio.run(
            delivery._fetch_polygon_options_chain_df(
                symbol,
                expiration_ymd=expiry,
                strike_window_pct=strike_window_pct,
                max_contracts=max_contracts,
                concurrency=8,
            )
        )
    except Exception as exc:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, artifact_url=None, meta=None, error=f"fetch_failed:{type(exc).__name__}:{exc}", took_ms=took)

    if df is None or getattr(df, "empty", True):
        took = int((_now_s() - t0) * 1000)
        diag = _probe_options_access(symbol=symbol, expiry=expiry, strike_window_pct=strike_window_pct, max_contracts=max_contracts)
        try:
            reason = str(diag.get("reason") or "unknown")
        except Exception:
            reason = "unknown"
        # Preserve legacy surface error while making it actionable.
        return JobResult(
            job_id=job_id,
            ok=False,
            type=typ,
            symbol=symbol,
            out_path=None,
            artifact_url=None,
            meta={"probe": diag},
            error=f"no_data:{reason}",
            took_ms=took,
        )

    strikes, oi_calls, oi_puts, iv_pct = bucket_oi_iv_by_strike(df, sym=symbol)
    if not strikes:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, artifact_url=None, meta=None, error="no_strikes", took_ms=took)

    # Keep only top-N by total OI for readability.
    try:
        if top_n > 0 and len(strikes) > top_n:
            totals = [float(oi_calls[i]) + float(oi_puts[i]) for i in range(len(strikes))]
            keep = sorted(range(len(strikes)), key=lambda i: totals[i], reverse=True)[:top_n]
            keep_sorted = sorted(keep, key=lambda i: strikes[i])
            strikes = [strikes[i] for i in keep_sorted]
            oi_calls = [oi_calls[i] for i in keep_sorted]
            oi_puts = [oi_puts[i] for i in keep_sorted]
            iv_pct = [iv_pct[i] for i in keep_sorted]
    except Exception:
        pass

    if r is not None and hb_key:
        _set_hb(r, key=hb_key, ttl_sec=hb_ttl_sec, message=f"{job_id}: rendering png")
    labels = [f"{s:g}" for s in strikes]
    title = f"{symbol} Options — OI by Strike + IV Overlay"
    png = render_oi_iv_png(
        title=title,
        x_labels=labels,
        iv_pct=iv_pct,
        oi_calls=oi_calls,
        oi_puts=oi_puts,
        dpi=dpi,
        include_iv_overlay=True,
    )
    if not png:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, artifact_url=None, meta=None, error="render_failed", took_ms=took)

    try:
        # If reply path is local folder, ensure it exists. For UNC shares, mkdir may fail; that’s fine.
        _ensure_dir(out_folder)
    except Exception:
        pass

    artifact_id = _artifact_basename(job_id)
    out_path = _join_path(out_folder, f"{artifact_id}.png")
    try:
        if r is not None and hb_key:
            _set_hb(r, key=hb_key, ttl_sec=hb_ttl_sec, message=f"{job_id}: writing artifact")
        Path(out_path).write_bytes(png)
    except Exception as exc:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=out_path, artifact_url=None, meta=None, error=f"write_failed:{type(exc).__name__}:{exc}", took_ms=took)

    took = int((_now_s() - t0) * 1000)
    return JobResult(job_id=job_id, ok=True, type=typ, symbol=symbol, out_path=out_path, artifact_url=_artifact_url_for(job_id, ext="png"), meta=None, error=None, took_ms=took)


def _alert_trigger_job(job: dict[str, Any], *, r: object | None = None, hb_key: str | None = None, hb_ttl_sec: int = 300) -> JobResult:
    t0 = _now_s()

    job_id = str(job.get("job_id") or str(uuid.uuid4()))
    typ = str(job.get("type") or "")
    symbol = _safe_upper_symbol(str(job.get("symbol") or ""))

    if r is not None and hb_key:
        _set_hb(r, key=hb_key, ttl_sec=hb_ttl_sec, message=f"{job_id}: started")

    reply = job.get("reply") if isinstance(job.get("reply"), dict) else {}
    out_folder = str(reply.get("path") or reply.get("results_dir") or "").strip()
    if not out_folder:
        out_folder = (_env_str("TNT_ARTIFACTS_DIR", "artifacts") or "artifacts").strip()

    try:
        _ensure_dir(out_folder)
    except Exception:
        pass

    artifact_id = _artifact_basename(job_id)
    out_path = _join_path(out_folder, f"{artifact_id}.json")

    payload = {
        "job_id": job_id,
        "type": typ,
        "symbol": symbol,
        "alert_id": job.get("alert_id"),
        "ts_utc": job.get("ts_utc"),
        "intent": job.get("intent"),
        "event": job.get("event"),
        "actions": job.get("actions"),
        "worker_pid": int(os.getpid()),
        "worker_ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    try:
        if r is not None and hb_key:
            _set_hb(r, key=hb_key, ttl_sec=hb_ttl_sec, message=f"{job_id}: writing json")
        Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        # Ops telemetry (best-effort).
        if r is not None:
            try:
                r.set("tnt:alerts:worker:last_job_utc", str(payload.get("worker_ts_utc") or ""))
                r.set("tnt:alerts:worker:last_job_id", str(job_id))
                r.set("tnt:alerts:worker:last_alert_id", str(job.get("alert_id") or ""))
                r.set("tnt:alerts:worker:last_symbol", str(symbol))
            except Exception:
                pass

        # Optional: enqueue for Discord delivery (handled by cli.discord_bot delivery loop).
        # This is gated so environments can run workers without posting.
        if r is not None and (
            _truthy_env("TNT_ALERTS_DISCORD_ENQUEUE_ENABLED", "0")
            or _truthy_env("TNT_ALERTS_DISCORD_DELIVERY_ENABLED", "0")
        ):
            queue_key = (
                os.getenv("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue")
                or "tnt:alerts:discord_queue"
            ).strip()

            # Delivery loop expects a job-shaped dict (alert_id/symbol/tf/ts_utc/intent/event/actions).
            delivery_job: dict[str, Any] = dict(job)
            delivery_job.setdefault("type", "alert_trigger")
            delivery_job.setdefault("job_type", "alert_trigger")
            delivery_job.setdefault("job_id", job_id)
            delivery_job.setdefault("alert_id", job.get("alert_id"))
            delivery_job.setdefault("symbol", symbol)
            # Provide helpful linkage (not required by current formatter).
            delivery_job["worker_out_path"] = out_path
            delivery_job["worker_artifact_url"] = _artifact_url_for(job_id, ext="json")

            try:
                getattr(r, "rpush")(queue_key, json.dumps(delivery_job, ensure_ascii=False, separators=(",", ":")))
                print(
                    "[TNT][ALERTS][ENQUEUE_DISCORD] "
                    + f"queue={queue_key} job_id={job_id} alert_id={job.get('alert_id')} symbol={symbol}"
                )
            except Exception as exc:
                print(
                    "[TNT][ALERTS][ENQUEUE_DISCORD][WARN] "
                    + f"failed queue={queue_key} job_id={job_id} err={type(exc).__name__}:{exc}"
                )
    except Exception as exc:
        took = int((_now_s() - t0) * 1000)
        return JobResult(
            job_id=job_id,
            ok=False,
            type=typ,
            symbol=symbol,
            out_path=out_path,
            artifact_url=None,
            meta={
                "alert_id": job.get("alert_id"),
                "intent": job.get("intent"),
                "event": job.get("event"),
                "actions": job.get("actions"),
            },
            error=f"write_failed:{type(exc).__name__}:{exc}",
            took_ms=took,
        )

    took = int((_now_s() - t0) * 1000)
    return JobResult(
        job_id=job_id,
        ok=True,
        type=typ,
        symbol=symbol,
        out_path=out_path,
        artifact_url=_artifact_url_for(job_id, ext="json"),
        meta={
            "alert_id": job.get("alert_id"),
            "intent": job.get("intent"),
            "event": job.get("event"),
            "actions": job.get("actions"),
        },
        error=None,
        took_ms=took,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="TNT Redis worker (Dell-side).")
    p.add_argument("--redis-host", default=os.getenv("TNT_REDIS_HOST", "127.0.0.1"))
    p.add_argument("--redis-port", type=int, default=_env_int("TNT_REDIS_PORT", 6379))
    p.add_argument("--redis-db", type=int, default=_env_int("TNT_REDIS_DB", 0))
    p.add_argument("--queue", default=os.getenv("TNT_REDIS_QUEUE", "tnt:jobs"))
    p.add_argument("--results", default=os.getenv("TNT_REDIS_RESULTS", "tnt:results"))
    p.add_argument("--poll-timeout", type=int, default=_env_int("TNT_REDIS_BLPOP_TIMEOUT", 5))
    p.add_argument("--result-key-prefix", default=os.getenv("TNT_REDIS_RESULT_KEY_PREFIX", "tnt:result:"))
    p.add_argument("--heartbeat-key-prefix", default=os.getenv("TNT_REDIS_HEARTBEAT_KEY_PREFIX", "tnt:hb:"))
    p.add_argument("--result-ttl-sec", type=int, default=_env_int("TNT_REDIS_RESULT_TTL_SEC", 900))
    p.add_argument("--heartbeat-ttl-sec", type=int, default=_env_int("TNT_REDIS_HEARTBEAT_TTL_SEC", 300))

    args = p.parse_args(argv)

    try:
        import redis  # type: ignore
    except Exception as exc:
        print(f"[FATAL] redis not installed: {type(exc).__name__}: {exc}")
        return 2

    r = redis.Redis(host=args.redis_host, port=int(args.redis_port), db=int(args.redis_db), decode_responses=True)

    dq_key = (os.getenv("TNT_ALERTS_DISCORD_QUEUE", "tnt:alerts:discord_queue") or "tnt:alerts:discord_queue").strip()
    dq_on = _truthy_env("TNT_ALERTS_DISCORD_ENQUEUE_ENABLED", "0") or _truthy_env("TNT_ALERTS_DISCORD_DELIVERY_ENABLED", "0")
    print(
        f"[TNT][WORKER] pid={os.getpid()} redis={args.redis_host}:{args.redis_port}/{args.redis_db} "
        f"queue={args.queue} results={args.results} alerts_discord_enqueue={int(bool(dq_on))} alerts_discord_queue={dq_key}"
    )

    try:
        while True:
            item = r.blpop([str(args.queue)], timeout=int(args.poll_timeout))
            if not item:
                continue
            _key, raw = item
            if not isinstance(raw, str) or not raw.strip():
                continue

            job = _parse_job(raw)
            if job is None:
                continue

            job_id = str(job.get("job_id") or "")
            typ = str(job.get("type") or "")
            symbol0 = _safe_upper_symbol(str(job.get("symbol") or ""))

            # Ops: cheap counters for "are we draining?" and heartbeat panels.
            try:
                b = _bucket_5m()
                _incr_bucket(r, key_prefix="tnt:alerts:worker:consumed_bucket:", bucket=b)
                if typ == "alert_trigger":
                    _incr_bucket(r, key_prefix="tnt:alerts:worker:consumed_alert_trigger_bucket:", bucket=b)
            except Exception:
                pass

            hb_key = f"{args.heartbeat_key_prefix}{job_id}" if job_id else ""
            if job_id and hb_key:
                _set_hb(r, key=hb_key, ttl_sec=int(args.heartbeat_ttl_sec), message=f"{job_id}: dequeued")

            if typ == "alert_trigger":
                try:
                    print(
                        "[TNT][WORKER][POP] "
                        + f"type={typ} job_id={job_id} alert_id={job.get('alert_id')} symbol={symbol0} ts_utc={job.get('ts_utc')}"
                    )
                except Exception:
                    pass

            if typ == "render_oi":
                res = _render_oi_job(job, r=r, hb_key=hb_key or None, hb_ttl_sec=int(args.heartbeat_ttl_sec))
            elif typ == "alert_trigger":
                res = _alert_trigger_job(job, r=r, hb_key=hb_key or None, hb_ttl_sec=int(args.heartbeat_ttl_sec))
            else:
                res = JobResult(
                    job_id=str(job.get("job_id") or ""),
                    ok=False,
                    type=typ,
                    symbol=symbol0,
                    out_path=None,
                    artifact_url=None,
                    meta=None,
                    error=f"unsupported_type:{typ}",
                    took_ms=0,
                )

            # Ops: completion counters (best-effort).
            try:
                b2 = _bucket_5m()
                _incr_bucket(r, key_prefix="tnt:alerts:worker:completed_bucket:", bucket=b2)
                if res.type == "alert_trigger":
                    _incr_bucket(r, key_prefix="tnt:alerts:worker:completed_alert_trigger_bucket:", bucket=b2)
            except Exception:
                pass

            # Per-job result key (primary API for enqueue_and_wait)
            try:
                if res.job_id:
                    result_key = f"{args.result_key_prefix}{res.job_id}"
                    getattr(r, "setex")(result_key, int(args.result_ttl_sec), res.to_json())
            except Exception:
                pass

            # Best-effort terminal heartbeat update
            try:
                if res.job_id and hb_key:
                    _set_hb(r, key=hb_key, ttl_sec=int(args.heartbeat_ttl_sec), message=f"{res.job_id}: done ({'ok' if res.ok else 'err'})")
            except Exception:
                pass

            try:
                r.rpush(str(args.results), res.to_json())
            except Exception:
                pass

            status = "OK" if res.ok else "ERR"
            print(f"[JOB {status}] type={res.type} symbol={res.symbol} job_id={res.job_id} took_ms={res.took_ms} out={res.out_path or '-'} err={res.error or '-'}")

    except KeyboardInterrupt:
        print("[STOP] Keyboard interrupt")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
