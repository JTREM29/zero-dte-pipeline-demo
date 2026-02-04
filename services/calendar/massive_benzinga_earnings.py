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


@dataclass(frozen=True)
class NormalizedEarningsItem:
    id: str
    symbol: str
    ts_utc: datetime
    confirmed: bool
    source: str = "benzinga"

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "ts_utc": self.ts_utc.isoformat(),
            "confirmed": bool(self.confirmed),
            "source": self.source,
        }


def _infer_confirmed(item: dict[str, Any]) -> bool:
    for k in ("confirmed", "is_confirmed", "isConfirmed"):
        if k in item:
            return bool(item.get(k))
    # If API doesn't provide it, treat as confirmed; the gate has confirmed_only option anyway.
    return True


def _infer_dt_from_fields(item: dict[str, Any]) -> datetime | None:
    # Common candidates
    for k in ("ts_utc", "earnings_ts_utc", "datetime", "date_time", "time_utc", "starts_at"):
        dt = _parse_dt_utc(item.get(k))
        if dt is not None:
            return dt

    # Benzinga-style often provides a date plus a session like "bmo"/"amc".
    d = _parse_date(item.get("date") or item.get("report_date") or item.get("earnings_date"))
    if d is None:
        return None

    session = str(item.get("time") or item.get("session") or item.get("when") or "").strip().lower()
    # Approximate ET anchors converted to UTC:
    # - BMO: 08:10 ET ~ 13:10 UTC (winter)
    # - AMC: 16:10 ET ~ 21:10 UTC (winter)
    # We store UTC and keep it stable.
    if session in {"bmo", "before", "before_open", "before open", "pre", "premarket"}:
        hh, mm = 13, 10
    elif session in {"amc", "after", "after_close", "after close", "post", "postmarket"}:
        hh, mm = 21, 10
    else:
        # Unknown: use 16:00 ET-ish (close) as a neutral default.
        hh, mm = 21, 0

    return datetime.combine(d, time(hh, mm), tzinfo=timezone.utc)


async def fetch_benzinga_earnings(
    *,
    base_url: str,
    api_key: str,
    tickers: Iterable[str] | None = None,
    start_date: Any | None = None,
    end_date: Any | None = None,
    limit: int = 100,
    timeout_s: float = 15.0,
) -> list[NormalizedEarningsItem]:
    """Fetch Benzinga earnings via Massive: GET /benzinga/v1/earnings.

    Returns normalized earnings records; caller can pick the next upcoming.
    """

    base = (base_url or "").rstrip("/")
    if not base or not api_key:
        return []

    cleaned: list[str] = []
    if tickers:
        cleaned = [str(t).strip().upper() for t in tickers if str(t).strip()]
    want = {t for t in cleaned if t}

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
        ]
        for k_from, k_to in pairs:
            d: dict[str, Any] = {}
            if sd_s:
                d[k_from] = sd_s
            if ed_s:
                d[k_to] = ed_s
            if d:
                date_param_variants.append(d)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ZeroDTE-pipeline/benzinga-earnings",
    }

    url = f"{base}/benzinga/v1/earnings"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def _get_payload(params: dict[str, Any]) -> Any:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={**params, "apiKey": api_key}, headers=headers) as resp:
                status = int(resp.status)
                if status == 429 or 500 <= status <= 599:
                    raise RuntimeError(f"Benzinga earnings upstream error status={status}")
                if status != 200:
                    # Treat non-200 as a hard failure so callers can surface the issue
                    # (otherwise we silently return empty results and the cache stays cold).
                    raise RuntimeError(f"Benzinga earnings request failed status={status}")
                return await resp.json(content_type=None)

    # Some upstreams accept symbol filters under different names.
    # Try a small set of known keys until we actually observe any hits.
    attempts: list[dict[str, Any]] = []
    sym_variants: list[dict[str, Any]]
    if want:
        joined = ",".join(sorted(want))
        sym_variants = [
            {"tickers": joined},
            {"symbols": joined},
            {"ticker": joined},
            {"symbol": joined},
        ]
    else:
        sym_variants = [{}]

    for sp in sym_variants:
        for dp in date_param_variants:
            attempts.append({**base_params, **sp, **dp})

    payload: Any = None
    for params in attempts:
        payload = await _get_payload(params)

        items_raw: Any = None
        if isinstance(payload, dict):
            items_raw = payload.get("results") or payload.get("data") or payload.get("earnings")
        else:
            items_raw = payload

        if not isinstance(items_raw, list):
            continue

        out: list[NormalizedEarningsItem] = []
        for it in items_raw:
            if not isinstance(it, dict):
                continue

            sym = str(it.get("symbol") or it.get("ticker") or "").strip().upper()
            if not sym:
                continue
            if want and sym not in want:
                continue

            dt = _infer_dt_from_fields(it)
            if dt is None:
                continue

            confirmed = _infer_confirmed(it)

            raw_id = it.get("id") or it.get("earnings_id") or it.get("uuid")
            eid = str(raw_id).strip() if raw_id is not None else ""
            if not eid:
                eid = _stable_id(sym, dt.isoformat())

            out.append(NormalizedEarningsItem(id=eid, symbol=sym, ts_utc=dt, confirmed=confirmed))

        out.sort(key=lambda x: x.ts_utc)
        if out or not want:
            return out

    return []


def pick_next_earnings(
    records: list[NormalizedEarningsItem], *, symbol: str, now_utc: datetime
) -> NormalizedEarningsItem | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    now = now_utc.astimezone(timezone.utc)
    for r in records or []:
        if r.symbol != sym:
            continue
        if r.ts_utc >= now:
            return r
    return None
