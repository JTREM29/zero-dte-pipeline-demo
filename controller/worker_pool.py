from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

from controller.worker_health import WorkerStatus


# --- URL-based worker pool (used by WorkerHeartbeat / ops) -----------------------


class UrlWorkerPool:
    def __init__(self, urls: Optional[List[str]] = None):
        if urls is None:
            raw = os.getenv("TNT_WORKER_URLS", "").strip()
            urls = [u.strip() for u in raw.split(",") if u.strip()]
        self._urls: List[str] = urls or []
        self._status: Dict[str, WorkerStatus] = {}
        self._last_ok_ts: Dict[str, float] = {}
        self._inflight: Dict[str, int] = {u: 0 for u in self._urls}
        self._ready: Dict[str, bool] = {u: False for u in self._urls}
        self._suppress_until: Dict[str, float] = {}
        self._suppress_reason: Dict[str, str] = {}

    def enabled(self) -> bool:
        try:
            return bool(int(os.getenv("TNT_WORKER_POOL_ENABLE", "0"))) and len(self._urls) > 0
        except Exception:
            return False

    def urls(self) -> List[str]:
        return list(self._urls)

    def update(self, url: str, status: WorkerStatus, inflight: Optional[int] = None) -> None:
        self._status[url] = status
        if status.ok:
            self._last_ok_ts[url] = time.time()
            # retain previous ready flag unless explicitly set elsewhere
        if inflight is not None:
            self._inflight[url] = max(0, int(inflight))
        elif url not in self._inflight:
            self._inflight[url] = 0

    def inflight(self, url: str) -> int:
        return int(self._inflight.get(url, 0))

    # Simple per-worker inflight controls
    def inc_inflight(self, url: str, n: int = 1) -> None:
        if url not in self._inflight:
            self._inflight[url] = 0
        self._inflight[url] = max(0, int(self._inflight[url]) + int(n))

    def dec_inflight(self, url: str, n: int = 1) -> None:
        if url not in self._inflight:
            self._inflight[url] = 0
        self._inflight[url] = max(0, int(self._inflight[url]) - int(n))

    def any_ok(self) -> bool:
        return any(getattr(st, "ok", False) for st in self._status.values())

    def healthy_count(self) -> int:
        return sum(1 for st in self._status.values() if getattr(st, "ok", False))

    def best_url(self, exclude: Optional[Set[str]] = None) -> Optional[str]:
        # pick ok workers with lowest inflight under optional cap; tie-breaker: order
        try:
            cap = int(os.getenv("TNT_WORKER_MAX_INFLIGHT", "0"))
        except Exception:
            cap = 0
        candidates: List[Tuple[int, int, str]] = []
        for idx, u in enumerate(self._urls):
            st = self._status.get(u)
            if exclude and u in exclude:
                continue
            if st and st.ok and not self.is_suppressed(u):
                infl = self.inflight(u)
                if cap > 0 and infl >= cap:
                    continue
                candidates.append((infl, idx, u))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    def saturated(self) -> bool:
        # True when there are healthy workers but none pass cap filter
        healthy = [u for u, st in self._status.items() if getattr(st, "ok", False)]
        return bool(healthy) and (self.best_url() is None)

    # readiness marking (after real render probe)
    def set_ready(self, url: str, value: bool) -> None:
        self._ready[url] = bool(value)

    def is_ready(self, url: str) -> bool:
        return bool(self._ready.get(url, False))

    # suppression / quarantine
    def suppress(self, url: str, ttl_s: Optional[int] = None, reason: Optional[str] = None) -> None:
        try:
            default_ttl = int(os.getenv("TNT_WORKER_QUARANTINE_S", "30"))
        except Exception:
            default_ttl = 30
        until = time.time() + (ttl_s if ttl_s is not None else default_ttl)
        self._suppress_until[url] = until
        if reason:
            self._suppress_reason[url] = reason

    def is_suppressed(self, url: str) -> bool:
        until = self._suppress_until.get(url, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            # expired
            self._suppress_until.pop(url, None)
            self._suppress_reason.pop(url, None)
            return False
        return True

    def suppressed_remaining_s(self, url: str) -> int:
        until = self._suppress_until.get(url, 0.0)
        return max(0, int(until - time.time())) if until > 0 else 0

    def suppressed_reason(self, url: str) -> Optional[str]:
        return self._suppress_reason.get(url)

    def status_table(self) -> str:
        lines = []
        for u in self._urls:
            st = self._status.get(u)
            ok = bool(st.ok) if st else False
            build = getattr(st, "build", None) if st else None
            detail = getattr(st, "detail", "n/a") if st else "n/a"
            infl = self.inflight(u)
            ready = self.is_ready(u)
            sup = self.is_suppressed(u)
            sup_rem = self.suppressed_remaining_s(u) if sup else 0
            sup_reason = self.suppressed_reason(u) or ""
            sup_txt = f" suppressed={int(sup)}{f'({sup_rem}s {sup_reason})' if sup else ''}"
            lines.append(f"- {u} | {'OK' if ok else 'DOWN'} | ready={int(ready)} | inflight={infl} | build={build or 'n/a'} |{sup_txt} | {detail}")
        return "\n".join(lines) if lines else "(no workers configured)"


# Global pool instance
WORKER_POOL = UrlWorkerPool()


# --- SMB worker routing (primary vs overflow) ----------------------------------


def _now_s() -> float:
    return float(time.time())


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _env_str(name: str, default: str = "") -> str:
    try:
        return str(os.getenv(name, default) or default)
    except Exception:
        return str(default)


@dataclass(frozen=True)
class WorkerTarget:
    name: str
    jobs_dir: str
    results_dir: str
    heartbeat_filename: str = "tnt_worker.heartbeat.txt"
    heartbeat_ttl_s: int = 45
    max_queue_depth: int = 9999


@dataclass(frozen=True)
class WorkerProbe:
    ok: bool
    reason: str
    queue_depth: int
    oldest_job_age_s: Optional[float]
    hb_age_s: Optional[float]


@dataclass(frozen=True)
class RouteDecision:
    target: WorkerTarget
    routed_name: str
    reason: str
    primary_ok: bool
    primary_reason: str
    secondary_ok: bool
    secondary_reason: str


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except Exception:
        return False


def _safe_stat_mtime(path: Path) -> Optional[float]:
    try:
        return float(path.stat().st_mtime)
    except Exception:
        return None


def probe_worker(target: WorkerTarget, *, now_s: Optional[float] = None) -> WorkerProbe:
    """Probe SMB worker liveness + queue pressure.

    Liveness: results_dir/<heartbeat_filename> mtime age <= heartbeat_ttl_s.
    Pressure: jobs_dir/*.job.json count and oldest mtime age.
    """

    now = float(now_s if now_s is not None else _now_s())
    jobs_dir = Path(str(target.jobs_dir))
    results_dir = Path(str(target.results_dir))

    if not _safe_exists(jobs_dir):
        return WorkerProbe(ok=False, reason="jobs_dir_missing", queue_depth=0, oldest_job_age_s=None, hb_age_s=None)
    if not _safe_exists(results_dir):
        return WorkerProbe(ok=False, reason="results_dir_missing", queue_depth=0, oldest_job_age_s=None, hb_age_s=None)

    hb_path = results_dir / str(target.heartbeat_filename)
    hb_mtime = _safe_stat_mtime(hb_path)
    hb_age = (now - hb_mtime) if hb_mtime is not None else None

    # Queue scan (best-effort)
    queue_depth = 0
    oldest_mtime = None
    try:
        for p in jobs_dir.glob("*.job.json"):
            queue_depth += 1
            mt = _safe_stat_mtime(p)
            if mt is None:
                continue
            if oldest_mtime is None or mt < oldest_mtime:
                oldest_mtime = mt
    except Exception:
        # If we cannot scan jobs, treat as unhealthy.
        return WorkerProbe(ok=False, reason="jobs_scan_failed", queue_depth=0, oldest_job_age_s=None, hb_age_s=hb_age)

    oldest_age = (now - oldest_mtime) if oldest_mtime is not None else None

    # Heartbeat gate
    ttl = max(3, int(target.heartbeat_ttl_s))
    if hb_age is None:
        return WorkerProbe(ok=False, reason="heartbeat_missing", queue_depth=queue_depth, oldest_job_age_s=oldest_age, hb_age_s=None)
    if hb_age > float(ttl):
        return WorkerProbe(
            ok=False,
            reason=f"heartbeat_stale:{int(hb_age)}s",
            queue_depth=queue_depth,
            oldest_job_age_s=oldest_age,
            hb_age_s=hb_age,
        )

    # Capacity gate (overflow cap)
    if queue_depth > int(target.max_queue_depth):
        return WorkerProbe(ok=False, reason=f"queue_full:{queue_depth}", queue_depth=queue_depth, oldest_job_age_s=oldest_age, hb_age_s=hb_age)

    return WorkerProbe(ok=True, reason="ok", queue_depth=queue_depth, oldest_job_age_s=oldest_age, hb_age_s=hb_age)


def _is_pressured(probe: WorkerProbe, *, depth_threshold: int, oldest_threshold_s: float) -> bool:
    if probe.queue_depth >= int(depth_threshold):
        return True
    if probe.oldest_job_age_s is not None and probe.oldest_job_age_s >= float(oldest_threshold_s):
        return True
    return False


def choose_worker(
    *,
    primary: WorkerTarget,
    secondary: WorkerTarget,
    now_s: Optional[float] = None,
) -> RouteDecision:
    """Choose primary vs secondary deterministically."""

    depth_threshold = _env_int("TNT_SMB_PRIMARY_PRESSURE_DEPTH", 4)
    oldest_threshold_s = float(_env_int("TNT_SMB_PRIMARY_PRESSURE_OLDEST_SEC", 25))

    p = probe_worker(primary, now_s=now_s)
    s = probe_worker(secondary, now_s=now_s)

    p_pressured = p.ok and _is_pressured(p, depth_threshold=depth_threshold, oldest_threshold_s=oldest_threshold_s)

    # Default to primary when things look normal.
    if p.ok and not p_pressured:
        return RouteDecision(
            target=primary,
            routed_name=str(primary.name),
            reason=f"primary_ok depth={p.queue_depth}",
            primary_ok=True,
            primary_reason=p.reason,
            secondary_ok=bool(s.ok),
            secondary_reason=s.reason,
        )

    if not p.ok and s.ok:
        return RouteDecision(
            target=secondary,
            routed_name=str(secondary.name),
            reason=f"primary_unhealthy:{p.reason}",
            primary_ok=False,
            primary_reason=p.reason,
            secondary_ok=True,
            secondary_reason=s.reason,
        )

    if p_pressured and s.ok:
        oldest = int(p.oldest_job_age_s or 0)
        return RouteDecision(
            target=secondary,
            routed_name=str(secondary.name),
            reason=f"primary_pressured depth={p.queue_depth} oldest={oldest}s",
            primary_ok=True,
            primary_reason=p.reason,
            secondary_ok=True,
            secondary_reason=s.reason,
        )

    # Fail-open: stick with primary.
    fallback_reason = p.reason
    if p_pressured:
        oldest = int(p.oldest_job_age_s or 0)
        fallback_reason = f"primary_pressured_no_overflow depth={p.queue_depth} oldest={oldest}s secondary={s.reason}"
    elif not p.ok:
        fallback_reason = f"both_unhealthy primary={p.reason} secondary={s.reason}"

    return RouteDecision(
        target=primary,
        routed_name=str(primary.name),
        reason=fallback_reason,
        primary_ok=bool(p.ok),
        primary_reason=p.reason,
        secondary_ok=bool(s.ok),
        secondary_reason=s.reason,
    )


def build_default_targets() -> tuple[WorkerTarget, WorkerTarget]:
    """Build default (primary, secondary) SMB worker targets from env."""

    primary_host = (_env_str("TNT_PRIMARY_SMB_HOST", _env_str("TNT2_HOST", "TNT2")) or "TNT2").strip()
    secondary_host = (_env_str("TNT_OVERFLOW_SMB_HOST", "CLX") or "CLX").strip()

    # Optional mapped drives override (primary only) for compatibility.
    jdrv = _env_str("TNT2_JOBS_DRIVE", "").strip()
    rdrv = _env_str("TNT2_RESULTS_DRIVE", "").strip()

    if jdrv and rdrv:
        primary_jobs = f"{jdrv}\\"
        primary_results = f"{rdrv}\\"
    else:
        primary_jobs = rf"\\{primary_host}\tnt_jobs"
        primary_results = rf"\\{primary_host}\tnt_results"

    secondary_jobs = rf"\\{secondary_host}\tnt_jobs"
    secondary_results = rf"\\{secondary_host}\tnt_results"

    hb_ttl = _env_int("TNT_SMB_WORKER_HEARTBEAT_TTL_SEC", 45)
    overflow_cap = _env_int("TNT_SMB_OVERFLOW_MAX_QUEUE_DEPTH", 6)

    primary = WorkerTarget(name="DELL", jobs_dir=primary_jobs, results_dir=primary_results, heartbeat_ttl_s=hb_ttl, max_queue_depth=9999)
    secondary = WorkerTarget(name="CLX", jobs_dir=secondary_jobs, results_dir=secondary_results, heartbeat_ttl_s=hb_ttl, max_queue_depth=overflow_cap)
    return primary, secondary


# --- HTTP worker pool (primary + overflow) --------------------------------------


@dataclass
class Worker:
    name: str
    base_url: str  # e.g. "http://192.168.1.145:8787"
    inflight_cap: int
    role: str = "primary"  # "primary" | "overflow"
    last_ok_ts: float = 0.0
    last_err_ts: float = 0.0

    def healthz(self, timeout_s: float = 1.0) -> bool:
        try:
            import requests  # type: ignore

            r = requests.get(f"{self.base_url.rstrip('/')}/healthz", timeout=float(timeout_s))
            ok = bool(r.json().get("ok"))
            if ok:
                self.last_ok_ts = time.time()
            return ok
        except Exception:
            self.last_err_ts = time.time()
            return False


class WorkerPool:
    def __init__(self, primary: Worker, overflow: Worker):
        self.primary = primary
        self.overflow = overflow

    def pick(
        self,
        *,
        primary_inflight_used: int,
        primary_oldest_age_s: float,
        overflow_inflight_used: int,
        pressure_age_s: float = 15.0,
    ) -> tuple[Worker, str]:
        # 1) Failover if primary unhealthy
        primary_ok = self.primary.healthz()
        if not primary_ok:
            if self.overflow.healthz():
                if overflow_inflight_used < self.overflow.inflight_cap:
                    return self.overflow, "failover_primary_down"
                return self.overflow, "failover_primary_down_overflow_full"
            return self.primary, "primary_down_no_overflow"

        # 2) Pressure overflow (bounded)
        primary_full = int(primary_inflight_used) >= int(self.primary.inflight_cap)
        pressure = float(primary_oldest_age_s) >= float(pressure_age_s)

        if primary_full or pressure:
            if self.overflow.healthz() and int(overflow_inflight_used) < int(self.overflow.inflight_cap):
                return self.overflow, "overflow_pressure"

        return self.primary, "primary_normal"


__all__ = [
    "UrlWorkerPool",
    "WORKER_POOL",
    "WorkerTarget",
    "WorkerProbe",
    "RouteDecision",
    "probe_worker",
    "choose_worker",
    "build_default_targets",
    "Worker",
    "WorkerPool",
]
