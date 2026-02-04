from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateResult:
    ok: bool
    code: str = ""
    note: str = ""
    details: dict[str, Any] | None = None
