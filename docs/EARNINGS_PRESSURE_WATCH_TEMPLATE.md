# Earnings Pressure Watch (Paste-Ready Template)

This is a drop-in, production-ready **Earnings Pressure Watch** message format designed to add signal without forcing trades. It’s explicitly **context-only**.

## Paste-ready text

```text
🔔 EARNINGS PRESSURE WATCH

Symbol: {TICKER}
Event: Earnings {DATE} {TIME_ET}
Implied Move: {±X.X%}
Regime: {RISK_ON | DEFENSIVE | MIXED}

🧠 Pre-Earnings Market State

Price: {compressing | grinding | extended | range-bound}
Participation: {rising | flat | falling}
Pressure: {bid-dominant | offer-dominant | neutral}
Volatility: {building | muted | elevated}

⚠️ Pressure Assessment

Divergence detected: {YES | NO}
Type: {Price ↑ / Participation ↓ | Price flat / Pressure ↑ | None}

This does not predict direction.
It identifies stored pressure going into the earnings catalyst.

🎯 Reaction Bias (Not a Trade Call)

Upside reaction risk: {LOW | MODERATE | HIGH}
Downside reaction risk: {LOW | MODERATE | HIGH}

Bias reflects how price is likely to react,
not where it “should” go.

🛑 Trader Guidance

Expect faster-than-normal resolution
Avoid pre-event over-positioning
Post-earnings confirmation > prediction

🧩 TNT Interpretation

Earnings act as a stress test on existing structure.
When pressure exists, reactions tend to be asymmetric.

Optional compact footer (for Discord noise control)

Context alert • No trade issued • Monitor post-earnings follow-through

✅ Why this works

Doesn’t promise trades
Explains why earnings matter for this ticker
Lets divergence stay visible instead of silenced
Trains users to think in reaction risk, not guesses
```

## TNT automation

The bot can now auto-generate this format when enabled.

- Enable via env: `TNT_EARNINGS_PRESSURE_WATCH_ENABLED=1`
- Configure symbols: `TNT_EARNINGS_PRESSURE_SYMBOLS=TSLA,AAPL,NVDA`
- Routing: set `AI_TRADE_JOURNAL_CHANNEL_ID` (preferred) or `JOURNAL_CHANNEL_ID` (fallback).
