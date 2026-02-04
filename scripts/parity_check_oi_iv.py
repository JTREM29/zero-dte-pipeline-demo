from __future__ import annotations

import hashlib
import json
import os
import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass(frozen=True)
class ParityResult:
    ok: bool
    reason: str
    worker_url: str
    sha256_local_png: str | None
    sha256_worker_png: str | None
    sha256_local_pixels: str | None
    sha256_worker_pixels: str | None
    local_ms: int | None
    worker_ms: int | None

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ok": bool(self.ok),
            "reason": str(self.reason),
            "worker_url": str(self.worker_url),
            "sha256_local_png": self.sha256_local_png,
            "sha256_worker_png": self.sha256_worker_png,
            "sha256_local_pixels": self.sha256_local_pixels,
            "sha256_worker_pixels": self.sha256_worker_pixels,
            "local_ms": self.local_ms,
            "worker_ms": self.worker_ms,
        }
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_pixels_png(png: bytes) -> str | None:
    """Return SHA256 of decoded RGBA pixels.

    This is useful debug signal when PNG-bytes differ due to metadata/compression.
    """

    try:
        from PIL import Image  # type: ignore

        import io

        im = Image.open(io.BytesIO(png)).convert("RGBA")
        return hashlib.sha256(im.tobytes()).hexdigest()
    except Exception:
        return None


def _http_post_png(url: str, payload: dict[str, Any], *, timeout_s: float = 20.0) -> bytes:
    # Prefer httpx (already used elsewhere in repo); fall back to urllib.
    try:
        import httpx

        with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=2.0)) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            return bytes(r.content)
    except Exception:
        import urllib.request

        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
            return resp.read()


def _http_post_json(url: str, payload: dict[str, Any], *, timeout_s: float = 10.0) -> None:
    try:
        import httpx

        with httpx.Client(timeout=httpx.Timeout(timeout_s, connect=2.0)) as client:
            client.post(url, json=payload)
            return
    except Exception:
        try:
            import urllib.request

            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=float(timeout_s)).read()
        except Exception:
            return


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_jsonl(path: Path, line: str) -> None:
    _ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--worker-url", dest="worker_url", default="", help="Worker base URL (overrides TNT_WORKER_URL)")
    args, _unknown = parser.parse_known_args()

    worker_base = (str(args.worker_url or "") or (os.getenv("TNT_WORKER_URL") or "")).strip().rstrip("/")
    logs_path = Path("logs") / "parity_oi_iv.jsonl"
    webhook = (os.getenv("TNT_PARITY_ALERT_WEBHOOK_URL") or "").strip()

    def _emit(result: ParityResult) -> None:
        _write_jsonl(logs_path, result.to_json())
        if not webhook:
            return
        if result.ok:
            return
        try:
            _http_post_json(
                webhook,
                {
                    "text": "TNT parity check failed",
                    "result": json.loads(result.to_json()),
                },
                timeout_s=10.0,
            )
        except Exception:
            return

    if not worker_base:
        res = ParityResult(
            ok=False,
            reason="missing_TNT_WORKER_URL",
            worker_url="",
            sha256_local_png=None,
            sha256_worker_png=None,
            sha256_local_pixels=None,
            sha256_worker_pixels=None,
            local_ms=None,
            worker_ms=None,
        )
        print("[PARITY] TNT_WORKER_URL missing")
        _emit(res)
        return 3

    strict_bytes = (os.getenv("TNT_PARITY_STRICT_BYTES", "1").strip().lower() in {"1", "true", "yes", "on"})

    # Deterministic, fixed payload (no market-data dependency)
    payload = {
        "symbol": "SPY",
        "strikes": [
            480.0,
            485.0,
            490.0,
            495.0,
            500.0,
            505.0,
            510.0,
            515.0,
            520.0,
        ],
        "call_oi": [12000, 18000, 35000, 42000, 78000, 41000, 28000, 16000, 9000],
        "put_oi": [9000, 15000, 24000, 38000, 66000, 52000, 34000, 19000, 11000],
        "call_iv": [22.1, 21.7, 21.4, 21.2, 20.9, 21.1, 21.5, 22.0, 22.6],
        "put_iv": None,
        "title": "PARITY — SPY Options — OI by Strike | deterministic payload",
    }

    # Local render uses the same shared renderer used by workers (delivery/oi_iv_render.py)
    local_png: bytes | None = None
    local_ms: int | None = None
    try:
        from delivery.oi_iv_render import render_oi_iv_png

        labels = [f"{float(s):g}" for s in payload["strikes"]]
        t0 = time.perf_counter()
        local_png = render_oi_iv_png(
            title=str(payload["title"]),
            x_labels=labels,
            iv_pct=[float(x) for x in payload["call_iv"]],
            oi_calls=[float(x) for x in payload["call_oi"]],
            oi_puts=[float(x) for x in payload["put_oi"]],
            dpi=150,
            include_iv_overlay=True,
        )
        local_ms = int((time.perf_counter() - t0) * 1000.0)
    except Exception as exc:
        res = ParityResult(
            ok=False,
            reason=f"local_render_failed:{type(exc).__name__}:{exc}",
            worker_url=worker_base,
            sha256_local_png=None,
            sha256_worker_png=None,
            sha256_local_pixels=None,
            sha256_worker_pixels=None,
            local_ms=None,
            worker_ms=None,
        )
        print(f"[PARITY] local render failed: {type(exc).__name__}: {exc}")
        _emit(res)
        return 4

    if not isinstance(local_png, (bytes, bytearray)) or not local_png:
        res = ParityResult(
            ok=False,
            reason="local_render_empty",
            worker_url=worker_base,
            sha256_local_png=None,
            sha256_worker_png=None,
            sha256_local_pixels=None,
            sha256_worker_pixels=None,
            local_ms=local_ms,
            worker_ms=None,
        )
        print("[PARITY] local render returned empty")
        _emit(res)
        return 4

    # Worker render
    worker_png: bytes | None = None
    worker_ms: int | None = None
    try:
        url = f"{worker_base}/v1/render/oi_iv"
        t0 = time.perf_counter()
        worker_png = _http_post_png(url, payload, timeout_s=25.0)
        worker_ms = int((time.perf_counter() - t0) * 1000.0)
    except Exception as exc:
        res = ParityResult(
            ok=False,
            reason=f"worker_fetch_failed:{type(exc).__name__}:{exc}",
            worker_url=worker_base,
            sha256_local_png=_sha256_bytes(bytes(local_png)),
            sha256_worker_png=None,
            sha256_local_pixels=_sha256_pixels_png(bytes(local_png)),
            sha256_worker_pixels=None,
            local_ms=local_ms,
            worker_ms=None,
        )
        print(f"[PARITY] worker fetch failed: {type(exc).__name__}: {exc}")
        _emit(res)
        return 5

    if not worker_png:
        res = ParityResult(
            ok=False,
            reason="worker_returned_empty",
            worker_url=worker_base,
            sha256_local_png=_sha256_bytes(bytes(local_png)),
            sha256_worker_png=None,
            sha256_local_pixels=_sha256_pixels_png(bytes(local_png)),
            sha256_worker_pixels=None,
            local_ms=local_ms,
            worker_ms=worker_ms,
        )
        print("[PARITY] worker returned empty")
        _emit(res)
        return 5

    sha_local = _sha256_bytes(bytes(local_png))
    sha_worker = _sha256_bytes(bytes(worker_png))
    pix_local = _sha256_pixels_png(bytes(local_png))
    pix_worker = _sha256_pixels_png(bytes(worker_png))

    ok = (sha_local == sha_worker)
    reason = "sha256_match" if ok else "sha256_mismatch"

    out = ParityResult(
        ok=ok,
        reason=reason,
        worker_url=worker_base,
        sha256_local_png=sha_local,
        sha256_worker_png=sha_worker,
        sha256_local_pixels=pix_local,
        sha256_worker_pixels=pix_worker,
        local_ms=local_ms,
        worker_ms=worker_ms,
    )
    _emit(out)

    # Save artifacts on mismatch
    if not ok:
        art_dir = Path("artifacts") / "parity" / time.strftime("%Y-%m-%d")
        _ensure_dir(art_dir)
        (art_dir / "local.png").write_bytes(bytes(local_png))
        (art_dir / "worker.png").write_bytes(bytes(worker_png))

    print(
        "[PARITY] "
        + " ".join(
            [
                f"ok={int(ok)}",
                f"cache=N/A",
                f"RENDER_SOURCE=LOCAL+DELL",
                f"local_ms={local_ms}",
                f"worker_ms={worker_ms}",
                f"sha_local={sha_local[:12]}",
                f"sha_worker={sha_worker[:12]}",
                f"pix_local={(pix_local or '')[:12]}",
                f"pix_worker={(pix_worker or '')[:12]}",
            ]
        )
    )

    if ok:
        return 0

    # If byte hash mismatches but pixel hash matches, we still treat as mismatch by default
    # (institutional-grade strictness); you can loosen with TNT_PARITY_STRICT_BYTES=0.
    if (not strict_bytes) and (pix_local is not None) and (pix_local == pix_worker):
        print("[PARITY] PNG bytes differ but pixels match; treating as OK (strict disabled)")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
