from __future__ import annotations

import datetime as dt
import os
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

import discord

try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = dt.timezone.utc


logger = logging.getLogger(__name__)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_iso(ts: str) -> dt.datetime | None:
    if not ts:
        return None
    try:
        raw = ts.strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(raw)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        return None


def _fmt_et(ts_utc_iso: str) -> str:
    d = _parse_iso(ts_utc_iso)
    if d is None:
        return "unknown"
    return d.astimezone(ET).strftime("%a %b %d • %I:%M %p ET")


def _age_str(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _freshness_footer(refreshed_epoch: int | None, *, stale_hours: int = 24) -> str | None:
    if not refreshed_epoch:
        return None
    age_s = int(_now_utc().timestamp()) - int(refreshed_epoch)
    footer = f"Last refreshed {_age_str(age_s)} ago"
    if age_s >= int(stale_hours) * 3600:
        footer += "  •  ⚠️ stale"
    return footer


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return int(default)


def _truthy_env(name: str, default: str = "0") -> bool:
    v = str(os.getenv(name, default) or "").strip().lower()
    return v in {"1", "true", "yes", "y", "on"}


def _countdown(ts_utc_iso: str) -> str | None:
    d = _parse_iso(ts_utc_iso)
    if d is None:
        return None
    now = _now_utc()
    delta = d - now
    total_s = int(delta.total_seconds())
    if total_s <= 0:
        return "now"
    days = total_s // 86400
    rem = total_s % 86400
    hours = rem // 3600
    rem = rem % 3600
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _risk_label(*, expected_move_pct: float | None, liquidity_risk: str | None, style: str) -> str | None:
    if expected_move_pct is None:
        return None
    em = float(expected_move_pct)
    base = "LOW"
    if em >= 7.0:
        base = "HIGH"
    elif em >= 4.0:
        base = "ELEVATED"

    lr = str(liquidity_risk or "").strip().upper()
    if lr in {"HIGH", "ELEVATED"} and base == "LOW":
        base = "ELEVATED"
    elif lr in {"HIGH"} and base == "ELEVATED":
        base = "HIGH"

    if style == "simple":
        return "HIGH" if base == "HIGH" else ("MED" if base == "ELEVATED" else "LOW")
    # marketing default
    return "HIGH IMPACT" if base == "HIGH" else ("ELEVATED" if base == "ELEVATED" else "LOW")


def _guidance_line(*, risk: str | None, session: str, confirmed: bool) -> str | None:
    if not risk:
        return None
    risk_u = str(risk).upper()
    if any(k in risk_u for k in ("HIGH", "IMPACT")):
        # One sentence, intent-focused.
        return "⚠️ TNT favors no pre-earnings exposure; trade post-reaction structure only."
    if any(k in risk_u for k in ("ELEVATED", "MED")):
        tag = "confirmed" if confirmed else "estimated"
        return f"Caution: earnings ({tag}) can dominate price action; size down or wait for post-reaction structure."
    return None


def _fmt_pct(x: object) -> str | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except Exception:
        return None
    if not (v == v):
        return None
    return f"{v:.1f}%"


def _blackout_active(ts_utc_iso: str, *, pre_min: int, post_min: int) -> bool:
    d = _parse_iso(ts_utc_iso)
    if d is None:
        return False
    now = _now_utc()
    start = d - dt.timedelta(minutes=int(pre_min))
    end = d + dt.timedelta(minutes=int(post_min))
    return start <= now <= end


def _session_short(value: object) -> str:
    s = str(value or "").strip().upper()
    if s in {"BMO", "AMC", "DURING"}:
        return s
    return "UNKNOWN"


def build_earnings_embed(
    sym: str,
    ev: dict[str, Any] | None,
    refreshed_utc: int | None,
    *,
    pre_min: int = 45,
    post_min: int = 30,
    news_items: Iterable[dict[str, Any]] | None = None,
    price_line: str | None = None,
    premium: bool | None = None,
    missing_mode: str = "retrieving",
) -> discord.Embed:
    symbol = str(sym or "").strip().upper() or "?"
    e = discord.Embed(title=f"🗓 Earnings — {symbol}", color=discord.Color.blurple())

    price_txt = str(price_line or "").strip()
    if price_txt:
        e.description = price_txt

    def _news_lines(items: Iterable[dict[str, Any]] | None, *, company_name: str | None = None) -> list[str]:
        out: list[str] = []
        if not items:
            return out

        sym_u = symbol

        def _tok_u(text: str) -> list[str]:
            return [t for t in re.split(r"[^A-Za-z0-9]+", (text or "").upper()) if t]

        def _headline_tokens_u(text: str) -> set[str]:
            return {t for t in _tok_u(text) if len(t) >= 3}

        def _contains_symbol(text: str, sym: str) -> bool:
            if not text or not sym:
                return False
            toks = _tok_u(text)
            if sym in toks:
                return True
            # Common cashtags like $AAPL.
            if f"${sym}" in (text or "").upper():
                return True
            return False

        def _company_tokens(name: str | None) -> set[str]:
            if not name:
                return set()
            stop = {
                "INC",
                "INCORPORATED",
                "CORP",
                "CORPORATION",
                "CO",
                "COMPANY",
                "LTD",
                "LIMITED",
                "PLC",
                "HOLDINGS",
                "HOLDING",
                "GROUP",
                "THE",
                "CLASS",
                "ORD",
                "COMMON",
                "SHARE",
                "SHARES",
            }
            toks = {t for t in _tok_u(name) if len(t) >= 3 and t not in stop and t != sym_u}
            # Avoid being too permissive: cap to a few distinctive tokens.
            return set(list(toks)[:4])

        cname_toks = _company_tokens(company_name)
        now = _now_utc()
        max_age_s = int(max(3600, _env_int("TNT_EARNINGS_NEWS_MAX_AGE_SEC", 86400)))

        def _is_recent(it: dict[str, Any]) -> bool:
            ts_s = str(
                it.get("published_utc")
                or it.get("published")
                or it.get("created_at")
                or it.get("created")
                or ""
            ).strip()
            d = _parse_iso(ts_s)
            if d is None:
                # If timestamp is missing/invalid, don't hard-drop; relevance must carry it.
                return True
            age = (now - d).total_seconds()
            return 0 <= age <= float(max_age_s)

        def _is_relevant(it: dict[str, Any]) -> bool:
            tickers_raw = it.get("tickers") or it.get("symbols") or it.get("stocks")
            tickers: set[str] = set()
            if isinstance(tickers_raw, str):
                tickers = {t.strip().upper() for t in tickers_raw.split(",") if t and str(t).strip()}
            elif isinstance(tickers_raw, list):
                tickers = {str(t).strip().upper() for t in tickers_raw if t and str(t).strip()}

            if sym_u and sym_u in tickers:
                return True

            headline = str(it.get("headline") or it.get("title") or "").strip()
            if _contains_symbol(headline, sym_u):
                return True

            if cname_toks:
                ht = _headline_tokens_u(headline)
                if any(t in ht for t in cname_toks):
                    return True

            return False

        def _tag_for(it: dict[str, Any]) -> str:
            headline = str(it.get("headline") or it.get("title") or "").strip()
            summary = str(it.get("summary") or it.get("description") or it.get("snippet") or "").strip()
            blob = f"{headline} {summary}".lower()
            if any(
                k in blob
                for k in (
                    "earnings",
                    "eps",
                    "revenue",
                    "guidance",
                    "outlook",
                    "beats",
                    "misses",
                    "beat ",
                    "miss ",
                    "quarter",
                    "q1",
                    "q2",
                    "q3",
                    "q4",
                )
            ):
                return "EARNINGS"
            if any(
                k in blob
                for k in (
                    "fed",
                    "fomc",
                    "powell",
                    "cpi",
                    "ppi",
                    "inflation",
                    "jobs",
                    "payroll",
                    "rates",
                    "yield",
                    "gdp",
                    "recession",
                    "ism",
                )
            ):
                return "MACRO"
            if any(
                k in blob
                for k in (
                    "sector",
                    "industry",
                    "peer",
                    "peers",
                    "etf",
                    "opec",
                    "crude",
                    "oil",
                    "semiconductor",
                    "chip",
                )
            ):
                return "SECTOR"
            return "COMPANY"

        lim = int(max(0, min(5, _env_int("TNT_EARNINGS_NEWS_LIMIT", 3))))

        selected: list[dict[str, Any]] = []
        seen = 0
        kept = 0
        for it in list(items):
            if not isinstance(it, dict):
                continue
            seen += 1
            if not _is_recent(it):
                continue
            if not _is_relevant(it):
                continue
            selected.append(it)
            kept += 1
            if len(selected) >= lim:
                break

        if _truthy_env("TNT_DEBUG_NEWS_FILTER_COUNTS", "0"):
            try:
                filtered_out = max(0, int(seen) - int(kept))
            except Exception:
                filtered_out = 0
            logger.debug(
                "earnings_news_filter_counts symbol=%s news_kept_count=%s news_filtered_out_count=%s",
                sym_u,
                int(kept),
                int(filtered_out),
            )

        for it in selected:
            if not isinstance(it, dict):
                continue
            headline = str(it.get("headline") or it.get("title") or "").strip()
            url = str(it.get("url") or "").strip()
            if not headline:
                continue

            tag = _tag_for(it)
            h = headline.replace("\n", " ").replace("\r", " ").strip()
            prefix = f"• [{tag}] "
            max_h = 140 - len(prefix)
            if len(h) > max_h:
                h = h[: max(0, max_h - 1)].rstrip() + "…"
            if url:
                out.append(f"{prefix}{h}\n  {url}")
            else:
                out.append(f"{prefix}{h}")
        return out

    def _reality_check_line(payload: dict[str, Any]) -> str | None:
        try:
            implied = float(payload.get("expected_move_pct"))
        except Exception:
            implied = None
        if implied is None or not (abs(implied) > 0.05):
            return None

        hist = payload.get("history") if isinstance(payload.get("history"), list) else []
        realized = None
        for it in hist:
            if not isinstance(it, dict):
                continue
            try:
                mv = float(it.get("move_pct"))
            except Exception:
                continue
            realized = mv
            break
        if realized is None:
            return None

        a_imp = abs(float(implied))
        if a_imp <= 0:
            return None

        ratio = abs(float(realized)) / a_imp
        if ratio < 0.60:
            verdict = "IV overpriced"
        elif ratio <= 1.20:
            verdict = "Matched pricing"
        else:
            verdict = "Move exceeded pricing"

        return f"Reality Check: Last earnings realized {float(realized):+.1f}% vs Implied ±{a_imp:.1f}% → {verdict}"

    def _estimates_lines(est: object) -> list[str]:
        if not isinstance(est, dict) or not est:
            return []

        def _fmt_money(v: object) -> str | None:
            try:
                x = float(v)  # type: ignore[arg-type]
            except Exception:
                return None
            if not (x == x):
                return None
            if abs(x) >= 1_000_000_000:
                return f"${x/1_000_000_000:.2f}B"
            if abs(x) >= 1_000_000:
                return f"${x/1_000_000:.2f}M"
            if abs(x) >= 1_000:
                return f"${x:,.0f}"
            return f"${x:.2f}"

        def _first_val(*keys: str) -> object | None:
            for k in keys:
                if k in est and est.get(k) is not None:
                    return est.get(k)
            return None

        lines: list[str] = []
        eps = _first_val("eps", "eps_estimate", "epsEstimate", "eps_consensus", "epsConsensus")
        rev = _first_val("revenue", "revenue_estimate", "revenueEstimate", "rev", "rev_estimate")
        if eps is not None:
            try:
                lines.append(f"EPS est: {float(eps):.2f}")
            except Exception:
                lines.append(f"EPS est: {str(eps).strip()}")
        if rev is not None:
            money = _fmt_money(rev)
            lines.append(f"Revenue est: {money or str(rev).strip()}")

        if lines:
            return lines

        # Fallback: show a few key/value pairs without dumping everything.
        for k in sorted(est.keys())[:4]:
            try:
                v = est.get(k)
                if v is None:
                    continue
                s = str(v).strip()
                if len(s) > 40:
                    s = s[:39].rstrip() + "…"
                lines.append(f"{k}: {s}")
            except Exception:
                continue
        return lines

    if premium is None:
        premium = _truthy_env("TNT_EARNINGS_PREMIUM", "1")

    if not ev:
        mode = str(missing_mode or "retrieving").strip().lower()

        # Default: show the one-time "retrieving" message.
        if mode != "sparse":
            miss = f"I’m retrieving the latest earnings information for {symbol}. If an update is available, it will appear shortly."
            e.description = (f"{price_txt}\n\n{miss}" if price_txt else miss)
            try:
                nl = _news_lines(news_items)
                e.add_field(
                    name="Latest News",
                    value=("\n".join(nl)[:1024] if nl else "• No relevant headlines in the last 24h."),
                    inline=False,
                )
            except Exception:
                pass
            footer = _freshness_footer(refreshed_utc, stale_hours=int(max(1, _env_int("TNT_EARNINGS_EMBED_STALE_HOURS", 48))))
            if footer:
                e.set_footer(text=footer)
            return e

        # Sparse mode: stable card without the "retrieving" loop.
        if price_txt:
            e.description = price_txt

        e.add_field(name="Next earnings", value="Not yet announced", inline=False)
        if premium is None:
            premium = _truthy_env("TNT_EARNINGS_PREMIUM", "1")
        if premium:
            e.add_field(
                name="Impact",
                value="Earnings impact unavailable (no confirmed date).",
                inline=False,
            )

        try:
            nl = _news_lines(news_items)
            e.add_field(
                name="Latest News",
                value=("\n".join(nl)[:1024] if nl else "• No relevant headlines in the last 24h."),
                inline=False,
            )
        except Exception:
            pass

        e.add_field(
            name="Last 4 earnings reactions",
            value="Unavailable right now (recent reactions not available).",
            inline=False,
        )

        footer = _freshness_footer(refreshed_utc, stale_hours=int(max(1, _env_int("TNT_EARNINGS_EMBED_STALE_HOURS", 48))))
        if footer:
            e.set_footer(text=footer)
        return e

    ts = str(ev.get("ts_utc") or "")
    confirmed = bool(ev.get("confirmed", False))
    session = _session_short(ev.get("session"))

    blackout = ev.get("blackout") if isinstance(ev.get("blackout"), dict) else {}
    pre = int(blackout.get("pre_min", pre_min)) if blackout else int(pre_min)
    post = int(blackout.get("post_min", post_min)) if blackout else int(post_min)
    pre = max(0, pre)
    post = max(0, post)

    suppressed = _blackout_active(ts, pre_min=pre, post_min=post)

    exp_move = _fmt_pct(ev.get("expected_move_pct"))

    countdown = _countdown(ts)
    next_lines = [f"**{_fmt_et(ts)}**", f"{session}  •  {'✅ Confirmed' if confirmed else '≈ Estimated'}"]
    if countdown:
        next_lines.append(f"In: {countdown}")

    e.add_field(
        name="Next earnings",
        value="\n".join(next_lines),
        inline=False,
    )

    e.add_field(
        name="Blackout window",
        value=(
            f"{int(pre)}m before → {int(post)}m after\n"
            f"Alerts suppressed: {'YES' if suppressed else 'no'}"
        ),
        inline=True,
    )

    if premium:
        exp_move_f = None
        try:
            exp_move_f = float(ev.get("expected_move_pct")) if ev.get("expected_move_pct") is not None else None
        except Exception:
            exp_move_f = None

        iv_state = ev.get("iv_state") if isinstance(ev.get("iv_state"), dict) else {}
        crush = str(iv_state.get("crush_risk") or "").strip().upper()
        iv_badge = None
        if crush in {"HIGH", "MED", "LOW"}:
            iv_badge = f"IV crush risk: {crush}"

        style = (os.getenv("TNT_EARNINGS_RISK_LABEL_STYLE", "marketing") or "marketing").strip().lower()
        risk = _risk_label(expected_move_pct=exp_move_f, liquidity_risk=str(ev.get("liquidity_risk") or "") or None, style=style)

        em_snap = ev.get("expected_move") if isinstance(ev.get("expected_move"), dict) else {}
        under = None
        upper = None
        lower = None
        straddle = None
        try:
            under = float(em_snap.get("underlying")) if em_snap.get("underlying") is not None else None
        except Exception:
            under = None
        try:
            upper = float(em_snap.get("upper")) if em_snap.get("upper") is not None else None
        except Exception:
            upper = None
        try:
            lower = float(em_snap.get("lower")) if em_snap.get("lower") is not None else None
        except Exception:
            lower = None
        try:
            straddle = float(em_snap.get("straddle")) if em_snap.get("straddle") is not None else None
        except Exception:
            straddle = None

        impact_bits: list[str] = []
        if exp_move and straddle is not None and straddle > 0:
            impact_bits.append(f"Expected move (weekly ATM): ±{exp_move} (±${straddle:.2f})")
        elif exp_move:
            impact_bits.append(f"Expected move: ±{exp_move}")

        # Risk shape from ±$wings fan-out (best-effort).
        try:
            from services.calendar.earnings_options import risk_shape_from_expected_move_snapshot

            rs = risk_shape_from_expected_move_snapshot(em_snap)
            label = str((rs or {}).get("label") or "").strip().upper() if isinstance(rs, dict) else ""
            score = None
            try:
                score = float((rs or {}).get("score")) if isinstance(rs, dict) and (rs or {}).get("score") is not None else None
            except Exception:
                score = None

            label_txt = None
            if label == "BALANCED":
                label_txt = "Balanced"
            elif label == "UPSIDE_SKEW":
                label_txt = "Skewed upside"
            elif label == "DOWNSIDE_SKEW":
                label_txt = "Skewed downside"

            if label_txt:
                if score is not None:
                    impact_bits.append(f"Risk shape: {label_txt} (skew {score:+.2f})")
                else:
                    impact_bits.append(f"Risk shape: {label_txt}")
            else:
                # If the snapshot exists but lacks wings, be explicit.
                wings = em_snap.get("wings") if isinstance(em_snap.get("wings"), dict) else None
                if wings is None or not wings:
                    impact_bits.append("Risk shape: unavailable (no wing snapshots)")
        except Exception:
            pass

        if risk:
            impact_bits.append(f"Risk: {risk}")
        if iv_badge:
            impact_bits.append(iv_badge)
        if impact_bits:
            e.add_field(name="Impact", value=" • ".join(impact_bits)[:1024], inline=False)
        elif not exp_move:
            e.add_field(
                name="Expected move",
                value="Unavailable right now (options snapshot not available).",
                inline=False,
            )

        if upper is not None and lower is not None and upper > 0 and lower > 0:
            e.add_field(name="Expected move bounds", value=f"Upper: {upper:.2f}  •  Lower: {lower:.2f}", inline=False)

        g = _guidance_line(risk=risk, session=session, confirmed=confirmed)
        if g:
            e.add_field(name="Guidance", value=g[:1024], inline=False)

        try:
            rc = _reality_check_line(ev)
            if rc:
                e.add_field(name="Reality check", value=rc[:1024], inline=False)
        except Exception:
            pass
    else:
        if exp_move:
            e.add_field(name="Expected move", value=f"±{exp_move}", inline=True)

    try:
        est_lines = _estimates_lines(ev.get("estimates"))
        if est_lines:
            e.add_field(name="Estimates", value="\n".join(est_lines)[:1024], inline=False)
    except Exception:
        pass

    try:
        company_name = None
        for k in ("company", "company_name", "name"):
            v = ev.get(k)
            if isinstance(v, str) and v.strip():
                company_name = v.strip()
                break

        nl = _news_lines(news_items, company_name=company_name)
        e.add_field(
            name="Latest News",
            value=("\n".join(nl)[:1024] if nl else "• No relevant headlines in the last 24h."),
            inline=False,
        )
    except Exception:
        pass

    # Compact last-4 history rendering.
    hist = ev.get("history") if isinstance(ev.get("history"), list) else []
    lines: list[str] = []
    for it in hist[:4]:
        if not isinstance(it, dict):
            continue
        ts_h = str(it.get("ts_utc") or "")
        mv = _fmt_pct(it.get("move_pct"))
        gp = _fmt_pct(it.get("gap_pct"))
        tag = str(it.get("tag") or "").strip()
        if not mv:
            continue
        dtxt = ""
        if ts_h:
            try:
                d = _parse_iso(ts_h)
                dtxt = d.astimezone(ET).strftime("%b %d") if d is not None else ""
            except Exception:
                dtxt = ""
        parts: list[str] = []
        if gp:
            parts.append(f"gap {gp}")
        if tag:
            parts.append(tag)
        extra = "; ".join(parts)
        if extra:
            lines.append(f"• {dtxt}: {mv} ({extra})".strip())
        else:
            lines.append(f"• {dtxt}: {mv}".strip())

    if lines:
        e.add_field(name="Last 4 earnings reactions", value="\n".join(lines)[:1024], inline=False)
    else:
        e.add_field(
            name="Last 4 earnings reactions",
            value="Unavailable right now (recent reactions not available).",
            inline=False,
        )

    footer = _freshness_footer(refreshed_utc, stale_hours=int(max(1, _env_int("TNT_EARNINGS_EMBED_STALE_HOURS", 48))))
    if footer:
        e.set_footer(text=footer)
    return e


@dataclass(frozen=True)
class UpcomingEarningsItem:
    symbol: str
    ts_utc_iso: str
    confirmed: bool
    session: str = "UNKNOWN"
    expected_move_pct: float | None = None


def build_earnings_upcoming_embed(
    items: Iterable[UpcomingEarningsItem],
    *,
    days: int,
    refreshed_utc: int | None,
) -> discord.Embed:
    lookahead_days = int(max(1, min(14, int(days))))
    now = _now_utc()
    today_et = now.astimezone(ET).date()

    bucket_today: list[str] = []
    bucket_tomorrow: list[str] = []
    bucket_week: list[str] = []

    for it in items:
        ts = _parse_iso(it.ts_utc_iso)
        if ts is None:
            continue
        ts_et = ts.astimezone(ET)
        tag = "✅" if it.confirmed else "~"
        session = _session_short(it.session)
        exp = _fmt_pct(it.expected_move_pct)
        exp_txt = f" — exp move ±{exp}" if exp else ""
        line = f"{it.symbol} — {ts_et.strftime('%a')} {session} ({tag}){exp_txt}"

        d = ts_et.date()
        if d == today_et:
            bucket_today.append(line)
        elif d == (today_et + dt.timedelta(days=1)):
            bucket_tomorrow.append(line)
        else:
            bucket_week.append(line)

    e = discord.Embed(title=f"Upcoming Earnings (next {lookahead_days}d)", color=discord.Color.blurple())
    if not bucket_today and not bucket_tomorrow and not bucket_week:
        e.description = f"No major earnings in the next {lookahead_days}d (for the configured symbols)."
    e.add_field(name="Today", value="\n".join(bucket_today) if bucket_today else "—", inline=False)
    e.add_field(name="Tomorrow", value="\n".join(bucket_tomorrow) if bucket_tomorrow else "—", inline=False)
    e.add_field(name="This window", value="\n".join(bucket_week) if bucket_week else "—", inline=False)

    footer = _freshness_footer(refreshed_utc)
    if footer:
        footer = f"✅ confirmed • ~ estimated • {footer}"
        e.set_footer(text=footer)
    else:
        e.set_footer(text="✅ confirmed • ~ estimated")

    return e


def build_earnings_today_embed(
    items: Iterable[UpcomingEarningsItem],
    *,
    refreshed_utc: int | None,
    title: str = "Earnings Today",
) -> discord.Embed:
    now = _now_utc()
    today_et = now.astimezone(ET).date()

    bucket_today: list[tuple[dt.datetime, str]] = []
    for it in items:
        ts = _parse_iso(it.ts_utc_iso)
        if ts is None:
            continue
        ts_et = ts.astimezone(ET)
        if ts_et.date() != today_et:
            continue
        tag = "✅" if it.confirmed else "~"
        session = _session_short(it.session)
        exp = _fmt_pct(it.expected_move_pct)
        exp_txt = f" — exp move ±{exp}" if exp else ""
        line = f"{it.symbol} — {session} ({tag}){exp_txt}"
        bucket_today.append((ts_et, line))

    bucket_today.sort(key=lambda x: x[0])
    lines = [x[1] for x in bucket_today]

    e = discord.Embed(title=title, color=discord.Color.blurple())
    if not lines:
        e.description = "No major earnings today (for the configured symbols)."
    e.add_field(name="Today", value="\n".join(lines) if lines else "—", inline=False)

    footer = _freshness_footer(refreshed_utc)
    if footer:
        footer = f"✅ confirmed • ~ estimated • {footer}"
        e.set_footer(text=footer)
    else:
        e.set_footer(text="✅ confirmed • ~ estimated")

    return e


def build_earnings_preclose_embed(
    *,
    today_amc: Iterable[UpcomingEarningsItem],
    tomorrow_bmo: Iterable[UpcomingEarningsItem],
    refreshed_utc: int | None,
) -> discord.Embed:
    e = discord.Embed(title="Earnings pre-close check", color=discord.Color.blurple())

    def _lines(items: Iterable[UpcomingEarningsItem]) -> list[str]:
        out: list[str] = []
        for it in items:
            tag = "✅" if it.confirmed else "~"
            session = _session_short(it.session)
            exp = _fmt_pct(it.expected_move_pct)
            exp_txt = f" — exp move ±{exp}" if exp else ""
            out.append(f"{it.symbol} — {session} ({tag}){exp_txt}")
        return out

    a = _lines(today_amc)
    b = _lines(tomorrow_bmo)

    if not a and not b:
        e.description = "No major earnings: Today (AMC) and Tomorrow (BMO) are empty (for the configured symbols)."

    e.add_field(name="Today (AMC)", value="\n".join(a) if a else "—", inline=False)
    e.add_field(name="Tomorrow (BMO)", value="\n".join(b) if b else "—", inline=False)

    footer = _freshness_footer(refreshed_utc)
    if footer:
        footer = f"✅ confirmed • ~ estimated • {footer}"
        e.set_footer(text=footer)
    else:
        e.set_footer(text="✅ confirmed • ~ estimated")

    return e
