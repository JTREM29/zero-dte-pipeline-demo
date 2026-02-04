import json
import time

from delivery.moderator_audit import ModeratorAudit
from services.moderator.moderator_authority import Decision, DataState, Intent


def test_audit_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)

    audit = ModeratorAudit(enabled=True, root_dir=str(tmp_path), include_text=False)

    decision = Decision(
        intent=Intent.TRADE_ACTION,
        state=DataState.FUTURES_ONLY,
        confidence="LOW",
        reply="hello",
        reason="unit",
    )

    audit.log(
        decision=decision,
        source="ask_tnt_free_text",
        user_id=123,
        channel_id=456,
        is_admin=False,
        owner_available=False,
        draining=False,
        safe_mode=False,
        live_hours=True,
        text="What strike should I buy?",
        ctx_market={"ts_utc": 1_700_000_000},
        ctx_sym=None,
    )

    day_dir = tmp_path / "2023-11-14"  # 1700000000 epoch
    path = day_dir / "moderator.jsonl"
    assert path.exists()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])

    assert rec["source"] == "ask_tnt_free_text"
    assert rec["user_id"] == 123
    assert rec["channel_id"] == 456
    assert rec["intent"] == Intent.TRADE_ACTION.value
    assert rec["state"] == DataState.FUTURES_ONLY.value
    assert rec["market_ok"] is True
    assert rec["symbol_ok"] is False
    assert "text" not in rec  # include_text=False
