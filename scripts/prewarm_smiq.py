"""One-shot Smart Market IQ (SMIQ) prewarm.

Usage:
  python -m scripts.prewarm_smiq --print
  python -m scripts.prewarm_smiq --force --write

This is an ops helper. It computes a market participation regime from ETF proxy
ratios using Polygon daily aggs and (optionally) writes it to SMART_MARKET_IQ_PATH.

Notes:
- Requires POLYGON_API_KEY in the current process environment.
- Writes are atomic (tmp + replace).
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")


def _compute_smart_market_iq_from_etf_proxies(*, lookback: int, window_days: int) -> Dict[str, Any]:
    try:
        from delivery.on_demand_data import polygon_aggs
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"delivery.on_demand_data import failed: {exc}") from exc

    if not (os.getenv("POLYGON_API_KEY", "") or "").strip():
        raise RuntimeError("POLYGON_API_KEY not set")

    tz = ET
    now_et = datetime.now(tz)

    sym_spy = "SPY"
    sym_qqq = "QQQ"
    sym_iwm = "IWM"
    sym_tlt = "TLT"
    sym_shy = "SHY"
    sym_uso = "USO"

    payloads = {
        sym_spy: polygon_aggs(sym_spy, multiplier=1, timespan="day", days=window_days),
        sym_qqq: polygon_aggs(sym_qqq, multiplier=1, timespan="day", days=window_days),
        sym_iwm: polygon_aggs(sym_iwm, multiplier=1, timespan="day", days=window_days),
        sym_tlt: polygon_aggs(sym_tlt, multiplier=1, timespan="day", days=window_days),
        sym_shy: polygon_aggs(sym_shy, multiplier=1, timespan="day", days=window_days),
        sym_uso: polygon_aggs(sym_uso, multiplier=1, timespan="day", days=window_days),
    }

    def _extract_close_map(payload: dict[str, Any]) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in (payload.get("results") or []):
            try:
                t_ms = float(row.get("t"))
                c = float(row.get("c"))
            except Exception:
                continue
            if c <= 0:
                continue
            dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(tz)
            out[dt.date().isoformat()] = float(c)
        return out

    maps = {k: _extract_close_map(v) for k, v in payloads.items()}
    counts = {k: len(v) for k, v in maps.items()}
    if any(int(n) < (lookback + 6) for n in counts.values()):
        raise RuntimeError(f"insufficient history counts={counts} lookback={lookback}")

    def _ratio_series(num: str, den: str) -> list[float]:
        a = maps.get(num) or {}
        b = maps.get(den) or {}
        common = sorted(set(a.keys()) & set(b.keys()))
        ys: list[float] = []
        for k in common:
            c_a = a.get(k)
            c_b = b.get(k)
            if c_a is None or c_b is None or c_b <= 0:
                continue
            ys.append(float(c_a) / float(c_b))
        return ys

    r_growth = _ratio_series(sym_qqq, sym_spy)
    r_breadth = _ratio_series(sym_iwm, sym_spy)
    r_duration = _ratio_series(sym_tlt, sym_shy)
    r_inflation = _ratio_series(sym_uso, sym_spy)

    lens = {
        "QQQ/SPY": len(r_growth),
        "IWM/SPY": len(r_breadth),
        "TLT/SHY": len(r_duration),
        "USO/SPY": len(r_inflation),
    }
    if min(lens.values()) < (lookback + 3):
        raise RuntimeError(f"insufficient overlap lens={lens} lookback={lookback} counts={counts}")

    def _pct_change(values: list[float], lb: int) -> float:
        if len(values) < 2:
            return 0.0
        idx = max(0, len(values) - 1 - lb)
        base = float(values[idx])
        last = float(values[-1])
        if base == 0:
            return 0.0
        return (last / base - 1.0) * 100.0

    def _sgn(x: float) -> int:
        if x > 0:
            return 1
        if x < 0:
            return -1
        return 0

    chg_growth = _pct_change(r_growth, lookback)
    chg_breadth = _pct_change(r_breadth, lookback)
    chg_duration = _pct_change(r_duration, lookback)
    chg_inflation = _pct_change(r_inflation, lookback)

    dir_growth = _sgn(chg_growth)
    dir_breadth = _sgn(chg_breadth)
    dir_duration = _sgn(chg_duration)
    dir_inflation = _sgn(chg_inflation)

    score = int(dir_growth + dir_breadth - dir_duration + dir_inflation)
    if score > 0:
        regime_sign = 1
    elif score < 0:
        regime_sign = -1
    else:
        regime_sign = 0

    aligned = 0
    if regime_sign != 0:
        for v in (dir_growth, dir_breadth, -dir_duration, dir_inflation):
            if int(v) == int(regime_sign):
                aligned += 1
    conflict = bool(regime_sign == 0 or aligned <= 2)

    if conflict:
        regime = "MIXED"
    elif regime_sign > 0:
        regime = "RISK_ON"
    else:
        regime = "DEFENSIVE"

    return {
        "timestamp_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "data_quality": "OK",
        "regime": regime,
        "top_gainers": [],
        "top_losers": [],
        "meta": {
            "source": "polygon_etf_proxy_ratios",
            "lookback_days": int(lookback),
            "window_days": int(window_days),
            "score": int(score),
            "aligned": int(aligned),
            "conflict": bool(conflict),
            "counts": counts,
            "lens": lens,
            "chg_qqq_spy_lookback": float(chg_growth),
            "chg_iwm_spy_lookback": float(chg_breadth),
            "chg_tlt_shy_lookback": float(chg_duration),
            "chg_uso_spy_lookback": float(chg_inflation),
        },
    }


def _default_path() -> Path:
    return Path(os.getenv("SMART_MARKET_IQ_PATH", "data/smart_market_iq.json") or "data/smart_market_iq.json").expanduser()


def _age_seconds(path: Path) -> float | None:
    try:
        if not path.exists():
            return None
        return (datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).total_seconds()
    except Exception:
        return None


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Prewarm Smart Market IQ snapshot (SMIQ).")
    ap.add_argument("--path", default="", help="Override output path (defaults to SMART_MARKET_IQ_PATH).")
    ap.add_argument("--write", action="store_true", help="Write snapshot JSON to disk.")
    ap.add_argument("--print", dest="do_print", action="store_true", help="Print a one-line summary.")
    ap.add_argument("--force", action="store_true", help="Ignore freshness and recompute.")
    ap.add_argument("--max-age-sec", type=int, default=3600, help="Skip recompute if file age <= this (unless --force).")
    ap.add_argument("--lookback", type=int, default=20, help="Lookback days for ratio change (default 20).")
    ap.add_argument("--window-days", type=int, default=80, help="History window in days (default 80).")

    args = ap.parse_args()

    out_path = Path(args.path).expanduser() if str(args.path or "").strip() else _default_path()
    lookback = min(max(int(args.lookback), 5), 60)
    window_days = min(max(int(args.window_days), 45), 260)

    age = _age_seconds(out_path)
    if (not args.force) and age is not None and int(args.max_age_sec) > 0 and age <= float(args.max_age_sec):
        if args.do_print:
            print(f"SMIQ fresh: path={out_path} age_sec={age:.0f} (<= {int(args.max_age_sec)})")
        return 0

    payload = _compute_smart_market_iq_from_etf_proxies(lookback=lookback, window_days=window_days)

    if args.write:
        _write_atomic(out_path, payload)
        print(f"[OK] wrote {out_path}")

    if args.do_print or (not args.write):
        print(
            "SMIQ computed: "
            f"regime={str(payload.get('regime') or 'UNKNOWN').upper()} "
            f"quality={payload.get('data_quality') or 'UNKNOWN'} "
            f"ts={payload.get('timestamp_et') or 'n/a'} "
            f"path={out_path}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
