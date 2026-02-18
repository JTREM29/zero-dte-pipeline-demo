from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


PROMPT_PATH = Path(__file__).with_name("prompt.txt")


@dataclass(frozen=True)
class CompileResult:
    envelope: dict[str, Any]
    raw_text: str


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
