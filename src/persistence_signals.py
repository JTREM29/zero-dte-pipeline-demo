"""Signal persistence utilities.

Writes strategy signals to partitioned parquet layout:
data/derived/signals/strategy=<strategy>/date=YYYY-MM-DD/part-*.parquet
"""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import time
import pandas as pd
from typing import List, Dict, Any


class SignalWriter:
    def __init__(self, base_dir: str | Path, strategy_name: str):
        self.base_dir = Path(base_dir)
        self.strategy = strategy_name
        self.buffer: List[Dict[str, Any]] = []

    def add(self, signal_obj):  # duck-typed StrategySignal
        rec = asdict(signal_obj)
        rec["ts"] = time.time()
        self.buffer.append(rec)

    def flush(self) -> Path | None:
        if not self.buffer:
            return None
        date = time.strftime("%Y-%m-%d", time.gmtime())
        out_dir = self.base_dir / "derived" / "signals" / f"strategy={self.strategy}" / f"date={date}"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        out_path = out_dir / f"part-{ts}.parquet"
        df = pd.DataFrame(self.buffer)
        df.to_parquet(out_path, index=False)
        self.buffer.clear()
        return out_path


__all__ = ["SignalWriter"]