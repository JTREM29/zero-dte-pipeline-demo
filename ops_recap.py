from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _tail_text(path: Path, n: int) -> str | None:
    try:
        # Read as bytes to be resilient to mixed encodings.
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except Exception:
        return None

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode(errors="replace")

    lines = text.splitlines(True)
    if not lines:
        return ""
    return "".join(lines[-int(max(0, n)) :])


def _get_int(d: dict[str, Any], *keys: str, default: int = 0) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            try:
                return int(v)
            except Exception:
                pass
    return int(default)


def _get_float(d: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            try:
                return float(v)
            except Exception:
                pass
    return float(default)


def _pct(a: int, b: int) -> str:
    if b <= 0:
        return "n/a"
    return f"{(100.0 * float(a) / float(b)):.0f}%"


def _scan_canary_count(text: str) -> int:
    # Uncertainty canary messages use this stable substring.
    return text.count("Canary: elevated uncertainty")


def main() -> int:
    ap = argparse.ArgumentParser(description="30-second TNT after-close ops recap")
    ap.add_argument("--root", default=os.getcwd(), help="Repo root (default: cwd)")
    ap.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Date YYYY-MM-DD (default: today)",
    )
    ap.add_argument("--tail", type=int, default=60, help="Tail N lines of key logs (default: 60)")
    ap.add_argument("--no-tail", action="store_true", help="Skip log tails")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    date = str(args.date).strip()[:10]

    ops_path = root / "logs" / "ops_metrics" / f"{date}.json"

    # Common log names in this repo.
    log_candidates = [
        root / "logs" / "bot_live.log",
        root / "logs" / "bot_stdout.log",
        root / "logs" / "bot_stderr.log",
        root / "logs" / "slash_live.log",
        root / "logs" / "slash_bot_stdout.log",
        root / "logs" / "slash_bot_stderr.log",
        root / "logs" / "worker_debug.log",
        root / "logs" / "worker_stdout.log",
        root / "logs" / "worker_stderr.log",
        root / "logs" / "delivery_bot_stdout.log",
        root / "logs" / "delivery_bot_stderr.log",
        root / "logs" / "smb_job_bot_stdout.log",
        root / "logs" / "smb_job_bot_stderr.log",
    ]

    print(f"=== TNT Ops Recap ({date}) ===")

    ops = _load_json(ops_path)
    if not ops:
        print(f"WARNING: No ops metrics file found: {ops_path}")
    else:
        down_m = _get_int(ops, "minutes_down", "down_minutes")
        drain_m = _get_int(ops, "minutes_draining", "draining_minutes")
        safe_m = _get_int(ops, "minutes_safe_mode", "safemode_minutes")
        busy_m = _get_int(ops, "minutes_busy", "busy_minutes")

        max_inflight = _get_int(ops, "max_inflight")

        heavy_started = _get_int(ops, "heavy_started")
        heavy_completed = _get_int(ops, "heavy_completed")
        heavy_cost_started = _get_int(ops, "heavy_cost_started")
        heavy_cost_completed = _get_int(ops, "heavy_cost_completed")

        blocked_rate = _get_int(ops, "blocked_rate_limit")
        blocked_draining = _get_int(ops, "blocked_draining")
        blocked_safe = _get_int(ops, "blocked_safe_mode", "blocked_safemode")
        blocked_unhealthy = _get_int(ops, "blocked_unhealthy")
        autoscale = _get_int(ops, "autoscale_hints")

        http_a = _get_int(ops, "http_attempted")
        http_s = _get_int(ops, "http_succeeded")
        http_fb = _get_int(ops, "http_fallback_smb")

        shadow_a = _get_int(ops, "http_shadow_attempted")
        shadow_ok = _get_int(ops, "http_shadow_ok")
        shadow_fail = _get_int(ops, "http_shadow_fail")

        http_ms = _get_float(ops, "http_ms_last")
        smb_ms = _get_float(ops, "smb_ms_last")

        print(f"State minutes: DOWN={down_m} DRAIN={drain_m} SAFE={safe_m} BUSY={busy_m}")
        print(f"Inflight: max={max_inflight}")
        if heavy_started or heavy_completed or heavy_cost_started or heavy_cost_completed:
            print(f"Heavy: {heavy_started}/{heavy_completed} (cost {heavy_cost_started}/{heavy_cost_completed})")
        print(f"Blocked: rate={blocked_rate} drain={blocked_draining} safe={blocked_safe} unhealthy={blocked_unhealthy}")
        print(f"Autoscale hints: {autoscale}")
        if http_a or shadow_a:
            print(
                "HTTP: a/s/fb="
                f"{http_a}/{http_s}/{http_fb} (success {_pct(http_s, http_a)})"
                f" | Shadow a/ok/fail={shadow_a}/{shadow_ok}/{shadow_fail} (ok {_pct(shadow_ok, shadow_a)})"
                f" | lat http={http_ms:.0f}ms smb={smb_ms:.0f}ms"
            )

    # Canary flip scan (best-effort): count occurrences in available logs.
    canary_hits = 0
    for p in log_candidates:
        t = _tail_text(p, n=5000)
        if t:
            canary_hits += _scan_canary_count(t)
    if canary_hits:
        print(f"Uncertainty canary: {canary_hits} recent hits (from log tails)")

    if not args.no_tail:
        for p in log_candidates:
            t = _tail_text(p, n=int(args.tail))
            if t is None:
                continue
            print(f"\n--- Tail: {p.relative_to(root)} ({args.tail} lines) ---")
            print(t)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
