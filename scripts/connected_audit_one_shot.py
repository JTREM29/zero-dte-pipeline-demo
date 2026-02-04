from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _now() -> int:
    return int(time.time())


def _print(section: str, msg: str) -> None:
    print(f"[{section}] {msg}")


def _fail(msg: str) -> None:
    _print("FAIL", msg)


def _ok(msg: str) -> None:
    _print("OK", msg)


def _warn(msg: str) -> None:
    _print("WARN", msg)


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _dotenv_get_value(path: Path, key: str) -> str | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        val = v.strip()
        if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'"))):
            val = val[1:-1]
        return val.strip()
    return None


def _get_setting(key: str) -> str:
    raw = _env(key)
    if raw:
        return raw

    try:
        root = Path(__file__).resolve().parents[1]
    except Exception:
        root = Path.cwd()

    for env_path in (root / ".env.local", root / ".env"):
        val = _dotenv_get_value(env_path, key)
        if val:
            return val
    return ""


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_text_tail(path: Path, n: int = 120) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-max(1, n) :])
    except Exception:
        return ""


def _http_get_json(url: str, timeout_s: int = 6) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tnt-connected-audit"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
        obj = json.loads(data.decode("utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _http_get_bytes(url: str, timeout_s: int = 10) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tnt-connected-audit"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        _warn(f"http_error url={url} code={exc.code}")
        return None
    except Exception:
        return None


def _win_ps_json(command: str, timeout_s: int = 8) -> Any:
    try:
        p = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        raw = (p.stdout or "").strip()
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _check_bot_processes() -> tuple[bool, str]:
    if not sys.platform.startswith("win"):
        return True, "skipped (non-windows)"

    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^python' -and $_.CommandLine -and $_.CommandLine -match '(cli\\.discord_bot|delivery\\.discord_bot)' } | "
        "Select-Object ProcessId, ParentProcessId, ExecutablePath, CommandLine | ConvertTo-Json"
    )
    obj = _win_ps_json(ps)
    if obj is None:
        return False, "unable to query processes"

    procs: list[dict[str, Any]]
    if isinstance(obj, list):
        procs = [p for p in obj if isinstance(p, dict)]
    elif isinstance(obj, dict):
        procs = [obj]
    else:
        procs = []

    if not procs:
        return False, "no discord bot process found"

    # Windows quirk: depending on install/venv, a python.exe launcher process may spawn a
    # python3.x.exe child with the same commandline and remain resident. Treat that as
    # a single bot instance by counting only leaf processes.
    try:
        parents = {int(p.get("ParentProcessId")) for p in procs if str(p.get("ParentProcessId") or "").isdigit()}
        leaf = [p for p in procs if int(p.get("ProcessId")) not in parents]
        if leaf:
            procs = leaf
    except Exception:
        pass

    if len(procs) > 1:
        pids = [str(p.get("ProcessId")) for p in procs]
        return False, f"multiple bot processes running pids={pids} (stop extras)"

    p0 = procs[0]
    pid = p0.get("ProcessId")
    exe = (p0.get("ExecutablePath") or "").strip()
    return True, f"pid={pid} exe={exe}"


def _check_env() -> tuple[bool, list[str]]:
    missing: list[str] = []

    ask_id = _get_setting("TNT_ASK_CHANNEL_ID") or _get_setting("ASK_TNT_CHANNEL_ID")
    if not ask_id or ask_id in {"0", "(unset)", "(missing)"}:
        missing.append("TNT_ASK_CHANNEL_ID")

    # Not strictly required, but it’s how you get deterministic no-@ routing.
    if not _truthy(_get_setting("TNT_ASK_CHANNEL_ONLY_ENABLED") or "0"):
        missing.append("TNT_ASK_CHANNEL_ONLY_ENABLED (expected 1)")

    token = _get_setting("DISCORD_BOT_TOKEN") or _get_setting("DISCORD_TOKEN")
    if not token:
        missing.append("DISCORD_BOT_TOKEN")

    return (len(missing) == 0), missing


def _check_bot_log() -> tuple[bool, str]:
    path = Path("logs") / "slash_live.log"
    if not path.exists():
        return False, "missing logs/slash_live.log"

    # Read the last chunk of the file to find the login line.
    try:
        size = int(path.stat().st_size)
        read_bytes = min(size, 256_000)
        with path.open("rb") as f:
            if size > read_bytes:
                f.seek(size - read_bytes)
            chunk = f.read(read_bytes)
        tail = chunk.decode("utf-8", errors="ignore")
    except Exception:
        tail = _read_text_tail(path, 400)

    if "Logged in as" not in tail:
        return False, "slash_live.log tail missing 'Logged in as' (bot may not be connected or log is truncated)"

    return True, "logged in (see logs/slash_live.log)"


def _check_local_heartbeat_file(max_age_s: int = 180) -> tuple[bool, str]:
    path = Path(_get_setting("TNT_HEARTBEAT_PATH") or "logs/tnt_heartbeat.json")
    if not path.exists():
        return False, f"missing {path.as_posix()}"

    try:
        age = _now() - int(path.stat().st_mtime)
    except Exception:
        return False, "heartbeat file stat failed"

    if age > max_age_s:
        return False, f"heartbeat file stale age={age}s max_age_s={max_age_s}"

    return True, f"heartbeat file fresh age={age}s"


def _run_futures_proof() -> tuple[bool, str]:
    cmd = [sys.executable, "-u", "scripts/futures_ops_proof_one_shot.py"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        return False, f"failed to run futures proof: {exc}"

    out = (p.stdout or "") + "\n" + (p.stderr or "")
    out = out.strip()
    last = " | ".join([ln.strip() for ln in out.splitlines()[-6:] if ln.strip()])

    if p.returncode == 0:
        return True, f"exit=0 {last[:900]}"
    return False, f"exit={p.returncode} {last[:900]}"


def _check_worker(worker_base: str, *, smoke_path: Path) -> tuple[bool, str]:
    base = (worker_base or "").strip().rstrip("/")
    if not base:
        return False, "missing worker url"

    hz = _http_get_json(f"{base}/healthz")
    if not hz or not bool(hz.get("ok")):
        return False, f"healthz not ok url={base}/healthz"

    data = _http_get_bytes(f"{base}/v1/render/smoke")
    if not data or len(data) < 1000:
        return False, f"smoke render failed url={base}/v1/render/smoke"

    try:
        smoke_path.parent.mkdir(parents=True, exist_ok=True)
        smoke_path.write_bytes(data)
    except Exception as exc:
        return False, f"failed to write smoke file: {exc}"

    return True, f"healthz ok build={hz.get('build')} smoke_bytes={len(data)}"


def main() -> int:
    # Run from repo root if possible.
    try:
        repo_root = Path(__file__).resolve().parents[1]
        os.chdir(repo_root)
    except Exception:
        pass

    _print("AUDIT", "Connected Audit (CLX) — deterministic checks")

    ok_all = True

    ok, msg = _check_bot_processes()
    if ok:
        _ok(f"discord bot process: {msg}")
    else:
        _fail(f"discord bot process: {msg}")
        ok_all = False

    ok, msg = _check_bot_log()
    if ok:
        _ok(f"discord gateway: {msg}")
    else:
        _warn(f"discord gateway: {msg}")
        # Don't hard-fail on log path; process check above is the primary proof.

    env_ok, missing = _check_env()
    if env_ok:
        ask_id = _get_setting("TNT_ASK_CHANNEL_ID") or _get_setting("ASK_TNT_CHANNEL_ID")
        _ok(
            "ask env: "
            f"TNT_ASK_CHANNEL_ID={ask_id} "
            f"channel_only={_get_setting('TNT_ASK_CHANNEL_ONLY_ENABLED') or '0'} "
            f"cooldown={_get_setting('TNT_ASK_CHANNEL_COOLDOWN_SEC') or '(default)'}"
        )
    else:
        _fail(f"env missing/mis-set: {missing}")
        ok_all = False

    ok, msg = _run_futures_proof()
    if ok:
        _ok(f"futures proof: {msg}")
    else:
        _fail(f"futures proof: {msg}")
        ok_all = False

    worker_url = _get_setting("TNT_WORKER_URL") or "http://192.168.1.145:8787"
    ok, msg = _check_worker(worker_url, smoke_path=Path("tmp") / "worker_smoke.png")
    if ok:
        _ok(f"worker render: url={worker_url} {msg}")
    else:
        _fail(f"worker render: url={worker_url} {msg}")
        ok_all = False

    ok, msg = _check_local_heartbeat_file()
    if ok:
        _ok(f"bot heartbeat: {msg}")
    else:
        _warn(f"bot heartbeat: {msg}")

    _print(
        "MANUAL",
        "Discord checks (interactive): in #ask-tnt send 'macro posture?' and run /macro_pulse and /oi SPY.",
    )

    return 0 if ok_all else 2


if __name__ == "__main__":
    raise SystemExit(main())
