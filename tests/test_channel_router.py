import os

import pytest

from delivery.channel_router import decide_route


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    # Ensure we control env parsing behavior inside router module.
    for k in (
        "ASK_TNT_CHANNEL_ID",
        "ALERTS_CHANNEL_ID",
        "CALENDAR_EARNINGS_CHANNEL_ID",
        "AI_TRADE_JOURNAL_CHANNEL_ID",
        "PAPER_DESK_TRADES_CHANNEL_ID",
        "PAPER_DESK_DISCUSSION_CHANNEL_ID",
        "ANNOUNCEMENTS_CHANNEL_ID",
        "HOW_TO_USE_TNT_CHANNEL_ID",
        "TNT_CANARY_CHANNEL_ID",
        "TNT_CHANNEL_ROUTER_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def test_decide_route_allows_ask_tnt_by_id():
    os.environ["ASK_TNT_CHANNEL_ID"] = "123"
    d = decide_route(channel_id=123, content="@bot what do futures look like?")
    assert d.action == "allow_full"


def test_decide_route_allows_ask_tnt_by_name_when_id_missing():
    # Router can be enabled by operator without providing ASK_TNT_CHANNEL_ID.
    # In that case, channel name fallback must prevent a lockout.
    d = decide_route(channel_id=999, channel_name="ask-tnt", content="@bot what do futures look like?")
    assert d.action == "allow_full"


def test_decide_route_redirects_other_channels_without_mapping():
    d = decide_route(channel_id=999, channel_name="alerts", content="@bot what do futures look like?")
    assert d.action == "redirect"
    assert d.reply_text
