from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


MWF_DOW = {0, 2, 4}  # Mon/Wed/Fri
DEFAULT_UNIVERSE = ("NVDA", "TSLA", "AAPL", "AMZN", "AVGO", "GOOGL", "MSFT", "META")


@dataclass(frozen=True)
class ExpiryResolution:
    symbol: str
    date_et: str
    ok: bool
    expiry_ymd: str | None
    reason: str
    details: dict[str, Any] | None = None


def _et_tz():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone.utc


def _parse_date_ymd(value: str) -> date | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def _date_et_string(d: date) -> str:
    return d.isoformat()


def _earnings_blocks_today(*, earnings_payload: Any, today_et: date) -> bool:
    """True if the earnings event is on the same ET calendar date."""

    if not isinstance(earnings_payload, dict):
        return False
    ts = str(earnings_payload.get("ts_utc") or "").strip()
    if not ts:
        return False
    try:
        raw = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_et = dt.astimezone(_et_tz())
        return dt_et.date() == today_et
    except Exception:
        return False


def resolve_mwf_expiry(
    *,
    symbol: str,
    today_et: date | None = None,
    universe: Iterable[str] = DEFAULT_UNIVERSE,
    earnings_payload: Any | None = None,
    available_expiries: Iterable[str] | None = None,
    discover_expiries: Callable[[str, str], Iterable[str]] | None = None,
    non_mwf_mode: str = "strict",
) -> ExpiryResolution:
    """Deterministic resolver for the M/W/F expiry pack.

    Rules (v1 production-safe):
    - If today is Mon/Wed/Fri:
        - If earnings event is today (ET) => earnings_blocked
        - Else if same-day expiry exists => ok
        - Else => no_expiry_available
    - If today is not Mon/Wed/Fri:
        - If non_mwf_mode == "next_mwf": return nearest next M/W/F expiry >= today (reason=next_mwf)
        - Else: return not_mwf

    Notes:
    - This intentionally does NOT probe the options chain to discover actual listed expiries.
      The render/build steps handle "no_data" gracefully if the feed doesn't support that expiry.
    """

    sym = str(symbol or "").strip().upper()
    uni = {str(s or "").strip().upper() for s in universe if str(s or "").strip()}

    if today_et is None:
        now_et = datetime.now(_et_tz())
        today_et = now_et.date()

    date_str = _date_et_string(today_et)

    if not sym:
        return ExpiryResolution(symbol=sym, date_et=date_str, ok=False, expiry_ymd=None, reason="missing_symbol")

    if sym not in uni:
        return ExpiryResolution(symbol=sym, date_et=date_str, ok=False, expiry_ymd=None, reason="not_in_universe")

    # Expiry universe (best-effort). Prefer explicit list; else allow a discovery callback.
    expiries: list[str] = []
    if available_expiries is not None:
        expiries = [str(x).strip()[:10] for x in available_expiries if str(x).strip()]
    elif callable(discover_expiries):
        try:
            expiries = [str(x).strip()[:10] for x in (discover_expiries(sym, date_str) or []) if str(x).strip()]
        except Exception:
            expiries = []
    expiries = sorted(set(expiries))

    is_mwf_today = int(today_et.weekday()) in MWF_DOW

    if is_mwf_today:
        if _earnings_blocks_today(earnings_payload=earnings_payload, today_et=today_et):
            return ExpiryResolution(
                symbol=sym,
                date_et=date_str,
                ok=False,
                expiry_ymd=None,
                reason="earnings_blocked",
                details={"earnings_ts_utc": str(getattr(earnings_payload, "get", lambda _k, _d=None: None)("ts_utc", None) or "")},
            )

        if date_str in expiries:
            return ExpiryResolution(symbol=sym, date_et=date_str, ok=True, expiry_ymd=date_str, reason="ok")

        return ExpiryResolution(
            symbol=sym,
            date_et=date_str,
            ok=False,
            expiry_ymd=None,
            reason="no_expiry_available",
            details={"date": date_str, "expiries": expiries[:12]},
        )

    # Not a M/W/F day.
    mode = str(non_mwf_mode or "").strip().lower() or "next_mwf"
    if mode not in {"next_mwf", "strict"}:
        mode = "next_mwf"
    if mode == "strict":
        return ExpiryResolution(symbol=sym, date_et=date_str, ok=False, expiry_ymd=None, reason="not_mwf")

    # Nearest next expiry >= today among known expiries.
    next_exp = None
    for exp in expiries:
        d = _parse_date_ymd(exp)
        if d is None:
            continue
        if d >= today_et and int(d.weekday()) in MWF_DOW:
            next_exp = exp
            break

    if next_exp:
        return ExpiryResolution(symbol=sym, date_et=date_str, ok=True, expiry_ymd=next_exp, reason="next_mwf")

    return ExpiryResolution(
        symbol=sym,
        date_et=date_str,
        ok=False,
        expiry_ymd=None,
        reason="no_expiry_available",
        details={"date": date_str, "expiries": expiries[:12]},
    )
