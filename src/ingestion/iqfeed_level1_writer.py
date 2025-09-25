"""Micro-batch parquet writer for IQFeed Level1 quotes.

Collects parsed quote dicts and flushes to partitioned parquet files on size or time threshold.
"""
from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
import time
from typing import List, Dict, Any, Optional, Literal
import pandas as pd

from src.persistence import ensure_dir
from src.utils.logging_setup import get_logger


@dataclass
class BatchConfig:
    base_dir: Path
    flush_secs: float = 2.0
    max_rows: int = 5000
    compression: Literal["snappy", "gzip", "brotli", "lz4", "zstd"] = "zstd"


class Level1BatchWriter:
    def __init__(self, cfg: BatchConfig):
        self.cfg = cfg
        self.buffer: List[Dict[str, Any]] = []
        self.last_flush = time.time()
        self.log = get_logger("iqfeed.l1writer")

    def add(self, quote: Dict[str, Any]):
        self.buffer.append(quote)
        now = time.time()
        if len(self.buffer) >= self.cfg.max_rows or (now - self.last_flush) >= self.cfg.flush_secs:
            self.flush()

    def flush(self) -> Optional[Path]:
        if not self.buffer:
            return None
        df = pd.DataFrame(self.buffer)
        self.buffer.clear()
        self.last_flush = time.time()
        date = time.strftime("%Y-%m-%d", time.gmtime())
        out_dir = ensure_dir(self.cfg.base_dir / "raw" / "iqfeed" / "level1" / f"date={date}")
        fname = f"part-{int(self.last_flush*1000)}.parquet"
        out_path = out_dir / fname
        df.to_parquet(out_path, index=False, compression=self.cfg.compression)
        self.log.debug("Flushed %s rows -> %s", len(df), out_path)
        return out_path


__all__ = ["Level1BatchWriter", "BatchConfig"]