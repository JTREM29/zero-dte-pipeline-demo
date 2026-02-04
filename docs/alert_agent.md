# TNT Alert Agent (v1)

Goal: Sidekick-like alert creation in plain English, but TNT-native (regime + edge gates + audit).

This is a **deterministic compiler** design:
- User text → compiler → constrained `AlertIntentV1` JSON
- Validator rejects ambiguous/unsupported requests (no “creative” alerts)
- Everything is auditable on disk

## Discord UX

Creation:
- `/alert <text>`
  - Natural language compiled into strict `AlertIntentV1` JSON (preview + confirm)
  - Bot replies with a preview and a **Confirm/Cancel** confirmation before writing to Redis

Management:
- `/alerts` — list your alerts
- `/alert_show id:<id>`
- `/alert_pause id:<id>` / `/alert_resume id:<id>`
- `/alert_delete id:<id>`

Notes:
- `/alert` currently uses the strict LLM compiler contract (via the TNT “One Door” wrapper) and then validates the envelope + intent.
- Trigger delivery to Discord is handled separately via the worker result pipeline (`type=alert_trigger`).

## Supported primitives (v1)

- VWAP crosses: `crosses above VWAP`, `crosses below VWAP`
- Touch pivot: `touches R1` (pivot: P/R1/R2/S1/S2)
- Break levels: `breaks yesterday high`, `breaks yesterday low`
- Opening range: `breaks the 15-minute opening range high/low`
- Daily SMA: `crosses below 200-day SMA` (timeframe defaults to `1D`)
- Relative volume: `rvol > 2`

Gates (v1):
- Time window: `during RTH`
- Regime gate: `if regime bullish` / `only in bullish regime` (optionally `confidence > 0.6`)
- Cooldown + max triggers are applied with defaults (configurable later)

## Lightweight DSL (what the bot previews)

The bot normalizes into a readable line so users learn the format:

`SPY WHEN crosses_above(price,vwap) CONFIRM close ON 5m DURING RTH UNTIL 3d THEN notify(compact)`


## LLM Compiler Contract (Strict)

If you wire in an LLM-based compiler, keep it on a tight contract: the model may only emit validated JSON that matches the schema in [tnt_alerts/alert_intent.py](../tnt_alerts/alert_intent.py).

Required envelope shape (no markdown, raw JSON only):

- Success:
  - `{ "ok": true, "intent": <AlertIntent JSON>, "warnings": [...], "clarify": null }`
- Clarify / reject:
  - `{ "ok": false, "intent": null, "warnings": [...], "clarify": { ... } }`

The drop-in system prompt lives in [tnt_alerts/llm_contract.py](../tnt_alerts/llm_contract.py) as `SYSTEM_PROMPT_ALERT_COMPILER_V1`.

If enabled, the Discord bot command `/alert_llm` will call the TNT “One Door” wrapper with this system prompt and then feed the resulting envelope into the same validation + preview + audit-store path as `/alert`.

## Watchlists + Timeframe Scheduler (Redis)

The repo includes a Redis-friendly scheduler skeleton in [tnt_alerts/scheduler_redis.py](../tnt_alerts/scheduler_redis.py):

- Index keys
  - `alerts:active` (set)
  - `alerts:tf:<tf>` (set)
  - `alert:<id>:intent` (string JSON)
  - `alert:<id>:state:<symbol>` (string JSON)

- Watchlist expansion
  - [tnt_alerts/watchlists.py](../tnt_alerts/watchlists.py) provides `expand_targets()` with a local file backend (`watchlists/<user_id>/<name>.json`) and a fallback to TNT concierge watchlists if available.

This is a skeleton designed to plug into your existing worker/job pipeline; it does not change the current `/alert` runtime behavior by itself.

## Local Redis + sanity steps

If you don't have a shared Redis reachable, run a native Redis-compatible service on Windows and validate persistence.

Recommendation (Windows): **Memurai Community Edition** (drop-in Redis compatible, stable Windows service).

VS Code tasks:
- `Redis: Verify service + port (native)`
- `Alerts: Redis sanity`
- `Alerts: Scheduler 5m (log-only)`
- `Alerts: Scheduler 5m (enqueue)` (pushes `type=alert_trigger` jobs to `tnt:jobs`)

These tasks read `TNT_REDIS_HOST`, `TNT_REDIS_PORT`, `TNT_REDIS_DB` from your environment.

Quick setup (local CLX / Memurai):
- Install Memurai as a Windows Service (port `6379`, bind `127.0.0.1` is fine).
- Set env vars:
  - `setx TNT_REDIS_HOST 127.0.0.1`
  - `setx TNT_REDIS_PORT 6379`
  - `setx TNT_REDIS_DB 0`
- Restart VS Code so tasks inherit the vars.
- Run `Redis: Verify service + port (native)` then `Alerts: Redis sanity`.

Manual equivalent (PowerShell):
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/redis_verify.ps1`

Scripts (equivalent to the original redis-cli sanity checks):
- `scripts/redis_alerts_sanity.py` prints `SMEMBERS alerts:active` and scans `alert:*`.
- `scripts/run_alert_scheduler_5m.py` runs a 5-minute evaluation loop (defaults to log-only; add `--enqueue` to push jobs).
## Audit storage

By default, alerts are written to:
- `alerts/YYYY-MM-DD/<alert_id>.json`
- `alerts/YYYY-MM-DD/<alert_id>_events.jsonl`

Events include `created` and status changes (paused/resumed/deleted). Trigger/suppression events are reserved for the engine phase.

## Environment

- `TNT_ALERTS_ENABLED=1` (default) — set `0` to disable alert commands
- `TNT_ALERTS_ROOT=alerts` (default) — change the on-disk alert root
- `TNT_USER_ALERT_COOLDOWN_SEC=4` — optional per-user cooldown
- `TNT_ALERTS_LLM_ENABLED=0` (default) — set `1` to enable `/alert_llm`
- `TNT_ALERTS_LLM_TIMEOUT_S=10` — optional timeout for the LLM compile step
- `TNT_ALERTS_LLM_MAX_TOKENS=900` — optional max output tokens for the LLM compile step

## Presets

- `preset:spy_vwap_breakout`

## Next step (engine)

This PR intentionally focuses on the contract + Discord UX + audit. The evaluation engine (timeframe buckets, bar fetch, indicator calc, dedupe, delivery) can be added as a separate step once the schema stabilizes.
