# Copilot chat bookmark — 2026-01-16 — Alerts: Redis `alert_trigger` notifier

## What we accomplished
- Added **Discord notifications for scheduled alert triggers** by consuming Redis results (`tnt:results`) and posting a message into the originating Discord channel.
- Extended the **Redis worker result payload** to include `meta` so the bot can route + format alerts without needing to fetch the artifact file.
- Verified test suite: `146 passed, 1 warning`.

## Key files changed
- [cli/discord_bot.py](../../cli/discord_bot.py)
  - Adds a background task (feature-flagged) that `BLPOP`s Redis results and posts `alert_trigger` events to Discord.
  - Reads `intent.channel_id` from the result payload’s `meta.intent.channel_id`.
- [massive_service/redis_worker.py](../../massive_service/redis_worker.py)
  - Adds `meta` to `JobResult` JSON.
  - For `type=alert_trigger` jobs, includes `meta={alert_id,intent,event,actions}`.

## Runtime flags / environment variables
Enable the notifier in the Discord bot:
- `TNT_ALERTS_REDIS_NOTIFIER=1`

Redis connection (bot-side):
- `TNT_REDIS_HOST` (default `127.0.0.1`)
- `TNT_REDIS_PORT` (default `6379`)
- `TNT_REDIS_DB` (default `0`)
- `TNT_REDIS_RESULTS` (default `tnt:results`)

Related scheduler integration (already present from prior work):
- `TNT_ALERTS_REDIS_SCHEDULER=1` (mirror alert lifecycle into Redis scheduler keys)

## Expected data flow
1. Scheduler evaluates alerts and enqueues jobs of `type=alert_trigger`.
2. Worker consumes from Redis queue and pushes job results into Redis list `tnt:results`.
3. Discord bot notifier loop consumes `tnt:results` and posts an alert message to the correct channel.

## Quick “turn it on” checklist
1. Ensure Redis is reachable from the bot process.
2. Set `TNT_ALERTS_REDIS_NOTIFIER=1` in the bot environment.
3. Restart the Discord bot.
4. Trigger an alert (or run scheduler) and confirm messages appear.

## Notes / constraints
- The notifier is intentionally **deterministic** (no LLM usage).
- Duplicate suppression is best-effort (in-memory recent job ids); restarting the bot clears this cache.

## Next steps (optional)
- Add persistent dedupe (Redis `SETNX` / TTL) to prevent duplicates across bot restarts.
- Add a richer message format (include key condition fields / levels) while keeping it < 1900 chars.
- Add a `/alerts_notifier_status` command to show last processed job id + lag.
