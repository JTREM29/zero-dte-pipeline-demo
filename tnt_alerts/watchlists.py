from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .alert_intent import Targets, TargetsType


def _watchlists_root() -> Path:
    # Simple local storage option:
    # watchlists/<user_id>/<name>.json
    return Path("watchlists")


def load_watchlist_symbols(*, user_id: str, name: str) -> list[str]:
    """Load a user's watchlist symbols.

    Supported backends (in this order):
    1) Local file: watchlists/<user_id>/<name>.json with {"symbols": ["SPY", ...]}
    2) TNT concierge integration (if available)

    Always returns unique uppercase symbols.
    """

    user_id = (user_id or "").strip()
    name = (name or "").strip() or "default"

    # (1) Local file backend
    path = _watchlists_root() / user_id / f"{name}.json"
    if path.exists():
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            raw = obj.get("symbols", []) if isinstance(obj, dict) else []
            return _normalize_symbols(raw)
        except Exception:
            return []

    # (2) Concierge fallback (existing repo integration)
    try:
        from tnt_concierge.throttle import watchlist_for_user

        # user_id is typically like "discord:123"; best-effort int extraction
        user_id_int = None
        if ":" in user_id:
            tail = user_id.split(":", 1)[1]
            try:
                user_id_int = int(tail)
            except Exception:
                user_id_int = None

        wl = watchlist_for_user(user_id_int)
        return _normalize_symbols(wl)
    except Exception:
        return []


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        s2 = (s or "").strip().upper()
        if not s2:
            continue
        if s2 not in seen:
            out.append(s2)
            seen.add(s2)
    return out


def expand_targets(targets: Targets, *, user_id: str) -> list[str]:
    """Expand Targets into a concrete list of symbols, enforcing max_symbols deterministically."""

    if targets.type == TargetsType.symbols:
        return targets.symbols[: int(targets.max_symbols or 20)]

    name = (targets.watchlist or "").strip() or "default"
    syms = load_watchlist_symbols(user_id=user_id, name=name)
    return syms[: int(targets.max_symbols or 20)]
