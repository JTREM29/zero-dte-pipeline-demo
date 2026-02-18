from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from enum import IntEnum
from collections import deque
from typing import Any, Callable


class ProfileLevel(IntEnum):
    NORMAL = 0
    EFFICIENT = 1
    DEGRADED = 2
    SURVIVAL = 3


@dataclass(frozen=True)
class RenderProfile:
    level: ProfileLevel
    dpi: int
    top_strikes: int
    include_iv_overlay: bool
    allow_cache_miss_render: bool
    heavy_cmd_allowed: bool
    heavy_cooldown_mult: float

    @property
    def name(self) -> str:
        return {
            ProfileLevel.NORMAL: "normal",
            ProfileLevel.EFFICIENT: "efficient",
            ProfileLevel.DEGRADED: "degraded",
            ProfileLevel.SURVIVAL: "survival",
        }.get(ProfileLevel(int(self.level)), "unknown")

    @property
    def banner_line(self) -> str | None:
        lvl = ProfileLevel(int(self.level))
        if lvl == ProfileLevel.NORMAL:
            return None
        if lvl == ProfileLevel.EFFICIENT:
            return "System: Busy Mode (efficient) — reduced detail to stay responsive."
        if lvl == ProfileLevel.DEGRADED:
            return "System: Busy Mode (degraded) — reduced detail; some overlays may be hidden."
        if lvl == ProfileLevel.SURVIVAL:
            return "System: Busy Mode (survival) — cache-only for heavy charts right now. Try again in 30–60s."
        return None


def _clamp01(x: float) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    if v <= 0.0:
        return 0.0
    if v >= 1.0:
        return 1.0
    return v


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except Exception:
        v = float(lo)
    if v < float(lo):
        return float(lo)
    if v > float(hi):
        return float(hi)
    return float(v)


def _truthy_env(key: str, default: str = "0") -> bool:
    try:
        return (os.getenv(key, default) or "").strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return int(default)


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return float(default)


def profiles_v1() -> dict[ProfileLevel, RenderProfile]:
    return {
        ProfileLevel.NORMAL: RenderProfile(ProfileLevel.NORMAL, dpi=160, top_strikes=25, include_iv_overlay=True, allow_cache_miss_render=True, heavy_cmd_allowed=True, heavy_cooldown_mult=1.0),
        ProfileLevel.EFFICIENT: RenderProfile(ProfileLevel.EFFICIENT, dpi=130, top_strikes=18, include_iv_overlay=True, allow_cache_miss_render=True, heavy_cmd_allowed=True, heavy_cooldown_mult=1.2),
        ProfileLevel.DEGRADED: RenderProfile(ProfileLevel.DEGRADED, dpi=110, top_strikes=12, include_iv_overlay=False, allow_cache_miss_render=True, heavy_cmd_allowed=True, heavy_cooldown_mult=1.6),
        ProfileLevel.SURVIVAL: RenderProfile(ProfileLevel.SURVIVAL, dpi=100, top_strikes=10, include_iv_overlay=False, allow_cache_miss_render=False, heavy_cmd_allowed=False, heavy_cooldown_mult=2.5),
    }


class GPRRManager:
    """GPU Pressure Relief & Render Autopilot (V1a heuristic baseline).

    This module intentionally stays dependency-light and treats failures as safe.
    """

    def __init__(
        self,
        *,
        gather_telemetry: Callable[[], dict[str, Any]],
    ) -> None:
        self._gather_telemetry = gather_telemetry

        self._enabled = _truthy_env("TNT_GPRR_ENABLED", "0")
        self._sample_hz = max(1, _int_env("TNT_GPRR_SAMPLE_HZ", 1))
        self._ring_sec = max(60, _int_env("TNT_GPRR_RING_SEC", 300))
        self._min_profile_sec = max(1, _int_env("TNT_GPRR_PROFILE_MIN_SEC", 10))

        forced = (os.getenv("TNT_GPRR_FORCE_PROFILE") or "").strip()
        self._forced_profile: ProfileLevel | None = None
        if forced:
            try:
                self._forced_profile = ProfileLevel(int(forced))
            except Exception:
                self._forced_profile = None

        self._profiles = profiles_v1()

        self._ring: deque[dict[str, Any]] = deque(maxlen=int(self._ring_sec * self._sample_hz))
        self._pressure: float = 0.0
        self._profile_level: ProfileLevel = ProfileLevel.NORMAL
        self._profile_since_ts: float = time.time()
        self._profile_change_ts: float = self._profile_since_ts

        self._event_loop_lag_ms_ema: float = 0.0

        self._render_avg_ms_ema: float | None = None

        self._ram_baseline_mb: float = float(_float_env("TNT_GPRR_RAM_BASELINE_MB", 800.0))
        self._ram_baseline_locked: bool = bool(os.getenv("TNT_GPRR_RAM_BASELINE_MB"))
        self._warmup_sec: int = max(0, _int_env("TNT_GPRR_WARMUP_SEC", 30))
        self._start_ts: float = time.time()

        # Rolling 60s command counters.
        self._cmd_ts: dict[str, deque[float]] = {
            "chart": deque(),
            "oi": deque(),
            "pcr": deque(),
            "risk": deque(),
            "crypto": deque(),
        }
        self._symbol_last: dict[str, float] = {}

        self._task_sampler: asyncio.Task | None = None
        self._task_lag: asyncio.Task | None = None

        # Hysteresis trackers.
        self._under_exit_since: dict[ProfileLevel, float | None] = {
            ProfileLevel.SURVIVAL: None,
            ProfileLevel.DEGRADED: None,
            ProfileLevel.EFFICIENT: None,
        }

    def enabled(self) -> bool:
        return bool(self._enabled)

    def set_render_avg_ms(self, ms: float) -> None:
        try:
            x = float(ms)
        except Exception:
            return
        if x <= 0:
            return
        # EMA alpha ~0.2
        if self._render_avg_ms_ema is None:
            self._render_avg_ms_ema = x
        else:
            self._render_avg_ms_ema = (0.2 * x) + (0.8 * float(self._render_avg_ms_ema))

    def note_command(self, *, cmd: str, symbol: str | None = None) -> None:
        now = time.time()
        k = (cmd or "").strip().lower()
        if k in self._cmd_ts:
            self._cmd_ts[k].append(now)
        if symbol:
            s = (symbol or "").strip().upper()
            if s:
                self._symbol_last[s] = now

    def current_profile(self) -> RenderProfile:
        lvl = self._forced_profile if self._forced_profile is not None else self._profile_level
        return self._profiles.get(lvl, self._profiles[ProfileLevel.DEGRADED])

    def pressure(self) -> float:
        try:
            return float(self._pressure)
        except Exception:
            return 0.0

    def event_loop_lag_ms(self) -> float:
        try:
            return float(self._event_loop_lag_ms_ema)
        except Exception:
            return 0.0

    def ram_baseline_mb(self) -> float:
        try:
            return float(self._ram_baseline_mb)
        except Exception:
            return 800.0

    def render_avg_ms_ema(self) -> float | None:
        return self._render_avg_ms_ema

    def cmd_counts_60s(self) -> dict[str, int]:
        now = time.time()
        cutoff = now - 60.0
        out: dict[str, int] = {}
        for k, dq in self._cmd_ts.items():
            while dq and dq[0] < cutoff:
                dq.popleft()
            out[k] = int(len(dq))
        # Unique symbols (approx) over last 60s.
        try:
            self._symbol_last = {s: t for s, t in self._symbol_last.items() if float(t) >= cutoff}
        except Exception:
            pass
        out["unique_symbols"] = int(len(self._symbol_last))
        return out

    def cache_key_suffix(self) -> str:
        p = self.current_profile()
        return f"p={int(p.level)}:dpi={int(p.dpi)}:top={int(p.top_strikes)}:iv={1 if p.include_iv_overlay else 0}"

    def effective_heavy_cooldown(self, base_sec: int) -> int:
        try:
            base = int(base_sec)
        except Exception:
            base = 0
        base = max(0, base)
        p = self.current_profile()
        try:
            eff = int(round(float(base) * float(p.heavy_cooldown_mult)))
        except Exception:
            eff = base
        return max(base, eff)

    async def start(self) -> None:
        if not self._enabled:
            return
        if self._task_sampler is None:
            self._task_sampler = asyncio.create_task(self._sampler_loop())
        if self._task_lag is None:
            self._task_lag = asyncio.create_task(self._event_loop_lag_loop())

    async def stop(self) -> None:
        for t in (self._task_sampler, self._task_lag):
            if t is not None:
                try:
                    t.cancel()
                except Exception:
                    pass
        self._task_sampler = None
        self._task_lag = None

    async def _event_loop_lag_loop(self) -> None:
        period = 0.25
        loop = asyncio.get_running_loop()
        expected = loop.time() + period
        while True:
            await asyncio.sleep(period)
            now = loop.time()
            lag = max(0.0, float(now - expected))
            expected = expected + period
            # Smooth: EMA alpha 0.2
            lag_ms = float(lag) * 1000.0
            self._event_loop_lag_ms_ema = (0.2 * lag_ms) + (0.8 * float(self._event_loop_lag_ms_ema))

    async def _sampler_loop(self) -> None:
        interval = 1.0 / float(max(1, int(self._sample_hz)))
        while True:
            t0 = time.time()
            try:
                sample = dict(self._gather_telemetry() or {})
                sample["ts"] = float(sample.get("ts") or time.time())
                sample["event_loop_lag_ms"] = float(self.event_loop_lag_ms())
                # Attach demand rollups.
                counts = self.cmd_counts_60s()
                for k, v in counts.items():
                    sample[f"cmd_{k}_60s"] = int(v)

                # Warmup baseline (best-effort)
                if not self._ram_baseline_locked:
                    try:
                        if (time.time() - float(self._start_ts)) >= float(self._warmup_sec):
                            rss = sample.get("ram_rss_mb")
                            if isinstance(rss, (int, float)) and float(rss) > 0.0:
                                self._ram_baseline_mb = float(rss)
                                self._ram_baseline_locked = True
                    except Exception:
                        pass

                self._ring.append(sample)
                self._pressure = float(self._compute_pressure(sample))
                self._step_profile(sample)
            except Exception:
                # If scorer fails: default to Degraded (safe).
                self._pressure = 0.6
                self._profile_level = ProfileLevel.DEGRADED
            dt = max(0.0, float(interval) - float(time.time() - t0))
            await asyncio.sleep(dt)

    def _compute_pressure(self, s: dict[str, Any]) -> float:
        # Required signals (defaults are safe).
        render_waiting = float(s.get("render_waiting") or 0.0)
        render_active = float(s.get("render_active") or 0.0)
        max_renders = float(s.get("max_renders") or max(1.0, render_active))
        render_avg_ms_ema = float(s.get("render_avg_ms_ema") or (self._render_avg_ms_ema or 0.0))
        lag_ms = float(s.get("event_loop_lag_ms") or 0.0)
        rss_mb = float(s.get("ram_rss_mb") or 0.0)
        http_inflight = float(s.get("http_inflight") or 0.0)
        max_http = float(s.get("max_http") or max(1.0, http_inflight))
        http_timeouts_60s = float(s.get("http_timeouts_60s") or 0.0)
        http_429_60s = float(s.get("http_429_60s") or 0.0)
        http_5xx_60s = float(s.get("http_5xx_60s") or 0.0)

        q = _clamp01(render_waiting / 6.0)
        a = _clamp01(render_active / float(max(1.0, max_renders)))
        t = _clamp01(render_avg_ms_ema / 2500.0)
        e = _clamp01(lag_ms / 250.0)
        m = _clamp01((rss_mb - float(self._ram_baseline_mb)) / 2000.0)
        p = _clamp01((http_timeouts_60s + http_429_60s + http_5xx_60s) / 12.0)
        h = _clamp01(http_inflight / float(max(1.0, max_http)))

        pressure = (0.22 * q) + (0.18 * a) + (0.15 * t) + (0.20 * e) + (0.10 * m) + (0.10 * p) + (0.05 * h)
        return _clamp(pressure, 0.0, 1.0)

    def _step_profile(self, s: dict[str, Any]) -> None:
        # Forced profile (debug)
        if self._forced_profile is not None:
            self._profile_level = self._forced_profile
            return

        now = time.time()
        lag_ms = float(s.get("event_loop_lag_ms") or 0.0)
        waiting = int(s.get("render_waiting") or 0)
        rss_mb = float(s.get("ram_rss_mb") or 0.0)

        emergency = bool(
            (lag_ms > 600.0)
            or (waiting > 12)
            or (rss_mb > (float(self._ram_baseline_mb) + 3500.0))
        )
        if emergency:
            self._set_profile(ProfileLevel.SURVIVAL, now=now, force=True)
            return

        p = float(self._pressure)

        # Enter thresholds.
        desired = ProfileLevel.NORMAL
        if p >= 0.72:
            desired = ProfileLevel.SURVIVAL
        elif p >= 0.55:
            desired = ProfileLevel.DEGRADED
        elif p >= 0.35:
            desired = ProfileLevel.EFFICIENT

        cur = ProfileLevel(int(self._profile_level))

        # Exit thresholds with dwell-time hysteresis.
        # Track how long we've been below the exit threshold.
        def _track_under(level: ProfileLevel, threshold: float, hold_sec: float) -> bool:
            under = bool(p < float(threshold))
            since = self._under_exit_since.get(level)
            if under:
                if since is None:
                    self._under_exit_since[level] = now
                    return False
                return (now - float(since)) >= float(hold_sec)
            self._under_exit_since[level] = None
            return False

        if cur == ProfileLevel.SURVIVAL:
            if _track_under(ProfileLevel.SURVIVAL, 0.62, 20.0):
                desired = ProfileLevel.DEGRADED
            else:
                desired = ProfileLevel.SURVIVAL
        elif cur == ProfileLevel.DEGRADED:
            if _track_under(ProfileLevel.DEGRADED, 0.45, 30.0):
                desired = ProfileLevel.EFFICIENT
            else:
                desired = ProfileLevel.DEGRADED if p >= 0.55 else ProfileLevel.DEGRADED
        elif cur == ProfileLevel.EFFICIENT:
            if _track_under(ProfileLevel.EFFICIENT, 0.30, 45.0):
                desired = ProfileLevel.NORMAL
            else:
                desired = ProfileLevel.EFFICIENT if p >= 0.35 else ProfileLevel.EFFICIENT

        # Minimum time between changes unless it's an enter-to-worse transition.
        force_change = bool(desired > cur)
        self._set_profile(desired, now=now, force=force_change)

    def _set_profile(self, desired: ProfileLevel, *, now: float, force: bool) -> None:
        desired = ProfileLevel(int(desired))
        cur = ProfileLevel(int(self._profile_level))
        if desired == cur:
            return

        if not force:
            if (now - float(self._profile_change_ts)) < float(self._min_profile_sec):
                return

        self._profile_level = desired
        self._profile_change_ts = float(now)
        self._profile_since_ts = float(now)


__all__ = [
    "GPRRManager",
    "ProfileLevel",
    "RenderProfile",
]
