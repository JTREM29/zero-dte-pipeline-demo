import os
import time
import threading
from dataclasses import dataclass
from typing import Optional

import requests

PNG_SIG = b"\x89PNG\r\n\x1a\n"


@dataclass
class WorkerStatus:
    ok: bool
    worker_url: str
    uptime_s: Optional[int] = None
    latency_ms: Optional[int] = None
    build: Optional[str] = None
    detail: str = ""
    checked_at_ts: float = 0.0


class WorkerHealth:
    """
    Tracks worker up/down, supports controller-side safe-mode gating and draining.
    """

    def __init__(self, worker_url: Optional[str] = None):
        self.worker_url = (worker_url or os.getenv("TNT_WORKER_URL", "http://127.0.0.1:8787")).rstrip("/")
        self._lock = threading.Lock()
        self._status = WorkerStatus(ok=False, worker_url=self.worker_url, detail="not checked yet", checked_at_ts=0.0)
        self.safe_mode = False
        self.draining = False
        self._inflight = 0
        self._drain_started_ts: float = 0.0

    # ----- request lifecycle gating -----
    def begin_request(self) -> bool:
        with self._lock:
            if self.draining or self.safe_mode:
                return False
            self._inflight += 1
            return True

    def end_request(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def set_draining(self, value: bool) -> None:
        with self._lock:
            self.draining = value
            if value:
                if self._drain_started_ts == 0.0:
                    self._drain_started_ts = time.time()
            else:
                self._drain_started_ts = 0.0

    def drain_elapsed_s(self) -> float:
        with self._lock:
            if not self.draining or self._drain_started_ts <= 0.0:
                return 0.0
            return max(0.0, time.time() - self._drain_started_ts)

    # ----- status -----
    def get_status(self) -> WorkerStatus:
        with self._lock:
            return self._status

    def _set_status(self, st: WorkerStatus) -> None:
        with self._lock:
            self._status = st

    # ----- active check -----
    def check(self, timeout_s: float = 2.5, url: Optional[str] = None) -> WorkerStatus:
        t0 = time.time()
        try:
            base = (url or self.worker_url).rstrip("/")
            r = requests.get(f"{base}/healthz", timeout=timeout_s)
            r.raise_for_status()
            health = r.json()
            uptime = health.get("uptime_s")
            build = health.get("build")

            r = requests.get(f"{base}/v1/render/smoke", timeout=timeout_s)
            r.raise_for_status()
            blob = r.content
            if not blob.startswith(PNG_SIG):
                raise RuntimeError(f"smoke not png signature: head={blob[:8].hex()}")

            dt_ms = int((time.time() - t0) * 1000)
            st = WorkerStatus(ok=True, worker_url=base, uptime_s=uptime, latency_ms=dt_ms, build=build, detail="ok", checked_at_ts=time.time())
            self._set_status(st)
            with self._lock:
                self.safe_mode = False
            return st
        except Exception as e:
            dt_ms = int((time.time() - t0) * 1000)
            st = WorkerStatus(ok=False, worker_url=(url or self.worker_url), uptime_s=None, latency_ms=dt_ms, detail=f"{type(e).__name__}: {e}", checked_at_ts=time.time())
            self._set_status(st)
            with self._lock:
                self.safe_mode = True
            return st
