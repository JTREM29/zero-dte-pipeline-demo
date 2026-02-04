from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord

from tnt_alerts.direction import Direction, coerce_direction, describe_direction
import os
import json
from datetime import datetime, timezone


def _as_str(x: Any) -> str:
    """Best-effort string coercion for intent dictionaries.

    Intents are expected to be JSON-serializable, but in practice we may see
    enum-like dicts or objects during migrations. This must never raise.
    """

    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        # Common shapes: {"value": "close"}, {"name": "vwap"}
        for k in ("value", "name"):
            v = x.get(k)
            if isinstance(v, str):
                return v
        # Fall back to a stable representation.
        try:
            return json.dumps(x, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(x)
    try:
        v = getattr(x, "value", None)
        if isinstance(v, str):
            return v
    except Exception:
        pass
    try:
        return str(x)
    except Exception:
        return ""


def _safe_int(x: Any) -> int | None:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _fmt_seconds_compact(seconds: int | None) -> str:
    if seconds is None:
        return ""
    s = _safe_int(seconds)
    if s is None:
        return ""
    if s < 60:
        return f"{s}s"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s // 60}m {s % 60}s"


def _tf_minutes_phrase(tf: str) -> str:
    t = (tf or "").strip().lower()
    if t.endswith("m"):
        n = _safe_int(t[:-1])
        if n:
            return f"{n}-minute"
    if t.endswith("h"):
        n = _safe_int(t[:-1])
        if n:
            return f"{n}-hour"
    if t.endswith("d"):
        n = _safe_int(t[:-1])
        if n:
            return f"{n}-day"
    return tf or "?"


def _humanize_confirmation(confirm: str | None) -> str:
    c = _as_str(confirm).strip().lower()
    if c == "close":
        return "Candle close"
    # Treat missing/other as intrabar in v1 copy.
    return "Intrabar"


def _fmt_price_level(v: float) -> str:
    try:
        if abs(v - round(v)) < 1e-9:
            return str(int(round(v)))
        return f"{v:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def _humanize_level(level: dict[str, Any]) -> str:
    t = _as_str(level.get("type")).strip().lower()
    if t == "number":
        try:
            v = level.get("value")
            if isinstance(v, (int, float)):
                return _fmt_price_level(float(v))
        except Exception:
            pass
        return "(level)"
    if t == "pivot":
        p = str(level.get("pivot") or "").strip().upper()
        return p or "(pivot)"
    if t == "ref":
        ref = str(level.get("ref") or "").strip().lower()
        params = level.get("params") if isinstance(level.get("params"), dict) else {}
        if ref == "y_high":
            return "yesterday high"
        if ref == "y_low":
            return "yesterday low"
        if ref == "or_high":
            mins = _safe_int(params.get("minutes"))
            return f"OR high ({mins}m)" if mins else "OR high"
        if ref == "or_low":
            mins = _safe_int(params.get("minutes"))
            return f"OR low ({mins}m)" if mins else "OR low"
        return ref or "(ref)"
    return "(level)"


def _alert_condition_title_line(symbol: str, condition: dict[str, Any]) -> tuple[str, str]:
    tf = _as_str(condition.get("timeframe")).strip() or "5m"
    confirm = _as_str(condition.get("confirm")).strip() or "close"
    confirm_short = "close" if confirm.strip().lower() == "close" else "intrabar"

    ctype = _as_str(condition.get("type")).strip().lower()
    op = _as_str(condition.get("op")).strip().lower()

    if ctype == "cross":
        ind = _pick_indicator_for_cross(condition)
        return f"{symbol} — {ind} Cross ({tf})", f"Watching: {symbol} • {ind} • {tf}"

    if ctype == "break":
        level = condition.get("level") if isinstance(condition.get("level"), dict) else {}
        level_txt = _humanize_level(level) if isinstance(level, dict) else "(level)"
        if op == "breaks_below":
            title = f"{symbol} — Break below {level_txt} ({tf} {confirm_short})"
        elif op == "breaks_above":
            title = f"{symbol} — Break above {level_txt} ({tf} {confirm_short})"
        else:
            title = f"{symbol} — Break {level_txt} ({tf} {confirm_short})"
        return title, f"Watching: {symbol} • {op or 'break'} • {level_txt} • {tf} {confirm_short}"

    # Default fallback.
    return f"{symbol} — Alert ({tf})", f"Watching: {symbol} • {tf}"


def _humanize_session(gates: dict[str, Any]) -> str:
    mh = gates.get("market_hours") if isinstance(gates.get("market_hours"), dict) else {}
    session = _as_str(mh.get("session")).strip().upper()
    if session == "CUSTOM":
        win = mh.get("time_window_et")
        if isinstance(win, list) and len(win) == 2:
            start_et = _as_str(win[0]).strip()
            end_et = _as_str(win[1]).strip()
            if start_et and end_et:
                return f"Custom ({start_et}-{end_et} ET)"
        return "Custom market hours"
    if session == "ETH":
        return "Extended hours"
    # Default
    return "Regular market hours"


def _session_tag(gates: dict[str, Any]) -> str:
    mh = gates.get("market_hours") if isinstance(gates.get("market_hours"), dict) else {}
    session = _as_str(mh.get("session")).strip().upper()
    if session == "CUSTOM":
        win = mh.get("time_window_et")
        if isinstance(win, list) and len(win) == 2:
            start_et = _as_str(win[0]).strip()
            end_et = _as_str(win[1]).strip()
            if start_et and end_et:
                return f"CUSTOM {start_et}-{end_et} ET"
        return "CUSTOM"
    if session == "ETH":
        return "ETH"
    return "RTH"


def _pick_symbol(intent: dict[str, Any]) -> str:
    targets = intent.get("targets") if isinstance(intent.get("targets"), dict) else {}
    symbols = targets.get("symbols") if isinstance(targets.get("symbols"), list) else []
    sym = _as_str(symbols[0] if symbols else "").strip() if symbols else ""
    return sym or "(symbol)"


def _pick_indicator_for_cross(condition: dict[str, Any]) -> str:
    right = condition.get("right") if isinstance(condition.get("right"), dict) else {}
    if right.get("type") == "indicator":
        name = _as_str(right.get("name")).strip()
        if name:
            return name.upper()
    # Default.
    return "VWAP"


@dataclass(frozen=True)
class _CrossSemantics:
    direction: str
    direction_defaulted: bool


def _cross_semantics(condition: dict[str, Any]) -> _CrossSemantics:
    op = _as_str(condition.get("op")).strip().lower()
    if op == "crosses_below":
        return _CrossSemantics(direction="BELOW", direction_defaulted=False)
    if op == "crosses_above":
        return _CrossSemantics(direction="ABOVE", direction_defaulted=False)
    # Missing/unknown op: default ABOVE.
    return _CrossSemantics(direction="ABOVE", direction_defaulted=True)


def _humanize_expires(intent: dict[str, Any]) -> str:
    lifecycle = intent.get("lifecycle") if isinstance(intent.get("lifecycle"), dict) else {}
    exp = lifecycle.get("expires") if isinstance(lifecycle.get("expires"), dict) else {}

    # Explicit date.
    exp_date = _as_str(exp.get("date")).strip()
    if exp_date:
        return f"Expires {exp_date}"

    days = _safe_int(exp.get("days"))
    if days:
        return f"Expires in {days}d"

    hours = _safe_int(exp.get("hours"))
    if hours:
        return f"Expires in {hours}h"

    exp_type = _as_str(exp.get("type") or exp.get("kind")).strip().upper()
    if exp_type in {"UNTIL_EOD", "EOD", "UNTIL_END_OF_DAY"}:
        return "Expires end of day"

    # Opinionated v1 default.
    return "Expires in 8h"


def _short_expires_label(intent: dict[str, Any]) -> str:
    s = _humanize_expires(intent)
    if s == "Expires end of day":
        return "EOD"
    if s.startswith("Expires "):
        return s.replace("Expires ", "", 1)
    return s


def _humanize_guards(intent: dict[str, Any]) -> str:
    gates = intent.get("gates") if isinstance(intent.get("gates"), dict) else {}

    parts: list[str] = []

    cooldown = _humanize_cooldown(intent)
    if cooldown:
        parts.append(f"Cooldown {cooldown}")

    mt = _humanize_max_triggers(intent)
    if mt is not None:
        parts.append(f"Max triggers {mt}")

    # Regime gates
    rg = gates.get("regime") if isinstance(gates.get("regime"), dict) else {}
    allowed = rg.get("allowed") if isinstance(rg.get("allowed"), list) else []
    allowed = [str(x).strip().upper() for x in allowed if str(x).strip()]
    if allowed:
        parts.append("Regime " + "/".join(allowed))
    mconf = rg.get("min_confidence")
    if isinstance(mconf, (int, float)):
        parts.append(f"Min conf {float(mconf):.2f}")

    # Data freshness
    df = gates.get("data_freshness") if isinstance(gates.get("data_freshness"), dict) else {}
    age = _safe_int(df.get("price_age_seconds"))
    if age:
        parts.append(f"Freshness {age}s")

    return " • ".join(parts) if parts else "None"


def _humanize_cooldown(intent: dict[str, Any]) -> str | None:
    gates = intent.get("gates") if isinstance(intent.get("gates"), dict) else {}
    cd = gates.get("cooldown") if isinstance(gates.get("cooldown"), dict) else {}
    seconds = _safe_int(cd.get("seconds"))
    if not seconds:
        return None
    return _fmt_seconds_compact(seconds)


def _humanize_max_triggers(intent: dict[str, Any]) -> int | None:
    gates = intent.get("gates") if isinstance(intent.get("gates"), dict) else {}
    mt = gates.get("max_triggers") if isinstance(gates.get("max_triggers"), dict) else {}
    return _safe_int(mt.get("count"))


def _power_user_warnings(warnings: list[Any]) -> str | None:
    # Keep warnings subtle; prefer semantic footers.
    notes: list[str] = []
    for w in warnings[:3]:
        if isinstance(w, dict):
            note = w.get("note") or w.get("message")
        else:
            note = w
        if not note:
            continue
        s = str(note).strip()
        if not s:
            continue
        notes.append(s)
    if not notes:
        return None
    return " ".join(notes)[:240]


def build_earnings_preview_footer(store: Any, symbol: str) -> str | None:
    """Optional premium footer hint for alert preview.

    Anti-regret rules:
    - One Redis GET max
    - No external API calls
    - No writes
    - Only show for today/tomorrow (ET)
    - If missing earnings / not soon / stale -> do nothing
    """

    sym = (symbol or "").strip().upper()
    if not sym:
        return None

    r = getattr(store, "r", None)
    if r is None:
        return None

    # Prefer ctx snapshot, but fall back to legacy key during migration.
    # Use MGET when available to keep this to one Redis roundtrip.
    ctx_key = f"ctx:sym:{sym}"
    cal_key = f"cal:earnings:{sym}"

    raw_ctx = None
    raw_cal = None
    try:
        if hasattr(r, "mget"):
            vals = r.mget([ctx_key, cal_key])
            if isinstance(vals, (list, tuple)) and len(vals) == 2:
                raw_ctx, raw_cal = vals[0], vals[1]
        else:
            raw_ctx = r.get(ctx_key)
            raw_cal = r.get(cal_key)
    except Exception:
        return None

    def _decode(v: Any) -> str | None:
        if not v:
            return None
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", errors="replace")
        return str(v)

    s_ctx = _decode(raw_ctx)
    if s_ctx:
        try:
            ctx = json.loads(s_ctx)
        except Exception:
            ctx = None

        if isinstance(ctx, dict) and int(ctx.get("ctx_version") or 0) == 1:
            try:
                from services.context.context_formatters import fmt_earnings_preview_footer

                msg = fmt_earnings_preview_footer(ctx)
                return msg[:240] if msg else None
            except Exception:
                pass

    s_cal = _decode(raw_cal)
    if not s_ctx:
        try:
            from services.context.ctx_mode import ctx_enabled, ctx_strict_mode
            from services.context.context_miss import record_ctx_miss

            if ctx_enabled():
                mode = ctx_strict_mode()
                record_ctx_miss(r, "preview_footer", sym=sym, why="ctx_missing")
                if mode == "on":
                    # Strict: omit footer (no legacy fallback).
                    return None
        except Exception:
            pass
    if not s_cal:
        return None

    # Legacy path.
    try:
        ev = json.loads(s_cal)
    except Exception:
        return None
    if not isinstance(ev, dict):
        return None

    refreshed_utc = str(ev.get("refreshed_utc") or "").strip()
    if not refreshed_utc:
        return None

    try:
        dt_ref = datetime.fromisoformat(refreshed_utc.replace("Z", "+00:00"))
        if dt_ref.tzinfo is None:
            dt_ref = dt_ref.replace(tzinfo=timezone.utc)
        dt_ref = dt_ref.astimezone(timezone.utc)
    except Exception:
        return None

    try:
        stale_hours = int(os.getenv("EARNINGS_PREVIEW_STALE_HOURS", "48"))
    except Exception:
        stale_hours = 48
    stale_hours = max(1, min(168, int(stale_hours)))

    age_s = int((datetime.now(timezone.utc) - dt_ref).total_seconds())
    if age_s < 0:
        age_s = 0
    if age_s > (stale_hours * 3600):
        return None

    try:
        from services.calendar.earnings_overlay import build_earnings_risk_overlay

        o = build_earnings_risk_overlay(ev)
    except Exception:
        o = {}

    if not isinstance(o, dict) or not o.get("headline"):
        return None

    tags = o.get("tags") if isinstance(o.get("tags"), dict) else {}
    if not (tags.get("today") or tags.get("tomorrow")):
        return None

    headline = str(o.get("headline") or "").strip()
    if not headline:
        return None
    headline = headline[0:1].upper() + headline[1:]

    msg = f"⚠ {headline}"
    detail = str(o.get("detail") or "").strip()
    if detail:
        msg += f" • {detail}"
    return msg[:240]


def _defaults_used_line(warnings: list[Any]) -> str | None:
    codes: list[str] = []
    for w in warnings or []:
        if not isinstance(w, dict):
            continue
        c = w.get("code")
        if c:
            codes.append(str(c))
    if not codes:
        return None
    # Preserve order while uniquing.
    seen: set[str] = set()
    uniq: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return ", ".join(uniq)


def build_alert_preview_embed(
    intent: dict[str, Any],
    warnings: list[Any],
    *,
    store,
    dsl: str | None = None,
) -> tuple[discord.Embed, discord.ui.View]:
    symbol = _pick_symbol(intent)
    condition = intent.get("condition") if isinstance(intent.get("condition"), dict) else {}
    gates = intent.get("gates") if isinstance(intent.get("gates"), dict) else {}

    tf = _as_str(condition.get("timeframe")).strip() or "5m"
    confirm = _as_str(condition.get("confirm")).strip() or "intrabar"

    ctype = _as_str(condition.get("type")).strip().lower()
    if ctype == "cross":
        ind = _pick_indicator_for_cross(condition)
        sem = _cross_semantics(condition)
        title_line = f"{symbol} — {ind} Cross ({tf})"
        trigger = f"Notify me when {symbol} crosses {sem.direction} {ind} (on {_tf_minutes_phrase(tf)} candles)"
        footer = None
        if sem.direction_defaulted:
            footer = "Direction wasn’t specified — I assumed cross ABOVE. Want both? Create a second alert for cross BELOW."
    else:
        # Fallback: still keep copy stable and non-nerdy.
        title_line = f"{symbol} — Alert ({tf})"
        trigger = f"Notify me when {symbol} meets the condition (on {_tf_minutes_phrase(tf)} candles)"
        footer = None

    embed = discord.Embed(title="Alert Preview", color=0x2ECC71)
    embed.description = title_line

    # Rule (DSL) is the primary contract surface.
    rule_txt = str(dsl or "").strip()
    if not rule_txt:
        rule_txt = "(unavailable)"
    embed.add_field(name="Rule", value=f"```text\n{rule_txt}\n```", inline=False)

    # Active window
    embed.add_field(
        name="Active window",
        value=f"{_session_tag(gates)} • {_short_expires_label(intent)}",
        inline=False,
    )

    # Guards
    embed.add_field(name="Guards", value=_humanize_guards(intent), inline=False)

    defaults_line = _defaults_used_line(warnings)
    if defaults_line:
        embed.add_field(name="Defaults used", value=defaults_line, inline=False)

    earnings_footer = None
    try:
        earnings_footer = build_earnings_preview_footer(store, symbol)
    except Exception:
        earnings_footer = None

    if earnings_footer:
        embed.add_field(name="Earnings", value=earnings_footer, inline=False)

    # Keep footer consistent (trust).
    embed.set_footer(text="Simulation only. Not financial advice.")

    view = _AlertPreviewView(intent_dict=intent, store=store)
    return embed, view


class _AlertMultiPreviewView(discord.ui.View):
    def __init__(self, *, intent_dicts: list[dict[str, Any]], store) -> None:
        super().__init__(timeout=600)
        self._intent_dicts = intent_dicts
        self._store = store

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        try:
            for child in self.children:
                try:
                    child.disabled = True  # type: ignore[attr-defined]
                except Exception:
                    pass
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Confirm both", style=discord.ButtonStyle.green, row=0)
    async def confirm_both(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        created: list[str] = []
        try:
            for intent_dict in self._intent_dicts:
                alert_id = self._store.next_id()
                self._store.create_alert(alert_id, intent_dict)
                created.append(alert_id)
        except Exception as exc:
            await self._disable_all(interaction)
            await interaction.followup.send(f"❌ Alert create failed: {type(exc).__name__}: {exc}", ephemeral=True)
            return

        await self._disable_all(interaction)
        # Send per-alert created embeds so each has its own controls.
        for alert_id, intent_dict in zip(created, self._intent_dicts, strict=False):
            embed, view = build_alert_created_embed(alert_id, intent_dict, store=self._store)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._disable_all(interaction)
        await interaction.followup.send("Cancelled.", ephemeral=True)


def build_alert_multi_preview_embed(
    intent_dicts: list[dict[str, Any]],
    warnings: list[Any],
    *,
    store,
    dsls: list[str],
) -> tuple[discord.Embed, discord.ui.View]:
    # Best-effort: show shared session/expiry/guards from the first intent.
    intent0 = intent_dicts[0] if intent_dicts else {}
    symbol = _pick_symbol(intent0)
    condition = intent0.get("condition") if isinstance(intent0.get("condition"), dict) else {}
    gates = intent0.get("gates") if isinstance(intent0.get("gates"), dict) else {}

    tf = _as_str(condition.get("timeframe")).strip() or "5m"
    title_line = f"{symbol} — Alert ({tf})"

    embed = discord.Embed(title="Alert Preview", color=0x2ECC71)
    embed.description = f"{title_line}\n(2 alerts will be created)"

    for i, dsl in enumerate(dsls[:2], start=1):
        rule_txt = str(dsl or "").strip() or "(unavailable)"
        embed.add_field(name=f"Rule {i}", value=f"```text\n{rule_txt}\n```", inline=False)

    embed.add_field(
        name="Active window",
        value=f"{_session_tag(gates)} • {_short_expires_label(intent0)}",
        inline=False,
    )
    embed.add_field(name="Guards", value=_humanize_guards(intent0), inline=False)

    defaults_line = _defaults_used_line(warnings)
    if defaults_line:
        embed.add_field(name="Defaults used", value=defaults_line, inline=False)

    earnings_footer = None
    try:
        earnings_footer = build_earnings_preview_footer(store, symbol)
    except Exception:
        earnings_footer = None
    if earnings_footer:
        embed.add_field(name="Earnings", value=earnings_footer, inline=False)

    embed.set_footer(text="Simulation only. Not financial advice.")

    view = _AlertMultiPreviewView(intent_dicts=intent_dicts, store=store)
    return embed, view


def build_alert_created_embed(alert_id: str, intent: dict[str, Any], *, store) -> tuple[discord.Embed, discord.ui.View]:
    symbol = _pick_symbol(intent)
    condition = intent.get("condition") if isinstance(intent.get("condition"), dict) else {}
    gates = intent.get("gates") if isinstance(intent.get("gates"), dict) else {}
    title_line, watching_line = _alert_condition_title_line(symbol, condition)

    meta = None
    try:
        meta = store.get_meta(alert_id)
    except Exception:
        meta = None
    status = (meta.get("status") if isinstance(meta, dict) else None) or "active"
    status_txt = str(status).strip().lower() or "active"
    if status_txt == "paused":
        color = 0xF1C40F
        status_line = "🟡 Paused"
    elif status_txt == "expired":
        color = 0x95A5A6
        status_line = "⚫ Expired"
    elif status_txt == "deleted":
        color = 0xE74C3C
        status_line = "🔴 Deleted"
    else:
        color = 0x2ECC71
        status_line = "🟢 Active"

    embed = discord.Embed(title=f"✅ Alert created — {alert_id}", color=color)
    embed.description = f"{title_line}\n{watching_line}"

    # Only surface bias when it's actually gating something (regime filter).
    try:
        rg = gates.get("regime") if isinstance(gates, dict) else None
        rg_allowed = rg.get("allowed") if isinstance(rg, dict) else None
        if isinstance(rg_allowed, list) and rg_allowed:
            embed.add_field(name="Bias", value=describe_direction(intent), inline=True)
    except Exception:
        pass

    # Minimal confidence-hit line.
    expires = _humanize_expires(intent).replace("Expires ", "Expires ")
    cooldown = _humanize_cooldown(intent)
    bits = [expires]
    if cooldown:
        bits.append(f"Cooldown {cooldown}")
    embed.add_field(name="", value=" • ".join(bits), inline=False)

    embed.add_field(name="Status", value=status_line, inline=True)

    # Runtime proof: re-read what was actually persisted.
    try:
        stored = store.get_intent(alert_id)
    except Exception:
        stored = None
    if isinstance(stored, dict):
        try:
            s_cond = stored.get("condition") if isinstance(stored.get("condition"), dict) else {}
            s_gates = stored.get("gates") if isinstance(stored.get("gates"), dict) else {}

            s_tf = _as_str(s_cond.get("timeframe")).strip() or "?"
            s_confirm = _as_str(s_cond.get("confirm")).strip() or "intrabar"
            s_ctype = _as_str(s_cond.get("type")).strip().lower() or "?"
            s_op = _as_str(s_cond.get("op")).strip().lower()

            if s_ctype == "cross":
                s_ind = _pick_indicator_for_cross(s_cond)
                sem = _cross_semantics(s_cond)
                cond_line = f"crosses_{sem.direction.lower()} {s_ind} ({s_tf}, {_humanize_confirmation(s_confirm)})"
            else:
                cond_line = f"{s_ctype} {s_op} ({s_tf}, {_humanize_confirmation(s_confirm)})".strip()

            window_line = _session_tag(s_gates)
            embed.add_field(
                name="Stored (Redis)",
                value=f"Window: {window_line}\nCondition: {cond_line}",
                inline=False,
            )
        except Exception:
            pass

    view = _AlertPostCreateView(alert_id=alert_id, intent_dict=intent, store=store)
    return embed, view


def build_alert_details_embed(
    alert_id: str,
    intent: dict[str, Any],
    meta: dict[str, Any] | None = None,
    *,
    store=None,
) -> discord.Embed:
    condition = intent.get("condition") if isinstance(intent.get("condition"), dict) else {}
    gates = intent.get("gates") if isinstance(intent.get("gates"), dict) else {}

    tf = _as_str(condition.get("timeframe")).strip() or "5m"
    confirm = _as_str(condition.get("confirm")).strip() or "intrabar"
    ctype = _as_str(condition.get("type")).strip() or "?"
    op = _as_str(condition.get("op")).strip() or ""

    embed = discord.Embed(title=f"Details — {alert_id}", color=0x5865F2)

    # Only surface bias when it's actually gating something (regime filter).
    try:
        rg = gates.get("regime") if isinstance(gates, dict) else None
        rg_allowed = rg.get("allowed") if isinstance(rg, dict) else None
        if isinstance(rg_allowed, list) and rg_allowed:
            embed.add_field(name="Bias", value=describe_direction(intent), inline=True)
    except Exception:
        pass

    # Condition nerd sheet
    if ctype.lower() == "cross":
        ind = _pick_indicator_for_cross(condition)
        sem = _cross_semantics(condition)
        embed.add_field(name="Condition", value=f"crosses_{sem.direction.lower()} {ind} ({tf}, {_humanize_confirmation(confirm)})", inline=False)
    else:
        embed.add_field(name="Condition", value=f"{ctype} {op} ({tf}, {_humanize_confirmation(confirm)})".strip(), inline=False)

    def _stored_window_and_condition(intent_dict: dict[str, Any]) -> tuple[str, str, str, str | None]:
        s_cond = intent_dict.get("condition") if isinstance(intent_dict.get("condition"), dict) else {}
        s_gates = intent_dict.get("gates") if isinstance(intent_dict.get("gates"), dict) else {}
        s_src = intent_dict.get("source") if isinstance(intent_dict.get("source"), dict) else {}

        s_tf = _as_str(s_cond.get("timeframe")).strip() or "?"
        s_confirm = _as_str(s_cond.get("confirm")).strip().lower() or "intrabar"
        s_ctype = _as_str(s_cond.get("type")).strip().lower() or "?"
        s_op = _as_str(s_cond.get("op")).strip().lower()

        if s_ctype == "cross":
            s_ind = _pick_indicator_for_cross(s_cond)
            sem = _cross_semantics(s_cond)
            cond_line = f"cross_{sem.direction.lower()} {s_ind} ({s_tf}, confirm {s_confirm})"
        else:
            cond_line = f"{s_ctype} {s_op} ({s_tf}, confirm {s_confirm})".strip()

        window_line = _session_tag(s_gates)
        updated = _as_str(s_src.get("created_at_utc")).strip() or None
        return window_line, cond_line, s_tf, updated

    # Gates nerd sheet
    gate_parts: list[str] = []
    gate_parts.append(_humanize_session(gates).replace(" market hours", ""))

    df = gates.get("data_freshness") if isinstance(gates.get("data_freshness"), dict) else {}
    if _safe_int(df.get("price_age_seconds")):
        gate_parts.append(f"freshness≤{_safe_int(df.get('price_age_seconds'))}s")

    rg = gates.get("regime") if isinstance(gates.get("regime"), dict) else {}
    if rg.get("allowed"):
        mc = rg.get("min_confidence")
        if mc is not None:
            gate_parts.append(f"regime {rg.get('allowed')}+ (min_conf {mc})")
        else:
            gate_parts.append(f"regime {rg.get('allowed')}")

    if gate_parts:
        embed.add_field(name="Gates", value=", ".join(gate_parts), inline=False)

    # Runtime proof: re-read what was actually persisted (avoid preview/persist drift).
    if store is not None:
        stored = None
        try:
            stored = store.get_intent(alert_id)
        except Exception:
            stored = None

        if isinstance(stored, dict):
            try:
                s_window, s_cond_line, s_tf_key, s_updated = _stored_window_and_condition(stored)
                d_window, d_cond_line, _d_tf_key, _d_updated = _stored_window_and_condition(intent)

                mismatch = (s_window.strip().lower() != d_window.strip().lower()) or (s_cond_line.strip().lower() != d_cond_line.strip().lower())

                parts = [f"Window: {s_window}", f"Condition: {s_cond_line}"]
                if s_tf_key and s_tf_key != "?":
                    parts.append(f"TF key: {s_tf_key}")
                if s_updated:
                    parts.append(f"Updated: {s_updated}")
                if mismatch:
                    parts.append("⚠ mismatch detected; re-save recommended")

                embed.add_field(name="Stored (Redis)", value="\n".join(parts), inline=False)
            except Exception:
                embed.add_field(name="Stored (Redis)", value="Stored: unavailable", inline=False)
        else:
            embed.add_field(name="Stored (Redis)", value="Stored: unavailable (not found)", inline=False)

    cooldown = _humanize_cooldown(intent)
    if cooldown:
        embed.add_field(name="Cooldown", value=cooldown, inline=True)

    max_triggers = _humanize_max_triggers(intent)
    if max_triggers is not None:
        embed.add_field(name="Max triggers", value=str(max_triggers), inline=True)

    embed.add_field(name="Redis", value="stored ✅", inline=True)

    status = None
    if isinstance(meta, dict):
        status = meta.get("status")
    if status:
        embed.set_footer(text=f"status={status}")
    else:
        embed.set_footer(text="Stored ✅")

    return embed


class _AlertEditModal(discord.ui.Modal, title="Edit alert"):
    direction = discord.ui.TextInput(
        label="Cross direction (above/below/both)",
        required=False,
        default="above",
        max_length=12,
    )
    timeframe = discord.ui.TextInput(
        label="Timeframe (1m/5m/15m)",
        required=False,
        default="5m",
        max_length=8,
    )
    confirm = discord.ui.TextInput(
        label="Confirm (close/intrabar)",
        required=False,
        default="close",
        max_length=12,
    )
    expiry = discord.ui.TextInput(
        label="Expiry (8h/eod)",
        required=False,
        default="8h",
        max_length=8,
    )
    cooldown = discord.ui.TextInput(
        label="Cooldown minutes (e.g. 5)",
        required=False,
        default="5",
        max_length=8,
    )

    def __init__(self, *, intent_dict: dict[str, Any], store) -> None:
        super().__init__()
        self._intent_dict = intent_dict
        self._store = store

        condition = intent_dict.get("condition") if isinstance(intent_dict.get("condition"), dict) else {}
        tf = _as_str(condition.get("timeframe")).strip() or "5m"
        self.timeframe.default = tf
        c = _as_str(condition.get("confirm")).strip().lower()
        self.confirm.default = "close" if c == "close" else "intrabar"

        op = _as_str(condition.get("op")).strip().lower()
        if op == "crosses_below":
            self.direction.default = "below"
        else:
            self.direction.default = "above"

        # expiry default is opinionated v1; leave as 8h.

        gates = intent_dict.get("gates") if isinstance(intent_dict.get("gates"), dict) else {}
        cd = gates.get("cooldown") if isinstance(gates.get("cooldown"), dict) else {}
        sec = _safe_int(cd.get("seconds"))
        if sec and sec % 60 == 0:
            self.cooldown.default = str(sec // 60)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Apply minimal edits back onto the intent.

        condition = self._intent_dict.setdefault("condition", {})
        if not isinstance(condition, dict):
            condition = {}
            self._intent_dict["condition"] = condition

        dir_txt = (str(self.direction.value or "") or "").strip().lower()
        if dir_txt in {"below", "down"}:
            condition["op"] = "crosses_below"
        elif dir_txt in {"both", "either"}:
            # Keep single intent; traders can create a second alert.
            condition["op"] = "crosses_above"
        else:
            condition["op"] = "crosses_above"

        tf_txt = (str(self.timeframe.value or "") or "").strip()
        if tf_txt:
            condition["timeframe"] = tf_txt

        conf_txt = (str(self.confirm.value or "") or "").strip().lower()
        if conf_txt in {"close", "candle_close"}:
            condition["confirm"] = "close"
        else:
            condition["confirm"] = "intrabar"

        exp_txt = (str(self.expiry.value or "") or "").strip().lower()
        lifecycle = self._intent_dict.setdefault("lifecycle", {})
        if not isinstance(lifecycle, dict):
            lifecycle = {}
            self._intent_dict["lifecycle"] = lifecycle
        if exp_txt in {"eod", "until_eod"}:
            lifecycle["expires"] = {"type": "UNTIL_EOD"}
        else:
            lifecycle["expires"] = {"type": "relative", "hours": 8}

        cd_txt = (str(self.cooldown.value or "") or "").strip()
        cd_min = _safe_int(cd_txt)
        if cd_min and cd_min > 0:
            gates = self._intent_dict.setdefault("gates", {})
            if not isinstance(gates, dict):
                gates = {}
                self._intent_dict["gates"] = gates
            gates["cooldown"] = {"seconds": int(cd_min) * 60}

        # Send a fresh preview (keep original message untouched).
        embed, view = build_alert_preview_embed(self._intent_dict, warnings=[], store=self._store)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class _BiasSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        mode: str,
        alert_id: str | None,
        intent_dict: dict[str, Any],
        store,
    ) -> None:
        self._mode = mode
        self._alert_id = alert_id
        self._intent_dict = intent_dict
        self._store = store

        current = coerce_direction(intent_dict.get("direction"))
        options = [
            discord.SelectOption(label="AUTO", value="AUTO", default=(current == "AUTO")),
            discord.SelectOption(label="BULLISH", value="BULLISH", default=(current == "BULLISH")),
            discord.SelectOption(label="BEARISH", value="BEARISH", default=(current == "BEARISH")),
            discord.SelectOption(label="NEUTRAL", value="NEUTRAL", default=(current == "NEUTRAL")),
        ]

        super().__init__(
            placeholder="Bias: AUTO/BULLISH/BEARISH/NEUTRAL",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            val = (self.values[0] if self.values else "AUTO").strip().upper() or "AUTO"
            self._intent_dict["direction"] = coerce_direction(val)

            if self._mode == "created" and self._alert_id:
                try:
                    self._store.update_alert(self._alert_id, self._intent_dict)
                except Exception:
                    # Don't hard-fail UI; user can still edit via the modal.
                    pass

            if self._mode == "created" and self._alert_id:
                embed, view = build_alert_created_embed(self._alert_id, self._intent_dict, store=self._store)
            else:
                embed, view = build_alert_preview_embed(self._intent_dict, warnings=[], store=self._store)

            await interaction.response.edit_message(embed=embed, view=view)
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Bias update failed: {type(exc).__name__}: {exc}", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"❌ Bias update failed: {type(exc).__name__}: {exc}",
                    ephemeral=True,
                )


class _AlertPostCreateView(discord.ui.View):
    def __init__(self, *, alert_id: str, intent_dict: dict[str, Any], store) -> None:
        super().__init__(timeout=600)
        self._alert_id = alert_id
        self._intent_dict = intent_dict
        self._store = store

        # Bias dropdown (AUTO/BULLISH/BEARISH/NEUTRAL)
        self.add_item(_BiasSelect(mode="created", alert_id=alert_id, intent_dict=intent_dict, store=store))

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        try:
            for child in self.children:
                try:
                    child.disabled = True  # type: ignore[attr-defined]
                except Exception:
                    pass
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

    @discord.ui.button(label="View details", style=discord.ButtonStyle.secondary, row=1)
    async def view_details(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            meta = None
            try:
                meta = self._store.get_meta(self._alert_id)
            except Exception:
                meta = None
            embed = build_alert_details_embed(self._alert_id, self._intent_dict, meta, store=self._store)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            await interaction.response.send_message("⚠️ Failed to render details.", ephemeral=True)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        modal = _AlertUpdateModal(alert_id=self._alert_id, intent_dict=self._intent_dict, store=self._store)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, row=1)
    async def pause(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            meta = self._store.get_meta(self._alert_id)
            status = _as_str(meta.get("status")).strip().lower()
            if status == "paused":
                self._store.set_alert_paused(self._alert_id, False)
                embed, _ = build_alert_created_embed(self._alert_id, self._intent_dict, store=self._store)
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                self._store.set_alert_paused(self._alert_id, True)
                embed, _ = build_alert_created_embed(self._alert_id, self._intent_dict, store=self._store)
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception as exc:
            await interaction.response.send_message(f"❌ Pause failed: {type(exc).__name__}: {exc}", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            self._store.delete(self._alert_id)
            await self._disable_all(interaction)
            await interaction.followup.send(f"🗑️ Deleted **{self._alert_id}**", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"❌ Delete failed: {type(exc).__name__}: {exc}", ephemeral=True)


class _AlertPreviewView(discord.ui.View):
    def __init__(self, *, intent_dict: dict[str, Any], store) -> None:
        super().__init__(timeout=600)
        self._intent_dict = intent_dict
        self._store = store

        # Bias dropdown (AUTO/BULLISH/BEARISH/NEUTRAL)
        self.add_item(_BiasSelect(mode="preview", alert_id=None, intent_dict=intent_dict, store=store))

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        try:
            for child in self.children:
                try:
                    child.disabled = True  # type: ignore[attr-defined]
                except Exception:
                    pass
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, row=1)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            alert_id = self._store.next_id()
            self._store.create_alert(alert_id, self._intent_dict)
        except Exception as exc:
            await self._disable_all(interaction)
            await interaction.followup.send(f"❌ Alert create failed: {type(exc).__name__}: {exc}", ephemeral=True)
            return

        await self._disable_all(interaction)
        embed, view = build_alert_created_embed(alert_id, self._intent_dict, store=self._store)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        # Keep the preview visible; open a modal and then send a refreshed preview on submit.
        modal = _AlertEditModal(intent_dict=self._intent_dict, store=self._store)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._disable_all(interaction)
        await interaction.followup.send("Cancelled.", ephemeral=True)


class _AlertUpdateModal(discord.ui.Modal):
    direction = discord.ui.TextInput(
        label="Cross direction (above/below/both)",
        required=False,
        default="above",
        max_length=12,
    )
    timeframe = discord.ui.TextInput(
        label="Timeframe (1m/5m/15m)",
        required=False,
        default="5m",
        max_length=8,
    )
    confirm = discord.ui.TextInput(
        label="Confirm (close/intrabar)",
        required=False,
        default="close",
        max_length=12,
    )
    expiry = discord.ui.TextInput(
        label="Expiry (8h/eod)",
        required=False,
        default="8h",
        max_length=8,
    )
    cooldown = discord.ui.TextInput(
        label="Cooldown minutes (e.g. 5)",
        required=False,
        default="5",
        max_length=8,
    )

    def __init__(self, *, alert_id: str, intent_dict: dict[str, Any], store) -> None:
        super().__init__(title=f"Edit {alert_id} (updates in place)")
        self._alert_id = alert_id
        self._intent_dict = intent_dict
        self._store = store

        # Prefill from intent when possible.
        condition = intent_dict.get("condition") if isinstance(intent_dict.get("condition"), dict) else {}
        tf = _as_str(condition.get("timeframe")).strip() or "5m"
        self.timeframe.default = tf
        c = _as_str(condition.get("confirm")).strip().lower()
        self.confirm.default = "close" if c == "close" else "intrabar"

        op = _as_str(condition.get("op")).strip().lower()
        if op == "crosses_below":
            self.direction.default = "below"
        else:
            self.direction.default = "above"

        gates = intent_dict.get("gates") if isinstance(intent_dict.get("gates"), dict) else {}
        cd = gates.get("cooldown") if isinstance(gates.get("cooldown"), dict) else {}
        sec = _safe_int(cd.get("seconds"))
        if sec and sec % 60 == 0:
            self.cooldown.default = str(sec // 60)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        condition = self._intent_dict.setdefault("condition", {})
        if not isinstance(condition, dict):
            condition = {}
            self._intent_dict["condition"] = condition

        dir_txt = (str(self.direction.value or "") or "").strip().lower()
        if dir_txt in {"below", "down"}:
            condition["op"] = "crosses_below"
        elif dir_txt in {"both", "either"}:
            condition["op"] = "crosses_above"
        else:
            condition["op"] = "crosses_above"

        tf_txt = (str(self.timeframe.value or "") or "").strip()
        if tf_txt:
            condition["timeframe"] = tf_txt

        conf_txt = (str(self.confirm.value or "") or "").strip().lower()
        if conf_txt in {"close", "candle_close"}:
            condition["confirm"] = "close"
        else:
            condition["confirm"] = "intrabar"

        exp_txt = (str(self.expiry.value or "") or "").strip().lower()
        lifecycle = self._intent_dict.setdefault("lifecycle", {})
        if not isinstance(lifecycle, dict):
            lifecycle = {}
            self._intent_dict["lifecycle"] = lifecycle
        if exp_txt in {"eod", "until_eod"}:
            lifecycle["expires"] = {"type": "UNTIL_EOD"}
        else:
            lifecycle["expires"] = {"type": "relative", "hours": 8}

        cd_txt = (str(self.cooldown.value or "") or "").strip()
        cd_min = _safe_int(cd_txt)
        if cd_min and cd_min > 0:
            gates = self._intent_dict.setdefault("gates", {})
            if not isinstance(gates, dict):
                gates = {}
                self._intent_dict["gates"] = gates
            gates["cooldown"] = {"seconds": int(cd_min) * 60}

        try:
            self._store.update_alert(self._alert_id, self._intent_dict)
        except Exception as exc:
            await interaction.response.send_message(f"❌ Update failed: {type(exc).__name__}: {exc}", ephemeral=True)
            return

        embed, view = build_alert_created_embed(self._alert_id, self._intent_dict, store=self._store)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
