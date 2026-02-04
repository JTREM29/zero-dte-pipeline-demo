import os, json, uuid, datetime as dt

# Prefer mapped drives if provided (works around some UNC quirks with Python on Windows)
_JDRV = os.environ.get("TNT2_JOBS_DRIVE")   # e.g., "Z:"
_RDRV = os.environ.get("TNT2_RESULTS_DRIVE")  # e.g., "Y:"
if _JDRV and _RDRV:
    JOBS = f"{_JDRV}\\"  # mapped drive to \\TNT2\tnt_jobs
    RESULTS = f"{_RDRV}\\"  # mapped drive to \\TNT2\tnt_results
else:
    # Resolve Dell host from env for flexibility (supports hostname or IP)
    _HOST = os.environ.get("TNT2_HOST", "TNT2")
    JOBS = rf"\\\\{_HOST}\tnt_jobs"  # UNC to Dell
    RESULTS = rf"\\\\{_HOST}\tnt_results"


def _atomic_write_json(path_final: str, obj: dict):
    tmp = path_final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path_final)  # atomic on Windows


def push_job(job_type: str, payload: dict) -> str:
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "type": job_type,
        "payload": payload,
        "created_at": dt.datetime.utcnow().isoformat() + "Z",
        "attempt": 1,
    }
    final = os.path.join(JOBS, f"{job_id}.json")
    _atomic_write_json(final, job)
    print(f"Wrote job: {final}")
    return job_id


if __name__ == "__main__":
    # smoke test
    push_job("render_oi", {"symbol": "SPY"})
