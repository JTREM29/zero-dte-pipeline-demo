# Autopost Operations Runbook

Source of truth for running the Discord autopost bot across local development, canary soaking, and production. Follow this document during market hours and when performing smoke drills.

## 1. Environment Modes

| Mode | Key Env Vars | Behavior |
| --- | --- | --- |
| Local Dev | `DRY_RUN=1`<br>`STRICT_CONTRACTS=1`<br>`DISCORD_CANARY_CHANNEL_ID=<staging_channel>` | Generates renders and audit logs only; no Discord messages are sent. Hard contract violations raise immediately. |
| Canary | `DRY_RUN=0`<br>`STRICT_CONTRACTS=0`<br>`DISCORD_CANARY_CHANNEL_ID=<staging_channel>` | Posts to the specified canary channel while keeping production silent. Contract violations fall back to the standby message. |
| Production | `DRY_RUN=0`<br>`STRICT_CONTRACTS=0` | Posts to the configured production channels. Canary override may be unset or ignored. |

**Notes**
- `DRY_RUN=1` → renders + audits only, no outbound Discord traffic.
- `STRICT_CONTRACTS=1` → fail-hard (CI/dev only).
- `STRICT_CONTRACTS=0` → fail-soft with fallback messaging (required in prod + canary).
- Canary runs require `DRY_RUN=0` so messages actually publish to the canary channel.

## 2. Required Environment Variables

Populate these in `.env` (or the deployment secret store) before launching:

- `DISCORD_BOT_TOKEN` – bot token with message privileges.
- `DISCORD_GUILD_ID` – server id, used for audit metadata.
- Channel mappings (one or more):
  - `AUTOPOST_CHANNEL_ID` – primary autopost target (production daily posts).
  - `DISCORD_DAILY_CHANNEL_ID`, `DISCORD_INTRADAY_CHANNEL_ID`, `DISCORD_FOCUS_CHANNEL_ID` – other command targets (optional, leave blank if unused).
  - `DISCORD_CANARY_CHANNEL_ID` – staging/canary channel id.
- Safety toggles: `DRY_RUN`, `STRICT_CONTRACTS`.

## 3. Launch Command

From the repo root, run:

```powershell
python cli/discord_bot.py
```

Choose the correct mode first (edit `.env` or export directly in the shell). The bot uses the environment at process start.

## 4. Pre-flight Checklist (Canary + Production)

1. **Credentials:** Verify `DISCORD_BOT_TOKEN` is valid and `.env` points at the latest token.
2. **Channels:** Double-check channel ids in `.env` match Discord (right-click → “Copy ID”).
3. **Mode flags:** Confirm `DRY_RUN`/`STRICT_CONTRACTS` match the desired mode (see table above).
4. **IQFeed/market data:** Ensure collectors are running so futures context is fresh.
5. **OpenAI (optional):** If insights mode is enabled, make sure `OPENAI_API_KEY` is present.
6. **TNT prompt (required):** Confirm `tnt_system_prompt.txt` is present and updated.
7. **TNT prompt runtime (2-minute verification):** On startup, confirm console logs a line like:
   - `[TNT][PROMPT] Loaded ... (sha256=..., chars=..., head='...')`
   This guarantees the process is reading the on-disk prompt (not an embedded string).
8. **TNT_STATE schema (v1.0):** Ensure the agent is receiving `TNT_STATE (authoritative)` with `meta.schema_version=1.0` and `meta.data_health=OK` during normal operation; if `STALE/DEGRADED/DOWN`, the bot must stand down.

## 5. Runtime Validation

- The bot should appear online in Discord shortly after launch.
- **Canary/Prod mode:** Expect posts to land in the configured channel(s). Keep Discord open during initial runs.
- **Audit trail:** JSON files write to `logs/autopost_audit/<YYYY-MM-DD>/*.json`. Inspect the latest file for contract version, git sha, symbol list, and metadata.

## 6. Contract Violation Drill

With `STRICT_CONTRACTS=0` (fail-soft):

1. Induce a violation (e.g., temporarily raise `DEFAULT_MAX_LINES` guard or inject bad copy).
2. Trigger the post (manual builder call or wait for loop).
3. Confirm:
   - A fallback "data incomplete / stand-down" message posts.
   - Audit payload logs the violation list.
   - The bot continues running.
4. Revert the deliberate violation afterward.

With `STRICT_CONTRACTS=1` (dev/CI): the same violation should raise `ContractViolationError` and halt the post.

## 7. Troubleshooting “What Broke?”

1. **No Discord messages**
   - Check `DRY_RUN` isn’t stuck at `1`.
   - Verify the channel id exists and the bot has send permissions.
   - Review console output for contract violations.
2. **Contract violation loops**
   - Tail `logs/autopost_audit/YYYY-MM-DD/*.json` for the `violations` array.
   - Compare text against `zero_dte_pipeline.tech.contracts` guard rails.
3. **Audit logs missing**
   - Confirm `AUTOPOST_AUDIT_ROOT` path is writable.
   - Ensure the bot process has run `_publish_autopost_render` (no posts → no audits).
4. **Token errors**
   - Refresh the bot token in Discord developer portal and update `.env`.

## 8. Post-Run Tasks

- For canary validation, capture a screenshot/log snippet confirming successful posts.
- Rotate `DRY_RUN` back to `1` when returning to local development.
- Periodically prune old audit folders (automatic pruning keeps the last 60 days by default).

Keep this document updated when guardrails, message formats, or operational procedures change.
