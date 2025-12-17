"""Default OpenAI model settings."""
from __future__ import annotations

import os

DEFAULT_MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5.1-pro")
FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-4.1")
FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-5.1-mini")
