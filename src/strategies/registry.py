"""Strategy registry for dynamic discovery and loading.

Allows referencing strategies by short name on the CLI instead of hard-coding
imports in each command. New strategies can register themselves via the
`register_strategy` decorator or manual call.
"""
from __future__ import annotations
from typing import Callable, Dict, Type, Any

_REGISTRY: Dict[str, Callable[..., Any]] = {}


def register_strategy(name: str):
    """Decorator to register a strategy class or factory under a name.

    Example:
        @register_strategy("simple_intraday_spx")
        class SimpleIntradaySPXStrategy: ...
    """
    def _wrap(obj: Callable[..., Any]):
        key = name.strip().lower()
        if key in _REGISTRY:
            # Overwrite allowed: last one wins (explicit for iterative dev)
            pass
        _REGISTRY[key] = obj
        return obj
    return _wrap


def get_strategy(name: str) -> Callable[..., Any]:
    key = name.strip().lower()
    if key not in _REGISTRY:
        raise KeyError(f"Strategy '{name}' not found. Registered: {list(_REGISTRY)}")
    return _REGISTRY[key]


def list_strategies() -> Dict[str, str]:
    return {k: v.__name__ for k, v in _REGISTRY.items()}

__all__ = ["register_strategy", "get_strategy", "list_strategies"]
