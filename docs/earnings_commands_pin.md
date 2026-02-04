# Earnings commands (TNT)

## Quick commands

- `/earnings SYMBOL`
  - Next earnings time (ET), session (BMO/AMC/DURING), confirmed vs estimated
  - Blackout window + whether alerts are suppressed right now
  - Countdown (e.g. `In: 4d 2h`)
  - **Premium card** (when enabled):
    - Expected move (weekly ATM) with upper/lower bounds when available
    - Risk + (optional) IV crush risk + one-line TNT guidance
    - Last 4 earnings reactions (move + gap + tag) when history is available
  - Related news (limited)

- `/earnings_upcoming days:7`
  - Upcoming earnings list for the configured majors/watchlist

## Notes

- Expected move is computed from the options chain **only for single-symbol requests** (e.g. `/earnings TSLA`) and is rate/timeout bounded.
- “Last refreshed” shows cache freshness; stale badges appear when beyond your configured threshold.

## Known-good .env.local defaults

```dotenv
# Earnings “premium” card
TNT_EARNINGS_PREMIUM=1
TNT_EARNINGS_EMBED_STALE_HOURS=48
TNT_EARNINGS_RISK_LABEL_STYLE=marketing   # or simple
TNT_EARNINGS_NEWS_LIMIT=3

# Expected move enrichment (safe & bounded)
EARNINGS_OPTIONS_ENRICH_SINGLE=1
EARNINGS_OPTIONS_ENRICH_SINGLE_TIMEOUT=8

# Earnings reactions enrichment (safe & bounded)
EARNINGS_REACTIONS_ENRICH_SINGLE=1
EARNINGS_REACTIONS_ENRICH_SINGLE_TIMEOUT=6
EARNINGS_REACTIONS_LOOKBACK_DAYS=420
```

## 60-second verify

1) Restart bot (after killing old process + removing lock):

```powershell
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
Remove-Item -Force -ErrorAction SilentlyContinue .\logs\tnt_discord_bot.lock
& $py -u -m cli.discord_bot
```

2) In Discord:
- `/earnings TSLA` → should show Expected move + Risk when chain is available
- `/earnings AMD` → should show countdown + confirmed/estimated + session
- `/earnings_upcoming days:7` → should show upcoming list
