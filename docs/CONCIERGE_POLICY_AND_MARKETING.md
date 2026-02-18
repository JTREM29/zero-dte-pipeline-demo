# TNT Concierge: Channel Policy + Marketing Kit

## 1) Safe channel policy (won't spam)

### Allowed auto-reply channels

- `#watchlist-alerts` ✅ (best place for concierge nudges)
- `#charts` ✅ (if you want “chart suggested” posts)
- `#trading-floor` ⚠️ (only if throttled hard)

### Disallowed auto-reply channels

- `#announcements` ❌ (manual only)
- `#help` ❌ (support should be human/interactive)
- `#general` ❌ (spam risk)

### Hard throttles (must-have)

- Per ticker: max 2 nudges/day
- Per user: cooldown 30–60 min
- Global: max 1 nudge/minute across server
- Only fire on watchlist tickers (never “all tickers”)

## 1b) Watchlist (sane default)

Default file-based watchlist (edit without env sprawl): [config/concierge_watchlist.json](config/concierge_watchlist.json)

Optional overrides:

- `TNT_CONCIERGE_WATCHLIST_PATH` (path to JSON file)
- `TNT_CONCIERGE_WATCHLIST` (comma-separated fallback)


## 2) “Before vs TNT” marketing kit (copy ready)

Use this as the text basis for a side-by-side graphic:

### BEFORE (typical Discord)

User: “SPY?”

Answer: “Support 691.27, needs 692.34 for breakout.”

Result: Chopped → stopped out both ways → confusion.

### TNT (Chart Concierge)

TNT: “SPY is between major OI walls — no dominant side.”

TNT: “Type /oi SPY. If OI doesn’t unwind, expect stop-runs both ways.”

TNT: “Decision Lock: SOFT. Wait for structure to choose.”

Result: Fewer trades, better timing, less bleed.
