from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_job_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3).upper()}"


def _safe_upper_symbol(value: str) -> str:
    return (value or "").strip().upper()


def _default_jobs_dir() -> str:
    # Matches your checklist.
    return os.getenv("TNT_SMB_JOBS_DIR", r"\\TNT2\tnt_jobs")


def _default_results_dir() -> str:
    return os.getenv("TNT_SMB_RESULTS_DIR", r"\\TNT2\tnt_results")


def _write_text(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


@dataclass(frozen=True)
class EnqueueOutcome:
    job_id: str
    job_path: str
    results_dir: str


def enqueue_job(
    *,
    job_id: str | None = None,
    symbol: str,
    job_type: str = "render_oi",
    params: Optional[Dict[str, Any]] = None,
    jobs_dir: Optional[str] = None,
    results_dir: Optional[str] = None,
) -> EnqueueOutcome:
    sym = _safe_upper_symbol(symbol)
    if not sym:
        raise ValueError("symbol is required")

    jd = (jobs_dir or _default_jobs_dir() or "").strip()
    if not jd:
        raise ValueError("jobs_dir is required (or set TNT_SMB_JOBS_DIR)")

    rd = (results_dir or _default_results_dir() or "").strip()
    if not rd:
        raise ValueError("results_dir is required (or set TNT_SMB_RESULTS_DIR)")

    job_id_override = job_id
    job_id = (str(job_id_override).strip() if job_id_override is not None else "")
    if not job_id:
        job_id = make_job_id()
    job = {
        "job_id": job_id,
        "type": str(job_type),
        "symbol": sym,
        "params": params or {},
        "reply": {"mode": "smb_job_folders", "results_dir": rd},
        "created_at": utc_now_iso(),
    }

    # Ensure the folder is reachable; for UNC shares this will fail fast if creds/share missing.
    Path(jd).mkdir(parents=True, exist_ok=True)

    final_path = str(Path(jd) / f"{job_id}.job.json")

    # If the caller provided a deterministic job id, avoid duplicate enqueues by
    # creating the job file exclusively. If it already exists, treat it as a join.
    if job_id_override is not None and str(job_id_override).strip():
        try:
            fd = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(job, separators=(",", ":"), ensure_ascii=False))
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise
        except FileExistsError:
            pass
    else:
        tmp_path = str(Path(jd) / f"{job_id}.job.json.tmp")
        _write_text(tmp_path, json.dumps(job, separators=(",", ":"), ensure_ascii=False))
        os.replace(tmp_path, final_path)

    return EnqueueOutcome(job_id=job_id, job_path=final_path, results_dir=rd)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Enqueue a TNT render job via SMB job folders (Controller-side).")
    p.add_argument("--symbol", default=os.getenv("TNT_OI_SYMBOL", "SPY"))
    p.add_argument("--type", default="render_oi", choices=["render_oi"])
    p.add_argument("--jobs-dir", default=_default_jobs_dir())
    p.add_argument("--results-dir", default=_default_results_dir())
    p.add_argument("--expiry", default=os.getenv("TNT_OI_EXPIRY", ""), help="YYYY-MM-DD (optional)")
    p.add_argument("--top", type=int, default=int(os.getenv("TNT_OI_TOP", "18")))
    p.add_argument("--dpi", type=int, default=int(os.getenv("TNT_OI_DPI", "130")))

    args = p.parse_args(argv)

    expiry = (str(args.expiry).strip() or None)
    params = {"expiry": expiry, "top": int(args.top), "dpi": int(args.dpi)}

    try:
        out = enqueue_job(
            symbol=str(args.symbol),
            job_type=str(args.type),
            params=params,
            jobs_dir=str(args.jobs_dir),
            results_dir=str(args.results_dir),
        )
    except Exception as exc:
        print(f"[FATAL] {type(exc).__name__}: {exc}")
        return 2

    # Match your checklist’s expectation.
    print(f"Wrote job: {out.job_path}")
    print(f"Job ID: {out.job_id}")
    print(f"Results dir: {out.results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
