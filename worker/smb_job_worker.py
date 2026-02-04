import os, json, time, traceback, datetime as dt
from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Prefer mapped drives if provided
_JDRV = os.environ.get("TNT2_JOBS_DRIVE")   # e.g., "Z:"
_RDRV = os.environ.get("TNT2_RESULTS_DRIVE")  # e.g., "Y:"
if _JDRV and _RDRV:
    UNC_JOBS = f"{_JDRV}\\"
    UNC_RESULTS = f"{_RDRV}\\"
else:
    # Resolve Dell host from env for flexibility (supports hostname or IP)
    _HOST = os.environ.get("TNT2_HOST", "TNT2")
    UNC_JOBS = rf"\\\\{_HOST}\tnt_jobs"
    UNC_RESULTS = rf"\\\\{_HOST}\tnt_results"


def _atomic_write_json(path_final: str, obj: dict):
    tmp = path_final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path_final)


def _heartbeat(job_id: str):
    hb = {"job_id": job_id, "ts": dt.datetime.utcnow().isoformat() + "Z"}
    _atomic_write_json(os.path.join(UNC_RESULTS, f"{job_id}.heartbeat"), hb)


def _process(job_path: str):
    job_name = os.path.basename(job_path)
    job_id, _ = os.path.splitext(job_name)
    lock_path = os.path.join(UNC_JOBS, f"{job_id}.processing.json")

    # try to lock by rename; if it fails, someone else has it
    try:
        os.replace(job_path, lock_path)
    except OSError:
        return False

    started = time.time()
    status = "succeeded"
    err = None
    art_path = None
    summary_path = None

    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            job = json.load(f)

        # heartbeat start
        _heartbeat(job_id)

        # Minimal placeholder artifact (kept for traceability)
        art_path = os.path.join(UNC_RESULTS, f"{job_id}__ok.txt")
        with open(art_path, "w", encoding="utf-8") as af:
            af.write(f"OK {job.get('type')} {job.get('payload')}")

        # Tiny deterministic summary.txt (<=8 KB, 6–10 lines)
        jtype = str(job.get("type") or "render").lower()
        payload = job.get("payload") or job.get("inputs") or {}
        sym = str((payload.get("symbol") if isinstance(payload, dict) else "SPY") or "SPY").upper()
        interval = str((payload.get("interval") if isinstance(payload, dict) else "1m") or "1m")
        days = int((payload.get("days") if isinstance(payload, dict) else 1) or 1)
        top = int((payload.get("top") if isinstance(payload, dict) else 18) or 18)
        type_hdr = jtype.replace("render_", "").upper()
        title = f"TNT RENDER — {type_hdr}"
        try:
            tz = ZoneInfo("America/New_York") if ZoneInfo else None
            ts_et = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z") if tz else time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_et = time.strftime("%Y-%m-%d %H:%M:%S")
        data_age = "n/a"
        feed = "polygon"
        eligibility = "STAND_DOWN"
        reason = "LOW_CONF"
        # Optional summary overrides from payload
        try:
            s = payload.get("summary") if isinstance(payload, dict) else None
            if isinstance(s, dict):
                as_of_et = str(s.get("as_of_et") or ts_et)
                ts_et = as_of_et
                if s.get("data_age_min") is not None:
                    try:
                        data_age = f"{float(s.get('data_age_min')):.1f}m"
                    except Exception:
                        pass
                feed = str(s.get("feed") or feed)
                if s.get("regime"):
                    regime_line = f"Regime: {str(s.get('regime')).upper()} (conf {float(s.get('confidence') or 0):.2f}) | Confirm: {str(s.get('confirm') or 'NEUTRAL').upper()}"
                else:
                    regime_line = "Regime: TRANSITION (conf 0.50) | Confirm: NEUTRAL"
                eligibility = str(s.get("eligibility") or eligibility)
                reason = str(s.get("reason_token") or reason)
                cache_hint = str(s.get("cache") or "MISS").upper()
                dedupe_hint = str(s.get("dedupe") or "n/a").upper()
            else:
                regime_line = "Regime: TRANSITION (conf 0.50) | Confirm: NEUTRAL"
                cache_hint = "MISS"
                dedupe_hint = "n/a"
        except Exception:
            regime_line = "Regime: TRANSITION (conf 0.50) | Confirm: NEUTRAL"
            cache_hint = "MISS"
            dedupe_hint = "n/a"
        summary_lines = [
            title,
            f"Symbol: {sym} | Interval: {interval} | Window: {days}d | Top: {top}",
            f"As of (ET): {ts_et} | Data age: {data_age} | Feed: {feed}",
            regime_line,
            f"Eligibility: {eligibility} | Reason: {reason}",
            f"Dedupe: {dedupe_hint}",
            f"Cache: {cache_hint}",
        ]
        summary_path = os.path.join(UNC_RESULTS, f"{job_id}__summary.txt")
        with open(summary_path, "w", encoding="utf-8") as sf:
            sf.write("\n".join(summary_lines) + "\n")

        # heartbeat end
        _heartbeat(job_id)

    except Exception as e:
        status = "failed"
        err = {"message": str(e), "trace_tail": traceback.format_exc()[-2000:]}
    finally:
        # finalize summary with duration/cache line (append to keep <8KB)
        try:
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as sf:
                    sf.write(f"Render: {int((time.time()-started)*1000)/1000:.1f}s | Job: {job_id}\n")
        except Exception:
            pass

        # write result atomically
        artifacts = []
        if art_path:
            artifacts.append({"path": art_path})
        if summary_path:
            artifacts.append({"path": summary_path})
        result = {
            "job_id": job_id,
            "status": status,
            "started_at": dt.datetime.utcfromtimestamp(started).isoformat() + "Z",
            "completed_at": dt.datetime.utcnow().isoformat() + "Z",
            "duration_ms": int((time.time() - started) * 1000),
            "artifacts": artifacts if status == "succeeded" else [],
            "error": err,
        }
        _atomic_write_json(os.path.join(UNC_RESULTS, f"{job_id}.result.json"), result)
        try:
            os.remove(lock_path)
        except OSError:
            pass

    print(f"{job_id}: {status}")
    return True


def main():
    os.makedirs(UNC_JOBS, exist_ok=True)
    os.makedirs(UNC_RESULTS, exist_ok=True)
    while True:
        processed_any = False
        for name in os.listdir(UNC_JOBS):
            if name.endswith(".json") and not name.endswith(".processing.json"):
                processed_any |= _process(os.path.join(UNC_JOBS, name))
        if not processed_any:
            time.sleep(1)


if __name__ == "__main__":
    main()
