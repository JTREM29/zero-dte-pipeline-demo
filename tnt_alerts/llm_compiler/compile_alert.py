from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


PROMPT_PATH = Path(__file__).with_name("prompt.txt")


@dataclass(frozen=True)
class CompileResult:
    envelope: dict[str, Any]
    raw_text: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_legacy_intent_v1(
    intent: dict[str, Any],
    *,
    user_text: str,
    user_id: str,
    channel_id: str,
) -> dict[str, Any]:
    """Normalize legacy compiler intent shapes into the current AlertIntent v1 schema.

    The repo has evolved from an older intent structure like:
      {schema_version, symbols, timeframe, session, confirm, expiry, conditions[], gates[], actions[]}

    into the current contract:
      {version, source, targets, condition, gates, lifecycle, actions, tags}

    We keep strict schema validation, but allow the LLM to be slightly behind while
    we iterate.
    """

    symbols = intent.get("symbols") or []
    timeframe = intent.get("timeframe")
    confirm = intent.get("confirm")
    session = intent.get("session")

    # Collapse multi-condition legacy payloads into a single condition for now.
    # If the request was ambiguous and emitted both directions, default to the
    # first condition and rely on future hardening to split/clarify.
    conditions = intent.get("conditions") or []
    condition_obj: dict[str, Any] | None = None
    if isinstance(conditions, list) and conditions:
        if isinstance(conditions[0], dict):
            condition_obj = dict(conditions[0])

    # Legacy single condition sometimes appears as "condition".
    if condition_obj is None and isinstance(intent.get("condition"), dict):
        condition_obj = dict(intent["condition"])

    if condition_obj is None:
        # Let schema validation fail with a clear error later.
        condition_obj = {}

    # Ensure timeframe/confirm are attached to the condition (required by current schema).
    condition_obj.setdefault("timeframe", timeframe or "5m")
    condition_obj.setdefault("confirm", confirm or "close")

    gates_list = intent.get("gates") or []
    gates_obj: dict[str, Any] = {}
    if isinstance(gates_list, list):
        for g in gates_list:
            if not isinstance(g, dict):
                continue
            gt = g.get("type")
            if gt == "data_freshness":
                pas = g.get("price_age_seconds")
                if pas is not None:
                    gates_obj["data_freshness"] = {"price_age_seconds": int(pas)}
            elif gt == "news_blackout":
                mins = g.get("minutes")
                mmins = g.get("market_minutes")
                obj: dict[str, Any] = {}
                if mins is not None:
                    obj["minutes"] = int(mins)
                if mmins is not None:
                    obj["market_minutes"] = int(mmins)
                if obj:
                    gates_obj["news_blackout"] = obj
            elif gt == "macro_blackout":
                pre = g.get("pre_minutes")
                post = g.get("post_minutes")
                et = g.get("event_types")
                obj2: dict[str, Any] = {}
                if isinstance(et, list):
                    obj2["event_types"] = et
                if pre is not None:
                    obj2["pre_minutes"] = int(pre)
                if post is not None:
                    obj2["post_minutes"] = int(post)
                if obj2:
                    gates_obj["macro_blackout"] = obj2
            elif gt == "earnings_blackout":
                pre = g.get("pre_minutes")
                post = g.get("post_minutes")
                co = g.get("confirmed_only")
                obj3: dict[str, Any] = {}
                if pre is not None:
                    obj3["pre_minutes"] = int(pre)
                if post is not None:
                    obj3["post_minutes"] = int(post)
                if co is not None:
                    obj3["confirmed_only"] = bool(co)
                if obj3:
                    gates_obj["earnings_blackout"] = obj3
            elif gt == "cooldown":
                sec = g.get("seconds")
                if sec is not None:
                    gates_obj["cooldown"] = {"seconds": int(sec)}
            elif gt == "max_triggers":
                cnt = g.get("count")
                if cnt is not None:
                    gates_obj["max_triggers"] = {"count": int(cnt)}
            elif gt == "market_hours":
                session_v = g.get("session")
                if session_v in {"RTH", "ETH", "CUSTOM"}:
                    mh: dict[str, Any] = {"session": session_v}
                    if "time_window_et" in g:
                        mh["time_window_et"] = g.get("time_window_et")
                    gates_obj["market_hours"] = mh
            elif gt == "regime":
                allowed = g.get("allowed")
                if isinstance(allowed, list) and allowed:
                    rg: dict[str, Any] = {"allowed": allowed}
                    mc = g.get("min_confidence")
                    if mc is not None:
                        try:
                            rg["min_confidence"] = float(mc)
                        except Exception:
                            pass
                    gates_obj["regime"] = rg

    # If the legacy intent had top-level session, treat it as market_hours gate.
    if session in {"RTH", "ETH", "CUSTOM"} and "market_hours" not in gates_obj:
        gates_obj["market_hours"] = {"session": session, "time_window_et": None}

    actions = intent.get("actions") or [{"type": "discord_notify", "style": "compact"}]
    if isinstance(actions, list):
        for a in actions:
            if not isinstance(a, dict):
                continue
            if a.get("type") == "discord_notify":
                style = a.get("style")
                if style not in {"compact", "verbose"}:
                    a["style"] = "compact"
    lifecycle: dict[str, Any] = {"start": "now"}
    # Legacy had "expiry" like UNTIL_EOD; current schema doesn't represent EOD explicitly.
    # Use a conservative relative expiry to avoid "forever" alerts during early bring-up.
    expiry = intent.get("expiry")
    if isinstance(expiry, str) and expiry.upper() == "UNTIL_EOD":
        lifecycle["expires"] = {"type": "relative", "hours": 8}

    return {
        "version": "1.0",
        "source": {
            "user_id": str(intent.get("user_id") or user_id),
            "channel_id": str(intent.get("channel_id") or channel_id),
            "request_text": str(intent.get("request_text") or user_text),
            "created_at_utc": str(intent.get("created_at_utc") or _utc_now_iso()),
        },
        "targets": {
            "type": "symbols",
            "symbols": symbols,
            "watchlist": None,
            "max_symbols": int(intent.get("max_symbols") or 20),
        },
        "direction": str(intent.get("direction") or "AUTO").strip().upper().replace("LONG", "BULLISH").replace("SHORT", "BEARISH") or "AUTO",
        "condition": condition_obj,
        "gates": gates_obj,
        "lifecycle": lifecycle,
        "actions": actions,
        "tags": intent.get("tags") or {},
    }


def _normalize_compiler_envelope(
    envelope: dict[str, Any],
    *,
    user_text: str,
    user_id: str,
    channel_id: str,
) -> dict[str, Any]:
    def _as_float(v: object) -> float | None:
        if v is None:
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            try:
                return float(v)
            except Exception:
                return None
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                return float(s)
            except Exception:
                return None
        return None

    def _looks_like_level(node: object) -> bool:
        if isinstance(node, (int, float)):
            return _as_float(node) is not None
        if isinstance(node, str):
            return _as_float(node) is not None
        if not isinstance(node, dict):
            return False
        t = str(node.get("type") or "").strip().lower()
        if t in {"number", "pivot", "ref"}:
            return True
        # Legacy shapes we still see from the LLM.
        if t in {"level", "level_ref"}:
            return True
        if "value" in node and _as_float(node.get("value")) is not None:
            return True
        return False

    def _normalize_level(node: object) -> dict[str, Any]:
        """Return schema-friendly Level shape.

        Schema Level is one of:
          - {"type":"number","value":float}
          - {"type":"pivot","name":str}
          - {"type":"ref","name":str}
        """

        # Some LLM variants emit a bare number/string for a level.
        if isinstance(node, (int, float, str)):
            fv = _as_float(node)
            if fv is None:
                return {}
            return {"type": "number", "value": float(fv)}

        if not isinstance(node, dict):
            return {}

        t = str(node.get("type") or "").strip().lower()

        # Canonical number level.
        if t == "number":
            fv = _as_float(node.get("value"))
            if fv is None:
                return {}
            return {"type": "number", "value": float(fv)}

        # Legacy "level" wrapper.
        if t == "level":
            kind = str(node.get("kind") or "number").strip().lower() or "number"
            if kind == "number":
                fv = _as_float(node.get("value"))
                if fv is None:
                    return {}
                return {"type": "number", "value": float(fv)}
            if kind in {"pivot", "ref"}:
                name = str(node.get("name") or node.get("value") or "").strip()
                if not name:
                    return {}
                return {"type": kind, "name": name}
            return {}

        # Legacy "level_ref".
        if t == "level_ref":
            name = str(node.get("name") or node.get("value") or "").strip()
            if not name:
                return {}
            return {"type": "ref", "name": name}

        # Pivot/ref canonical.
        if t in {"pivot", "ref"}:
            name = str(node.get("name") or "").strip()
            if not name:
                return {}
            return {"type": t, "name": name}

        # Numeric fallback.
        if _as_float(node.get("value")) is not None:
            return {"type": "number", "value": float(_as_float(node.get("value")) or 0.0)}

        return {}

    def _normalize_condition(cond: object) -> dict[str, Any] | None:
        if not isinstance(cond, dict):
            return None
        c = dict(cond)
        ctype = str(c.get("type") or "").strip().lower()
        if not ctype:
            return c

        def _mentions_indicator(u: str) -> bool:
            try:
                tl = " ".join(str(u or "").lower().split())
            except Exception:
                tl = str(u or "").lower()
            # Keep this list small and explicit; we only want to treat these as
            # opt-in to indicator semantics.
            return any(
                k in tl
                for k in (
                    "vwap",
                    "ema",
                    "sma",
                    "ma ",
                    "moving average",
                    "rsi",
                    "macd",
                    "boll",
                    "bollinger",
                )
            )

        def _extract_level_from_text(u: str) -> float | None:
            """Best-effort extract a numeric level from user text.

            Used to interpret 'crosses above 689' as a price-level break unless
            the user explicitly mentions an indicator.
            """

            import re

            try:
                s = str(u or "")
            except Exception:
                return None

            # Ignore common timeframe tokens (e.g., "5m", "60m").
            # Capture numbers that are standalone or followed by punctuation.
            nums = re.findall(r"(?<![A-Za-z])(-?\d+(?:\.\d+)?)\b", s)
            if not nums:
                return None

            for raw in nums:
                try:
                    v = float(raw)
                except Exception:
                    continue
                # Heuristic: ignore tiny values likely to be minutes/hours.
                if v in {1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 30.0, 45.0, 60.0}:
                    continue
                if v <= 0.0 or v >= 100000.0:
                    continue
                return float(v)
            return None

        # Schema requires timeframe+confirm on all condition variants.
        allowed_timeframes = {"1m", "2m", "3m", "5m", "10m", "15m", "30m", "60m", "1D"}
        allowed_confirms = {"close", "intrabar"}

        def _ensure_tf_confirm(node: dict[str, Any]) -> dict[str, Any]:
            tf = node.get("timeframe")
            if not isinstance(tf, str) or tf not in allowed_timeframes:
                node["timeframe"] = "5m"
            cf = node.get("confirm")
            if not isinstance(cf, str) or cf not in allowed_confirms:
                node["confirm"] = "close"
            return node

        # Coerce cross-with-level into break semantics (schema requires cross RHS be a series).
        if ctype == "cross":
            # Default left series to price.last unless the model provided a valid Series.
            left = c.get("left")
            if not isinstance(left, dict) or str(left.get("type") or "").strip().lower() not in {"price", "indicator"}:
                c["left"] = {"type": "price", "field": "last"}

            right = c.get("right")
            if _looks_like_level(right):
                op = str(c.get("op") or "").strip().lower()
                if op == "crosses_below":
                    bop = "breaks_below"
                else:
                    bop = "breaks_above"

                tf = c.get("timeframe")
                confirm = c.get("confirm")
                return _ensure_tf_confirm(
                    {
                    "type": "break",
                    "op": bop,
                    "level": _normalize_level(right),
                    "timeframe": tf,
                    "confirm": confirm,
                    }
                )

            # If the user supplied a numeric level but did NOT ask for an indicator
            # (e.g., "SPY crosses above 689"), prefer break semantics.
            try:
                if not _mentions_indicator(user_text):
                    lvl = _extract_level_from_text(user_text)
                else:
                    lvl = None
            except Exception:
                lvl = None
            if isinstance(lvl, (int, float)):
                op = str(c.get("op") or "").strip().lower()
                bop = "breaks_below" if op == "crosses_below" else "breaks_above"
                tf = c.get("timeframe")
                confirm = c.get("confirm")
                return _ensure_tf_confirm(
                    {
                        "type": "break",
                        "op": bop,
                        "level": {"type": "number", "value": float(lvl)},
                        "timeframe": tf,
                        "confirm": confirm,
                    }
                )

            return _ensure_tf_confirm(c)

        if ctype == "touch":
            if "right" in c:
                c["right"] = _normalize_level(c.get("right"))
            return _ensure_tf_confirm(c)

        if ctype == "break":
            # Naming drift: LLM sometimes uses crosses_above/below in break condition.
            op = str(c.get("op") or "").strip().lower()
            if op == "crosses_above":
                c["op"] = "breaks_above"
            elif op == "crosses_below":
                c["op"] = "breaks_below"
            if "level" not in c and "right" in c:
                c["level"] = c.get("right")
                try:
                    del c["right"]
                except Exception:
                    pass
            c["level"] = _normalize_level(c.get("level"))
            return _ensure_tf_confirm(c)

        return _ensure_tf_confirm(c)

    if not isinstance(envelope, dict):
        return envelope

    if envelope.get("ok") and isinstance(envelope.get("intent"), dict):
        intent = envelope["intent"]

        # Legacy compiler payloads identify themselves via schema_version.
        if "schema_version" in intent:
            envelope["intent"] = _normalize_legacy_intent_v1(
                intent,
                user_text=user_text,
                user_id=user_id,
                channel_id=channel_id,
            )
        else:
            # Current shape: best-effort fill required source fields.
            src = intent.get("source")
            if isinstance(src, dict):
                src.setdefault("user_id", user_id)
                src.setdefault("channel_id", channel_id)
                src.setdefault("request_text", user_text)
                src.setdefault("created_at_utc", _utc_now_iso())
            else:
                intent["source"] = {
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "request_text": user_text,
                    "created_at_utc": _utc_now_iso(),
                }

            intent.setdefault("version", "1.0")
            if not intent.get("direction"):
                intent["direction"] = "AUTO"
            else:
                try:
                    d = str(intent.get("direction") or "AUTO").strip().upper() or "AUTO"
                    if d == "LONG":
                        d = "BULLISH"
                    elif d == "SHORT":
                        d = "BEARISH"
                    intent["direction"] = d
                except Exception:
                    intent["direction"] = "AUTO"
            envelope["intent"] = intent

        # Normalize condition to schema-safe shape (compiler-side).
        try:
            if isinstance(envelope.get("intent"), dict):
                intent2 = envelope["intent"]
                cond = intent2.get("condition")
                norm_cond = _normalize_condition(cond)
                if isinstance(norm_cond, dict):
                    intent2["condition"] = norm_cond
                envelope["intent"] = intent2
        except Exception:
            pass

    # Schema requires warnings: array of objects {code,note}. Some LLMs emit warnings as strings.
    warnings = envelope.get("warnings")
    norm_warnings: list[dict[str, Any]] = []
    if isinstance(warnings, list):
        for w in warnings:
            if isinstance(w, dict):
                # Ensure required key exists.
                if "code" not in w and "note" in w:
                    w = {"code": "WARN", "note": w.get("note")}
                if "code" in w:
                    norm_warnings.append({"code": str(w.get("code") or "WARN"), "note": w.get("note")})
            elif isinstance(w, str):
                s = w.strip()
                if s:
                    norm_warnings.append({"code": "WARN", "note": s})
    envelope["warnings"] = norm_warnings
    envelope.setdefault("clarify", None)
    return envelope


def compile_alert(
    user_text: str,
    *,
    user_id: str,
    channel_id: str,
    llm_call: Callable[[str], str],
) -> CompileResult:
    """LLM boundary compiler.

    llm_call(prompt: str) -> str (raw JSON string)

    This function does not validate schema. Pair with validate.validate_envelope.
    """

    prompt = ""
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        prompt = ""

    full_prompt = (
        (prompt + "\n\n" if prompt else "")
        + "USER_ID: "
        + str(user_id)
        + "\n"
        + "CHANNEL_ID: "
        + str(channel_id)
        + "\n"
        + "REQUEST: "
        + str(user_text)
        + "\n"
    )

    raw = llm_call(full_prompt) or ""

    try:
        env = json.loads(raw)
        if not isinstance(env, dict):
            raise ValueError("envelope is not an object")

        env = _normalize_compiler_envelope(env, user_text=user_text, user_id=user_id, channel_id=channel_id)
        return CompileResult(envelope=env, raw_text=raw)
    except Exception:
        # Hard fail safe envelope
        return CompileResult(
            envelope={
                "ok": False,
                "intent": None,
                "warnings": [{"code": "ERR_BAD_JSON", "note": "Compiler did not return valid JSON"}],
                "clarify": {
                    "question": "I couldn't parse that. Try rephrasing with symbol + condition.",
                    "choices": [],
                    "default": "",
                },
            },
            raw_text=raw,
        )


def compile_alert_via_tnt_llm(
    *,
    tnt_state: Mapping[str, Any],
    user_text: str,
    label: str,
    user_id: str,
    channel_id: str,
    max_output_tokens: int = 900,
    temperature: float = 0.0,
) -> CompileResult:
    """Convenience adapter for this repo: uses delivery.tnt_llm as llm_call."""

    from delivery.tnt_llm import call_tnt_agent

    def _llm_call(prompt: str) -> str:
        res = call_tnt_agent(
            tnt_state=tnt_state,
            user_text=user_text,
            label=label,
            max_output_tokens=int(max_output_tokens),
            temperature=float(temperature),
            system_addendum=prompt,
        )
        return (res.text or "").strip()

    return compile_alert(user_text=user_text, user_id=user_id, channel_id=channel_id, llm_call=_llm_call)
