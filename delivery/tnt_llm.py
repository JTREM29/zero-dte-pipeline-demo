"""One Door LLM wrapper for TNT.

All model calls must go through this module so we can enforce:
- TNT system prompt (from disk)
- TNT_STATE injection (authoritative JSON)
- Stable hashing for audits and regression tests

Callers should treat TNT_STATE as the only factual context.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from openai import OpenAI

from delivery.tnt_prompt import TNTSystemPromptInfo, load_tnt_system_prompt
from delivery.tnt_state import format_tnt_state_block, validate_tnt_state


logger = logging.getLogger(__name__)


TNT_LOCKED_MODEL = "gpt-5.2"


@dataclass(frozen=True)
class TNTLLMResult:
    text: str
    model: str
    prompt_sha256: str
    tnt_state_sha256: str


_openai_client: Optional[OpenAI] = None


def _now_timeout_s() -> float:
    try:
        return float(os.getenv("OPENAI_TIMEOUT_S", os.getenv("DISCORD_AI_TIMEOUT_S", "12")) or "12")
    except Exception:  # noqa: BLE001
        return 12.0


def _default_model() -> str:
    # TNT's agent model is intentionally locked to avoid silent drift.
    # Allow the env var to exist for runtime visibility, but the value must be GPT-5.2.
    return (os.getenv("TNT_LLM_MODEL") or TNT_LOCKED_MODEL).strip()


def _fingerprint_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compose_instructions(
    *,
    tnt_state: Mapping[str, Any],
    prompt_info: Optional[TNTSystemPromptInfo] = None,
    system_addendum: Optional[str] = None,
) -> tuple[str, str, str]:
    """Return (instructions, prompt_sha256, tnt_state_sha256)."""

    prompt = prompt_info or load_tnt_system_prompt(log=False)

    ok, reason = validate_tnt_state(tnt_state)
    if not ok:
        raise ValueError(f"Invalid TNT_STATE: {reason}")

    tnt_sha = _fingerprint_json(tnt_state)

    base = (prompt.text or "").strip()
    add = (system_addendum or "").strip()
    if add:
        base = f"{base}\n\n{add}"

    instructions = f"{base}\n\n{format_tnt_state_block(tnt_state)}".strip()
    return instructions, prompt.sha256, tnt_sha


def _get_openai_client() -> OpenAI:
    global _openai_client  # noqa: PLW0603

    if _openai_client is not None:
        return _openai_client

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OpenAI API key not configured")

    _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _extract_output_text(resp: object) -> str:
    text_attr = getattr(resp, "output_text", None)
    if isinstance(text_attr, str) and text_attr.strip():
        return text_attr.strip()

    dump_fn = getattr(resp, "model_dump", None)
    dump = None
    if callable(dump_fn):
        try:
            dump = dump_fn()
        except Exception:  # noqa: BLE001
            dump = None

    if isinstance(dump, dict):
        text_fallback = dump.get("output_text") or dump.get("outputText")
        if isinstance(text_fallback, str) and text_fallback.strip():
            return text_fallback.strip()

        output_items = dump.get("output") or dump.get("outputs")
        if isinstance(output_items, list):
            lines: list[str] = []
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "message":
                    continue
                for chunk in item.get("content") or []:
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("type") in {"output_text", "text"}:
                        text_val = chunk.get("text") or chunk.get("value")
                        if isinstance(text_val, str) and text_val.strip():
                            lines.append(text_val.strip())
            if lines:
                return "\n".join(lines).strip()

    return ""


def call_tnt_agent(
    *,
    tnt_state: Mapping[str, Any],
    user_text: str,
    label: str,
    model: Optional[str] = None,
    max_output_tokens: int = 800,
    temperature: float = 0.2,
    system_addendum: Optional[str] = None,
) -> TNTLLMResult:
    instructions, prompt_sha, tnt_sha = compose_instructions(
        tnt_state=tnt_state,
        system_addendum=system_addendum,
    )

    chosen_model = (model or _default_model()).strip()
    assert chosen_model == TNT_LOCKED_MODEL, f"TNT model drift detected: {chosen_model}"
    logger.info("[TNT][LOCK] Agent model locked to GPT-5.2")

    client = _get_openai_client()
    resp = client.responses.create(
        model=chosen_model,
        input=(user_text or "").strip() or "Task: Respond using TNT_STATE only.",
        instructions=instructions,
        max_output_tokens=int(max_output_tokens),
        temperature=float(temperature),
        timeout=_now_timeout_s(),
        metadata={"label": str(label)[:64], "prompt_sha256": prompt_sha, "tnt_state_sha256": tnt_sha},
    )

    resp_model = getattr(resp, "model", None)
    model_used_raw = resp_model.strip() if isinstance(resp_model, str) and resp_model.strip() else chosen_model
    # Some APIs return versioned identifiers like "gpt-5.2-2025-12-11".
    # Enforce that the family stays on GPT-5.2 while keeping the canonical stored name stable.
    if model_used_raw == TNT_LOCKED_MODEL or str(model_used_raw).startswith(TNT_LOCKED_MODEL + "-"):
        model_used = TNT_LOCKED_MODEL
    else:
        raise AssertionError(f"TNT model drift detected: {model_used_raw}")

    text = _extract_output_text(resp)
    if not text:
        raise RuntimeError("empty LLM response")

    return TNTLLMResult(text=text, model=model_used, prompt_sha256=prompt_sha, tnt_state_sha256=tnt_sha)


async def call_tnt_agent_async(
    *,
    tnt_state: Mapping[str, Any],
    user_text: str,
    label: str,
    model: Optional[str] = None,
    max_output_tokens: int = 800,
    temperature: float = 0.2,
    system_addendum: Optional[str] = None,
) -> TNTLLMResult:
    loop = asyncio.get_running_loop()

    def _invoke() -> TNTLLMResult:
        return call_tnt_agent(
            tnt_state=tnt_state,
            user_text=user_text,
            label=label,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            system_addendum=system_addendum,
        )

    return await loop.run_in_executor(None, _invoke)
