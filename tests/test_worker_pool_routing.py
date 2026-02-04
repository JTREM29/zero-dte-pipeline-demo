from __future__ import annotations

import os
import time
from pathlib import Path

from controller.worker_pool import Worker, WorkerPool, WorkerTarget, choose_worker


def _touch(path: Path, *, mtime_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok", encoding="utf-8")
    os.utime(path, (mtime_s, mtime_s))


def test_routes_primary_when_healthy_and_not_pressured(tmp_path: Path) -> None:
    now = time.time()
    p_jobs = tmp_path / "p_jobs"
    p_res = tmp_path / "p_res"
    s_jobs = tmp_path / "s_jobs"
    s_res = tmp_path / "s_res"

    p_jobs.mkdir()
    p_res.mkdir()
    s_jobs.mkdir()
    s_res.mkdir()

    _touch(p_res / "tnt_worker.heartbeat.txt", mtime_s=now)
    _touch(s_res / "tnt_worker.heartbeat.txt", mtime_s=now)

    primary = WorkerTarget(name="DELL", jobs_dir=str(p_jobs), results_dir=str(p_res), heartbeat_ttl_s=60)
    secondary = WorkerTarget(name="CLX", jobs_dir=str(s_jobs), results_dir=str(s_res), heartbeat_ttl_s=60)

    os.environ["TNT_SMB_PRIMARY_PRESSURE_DEPTH"] = "4"
    os.environ["TNT_SMB_PRIMARY_PRESSURE_OLDEST_SEC"] = "25"

    dec = choose_worker(primary=primary, secondary=secondary, now_s=now)
    assert dec.routed_name == "DELL"


def test_routes_secondary_when_primary_stale(tmp_path: Path) -> None:
    now = time.time()
    p_jobs = tmp_path / "p_jobs"
    p_res = tmp_path / "p_res"
    s_jobs = tmp_path / "s_jobs"
    s_res = tmp_path / "s_res"

    p_jobs.mkdir()
    p_res.mkdir()
    s_jobs.mkdir()
    s_res.mkdir()

    _touch(p_res / "tnt_worker.heartbeat.txt", mtime_s=now - 999)
    _touch(s_res / "tnt_worker.heartbeat.txt", mtime_s=now)

    primary = WorkerTarget(name="DELL", jobs_dir=str(p_jobs), results_dir=str(p_res), heartbeat_ttl_s=30)
    secondary = WorkerTarget(name="CLX", jobs_dir=str(s_jobs), results_dir=str(s_res), heartbeat_ttl_s=30)

    dec = choose_worker(primary=primary, secondary=secondary, now_s=now)
    assert dec.routed_name == "CLX"
    assert "primary_unhealthy" in dec.reason


def test_routes_secondary_when_primary_pressured_by_depth(tmp_path: Path) -> None:
    now = time.time()
    p_jobs = tmp_path / "p_jobs"
    p_res = tmp_path / "p_res"
    s_jobs = tmp_path / "s_jobs"
    s_res = tmp_path / "s_res"

    p_jobs.mkdir()
    p_res.mkdir()
    s_jobs.mkdir()
    s_res.mkdir()

    _touch(p_res / "tnt_worker.heartbeat.txt", mtime_s=now)
    _touch(s_res / "tnt_worker.heartbeat.txt", mtime_s=now)

    # Create pending jobs in primary to exceed pressure depth.
    for i in range(5):
        _touch(p_jobs / f"job{i}.job.json", mtime_s=now - 1)

    os.environ["TNT_SMB_PRIMARY_PRESSURE_DEPTH"] = "4"
    os.environ["TNT_SMB_PRIMARY_PRESSURE_OLDEST_SEC"] = "25"

    primary = WorkerTarget(name="DELL", jobs_dir=str(p_jobs), results_dir=str(p_res), heartbeat_ttl_s=60)
    secondary = WorkerTarget(name="CLX", jobs_dir=str(s_jobs), results_dir=str(s_res), heartbeat_ttl_s=60)

    dec = choose_worker(primary=primary, secondary=secondary, now_s=now)
    assert dec.routed_name == "CLX"
    assert "primary_pressured" in dec.reason


class DummyWorker(Worker):
    def __init__(self, *args, ok: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self._ok = bool(ok)

    def healthz(self, timeout_s: float = 1.0) -> bool:  # noqa: ARG002
        return bool(self._ok)


def test_http_overflow_on_pressure() -> None:
    dell = DummyWorker("dell", "http://dell", inflight_cap=2, role="primary", ok=True)
    clx = DummyWorker("clx", "http://clx", inflight_cap=1, role="overflow", ok=True)
    pool = WorkerPool(dell, clx)

    w, reason = pool.pick(primary_inflight_used=2, primary_oldest_age_s=1, overflow_inflight_used=0)
    assert w.name == "clx"
    assert reason == "overflow_pressure"


def test_http_primary_when_not_pressure() -> None:
    dell = DummyWorker("dell", "http://dell", inflight_cap=2, role="primary", ok=True)
    clx = DummyWorker("clx", "http://clx", inflight_cap=1, role="overflow", ok=True)
    pool = WorkerPool(dell, clx)

    w, reason = pool.pick(primary_inflight_used=1, primary_oldest_age_s=1, overflow_inflight_used=0)
    assert w.name == "dell"
    assert reason == "primary_normal"


def test_http_failover_when_primary_down() -> None:
    dell = DummyWorker("dell", "http://dell", inflight_cap=2, role="primary", ok=False)
    clx = DummyWorker("clx", "http://clx", inflight_cap=1, role="overflow", ok=True)
    pool = WorkerPool(dell, clx)

    w, reason = pool.pick(primary_inflight_used=0, primary_oldest_age_s=0, overflow_inflight_used=0)
    assert w.name == "clx"
    assert reason == "failover_primary_down"
