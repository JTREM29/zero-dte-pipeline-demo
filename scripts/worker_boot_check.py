import os
import sys
import time
import requests

PNG_SIG = b"\x89PNG\r\n\x1a\n"

def check_worker(worker_url: str, timeout_s: float = 3.0):
    worker_url = worker_url.rstrip("/")
    t0 = time.time()

    # healthz
    r = requests.get(f"{worker_url}/healthz", timeout=timeout_s)
    r.raise_for_status()
    health = r.json()

    # smoke png
    r = requests.get(f"{worker_url}/v1/render/smoke", timeout=timeout_s)
    r.raise_for_status()
    content = r.content
    if not content.startswith(PNG_SIG):
        raise RuntimeError(
            f"Smoke did not return PNG signature. First 8 bytes: {content[:8].hex()}"
        )

    dt_ms = int((time.time() - t0) * 1000)
    return health, dt_ms


def post_canary(webhook_url: str, message: str, timeout_s: float = 3.0):
    # Discord webhook expects {"content": "..."}
    r = requests.post(webhook_url, json={"content": message}, timeout=timeout_s)
    r.raise_for_status()


def main():
    worker_url = os.getenv("TNT_WORKER_URL", "http://127.0.0.1:8787")
    webhook_url = os.getenv("TNT_CANARY_WEBHOOK_URL", "").strip()

    try:
        health, dt_ms = check_worker(worker_url)
        msg = f"✅ Worker OK | {worker_url} | uptime_s={health.get('uptime_s')} | {dt_ms}ms"
        print(msg)
        if webhook_url:
            post_canary(webhook_url, msg)
    except Exception as e:
        msg = f"❌ Worker DOWN | {worker_url} | {type(e).__name__}: {e}"
        print(msg, file=sys.stderr)
        if webhook_url:
            try:
                post_canary(webhook_url, msg)
            except Exception as e2:
                print(f"(also failed posting canary) {type(e2).__name__}: {e2}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
