from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Optional

PublishMode = Literal["scores", "quotes", "full"]


@dataclass
class FutQuote:
    sym: str
    px: float
    chg: float
    chg_pct: float
    ts_utc: int
    source: str


@dataclass
class FutScores:
    trend: Dict[str, float]
    impulse: Dict[str, float]
    vol_mult: float
    breadth_bearish: int
    breadth_total: int
    regime: str
    updated_utc: int


@dataclass
class FutConfig:
    enabled: bool = True
    publish_mode: PublishMode = "quotes"
    post_channel_id: Optional[int] = None
    post_interval_sec: int = 60
