from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.redis_env import redis_client
from services.calendar.calendar_keys import cal_earnings_key
from services.triple_expiry.keys import pack_key, manifest_key, render_lock_key
from services.triple_expiry.locks import acquire_lock, release_lock
from services.triple_expiry.resolver import DEFAULT_UNIVERSE, resolve_mwf_expiry
from services.triple_expiry.expiries import discover_expirations, filter_mwf
from services.triple_expiry.status import build_status_rows


def _et_tz():
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo("America/New_York")
    except Exception:
        return timezone.utc


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date_ymd(value: str) -> date:
    s = str(value or "").strip()
    if not s:
        return datetime.now(_et_tz()).date()
    return date.fromisoformat(s)


def _json_loads(s: Any) -> Any:
    if not isinstance(s, (str, bytes, bytearray)):
        return None
    try:
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="replace")
        return json.loads(str(s))
    except Exception:
        return None


def _log_path(date_et: str) -> Path:
    d = str(date_et)
    out_dir = ROOT / "logs" / "triple_expiry" / d
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"fanout_{datetime.now(timezone.utc).strftime('%H%M%S')}.jsonl"


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
            f.write("\n")
    except Exception:
        pass


def _run_id() -> str:
    # Stable-ish ID for correlating jsonl rows across build/render/post.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _impact_from_pack(symbol: str, pack: dict[str, Any]) -> dict[str, Any]:
    """Deterministic, explainable impact scoring.

    - Primary: expected_move_pct (higher => higher impact)
    - Fallback: deterministic per-symbol bucket (coarse)
    """

    sym = str(symbol or "").strip().upper()

    opt = pack.get("options") if isinstance(pack.get("options"), dict) else {}
    em = opt.get("expected_move_pct")
    if isinstance(em, (int, float)):
        score = max(0.0, float(em))
        return {
            "score": score,
            "components": {"method": "expected_move_pct", "expected_move_pct": float(em)},
        }

    # Coarse, deterministic fallback for the premium universe.
    bucket_score: dict[str, float] = {
        "NVDA": 2.6,
        "TSLA": 2.5,
        "AAPL": 2.2,
        "AMZN": 2.2,
        "AVGO": 2.1,
        "GOOGL": 2.0,
        "MSFT": 2.0,
        "META": 2.0,
    }
    score = float(bucket_score.get(sym, 1.0))
    return {"score": score, "components": {"method": "symbol_bucket", "symbol": sym, "bucket_score": score}}


def _symbols_list(value: str | None) -> list[str]:
    if not value:
        return list(DEFAULT_UNIVERSE)
    parts = [p.strip().upper() for p in str(value).replace(";", ",").split(",")]
    return [p for p in parts if p]


@dataclass(frozen=True)
class RenderOutcome:
    symbol: str
    expiry: str
    ok: bool
    job_id: str | None
    artifact_url: str | None
    out_path: str | None
    error: str | None


def _enqueue_and_wait_render_oi(*, symbol: str, expiry: str, top: int, dpi: int, timeout_s: float) -> RenderOutcome:
    from scripts.enqueue_and_wait import enqueue_and_wait

    env_host = os.getenv("TNT_REDIS_HOST", "127.0.0.1")
    env_port = int(os.getenv("TNT_REDIS_PORT", "6379"))
    env_db = int(os.getenv("TNT_REDIS_DB", "0"))
    env_queue = os.getenv("TNT_REDIS_QUEUE", "tnt:jobs")

    out = enqueue_and_wait(
        redis_host=env_host,
        redis_port=env_port,
        redis_db=env_db,
        queue=env_queue,
        symbol=symbol,
        job_type="render_oi",
        payload={"expiry": expiry, "top": int(top), "dpi": int(dpi)},
        timeout_s=float(timeout_s),
    )

    res = out.result if isinstance(out.result, dict) else {}
    artifacts = res.get("artifacts") if isinstance(res.get("artifacts"), dict) else {}
    png_obj = artifacts.get("png") if isinstance(artifacts.get("png"), dict) else {}

    artifact_url = str(png_obj.get("url") or res.get("artifact_url") or "").strip() or None
    out_path = str(png_obj.get("path") or res.get("out_path") or "").strip() or None
    err = str(res.get("error") or "").strip() or None

    return RenderOutcome(
        symbol=str(symbol).upper(),
        expiry=str(expiry),
        ok=bool(res.get("ok")),
        job_id=str(out.job_id),
        artifact_url=artifact_url,
        out_path=out_path,
        error=err,
    )


def build(*, r: Any, date_et: date, symbols: Iterable[str], log: Path, ttl_sec: int) -> dict[str, Any]:
    d = date_et.isoformat()
    results: list[dict[str, Any]] = []

    for sym in symbols:
        earnings = _json_loads(r.get(cal_earnings_key(sym)))

        # Discover expirations (best-effort) so resolver can verify same-day exists.
        exp_all = discover_expirations(
            symbol=sym,
            start_ymd=d,
            max_expirations=int(os.getenv("TRIPLE_EXPIRY_MAX_EXPIRATIONS", "28")),
        )
        exp_mwf = filter_mwf(exp_all)

        res = resolve_mwf_expiry(
            symbol=sym,
            today_et=date_et,
            earnings_payload=earnings,
            available_expiries=exp_mwf,
            non_mwf_mode=str(os.getenv("TRIPLE_EXPIRY_NON_MWF_MODE", "next_mwf")),
        )

        key = pack_key(sym, res.expiry_ymd or d)

        # Price snapshot (used for post completeness).
        price_payload = None
        try:
            from delivery import discord_bot as delivery

            snap = delivery._get_last_price_snapshot(sym)
            price_payload = {
                "px": getattr(snap, "px", None),
                "ok": bool(getattr(snap, "ok", False)),
                "reason": getattr(snap, "reason", None),
                "source": getattr(snap, "source", None),
                "market_state": getattr(snap, "market_state", None),
                "age_minutes": getattr(snap, "age_minutes", None),
                "asof_et": getattr(getattr(snap, "asof_et", None), "isoformat", lambda: None)(),
            }
        except Exception:
            price_payload = None

        payload = {
            "schema": "triple_expiry_pack_v1",
            "symbol": sym,
            "date_et": d,
            "ok": bool(res.ok),
            "reason": str(res.reason),
            "expiry": res.expiry_ymd,
            "created_utc": _utc_now_iso(),
            "earnings": earnings if isinstance(earnings, dict) else None,
            "price": price_payload,
            "expirations": {"mwf": list(exp_mwf[:12])},
            "renders": {},
        }
        if res.details:
            payload["details"] = dict(res.details)

        # Optional: compute simple pack metrics (expected move + walls) from a bounded chain.
        if res.ok and res.expiry_ymd:
            try:
                import asyncio

                from delivery import discord_bot as delivery
                from delivery.options_chain_summary import summarize_options_chain
                from services.calendar.earnings_options import compute_expected_move_from_chain_df

                df = asyncio.run(
                    delivery._fetch_polygon_options_chain_df(
                        sym,
                        expiration_ymd=res.expiry_ymd,
                        strike_window_pct=float(os.getenv("TRIPLE_EXPIRY_STRIKE_WINDOW_PCT", "0.10")),
                        max_contracts=int(os.getenv("TRIPLE_EXPIRY_MAX_CONTRACTS", "220")),
                        concurrency=int(os.getenv("TRIPLE_EXPIRY_CHAIN_CONCURRENCY", "8")),
                    )
                )
                if df is not None and not getattr(df, "empty", True):
                    chain_summary = summarize_options_chain(df, sym=sym)
                    em = compute_expected_move_from_chain_df(df)
                    payload["options"] = {
                        "expected_move_pct": (em.get("expected_move_pct") if isinstance(em, dict) else None),
                        "call_wall": (chain_summary.get("call_wall") if isinstance(chain_summary, dict) else None),
                        "put_wall": (chain_summary.get("put_wall") if isinstance(chain_summary, dict) else None),
                    }
            except Exception:
                pass

        try:
            impact = _impact_from_pack(sym, payload)
            payload["impact"] = {
                "score": float(impact.get("score") or 0.0),
                "components": impact.get("components") if isinstance(impact.get("components"), dict) else {},
            }
        except Exception:
            pass

        r.set(key, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        r.expire(key, int(ttl_sec))

        results.append({"symbol": sym, "key": key, "ok": res.ok, "reason": res.reason, "expiry": res.expiry_ymd})
        _append_jsonl(log, {"event": "build", "symbol": sym, "key": key, "ok": res.ok, "reason": res.reason, "expiry": res.expiry_ymd})

    # Write manifest (list of keys) for the ET date.
    mkey = manifest_key(d)
    r.set(mkey, json.dumps({"date_et": d, "keys": [it["key"] for it in results]}, separators=(",", ":"), ensure_ascii=False))
    r.expire(mkey, int(ttl_sec))

    return {"date_et": d, "count": len(results), "manifest_key": mkey}


def render(*, r: Any, date_et: date, symbols: Iterable[str], log: Path, ttl_sec: int, top: int, dpi: int, timeout_s: float) -> dict[str, Any]:
    d = date_et.isoformat()
    rendered: list[dict[str, Any]] = []

    for sym in symbols:
        # Load the pack doc written by build.
        # If missing, create a minimal placeholder doc.
        key_guess = pack_key(sym, d)
        raw = r.get(key_guess)
        obj = _json_loads(raw) if raw is not None else None
        if not isinstance(obj, dict):
            obj = {
                "schema": "triple_expiry_pack_v1",
                "symbol": sym,
                "date_et": d,
                "ok": False,
                "reason": "missing_build",
                "expiry": None,
                "created_utc": _utc_now_iso(),
                "renders": {},
            }

        exp = str(obj.get("expiry") or "").strip()
        if not obj.get("ok") or not exp:
            _append_jsonl(log, {"event": "render_skip", "symbol": sym, "reason": str(obj.get("reason") or ""), "expiry": exp or None})
            continue

        lk = render_lock_key(sym, exp)
        handle = acquire_lock(r, lk, ttl_sec=int(os.getenv("TRIPLE_EXPIRY_RENDER_LOCK_TTL_SEC", "300")))
        if handle is None:
            _append_jsonl(log, {"event": "render_join", "symbol": sym, "expiry": exp, "lock": lk})
            continue

        try:
            out = _enqueue_and_wait_render_oi(symbol=sym, expiry=exp, top=top, dpi=dpi, timeout_s=timeout_s)
        finally:
            release_lock(r, handle)

        obj.setdefault("renders", {})
        obj["renders"]["oi_iv"] = {
            "ok": bool(out.ok),
            "job_id": out.job_id,
            "artifact_url": out.artifact_url,
            "out_path": out.out_path,
            "error": out.error,
            "rendered_utc": _utc_now_iso(),
        }

        # Persist under the canonical symbol+expiry key.
        key = pack_key(sym, exp)
        r.set(key, json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        r.expire(key, int(ttl_sec))

        rendered.append({"symbol": sym, "expiry": exp, "ok": out.ok, "url": out.artifact_url, "path": out.out_path, "error": out.error})
        _append_jsonl(log, {"event": "render", **rendered[-1]})

    return {"date_et": d, "rendered": len(rendered)}


def post(*, r: Any, date_et: date, symbols: Iterable[str], log: Path) -> dict[str, Any]:
    d = date_et.isoformat()

    if not str(os.getenv("TNT_TRIPLE_EXPIRY_ENABLED", "0") or "0").strip() in {"1", "true", "TRUE", "yes", "YES"}:
        _append_jsonl(log, {"event": "post_skip", "date_et": d, "reason": "disabled"})
        return {"date_et": d, "ok": False, "reason": "disabled"}

    try:
        from zero_dte_pipeline.discord.webhook import post_message
    except Exception:
        post_message = None

    embeds: list[dict[str, Any]] = []
    lines: list[str] = [f"**TNT Trading — M/W/F Expiry Packs** — {d}"]

    bundle_only_mode = str(os.getenv("TRIPLE_EXPIRY_BUNDLE_ONLY_MODE", "0") or "0").strip() in {"1", "true", "TRUE", "yes", "YES"}

    # Rate-limits (shared discipline with context-only alerts).
    try:
        now_et = datetime.now(_et_tz())
        hhmm = now_et.hour * 60 + now_et.minute
        quiet = str(os.getenv("TRIPLE_EXPIRY_QUIET_HOURS_ET", "") or "").strip()
        if quiet and "-" in quiet:
            a, b = quiet.split("-", 1)
            q0 = int(a.split(":")[0]) * 60 + int(a.split(":")[1]) if ":" in a else int(a) * 60
            q1 = int(b.split(":")[0]) * 60 + int(b.split(":")[1]) if ":" in b else int(b) * 60
            in_quiet = (q0 <= hhmm <= q1) if q0 <= q1 else (hhmm >= q0 or hhmm <= q1)
            if in_quiet:
                _append_jsonl(log, {"event": "post_skip", "date_et": d, "reason": "quiet_hours", "quiet": quiet})
                return {"date_et": d, "ok": False, "reason": "quiet_hours"}
    except Exception:
        pass

    # Global cap/hour.
    try:
        now_utc = int(datetime.now(timezone.utc).timestamp())
        hour_bucket = now_utc // 3600
        cap = int(os.getenv("TRIPLE_EXPIRY_GLOBAL_CAP_PER_HOUR", "8") or "8")
        gkey = f"triple_expiry:post:hour:{hour_bucket}"
        cur = int(str(r.get(gkey) or "0") or "0")
        if cur >= cap:
            _append_jsonl(log, {"event": "post_skip", "date_et": d, "reason": "global_cap", "cap": cap, "count": cur})
            return {"date_et": d, "ok": False, "reason": "global_cap"}
    except Exception:
        pass

    per_sym_cooldown_s = int(os.getenv("TRIPLE_EXPIRY_PER_SYMBOL_COOLDOWN_SEC", "1800") or "1800")
    eligible: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    for sym in symbols:
        # Prefer symbol+date key; if it contains expiry, re-load canonical symbol+expiry.
        raw0 = r.get(pack_key(sym, d))
        obj0 = _json_loads(raw0)
        if not isinstance(obj0, dict):
            continue

        exp = str(obj0.get("expiry") or "").strip()
        if exp:
            raw = r.get(pack_key(sym, exp))
            obj = _json_loads(raw)
        else:
            obj = obj0

        if not isinstance(obj, dict):
            continue

        ok = bool(obj.get("ok"))
        reason = str(obj.get("reason") or "")
        exp2 = str(obj.get("expiry") or "").strip() or None

        r_oi = obj.get("renders", {}).get("oi_iv") if isinstance(obj.get("renders"), dict) else None
        url = None
        if isinstance(r_oi, dict):
            url = str(r_oi.get("artifact_url") or "").strip() or None

        if not ok:
            lines.append(f"• **{sym}** — skipped ({reason})")
            skipped[reason or "unknown"] = skipped.get(reason or "unknown", 0) + 1
            continue

        # Completeness gates (strict by default; bundle-only mode relaxes missing artifacts).
        artifact_present = bool(url) or (isinstance(r_oi, dict) and str(r_oi.get("out_path") or "").strip())
        if not artifact_present and not bundle_only_mode:
            _append_jsonl(log, {"event": "post_skip_symbol", "symbol": sym, "reason": "skip_post_missing_artifact", "expiry": exp2})
            skipped["skip_post_missing_artifact"] = skipped.get("skip_post_missing_artifact", 0) + 1
            continue

        price_ok = True
        try:
            p = obj.get("price") if isinstance(obj.get("price"), dict) else {}
            price_ok = bool(p.get("ok", True))
        except Exception:
            price_ok = True
        if not price_ok:
            _append_jsonl(log, {"event": "post_skip_symbol", "symbol": sym, "reason": "skip_post_stale_price", "expiry": exp2})
            skipped["skip_post_stale_price"] = skipped.get("skip_post_stale_price", 0) + 1
            continue

        # One-line premium summary.
        em = None
        cw = None
        pw = None
        try:
            opt = obj.get("options") if isinstance(obj.get("options"), dict) else {}
            em = opt.get("expected_move_pct")
            cw = opt.get("call_wall")
            pw = opt.get("put_wall")
        except Exception:
            pass
        em_s = f"±{float(em):.1f}%" if isinstance(em, (int, float)) else "n/a"
        cw_s = "n/a"
        pw_s = "n/a"
        if isinstance(cw, dict) and cw.get("strike") is not None:
            try:
                cw_s = f"{float(cw.get('strike')):.0f}"
            except Exception:
                pass
        if isinstance(pw, dict) and pw.get("strike") is not None:
            try:
                pw_s = f"{float(pw.get('strike')):.0f}"
            except Exception:
                pass

        lines.append(f"• **{sym}** — EM {em_s} | Call wall {cw_s} | Put wall {pw_s} | exp **{exp2}**")

        # Per-symbol cooldown gate (applied to inclusion).
        try:
            now_utc = int(datetime.now(timezone.utc).timestamp())
            last_key = f"triple_expiry:last_post:{sym}"
            last = int(str(r.get(last_key) or "0") or "0")
            if last and (now_utc - last) < per_sym_cooldown_s:
                _append_jsonl(log, {"event": "post_skip_symbol", "symbol": sym, "reason": "cooldown", "cooldown_s": per_sym_cooldown_s})
                continue
        except Exception:
            pass

        impact_score = 0.0
        impact_components: dict[str, Any] = {}
        try:
            imp = obj.get("impact") if isinstance(obj.get("impact"), dict) else None
            if imp and isinstance(imp.get("score"), (int, float)):
                impact_score = float(imp.get("score") or 0.0)
                impact_components = imp.get("components") if isinstance(imp.get("components"), dict) else {}
            else:
                # Back-compat: compute on the fly.
                imp2 = _impact_from_pack(sym, obj)
                impact_score = float(imp2.get("score") or 0.0)
                impact_components = imp2.get("components") if isinstance(imp2.get("components"), dict) else {}
        except Exception:
            impact_score = 0.0
            impact_components = {}

        eligible.append(
            {
                "symbol": sym,
                "expiry": exp2,
                "impact": impact_score,
                "impact_components": impact_components,
                "url": url,
                "artifact_present": bool(artifact_present),
                "obj": obj,
            }
        )

    if not eligible:
        _append_jsonl(log, {"event": "post_skip", "date_et": d, "reason": "no_eligible_symbols"})
        return {"date_et": d, "ok": False, "reason": "no_eligible_symbols"}

    # Attach top-N images by impact score (avoid spam).
    top_n = int(os.getenv("TRIPLE_EXPIRY_TOP_IMAGES", "3") or "3")
    eligible_sorted = sorted(eligible, key=lambda x: float(x.get("impact") or 0.0), reverse=True)
    images_added = 0
    for it in eligible_sorted:
        if images_added >= top_n:
            break
        u = str(it.get("url") or "").strip()
        if not u:
            continue
        embeds.append({"title": f"{it['symbol']} — Exp {it['expiry']}", "description": "OI by strike + IV overlay", "image": {"url": u}})
        images_added += 1

    content = "\n".join(lines)[:1900]
    ok_post = False
    if callable(post_message):
        ok_post = bool(post_message(content, username="TNT Expiry Pack", embeds=embeds[:10] if embeds else None))
        # Fail-safe: if embed posting fails, retry bundle-only once.
        if not ok_post and embeds:
            ok_post = bool(post_message(content, username="TNT Expiry Pack", embeds=None))

    if ok_post:
        try:
            now_utc = int(datetime.now(timezone.utc).timestamp())
            hour_bucket = now_utc // 3600
            gkey = f"triple_expiry:post:hour:{hour_bucket}"
            cur = int(str(r.get(gkey) or "0") or "0")
            r.set(gkey, str(cur + 1))
            r.expire(gkey, 3600 * 6)
            for it in eligible:
                r.set(f"triple_expiry:last_post:{it['symbol']}", str(now_utc))
                r.expire(f"triple_expiry:last_post:{it['symbol']}", max(3600 * 48, per_sym_cooldown_s * 3))
        except Exception:
            pass

    top_chosen = [{"symbol": it["symbol"], "impact": it.get("impact"), "expiry": it.get("expiry")} for it in eligible_sorted[: max(1, top_n)]]
    _append_jsonl(
        log,
        {
            "event": "post",
            "date_et": d,
            "ok": bool(ok_post),
            "embeds": len(embeds),
            "eligible": len(eligible),
            "bundle_only_mode": bool(bundle_only_mode),
            "skipped_by_reason": skipped,
            "top": top_chosen,
        },
    )
    return {
        "date_et": d,
        "ok": bool(ok_post),
        "embeds": len(embeds),
        "eligible": len(eligible),
        "bundle_only_mode": bool(bundle_only_mode),
        "skipped_by_reason": skipped,
        "top": top_chosen,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MWF expiry pack fanout (build/render/post).")
    p.add_argument("--mode", default="all", choices=["build", "render", "post", "all"])
    p.add_argument("--date", default="", help="ET date YYYY-MM-DD (default: today ET)")
    p.add_argument("--symbols", default=",".join(DEFAULT_UNIVERSE))
    p.add_argument("--ttl-sec", type=int, default=int(os.getenv("TRIPLE_EXPIRY_TTL_SEC", "129600")))  # 36h
    p.add_argument("--top", type=int, default=int(os.getenv("TNT_OI_TOP", "18")))
    p.add_argument("--dpi", type=int, default=int(os.getenv("TNT_OI_DPI", "130")))
    p.add_argument("--timeout", type=float, default=float(os.getenv("TRIPLE_EXPIRY_RENDER_TIMEOUT_S", "120")))

    args = p.parse_args(argv)

    date_et = _parse_date_ymd(str(args.date))
    symbols = _symbols_list(str(args.symbols))

    r = redis_client(timeout_s=2.0)

    run_id = _run_id()
    log = _log_path(date_et.isoformat())
    _append_jsonl(
        log,
        {
            "event": "start",
            "run_id": run_id,
            "mode": args.mode,
            "date_et": date_et.isoformat(),
            "symbols": symbols,
            "ts_utc": _utc_now_iso(),
        },
    )

    out: dict[str, Any] = {}
    if args.mode in {"build", "all"}:
        out["build"] = build(r=r, date_et=date_et, symbols=symbols, log=log, ttl_sec=int(args.ttl_sec))
    if args.mode in {"render", "all"}:
        out["render"] = render(
            r=r,
            date_et=date_et,
            symbols=symbols,
            log=log,
            ttl_sec=int(args.ttl_sec),
            top=int(args.top),
            dpi=int(args.dpi),
            timeout_s=float(args.timeout),
        )
    if args.mode in {"post", "all"}:
        out["post"] = post(r=r, date_et=date_et, symbols=symbols, log=log)

    # End-of-run rollup (one JSON row) for --mode all.
    if args.mode == "all":
        try:
            per_sym_cooldown_s = int(os.getenv("TRIPLE_EXPIRY_PER_SYMBOL_COOLDOWN_SEC", "1800") or "1800")
            rows = build_status_rows(r=r, date_et_ymd=date_et.isoformat(), symbols=symbols, per_symbol_cooldown_s=per_sym_cooldown_s)
            ok_syms = [x["symbol"] for x in rows if x.get("pack_ok")]
            posted_syms = []
            skipped_by_reason: dict[str, int] = {}
            if isinstance(out.get("post"), dict):
                top = out["post"].get("top") if isinstance(out["post"].get("top"), list) else []
                posted_syms = [str(t.get("symbol") or "") for t in top if isinstance(t, dict) and str(t.get("symbol") or "").strip()]
                sbr = out["post"].get("skipped_by_reason")
                if isinstance(sbr, dict):
                    skipped_by_reason = {str(k): int(v) for k, v in sbr.items() if isinstance(v, int)}

            # Global cap + quiet-hours state snapshot.
            quiet = str(os.getenv("TRIPLE_EXPIRY_QUIET_HOURS_ET", "") or "").strip()
            cap = int(os.getenv("TRIPLE_EXPIRY_GLOBAL_CAP_PER_HOUR", "8") or "8")
            now_utc = int(datetime.now(timezone.utc).timestamp())
            hour_bucket = now_utc // 3600
            gkey = f"triple_expiry:post:hour:{hour_bucket}"
            cur = int(str(r.get(gkey) or "0") or "0")

            _append_jsonl(
                log,
                {
                    "event": "run_summary",
                    "run_id": run_id,
                    "date_et": date_et.isoformat(),
                    "symbols_ok": ok_syms,
                    "symbols_posted": posted_syms,
                    "symbols_skipped_by_reason": skipped_by_reason,
                    "global_cap": {"cap": cap, "count": cur, "key": gkey},
                    "quiet_hours": quiet or None,
                    "top": out.get("post", {}).get("top") if isinstance(out.get("post"), dict) else None,
                    "ts_utc": _utc_now_iso(),
                },
            )
        except Exception:
            pass

    _append_jsonl(log, {"event": "done", "run_id": run_id, "out": out, "ts_utc": _utc_now_iso()})
    print(json.dumps(out, indent=2))
    print(f"log: {log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
