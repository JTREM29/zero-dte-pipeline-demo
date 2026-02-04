# TNT Chart & Structure Commands (Internal / Power)

These commands are internal tools for admins/agent use to render specific visual artifacts.
They are not part of subscriber UX. Subscribers should not need to know these exist.

## Commands

### /oi — Open Interest Structure

**Renders**
- Open interest by strike
- IV overlay
- Dominant positioning zones

**Use when**
- Explaining pinning / structural magnets
- Identifying why price is "stuck" or rejecting levels

**Agent rule**
- Reference structure verbally by default
- Post a chart only when the user explicitly asks for a visual, or asks why price is behaving unusually and a visual materially helps

---

### /chart — Price Context

**Renders**
- Standard price chart for a symbol

**Use when**
- Anchoring discussions about levels
- Visual confirmation of range vs trend

**Agent rule**
- Never default-post
- Post only if the user explicitly asks to see price or levels

---

### /gex — Gamma Exposure

**Renders**
- Gamma profile
- Zero-gamma level

**Use when**
- Explaining volatility compression vs expansion
- Explaining chop vs trend regimes
- Framing why moves fail / revert

**Agent rule**
- May be used internally to determine regime
- Post only when the user explicitly asks about volatility behavior, or requests the chart

---

### /pressure — Positioning Pressure (7–14 DTE)

**Renders**
- Flow/positioning imbalance across near-dated options

**Use when**
- Short-term directional confirmation
- Detecting pressure build vs exhaustion

**Agent rule**
- Never contradicts TNT bias tokens
- If pressure diverges from bias, agent must explicitly downgrade confidence (e.g., "pressure diverging — confidence reduced")

---

### /ddp — Dealer Delta Pressure

**Renders**
- Net dealer delta stress

**Use when**
- Intraday momentum confirmation
- Early warning for reversals
- Identifying absorption vs chase

**Agent rule**
- Cannot override regime
- Used to refine timing, not direction

---

### /pcr — Put/Call Ratio

**Renders**
- Sentiment skew

**Use when**
- Sentiment extremes
- Contrarian vs confirmation framing

**Agent rule**
- Never used standalone
- Must be framed relative to regime (e.g., "high PCR in bearish regime = confirmation, not reversal")

---

### /futures_chart — Index Futures

**Renders**
- Futures price action
- Session context (overnight vs RTH)

**Use when**
- Premarket bias context
- Explaining cash/futures divergence
- News-driven moves

**Agent rule**
- Futures lead cash
- If futures context is stale/degraded, agent must downgrade confidence and avoid directive action language

---

### /htf — Higher-Timeframe Structure

**Renders**
- Daily/weekly structure
- Macro inflection zones

**Use when**
- Anchoring intraday bias
- Explaining why moves fail or extend
- Avoiding countertrend trades

**Agent rule**
- HTF overrides intraday narratives
- If discussing countertrend action, agent must explicitly label it as countertrend and reduce confidence

## Slash Command Visibility Policy

**Subscribers**
- Do not see slash commands
- Do not need to know commands exist
- Interact only via the agent/bot conversational interface

**Power/Admin**
- Can invoke commands manually
- Can debug/visualize directly

**Agent**
- May invoke commands internally
- Decides if a chart adds value
- Never exposes command syntax unless explicitly asked

## Agent Routing Matrix (Critical)

This matrix prevents contradiction between alerts, state, and agent language.

### Step 1 — Classify the question

- Market state: "What’s going on today?"
- Symbol bias: "SPY bullish or bearish?"
- Why move: "Why did we dump?"
- Trade viability: "Should I trade this?"
- Visualization: "Show me futures"
- Education: "What is gamma?"

### Step 2 — Load canonical state (always)

Agent must read:
- `ctx:market`
- `ctx:sym:{SYM}` (if applicable)
- Verdict tokens: `bias`, `regime`, `confidence`, `eligibility`, `reason_token`
- Data freshness/health fields

If missing or stale: degrade gracefully (observational mode; no invention).

### Step 3 — Decide action

A) If `eligibility = NO_TRADE`
- Lead with **DO NOTHING**
- May explain what would change eligibility
- May reference structure; must not suggest entries

B) If confidence is low
- Frame the environment as degraded
- Use non-directive language (edge weak / mixed)
- Charts optional; never directive

C) If the user explicitly requests a chart
- Invoke the appropriate command
- Preface with context: "Here’s the chart for visual context — bias remains X."

D) If explanation-only
- Answer verbally
- Reference tools implicitly (no chart spam)

### Step 4 — Consistency guard (mandatory)

Before responding, agent must check:
- Does stated bias match TNT bias?
- Am I suggesting action when eligibility says NO?
- Am I implying certainty when confidence is low?
- Am I using levels not present in the canonical snapshot?

If any are true: rewrite the response to comply.
