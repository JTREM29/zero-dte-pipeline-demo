import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

try:  # pragma: no cover - optional dependency
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore


OPS_DIR = Path("logs") / "ops_metrics"
OPS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class OpsSnapshot:
    date: str
    max_inflight: int
    down_minutes: int
    draining_minutes: int
    safemode_minutes: int
    busy_minutes: int
    # Friendly alias keys (kept in sync with *_minutes fields)
    minutes_down: int
    minutes_draining: int
    minutes_safe_mode: int
    minutes_busy: int
    blocked_rate_limit: int
    blocked_draining: int
    blocked_safemode: int
    blocked_unhealthy: int
    autoscale_hints: int

    heavy_started: int
    heavy_completed: int
    heavy_cost_started: int
    heavy_cost_completed: int

    http_attempted: int
    http_succeeded: int
    http_fallback_smb: int
    http_shadow_attempted: int
    http_shadow_ok: int
    http_shadow_fail: int
    http_ms_last: int
    smb_ms_last: int

    # LLM usage totals (daily)
    llm_calls: int
    llm_input_tokens: int
    llm_cached_input_tokens: int
    llm_output_tokens: int
    llm_estimated_cost_usd: float


class OpsMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._date = time.strftime("%Y-%m-%d")
        self._max_inflight = 0
        self._down_s = 0
        self._draining_s = 0
        self._safemode_s = 0
        self._busy_s = 0
        self._blocked: Dict[str, int] = {
            "rate_limit": 0,
            "draining": 0,
            "safe_mode": 0,
            "unhealthy": 0,
        }
        self._autoscale_hints = 0
        self._heavy_started = 0
        self._heavy_completed = 0
        self._heavy_cost_started = 0
        self._heavy_cost_completed = 0
        self._http_attempted = 0
        self._http_succeeded = 0
        self._http_fallback_smb = 0
        self._http_shadow_attempted = 0
        self._http_shadow_ok = 0
        self._http_shadow_fail = 0
        self._http_ms_last = 0
        self._smb_ms_last = 0
        self._llm_calls = 0
        self._llm_input_tokens = 0
        self._llm_cached_input_tokens = 0
        self._llm_output_tokens = 0
        self._llm_estimated_cost_usd = 0.0
        self._eod_written_date: str | None = None

    def _rollover_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._date:
            # Persist previous day snapshot
            self._persist_locked()
            # Reset counters for new day
            self._date = today
            self._max_inflight = 0
            self._down_s = 0
            self._draining_s = 0
            self._safemode_s = 0
            self._busy_s = 0
            self._blocked = {"rate_limit": 0, "draining": 0, "safe_mode": 0, "unhealthy": 0}
            self._autoscale_hints = 0
            self._heavy_started = 0
            self._heavy_completed = 0
            self._heavy_cost_started = 0
            self._heavy_cost_completed = 0
            self._http_attempted = 0
            self._http_succeeded = 0
            self._http_fallback_smb = 0
            self._http_shadow_attempted = 0
            self._http_shadow_ok = 0
            self._http_shadow_fail = 0
            self._http_ms_last = 0
            self._smb_ms_last = 0
            self._llm_calls = 0
            self._llm_input_tokens = 0
            self._llm_cached_input_tokens = 0
            self._llm_output_tokens = 0
            self._llm_estimated_cost_usd = 0.0

    def record_llm_usage(
        self,
        *,
        calls: int = 1,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
    ) -> None:
        """Record per-call LLM usage (best-effort).

        This is intentionally schema-light: we track daily totals only.
        """

        with self._lock:
            self._rollover_if_needed()
            try:
                self._llm_calls += max(0, int(calls))
            except Exception:
                self._llm_calls += 1

            if isinstance(input_tokens, (int, float)):
                try:
                    self._llm_input_tokens += max(0, int(input_tokens))
                except Exception:
                    pass
            if isinstance(cached_input_tokens, (int, float)):
                try:
                    self._llm_cached_input_tokens += max(0, int(cached_input_tokens))
                except Exception:
                    pass
            if isinstance(output_tokens, (int, float)):
                try:
                    self._llm_output_tokens += max(0, int(output_tokens))
                except Exception:
                    pass
            if isinstance(estimated_cost_usd, (int, float)):
                try:
                    self._llm_estimated_cost_usd += max(0.0, float(estimated_cost_usd))
                except Exception:
                    pass

            self._persist_locked()

    def update_tick(self, state: str, inflight: int, dt_s: int):
        with self._lock:
            self._rollover_if_needed()
            self._max_inflight = max(self._max_inflight, int(inflight))
            s = state.upper()
            if s == "DOWN":
                self._down_s += dt_s
            elif s == "DRAINING":
                self._draining_s += dt_s
            elif s == "SAFE_MODE":
                self._safemode_s += dt_s
            elif s == "BUSY":
                self._busy_s += dt_s
            # Persist lightweight snapshot each tick
            self._persist_locked()

    def inc_block(self, reason: str):
        with self._lock:
            self._rollover_if_needed()
            key = reason.lower()
            # normalize
            if key in {"unhealthy", "blocked_unhealthy"}:
                key = "unhealthy"
            if key in {"safe", "safe_mode_block", "blocked_safe_mode"}:
                key = "safe_mode"
            if key not in self._blocked:
                self._blocked[key] = 0
            self._blocked[key] += 1
            self._persist_locked()

    def inc_autoscale_hint(self):
        with self._lock:
            self._rollover_if_needed()
            self._autoscale_hints += 1
            self._persist_locked()

    def inc_heavy_started(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._heavy_started += n
            self._persist_locked()

    def inc_heavy_completed(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._heavy_completed += n
            self._persist_locked()

    def inc_heavy_cost_started(self, cost: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._heavy_cost_started += max(1, int(cost))
            self._persist_locked()

    def inc_heavy_cost_completed(self, cost: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._heavy_cost_completed += max(1, int(cost))
            self._persist_locked()

    def inc_http_attempted(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._http_attempted += n
            self._persist_locked()

    def inc_http_succeeded(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._http_succeeded += n
            self._persist_locked()

    def inc_http_fallback_smb(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._http_fallback_smb += n
            self._persist_locked()

    def inc_http_shadow_attempted(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._http_shadow_attempted += n
            self._persist_locked()

    def inc_http_shadow_ok(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._http_shadow_ok += n
            self._persist_locked()

    def inc_http_shadow_fail(self, n: int = 1):
        with self._lock:
            self._rollover_if_needed()
            self._http_shadow_fail += n
            self._persist_locked()

    def set_http_ms_last(self, ms: int):
        with self._lock:
            self._rollover_if_needed()
            self._http_ms_last = int(ms)
            self._persist_locked()

    def set_smb_ms_last(self, ms: int):
        with self._lock:
            self._rollover_if_needed()
            self._smb_ms_last = int(ms)
            self._persist_locked()

    def _persist_locked(self):
        down_m = int(self._down_s // 60)
        drain_m = int(self._draining_s // 60)
        safe_m = int(self._safemode_s // 60)
        busy_m = int(self._busy_s // 60) if hasattr(self, "_busy_s") else 0
        snap = OpsSnapshot(
            date=self._date,
            max_inflight=self._max_inflight,
            down_minutes=down_m,
            draining_minutes=drain_m,
            safemode_minutes=safe_m,
            busy_minutes=busy_m,
            minutes_down=down_m,
            minutes_draining=drain_m,
            minutes_safe_mode=safe_m,
            minutes_busy=busy_m,
            blocked_rate_limit=int(self._blocked.get("rate_limit", 0)),
            blocked_draining=int(self._blocked.get("draining", 0)),
            blocked_safemode=int(self._blocked.get("safe_mode", 0)),
            blocked_unhealthy=int(self._blocked.get("unhealthy", 0)),
            autoscale_hints=self._autoscale_hints,

            heavy_started=int(self._heavy_started),
            heavy_completed=int(self._heavy_completed),
            heavy_cost_started=int(self._heavy_cost_started),
            heavy_cost_completed=int(self._heavy_cost_completed),

            http_attempted=int(self._http_attempted),
            http_succeeded=int(self._http_succeeded),
            http_fallback_smb=int(self._http_fallback_smb),
            http_shadow_attempted=int(self._http_shadow_attempted),
            http_shadow_ok=int(self._http_shadow_ok),
            http_shadow_fail=int(self._http_shadow_fail),
            http_ms_last=int(self._http_ms_last),
            smb_ms_last=int(self._smb_ms_last),

            llm_calls=int(self._llm_calls),
            llm_input_tokens=int(self._llm_input_tokens),
            llm_cached_input_tokens=int(self._llm_cached_input_tokens),
            llm_output_tokens=int(self._llm_output_tokens),
            llm_estimated_cost_usd=float(self._llm_estimated_cost_usd),
        )
        path = OPS_DIR / f"{self._date}.json"
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(snap), f, indent=2)
        os.replace(tmp, path)

    def maybe_write_eod_recap(
        self,
        *,
        now_epoch: int | None = None,
        at_hour: int = 16,
        at_minute: int = 15,
        root_dir: str | Path | None = None,
        tail_lines: int = 60,
    ) -> bool:
        """Write an end-of-day recap once per day (env-gated).

        Writes:
        - logs/ops_metrics/eod_recap_YYYY-MM-DD.txt (history)
        - logs/eod_recap_latest.txt (rolling pointer)

        Returns True when it wrote a recap.
        """

        try:
            if bool(int(os.getenv("TNT_EOD_RECAP_ENABLED", "0") or "0")) is False:
                return False
        except Exception:
            return False

        try:
            tz_name = (os.getenv("TNT_EOD_RECAP_TZ", "America/New_York") or "America/New_York").strip()
        except Exception:
            tz_name = "America/New_York"

        et_tz = None
        if ZoneInfo is not None:
            try:
                et_tz = ZoneInfo(tz_name)
            except Exception:
                et_tz = None
        if et_tz is None:
            et_tz = timezone.utc

        now_s = int(now_epoch if now_epoch is not None else time.time())
        now_dt = datetime.fromtimestamp(float(now_s), tz=timezone.utc).astimezone(et_tz)
        date = now_dt.strftime("%Y-%m-%d")

        # Only write after the configured time.
        if (int(now_dt.hour), int(now_dt.minute)) < (int(at_hour), int(at_minute)):
            return False

        root = Path(root_dir).resolve() if root_dir is not None else Path.cwd().resolve()
        logs_dir = root / "logs"
        ops_dir = logs_dir / "ops_metrics"
        ops_dir.mkdir(parents=True, exist_ok=True)

        out_path = ops_dir / f"eod_recap_{date}.txt"
        latest_path = logs_dir / "eod_recap_latest.txt"

        with self._lock:
            self._rollover_if_needed()
            if self._eod_written_date == date and out_path.exists():
                return False

            # Ensure the daily snapshot json is up-to-date before recapping.
            self._persist_locked()

            ops_json = ops_dir / f"{date}.json"
            ops = None
            try:
                ops = json.loads(ops_json.read_text(encoding="utf-8"))
            except Exception:
                ops = None

            lines: list[str] = []
            lines.append(f"=== TNT EOD Ops Recap ({date}) ===")
            lines.append(f"Generated at {now_dt.strftime('%H:%M')} {tz_name}")
            lines.append("")

            if isinstance(ops, dict):
                down_m = int(ops.get("minutes_down", ops.get("down_minutes", 0)) or 0)
                drain_m = int(ops.get("minutes_draining", ops.get("draining_minutes", 0)) or 0)
                safe_m = int(ops.get("minutes_safe_mode", ops.get("safemode_minutes", 0)) or 0)
                busy_m = int(ops.get("minutes_busy", ops.get("busy_minutes", 0)) or 0)
                lines.append(f"State minutes: DOWN={down_m} DRAIN={drain_m} SAFE={safe_m} BUSY={busy_m}")
                lines.append(f"Inflight: max={int(ops.get('max_inflight', 0) or 0)}")
                lines.append(
                    "Heavy: "
                    f"{int(ops.get('heavy_started', 0) or 0)}/{int(ops.get('heavy_completed', 0) or 0)} "
                    f"(cost {int(ops.get('heavy_cost_started', 0) or 0)}/{int(ops.get('heavy_cost_completed', 0) or 0)})"
                )
                lines.append(
                    "Blocked: "
                    f"rate={int(ops.get('blocked_rate_limit', 0) or 0)} "
                    f"drain={int(ops.get('blocked_draining', 0) or 0)} "
                    f"safe={int(ops.get('blocked_safemode', ops.get('blocked_safe_mode', 0)) or 0)} "
                    f"unhealthy={int(ops.get('blocked_unhealthy', 0) or 0)}"
                )
                lines.append(f"Autoscale hints: {int(ops.get('autoscale_hints', 0) or 0)}")
                lines.append(
                    "HTTP: a/s/fb="
                    f"{int(ops.get('http_attempted', 0) or 0)}/{int(ops.get('http_succeeded', 0) or 0)}/{int(ops.get('http_fallback_smb', 0) or 0)} "
                    f"| Shadow a/ok/fail={int(ops.get('http_shadow_attempted', 0) or 0)}/{int(ops.get('http_shadow_ok', 0) or 0)}/{int(ops.get('http_shadow_fail', 0) or 0)} "
                    f"| lat http={int(ops.get('http_ms_last', 0) or 0)}ms smb={int(ops.get('smb_ms_last', 0) or 0)}ms"
                )

                llm_calls = int(ops.get("llm_calls", 0) or 0)
                llm_in = int(ops.get("llm_input_tokens", 0) or 0)
                llm_cached = int(ops.get("llm_cached_input_tokens", 0) or 0)
                llm_out = int(ops.get("llm_output_tokens", 0) or 0)
                llm_cost = float(ops.get("llm_estimated_cost_usd", 0.0) or 0.0)
                if llm_calls or llm_in or llm_cached or llm_out or llm_cost:
                    lines.append(
                        f"LLM: calls={llm_calls} input_tokens={llm_in} cached_input_tokens={llm_cached} output_tokens={llm_out} est_cost=${llm_cost:.4f}"
                    )
            else:
                lines.append(f"WARNING: No ops metrics file found: {ops_json}")

            # Best-effort canary jitter scan from log tails.
            try:
                canary_hits = 0
                for cand in [
                    logs_dir / "bot_live.log",
                    logs_dir / "slash_live.log",
                    logs_dir / "worker_debug.log",
                    logs_dir / "bot_stdout.log",
                    logs_dir / "bot_stderr.log",
                ]:
                    if not cand.exists():
                        continue
                    try:
                        data = cand.read_bytes()
                        text = data.decode("utf-8", errors="replace")
                        tail = "\n".join(text.splitlines()[-5000:])
                        canary_hits += tail.count("Canary: elevated uncertainty")
                    except Exception:
                        continue
                if canary_hits:
                    lines.append(f"Uncertainty canary: {canary_hits} recent hits (from log tails)")
            except Exception:
                pass

            # Optional tails for fast triage (bounded).
            try:
                n = max(0, int(tail_lines))
            except Exception:
                n = 60
            if n > 0:
                for p in [
                    logs_dir / "bot_live.log",
                    logs_dir / "slash_live.log",
                    logs_dir / "worker_debug.log",
                ]:
                    if not p.exists():
                        continue
                    try:
                        txt = p.read_text(encoding="utf-8", errors="replace")
                        tail = "\n".join(txt.splitlines()[-n:])
                        lines.append("")
                        lines.append(f"--- Tail: {p.name} ({n} lines) ---")
                        lines.append(tail)
                    except Exception:
                        continue

            text_out = "\n".join(lines).rstrip() + "\n"

            try:
                out_path.write_text(text_out, encoding="utf-8")
            except Exception:
                return False

            try:
                latest_path.write_text(text_out, encoding="utf-8")
            except Exception:
                # Non-fatal: history file is the source of truth.
                pass

            self._eod_written_date = date
            return True

    def summary_text(self) -> str:
        with self._lock:
            down_m = int(self._down_s // 60)
            drain_m = int(self._draining_s // 60)
            safe_m = int(self._safemode_s // 60)
            busy_m = int(self._busy_s // 60) if hasattr(self, "_busy_s") else 0
            snap = OpsSnapshot(
                date=self._date,
                max_inflight=self._max_inflight,
                down_minutes=down_m,
                draining_minutes=drain_m,
                safemode_minutes=safe_m,
                busy_minutes=busy_m,
                minutes_down=down_m,
                minutes_draining=drain_m,
                minutes_safe_mode=safe_m,
                minutes_busy=busy_m,
                blocked_rate_limit=int(self._blocked.get("rate_limit", 0)),
                blocked_draining=int(self._blocked.get("draining", 0)),
                blocked_safemode=int(self._blocked.get("safe_mode", 0)),
                blocked_unhealthy=int(self._blocked.get("unhealthy", 0)),
                autoscale_hints=self._autoscale_hints,

                heavy_started=int(self._heavy_started),
                heavy_completed=int(self._heavy_completed),
                heavy_cost_started=int(self._heavy_cost_started),
                heavy_cost_completed=int(self._heavy_cost_completed),

                http_attempted=int(self._http_attempted),
                http_succeeded=int(self._http_succeeded),
                http_fallback_smb=int(self._http_fallback_smb),
                http_shadow_attempted=int(self._http_shadow_attempted),
                http_shadow_ok=int(self._http_shadow_ok),
                http_shadow_fail=int(self._http_shadow_fail),
                http_ms_last=int(self._http_ms_last),
                smb_ms_last=int(self._smb_ms_last),

                llm_calls=int(self._llm_calls),
                llm_input_tokens=int(self._llm_input_tokens),
                llm_cached_input_tokens=int(self._llm_cached_input_tokens),
                llm_output_tokens=int(self._llm_output_tokens),
                llm_estimated_cost_usd=float(self._llm_estimated_cost_usd),
            )
        return (
            f"OPS {snap.date} | max inflight={snap.max_inflight} | down={snap.down_minutes}m | "
            f"draining={snap.draining_minutes}m | safe_mode={snap.safemode_minutes}m | busy={snap.busy_minutes}m | "
            f"heavy={self._heavy_started}/{self._heavy_completed} (cost={self._heavy_cost_started}/{self._heavy_cost_completed}) | "
            f"blocked rate_limit={snap.blocked_rate_limit} draining={snap.blocked_draining} safe_mode={snap.blocked_safemode} unhealthy={snap.blocked_unhealthy} | "
            f"http a/s/fb={self._http_attempted}/{self._http_succeeded}/{self._http_fallback_smb} "
            f"shadow a/ok/fail={self._http_shadow_attempted}/{self._http_shadow_ok}/{self._http_shadow_fail} "
            f"lat http={self._http_ms_last}ms smb={self._smb_ms_last}ms | "
            f"autoscale_hints={snap.autoscale_hints} | "
            f"llm calls={snap.llm_calls} tok_in={snap.llm_input_tokens} tok_cached_in={snap.llm_cached_input_tokens} tok_out={snap.llm_output_tokens} est_cost=${snap.llm_estimated_cost_usd:.4f}"
        )


# Global singleton
OPS_METRICS = OpsMetrics()
