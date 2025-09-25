"""Basic smoke tests to ensure the scaffold imports and entrypoint run without errors."""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_import_packages():
    for mod in [
        "src.utils.logging_setup",
        "src.datafeeds.polygon_client",
        "src.strategies.simple_intraday_spx",
    ]:
        importlib.import_module(mod)


def test_main_execution():
    # Run the main entrypoint and ensure exit code 0
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Run complete" in result.stdout or "Run complete" in result.stderr
