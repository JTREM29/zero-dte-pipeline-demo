from __future__ import annotations
from pathlib import Path
import json
from src.utils.metrics_store import append_metric, compact_metrics
import pandas as pd


def test_compact_metrics_roundtrip(tmp_path: Path):
    for i in range(3):
        append_metric(tmp_path, "backtest", {"run": i, "pnl": i * 1.5})
    out = compact_metrics(tmp_path, "backtest")
    assert out is not None
    df = pd.read_parquet(out)
    assert len(df) == 3
    assert set(df.columns) >= {"run", "pnl", "ts"}