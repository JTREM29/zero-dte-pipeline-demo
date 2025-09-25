"""Persistence helpers for writing datasets to Parquet.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_parquet(df: pd.DataFrame, base_dir: str | Path, name: str) -> Path:
    out_dir = ensure_dir(base_dir)
    out_path = out_dir / f"{name}.parquet"
    df.to_parquet(out_path, index=False)
    return out_path
