from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from tnt_redis import redis_client, rkey


@dataclass(frozen=True)
class CacheResult:
    hit: bool
    value: Optional[Any]


def _json_dumps(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), ensure_ascii=False)


def _json_loads(s: str) -> Any:
    return json.loads(s)


def cache_get(key: str) -> CacheResult:
    r = redis_client()
    raw = r.get(key)
    if raw is None:
        return CacheResult(hit=False, value=None)
    try:
        return CacheResult(hit=True, value=_json_loads(raw))
    except Exception:
        return CacheResult(hit=True, value=raw)


def cache_set(key: str, value: Any, ttl_s: int) -> None:
    r = redis_client()
    r.set(key, _json_dumps(value), ex=ttl_s)


# Optional: semantic cache utilities (not required for /oi)
_whitespace = re.compile(r"\s+")
_non_word = re.compile(r"[^\w\s\-/.:]")


def normalize_text(text: str) -> str:
    t = text.strip().lower()
    t = _non_word.sub("", t)
    t = _whitespace.sub(" ", t)
    return t[:500]


def semantic_key(scope: str, text: str, extra: str = "") -> str:
    norm = normalize_text(text) + "|" + extra
    h = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
    return rkey("scache", scope, h)


def semantic_get(scope: str, text: str, extra: str = "") -> CacheResult:
    return cache_get(semantic_key(scope, text, extra))


def semantic_set(scope: str, text: str, value: Any, ttl_s: int, extra: str = "") -> None:
    cache_set(semantic_key(scope, text, extra), value, ttl_s)
