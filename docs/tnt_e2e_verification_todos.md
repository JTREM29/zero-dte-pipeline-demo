# TNT E2E Verification TODOs (Ops-safe)

This checklist is designed to verify TNT end-to-end without getting hung up.

## 0) Preflight (must pass)

- [ ] Redis reachable: run `python -u scripts/redis_llen_once.py`
  - Expect: prints `redis=host:port/db` and shows key types/lengths (no hang).
- [ ] Background services running (or start them):
  - Start tasks: **Redis worker (bg)**, **Discord bot (bg)**, **Alerts: Scheduler 1m enqueue (bg)**, **Alerts: Scheduler 5m enqueue (bg)**, **Context writer (bg)**.
  - Expect: schedulers emit `[TICK]`, bot emits `[DELIVERY] Enabled`, worker emits `[WORKER]`.

## 1) Real-Time Alerts Engine (core path)

### A) Scheduler → Worker → Discord (true end-to-end)

- [ ] Run safe canary trigger (single fire):
  - `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\tnt_e2e_smoke.ps1 -UseCanary`
  - Expect: `greenlight created A########`, then `Discord POSTED observed`.

### B) Dedupe / Cooldown / Bundling

- [ ] Verify bundling occurred during canary:
  - Run: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\alerts_proof_one_shot.ps1 -Lines 40`
  - Expect: `DELIVERY][BUNDLE] items=` and only 1-2 `POSTED` for multiple POPs.

### C) Proof-grade logging

- [ ] One screenshot truth snapshot:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\alerts_proof_one_shot.ps1 -Lines 40`
  - Expect: LLEN + scheduler ticks + worker POP/ENQUEUE_DISCORD + bot POSTED + context writer START.

## 2) Daily Market Outlook (morning brief/report)

These features are IQFeed-gated in the CLI.

- [ ] Verify IQFeed connectivity (if enabled):
  - `python -m cli.main test-connectivity`
  - Expect: provider list with connected sources.
- [ ] Generate report (no Discord post):
  - `python -m cli.main morning-report-simple --symbol SPY`
  - Expect: TL;DR output and no exception.

## 3) Paper Desk (simulation-only)

- [ ] Verify desk loop / heartbeat (if enabled in your config):
  - Run the paper desk service/command you use in prod and confirm it emits periodic “quiet” reason codes.
  - Expect: explicit “why quiet” heartbeats and skipped-trade reason codes (no silent idling).

## 4) Earnings Intelligence

- [ ] Probe earnings debug:
  - `python -u scripts/earnings_debug_probe.py`
  - Expect: prints cached/derived earnings context; no crash.
- [ ] Enrich one symbol cache (optional):
  - `python -u scripts/earnings_enrich_cache.py --symbol AAPL --write`
  - Expect: cache updated + follow-up probe reflects enriched info.

## 5) Options & Market Structure Analytics

- [ ] Verify command surfaces exist (Discord):
  - In Discord: `/oi`, `/gex`, `/pcr`, `/pressure`, `/htf`, `/vwap_range`.
  - Expect: command responds or cleanly reports missing data (no traceback).

## 6) Futures & Macro Context

- [ ] Futures ingest running (if configured): start **Futures ingest (bg)**.
  - Expect: health/status output and no crash loops.
- [ ] Futures ingest proof (one-shot, pasteable):
  - Run: `python -u scripts/futures_ops_proof_one_shot.py`
  - Expect: `CLASSIFY=OK` and `exit_code=0` (if not OK, treat it as real: stand down and fix futures ingest before trusting futures-dependent features).
- [ ] Discord `/futures_status` responds.

## 7) Crypto Intelligence

- [ ] Discord crypto commands respond:
  - `/crypto_watchlist`, `/crypto_rs`, `/crypto_vol`, `/crypto_divergence`.
  - Expect: weekend-aware messaging; no weekday assumptions.

## 8) Safety & Noise Control (must behave)

- [ ] Publish-only-on-success: stop Discord permissions or set bad channel (in canary env only) and confirm retries/backoff occur without losing the job.
- [ ] Contract enforcement: try a command that would require missing data and confirm it is blocked with a clean explanation (no partial/hallucinated output).

## 9) Ops & Transparency (admin tools)

- [ ] Discord `/status` and `/ops` respond.
- [ ] Redis visibility:
  - `python -u scripts/redis_llen_once.py`
  - Expect: queues reflect reality (jobs/results/discord queue).
- [ ] Heartbeat telemetry present:
  - `logs/tnt_heartbeat.json` updates over time.

---

### Notes

- Prefer canary routing for active tests.
- Prefer scripts that include timeouts and avoid tailing forever.
- If a step fails, re-run `alerts_proof_one_shot.ps1` to locate the stage that broke.
