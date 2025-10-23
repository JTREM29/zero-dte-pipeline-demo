"""OpenAI client wrapper for generating commentary or aggregating signals.
"""
from __future__ import annotations
from typing import Sequence, Mapping, Any
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
            # Support project-scoped keys. If a project is provided, do NOT send organization
            # header to avoid mismatches with project-scoped API keys.
            project = os.getenv("OPENAI_PROJECT") or os.getenv("OPENAI_PROJECT_ID")
            organization = os.getenv("OPENAI_ORG") or os.getenv("OPENAI_ORGANIZATION")
            try:
                if project:
                    # Prefer project-only construction for project-scoped keys
                    self._client = OpenAI(api_key=api_key, project=project)
                elif organization:
                    self._client = OpenAI(api_key=api_key, organization=organization)
                else:
                    self._client = OpenAI(api_key=api_key)
            except TypeError:
                # Older SDKs may not accept project/org; fall back
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

    # --- New summarization method for neutral market commentary ---
    def summarize_market(self, features: Mapping[str, Any], max_tokens: int = 120, retries: int = 2) -> str:
        """Create a neutral, non-advisory summary of provided numeric/feature context.

        Strips obviously risky phrasing and ensures a bounded feature serialization.
        """
        if not self._client:
            return "(OpenAI disabled)"
        # Lightweight sanitization & truncation
        def _truncate(v: Any):
            s = str(v)
            if len(s) > 120:
                return s[:117] + "..."
            return s
        safe_items = []
        for k, v in list(features.items())[:50]:  # hard cap entries
            safe_items.append(f"{k}={_truncate(v)}")
        feat_str = ", ".join(safe_items)
        base_prompt = (
            "Provide a concise, strictly informational one-paragraph summary of these intraday "
            "features without offering investment advice, recommendations, or forward-looking guarantees. "
            "Avoid words like 'should', 'recommend', 'profit', or directives. Data: " + feat_str
        )
        attempt = 0
        last_err = None
        while attempt <= retries:
            try:
                completion = self._client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "system", "content": "You are a neutral market data summarizer."},
                              {"role": "user", "content": base_prompt}],
                    max_tokens=max_tokens,
                    temperature=0.2,
                )
                text = completion.choices[0].message.content  # type: ignore[attr-defined]
                if not isinstance(text, str):  # defensive
                    return "Summary unavailable"
                # Basic advisory scrubbing
                lowered = text.lower()
                banned = ["recommend", "should", "buy", "sell", "advice", "promise"]
                if any(b in lowered for b in banned):
                    text = "Neutral data summary: " + text
                return text.strip()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                attempt += 1
        return f"OpenAI summary error: {last_err}" if last_err else "Summary failed"

    def sentiment_score(self, text: str, *, retries: int = 1) -> float:
        """Classify sentiment and return a numeric score in [-1, 1].

        Uses a constrained prompt to elicit only a JSON with a numeric score field.
        Returns 0.0 if the client is disabled or parsing fails.
        """
        if not self._client:
            return 0.0
        system = (
            "You are a sentiment classifier for financial text. "
            "Return ONLY a JSON object with a numeric field 'score' strictly between -1 and 1, "
            "where -1 is very negative, 0 is neutral, and 1 is very positive."
        )
        user = f"Text: {text}"
        last_err: Exception | None = None
        for _ in range(max(1, retries)):
            try:
                completion = self._client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.0,
                    max_tokens=16,
                )
                content = completion.choices[0].message.content  # type: ignore[attr-defined]
                if isinstance(content, str):
                    # Try to extract number from JSON-like content
                    import json as _json
                    try:
                        data = _json.loads(content)
                        val = float(data.get("score", 0.0))
                    except Exception:
                        # Fallback: regex number search
                        import re as _re
                        m = _re.search(r"-?\d+(?:\.\d+)?", content)
                        val = float(m.group(0)) if m else 0.0
                    # clamp
                    if not (val == val):  # NaN check
                        val = 0.0
                    return float(max(-1.0, min(1.0, val)))
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        return 0.0
