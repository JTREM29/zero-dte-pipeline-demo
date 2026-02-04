import json
import os
import secrets
from datetime import datetime, timezone

JOBS_DIR = r"\\TNT2\tnt_jobs"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_job_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{secrets.token_hex(3).upper()}"


def atomic_write_json(path_final: str, payload: dict) -> None:
    tmp = path_final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path_final)


def main():
    os.makedirs(JOBS_DIR, exist_ok=True)
    job_id = make_job_id()

    payload = {
        "job_id": job_id,
        "type": "smoke",
        "created_at": utc_now_iso(),
        "inputs": {"note": "hello from controller"},
    }

    out_path = os.path.join(JOBS_DIR, f"{job_id}.json")
    atomic_write_json(out_path, payload)

    print(f"Wrote job: {out_path}")


if __name__ == "__main__":
    main()
