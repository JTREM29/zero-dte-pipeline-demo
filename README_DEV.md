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
   Fill in IQFeed login, Polygon API key, Discord tokens/webhooks, and any optional providers you plan to exercise.

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

## 7. Multi-Machine Deployment (Polygon WebSocket Control)

When running the pipeline across multiple machines (e.g., CLX for bot + data collection, Worker-01 for render-only), you need to control which machine runs the Polygon WebSocket collector to avoid exceeding connection limits.

**Architecture**:
- **CLX (Main)**: Discord bot, REST ingest, Polygon WS, schedulers, Redis
- **Worker-01 (Render)**: Render worker only (/healthz endpoint)

**Configuration**:

Set `TNT_DISABLE_POLYGON_WS` in your `.env` or `.env.local`:

- On **CLX** (main machine): `TNT_DISABLE_POLYGON_WS=0` (or leave unset) — WebSocket **enabled**
- On **Worker-01** (render worker): `TNT_DISABLE_POLYGON_WS=1` — WebSocket **disabled**

The `polygon_ws_collector` module checks this variable at startup:
```python
# In massive_service/polygon_ws_collector.py
if os.getenv("TNT_DISABLE_POLYGON_WS", "0") == "1":
    print("[polygon_ws_collector] TNT_DISABLE_POLYGON_WS=1, WebSocket disabled on this machine")
    return
```

**Verification**:

To confirm which processes are using Polygon WebSocket, run on each machine:

```powershell
# On CLX
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'polygon_ws_collector|polygon|websocket' } |
  Select-Object ProcessId, CommandLine | Format-Table -AutoSize

# On Worker-01 (after setting TNT_DISABLE_POLYGON_WS=1)
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'polygon_ws_collector|polygon|websocket' } |
  Select-Object ProcessId, CommandLine | Format-Table -AutoSize
```

Worker-01 should show no polygon_ws_collector processes. If both show none and you still get `max_connections` errors, check for duplicate processes on hidden terminals or scheduled tasks.

## 8. Troubleshooting Notes

- **Missing futures context**: confirm the IQFeed collector is writing fresh rows and that `FUTURES_CONTEXT_PATH` points at the expected JSON.
- **Discord rate limits**: the bot respects the 2,000 character limit; if messages are rejected, double-check the template edits didnt exceed the guards.
- **Pytest warnings**: the repo pins `pytest-asyncio` and `pytest-timeout`; rerun `pip install -r requirements-dev.txt` if you see configuration warnings about `asyncio_mode` or `timeout`.

Document updates alongside template or pipeline changes so future contributors can onboard without tribal knowledge.
