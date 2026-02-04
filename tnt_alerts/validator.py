from __future__ import annotations

import re

from .errors import AlertValidationError
from .models import AlertIntentV1, ExpiresDuration
from .reasons import (
    ERR_INVALID_NUMBER,
    ERR_INVALID_TIME_WINDOW,
    ERR_TOO_MANY_SYMBOLS,
    ERR_UNSUPPORTED_TIMEFRAME,
    WARN_EXPIRY_CLAMPED,
)

_ALLOWED_TF = {"1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "1D"}


def _validate_time_hhmm(value: str) -> None:
    if not re.fullmatch(r"\d{2}:\d{2}", value or ""):
        raise AlertValidationError(ERR_INVALID_TIME_WINDOW, f"invalid time window value: {value!r}")
    hh = int(value.split(":", 1)[0])
    mm = int(value.split(":", 1)[1])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise AlertValidationError(ERR_INVALID_TIME_WINDOW, f"invalid time window value: {value!r}")


def validate_intent(intent: AlertIntentV1) -> tuple[AlertIntentV1, list[str]]:
    warnings: list[str] = []

    # Symbols / fan-out constraints
    if intent.targets.type == "symbols":
        syms = [s.strip().upper() for s in (intent.targets.symbols or []) if isinstance(s, str) and s.strip()]
        if not syms:
            raise AlertValidationError(ERR_INVALID_NUMBER, "no symbols specified")
        if len(syms) > int(intent.targets.max_symbols or 20):
            raise AlertValidationError(
                ERR_TOO_MANY_SYMBOLS,
                f"too many symbols ({len(syms)}; max {intent.targets.max_symbols})",
                suggestion="Split into multiple /alert requests or use watchlist:<name> with <=20 symbols.",
            )
    else:
        if not (intent.targets.watchlist or "").strip():
            raise AlertValidationError(ERR_INVALID_NUMBER, "watchlist name missing (use watchlist:default)")

    # Timeframe
    tf = getattr(intent.condition, "timeframe", None)
    if tf and str(tf) not in _ALLOWED_TF:
        raise AlertValidationError(
            ERR_UNSUPPORTED_TIMEFRAME,
            f"unsupported timeframe {tf!r}",
            suggestion=f"Allowed: {', '.join(sorted(_ALLOWED_TF))}",
        )

    # Market hours gate
    mh = intent.gates.market_hours
    if mh and mh.time_window_et is not None:
        start, end = mh.time_window_et
        _validate_time_hhmm(start)
        _validate_time_hhmm(end)

    # Confidence gate
    if intent.gates.confidence_min is not None:
        v = float(intent.gates.confidence_min)
        if v < 0.0 or v > 1.0:
            raise AlertValidationError(ERR_INVALID_NUMBER, "confidence must be between 0 and 1")

    # Cooldown
    if intent.gates.cooldown and int(intent.gates.cooldown.seconds) < 0:
        raise AlertValidationError(ERR_INVALID_NUMBER, "cooldown.seconds must be >= 0")

    if intent.gates.max_triggers is not None and int(intent.gates.max_triggers) <= 0:
        raise AlertValidationError(ERR_INVALID_NUMBER, "max_triggers must be >= 1")

    # Expiry cap (v1): 30d
    exp = intent.lifecycle.expires
    if isinstance(exp, ExpiresDuration) and exp.days is not None:
        if int(exp.days) > 30:
            exp.days = 30  # type: ignore[misc]
            warnings.append(WARN_EXPIRY_CLAMPED)

    return intent, warnings
