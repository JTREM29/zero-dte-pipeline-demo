from __future__ import annotations

from typing import Any


def _sf(x: object) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except Exception:
        return None
    if not (v == v):
        return None
    return v


def earnings_reaction_answer_header(symbol: str, payload: dict[str, Any]) -> str:
    """Deterministic 1–2 line answer header for earnings reaction questions.

    Uses only the pre-classified tag field when available (gap, continuation/fade/chop/inside).
    """

    sym = (symbol or "").strip().upper() or "?"
    hist = payload.get("history") if isinstance(payload.get("history"), list) else []
    rows = [x for x in hist[:4] if isinstance(x, dict)]

    cont = 0
    fade = 0
    chop = 0
    have = 0

    for it in rows:
        tag = str(it.get("tag") or "").strip().lower()

        # Prefer explicit tags.
        if tag:
            have += 1
            if "continuation" in tag:
                cont += 1
            elif "fade" in tag:
                fade += 1
            elif "chop" in tag or "inside" in tag or "range" in tag:
                chop += 1
            else:
                # Unknown tag -> count as "chop" bucket to avoid over-confidence.
                chop += 1
            continue

        # Fallback: infer continuation vs fade from gap+move if present.
        g = _sf(it.get("gap_pct"))
        m = _sf(it.get("move_pct"))
        if g is None or m is None:
            continue
        have += 1
        if abs(g) < 1.0 and abs(m) < 1.0:
            chop += 1
        elif (g >= 0) == (m >= 0):
            cont += 1
        else:
            fade += 1

    if have == 0:
        return f"**{sym} can move sharply on earnings; recent reaction history isn’t available yet.**"

    if cont >= 2 and cont > fade:
        head = f"**{sym} often gaps and continues post-earnings.**"
    elif fade >= 2 and fade > cont:
        head = f"**{sym} often gaps and fades post-earnings.**"
    elif chop >= 2:
        head = f"**{sym} earnings reactions have been choppy/mixed recently.**"
    else:
        head = f"**{sym} earnings reactions have been mixed recently (no dominant pattern).**"

    return head + "\nBelow: implied range + prior reactions."


def earnings_risk_answer_header(symbol: str, payload: dict[str, Any]) -> str:
    """Deterministic 1–2 line answer header for earnings risk questions."""

    sym = (symbol or "").strip().upper() or "?"

    em = _sf(payload.get("expected_move_pct"))
    iv_state = payload.get("iv_state") if isinstance(payload.get("iv_state"), dict) else {}
    crush = str((iv_state or {}).get("crush_risk") or "").strip().upper()

    def _explicit_no_confirmed_date(p: dict[str, Any]) -> bool:
        # Used by sparse/missing-payload path to avoid over-claiming.
        if p.get("tnt_no_confirmed_earnings_date") is True:
            return True
        if p.get("date_confirmed") is False:
            return True
        return False

    if em is None:
        if _explicit_no_confirmed_date(payload):
            base = f"**No confirmed {sym} earnings date available right now — risk assessment updates once a date posts.**"
        else:
            base = f"**{sym} earnings risk is unclear (no options-implied expected move available).**"
    else:
        if em < 4.0:
            band = "lower-than-average"
        elif em < 6.0:
            band = "moderate"
        elif em < 8.0:
            band = "high"
        else:
            band = "very high"

        base = f"**Yes — {sym} earnings risk is {band.upper()} (options imply ~±{em:.1f}% move).**"

    if crush in {"HIGH", "MED", "LOW"}:
        base += f" IV crush risk: {crush}."

    return base + "\nBelow: implied range + prior reactions."


def earnings_should_trade_answer_header(symbol: str) -> str:
    sym = (symbol or "").strip().upper() or "?"
    return (
        f"**{sym} earnings:** TNT avoids trading into earnings volatility.\n"
        "Safer opportunities appear after the post-earnings reaction confirms structure."
    )
