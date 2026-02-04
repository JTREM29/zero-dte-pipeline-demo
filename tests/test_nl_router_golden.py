from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from services.nl.nl_router import route_text


@dataclass(frozen=True)
class GoldenCase:
    text: str
    expected_route: str
    expected_symbol: Optional[str] = None
    expected_handler: Optional[str] = None


CASES: list[GoldenCase] = [
    # --- Single-token ticker hard-route ---
    GoldenCase("aapl", expected_route="symbol", expected_symbol="AAPL", expected_handler="symbol_default"),
    GoldenCase("msft", expected_route="symbol", expected_symbol="MSFT", expected_handler="symbol_default"),
    GoldenCase("brk.b", expected_route="symbol", expected_symbol="BRK.B", expected_handler="symbol_default"),
    GoldenCase("appl", expected_route="symbol", expected_symbol="APPL", expected_handler="symbol_default"),

    # --- Earnings (calendar vs single-symbol) ---
    GoldenCase("Who has earnings next week?", expected_route="earnings", expected_handler="earnings_calendar"),
    GoldenCase("which stocks have earnings this week", expected_route="earnings", expected_handler="earnings_calendar"),
    GoldenCase("earnings tomorrow", expected_route="earnings", expected_handler="earnings_calendar"),
    GoldenCase("upcoming earnings next 7 days", expected_route="earnings", expected_handler="earnings_calendar"),
    GoldenCase("Earnings next week", expected_route="earnings", expected_handler="earnings_calendar"),

    GoldenCase("Does AAPL have earnings next week?", expected_route="earnings", expected_symbol="AAPL", expected_handler="earnings_symbol"),
    GoldenCase("AAPL earnings next week", expected_route="earnings", expected_symbol="AAPL", expected_handler="earnings_symbol"),
    GoldenCase("Apple earnings next week", expected_route="earnings", expected_symbol="AAPL", expected_handler="earnings_symbol"),
    GoldenCase("Does Apple have earnings next week?", expected_route="earnings", expected_symbol="AAPL", expected_handler="earnings_symbol"),
    GoldenCase("Google earnings next week", expected_route="earnings", expected_symbol="GOOGL", expected_handler="earnings_symbol"),
    GoldenCase("Alphabet earnings next week", expected_route="earnings", expected_symbol="GOOGL", expected_handler="earnings_symbol"),
    GoldenCase("meta earnings date", expected_route="earnings", expected_symbol="META", expected_handler="earnings_symbol"),
    GoldenCase("When is MSFT earnings?", expected_route="earnings", expected_symbol="MSFT", expected_handler="earnings_symbol"),
    GoldenCase("$TSLA earnings", expected_route="earnings", expected_symbol="TSLA", expected_handler="earnings_symbol"),
    GoldenCase("nvda er?", expected_route="earnings", expected_symbol="NVDA", expected_handler="earnings_symbol"),

    # --- Alerts ---
    GoldenCase("alert: spy breaks below 495", expected_route="alerts", expected_symbol="SPY", expected_handler="alert_create"),
    GoldenCase("spy crosses above 500", expected_route="alerts", expected_symbol="SPY", expected_handler="alert_create"),
    GoldenCase("SPY crosses below 498.25", expected_route="alerts", expected_symbol="SPY", expected_handler="alert_create"),
    GoldenCase("Notify: qqq breaks above 430", expected_route="alerts", expected_symbol="QQQ", expected_handler="alert_create"),
    GoldenCase("Set an alert for IWM breaks below 210", expected_route="alerts", expected_symbol="IWM", expected_handler="alert_create"),

    # --- News ---
    GoldenCase("news", expected_route="news", expected_symbol=None, expected_handler="news_symbol"),
    GoldenCase("why is AMD ripping", expected_route="news", expected_symbol="AMD", expected_handler="news_symbol"),
    GoldenCase("news on INTC", expected_route="news", expected_symbol="INTC", expected_handler="news_symbol"),
    GoldenCase("NVDA headline?", expected_route="news", expected_symbol="NVDA", expected_handler="news_symbol"),
    GoldenCase("what happened to TSLA", expected_route="news", expected_symbol="TSLA", expected_handler="news_symbol"),

    # --- Coach (general) ---
    GoldenCase("what's the plan into the open", expected_route="coach", expected_handler="coach"),
    GoldenCase("plan into the open?", expected_route="coach", expected_handler="coach"),
    GoldenCase("what can you do", expected_route="coach", expected_handler="coach"),

    # --- Edge cases ---
    GoldenCase("aapl earnings next week!!!", expected_route="earnings", expected_symbol="AAPL", expected_handler="earnings_symbol"),
    GoldenCase("  Apple, earnings next week? ", expected_route="earnings", expected_symbol="AAPL", expected_handler="earnings_symbol"),
    GoldenCase("spy crosses above 500?", expected_route="alerts", expected_symbol="SPY", expected_handler="alert_create"),
    GoldenCase("alert: $spy crosses above 500", expected_route="alerts", expected_symbol="SPY", expected_handler="alert_create"),
    GoldenCase("NEWS on $intc", expected_route="news", expected_symbol="INTC", expected_handler="news_symbol"),
    GoldenCase("amd, why did it spike?", expected_route="news", expected_symbol="AMD", expected_handler="news_symbol"),

    # Commands should be ignored by the NL router.
    GoldenCase("/earnings_upcoming 7", expected_route="ignore", expected_handler="command"),
    GoldenCase("!analyze SPY", expected_route="ignore", expected_handler="command"),
]


@pytest.mark.parametrize("case", CASES)
def test_nl_router_golden(case: GoldenCase) -> None:
    d = route_text(text=case.text)
    assert d.route == case.expected_route
    if case.expected_handler is not None:
        assert d.handler == case.expected_handler
    if case.expected_symbol is not None:
        assert d.symbol == case.expected_symbol
    else:
        assert d.symbol is None
