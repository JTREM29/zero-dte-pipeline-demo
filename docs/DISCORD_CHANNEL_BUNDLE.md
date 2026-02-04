# Discord Channel Bundle (Drop‑In)

This is the single drop‑in bundle for:

- All channel IDs → env vars
- One canonical purpose map
- Channel‑aware behavior rules (router contract)
- Pinned post text per channel (paste‑ready)

## 1) `.env` / `.env.local` — Channel IDs (Copy/Paste)

```ini
# TNT Channel IDs (canonical)
ASK_TNT_CHANNEL_ID=1462986002506186926
ALERTS_CHANNEL_ID=1458633494107525173
CALENDAR_EARNINGS_CHANNEL_ID=1462948320229200065
AI_TRADE_JOURNAL_CHANNEL_ID=1452144900752674897
PAPER_DESK_TRADES_CHANNEL_ID=1458632795969818788
PAPER_DESK_DISCUSSION_CHANNEL_ID=1458633111352119460
ANNOUNCEMENTS_CHANNEL_ID=1462982126982140056
HOW_TO_USE_TNT_CHANNEL_ID=1462982266203406480
TNT_CANARY_CHANNEL_ID=1451014818827079750

# Divergence Watch (INSIGHT posts → journal)
TNT_DIVERGENCE_WATCH_ENABLED=1
TNT_DIVERGENCE_WATCH_SYMBOLS=SPY
TNT_DIVERGENCE_WATCH_POLL_SEC=60
TNT_DIVERGENCE_WATCH_MODE=insights

# Earnings Pressure Watch (context-only)
TNT_EARNINGS_PRESSURE_WATCH_ENABLED=1
TNT_EARNINGS_PRESSURE_SYMBOLS=TSLA,AAPL,NVDA
TNT_EARNINGS_PRESSURE_LOOKAHEAD_HOURS=36
TNT_EARNINGS_PRESSURE_POLL_SEC=120
TNT_EARNINGS_PRESSURE_COOLDOWN_SEC=43200
TNT_EARNINGS_PRESSURE_FOOTER=1

# Divergence throttles (shared; also used by legacy autopost)
AUTOPOST_DIVERGENCE_COOLDOWN_SEC=900
AUTOPOST_DIVERGENCE_GLOBAL_MAX_PER_HOUR=8

# Candidate snapshot shaping
CTX_CANDIDATES_APPROVED_CAP=5

# Router enable (recommended)
TNT_CHANNEL_ROUTER_ENABLED=1
```

## 2) Canonical Channel Purpose Map (Single Source of Truth)

Implementation lives in `delivery/channel_router.py`.

## 3) Channel Router Contract (Behavior Rules)

Only ONE channel is “full power”.

- ✅ `ASK_TNT` = full agent answers + candidates allowed (when `candidates.status == APPROVED`)
- 🚫 all other channels = restricted (redirect or silence)

Redirect text (verbatim):

> For current market context or candidates, please ask in #ask-tnt.

## 4) Discord Permissions (Recommended “Enforce by Server”)

- `#alerts`: Members View ✅ / Send ❌
- `#calendar-earnings`: Members View ✅ / Send ❌
- `#announcements`: Members View ✅ / Send ❌
- `#how-to-use-tnt`: Members View ✅ / Send ❌
- `#tnt-canary`: hidden from members (admin only)

Writable by members:

- `#ask-tnt` ✅
- `#ai-trade-journal` ✅
- `#paper-desk-trades` ✅
- `#paper-desk-discussion` ✅

## 5) Pinned Posts Per Channel (Paste‑Ready)

### 📌 #ask-tnt

How to use TNT
Ask questions in plain English by tagging @TNT Trading Bot.

TNT does not issue trades or signals.
TNT surfaces AI Trade Candidates — option structures that fit current market conditions.

A candidate is not a command. Sometimes the correct answer is no trade.

The goal is not more trades — it’s better decisions.

### 📌 #alerts

TNT Alerts (Automated)
This channel is automated and read-only.

Alerts are posted only when predefined conditions are met.
If there’s no alert, conditions did not justify action.

### 📌 #calendar-earnings

Earnings & Market Events (Context Only)
This channel posts upcoming earnings and macro events.

These are context, not trade instructions.

### 📌 #ai-trade-journal

AI Trade Journal
Use this channel for post-trade reflection and learning.

The journal evaluates decisions after the fact. It does not generate trades.

For current market context or candidates, use #ask-tnt.

### 📌 #paper-desk-trades

Paper Desk (Trade Logs Only)
This channel is for logging simulated trades only.

No trade ideas here. No candidates here.
For questions or context, use #ask-tnt.

### 📌 #paper-desk-discussion

Paper Desk Discussion
Discuss paper trades, outcomes, and lessons here.

This is for review and learning — not real-time trade calls.
For current market context or candidates, use #ask-tnt.

### 📌 #announcements

Announcements
Official updates only: system changes, releases, maintenance, pricing.

### 📌 #how-to-use-tnt

Start Here
TNT is a decision-support system, not a signal service.

• Ask questions in #ask-tnt
• Alerts are gated, not commands
• Candidates are optional structures, not signals
• “No trade” is a valid output

## 6) AI Candidates Messaging Contract (Drop‑In Summary)

Principle

TNT does not issue trades. TNT surfaces candidates that fit current conditions.

Canonical definition

AI Trade Candidates are option structures that statistically fit the current market regime, volatility state, and positioning — not instructions to trade.

Required line in every candidate output

“This is a candidate, not a command.”

No-candidate phrasing

“There is no AI trade candidate right now because conditions do not offer a statistical edge.”

Full locked contract: see `docs/AI_TRADE_CANDIDATES_CONTRACT.md`.
