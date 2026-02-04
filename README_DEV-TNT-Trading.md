# Developer Operations Guide

This guide captures the day-to-day workflow for running the Zero DTE pipeline locally, refreshing goldens, and validating Discord output before production pushes.

## 1. Environment & Dependencies

1. Install Python 3.12 or 3.13 (matches the CI matrix).
2. Create a virtual environment and install dependencies:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt -r requirements-dev.txt
   ```
3. Copy the sample environment file and populate credentials:
   ```powershell
   Copy-Item .env.example .env
   ```
   Fill in IQFeed login, market data API keys, Discord tokens/webhooks, and any optional integrations you plan to exercise.

## 2. IQFeed Data Collectors

1. Launch **IQConnect** locally and verify the admin/lookup ports match the values from `.env` (defaults provided).
2. Start the SQLite quote collector (writes to `iqfeed_service/market_iqfeed.db`):
   ```powershell
   python -m iqfeed_service.iqfeed_realtime_collector
   ```
   The module reconnects automatically; stop with `Ctrl+C`.

## 3. Running the Discord Bot

### Discord bot (v1 beta profile)

Minimum beta profile (Focus List + Intraday Update + ops-only heartbeat; recap disabled) is enforced via env vars in:

- [run_discord_v1_beta.ps1](run_discord_v1_beta.ps1)
- [stop_discord_bot.ps1](stop_discord_bot.ps1)

Usage:

- Stop any existing instance: `./stop_discord_bot.ps1`
- Start clean (and optionally stop existing first): `./run_discord_v1_beta.ps1 -StopExisting`
- If Discord login fails with `401 Unauthorized`, force a secure reprompt and persist the new token: `./run_discord_v1_beta.ps1 -StopExisting -ResetToken`

If you want the token + canary channel saved (no PowerShell session weirdness), run:

- `./configure_canary_env.ps1`

It will prompt for `DISCORD_BOT_TOKEN` with hidden input and write/update `.env.local`.

Required env vars (must already exist in your shell/session):

- `DISCORD_BOT_TOKEN`
- `DISCORD_CANARY_CHANNEL_ID` (required for the v1 beta launcher)

Optional env vars (feature flags / tuning):

- `TNT_CRYPTO_WATCHLIST` — comma-separated symbols for `/crypto_watchlist` (default: `BTC,ETH,XRP,DOGE,LTC`).
- `TNT_TTL_CRYPTO_WATCHLIST_SEC` — TTL seconds for the text watchlist cache (default: `300`, clamped 30–3600).

### Alert trigger context line (“Edge Clarity”)

When an alert actually triggers, the bot can optionally append one extra line of context, for example:

`🧠 Context: ES: bearish • earnings AMC tomorrow (confirmed) • expected move ±4.2% • News: quiet`

Design constraints:

- Off by default (env-gated)
- Trigger-only (does not run on every evaluation)
- Cache-only (Redis reads only; no new external calls)
- Rate-limited per `alert_id` via Redis TTL to prevent noisy repeats

Enable in `.env.local` (recommended production defaults):

```ini
# Edge Clarity line (trigger messages)
ALERT_CONTEXT_LINE_ENABLED=1
ALERT_CONTEXT_LINE_MAX_TOKENS=100
ALERT_CONTEXT_LINE_COOLDOWN_SEC=300
ALERT_CONTEXT_LINE_INCLUDE_FUTURES=1
ALERT_CONTEXT_LINE_INCLUDE_EARNINGS=1
ALERT_CONTEXT_LINE_INCLUDE_NEWS=1
```

If news ingest isn’t stable yet, set:

```ini
ALERT_CONTEXT_LINE_INCLUDE_NEWS=0
```

Cache sources used:

- Futures: `fut:scores` (derives ES bias from the cached regime; does not include raw prices)
- Earnings: `cal:earnings:{SYM}` (requires `refreshed_utc` and honors `EARNINGS_PREVIEW_STALE_HOURS`; today/tomorrow only)
- News: `news:symbol:{SYM}:last_ts` and `news:market:last_ts` (stamp-only: “just hit / recent / quiet”)

Note: [run_discord_v1_beta.ps1](run_discord_v1_beta.ps1) will also load `DISCORD_BOT_TOKEN` and `DISCORD_CANARY_CHANNEL_ID` from `.env.local` or `.env` if they are not already set in your session. Avoid pasting bot tokens into chat logs.

One-channel canary: the v1 beta launcher always routes ops/heartbeat to `DISCORD_CANARY_CHANNEL_ID` at runtime (no separate ops channel needed).
It also upserts `DISCORD_BOT_TOKEN` and `DISCORD_CANARY_CHANNEL_ID` into `.env.local` once it has non-empty values, so they don't "go blank" on future runs.

For deployment, canary runs, and production operations, follow `docs/autopost_ops.md`. It covers the full checklist (env layouts, DRY_RUN vs STRICT_CONTRACTS behavior, canary validation, and incident response).

**Quick launch**
- From the repo root run `python cli/discord_bot.py` after setting the desired env mode.
- `DRY_RUN=1` keeps posts local while still writing audit JSON.

**Environment modes**

| Mode | Key Vars | Notes |
| --- | --- | --- |
| Local Dev | `DRY_RUN=1`<br>`STRICT_CONTRACTS=1`<br>`DISCORD_CANARY_CHANNEL_ID=...` | Generates renders + audits only (no Discord). Fail-hard on contract violations. |
| Canary | `DRY_RUN=0`<br>`STRICT_CONTRACTS=0`<br>`DISCORD_CANARY_CHANNEL_ID=your_canary_channel_id` | Posts to the staging channel. Fail-soft with fallback messaging. |
| Production | `DRY_RUN=0`<br>`STRICT_CONTRACTS=0` | Posts to production channels. Canary override optional. |

Notes: `DRY_RUN=1` → renders only, `STRICT_CONTRACTS=1` → fail-hard (CI/dev), `STRICT_CONTRACTS=0` → fail-soft (required in prod). Canary mode must use `DRY_RUN=0` so messages send.

### Slash command: /crypto_watchlist

Fast, text-only board showing 1D/7D/30D % change for your configured crypto watchlist. This is explicitly **overlay only** and does not flip Macro Regime action.

### Remote OI offload (Option A: SMB job folders)

The `/oi` command can offload the CPU-heavy render to a separate worker machine and then attach the resulting PNG from `\\TNT2\tnt_results`.

**Controller: 5 quick checks (before switching to the Dell)**

1. Confirm you can see the shares:
   ```powershell
   Test-Path "\\TNT2\tnt_jobs"
   Test-Path "\\TNT2\tnt_results"
   ```
2. If not using `Everyone` permissions, confirm you have mapped credentials:
   ```powershell
   net use \\TNT2\tnt_jobs
   net use \\TNT2\tnt_results
   ```
3. Confirm enqueue writes a job:
   ```powershell
   py -u smb_enqueue_job.py
   ```
   You should see `Wrote job: ...`.
4. Confirm the bot machine can read results:
   ```powershell
   Get-ChildItem "\\TNT2\tnt_results" | Select-Object -First 5 Name,Length
   ```
5. Pin the Dell address in case hostname flakes:
   - `\\192.168.1.145\tnt_jobs`
   - `\\192.168.1.145\tnt_results`

**Controller (Discord bot machine) env vars**

- `TNT_REMOTE_OI_ENABLED=1`
- `TNT_REMOTE_OI_TRANSPORT=smb` (default)
- `TNT_SMB_JOBS_DIR=\\TNT2\tnt_jobs`
- `TNT_SMB_RESULTS_DIR=\\TNT2\tnt_results`

**Worker (Dell render machine)**

- Ensure the worker can read from `tnt_jobs` and write to `tnt_results`.
- Ensure `POLYGON_API_KEY` is set.
- Run:
  ```powershell
  python -m massive_service.smb_worker
  ```

**Quick smoke (no Discord required)**

From the Controller, enqueue a job and wait for the result:
```powershell
python scripts/enqueue_and_wait_smb.py
```

Notes:
- Remote `/oi` currently supports `by=strike` only.

### Remote OI offload (Option B: Redis + shared-folder artifacts)

An alternate transport is supported for remote `/oi` using Redis (queue + heartbeat/result keys) plus a shared folder for artifacts.

- Set `TNT_REMOTE_OI_TRANSPORT=redis` and configure `TNT_REDIS_HOST` / `TNT_REDIS_PORT` and `TNT_ARTIFACTS_SHARE`.
- Worker: `python -m massive_service.redis_worker`
- Controller smoke: `python scripts/enqueue_and_wait.py`

## 4. Smoke Harness & Goldens

- Generate or refresh all scenario outputs (writes under `tests/goldens/`):
  ```powershell
  python scripts/render_posts_smoke.py --all-scenarios
  ```
- Compare current output to goldens (pre-push hook and CI call this):
  ```powershell
  python scripts/render_posts_smoke.py --compare --all-scenarios
  ```

When template copy changes intentionally, regenerate the goldens in one commit titled `Update Discord output goldens (template revision)` so reviewers can focus on content changes.

## 5. Test Shortcuts

Use the convenience wrapper to keep workflows consistent:
```powershell
# Smoke regression only (fast signal)
./scripts/dev.ps1 test-smoke

# Full pytest suite
./scripts/dev.ps1 test
```
The script sets `PYTHONUTF8=1` to match CI and guard against encoding drift.

## 6. CI Expectations

GitHub Actions executes both Python 3.12 and 3.13. Each job:
1. Installs `requirements.txt` + `requirements-dev.txt`.
2. Runs the smoke regression (`tests/test_render_posts_smoke.py`).
3. Runs the full pytest suite.

Keep local runs aligned with the same sequence so failures reproduce quickly.

## 7. Troubleshooting Notes

- **Missing futures context**: confirm the IQFeed collector is writing fresh rows and that `FUTURES_CONTEXT_PATH` points at the expected JSON.
- **Discord rate limits**: the bot respects the 2,000 character limit; if messages are rejected, double-check the template edits didnt exceed the guards.
- **Pytest warnings**: the repo pins `pytest-asyncio` and `pytest-timeout`; rerun `pip install -r requirements-dev.txt` if you see configuration warnings about `asyncio_mode` or `timeout`.

Document updates alongside template or pipeline changes so future contributors can onboard without tribal knowledge.
