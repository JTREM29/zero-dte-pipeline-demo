from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import aiohttp


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


def _stable_id(*parts: str) -> str:
    h = hashlib.sha1()  # noqa: S324
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass(frozen=True)
class NormalizedNewsItem:
    id: str
    published_utc: datetime
    headline: str
    tickers: list[str]
    url: str
    summary: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "published_utc": self.published_utc.isoformat(),
            "headline": self.headline,
            "tickers": list(self.tickers),
            "url": self.url,
            "summary": self.summary,
        }


async def fetch_benzinga_news(
    *,
    base_url: str,
    api_key: str,
    tickers: Iterable[str] | None = None,
    published_since_utc: datetime | None = None,
    limit: int = 50,
    timeout_s: float = 12.0,
) -> list[NormalizedNewsItem]:
    """Fetch Benzinga news via Massive: GET /benzinga/v2/news.

    Returns a normalized list. Defensive parsing to tolerate shape drift.
    """

    base = (base_url or "").rstrip("/")
    if not base or not api_key:
        return []

    params: dict[str, Any] = {}
    if tickers:
        cleaned = [str(t).strip().upper() for t in tickers if str(t).strip()]
        if cleaned:
            # Massive typically accepts comma-separated tickers.
            params["tickers"] = ",".join(cleaned)

    if published_since_utc is not None:
        dt = published_since_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        params["published_since"] = dt.astimezone(timezone.utc).isoformat()

    if limit and int(limit) > 0:
        params["limit"] = int(limit)

    headers = {
        # Massive generally accepts apiKey query param, but we keep key in headers as well for flexibility.
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "ZeroDTE-pipeline/benzinga-news",
    }

    url = f"{base}/benzinga/v2/news"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params={**params, "apiKey": api_key}, headers=headers) as resp:
            if resp.status == 429 or 500 <= int(resp.status) <= 599:
                raise RuntimeError(f"Benzinga news upstream error status={resp.status}")
            if resp.status != 200:
                return []
            payload = await resp.json(content_type=None)

    # Try common shapes: {results:[...]} or {data:[...]} or [...] directly.
    items_raw: Any = None
    if isinstance(payload, dict):
        items_raw = payload.get("results") or payload.get("data") or payload.get("news")
    else:
        items_raw = payload

    if not isinstance(items_raw, list):
        return []

    out: list[NormalizedNewsItem] = []
    for it in items_raw:
        if not isinstance(it, dict):
            continue

        headline = str(it.get("headline") or it.get("title") or it.get("name") or "").strip()
        url_item = str(it.get("url") or it.get("article_url") or it.get("link") or "").strip()
        published = (
            it.get("published_utc")
            or it.get("published")
            or it.get("created")
            or it.get("created_at")
            or it.get("updated")
        )
        published_dt = _parse_dt_utc(published)
        if not headline or published_dt is None:
            continue

        tickers_raw = it.get("tickers") or it.get("symbols") or it.get("stocks")
        tickers_list: list[str] = []
        if isinstance(tickers_raw, str):
            tickers_list = [t.strip().upper() for t in tickers_raw.split(",") if t and str(t).strip()]
        elif isinstance(tickers_raw, list):
            tickers_list = [str(t).strip().upper() for t in tickers_raw if t and str(t).strip()]

        # Deduplicate while preserving order.
        if tickers_list:
            seen: set[str] = set()
            deduped: list[str] = []
            for t in tickers_list:
                if not t or t in seen:
                    continue
                seen.add(t)
                deduped.append(t)
            tickers_list = deduped

        summary = it.get("summary") or it.get("description") or it.get("snippet")
        summary_s = str(summary).strip() if summary is not None else None

        raw_id = it.get("id") or it.get("news_id") or it.get("uuid")
        news_id = str(raw_id).strip() if raw_id is not None else ""
        if not news_id:
            news_id = _stable_id(url_item, headline, published_dt.isoformat())

        out.append(
            NormalizedNewsItem(
                id=news_id,
                published_utc=published_dt,
                headline=headline,
                tickers=tickers_list,
                url=url_item,
                summary=summary_s or None,
            )
        )

    # Newest-first is convenient for displays.
    out.sort(key=lambda x: x.published_utc, reverse=True)
    return out
