import pytest

from tests.fixtures_signal_payload import make_signal_payload

# Import the real module-scope renderers.
from cli.discord_bot import _render_analyze_text, _render_ask_text, _render_trade_text


def _nonempty_lines(txt: str) -> list[str]:
    return [ln.strip() for ln in (txt or "").splitlines() if ln.strip()]


@pytest.mark.parametrize("renderer", [_render_ask_text, _render_analyze_text, _render_trade_text])
def test_numeric_bias_pack_is_first_block(renderer) -> None:
    signal_payload = make_signal_payload("SPY")
    payload = {
        "symbol": "SPY",
        "signal_payload": signal_payload,
        "body": "Body block placeholder",
    }

    txt = renderer(payload)
    assert isinstance(txt, str)
    assert txt.strip(), "Renderer returned empty output"

    lines = _nonempty_lines(txt)
    assert lines, txt

    # Allow a command title line first; pack must be next.
    first = lines[0]
    second = lines[1] if len(lines) > 1 else ""

    assert "Numeric Bias Pack" in second, (
        "Numeric Bias Pack must be the first block after the title line.\n\n"
        f"Title line: {first}\n"
        f"First content line: {second}\n\nFull text:\n{txt}"
    )


@pytest.mark.parametrize("renderer", [_render_ask_text, _render_analyze_text, _render_trade_text])
def test_numeric_bias_pack_contains_required_fields(renderer) -> None:
    signal_payload = make_signal_payload("SPY")
    payload = {
        "symbol": "SPY",
        "signal_payload": signal_payload,
        "body": "Body block placeholder",
    }

    txt = renderer(payload)

    # Header + required semantics
    assert "Numeric Bias Pack" in txt
    assert "Bias" in txt
    assert "Net" in txt
    assert "Conv" in txt


def test_trade_renderer_injects_pack_even_if_body_missing_it() -> None:
    signal_payload = make_signal_payload("SPY")
    payload = {
        "symbol": "SPY",
        "signal_payload": signal_payload,
        "body": "\n".join([
            "🛡️ TNT Discipline Gate: **ON**",
            "Decision: STAND DOWN",
        ]),
    }

    txt = _render_trade_text(payload)
    lines = _nonempty_lines(txt)
    assert "Numeric Bias Pack" in lines[1], txt


def test_trade_no_trade_permission_still_has_pack_first_and_stand_down_language() -> None:
    signal_payload = make_signal_payload("SPY", no_trade=True)
    payload = {
        "symbol": "SPY",
        "signal_payload": signal_payload,
        "body": "🛡️ TNT Discipline Gate: **ON**",
    }

    txt = _render_trade_text(payload)
    lines = _nonempty_lines(txt)
    assert "Numeric Bias Pack" in lines[1], txt
    upper = txt.upper()
    assert ("STAND DOWN" in upper) or ("NO TRADE" in upper), txt
