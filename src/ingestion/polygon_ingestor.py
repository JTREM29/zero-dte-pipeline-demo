"""Polygon data ingestion routines.

Currently supports:
 - Previous aggregate bar for SPX (prev close) via /v2/aggs/ticker/SPX/prev
 - Underlying snapshot placeholder (extend with real endpoint later)

Writes normalized bars to parquet partition(s) under data/raw/polygon/.

Future extensions:
 - Multi-symbol support
 - Incremental daily aggregates (range endpoint)
 - WebSocket streaming ingestion (append mode)
 - Options chain snapshots
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import pandas as pd
from datetime import datetime, timezone

from src.datafeeds.polygon_client import PolygonClient, PolygonConfig
from src.utils.logging_setup import get_logger
from src.persistence import ensure_dir


@dataclass
class IngestionResult:
    symbol: str
    rows: int
    path: Optional[Path]
    normalized: bool
    ts: str
    cached: bool


class PolygonIngestor:
    def __init__(self, base_data_dir: str | Path = "data"):
        self.base_data_dir = Path(base_data_dir)
        self.log = get_logger("ingest.polygon")
        self.client: PolygonClient | None = None

    def _client(self) -> PolygonClient:
        if self.client is None:
            cfg = PolygonConfig.from_env()
            self.client = PolygonClient(cfg)
        return self.client

    def ingest_prev_spx(self, normalize: bool = True, use_cache: bool = True) -> IngestionResult:
        client = self._client()
        raw = client.last_trade_spx(use_cache=use_cache)
        if not raw:
            return IngestionResult("SPX", 0, None, normalize, datetime.now(timezone.utc).isoformat(), cached=False)
        cached = False
        # crude detection of served-from-cache (presence of our stored key timestamp age < ttl)
        # using internal attribute (acceptable for now)
        if "spx_prev" in client._cache:  # noqa: SLF001
            cached = True
        df: pd.DataFrame | None
        if normalize:
            df = client.normalize_prev_agg(raw)
        else:
            df = pd.DataFrame(raw.get("results", [])) if isinstance(raw, dict) else None
        if df is None or df.empty:
            return IngestionResult("SPX", 0, None, normalize, datetime.now(timezone.utc).isoformat(), cached=cached)
        # Add derived columns
        if "timestamp" in df.columns:
            df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df["date"] = df["datetime_utc"].dt.date.astype(str)
        out_root = ensure_dir(self.base_data_dir / "raw" / "polygon" / "prev_spx")
        # partition by date if present else just single file
        if "date" in df.columns:
            last_date = df["date"].iloc[0]
            part_dir = ensure_dir(out_root / f"date={last_date}")
            out_path = part_dir / "part.parquet"
        else:
            out_path = out_root / "prev.parquet"
        df.to_parquet(out_path, index=False)
        return IngestionResult("SPX", len(df), out_path, normalize, datetime.now(timezone.utc).isoformat(), cached=cached)


__all__ = ["PolygonIngestor", "IngestionResult"]