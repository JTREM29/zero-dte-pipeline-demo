"""OpenAI client helpers used across the Zero DTE pipeline."""
from __future__ import annotations

import logging
import os
import time
from typing import Iterable, List, Optional

import httpx
from openai import OpenAI, NotFoundError

from zero_dte_pipeline.config import config

DEFAULT_MODEL = os.getenv("OPENAI_MODEL_NAME", "gpt-4.1-mini")

logger = logging.getLogger(__name__)


class OpenAIError(RuntimeError):
    """Raised when the OpenAI API returns an error response."""


class OpenAIClient:
    """Thin wrapper around the OpenAI Chat Completions API."""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        api_key: Optional[str] = None,
        client: Optional[OpenAI] = None,
    ) -> None:
        key = api_key or config.openai_api_key or os.getenv("OPENAI_API_KEY")
        if not key and client is None:
            raise OpenAIError("OpenAI API key not configured; set OPENAI_API_KEY")

        self._api_key = key
        self.model = model or config.openai_model_name or DEFAULT_MODEL
        self.timeout_seconds = timeout_seconds or config.openai_timeout_seconds
        self._client: Optional[OpenAI] = None
        if client is not None:
            self._client = client
        else:
            try:
                self._client = OpenAI(api_key=key)
            except TypeError as exc:  # httpx>=0.28 removed proxies arg
                if "proxies" in str(exc).lower():
                    logger.warning(
                        "OpenAI SDK initialization failed due to proxies arg incompatibility; "
                        "falling back to raw HTTP client (install httpx<0.28 to restore SDK).",
                    )
                    self._client = None
                else:
                    raise

    # --- low-level chat helper ------------------------------------------------

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.1,
        max_tokens: int = 600,
        model: Optional[str] = None,
    ) -> str:
        """Single-turn chat completion against the configured model."""

        active_model = model or self.model
        fallback_candidates = [active_model]
        if model is None and active_model != DEFAULT_MODEL:
            fallback_candidates.append(DEFAULT_MODEL)

        last_error: Optional[Exception] = None
        for candidate_model in fallback_candidates:
            try:
                logger.info(
                    "OpenAIClient.chat start model=%s temp=%.2f max_tokens=%d",
                    candidate_model,
                    temperature,
                    max_tokens,
                )

                resp = self._chat_completion(
                    candidate_model,
                    system_prompt,
                    user_prompt,
                    temperature,
                    max_tokens,
                )

                message = resp["message"]
                content = message["content"] if isinstance(message, dict) else message.content
                if not content:
                    raise OpenAIError("OpenAI API returned empty content")

                if model is None:
                    self.model = candidate_model

                logger.debug("OpenAIClient.chat response len=%d", len(content))
                return content.strip()

            except NotFoundError as exc:  # pragma: no cover - network failures
                logger.warning(
                    "OpenAI model '%s' not found (%s); attempting fallback '%s'",
                    candidate_model,
                    exc,
                    DEFAULT_MODEL,
                )
                last_error = exc
                continue
            except Exception as exc:  # pragma: no cover - network failures
                logger.exception("OpenAIClient.chat failed: %s", exc)
                raise OpenAIError(str(exc)) from exc

        assert last_error is not None
        raise OpenAIError(str(last_error)) from last_error

    def _chat_completion(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> dict:
        """Execute a chat completion via SDK or HTTP fallback."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        if self._client is not None:
            resp = self._client.chat.completions.create(
                model=model,
                timeout=self.timeout_seconds,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            choice = resp.choices[0].message
            return {"message": choice}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise OpenAIError("OpenAI REST request timed out") from exc
        except httpx.HTTPError as exc:
            raise OpenAIError(f"OpenAI REST request failed: {exc}") from exc

        if response.status_code == 404:
            raise NotFoundError("model_not_found")
        if response.status_code >= 400:
            raise OpenAIError(
                f"OpenAI REST error {response.status_code}: {response.text.strip()}"
            )

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise OpenAIError("OpenAI REST API returned no choices")
        return {"message": choices[0].get("message", {})}

    # --- compat helpers with the previous wrapper ----------------------------

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
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
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model,
                )
            except OpenAIError as exc:  # pragma: no cover - network failures
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(1.5)
        assert last_error is not None
        raise last_error

    def complete(
        self,
        *,
        prompt: str,
        system: Optional[str] = None,
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
            return self.chat(system_prompt, user_prompt, temperature=0.2, max_tokens=220)
        except OpenAIError:
            return ""

    def summarize_morning_brief(self, raw_report_text: str) -> str:
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
            return self.chat(system_prompt, user_prompt, temperature=0.1, max_tokens=200)
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
