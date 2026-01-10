# Why TNT “nudges you” (Chart Concierge)

TNT includes a deterministic “Chart Concierge” layer that can post short, explainable nudges around watchlist tickers.

This is not “AI spam.” It is intentionally:

- Fast: runs before any LLM calls
- Scoped: watchlist-only + allowlisted channels
- Conservative: hard anti-spam throttles
- Explainable: deterministic templates, not free-form hallucinations

## When a nudge can happen

A Concierge nudge can fire in two modes:

1) **Mention-triggered** (safe default)
- You mention the bot (or its role) and include a watchlist ticker.

2) **Auto nudge** (optional)
- If enabled, TNT can nudge without a mention, but only in allowlisted channels and only for watchlist tickers.

## Hard anti-spam guarantees

Concierge nudges are gated by:

- **Channel allowlist**: only allowed channel names (e.g. `watchlist-alerts`, `charts`, `trading-floor`).
- **Watchlist-only**: ticker must be in the configured watchlist.
- **Throttles**:
  - Global cooldown
  - Per-user cooldown
  - Per-ticker daily cap

These are enforced before sending.

## Decision Lock™ (risk stand-down)

When market risk is elevated (e.g. high-volatility regimes), TNT can activate a **Decision Lock**.

- In **HARD** lock, TNT constrains responses to **risk-management only**.
- This is designed to prevent “forced trades” and reduce the chance of impulsive entries during unstable conditions.

Practically:
- Concierge can set a HARD lock for the channel.
- When a HARD lock is active, the LLM is constrained to:
  - avoid entries/exits and initiating risk
  - explain what would need to change to unlock
  - suggest stand-down posture and what to watch

## Configuration (ops)

Common environment flags:

- `TNT_CONCIERGE_ENABLED=1`
- `TNT_CONCIERGE_AUTO=1` (optional)
- `TNT_CONCIERGE_ALLOWED_CHANNELS=watchlist-alerts,charts,trading-floor`
- `TNT_CONCIERGE_TICKER_MAX_PER_DAY=2`
- `TNT_CONCIERGE_USER_COOLDOWN_SEC=3600`
- `TNT_CONCIERGE_GLOBAL_COOLDOWN_SEC=60`

Watchlists:

- Global watchlist file: `config/concierge_watchlist.json`
- Optional per-user watchlist file: `config/concierge_watchlist_users.json` (keyed by Discord user id)

Audit logging:

- `logs/concierge_events.jsonl` (append-only; path override via `TNT_CONCIERGE_AUDIT_PATH`)

Optional memory hint:

- `TNT_CONCIERGE_MEMORY=1` appends a tiny “recent nudges” line for the ticker.
