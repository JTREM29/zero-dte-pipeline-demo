import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple, Dict, Any, List

from controller.worker_pool import build_default_targets, choose_worker

# Resolve SMB locations

def resolve_paths() -> Tuple[str, str, str, str]:
    """Return (jobs_dir, results_dir, routed_name, reason)."""

    primary, secondary = build_default_targets()
    decision = choose_worker(primary=primary, secondary=secondary)
    return str(decision.target.jobs_dir), str(decision.target.results_dir), str(decision.routed_name), str(decision.reason)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_job_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{secrets.token_hex(3).upper()}"


def atomic_write_json(path_final: str, payload: Dict[str, Any]) -> None:
    tmp = path_final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path_final)


def enqueue_job(job_type: str = "smoke", inputs: Dict[str, Any] | None = None) -> Tuple[str, str, str, str, str]:
    jobs_dir, results_dir, routed_name, routed_reason = resolve_paths()
    # Avoid makedirs on UNC share roots (\\server\share) which can raise WinError 161
    if not jobs_dir.startswith("\\\\"):
        os.makedirs(jobs_dir, exist_ok=True)
    job_id = make_job_id()
    payload = {
        "job_id": job_id,
        "type": job_type,
        "created_at": utc_now_iso(),
        "inputs": inputs or {"note": "hello from controller"},
    }

    # Controller-side routing stamp (helps with postmortems + Discord UX).
    try:
        payload["routing"] = {"target": str(routed_name), "reason": str(routed_reason)}
    except Exception:
        pass
    out_path = os.path.join(jobs_dir, f"{job_id}.json")
    atomic_write_json(out_path, payload)
    print(f"Wrote job: {out_path} routed={routed_name} reason={routed_reason}")
    return job_id, jobs_dir, results_dir, routed_name, routed_reason


def list_artifacts(results_dir: str, job_id: str) -> List[str]:
    p = Path(results_dir)
    pref = f"{job_id}*"
    return sorted(str(child) for child in p.glob(pref))


def post_discord_webhook(message: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        import urllib.request
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as _:
            pass
    except Exception as e:
        print(f"webhook error: {e}")


def wait_for_result(results_dir: str, job_id: str, timeout_s: int = 120) -> int:
    result_path = os.path.join(results_dir, f"{job_id}.result.json")
    hb_path = os.path.join(results_dir, f"{job_id}.heartbeat")

    start = time.time()
    last_status = "waiting"
    while True:
        if os.path.exists(result_path):
            with open(result_path, "r", encoding="utf-8") as f:
                res = json.load(f)
            status = res.get("status", "unknown")
            artifacts = list_artifacts(results_dir, job_id)
            print("Result:", json.dumps(res, indent=2))
            print("Artifacts:")
            for a in artifacts:
                print(" -", a)
            post_discord_webhook(f"Job {job_id} {status}. Artifacts: \n" + "\n".join(artifacts))
            return 0 if status == "succeeded" else 1

        if os.path.exists(hb_path):
            try:
                hb = Path(hb_path).read_text(encoding="utf-8").strip()
                if hb != last_status:
                    print(f"heartbeat: {hb}")
                    last_status = hb
            except Exception:
                pass

        if time.time() - start > timeout_s:
            print(f"Timeout waiting for {result_path}")
            post_discord_webhook(f"Job {job_id} timed out after {timeout_s}s")
            return 2

        time.sleep(0.5)


def main():
    job_id, jobs_dir, results_dir, _routed_name, _routed_reason = enqueue_job()
    rc = wait_for_result(results_dir, job_id)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
