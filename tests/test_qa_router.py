from controller.qa_router import answer_divergence, classify_question
from delivery.qa_router import looks_like_earnings_calendar_query, route_question


def test_routes_empty_to_general():
    r = route_question(text="")
    assert r.kind == "general"


def test_routes_earnings_over_news():
    r = route_question(text="SPY earnings and news")
    assert r.kind == "earnings"


def test_routes_news_keywords():
    r = route_question(text="NVDA headline? why is it moving?")
    assert r.kind == "news"


def test_routes_market_context_keywords():
    r = route_question(text="what do futures and vix look like")
    assert r.kind == "market_context"


def test_routes_earnings_typos() -> None:
    r = route_question(text="earnigns tsla")
    assert r.kind == "earnings"


def test_routes_earnings_typos_random_symbol_nvda() -> None:
    r = route_question(text="earnigns nvda")
    assert r.kind == "earnings"


def test_routes_earnings_typos_double_typo_ups() -> None:
    r = route_question(text="ups ernings")
    assert r.kind == "earnings"


def test_routes_earnings_reaction_question() -> None:
    r = route_question(text="how does tsla react to earnings")
    assert r.kind == "earnings"
    assert r.sub_intent == "earnings_reaction"


def test_routes_earnings_risk_question() -> None:
    r = route_question(text="is meta earnings risky")
    assert r.kind == "earnings"
    assert r.sub_intent == "earnings_risk"


def test_routes_price_vs_earnings_range_question() -> None:
    r = route_question(text="where is aapl vs earnings range")
    assert r.kind == "price_range"
    assert r.sub_intent == "price_vs_range"


def test_routes_stretched_into_earnings_question() -> None:
    r = route_question(text="is tsla stretched into earnings")
    assert r.kind == "price_range"
    assert r.sub_intent == "stretched"


def test_routes_trade_now_structure_question() -> None:
    r = route_question(text="should i trade msft now")
    assert r.kind == "structure_timing"
    assert r.sub_intent == "trade_now"


def test_routes_policy_why_no_alert_question() -> None:
    r = route_question(text="why no alerts on tsla")
    assert r.kind == "policy"
    assert r.sub_intent == "why_no_alert"


def test_routes_policy_what_tnt_thinks_question() -> None:
    r = route_question(text="what does tnt think about tsla")
    assert r.kind == "policy"
    assert r.sub_intent == "what_think"


def test_classify_earnings() -> None:
    assert classify_question("Who has earnings this week?") == "earnings"


def test_earnings_calendar_intent_detector() -> None:
    assert looks_like_earnings_calendar_query(text="Who has earnings next week?") is True
    assert looks_like_earnings_calendar_query(text="Which stocks have earnings this week") is True
    assert looks_like_earnings_calendar_query(text="Earnings tomorrow") is True
    assert looks_like_earnings_calendar_query(text="INTC earnings?") is False
    assert looks_like_earnings_calendar_query(text="Apple earnings next week") is False
    assert looks_like_earnings_calendar_query(text="Does Apple have earnings next week?") is False


def test_classify_news() -> None:
    assert classify_question("Any news?") == "news_explain"


def test_classify_move() -> None:
    assert classify_question("What just happened? it shot up") == "news_explain"


def test_classify_context_default() -> None:
    assert classify_question("What is the current regime?") == "context"


def test_classify_divergence() -> None:
    assert classify_question("Any RSI divergence on SPY 5m?") == "divergence"


def test_answer_divergence_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    res = answer_divergence("", "Any RSI divergence on SPY 5m?", bars_provider=None)
    assert "not configured" in (res.body or "").lower()
