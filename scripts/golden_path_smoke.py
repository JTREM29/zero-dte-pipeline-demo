from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.redis_env import redis_client
from services.triple_expiry.keys import pack_key


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date_ymd(value: str | None) -> date:
    s = str(value or "").strip()
    return date.fromisoformat(s) if s else datetime.now().astimezone().date()


def _log_path(date_et: str) -> Path:
    out_dir = ROOT / "logs" / "golden_path" / date_et
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"smoke_{datetime.now(timezone.utc).strftime('%H%M%S')}.jsonl"


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")
    except Exception:
        pass


def _load_pack(r: Any, symbol: str, date_et: str) -> dict[str, Any] | None:
    sym = str(symbol).strip().upper()

    raw0 = r.get(pack_key(sym, date_et))
    if raw0 is None:
        return None
    try:
        obj0 = json.loads(raw0)
    except Exception:
        return None
    if not isinstance(obj0, dict):
        return None

    exp = str(obj0.get("expiry") or "").strip()
    if exp:
        raw = r.get(pack_key(sym, exp))
        if raw is not None:
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                return obj

    return obj0


def main() -> int:
    ap = argparse.ArgumentParser(description="Golden-path smoke: build+render one premium pack end-to-end.")
    ap.add_argument("--symbol", default="SPY")
    ap.add_argument("--date", default="", help="ET date YYYY-MM-DD (default: today)")
    ap.add_argument("--ttl-sec", type=int, default=6 * 3600)
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--dpi", type=int, default=160)
    ap.add_argument("--timeout-s", type=float, default=240.0)
    args = ap.parse_args()

    sym = str(args.symbol).strip().upper()
    date_obj = _parse_date_ymd(args.date)
    date_et = date_obj.isoformat()
    log = _log_path(date_et)

    _append_jsonl(log, {"event": "start", "ts_utc": _utc_now_iso(), "symbol": sym, "date_et": date_et})

    r = redis_client()

    # Reuse the production build/render pipeline.
    from scripts.triple_expiry_fanout import build as fanout_build
    from scripts.triple_expiry_fanout import render as fanout_render

    try:
        fanout_build(r=r, date_et=date_obj, symbols=[sym], log=log, ttl_sec=int(args.ttl_sec))
        _append_jsonl(log, {"event": "build_ok", "ts_utc": _utc_now_iso(), "symbol": sym})
    except Exception as exc:  # noqa: BLE001
        _append_jsonl(log, {"event": "build_failed", "ts_utc": _utc_now_iso(), "symbol": sym, "error": f"{type(exc).__name__}: {exc}"})
        print(f"[SMOKE][FAIL] build failed for {sym}: {type(exc).__name__}: {exc}")
        return 2

    try:
        fanout_render(
            r=r,
            date_et=date_obj,
            symbols=[sym],
            log=log,
            ttl_sec=int(args.ttl_sec),
            top=int(args.top),
            dpi=int(args.dpi),
            timeout_s=float(args.timeout_s),
        )
        _append_jsonl(log, {"event": "render_done", "ts_utc": _utc_now_iso(), "symbol": sym})
    except Exception as exc:  # noqa: BLE001
        _append_jsonl(log, {"event": "render_failed", "ts_utc": _utc_now_iso(), "symbol": sym, "error": f"{type(exc).__name__}: {exc}"})
        print(f"[SMOKE][FAIL] render failed for {sym}: {type(exc).__name__}: {exc}")
        return 3

    pack = _load_pack(r, sym, date_et)
    if not pack:
        _append_jsonl(log, {"event": "verify_failed", "ts_utc": _utc_now_iso(), "symbol": sym, "reason": "pack_missing"})
        print(f"[SMOKE][FAIL] pack missing in Redis for {sym} ({date_et})")
        return 4

    ok = bool(pack.get("ok"))
    exp = str(pack.get("expiry") or "").strip()
    reason = str(pack.get("reason") or "").strip()

    price = pack.get("price") if isinstance(pack.get("price"), dict) else {}
    px_ok = bool(price.get("ok"))
    px = price.get("px")

    renders = pack.get("renders") if isinstance(pack.get("renders"), dict) else {}
    oi = renders.get("oi_iv") if isinstance(renders.get("oi_iv"), dict) else {}
    r_ok = bool(oi.get("ok"))
    url = str(oi.get("artifact_url") or "").strip() or None

    if not ok or not exp:
        _append_jsonl(log, {"event": "verify_failed", "ts_utc": _utc_now_iso(), "symbol": sym, "reason": "resolver_not_ok", "pack_reason": reason, "expiry": exp or None})
        print(f"[SMOKE][FAIL] resolver not ok for {sym}: reason={reason or 'n/a'}")
        return 5

    if not px_ok or px is None:
        _append_jsonl(log, {"event": "verify_failed", "ts_utc": _utc_now_iso(), "symbol": sym, "reason": "price_not_ok", "expiry": exp})
        print(f"[SMOKE][FAIL] price snapshot not ok for {sym} (expiry {exp})")
        return 6

    if not r_ok or not url:
        _append_jsonl(log, {"event": "verify_failed", "ts_utc": _utc_now_iso(), "symbol": sym, "reason": "render_not_ok", "expiry": exp, "artifact_url": url})
        print(f"[SMOKE][FAIL] render did not produce artifact for {sym} (expiry {exp})")
        return 7

    _append_jsonl(
        log,
        {
            "event": "ok",
            "ts_utc": _utc_now_iso(),
            "symbol": sym,
            "expiry": exp,
            "price": float(px) if isinstance(px, (int, float)) else px,
            "artifact_url": url,
        },
    )
    print(f"[SMOKE][OK] {sym} expiry {exp} px={px} artifact={url}")
    print(f"[SMOKE][LOG] {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
