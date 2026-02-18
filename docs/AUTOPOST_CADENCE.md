# Autopost Cadence & Guardrails

This document tracks the shipping cadence, audience, and stop-gate rules for the TNT autopost suite. Keep it close to the product brief so operators know what "green" looks like before a run.

## Cadence Overview

| Payload | Default Channel | Target Send | Owner |
|---------|-----------------|-------------|-------|
| Daily Prep — Plan of Record | #desk-prep | 08:30 ET (or 30m before RTH) | Desk lead on rotation |
| Focus List — Next RTH | #focus | 09:45 ET (pause if futures stale/missing) | Flow specialist |
| Intraday Update | #intraday | As needed after major tape shifts; never more than 1/hr | Tape anchor |
| After Hours Rundown | #evening-brief | 16:30 ET | Evening closer |
| Pre-Market Briefing | #pre-market | 07:45 ET | Overnight desk |
| Daily Summary | #daily-summary | 18:00 ET | Ops archivist |

## Stand-Down Triggers (applied in copy + hooks)

- Futures context stale/missing ⇒ Focus List and Intraday Update stay in STAND DOWN.
- Directional confirmation `NEUTRAL/UNKNOWN` or signal edge < strict floor ⇒ Daily Prep marks STAND DOWN.
- Macro banner < 15 min ⇒ Daily Prep warns not to deploy new risk.
- Duplicate disclaimers, double blank lines, or markdown imbalance ⇒ pre-push hook fails.

## No-Trade Language Expectations

- Every payload must include an explicit "Stand-Down" or "No-Trade" section with two actionable bullets.
- When messages are in STAND DOWN, call out the reason in plain language (e.g., `waiting on futures context (missing)`).
- Guardrail bullets should mention both data quality and behavior (VWAP loss, confirmation flip, macro events).

## Futures Context Veto

- Daily Prep: can publish with stale/missing context but flips to STAND DOWN automatically.
- Focus List / Intraday Update: copy sets `Stand-Down: ON` and adds a pause bullet; operators should skip distribution until context is fresh.
- After Hours / Pre-Market: still publish but highlight the missing context in the futures section for situational awareness.

## QA Checklist (apply before posting or accepting goldens)

- Length under 1,900 characters (per payload via contract check).
- No `\n\n\n`, no trailing whitespace, and markdown tokens balanced (pre-push hook ensures this).
- Disclaimers appear exactly once, and major headers have no duplicates.
- Futures reasoning matches state (fresh vs stale vs missing) across Daily Prep, Focus List, and Intraday text.
- Tone stays decisive and professional: no filler, no TBD placeholders.

## Updating Goldens

When the template copy changes intentionally:
1. Run `python scripts/render_posts_smoke.py --all-scenarios` to regenerate text.
2. Review diff in `tests/goldens/**` for tone/guardrail alignment.
3. Commit with a message like `Update Discord output goldens (template revision)`.

Keeping this doc in sync with the copy ensures product stakeholders, QA, and automation all speak the same language.
