from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable

import aiohttp


def _stable_id(*parts: str) -> str:
    h = hashlib.sha1()  # noqa: S324
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def _parse_dt_utc(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            raw = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except Exception:
        return None


def _parse_hhmm(value: Any) -> tuple[int, int] | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Common formats: HH:MM, HH:MM:SS, H:MM AM/PM
    try:
        if "am" in s.lower() or "pm" in s.lower():
            ss = s.upper().replace(".", "").strip()
            # Accept "8:30 AM" / "08:30AM".
            for fmt in ("%I:%M %p", "%I:%M%p", "%I:%M:%S %p", "%I:%M:%S%p"):
                try:
                    dt = datetime.strptime(ss, fmt)
                    return int(dt.hour), int(dt.minute)
                except Exception:
                    continue
            return None

        parts = s.split(":")
        if len(parts) < 2:
            return None
        hh = int(parts[0])
        mm = int(parts[1])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm
    except Exception:
        return None
    return None


def infer_macro_type(title: str) -> str | None:
    t = (title or "").strip().lower()
    if not t:
        return None

    # Keep these conservative and stable — they drive risk gates.
    if "fomc" in t or "fed decision" in t or "rate decision" in t or "interest rate decision" in t:
        return "FOMC"
    if "powell" in t or "fed chair" in t or "press conference" in t and "fed" in t:
        return "FOMC"

    if "cpi" in t or "consumer price" in t:
        return "CPI"
    if "ppi" in t or "producer price" in t:
        return "PPI"
    if "pce" in t or "personal consumption expenditures" in t:
        return "PCE"

    if "nonfarm" in t or "non-farm" in t or "payroll" in t or "nfp" in t:
        return "NFP"
    if "jobless" in t or "initial claims" in t or "unemployment claims" in t:
        return "JOBLESS"

    if "gdp" in t:
        return "GDP"
    if "retail sales" in t:
        return "RETAIL_SALES"
    if "ism" in t and ("pmi" in t or "manufact" in t or "services" in t):
        return "PMI"
    if "pmi" in t:
        return "PMI"

    return None


def _infer_impact(value: Any) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return "MED"
    if s.isdigit():
        try:
            v = int(s)
            if v >= 3:
                return "HIGH"
            if v == 2:
                return "MED"
            return "LOW"
        except Exception:
            return "MED"
    if "high" in s or "important" in s or "major" in s:
        return "HIGH"
    if "low" in s or "minor" in s:
        return "LOW"
    if "med" in s or "medium" in s or "moderate" in s:
        return "MED"
    return "MED"


def _infer_title(item: dict[str, Any]) -> str:
    for k in (
        "title",
        "event",
        "event_name",
        "name",
        "description",
        "indicator",
        "indicator_name",
    ):
        v = item.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _infer_ts_utc(item: dict[str, Any]) -> datetime | None:
    # Prefer explicit timestamps.
    for k in (
        "ts_utc",
        "timestamp_utc",
        "time_utc",
        "datetime_utc",
        "starts_at",
        "start",
        "date_time",
        "datetime",
    ):
        dt = _parse_dt_utc(item.get(k))
        if dt is not None:
            return dt

    d = _parse_date(item.get("date") or item.get("event_date") or item.get("calendar_date"))
    if d is None:
        return None

    # Benzinga-style often uses ET times.
    hhmm = _parse_hhmm(item.get("time") or item.get("time_et") or item.get("event_time"))
    if hhmm is None:
        # Conservative default: 08:30 ET (most US macro releases)
        hhmm = (8, 30)

    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        dt_et = datetime.combine(d, time(hhmm[0], hhmm[1]), tzinfo=et)
        return dt_et.astimezone(timezone.utc)
    except Exception:
        # Fallback: treat it as UTC.
        return datetime.combine(d, time(hhmm[0], hhmm[1]), tzinfo=timezone.utc)


@dataclass(frozen=True)
class NormalizedMacroItem:
    id: str
    type: str
    ts_utc: datetime
    title: str
    impact: str = "MED"
    source: str = "benzinga"

    def to_macro_payload(self) -> dict[str, Any]:
        return {"type": self.type, "ts_utc": self.ts_utc.isoformat()}


async def fetch_benzinga_economic_calendar(
    *,
    base_url: str,
    api_key: str,
    start_date: Any | None = None,
    end_date: Any | None = None,
    limit: int = 200,
    timeout_s: float = 15.0,
    countries: Iterable[str] | None = None,
) -> list[NormalizedMacroItem]:
    """Fetch Benzinga economic calendar via Massive: GET /benzinga/v1/economic-calendar."""

    base = (base_url or "").rstrip("/")
    if not base or not api_key:
        return []

    want_countries = {str(c).strip().upper() for c in (countries or []) if str(c).strip()}

    base_params: dict[str, Any] = {}
    if limit and int(limit) > 0:
        base_params["limit"] = int(limit)

    sd = _parse_date(start_date)
    ed = _parse_date(end_date)
    sd_s = sd.isoformat() if sd is not None else ""
    ed_s = ed.isoformat() if ed is not None else ""

    date_param_variants: list[dict[str, Any]] = [{}]
    if sd_s or ed_s:
        pairs = [
            ("date_from", "date_to"),
            ("from", "to"),
            ("start_date", "end_date"),
            ("start", "end"),
            ("dateFrom", "dateTo"),
            ("startDate", "endDate"),
            ("date.gte", "date.lte"),
        ]
        for k_from, k_to in pairs:
            dct: dict[str, Any] = {}
            if sd_s:
                dct[k_from] = sd_s
            if ed_s:
                dct[k_to] = ed_s
            if dct:
                date_param_variants.append(dct)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ZeroDTE-pipeline/benzinga-economic-calendar",
    }

    url = f"{base}/benzinga/v1/economic-calendar"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def _get_payload(params: dict[str, Any]) -> Any:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={**params, "apiKey": api_key}, headers=headers) as resp:
                status = int(resp.status)
                if status == 429 or 500 <= status <= 599:
                    raise RuntimeError(f"Benzinga economic-calendar upstream error status={status}")
                if status != 200:
                    raise RuntimeError(f"Benzinga economic-calendar request failed status={status}")
                return await resp.json(content_type=None)

    payload: Any = None
    for params in date_param_variants:
        payload = await _get_payload({**base_params, **params})

        items_raw: Any = None
        if isinstance(payload, dict):
            items_raw = payload.get("results") or payload.get("data") or payload.get("events")
        else:
            items_raw = payload

        if not isinstance(items_raw, list):
            continue

        out: list[NormalizedMacroItem] = []
        for it in items_raw:
            if not isinstance(it, dict):
                continue

            title = _infer_title(it)
            typ = infer_macro_type(title)
            if not typ:
                continue

            country = str(it.get("country") or it.get("region") or "").strip().upper()
            if want_countries and country and country not in want_countries:
                continue

            ts = _infer_ts_utc(it)
            if ts is None:
                continue

            impact = _infer_impact(it.get("importance") or it.get("impact") or it.get("priority"))

            raw_id = it.get("id") or it.get("benzinga_id") or it.get("uuid")
            eid = str(raw_id).strip() if raw_id is not None else ""
            if not eid:
                eid = _stable_id(typ, ts.isoformat(), title)

            out.append(NormalizedMacroItem(id=eid, type=str(typ).upper(), ts_utc=ts, title=title, impact=impact))

        out.sort(key=lambda x: x.ts_utc)
        if out:
            return out

    return []
