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
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from delivery import discord_bot as delivery
from delivery.oi_iv_render import bucket_oi_iv_by_strike, render_oi_iv_png


def _now_s() -> float:
    return float(time.time())


def _safe_upper_symbol(value: str) -> str:
    return (value or "").strip().upper()


def _file_age_s(path: Path) -> float | None:
    try:
        st = path.stat()
    except Exception:
        return None
    return max(0.0, _now_s() - float(st.st_mtime))


def _hash_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest().upper()

def _artifact_basename(job_id: str) -> str:
    """Filesystem-safe basename for artifacts derived from job_id."""

    raw = str(job_id or "")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", raw)
    safe = re.sub(r"\s+", "_", safe)
    safe = re.sub(r"_+", "_", safe)
    safe = safe.strip(" ._-")
    if not safe:
        safe = "job"
    if safe != raw:
        safe = f"{safe}_{_hash_key(raw)[:10]}"
    return safe[:180]


def _session_now() -> str:
    try:
        return str(delivery.market_session_et(datetime.now(timezone.utc)))
    except Exception:
        return "UNKNOWN"


def _oi_data_ttl_sec(session: str) -> int:
    # Short during market hours; longer after-hours.
    if session in {"RTH", "PRE"}:
        return max(10, _env_int("TNT_OI_DATA_TTL_RTH_SEC", 90))
    return max(30, _env_int("TNT_OI_DATA_TTL_OFF_SEC", 900))


def _oi_png_ttl_sec(session: str) -> int:
    # PNG can live longer than data; SWR on controller can revalidate.
    if session in {"RTH", "PRE"}:
        return max(30, _env_int("TNT_OI_PNG_TTL_RTH_SEC", 900))
    return max(60, _env_int("TNT_OI_PNG_TTL_OFF_SEC", 1800))


def _oi_data_cache_path(results_dir: str, data_hash: str) -> Path:
    return Path(results_dir) / ".cache" / "oi_data" / f"{data_hash}.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _default_jobs_dir() -> str:
    return os.getenv("TNT_SMB_JOBS_DIR", r"\\TNT2\tnt_jobs")


def _default_results_dir() -> str:
    return os.getenv("TNT_SMB_RESULTS_DIR", r"\\TNT2\tnt_results")


def _write_text(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def _set_hb(results_dir: str, job_id: str, message: str) -> None:
    try:
        safe_id = _artifact_basename(job_id)
        hb_path = str(Path(results_dir) / f"{safe_id}.heartbeat.txt")
        _write_text(hb_path, str(message))
    except Exception:
        return


def _set_worker_hb(results_dir: str, message: str) -> None:
    """Write a global worker heartbeat so controllers can detect liveness even when idle."""
    try:
        hb_path = str(Path(results_dir) / "tnt_worker.heartbeat.txt")
        _write_text(hb_path, str(message))
    except Exception:
        return


def _parse_job_path(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


@dataclass(frozen=True)
class JobResult:
    job_id: str
    ok: bool
    type: str
    symbol: str
    out_path: str | None
    error: str | None
    took_ms: int
    meta: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        status = "succeeded" if bool(self.ok) else "failed"
        artifacts: dict[str, Any] = {}
        if self.out_path:
            try:
                ext = str(Path(self.out_path).suffix or "").lower()
            except Exception:
                ext = ""
            if ext == ".json":
                artifacts = {"json": self.out_path}
            else:
                artifacts = {"png": self.out_path}

        return {
            "job_id": self.job_id,
            "status": status,
            "ok": bool(self.ok),
            "type": self.type,
            "symbol": self.symbol,
            "out_path": self.out_path,
            "artifacts": artifacts,
            "error": self.error,
            "took_ms": int(self.took_ms),
            "meta": self.meta or {},
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "worker_pid": int(os.getpid()),
            "worker_host": os.getenv("COMPUTERNAME", ""),
        }


def _alert_trigger_job(job: dict[str, Any], *, results_dir: str) -> JobResult:
    t0 = _now_s()

    job_id = str(job.get("job_id") or str(uuid.uuid4()))
    typ = str(job.get("type") or "")
    symbol = _safe_upper_symbol(str(job.get("symbol") or ""))

    _set_hb(results_dir, job_id, f"{job_id}: started")

    out_path = str(Path(results_dir) / f"{job_id}.json")
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
        "worker_ts_utc": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _set_hb(results_dir, job_id, f"{job_id}: writing json")
        Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=out_path, error=f"write_failed:{type(exc).__name__}:{exc}", took_ms=took)

    took = int((_now_s() - t0) * 1000)
    return JobResult(job_id=job_id, ok=True, type=typ, symbol=symbol, out_path=out_path, error=None, took_ms=took)


def _render_oi_job(job: dict[str, Any], *, results_dir: str) -> JobResult:
    t0 = _now_s()

    job_id = str(job.get("job_id") or str(uuid.uuid4()))
    typ = str(job.get("type") or "")
    symbol = _safe_upper_symbol(str(job.get("symbol") or ""))

    _set_hb(results_dir, job_id, f"{job_id}: started")

    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    expiry = str(params.get("expiry") or "").strip() or None
    top_n = int(params.get("top") or 18)
    dpi = int(params.get("dpi") or 130)
    window_pct = None
    max_contracts = None
    try:
        wp = params.get("window_pct")
        window_pct = int(wp) if wp is not None else None
    except Exception:
        window_pct = None
    try:
        mc = params.get("max_contracts")
        max_contracts = int(mc) if mc is not None else None
    except Exception:
        max_contracts = None

    window_pct = 7 if window_pct is None else max(5, min(12, int(window_pct)))
    max_contracts = 250 if max_contracts is None else max(50, min(1000, int(max_contracts)))
    strike_window_pct = float(window_pct) / 100.0

    session = _session_now()
    data_ttl = int(_oi_data_ttl_sec(session))
    png_ttl = int(_oi_png_ttl_sec(session))

    # Deterministic data cache key (does not include dpi/top).
    data_key = f"v1|{symbol}|{expiry or 'auto'}|win{window_pct}|max{max_contracts}"
    data_hash = _hash_key(data_key)[:16]
    data_path = _oi_data_cache_path(results_dir, data_hash)

    # Deterministic render cache key (job_id may already be a hash provided by controller).
    render_key = f"{data_key}|top{int(top_n)}|dpi{int(dpi)}|iv1"
    render_hash = _hash_key(render_key)[:16]

    artifact_id = _artifact_basename(job_id)
    out_path = str(Path(results_dir) / f"{artifact_id}.png")

    # Fast path: PNG cache hit.
    try:
        out_p = Path(out_path)
        png_age = _file_age_s(out_p)
        if png_age is not None and png_age <= float(png_ttl):
            took = int((_now_s() - t0) * 1000)
            return JobResult(
                job_id=job_id,
                ok=True,
                type=typ,
                symbol=symbol,
                out_path=out_path,
                error=None,
                took_ms=took,
                meta={
                    "cache": {
                        "png": "HIT",
                        "png_age_s": float(png_age),
                        "png_ttl_s": int(png_ttl),
                        "data": "UNKNOWN",
                    },
                    "keys": {"render": str(render_hash), "data": str(data_hash)},
                    "session": session,
                },
            )
    except Exception:
        pass

    if typ != "render_oi":
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, error=f"unsupported_type:{typ}", took_ms=took)

    if not symbol:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, error="missing_symbol", took_ms=took)

    # Data cache (JSON) split from render cache.
    strikes: list[float] = []
    oi_calls: list[float] = []
    oi_puts: list[float] = []
    iv_pct: list[float] = []
    exp_used: str | None = expiry
    data_cache_hit = False
    data_age = None
    try:
        data_age = _file_age_s(data_path)
        if data_age is not None and data_age <= float(data_ttl):
            raw = data_path.read_text(encoding="utf-8")
            obj = json.loads(raw)
            if isinstance(obj, dict):
                exp_used = str(obj.get("expiry_used") or exp_used or "").strip() or exp_used
                strikes = [float(x) for x in (obj.get("strikes") or [])]
                oi_calls = [float(x) for x in (obj.get("oi_calls") or [])]
                oi_puts = [float(x) for x in (obj.get("oi_puts") or [])]
                iv_pct = [float(x) for x in (obj.get("iv_pct") or [])]
                if strikes and len(strikes) == len(oi_calls) == len(oi_puts):
                    data_cache_hit = True
    except Exception:
        data_cache_hit = False

    if not data_cache_hit:
        try:
            _set_hb(results_dir, job_id, f"{job_id}: fetching chain")
            df = asyncio.run(
                delivery._fetch_polygon_options_chain_df(
                    symbol,
                    expiration_ymd=expiry,
                    strike_window_pct=float(strike_window_pct),
                    max_contracts=int(max_contracts),
                    concurrency=8,
                )
            )
        except Exception as exc:
            took = int((_now_s() - t0) * 1000)
            return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, error=f"fetch_failed:{type(exc).__name__}:{exc}", took_ms=took)
        if df is None or getattr(df, "empty", True):
            took = int((_now_s() - t0) * 1000)
            return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, error="no_data", took_ms=took)

        strikes, oi_calls, oi_puts, iv_pct = bucket_oi_iv_by_strike(df, sym=symbol)
        if not strikes:
            took = int((_now_s() - t0) * 1000)
            return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, error="no_strikes", took_ms=took)

        # Best-effort: infer exp from the data if present.
        try:
            exp_col = None
            if hasattr(df, "get"):
                exp_col = df.get("expiration")
            if exp_col is not None and len(exp_col) > 0:
                exp_used = str(exp_col.iloc[0]).strip() or exp_used
        except Exception:
            pass

        try:
            data_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            payload = {
                "version": 1,
                "symbol": symbol,
                "expiry_used": exp_used,
                "window_pct": int(window_pct),
                "max_contracts": int(max_contracts),
                "computed_utc": datetime.now(timezone.utc).isoformat(),
                "session": session,
                "strikes": strikes,
                "oi_calls": oi_calls,
                "oi_puts": oi_puts,
                "iv_pct": iv_pct,
            }
            tmp = data_path.with_suffix(data_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(data_path))
        except Exception:
            pass

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

    _set_hb(results_dir, job_id, f"{job_id}: rendering png")

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
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=None, error="render_failed", took_ms=took)

    try:
        Path(results_dir).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        _set_hb(results_dir, job_id, f"{job_id}: writing artifact")
        Path(out_path).write_bytes(png)
    except Exception as exc:
        took = int((_now_s() - t0) * 1000)
        return JobResult(job_id=job_id, ok=False, type=typ, symbol=symbol, out_path=out_path, error=f"write_failed:{type(exc).__name__}:{exc}", took_ms=took)

    took = int((_now_s() - t0) * 1000)
    # Recompute ages for reporting.
    try:
        png_age2 = _file_age_s(Path(out_path))
    except Exception:
        png_age2 = None
    try:
        data_age2 = _file_age_s(data_path)
    except Exception:
        data_age2 = None

    return JobResult(
        job_id=job_id,
        ok=True,
        type=typ,
        symbol=symbol,
        out_path=out_path,
        error=None,
        took_ms=took,
        meta={
            "cache": {
                "png": "MISS",
                "png_age_s": float(png_age2) if png_age2 is not None else None,
                "png_ttl_s": int(png_ttl),
                "data": "HIT" if data_cache_hit else "MISS",
                "data_age_s": float(data_age2) if data_age2 is not None else None,
                "data_ttl_s": int(data_ttl),
            },
            "keys": {"render": str(render_hash), "data": str(data_hash)},
            "params": {"expiry": exp_used, "top": int(top_n), "dpi": int(dpi), "window_pct": int(window_pct), "max_contracts": int(max_contracts)},
            "session": session,
        },
    )


def _try_claim_job(job_path: Path) -> Path | None:
    # Atomically rename to avoid double-processing.
    claimed = job_path.with_suffix(job_path.suffix + ".claimed")
    try:
        os.replace(str(job_path), str(claimed))
        return claimed
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="TNT SMB job-folder worker (Dell-side).")
    p.add_argument("--jobs-dir", default=_default_jobs_dir())
    p.add_argument("--results-dir", default=_default_results_dir())
    p.add_argument("--poll-sec", type=float, default=float(os.getenv("TNT_SMB_POLL_SEC", "0.5")))

    args = p.parse_args(argv)

    jobs_dir = str(args.jobs_dir)
    results_dir = str(args.results_dir)

    print(f"[TNT][SMB_WORKER] pid={os.getpid()} jobs={jobs_dir} results={results_dir}")

    try:
        Path(jobs_dir).mkdir(parents=True, exist_ok=True)
        Path(results_dir).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"[FATAL] Cannot access jobs/results dirs: {type(exc).__name__}: {exc}")
        return 2

    hb_every_s = max(1.0, float(os.getenv("TNT_SMB_WORKER_HEARTBEAT_SEC", "10")))
    last_hb_s = 0.0

    while True:
        now_s = _now_s()
        if (now_s - last_hb_s) >= hb_every_s:
            try:
                _set_worker_hb(
                    results_dir,
                    f"worker_alive pid={os.getpid()} host={os.getenv('COMPUTERNAME','')} ts_utc={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
                )
            except Exception:
                pass
            last_hb_s = now_s

        try:
            job_files = sorted(Path(jobs_dir).glob("*.job.json"), key=lambda p: p.stat().st_mtime)
        except Exception:
            job_files = []

        if not job_files:
            time.sleep(float(args.poll_sec))
            continue

        for job_path in job_files:
            claimed = _try_claim_job(job_path)
            if claimed is None:
                continue

            job = _parse_job_path(claimed)
            if job is None:
                try:
                    claimed.unlink(missing_ok=True)
                except Exception:
                    pass
                continue

            job_id = str(job.get("job_id") or "")
            if job_id:
                _set_hb(results_dir, job_id, f"{job_id}: dequeued")

            typ = str(job.get("type") or "")
            if typ == "render_oi":
                res = _render_oi_job(job, results_dir=results_dir)
            elif typ == "alert_trigger":
                res = _alert_trigger_job(job, results_dir=results_dir)
            else:
                res = JobResult(
                    job_id=str(job.get("job_id") or ""),
                    ok=False,
                    type=typ,
                    symbol=_safe_upper_symbol(str(job.get("symbol") or "")),
                    out_path=None,
                    error=f"unsupported_type:{typ}",
                    took_ms=0,
                )
            payload = res.to_payload()

            safe_res_id = _artifact_basename(res.job_id)
            result_path = str(Path(results_dir) / f"{safe_res_id}.result.json")
            tmp_path = result_path + ".tmp"

            try:
                _set_hb(results_dir, res.job_id, f"{res.job_id}: writing result")
                _write_text(tmp_path, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
                os.replace(tmp_path, result_path)
            except Exception:
                pass

            try:
                _set_hb(results_dir, res.job_id, f"{res.job_id}: done ({'ok' if res.ok else 'err'})")
            except Exception:
                pass

            try:
                claimed.unlink(missing_ok=True)
            except Exception:
                pass

        time.sleep(float(args.poll_sec))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
