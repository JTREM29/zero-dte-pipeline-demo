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

- Preferred: canonical ctx snapshots (when enabled)
   - Market: `ctx:market`
   - Per-symbol: `ctx:sym:{SYM}`

- Futures: `fut:scores` (derives ES bias from the cached regime; does not include raw prices)
- Earnings: `cal:earnings:{SYM}` (requires `refreshed_utc` and honors `EARNINGS_PREVIEW_STALE_HOURS`; today/tomorrow only)
- News: `news:symbol:{SYM}:last_ts` and `news:market:last_ts` (stamp-only: “just hit / recent / quiet”)

Legacy keys above remain as fallback sources while `CTX_SNAPSHOT_STRICT=off|shadow`. In strict mode, UI context is omitted rather than falling back.

### Context Snapshots (`ctx:*`) rollout (recommended)

TNT can optionally build context snapshots in Redis (`ctx:market`, `ctx:sym:{SYM}`) so alerts, previews, gates, status, and trigger messaging all use the same canonical context (futures bias, earnings risk, news state).

Messaging contract (immutable): see `docs/AI_TRADE_CANDIDATES_CONTRACT.md`.

**Enable snapshots (safe)**

```ini
CTX_SNAPSHOT_ENABLED=1

# optional
CTX_SNAPSHOT_SYMBOLS=SPY,QQQ,IWM,AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL
CTX_SNAPSHOT_INTERVAL_SEC=30
CTX_SNAPSHOT_TTL_SEC=120
CTX_SNAPSHOT_EARNINGS_STALE_HOURS=48
CTX_CANDIDATES_APPROVED_CAP=5
```

**Strict modes (cutover is reversible)**

```ini
# off (default): consumers prefer ctx but may fallback
CTX_SNAPSHOT_STRICT=0

# shadow: allow fallback, but record ctx misses (recommended first)
CTX_SNAPSHOT_STRICT=shadow

# on: strict (no fallback); risk gates fail-closed if ctx missing/stale
CTX_SNAPSHOT_STRICT=1
```

**Recommended rollout**

Start in shadow mode during market hours:

```ini
CTX_SNAPSHOT_ENABLED=1
CTX_SNAPSHOT_STRICT=shadow
```

Monitor health with:

- `/context_status` (ephemeral): hb age, last errors, miss counters (`ctx:miss:*`, `ctx:miss_last:*`)
- `/alerts_status`: includes “Context (ctx:*)” and strict mode

If ctx misses stop increasing under normal load, flip to strict:

```ini
CTX_SNAPSHOT_STRICT=1
```

Emergency revert (instant):

```ini
CTX_SNAPSHOT_STRICT=shadow
```

**Miss telemetry (debug)**

When ctx is enabled, consumers record misses to Redis:

- `ctx:miss:{consumer}`
- `ctx:miss_last:{consumer}`

Global last miss (also written):

- `ctx:miss_last`

Common consumer tags:

`edge_clarity`, `preview_footer`, `earnings_blackout_gate`, `news_blackout_gate`,
`futures_conflict_gate`, `alerts_status`

### Go-live: enable ctx + news + futures + ops recaps (checklist)

Create your channels:

- `#calendar-earnings`
- (Optional) `#tnt-ops` (you can start with one channel; split later if volume warrants)

When you have your channel ID, set these in `.env.local`:

```ini
# Context snapshots
CTX_SNAPSHOT_ENABLED=1
CTX_SNAPSHOT_STRICT=shadow
CTX_SNAPSHOT_INTERVAL_SEC=30
CTX_SNAPSHOT_TTL_SEC=120

# Earnings autoposts
EARNINGS_AUTOPOST_ENABLED=1
EARNINGS_POST_CHANNEL_ID=YOUR_CHANNEL_ID
EARNINGS_PRECLOSE_ENABLED=1

# News broadcast (no spam)
NEWS_BROADCAST_ENABLED=1
NEWS_BROADCAST_CHANNEL_ID=YOUR_CHANNEL_ID
NEWS_BROADCAST_COOLDOWN_SEC=600

# Futures broadcast (bias flip only)
FUTURES_BROADCAST_ENABLED=1
FUTURES_BROADCAST_CHANNEL_ID=YOUR_CHANNEL_ID
FUTURES_BROADCAST_COOLDOWN_SEC=900

# Ops recaps
OPS_DAILY_RECAP_ENABLED=1
OPS_WEEKLY_RECAP_ENABLED=1
OPS_RECAP_CHANNEL_ID=YOUR_CHANNEL_ID
```

Recommended: keep all three posting into the same channel initially. Once you see volume, split ops to a separate channel.

Runbook for go-live:

- Restart bot
- Run `/context_status` → confirm hb age is healthy, `strict=shadow`, misses low
- Trigger a test event:
   - `/news_ping_market` (seeds market news state)
   - `/news_ping SPY` (seeds symbol news state)
 - Confirm broadcast happens once per transition (cooldown/locks prevent repeats)
 - After one clean session with low/no misses: `CTX_SNAPSHOT_STRICT=1`

Emergency revert (instant):

```ini
CTX_SNAPSHOT_STRICT=shadow
```

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

## 7. Troubleshooting Notes

- **Missing futures context**: confirm the IQFeed collector is writing fresh rows and that `FUTURES_CONTEXT_PATH` points at the expected JSON.
- **Discord rate limits**: the bot respects the 2,000 character limit; if messages are rejected, double-check the template edits didnt exceed the guards.
- **Pytest warnings**: the repo pins `pytest-asyncio` and `pytest-timeout`; rerun `pip install -r requirements-dev.txt` if you see configuration warnings about `asyncio_mode` or `timeout`.

Document updates alongside template or pipeline changes so future contributors can onboard without tribal knowledge.
