import json
import os
import time
from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

JOBS_DIR = r"\\TNT2\tnt_jobs"
RESULTS_DIR = r"\\TNT2\tnt_results"

POLL_SECONDS = 1.0
HEARTBEAT_EVERY_SECONDS = 1.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def atomic_write_json(path_final: str, payload: dict) -> None:
    tmp = path_final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path_final)


def process_job(job_path: str) -> None:
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)

    job_id = job.get("job_id", "unknown")
    start = time.time()

    hb_path = os.path.join(RESULTS_DIR, f"{job_id}.heartbeat")
    ok_path = os.path.join(RESULTS_DIR, f"{job_id}__ok.txt")
    result_path = os.path.join(RESULTS_DIR, f"{job_id}.result.json")

    last_hb = 0.0
    try:
        # Simulate work (replace this block with real rendering later)
        work_seconds = 3.0
        while (time.time() - start) < work_seconds:
            now = time.time()
            if now - last_hb >= HEARTBEAT_EVERY_SECONDS:
                write_text(hb_path, utc_now_iso())
                last_hb = now
            time.sleep(0.1)

        duration = round(time.time() - start, 3)

        # Tiny deterministic summary to pair with PNG (placeholder image omitted in this worker)
        payload = job.get("inputs") or job
        sym = str((payload.get("symbol") if isinstance(payload, dict) else "") or "SPY").upper()
        interval = str((payload.get("interval") if isinstance(payload, dict) else "1m") or "1m")
        days = int((payload.get("days") if isinstance(payload, dict) else 1) or 1)
        top = int((payload.get("top") if isinstance(payload, dict) else 18) or 18)
        title = f"SMOKE — {sym} | {interval} | {days}d | top={top}"
        try:
            ts_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z") if ZoneInfo else datetime.now().isoformat()
        except Exception:
            ts_et = datetime.now().isoformat()
        # Optional summary overrides
        s = payload.get("summary") if isinstance(payload, dict) else None
        cache_hint = "MISS"
        dedupe_hint = "n/a"
        regime_line = "Regime: TRANSITION (conf 0.50) | Confirm: NEUTRAL"
        data_age = "n/a"
        feed = "polygon"
        if isinstance(s, dict):
            if s.get("as_of_et"):
                ts_et = str(s.get("as_of_et"))
            if s.get("data_age_min") is not None:
                try:
                    data_age = f"{float(s.get('data_age_min')):.1f}m"
                except Exception:
                    pass
            feed = str(s.get("feed") or feed)
            if s.get("regime"):
                regime_line = f"Regime: {str(s.get('regime')).upper()} (conf {float(s.get('confidence') or 0):.2f}) | Confirm: {str(s.get('confirm') or 'NEUTRAL').upper()}"
            cache_hint = str(s.get("cache") or cache_hint).upper()
            dedupe_hint = str(s.get("dedupe") or dedupe_hint).upper()
        write_text(os.path.join(RESULTS_DIR, f"{job_id}__summary.txt"), "\n".join([
            title,
            f"As of (ET): {ts_et} | Data age: {data_age} | Feed: {feed}",
            regime_line,
            f"Dedupe: {dedupe_hint}",
            f"Cache: {cache_hint}",
            f"Render: {duration:.1f}s | Job: {job_id}",
        ]) + "\n")

        atomic_write_json(result_path, {
            "job_id": job_id,
            "status": "succeeded",
            "duration_s": duration,
            "completed_at": utc_now_iso(),
        })
        write_text(ok_path, "ok\n")
        print(f"{job_id}: succeeded")

    except Exception as e:
        duration = round(time.time() - start, 3)
        atomic_write_json(result_path, {
            "job_id": job_id,
            "status": "failed",
            "duration_s": duration,
            "error": repr(e),
            "completed_at": utc_now_iso(),
        })
        print(f"{job_id}: failed: {e}")

    finally:
        # Remove job file only after result is written
        try:
            os.remove(job_path)
        except Exception:
            pass


def main():
    os.makedirs(JOBS_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    print("SMB worker online.")
    while True:
        try:
            jobs = [f for f in os.listdir(JOBS_DIR) if f.lower().endswith(".json")]
            for fname in jobs:
                process_job(os.path.join(JOBS_DIR, fname))
        except Exception as e:
            print(f"worker error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
