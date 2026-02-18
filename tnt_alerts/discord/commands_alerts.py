"""Framework-agnostic helpers for alert commands.

This module mirrors the pseudocode wiring: compile -> validate -> preview -> store.
It is intentionally not directly wired into discord.py in this repo (see cli/discord_bot.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from tnt_alerts.llm_compiler.compile_alert import CompileResult, compile_alert
from tnt_alerts.llm_compiler.validate import validate_envelope, validate_intent


@dataclass(frozen=True)
class CreateResult:
    kind: str  # "clarify" | "preview"
    payload: dict[str, Any]


def handle_alert_create(
    text: str,
    *,
    user_id: str,
    channel_id: str,
    llm_call: Callable[[str], str],
) -> CreateResult:
    res: CompileResult = compile_alert(text, user_id=user_id, channel_id=channel_id, llm_call=llm_call)
    env = validate_envelope(res.envelope)

    if not env.get("ok"):
        return CreateResult(
            kind="clarify",
            payload={
                "warnings": env.get("warnings", []),
                "clarify": env.get("clarify"),
                "raw": res.raw_text,
            },
        )

    intent_dict = env["intent"]
    intent_obj = validate_intent(intent_dict)

    summary = {
        "targets": intent_dict.get("targets"),
        "condition": intent_dict.get("condition"),
        "gates": intent_dict.get("gates", {}),
        "lifecycle": intent_dict.get("lifecycle", {}),
        "actions": intent_dict.get("actions", []),
    }

    return CreateResult(
        kind="preview",
        payload={
            "summary": summary,
            "intent": intent_obj,
            "intent_dict": intent_dict,
            "warnings": env.get("warnings", []),
            "raw": res.raw_text,
        },
    )


def handle_alert_confirm(intent_dict: dict[str, Any], *, store) -> dict[str, Any]:
    alert_id = store.next_id()
    store.create_alert(alert_id, intent_dict)
    return {"alert_id": alert_id}
