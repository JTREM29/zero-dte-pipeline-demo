import os
import threading
import time

from controller.worker_health import WorkerHealth
from controller.canary_notify import CanaryNotifier
from controller.ops_metrics import OPS_METRICS
from controller.worker_pool import WORKER_POOL


class WorkerHeartbeat(threading.Thread):
    daemon = True

    def __init__(self, health: WorkerHealth, canary: CanaryNotifier):
        super().__init__()
        self.health = health
        self.canary = canary
        self.interval_s = int(os.getenv("TNT_WORKER_HEARTBEAT_S", "120"))
        self._stop = threading.Event()
        # autoscale hinting
        self.high_water = int(os.getenv("TNT_INFLIGHT_HIGH_WATER", "6"))
        self.sustain_s = int(os.getenv("TNT_INFLIGHT_SUSTAIN_S", "60"))
        self._above_since = None
        self._autoscale_alerted = False
        # build mismatch alerting
        self._build_mismatch_alerted = False

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            pool_enabled = WORKER_POOL.enabled()
            if pool_enabled:
                # Check all workers
                last_ok = False
                best_url = None
                for u in WORKER_POOL.urls():
                    st_u = self.health.check(url=u)
                    WORKER_POOL.update(u, st_u)
                    if st_u.ok:
                        last_ok = True
                best_url = WORKER_POOL.best_url()
                # Use best or default single url for state labelling
                st = self.health.get_status() if best_url is None else self.health.check(url=best_url)

                # Build/version drift detection across pool
                builds = {getattr(self.health.check(url=u), 'build', None) for u in WORKER_POOL.urls() if getattr(self.health.check(url=u), 'ok', False)}
                builds = {b for b in builds if b}
                if len(builds) > 1 and not self._build_mismatch_alerted:
                    self.canary.post_on_change("DRIFT", f"🟠 Worker build mismatch detected: {sorted(builds)}")
                    self._build_mismatch_alerted = True
                elif len(builds) <= 1 and self._build_mismatch_alerted:
                    # clear alert state when uniform again
                    self._build_mismatch_alerted = False
            else:
                st = self.health.check()
            # Update ops metrics with state durations & max inflight
            inflight_now = self.health.inflight()
            # Determine state label including BUSY
            if self.health.draining:
                state_label = "DRAINING"
            else:
                # compute BUSY vs OK/DOWN
                try:
                    hw = int(os.getenv("TNT_INFLIGHT_HIGH_WATER", "6"))
                except Exception:
                    hw = 6
                if st.ok and inflight_now >= hw:
                    state_label = "BUSY"
                else:
                    state_label = "OK" if st.ok else "DOWN"
            if self.health.safe_mode and state_label not in {"DRAINING", "DOWN"}:
                state_label = "SAFE_MODE"
            OPS_METRICS.update_tick(state_label, inflight_now, self.interval_s)
            # Optional: write a one-shot EOD recap after close.
            try:
                OPS_METRICS.maybe_write_eod_recap(tail_lines=60)
            except Exception:
                pass
            # autoscale hinting
            inflight = self.health.inflight()
            now = time.time()
            if inflight >= self.high_water:
                if self._above_since is None:
                    self._above_since = now
                elif (now - self._above_since) >= self.sustain_s and not self._autoscale_alerted:
                    self.canary.post_on_change(
                        "BUSY",
                        f"🟣 Worker busy sustained | inflight={inflight} (>= {self.high_water}) for {self.sustain_s}s | Consider -Workers 2 or a second box."
                    )
                    OPS_METRICS.inc_autoscale_hint()
                    self._autoscale_alerted = True
            else:
                self._above_since = None
                self._autoscale_alerted = False
            # Auto-expire draining if configured
            try:
                max_min = int(os.getenv("TNT_DRAIN_MAX_MIN", "0"))
            except Exception:
                max_min = 0
            if self.health.draining and max_min > 0 and self.health.drain_elapsed_s() >= max_min * 60:
                # auto disable draining
                self.health.set_draining(False)
                # Post a canary noting auto-expire; include current state
                if st.ok:
                    self.canary.post_on_change("OK", f"🟢 Drain auto-expired after {max_min}m | {st.worker_url} | uptime_s={st.uptime_s} | {st.latency_ms}ms")
                else:
                    self.canary.post_on_change("DOWN", f"🟢 Drain auto-expired after {max_min}m | {st.worker_url} | {st.detail}")
            if self.health.draining:
                state = "DRAINING"
            else:
                state = "OK" if st.ok else "DOWN"

            if state == "OK":
                self.canary.post_on_change("OK", f"✅ Worker OK | {st.worker_url} | uptime_s={st.uptime_s} | {st.latency_ms}ms")
            elif state == "DRAINING":
                self.canary.post_on_change("DRAINING", f"🟡 Worker DRAINING | {st.worker_url} | inflight={self.health.inflight()}")
            else:
                self.canary.post_on_change("DOWN", f"❌ Worker DOWN | {st.worker_url} | {st.detail}")

            for _ in range(max(1, self.interval_s)):
                if self._stop.is_set():
                    break
                time.sleep(1)
