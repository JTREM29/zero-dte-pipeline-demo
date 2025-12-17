"""Tests for the OpenAI client wrapper."""
from __future__ import annotations

from types import SimpleNamespace

from zero_dte_pipeline.openai_client import OpenAIClient


class _DummyResponse:
    """Simple OpenAI response stub used for unit testing."""

    def __init__(self, content: str) -> None:
        message = SimpleNamespace(content=content)
        choice = SimpleNamespace(message=message)
        self.choices = [choice]


class _DummyCompletions:
    def __init__(self, owner: "_DummyOpenAI", content: str) -> None:
        self._owner = owner
        self._content = content

    def create(self, **kwargs):  # type: ignore[override]
        self._owner.last_kwargs = kwargs
        return _DummyResponse(self._content)


class _DummyOpenAI:
    """Minimal stub that mimics the OpenAI client surface we rely on."""

    def __init__(self, *, api_key: str, content: str = "OK"):
        self.api_key = api_key
        self.last_kwargs: dict | None = None
        completions = _DummyCompletions(self, content)
        self.chat = SimpleNamespace(completions=completions)


def test_pro_model_available(monkeypatch):
    """Ensure the wrapper targets the pro model and surfaces the response content."""

    # Provide deterministic configuration.
    monkeypatch.setenv("OPENAI_MODEL_NAME", "gpt-5.1-pro")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    # Swap the real OpenAI client for the stub so no network call occurs.
    created_clients: list[_DummyOpenAI] = []

    def _fake_openai(api_key: str) -> _DummyOpenAI:
        client = _DummyOpenAI(api_key=api_key)
        created_clients.append(client)
        return client

    monkeypatch.setattr("zero_dte_pipeline.openai_client.OpenAI", _fake_openai)

    wrapper = OpenAIClient()
    result = wrapper.generate("test system", "Say OK.")

    assert result == "OK"
    assert created_clients
    assert created_clients[0].last_kwargs is not None
    assert created_clients[0].last_kwargs["model"] == "gpt-5.1-pro"
