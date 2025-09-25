"""IQFeed client placeholder.

Real implementation would manage TCP socket login, request chains, and streaming updates.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import os


@dataclass
class IQFeedConfig:
    username: str | None
    password: str | None

    @classmethod
    def from_env(cls) -> "IQFeedConfig":
        return cls(
            username=os.getenv("IQFEED_USERNAME"),
            password=os.getenv("IQFEED_PASSWORD"),
        )


class IQFeedClient:
    def __init__(self, cfg: IQFeedConfig):
        self._cfg = cfg

    def fetch_demo_chain(self, root: str) -> list[dict[str, Any]]:
        # Placeholder stub
        return [
            {"symbol": f"{root}250925C00000000", "type": "call", "_demo": True},
            {"symbol": f"{root}250925P00000000", "type": "put", "_demo": True},
        ]
