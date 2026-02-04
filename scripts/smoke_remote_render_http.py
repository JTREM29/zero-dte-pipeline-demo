from __future__ import annotations

import json
import os
import sys
import urllib.request


def _get_env(name: str, default: str = "") -> str:
    try:
        return str(os.getenv(name, default) or default).strip()
    except Exception:
        return str(default).strip()


def _fetch(url: str, *, token: str | None = None, timeout_s: float = 8.0) -> bytes:
    headers = {}
    if token:
        headers["X-TNT-Token"] = token
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:  # noqa: S310
        return resp.read()


def _maybe_preflight_healthz(*, base_url: str, token: str | None) -> None:
    """Best-effort preflight to warm up the connection/ARP path.

    This avoids flaky first-request ConnectTimeouts on some LAN setups.
    """

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return
    try:
        _fetch(f"{base}/healthz", token=token, timeout_s=2.5)
    except Exception:
        # Do not fail the smoke run on preflight.
        return


def _fetch_with_retry(
    url: str,
    *,
    token: str | None = None,
    timeout_s: float = 8.0,
    attempts: int = 3,
    backoff_s: float = 0.75,
) -> bytes:
    last_exc: Exception | None = None
    tries = max(1, int(attempts))
    for i in range(tries):
        try:
            # Slightly longer on the first attempt; subsequent attempts back off.
            t = float(timeout_s)
            if i == 0:
                t = max(t, 12.0)
            return _fetch(url, token=token, timeout_s=t)
        except Exception as exc:
            last_exc = exc
            if i < (tries - 1):
                try:
                    time_s = float(backoff_s) * (1.0 + float(i))
                except Exception:
                    time_s = 0.75
                try:
                    import time

                    time.sleep(max(0.1, time_s))
                except Exception:
                    pass
                continue
            break
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch failed")


def main() -> int:
    from scripts.enqueue_and_wait import enqueue_and_wait

    redis_host = _get_env("TNT_REDIS_HOST", "127.0.0.1")
    redis_port = int(_get_env("TNT_REDIS_PORT", "6379") or "6379")

    base_url = _get_env("TNT_ARTIFACT_HTTP_BASE_URL") or _get_env("TNT_ARTIFACTS_BASE_URL")
    token = _get_env("TNT_ARTIFACT_HTTP_TOKEN")
    fetch_timeout_s = float(_get_env("TNT_ARTIFACT_FETCH_TIMEOUT_S", "8") or "8")
    fetch_attempts = int(_get_env("TNT_ARTIFACT_FETCH_ATTEMPTS", "3") or "3")

    if not base_url:
        print("[FATAL] Set TNT_ARTIFACT_HTTP_BASE_URL (e.g. http://192.168.1.145:8787)")
        return 2

    print(f"[SMOKE] redis={redis_host}:{redis_port} base_url={base_url} fetch_timeout_s={fetch_timeout_s:g} attempts={fetch_attempts}")

    # Best-effort preflight to reduce first-connect flakiness.
    _maybe_preflight_healthz(base_url=base_url, token=(token or None))

    try:
        outcome = enqueue_and_wait(
            redis_host=redis_host,
            redis_port=redis_port,
            symbol=_get_env("TNT_SMOKE_SYMBOL", "SPY"),
            job_type="render_oi",
            payload={"top": 18, "dpi": 130},
            reply_path=None,
            timeout_s=float(_get_env("TNT_SMOKE_TIMEOUT_S", "120") or "120"),
        )
    except Exception as exc:
        print(f"[SMOKE][FATAL] enqueue failed: {type(exc).__name__}: {exc}")
        print(f"[SMOKE][HINT] Check Redis reachability: Test-NetConnection -ComputerName {redis_host} -Port {redis_port}")
        return 2

    result = outcome.result if isinstance(outcome.result, dict) else {"result": outcome.result}
    print(f"[SMOKE] job_id={outcome.job_id} status={outcome.status}")

    if str(outcome.status) != "succeeded":
        print("[SMOKE][FAIL] job did not succeed")
        print(json.dumps(result, indent=2)[:4000])
        return 1

    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    png_url = None
    png_obj = artifacts.get("png")
    if isinstance(png_obj, dict):
        png_url = str(png_obj.get("url") or "").strip() or None
    if not png_url:
        png_url = str(result.get("artifact_url") or "").strip() or None

    if not png_url:
        print("[SMOKE][FAIL] no artifact URL in result")
        print(json.dumps(result, indent=2)[:4000])
        return 1

    try:
        data = _fetch_with_retry(
            png_url,
            token=(token or None),
            timeout_s=fetch_timeout_s,
            attempts=fetch_attempts,
        )
    except Exception as exc:
        print(f"[SMOKE][FAIL] fetch failed: {type(exc).__name__}: {exc}")
        print(f"url={png_url}")
        return 1

    if not (data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 1024):
        print(f"[SMOKE][FAIL] unexpected artifact bytes: len={len(data)}")
        return 1

    print(f"[SMOKE][OK] fetched PNG len={len(data)} url={png_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
