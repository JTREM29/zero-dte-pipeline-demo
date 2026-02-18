from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable


def sleep_until_boundary(tf: str) -> None:
    """Align to bar close boundaries (minimal approach).

    For intraday minute-based timeframes, wake at the next minute boundary.
    """

    now = datetime.now(timezone.utc)
    sec = int(now.second)

    if tf.endswith("m"):
        wait = max(1, 60 - sec)
        time.sleep(wait)
        return

    # daily or unknown: poll
    time.sleep(60)


def run_tf_loop(
    *,
    tf: str,
    store,
    md,
    queue,
    build_levels_context: Callable[[str, str, Any], Any],
    build_gate_context: Callable[[str, datetime], Any],
    expand_targets: Callable[[dict[str, Any], str], list[str]],
    evaluate_alert: Callable[[dict[str, Any], Any, Any, Any, dict[str, Any]], dict[str, Any]],
) -> None:
    """Standalone scheduler loop.

    This is intentionally framework-agnostic; you provide adapters for:
    - md.fetch_bars(...)
    - queue.enqueue(...)
    - expand_targets(...)
    - build_levels_context(...)
    - build_gate_context(...)
    - evaluate_alert(...)
    """

    while True:
        sleep_until_boundary(tf)

        alert_ids = store.list_tf_alerts(tf)
        intents: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for aid in alert_ids:
            meta = store.get_meta(aid)
            if meta.get("status") != "active":
                continue
            intent = store.get_intent(aid)
            if intent:
                intents.append((aid, intent, meta))

        sym_to_alerts: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {}
        for aid, intent, meta in intents:
            src = intent.get("source") or {}
            user_id = str(src.get("user_id") or "")
            syms = expand_targets(intent.get("targets") or {}, user_id)
            for sym in syms:
                sym_to_alerts.setdefault(sym, []).append((aid, intent, meta))

        if not sym_to_alerts:
            continue

        snaps = md.fetch_bars(list(sym_to_alerts.keys()), tf, lookback=300)

        now_utc = datetime.now(timezone.utc)
        for sym, alert_list in sym_to_alerts.items():
            snap = snaps.get(sym)
            if not snap:
                continue

            ctx = build_levels_context(sym, tf, md)
            gctx = build_gate_context(sym, now_utc)

            for aid, intent, _meta in alert_list:
                state = store.get_state(aid, sym)
                event = evaluate_alert(intent, snap, ctx, gctx, state)

                store.set_state(aid, sym, state)

                if (event or {}).get("decision") == "TRIGGERED":
                    queue.enqueue(
                        {
                            "job_type": "alert_trigger",
                            "alert_id": aid,
                            "symbol": sym,
                            "channel_id": (intent.get("source") or {}).get("channel_id"),
                            "ts_utc": now_utc.isoformat(),
                            "event": event,
                            "actions": intent.get("actions", []),
                        }
                    )
