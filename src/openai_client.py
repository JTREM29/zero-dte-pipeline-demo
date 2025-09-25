"""OpenAI client wrapper for generating commentary or aggregating signals.
"""
from __future__ import annotations
from typing import Sequence
import os

try:  # soft dependency
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class OpenAIWrapper:
    def __init__(self, api_key: str | None):
        self.api_key = api_key
        self._client = None
        if api_key and OpenAI is not None:
            self._client = OpenAI(api_key=api_key)

    def summarize_signals(self, signals: Sequence[dict]) -> str:
        if not self._client:
            return "(OpenAI disabled)"
        # Minimal example; adapt to newer responses if needed
        prompt = "Summarize these strategy signals: " + str(signals)
        try:
            completion = self._client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=64,
            )
            return completion.choices[0].message.content  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return f"OpenAI error: {exc}"
