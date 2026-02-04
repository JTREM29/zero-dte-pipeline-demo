#!/usr/bin/env python3
"""
Discord SMB Job Bot

- Exposes a slash command to enqueue a simple SMB job to Dell (TNT2) and wait for results
- Uses controller.enqueue_and_wait utilities under the hood

Env required:
  DISCORD_BOT_TOKEN = <bot token>
Optional env for paths (use mapped drives recommended):
  TNT2_JOBS_DRIVE=Z:
  TNT2_RESULTS_DRIVE=Y:
  # or UNC fallback
  TNT2_HOST=TNT2

Run:
  py -u bots/discord_smb_job_bot.py
"""
from __future__ import annotations

import asyncio
import os
import json
import hashlib
import re
import time
from typing import Dict, List, Optional

import discord
from discord import app_commands

# Reuse controller helpers
from controller.enqueue_and_wait import enqueue_job, wait_for_result, list_artifacts, resolve_paths

INTENTS = discord.Intents.default()
CLIENT = discord.Client(intents=INTENTS)
TREE = app_commands.CommandTree(CLIENT)

# ---------------- Throttle, dedupe, and observability -----------------

USER_COOLDOWN_S = int(os.getenv("DISCORD_OI_USER_COOLDOWN", "45"))
CHANNEL_CONCURRENCY = int(os.getenv("DISCORD_OI_CHANNEL_CONCURRENCY", "1"))
GLOBAL_CONCURRENCY = int(os.getenv("DISCORD_OI_GLOBAL_CONCURRENCY", "5"))
DEDUPE_WINDOW_S = int(os.getenv("DISCORD_OI_DEDUPE_WINDOW", "30"))
WAIT_TIMEOUT_S = int(os.getenv("DISCORD_OI_WAIT_TIMEOUT", "90"))

_lock = asyncio.Lock()
_user_last: Dict[int, float] = {}
_chan_active: Dict[int, int] = {}
_global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
_inflight: Dict[str, Dict[str, object]] = {}
_dedupe_hit = 0
_dedupe_miss = 0
_last_error_msg: Optional[str] = None
_last_error_ts: float = 0.0

_SYMBOL_RE = re.compile(r"^[A-Z.\-]{1,10}$")
_ALLOWED_ROLE_IDS = {int(x) for x in os.getenv("DISCORD_ALLOWED_ROLE_IDS", "").split(",") if x.strip().isdigit()}


def _validate_symbol(sym: str) -> Optional[str]:
    s = (sym or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(s):
        return None
    allow = os.getenv("DISCORD_OI_ALLOWLIST", "").strip()
    if allow:
        allowed = {x.strip().upper() for x in allow.split(",") if x.strip()}
        if s not in allowed:
            return None
    return s


def _has_allowed_role(inter: discord.Interaction) -> bool:
    # Admins always allowed
    if _is_admin(inter):
        return True
    if not _ALLOWED_ROLE_IDS:
        return True  # if no roles configured, allow everyone
    try:
        roles = getattr(inter.user, "roles", []) or []
        role_ids = {int(getattr(r, "id", 0)) for r in roles}
        return bool(role_ids & _ALLOWED_ROLE_IDS)
    except Exception:
        return False


def _audit_log(entry: Dict[str, object]) -> None:
    try:
        from pathlib import Path
        import json
        logs = Path("logs")
        logs.mkdir(exist_ok=True)
        (logs / "smb_bot_audit.jsonl").open("a", encoding="utf-8").write(
            f"{__import__('json').dumps(entry, separators=(',',':'))}\n"
        )
    except Exception:
        pass


def _params_hash(payload: Dict[str, object]) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha1(blob).hexdigest()[:8]
    except Exception:
        return "00000000"


def _summary(job_id: str, artifacts: List[str]) -> str:
    art_lines = "\n".join(f"- {a}" for a in artifacts)
    return f"Job {job_id} completed.\nArtifacts:\n{art_lines}" if artifacts else f"Job {job_id} completed."


@TREE.command(name="smb_smoke", description="Enqueue a simple SMB job to Dell and wait for result")
@app_commands.describe(note="Optional note to include in the job")
async def smb_smoke(interaction: discord.Interaction, note: str | None = None):
    await interaction.response.defer(thinking=True, ephemeral=True)

    loop = asyncio.get_running_loop()

    try:
        # Offload blocking I/O to a thread
        job_id, jobs_dir, results_dir = await loop.run_in_executor(None, enqueue_job, "smoke", {"note": note or "from discord"})

        # Wait for result (blocking) in executor to avoid blocking event loop
        rc = await loop.run_in_executor(None, wait_for_result, results_dir, job_id, 120)

        artifacts = await loop.run_in_executor(None, list_artifacts, results_dir, job_id)

        msg = _summary(job_id, artifacts)
        files: List[discord.File] = []
        for path in artifacts:
            # Limit to small text/json artifacts to avoid large uploads; adjust as needed
            if path.lower().endswith((".txt", ".json")):
                try:
                    files.append(discord.File(path))
                except Exception:
                    pass

        await interaction.followup.send(content=msg, files=files, ephemeral=False)
    except Exception as exc:
        await interaction.followup.send(content=f"Error submitting job: {exc}", ephemeral=True)
        # record last error
        global _last_error_msg, _last_error_ts
        _last_error_msg = f"smb_smoke: {exc}"
        _last_error_ts = time.time()


def _is_admin(inter: discord.Interaction) -> bool:
    try:
        gp = getattr(inter.user, "guild_permissions", None)
        return bool(gp and gp.administrator)
    except Exception:
        return False


def _filter_artifacts(paths: List[str], allow_debug: bool) -> List[discord.File]:
    files: List[discord.File] = []
    png_added = False
    txt_added = False
    # Prefer summary txt specifically if present
    # sort: images first, then *_summary.txt, then other txt/json
    def _rank(p: str) -> int:
        low = p.lower()
        if low.endswith((".png", ".jpg", ".jpeg")):
            return 0
        if low.endswith("__summary.txt") or low.endswith("summary.txt"):
            return 1
        if low.endswith(".txt"):
            return 2
        if low.endswith((".json", ".log")):
            return 3
        return 9
        task: asyncio.Task
        shared = False
    for path in sorted(paths, key=_rank):
        low = path.lower()
        try:
            if low.endswith((".png", ".jpg", ".jpeg")) and not png_added:
                files.append(discord.File(path))
                shared = True
                global _dedupe_hit
                _dedupe_hit += 1
                png_added = True
            elif (low.endswith("__summary.txt") or low.endswith("summary.txt") or low.endswith(".txt")) and not txt_added:
                if os.path.getsize(path) <= 512_000:
                global _dedupe_miss
                _dedupe_miss += 1
                    files.append(discord.File(path))
                    txt_added = True
            elif allow_debug and low.endswith((".json", ".log")):
                if os.path.getsize(path) <= 2_000_000:
                    files.append(discord.File(path))
        except Exception:
            pass
    return files


@TREE.command(name="oi", description="Render OI remotely on Dell and post result (prototype)")
@app_commands.describe(symbol="Underlying symbol, e.g. SPY", debug="Attach extra JSON/debug artifacts (admin only)")
async def oi(interaction: discord.Interaction, symbol: str, debug: bool = False):
    await interaction.response.defer(thinking=True, ephemeral=True)

    # Security: validate symbol and allowlist
    norm = _validate_symbol(symbol)
    if not norm:
        await interaction.followup.send(content="Invalid or disallowed symbol.", ephemeral=True)
        return

    # Role gate: Traders role or Admin
    if not _has_allowed_role(interaction):
        await interaction.followup.send(content="You need the Traders role to run renders. Ask a mod.", ephemeral=True)
        dedupe_str = "HIT" if shared else "MISS"
        content = f"**{norm}** OI job `{job_id}` {status}. Dedupe: {dedupe_str}"

    user_id = int(interaction.user.id)
    chan_id = int(interaction.channel_id) if interaction.channel_id else 0
    now = time.time()

    # Throttles and concurrency under lock
    async with _lock:
        last = _user_last.get(user_id, 0.0)
        if now - last < USER_COOLDOWN_S:
            wait = int(USER_COOLDOWN_S - (now - last))
            await interaction.followup.send(content=f"Cooldown: try again in ~{wait}s.", ephemeral=True)
            return
        cnt = _chan_active.get(chan_id, 0)
        if cnt >= CHANNEL_CONCURRENCY:
            await interaction.followup.send(content="Already rendering OI for this channelâ€”try again shortly.", ephemeral=True)
            return
        _chan_active[chan_id] = cnt + 1
        _user_last[user_id] = now

    # Global concurrency gate (outside lock)
    await _global_sem.acquire()

    loop = asyncio.get_running_loop()

    # De-dupe key within window
    key = f"oi:{norm}:{int(now//DEDUPE_WINDOW_S)}"

    async def _run_job() -> tuple[int, str, List[str], str]:
        # returns rc, job_id, artifacts, results_dir
        # Prepare summary hints (controller-provided)
        summary = {"cache": "MISS", "dedupe": "MISS"}
        payload = {"symbol": norm, "summary": summary}
        job_id, jobs_dir, results_dir, routed_name, routed_reason = await loop.run_in_executor(None, enqueue_job, "render_oi", payload)
        # progress ping (ephemeral)
        try:
            used = GLOBAL_CONCURRENCY - int(getattr(_global_sem, "_value", 0))
            pos = _chan_active.get(chan_id, 1)
            await interaction.followup.send(
                content=(
                    f"Queued (#{pos}) | inflight {used}/{GLOBAL_CONCURRENCY} | "
                    f"Routed: {str(routed_name).upper()} ({routed_reason})"
                )[:1800],
                ephemeral=True,
            )
        except Exception:
            pass
        rc = await loop.run_in_executor(None, wait_for_result, results_dir, job_id, WAIT_TIMEOUT_S)
        artifacts = await loop.run_in_executor(None, list_artifacts, results_dir, job_id)
        return rc, job_id, artifacts, results_dir

    created = time.time()
    task: asyncio.Task
    async with _lock:
        entry = _inflight.get(key)
        if entry and isinstance(entry.get("task"), asyncio.Task) and (created - float(entry.get("created", created))) <= DEDUPE_WINDOW_S:
            task = entry["task"]  # type: ignore[assignment]
            entry["waiters"] = int(entry.get("waiters", 0)) + 1
            global _dedupe_hit
            _dedupe_hit += 1
        else:
            task = loop.create_task(_run_job())
            _inflight[key] = {"task": task, "created": created, "waiters": 1}
            global _dedupe_miss
            _dedupe_miss += 1

    try:
        rc, job_id, artifacts, _results_dir = await task
    except Exception as exc:
        global _last_error_msg, _last_error_ts
        _last_error_msg = f"oi await: {exc}"
        _last_error_ts = time.time()
        raise
    finally:
        async with _lock:
            # decrement channel active and clear inflight if no waiters
            _chan_active[chan_id] = max(0, _chan_active.get(chan_id, 1) - 1)
            ent = _inflight.get(key)
            if ent:
                ent["waiters"] = max(0, int(ent.get("waiters", 1)) - 1)
                if int(ent["waiters"]) == 0:
                    _inflight.pop(key, None)
        _global_sem.release()

    # Prepare attachments with policy limits
    allow_debug = _is_admin(interaction) and bool(debug)
    files = _filter_artifacts(artifacts, allow_debug)

    status = "succeeded" if rc == 0 else ("timeout" if rc == 2 else "failed")
    content = f"**{norm}** OI job `{job_id}` {status}."
    await interaction.followup.send(content=content, files=files, ephemeral=False)

    # Audit log
    _audit_log({
        "t": int(time.time()),
        "cmd": "oi",
        "symbol": norm,
        "user": user_id,
        "channel": chan_id,
        "status": status,
        "artifacts": artifacts,
        "key": key,
    })


@TREE.command(name="jobstatus", description="Check status/result files for a given job id")
@app_commands.describe(job_id="The job id (from the queue/oi response)")
async def jobstatus(interaction: discord.Interaction, job_id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        _jobs, results = resolve_paths()
        result_json = os.path.join(results, f"{job_id}.result.json")
        hb = os.path.join(results, f"{job_id}.heartbeat")
        artifacts = list_artifacts(results, job_id)
        content_lines = []
        if os.path.exists(result_json):
            content_lines.append(f"result.json present: {result_json}")
        if os.path.exists(hb):
            content_lines.append(f"heartbeat present: {hb}")
        content_lines.append(f"artifact_count: {len(artifacts)}")
        files = _filter_artifacts(artifacts, allow_debug=_is_admin(interaction))
        await interaction.followup.send("\n".join(content_lines)[:1800], files=files, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"jobstatus error: {exc}", ephemeral=True)


@TREE.command(name="queue", description="Show inflight queue state (admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def queue(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with _lock:
        inflight_n = len(_inflight)
        oldest_age = 0
        now = time.time()
        type_counts = {"oi": 0, "tc": 0, "bias": 0, "other": 0}
        for k, v in _inflight.items():
            try:
                oldest_age = max(oldest_age, int(now - float(v.get("created", now))))
            except Exception:
                pass
            t = "other"
            if k.startswith("oi:"):
                t = "oi"
            elif k.startswith("render:"):
                parts = k.split(":", 3)
                if len(parts) > 1 and parts[1] in ("oi", "tc", "bias"):
                    t = parts[1]
            type_counts[t] = type_counts.get(t, 0) + 1
        global_used = GLOBAL_CONCURRENCY - int(getattr(_global_sem, "_value", 0))
        # top channels
        top = sorted(((cid, cnt) for cid, cnt in _chan_active.items() if cnt > 0), key=lambda x: x[1], reverse=True)[:3]
        top_str = ", ".join([f"#{cid}={cnt}" for cid, cnt in top]) or "none"
        # dedupe stats and last error
        hits = _dedupe_hit
        misses = _dedupe_miss
        if _last_error_msg and _last_error_ts:
            last_err = f"{_last_error_msg} @ {int(now - _last_error_ts)}s ago"
        else:
            last_err = "none"
        msg = (
            f"inflight {global_used}/{GLOBAL_CONCURRENCY} | oldest {oldest_age}s\n"
            f"by_type: oi={type_counts['oi']} tc={type_counts['tc']} bias={type_counts['bias']}\n"
            f"top_channels: {top_str}\n"
            f"dedupe: hit={hits} miss={misses}\n"
            f"last_error: {last_err}"
        )

    # Best-effort: include per-worker SMB pressure probes (Dell primary / CLX overflow)
    # so admins can see why routing is choosing overflow.
    try:
        from controller.worker_pool import build_default_targets, probe_worker

        primary, secondary = build_default_targets()
        p = probe_worker(primary)
        s = probe_worker(secondary)

        def _fmt_probe(name: str, pr) -> str:
            hb = "?" if pr.hb_age_s is None else f"{int(pr.hb_age_s)}s"
            oldest = "?" if pr.oldest_job_age_s is None else f"{int(pr.oldest_job_age_s)}s"
            ok = "OK" if pr.ok else "DOWN"
            return f"{name}: {ok} ({pr.reason}) | depth={pr.queue_depth} oldest={oldest} hb={hb}"

        msg = msg + "\n" + _fmt_probe(str(primary.name).upper(), p) + "\n" + _fmt_probe(str(secondary.name).upper(), s)
    except Exception:
        pass
    await interaction.followup.send(msg, ephemeral=True)


@TREE.command(name="render", description="Render remotely on Dell with constrained options")
@app_commands.describe(
    job_type="oi|tc|bias",
    symbol="Underlying symbol, e.g. SPY",
    interval="Bar interval, e.g. 1m",
    days="Lookback days (e.g., 1)",
    top="Top N (e.g., 18)",
    private="If true, final message is ephemeral",
    debug="Attach extra JSON/debug artifacts (admin only)",
)
async def render(
    interaction: discord.Interaction,
    job_type: str,
    symbol: str,
    interval: str = "1m",
    days: int = 1,
    top: int = 18,
    private: bool = False,
    debug: bool = False,
):
    await interaction.response.defer(thinking=True, ephemeral=True)

    jt = (job_type or "").strip().lower()
    if jt not in {"oi", "tc", "bias"}:
        await interaction.followup.send("job_type must be one of oi|tc|bias", ephemeral=True)
        return

    # Type enable allowlist (env): default only 'oi' enabled
    enabled = {x.strip().lower() for x in os.getenv("DISCORD_RENDER_TYPES_ENABLED", "oi").split(",") if x.strip()}
    if jt not in enabled:
        await interaction.followup.send(f"'{jt}' render not enabled yet.", ephemeral=True)
        return

    # Validate symbol
    norm = _validate_symbol(symbol)
    if not norm:
        await interaction.followup.send(content="Invalid or disallowed symbol.", ephemeral=True)
        return

    if not _has_allowed_role(interaction):
        await interaction.followup.send(content="You need the Traders role to run renders. Ask a mod.", ephemeral=True)
        return

    # Clamp interval/days/top
    interval = (interval or "1m").lower()
    if interval not in {"1m", "5m", "15m"}:
        await interaction.followup.send("interval must be one of 1m|5m|15m", ephemeral=True)
        return
    days = max(1, min(5, int(days)))
    top = max(5, min(40, int(top)))

    user_id = int(interaction.user.id)
    chan_id = int(interaction.channel_id) if interaction.channel_id else 0
    now = time.time()

    # Throttle checks
    async with _lock:
        last = _user_last.get(user_id, 0.0)
        if now - last < USER_COOLDOWN_S:
            wait = int(USER_COOLDOWN_S - (now - last))
            await interaction.followup.send(content=f"Cooldown: try again in ~{wait}s.", ephemeral=True)
            return
        cnt = _chan_active.get(chan_id, 0)
        if cnt >= CHANNEL_CONCURRENCY:
            await interaction.followup.send(content="Already rendering in this channelâ€”try again shortly.", ephemeral=True)
            return
        _chan_active[chan_id] = cnt + 1
        _user_last[user_id] = now

    await _global_sem.acquire()

    loop = asyncio.get_running_loop()
    payload = {"symbol": norm, "interval": interval, "days": int(days), "top": int(top)}
    params_sig = _params_hash(payload)
    key = f"render:{jt}:{norm}:{interval}:{days}:{top}:{int(now//DEDUPE_WINDOW_S)}:{params_sig}"

    async def _run_job() -> tuple[int, str, List[str]]:
        job_map = {"oi": "render_oi", "tc": "render_trade_context", "bias": "render_bias_pack"}
        job_name = job_map.get(jt, f"render_{jt}")
        # add controller-provided summary hints
        payload_with_summary = dict(payload)
        payload_with_summary["summary"] = {"cache": "MISS", "dedupe": "MISS"}
        job_id, jobs_dir, results_dir, routed_name, routed_reason = await loop.run_in_executor(
            None,
            enqueue_job,
            job_name,
            payload_with_summary,
        )
        try:
            used = GLOBAL_CONCURRENCY - int(getattr(_global_sem, "_value", 0))
            pos = _chan_active.get(chan_id, 1)
            await interaction.followup.send(
                content=f"Queued (#{pos}) | inflight {used}/{GLOBAL_CONCURRENCY} | Routed: {routed_name} ({routed_reason})",
                ephemeral=True,
            )
        except Exception:
            pass
        rc = await loop.run_in_executor(None, wait_for_result, results_dir, job_id, WAIT_TIMEOUT_S)
        artifacts = await loop.run_in_executor(None, list_artifacts, results_dir, job_id)
        return rc, job_id, artifacts, routed_name, routed_reason

    created = time.time()
    task: asyncio.Task
    shared = False
    async with _lock:
        entry = _inflight.get(key)
        if entry and isinstance(entry.get("task"), asyncio.Task) and (created - float(entry.get("created", created))) <= DEDUPE_WINDOW_S:
            task = entry["task"]  # type: ignore[assignment]
            entry["waiters"] = int(entry.get("waiters", 0)) + 1
            shared = True
            global _dedupe_hit
            _dedupe_hit += 1
        else:
            task = loop.create_task(_run_job())
            _inflight[key] = {"task": task, "created": created, "waiters": 1}
            global _dedupe_miss
            _dedupe_miss += 1

    try:
        rc, job_id, artifacts, routed_name, routed_reason = await task
    except Exception as exc:
        global _last_error_msg, _last_error_ts
        _last_error_msg = f"render await: {exc}"
        _last_error_ts = time.time()
        raise
    finally:
        async with _lock:
            _chan_active[chan_id] = max(0, _chan_active.get(chan_id, 1) - 1)
            ent = _inflight.get(key)
            if ent:
                ent["waiters"] = max(0, int(ent.get("waiters", 1)) - 1)
                if int(ent["waiters"]) == 0:
                    _inflight.pop(key, None)
        _global_sem.release()

    allow_debug = _is_admin(interaction) and bool(debug)
    files = _filter_artifacts(artifacts, allow_debug)
    status = "succeeded" if rc == 0 else ("timeout" if rc == 2 else "failed")
    header = f"RENDER â€” {jt.upper()} | {norm} | {interval} | {days}d | top={top}"
    cache = "SHARED" if shared else "MISS"
    content = (
        f"{header}\n"
        f"Job: {job_id} | cache: {cache} | timeout: {WAIT_TIMEOUT_S}s\n"
        f"Routed: {routed_name} ({routed_reason})\n"
        f"Status: {status}"
    )
    await interaction.followup.send(content=content, files=files, ephemeral=bool(private))

    _audit_log({
        "t": int(time.time()),
        "cmd": "render",
        "job_type": jt,
        "symbol": norm,
        "user": user_id,
        "channel": chan_id,
        "status": status,
        "artifacts": artifacts,
        "key": key,
        "params_sig": params_sig,
        "interval": interval,
        "days": days,
        "top": top,
        "shared": shared,
    })


@CLIENT.event
async def on_ready():
    try:
        await TREE.sync()
    except Exception:
        pass
    print(f"Discord SMB Job Bot ready as {CLIENT.user} (synced commands)")


def main():
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN not set")
    CLIENT.run(token)


if __name__ == "__main__":
    main()

