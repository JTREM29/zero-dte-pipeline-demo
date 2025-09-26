"""Lightweight metrics persistence utilities.

Stores JSON-lines files under data/metrics/<category>/date=YYYY-MM-DD/metrics.log
to avoid frequent small parquet writes while preserving append-only semantics.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional
import pandas as pd


def _dated_dir(base: Path, category: str) -> Path:
    date = time.strftime("%Y-%m-%d", time.gmtime())
    p = base / "metrics" / category / f"date={date}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def append_metric(base_dir: str | Path, category: str, record: Dict[str, Any]) -> Path:
    base = Path(base_dir)
    d = _dated_dir(base, category)
    path = d / "metrics.log"
    enriched = {"ts": time.time(), **record}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(enriched) + "\n")
    return path


__all__ = ["append_metric"]


def compact_metrics(base_dir: str | Path, category: str, date: Optional[str] = None) -> Optional[Path]:
    """Read metrics log for a category/date and write a compact parquet dataset.

    Returns path to written parquet or None if no data.
    """
    base = Path(base_dir)
    if date is None:
        date = time.strftime("%Y-%m-%d", time.gmtime())
    log_path = base / "metrics" / category / f"date={date}" / "metrics.log"
    if not log_path.exists():
        return None
    rows = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        return None
    df = pd.DataFrame(rows)
    out_dir = base / "metrics_compact" / category / f"date={date}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "metrics.parquet"
    df.to_parquet(out_path, index=False)
    return out_path


__all__.append("compact_metrics")