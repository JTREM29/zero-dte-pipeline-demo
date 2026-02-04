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

Optional (recommended):

- `AUTOPOST_CANDIDATES_MESSAGING=1` – appends a short **AI Trade Candidates** block to autopost renders.
   - Source of truth is only canonical snapshots (`ctx:sym:{SYM}.candidates`).
   - Uses locked, verbatim messaging for:
      - Candidate boundary: "This is a candidate, not a command."
      - No-candidate: "There is no AI trade candidate right now because conditions do not offer a statistical edge."

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
   - Futures ingest proof (one-shot): run `python -u scripts/futures_ops_proof_one_shot.py` and require `CLASSIFY=OK`.
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


Keep this document updated when guardrails, message formats, or operational procedures change.
See also: [TNT_CHART_STRUCTURE_COMMANDS.md](TNT_CHART_STRUCTURE_COMMANDS.md) for internal-only chart commands and the agent routing/consistency matrix.

## 9. “Make it post” (streams + proof greps)

If `/force_*` works but a channel looks empty, the renderer is almost never the issue.
It’s usually one of:
- Scheduler not running (wrong bot process, or `TNT_AUTOMATION_ENABLED=0`)
- Deduped (it already posted earlier)
- Wrong channel id / routing fallback (autopost/canary instead of production)
- Schedule not yet due

### 9.1 Trading alerts → `#alerts`

Paste into `.env.local`:

```ini
# ============ Alerts (Discord delivery) ============
TNT_ALERTS_DISCORD_DELIVERY_ENABLED=1
TNT_ALERTS_CHANNEL_ID=1458633494107525173

# optional override (default is fine)
# TNT_ALERTS_DISCORD_QUEUE=tnt:alerts:discord_queue

# optional: bundling
TNT_ALERTS_BUNDLE_WINDOW_SEC=15
```

Proof grep (delivery worker / slash bot log):

```powershell
Get-Content -Tail 400 .\logs\slash_live.log |
   Select-String "\[TNT\]\[ALERTS\]\[DELIVERY\]\[(POP|BUNDLE|POSTED|WARN)\]" |
   Select-Object -Last 80 | % { $_.Line }
```

### 9.2 Earnings calendar + results → `#calendar-earnings`

Paste into `.env.local`:

```ini
# ============ Earnings + Macro (Calendar) ============
TNT_AUTOMATION_ENABLED=1

EARNINGS_PROVIDER=massive_benzinga
EARNINGS_AUTOPOST_ENABLED=1
EARNINGS_RESULTS_AUTOPOST_ENABLED=1

CALENDAR_EARNINGS_CHANNEL_ID=1462948320229200065
EARNINGS_POST_CHANNEL_ID=1462948320229200065

# schedule
TNT_EARNINGS_DAILY_TIME_ET=18:00
TNT_EARNINGS_RESULTS_TIMES_ET=11:00

# dedupe TTLs (3 days)
TNT_EARNINGS_DAILY_DEDUPE_TTL_SEC=259200
TNT_EARNINGS_RESULTS_DEDUPE_TTL_SEC=259200

# macro file
TNT_MACRO_EVENTS_FILE=data/macro_events.csv
```

Proof grep (delivery bot log):

```powershell
Get-Content -Tail 600 .\logs\bot_live.log |
   Select-String "\[AUTOPOST\]\[(EARNINGS|EARNINGS_RESULTS)\]( posting| deduped)|\[AUTOPOST\]\[(EARNINGS|EARNINGS_RESULTS)\]\[PROOF\]" |
   Select-Object -Last 120 | % { $_.Line }
```

### 9.3 Daily + weekly outlook → `#weekly-daily-outlook`

Paste into `.env.local`:

```ini
# ============ Crown Jewel Outlook ============
TNT_AUTOMATION_ENABLED=1

DAILY_OUTLOOK_ENABLED=1
DAILY_OUTLOOK_TIME_ET=08:45
DAILY_OUTLOOK_CHANNEL_ID=1465748835203547220
TNT_OUTLOOK_KIND=status

# headlines / wow knobs (optional)
TNT_OUTLOOK_HEADLINES_LIMIT=4
TNT_OUTLOOK_WOW_NEWS_WINDOW_MIN=90
TNT_HEADLINES_MATERIAL_ONLY=1

# weekly (normal schedule)
WEEKLY_OUTLOOK_ENABLED=1
WEEKLY_OUTLOOK_DAY_ET=SUN
WEEKLY_OUTLOOK_TIME_ET=18:00
WEEKLY_OUTLOOK_DEDUPE_SCOPE=week
WEEKLY_OUTLOOK_DEDUPE_TTL_SEC=691200
```

Proof grep (delivery bot log):

```powershell
Get-Content -Tail 600 .\logs\bot_live.log |
   Select-String "\[AUTOPOST\]\[(OUTLOOK|WEEKLY_OUTLOOK)\]( posting| deduped)|\[AUTOPOST\]\[(OUTLOOK|WEEKLY_OUTLOOK)\]\[PROOF\]" |
   Select-Object -Last 120 | % { $_.Line }
```

### 9.4 One extra “no doubt” proof (startup routing banner)

After a restart, confirm the delivery bot prints the resolved channels + key flags:

```powershell
Get-Content -Tail 200 .\logs\bot_live.log |
   Select-String "\[TNT\]\[CONFIG\] channels=|\[TNT\]\[CONFIG\] outlook_kind=|\[TNT\]\[CONFIG\] earnings_provider=" |
   Select-Object -Last 60 | % { $_.Line }
```
