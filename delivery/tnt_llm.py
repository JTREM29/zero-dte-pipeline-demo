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


def _repo_root() -> str:
    try:
        from pathlib import Path

        return str(Path(__file__).resolve().parents[1])
    except Exception:  # noqa: BLE001
        return os.getcwd()


def _maybe_load_repo_dotenv() -> None:
    """Best-effort load of repo-root .env/.env.local into this process.

    Some entrypoints are launched with a working directory that is not the repo root
    (e.g., VS Code tasks). In that case, other modules' `Path.cwd()` dotenv loading
    won't find the files even though they're present.
    """

    # If the key is already present, do nothing.
    if (os.getenv("OPENAI_API_KEY") or "").strip():
        return

    try:
        from pathlib import Path
        from dotenv import load_dotenv

        root = Path(_repo_root())

        # Precedence: .env < .env.local (local overrides)
        env_path = root / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        env_local_path = root / ".env.local"
        if env_local_path.exists():
            load_dotenv(env_local_path, override=True)
    except Exception:
        return


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

    _maybe_load_repo_dotenv()

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


def _extract_usage(resp: object) -> tuple[int | None, int | None, int | None]:
    """Return (input_tokens, output_tokens, cached_input_tokens) from a Responses API response (best-effort)."""

    usage = getattr(resp, "usage", None)
    if usage is not None:
        in_tok = getattr(usage, "input_tokens", None)
        out_tok = getattr(usage, "output_tokens", None)
        cached_tok = None

        # Some SDKs expose a details object.
        details = getattr(usage, "input_tokens_details", None)
        if details is not None:
            cached_tok = getattr(details, "cached_tokens", None)
        if isinstance(in_tok, (int, float)) or isinstance(out_tok, (int, float)):
            return (
                int(in_tok) if isinstance(in_tok, (int, float)) else None,
                int(out_tok) if isinstance(out_tok, (int, float)) else None,
                int(cached_tok) if isinstance(cached_tok, (int, float)) else None,
            )

        # Some SDKs surface prompt/completion naming.
        prompt_tok = getattr(usage, "prompt_tokens", None)
        comp_tok = getattr(usage, "completion_tokens", None)
        if isinstance(prompt_tok, (int, float)) or isinstance(comp_tok, (int, float)):
            return (
                int(prompt_tok) if isinstance(prompt_tok, (int, float)) else None,
                int(comp_tok) if isinstance(comp_tok, (int, float)) else None,
                int(cached_tok) if isinstance(cached_tok, (int, float)) else None,
            )

    dump_fn = getattr(resp, "model_dump", None)
    if callable(dump_fn):
        try:
            dump = dump_fn()
        except Exception:  # noqa: BLE001
            dump = None
        if isinstance(dump, dict):
            u = dump.get("usage")
            if isinstance(u, dict):
                in_tok = u.get("input_tokens", u.get("prompt_tokens"))
                out_tok = u.get("output_tokens", u.get("completion_tokens"))
                cached_tok = None
                details = u.get("input_tokens_details")
                if isinstance(details, dict):
                    cached_tok = details.get("cached_tokens")
                try:
                    in_tok_i = int(in_tok) if isinstance(in_tok, (int, float, str)) and str(in_tok).strip() else None
                except Exception:
                    in_tok_i = None
                try:
                    out_tok_i = int(out_tok) if isinstance(out_tok, (int, float, str)) and str(out_tok).strip() else None
                except Exception:
                    out_tok_i = None
                try:
                    cached_tok_i = int(cached_tok) if isinstance(cached_tok, (int, float, str)) and str(cached_tok).strip() else None
                except Exception:
                    cached_tok_i = None
                return in_tok_i, out_tok_i, cached_tok_i

    return None, None, None


def _estimate_cost_usd(*, input_tokens: int | None, cached_input_tokens: int | None, output_tokens: int | None) -> float:
    """Estimate USD cost using env-configured prices per 1M tokens.

    Set:
    - TNT_LLM_COST_INPUT_PER_1M_USD
    - TNT_LLM_COST_OUTPUT_PER_1M_USD
    - TNT_LLM_COST_CACHED_INPUT_PER_1M_USD (optional)
    """

    try:
        pin = float((os.getenv("TNT_LLM_COST_INPUT_PER_1M_USD") or "").strip() or "0")
    except Exception:  # noqa: BLE001
        pin = 0.0
    try:
        pout = float((os.getenv("TNT_LLM_COST_OUTPUT_PER_1M_USD") or "").strip() or "0")
    except Exception:  # noqa: BLE001
        pout = 0.0

    try:
        pcached = float((os.getenv("TNT_LLM_COST_CACHED_INPUT_PER_1M_USD") or "").strip() or "0")
    except Exception:  # noqa: BLE001
        pcached = 0.0

    it = max(0, int(input_tokens)) if isinstance(input_tokens, int) else 0
    cit = max(0, int(cached_input_tokens)) if isinstance(cached_input_tokens, int) else 0
    if cit > it:
        cit = it
    ot = max(0, int(output_tokens)) if isinstance(output_tokens, int) else 0
    uncached_in = max(0, it - cit)
    return (float(uncached_in) / 1_000_000.0) * pin + (float(cit) / 1_000_000.0) * pcached + (float(ot) / 1_000_000.0) * pout


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

    # Ops metrics: record LLM usage totals (best-effort, non-fatal).
    try:
        from controller.ops_metrics import OPS_METRICS

        in_tok, out_tok, cached_in_tok = _extract_usage(resp)
        OPS_METRICS.record_llm_usage(
            calls=1,
            input_tokens=in_tok,
            cached_input_tokens=cached_in_tok,
            output_tokens=out_tok,
            estimated_cost_usd=_estimate_cost_usd(input_tokens=in_tok, cached_input_tokens=cached_in_tok, output_tokens=out_tok),
        )
    except Exception:
        pass

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
