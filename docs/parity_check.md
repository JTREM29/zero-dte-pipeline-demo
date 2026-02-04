# Daily Render Parity Check (OI/IV)

Goal: institutional-grade correctness.

Once per day, render the **same deterministic payload** locally (Controller) and via the Dell HTTP worker, compute SHA256, and alert on mismatch.

## What it does

- Local render: `delivery.oi_iv_render.render_oi_iv_png(...)`
- Worker render: `POST $TNT_WORKER_URL/v1/render/oi_iv` with the same payload
- Compares SHA256 of PNG bytes (strict by default)
- Also computes SHA256 of decoded RGBA pixels (debug signal)
- Writes a JSONL record to `logs/parity_oi_iv.jsonl`
- On mismatch, saves the two PNGs to `artifacts/parity/YYYY-MM-DD/local.png` and `worker.png`

## Run manually

- Python: `python -u scripts/parity_check_oi_iv.py`
- PowerShell wrapper (loads `.env`/`.env.local`): `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/parity_check_oi_iv.ps1`

## Environment

Required:
 - `TNT_WORKER_URL` (example: `http://192.168.1.145:8787`)

Optional:
- `TNT_PARITY_STRICT_BYTES` (default `1`): `1` = fail if PNG bytes differ; `0` = allow OK if pixels match but PNG bytes differ.
- `TNT_PARITY_ALERT_WEBHOOK_URL`: if set, the script will POST a small JSON payload on failures.

## Task Scheduler (copy-paste friendly)

Create the task on the Controller (CLX):

1) Open **Task Scheduler** → **Create Task...**

2) **General**
- Name: `TNT Daily Parity (OI/IV)`
- Select **Run whether user is logged on or not**
- Check **Run with highest privileges**
- Configure for: Windows 10/11

3) **Triggers**
- New... → Begin the task: **On a schedule**
- Daily → set time (e.g. 7:30 AM)

4) **Actions**
- New... → Action: **Start a program**
- Program/script: `powershell.exe`
- Add arguments:
  - `-NoProfile -ExecutionPolicy Bypass -File "C:\Users\jttre\OneDrive\Desktop\ZeroDTE-pipeline\scripts\parity_check_oi_iv.ps1"`
- Start in:
  - `C:\Users\jttre\OneDrive\Desktop\ZeroDTE-pipeline`

5) **Settings**
- If the task fails, restart every: `1 minute` for `5` attempts
- Stop the task if it runs longer than: `5 minutes`

## Where to look

- Stdout: `logs/parity_oi_iv_stdout.log`
- Stderr: `logs/parity_oi_iv_stderr.log`
- JSONL history: `logs/parity_oi_iv.jsonl`
- Mismatch artifacts: `artifacts/parity/YYYY-MM-DD/*.png`
