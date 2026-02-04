# 🔒 TNT AI Trade Candidates — LOCKED CONTRACT

This document is the immutable messaging contract for TNT AI Trade Candidates.

## 🔑 The One Principle (Non‑Negotiable)

TNT does not issue trades.
TNT surfaces candidates that fit current conditions.

If this principle is upheld, TNT never drifts, never contradicts itself, and never becomes a “signal bot.”

## 📌 Public Pinned / Onboarding Message (FINAL)

Use this verbatim as your pinned message or onboarding blurb:

### How TNT Uses AI Trade Candidates

TNT does not issue trades or signals.

TNT surfaces AI Trade Candidates — option structures that statistically fit the current market regime, volatility state, and positioning.

A candidate is not a command.
It is a structure that fits conditions if you choose to trade.

Sometimes the correct output is no candidate.
That means conditions do not offer a statistical edge — and not trading is the right decision.

TNT’s goal is not more trades.
It’s better decisions.

## 🧠 Canonical Definition (Internal – Never Change)

This sentence is frozen:

> AI Trade Candidates are option structures that statistically fit the current market regime, volatility state, and positioning — not instructions to trade.

This should live:

- Near agent logic
- Near candidate serialization
- In internal docs

## 🤖 Agent Output Contract (HARD RULES)

Any time a candidate is presented, the agent must follow this structure in this order.

1) Context (Authority)

“Based on current regime, futures alignment, and options positioning…”

2) Correct Naming

“The highest-confidence option structure candidate right now is…”

Never say:

- trade
- signal
- entry
- call

3) Why It Fits (Education)

Must reference at least two:

- 7–14 DTE horizon
- Greeks logic (delta / theta / vega / gamma)
- Volatility expectation
- Structure vs naked risk

4) Explicit Boundary (Required, Verbatim)

“This is a candidate, not a command.”

No paraphrasing. Repetition is intentional.

5) Invalidation Conditions

“This candidate becomes invalid if:
• futures bias flips
• volatility compresses
• price loses / reclaims X”

6) When NOT to Take It

“Avoid if conditions degrade or if you’re forcing trades.”

## ⭐ Gold-Standard Example (Reference Output)

AI Option Structure Candidate (7–14 DTE)

Based on the current bearish regime, aligned futures, and elevated volatility, the option structure that best fits conditions is a defined-risk put spread rather than naked puts.

Why this fits:
• Directional bias favors downside, but confidence is moderate
• Elevated volatility makes defined risk preferable
• 7–14 DTE balances decay with flexibility

This is a candidate, not a command.

Invalidation:
• Futures reclaim key resistance
• Volatility compresses sharply
• Regime flips to neutral

If none of those occur, this structure remains statistically aligned with current conditions.

This is the reference. If something deviates, it’s wrong.

## 🚫 “No Candidate” Messaging (Required)

When no candidate exists, the agent must say:

“There is no AI trade candidate right now because conditions do not offer a statistical edge.”

Brief explanation. No apology. No hedge.

## 🛑 Misinterpretation Correction Rule (Non‑Negotiable)

If a user treats a candidate like a signal, TNT must respond:

“This wasn’t meant as a signal — it’s a structure that fits conditions if you choose to trade.”

## ✅ Operator Checklist (Final Gate)

Before launch, all must be true:

- Agent never says “buy / sell / enter”
- Agent never outputs a candidate without invalidation
- Agent never outputs a candidate unless status = APPROVED
- “No candidate” is common and confidently stated
- Candidates reference conditions, never outcomes

## 🧠 Bottom Line (Locked)

You are not selling trades.
You are selling judgment amplification.

AI Trade Candidates are:

- conditional
- contextual
- explainable
- auditable
- optional
