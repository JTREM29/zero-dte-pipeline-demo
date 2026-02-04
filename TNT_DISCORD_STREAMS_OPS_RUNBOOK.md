# TNT Discord Streams — Ops Runbook (Drop-in)

This doc maps each Discord “stream” to:
- the controlling env vars
- where it is implemented
- the log/queue greps that prove it’s working
- why a channel can look “empty” even when `/force_*` works

---

## A) Trading Alerts → #alerts

### Required env vars

```ini
# ============ Alerts (Discord delivery) ============
TNT_ALERTS_DISCORD_DELIVERY_ENABLED=1
TNT_ALERTS_CHANNEL_ID=1458633494107525173

# optional override (default is fine)
# TNT_ALERTS_DISCORD_QUEUE=tnt:alerts:discord_queue

# optional: bundling (presentation-only)
TNT_ALERTS_BUNDLE_WINDOW_SEC=15
```

### Proof (delivery loop)

```powershell
Get-Content -Tail 400 .\logs\slash_live.log |
  Select-String "\[TNT\]\[ALERTS\]\[DELIVERY\]\[(POP|BUNDLE|POSTED|WARN)\]" |
  Select-Object -Last 80 | % { $_.Line }
```

### “Why no alerts are firing?”

If the queue is empty (`LLEN=0`), nothing can post.

Root causes are typically:
- `gates.market_hours` window mismatch (CUSTOM window excluding RTH)
- `NO_TRIGGER_CONDITION_FALSE` (rules not actually triggering)
- `max_triggers` reached (`SUPPRESS_MAX_TRIGGERS_REACHED`)
- market data staleness (engine never sees a valid tick)

---

## B) Earnings Calendar + Results → #calendar-earnings

### Required env vars

```ini
# ============ Earnings + Macro (Calendar) ============
TNT_AUTOMATION_ENABLED=1

EARNINGS_PROVIDER=massive_benzinga
EARNINGS_AUTOPOST_ENABLED=1
EARNINGS_RESULTS_AUTOPOST_ENABLED=1

# watchlist behavior
# - watchlist: only EARNINGS_WATCHLIST matches
# - all: all earnings for the date
# - auto: watchlist if any hits, else fall back to all (recommended)
EARNINGS_WATCHLIST_MODE=auto

# upstream fetch robustness (optional)
EARNINGS_CALENDAR_HTTP_RETRIES=2
EARNINGS_CALENDAR_TIMEOUT_S=15

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

### Proof (autopost loops)

```powershell
Get-Content -Tail 600 .\logs\bot_live.log |
  Select-String "\[AUTOPOST\]\[(EARNINGS|EARNINGS_RESULTS)\]( posting| deduped)|\[AUTOPOST\]\[(EARNINGS|EARNINGS_RESULTS)\]\[PROOF\]" |
  Select-Object -Last 120 | % { $_.Line }
```

---

## C) Daily + Weekly Outlook → #weekly-daily-outlook (Crown Jewel)

### Required env vars

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

### Proof (autopost loops)

```powershell
Get-Content -Tail 600 .\logs\bot_live.log |
  Select-String "\[AUTOPOST\]\[(OUTLOOK|WEEKLY_OUTLOOK)\]( posting| deduped)|\[AUTOPOST\]\[(OUTLOOK|WEEKLY_OUTLOOK)\]\[PROOF\]" |
  Select-Object -Last 120 | % { $_.Line }
```

---

## Why a channel looks “empty” unless you force

If `/force_*` works but the channel has no scheduled posts, it’s usually one of:
- `TNT_AUTOMATION_ENABLED=0` (nothing schedules)
- delivery bot isn’t running (only slash bot is up)
- dedupe already tripped (it posted earlier, won’t repost)
- channel routing points somewhere else (autopost/canary)
- schedule hasn’t come due yet

### Always check routing at startup (no guessing)

```powershell
Get-Content -Tail 200 .\logs\bot_live.log |
  Select-String "\[TNT\]\[CONFIG\] channels=|\[TNT\]\[CONFIG\] outlook_kind=|\[TNT\]\[CONFIG\] earnings_provider=" |
  Select-Object -Last 80 | % { $_.Line }
```
