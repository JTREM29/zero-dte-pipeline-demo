from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _resolve_massive_key() -> str:
    key = (os.getenv("MASSIVE_API_KEY") or "").strip()
    if key:
        return key
    return (os.getenv("POLYGON_API_KEY") or "").strip()


def _resolve_massive_base() -> str:
    return (os.getenv("MASSIVE_BASE_URL") or "https://api.massive.com").strip().rstrip("/")


def _to_et(dt_utc: datetime) -> tuple[str, str]:
    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        dte = dt_utc.astimezone(et)
        return dte.strftime("%Y-%m-%d"), dte.strftime("%H:%M")
    except Exception:
        dtu = dt_utc.astimezone(timezone.utc)
        return dtu.strftime("%Y-%m-%d"), dtu.strftime("%H:%M")


async def _run(days_ahead: int, *, countries: list[str], write_path: Path) -> int:
    from services.calendar.massive_benzinga_macro import fetch_benzinga_economic_calendar

    key = _resolve_massive_key()
    base = _resolve_massive_base()
    if not key:
        raise SystemExit("Missing MASSIVE_API_KEY (or POLYGON_API_KEY fallback)")

    now_utc = datetime.now(timezone.utc)
    start = now_utc.date()
    end = (now_utc + timedelta(days=int(days_ahead))).date()

    items = await fetch_benzinga_economic_calendar(
        base_url=base,
        api_key=key,
        start_date=start,
        end_date=end,
        limit=500,
        countries=countries,
    )

    write_path.parent.mkdir(parents=True, exist_ok=True)

    with write_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date_et", "time_et", "impact", "title", "type", "ts_utc", "source"])
        w.writeheader()
        for it in items:
            date_et, time_et = _to_et(it.ts_utc)
            w.writerow(
                {
                    "date_et": date_et,
                    "time_et": time_et,
                    "impact": it.impact,
                    "title": it.title,
                    "type": it.type,
                    "ts_utc": it.ts_utc.isoformat(),
                    "source": it.source,
                }
            )

    return len(items)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=int(os.getenv("TNT_MACRO_CALENDAR_DAYS_AHEAD", "21") or "21"))
    ap.add_argument("--country", action="append", default=["US"], help="Filter country codes (repeatable). Default: US")
    ap.add_argument("--out", default=os.getenv("TNT_MACRO_EVENTS_FILE") or os.getenv("MACRO_EVENTS_FILE") or "data/macro_events.csv")
    args = ap.parse_args()

    import asyncio

    n = asyncio.run(_run(int(args.days), countries=list(args.country or []), write_path=Path(args.out)))
    print(f"[OK] wrote macro events: {n} -> {args.out}")


if __name__ == "__main__":
    main()
