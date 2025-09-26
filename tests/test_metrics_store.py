from __future__ import annotations
from pathlib import Path
import json
from src.utils.metrics_store import append_metric


def test_append_metric_creates_and_appends(tmp_path: Path):
    path = append_metric(tmp_path, "latency", {"symbol": "SPX", "p50_ms": 12.3})
    assert path.exists()
    # Append second
    second = append_metric(tmp_path, "latency", {"symbol": "SPX", "p50_ms": 14.0})
    assert second == path
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["symbol"] == "SPX"
    assert "ts" in rec