from __future__ import annotations
import json
from typer.testing import CliRunner
from src.cli import app

runner = CliRunner()


def test_list_strategies_cli():
    result = runner.invoke(app, ["list_strategies"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "strategies" in payload
    assert "simple_intraday_spx" in payload["strategies"]


def test_backtest_bars_avg_strength_field(monkeypatch, tmp_path):
    # Create minimal bars parquet to drive backtest_bars
    import pandas as pd
    date = "2025-01-01"
    base = tmp_path / "data" / "derived" / "bars" / "symbol=TEST" / "interval=1.0" / f"date={date}"
    base.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([
        {"symbol": "TEST", "start_ts": 0, "end_ts": 1, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1, "trades": 1},
        {"symbol": "TEST", "start_ts": 1, "end_ts": 2, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 2, "trades": 2},
        {"symbol": "TEST", "start_ts": 2, "end_ts": 3, "open": 11, "high": 13, "low": 10, "close": 12, "volume": 3, "trades": 3},
        {"symbol": "TEST", "start_ts": 3, "end_ts": 4, "open": 12, "high": 12, "low": 10, "close": 11, "volume": 4, "trades": 4},
        {"symbol": "TEST", "start_ts": 4, "end_ts": 5, "open": 11, "high": 12, "low": 10, "close": 10, "volume": 5, "trades": 5},
    ])
    df.to_parquet(base / "part-000.parquet", index=False)

    # Point settings.data_dir to tmp_path/data via env var patch
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    result = runner.invoke(app, [
        "backtest_bars", "TEST", "--interval", "1.0", "--date", date, "--fast", "2", "--slow", "4"
    ])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "avg_signal_strength" in payload
    assert payload["avg_signal_strength"] >= 0.0