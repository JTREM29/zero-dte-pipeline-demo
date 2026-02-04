# Go-live: Discord reliability (stop spam, prove core loop)

This checklist is optimized for a paid Discord where:
- On-demand answers work every time
- Scheduled posts work when explicitly enabled
- Nothing “helpfully” spams a channel by default

## 0) Safety defaults (stop the bleeding)

As of this change:
- Scheduled automation requires explicit opt-in: `TNT_AUTOMATION_ENABLED=1`
- Each scheduled post type defaults to OFF unless explicitly enabled:
  - `TNT_FOCUS_LIST_ENABLED` (default 0)
  - `TNT_INTRADAY_UPDATE_ENABLED` (default 0)
  - `TNT_RECAP_ENABLED` (default 0)
  - `TNT_HEARTBEAT_ENABLED` (default 0)
- Stand-down de-dupe guard (prevents repeated stand-down autoposts per label):
  - `TNT_AUTOMATION_STANDDOWN_DEDUPE_SEC` (default 3600)

You should also see a one-time startup banner in logs from the delivery bot showing the resolved flags + channel routing.

## 1) Golden-path smoke (end-to-end proof)

Prereqs:
- Redis up
- Worker up (render pipeline / queue consumer)
- Market data feeds configured (whatever you normally use for live price + options snapshot)

Run:

```powershell
.\.venv\Scripts\python.exe -u golden_path_smoke.py --symbol SPY
```

(Equivalent: `.\.venv\Scripts\python.exe -u scripts\golden_path_smoke.py --symbol SPY`)

Expected:
- Exit code `0`
- Prints one `[SMOKE][OK] ... artifact=...` line
- Writes a JSONL log under `logs/golden_path/YYYY-MM-DD/`

If it fails:
- `build_failed`: data ingestion/snapshot error
- `render_failed`: worker not running / queue misconfigured
- `price_not_ok`: price snapshot missing or stale
- `render_not_ok`: render job ran but produced no PNG

## 2) Scheduled posts (explicit enable)

To enable “Paper Desk style” scheduled posts to the autopost channel:

```powershell
$env:TNT_AUTOMATION_ENABLED='1'
$env:AUTOPOST_CHANNEL_ID='<channel_id>'

# Enable only what you actually want
$env:TNT_PAPER_DESK_ENABLED='1'          # master for Paper Desk style posts
$env:TNT_FOCUS_LIST_ENABLED='1'          # morning focus list
$env:TNT_INTRADAY_UPDATE_ENABLED='0'     # midday update
$env:TNT_RECAP_ENABLED='0'               # close recap
$env:TNT_HEARTBEAT_ENABLED='0'           # ops heartbeat (recommend off unless needed)
```

Notes:
- Each loop posts at most once per ET day.
- Feed staleness pauses scheduling automatically.

## 3) Triple-expiry (staged rollout)

Day 1 (build + render only):
- Keep `TNT_TRIPLE_EXPIRY_ENABLED=0`
- Run the runner in build+render mode (no posting) during market hours and verify Redis packs + artifacts.

Day 2 (posting on, conservative):
- Set `TNT_TRIPLE_EXPIRY_ENABLED=1`
- Set conservative caps:
  - `TRIPLE_EXPIRY_GLOBAL_CAP_PER_HOUR=2`
  - `TRIPLE_EXPIRY_PER_SYMBOL_COOLDOWN_SEC=3600`
  - `TRIPLE_EXPIRY_TOP_N_IMAGES=1`

Day 3 (normal):
- Increase `TRIPLE_EXPIRY_GLOBAL_CAP_PER_HOUR` and `TRIPLE_EXPIRY_TOP_N_IMAGES` if results look good.

## 4) If “nothing is posting”

1. Check the delivery bot startup banner (it prints all enable flags and routing).
2. Confirm channel routing:
   - `CANARY_CHANNEL_ID` overrides `AUTOPOST_CHANNEL_ID`.
3. Confirm staleness gates:
   - `DATA_STALE_MAX_MIN` too tight will pause automation.
4. Confirm quiet hours / caps:
   - context alerts: `AUTOPOST_CONTEXT_QUIET_HOURS_ET`, `AUTOPOST_CONTEXT_GLOBAL_MAX_PER_HOUR`
   - triple expiry: `TRIPLE_EXPIRY_QUIET_HOURS_ET`, `TRIPLE_EXPIRY_GLOBAL_CAP_PER_HOUR`
5. Confirm worker/queue settings:
   - `TNT_REDIS_HOST/PORT/DB/QUEUE`

## 5) Operational “what to look at”

- Golden-path logs: `logs/golden_path/YYYY-MM-DD/*.jsonl`
- Triple-expiry logs: `logs/triple_expiry/YYYY-MM-DD/*.jsonl`
- Redis keys:
  - `triple_expiry:pack:{SYMBOL}:{YYYY-MM-DD}` (date key)
  - `triple_expiry:pack:{SYMBOL}:{EXPIRY}` (canonical)
  - `triple_expiry:manifest:{YYYY-MM-DD}`
