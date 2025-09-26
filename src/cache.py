"""Simple pluggable cache abstraction (in-memory only for now)."""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class CacheEntry:
    created: float
    ttl: float
    value: Any

    def expired(self) -> bool:
        return (time.time() - self.created) > self.ttl


class InMemoryCache:
    def __init__(self):
        self._store: dict[str, CacheEntry] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if not entry:
            return None
        if entry.expired():
            self._store.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._store[key] = CacheEntry(time.time(), ttl, value)

    def purge(self) -> int:
        removed = 0
        for k in list(self._store.keys()):
            if self._store[k].expired():
                self._store.pop(k, None)
                removed += 1
        return removed
