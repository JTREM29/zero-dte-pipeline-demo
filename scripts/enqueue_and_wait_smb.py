from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


# Ensure repo root is importable when running from `scripts/`.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

POLL_SECONDS = 0.25


def _default_jobs_dir() -> str:
    return os.getenv("TNT_SMB_JOBS_DIR", r"\\TNT2\tnt_jobs")


def _default_results_dir() -> str:
    return os.getenv("TNT_SMB_RESULTS_DIR", r"\\TNT2\tnt_results")


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


@dataclass(frozen=True)
class WaitOutcome:
    job_id: str
    status: str
    result: Dict[str, Any]
    heartbeat_text: Optional[str]


def enqueue_and_wait_smb(
    *,
    job_id: Optional[str] = None,
    symbol: str,
    job_type: str,
    payload: Optional[Dict[str, Any]] = None,
    jobs_dir: Optional[str] = None,
    results_dir: Optional[str] = None,
    timeout_s: float = 120.0,
) -> WaitOutcome:
    # Import locally to keep Controller dependencies minimal.
    from smb_enqueue_job import enqueue_job  # repo-root module

    jd = (jobs_dir or _default_jobs_dir() or "").strip()
    rd = (results_dir or _default_results_dir() or "").strip()

    out = enqueue_job(job_id=job_id, symbol=symbol, job_type=job_type, params=payload or {}, jobs_dir=jd, results_dir=rd)
    job_id = out.job_id

    # Some workers write `<job_id>.result.json`, others write `<job_id>.job.result.json`.
    result_paths = [
        str(Path(rd) / f"{job_id}.result.json"),
        str(Path(rd) / f"{job_id}.job.result.json"),
    ]

    # Same story for heartbeat.
    hb_paths = [
        str(Path(rd) / f"{job_id}.heartbeat.txt"),
        str(Path(rd) / f"{job_id}.job.heartbeat.txt"),
    ]

    deadline = time.time() + float(timeout_s)
    last_hb: Optional[str] = None

    while True:
        hit_result = None
        for rp in result_paths:
            if Path(rp).exists():
                hit_result = rp
                break

        if hit_result:
            raw = _read_text(hit_result)
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {"raw": raw}
            status = str(parsed.get("status") or ("succeeded" if bool(parsed.get("ok")) else "failed"))
            return WaitOutcome(job_id=job_id, status=status, result=parsed if isinstance(parsed, dict) else {"result": parsed}, heartbeat_text=last_hb)

        hb_text = None
        for hp in hb_paths:
            if Path(hp).exists():
                try:
                    hb_text = _read_text(hp).strip()
                except Exception:
                    hb_text = ""
                if hb_text:
                    break

        if hb_text and hb_text != last_hb:
            last_hb = hb_text
            try:
                print(f"{job_id}: heartbeat {last_hb}")
            except Exception:
                pass

        if time.time() > deadline:
            raise TimeoutError(f"{job_id}: timeout after {timeout_s:.1f}s (last heartbeat={last_hb!r})")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    import sys

    # Usage:
    #   py -u scripts/enqueue_and_wait_smb.py SPY render_oi 18 130 60
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    job_type = sys.argv[2] if len(sys.argv) > 2 else "render_oi"
    top = int(sys.argv[3]) if len(sys.argv) > 3 else 18
    dpi = int(sys.argv[4]) if len(sys.argv) > 4 else 130
    timeout_s = float(sys.argv[5]) if len(sys.argv) > 5 else 30.0

    out = enqueue_and_wait_smb(
        symbol=symbol,
        job_type=job_type,
        payload={"top": top, "dpi": dpi},
        timeout_s=timeout_s,
    )
    print(out)
