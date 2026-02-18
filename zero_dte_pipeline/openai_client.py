"""OpenAI client helpers used across the Zero DTE pipeline."""
from __future__ import annotations

import logging
import os
from typing import Iterable, List, Mapping, Optional

from delivery.tnt_llm import call_tnt_agent

from zero_dte_pipeline.config import config

DEFAULT_MODEL = os.getenv("OPENAI_MODEL_NAME", "gpt-4.1-mini")

logger = logging.getLogger(__name__)


class OpenAIError(RuntimeError):
    """Raised when the OpenAI API returns an error response."""


class OpenAIClient:
    """Compatibility wrapper that routes calls through `delivery.tnt_llm`.

    This enforces the "One Door" invariant: all model calls must include TNT_STATE
    and flow through a single module.
    """

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        api_key: Optional[str] = None,
    ) -> None:
        key = api_key or config.openai_api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            raise OpenAIError("OpenAI API key not configured; set OPENAI_API_KEY")

        self._api_key = key
        self.model = model or config.openai_model_name or DEFAULT_MODEL
        self.timeout_seconds = timeout_seconds or config.openai_timeout_seconds

    # --- low-level chat helper ------------------------------------------------

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tnt_state: Mapping[str, object],
        temperature: float = 0.1,
        max_tokens: int = 600,
        model: Optional[str] = None,
    ) -> str:
        """Single-turn call via TNT "One Door" wrapper."""

        active_model = model or self.model
        try:
            logger.info(
                "OpenAIClient.chat start model=%s temp=%.2f max_tokens=%d",
                active_model,
                temperature,
                max_tokens,
            )
            result = call_tnt_agent(
                tnt_state=tnt_state,
                user_text=user_prompt,
                label="openai_client.chat",
                model=active_model,
                max_output_tokens=max_tokens,
                temperature=temperature,
                system_addendum=system_prompt,
            )
            return result.text.strip()
        except Exception as exc:  # pragma: no cover
            raise OpenAIError(str(exc)) from exc

    # --- compat helpers with the previous wrapper ----------------------------

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        tnt_state: Mapping[str, object],
        model: Optional[str] = None,
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int = 600,
    ) -> str:
        """Invoke chat completions with simple retry handling."""

        attempts = max(1, max_retries)
        last_error: Optional[OpenAIError] = None
        for attempt in range(attempts):
            try:
                return self.chat(
                    system_prompt,
                    user_prompt,
                    tnt_state=tnt_state,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model,
                )
            except OpenAIError as exc:  # pragma: no cover
                last_error = exc
                if attempt < attempts - 1:
                    continue
        assert last_error is not None
        raise last_error

    def complete(
        self,
        *,
        prompt: str,
        system: Optional[str] = None,
        tnt_state: Mapping[str, object],
        model: Optional[str] = None,
        max_retries: int = 3,
        temperature: float = 0.2,
        max_tokens: int = 600,
    ) -> str:
        """Backward-compatible alias that mirrors the old wrapper."""

        system_prompt = system or "You are a helpful assistant."
        return self.generate(
            system_prompt=system_prompt,
            user_prompt=prompt,
            tnt_state=tnt_state,
            model=model,
            max_retries=max_retries,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # --- higher-level helpers -------------------------------------------------

    def summarize_headlines(
        self,
        headlines: Iterable[str],
        *,
        symbol: str,
        tnt_state: Mapping[str, object],
        max_headlines: int = 12,
    ) -> str:
        """Summarize recent headlines for a symbol into a neutral bullet list."""

        headlines_list: List[str] = [h for h in headlines if h]
        if not headlines_list:
            return ""

        headlines_list = headlines_list[:max_headlines]

        system_prompt = (
            "You are a cautious market assistant. "
            "Summarize recent headlines into short, neutral bullets. "
            "Avoid trade advice or position sizing."
        )

        joined = "\n".join(f"- {h}" for h in headlines_list)
        user_prompt = (
            f"Symbol: {symbol}\n\n"
            "Recent headlines:\n"
            f"{joined}\n\n"
            "Task: Summarize the overall tone in 3-5 short bullet points, "
            "using neutral language. No trade advice."
        )

        try:
            return self.chat(
                system_prompt,
                user_prompt,
                tnt_state=tnt_state,
                temperature=0.2,
                max_tokens=220,
            )
        except OpenAIError:
            return ""

    def summarize_morning_brief(self, raw_report_text: str, *, tnt_state: Mapping[str, object]) -> str:
        """Compress a long raw morning report into a short TL;DR."""

        if not raw_report_text.strip():
            return ""

        system_prompt = (
            "You are a neutral, non-advisory market explainer. "
            "Given a morning market report, produce a 3-4 line TL;DR. "
            "Avoid words like 'buy', 'sell', or 'you should'."
        )

        user_prompt = (
            "Here is a morning market report. "
            "Return a brief TL;DR for a Discord message:\n\n"
            f"{raw_report_text}"
        )

        try:
            return self.chat(
                system_prompt,
                user_prompt,
                tnt_state=tnt_state,
                temperature=0.1,
                max_tokens=200,
            )
        except OpenAIError:
            return ""


# singleton-style helper -------------------------------------------------------

_default_client: Optional[OpenAIClient] = None


def get_client() -> OpenAIClient:
    global _default_client  # noqa: PLW0603
    if _default_client is None:
        _default_client = OpenAIClient()
    return _default_client


# Backward-compatible alias for legacy imports
OpenAIWrapper = OpenAIClient

__all__ = ["OpenAIClient", "OpenAIWrapper", "OpenAIError", "get_client"]
