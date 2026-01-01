from delivery.discord_bot import validate_analysis_message


def test_validate_analysis_message_on_demand_accepts_pivots_heading() -> None:
    text = """🔍 **On-Demand Analyze** — **SPY**

🧾 Data Mode: OPEN (live snapshot)
💵 **Last Price**
• SPY: 500.25 (live snapshot | 2025-01-01 10:00 ET | OPEN)

📐 **Pivots (RTH)**
• R1 505.00 | P 499.00 | S1 493.00

🔕 TNT STATUS
Market conditions unstable / low-confidence.
No Trade Context issued.
"""

    ok, reason = validate_analysis_message(text, label="analyze_spy")
    assert ok, reason


def test_validate_analysis_message_allows_stand_down() -> None:
    text = """⚠️ STAND DOWN — Price Feed Offline
Symbol: **I:SPX**
Missing: Validated price
Issue: missing_last_price

Action: retry in 60s or run /status

🧾 Data Mode: OPEN (unavailable; no price sources)
💵 **Last Price**
• I:SPX: n/a (unavailable | n/a | OPEN)
"""

    ok, reason = validate_analysis_message(text, label="analyze_i_spx")
    assert ok, reason
