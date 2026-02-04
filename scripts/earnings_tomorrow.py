from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


# Ensure workspace root is importable when running as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def main() -> int:
    # Use the same ET timezone object as the bot.
    from delivery.discord_bot import ET, fetch_earnings_for_date

    now_et = datetime.now(ET)
    target = _next_weekday(now_et.date() + timedelta(days=1))
    date_et = target.strftime("%Y-%m-%d")

    # Optional override for debugging.
    date_et = (os.getenv("DATE_ET") or date_et).strip() or date_et

    rows = fetch_earnings_for_date(date_et)

    def _bucket(label: str) -> list[str]:
        syms = [
            str(r.get("symbol") or "").strip().upper()
            for r in (rows or [])
            if isinstance(r, dict) and str(r.get("when") or "") == label
        ]
        return sorted([s for s in syms if s])

    bmo = _bucket("BMO")
    amc = _bucket("AMC")
    tas = _bucket("TAS")

    print(date_et)
    print(f"TOTAL {len(rows or [])}")
    if bmo:
        print("BMO", len(bmo), ":", ", ".join(bmo))
    if amc:
        print("AMC", len(amc), ":", ", ".join(amc))
    if tas:
        print("TAS", len(tas), ":", ", ".join(tas))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
