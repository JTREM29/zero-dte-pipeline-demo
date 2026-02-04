from __future__ import annotations

from datetime import date
from typing import Iterable

import requests


def _safe_upper(s: str) -> str:
    return str(s or "").strip().upper()


def _parse_date_ymd(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except Exception:
        return None


def discover_expirations(
    *,
    symbol: str,
    start_ymd: str,
    max_expirations: int = 24,
    strike_window_pct: float = 0.10,
    http_timeout_s: float = 10.0,
) -> list[str]:
    """Best-effort expiration discovery via Polygon options contracts endpoint.

    Returns a sorted list of YYYY-MM-DD expiration dates.

    This intentionally uses the reference/contracts endpoint because it is
    much cheaper than fetching full snapshots/chains across multiple expiries.
    """

    sym = _safe_upper(symbol)
    if not sym:
        return []

    start = str(start_ymd or "").strip()
    if not start:
        return []

    try:
        # Import from delivery layer to reuse Polygon base + API key + underlying mapping + last price.
        from delivery import discord_bot as delivery

        api_key, base_url = delivery._polygon_key_and_base()
        if not api_key:
            return []

        underlying = delivery._map_underlying_for_options(sym)

        underlying_px = None
        try:
            snap = delivery._get_last_price_snapshot(sym)
            if snap and getattr(snap, "px", None) is not None:
                underlying_px = float(getattr(snap, "px"))
        except Exception:
            underlying_px = None

        strike_min = strike_max = None
        if isinstance(underlying_px, (int, float)) and underlying_px and underlying_px > 0:
            strike_min = underlying_px * (1.0 - float(strike_window_pct))
            strike_max = underlying_px * (1.0 + float(strike_window_pct))

        params: dict[str, object] = {
            "underlying_ticker": underlying,
            "expiration_date.gte": start,
            "limit": 1000,
            "apiKey": api_key,
        }
        if strike_min is not None and strike_max is not None:
            params["strike_price.gte"] = f"{strike_min:.6f}"
            params["strike_price.lte"] = f"{strike_max:.6f}"

        url = f"{str(base_url).rstrip('/')}/v3/reference/options/contracts"
        resp = requests.get(url, params=params, timeout=float(http_timeout_s))
        if resp.status_code != 200:
            return []
        payload = resp.json() if resp.content else {}

        if not isinstance(payload, dict) or payload.get("status") != "OK":
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            return []

        expirations: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            exp = str(item.get("expiration_date") or "").strip()
            if exp:
                expirations.add(exp[:10])

        out = sorted(expirations)
        return out[: max(1, int(max_expirations))]

    except Exception:
        return []


def filter_mwf(expirations: Iterable[str]) -> list[str]:
    """Filter expirations to Mon/Wed/Fri based on calendar weekday."""

    out: list[str] = []
    for exp in expirations or []:
        d = _parse_date_ymd(str(exp))
        if d is None:
            continue
        if int(d.weekday()) in {0, 2, 4}:
            out.append(d.isoformat())
    return sorted(set(out))
