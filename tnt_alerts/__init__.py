"""TNT Alert Agent (v1): deterministic alert intents, storage, and Discord wiring.

This package intentionally keeps the alert contract deterministic:
- Natural language or DSL is compiled into a constrained JSON schema (AlertIntent v1)
- Validation gates unsupported/ambiguous requests
- Everything downstream is data-driven and auditable
"""

from __future__ import annotations

__all__ = [
    "__version__",
]

__version__ = "0.1.0"
