from __future__ import annotations
import json
from typer.testing import CliRunner
from src.cli import app

runner = CliRunner()


def test_chain_window_cli_invokes():
    symbols = "SPXW250925C00045000,SPXW250925P00045500,SPXW250925C00060000"
    result = runner.invoke(app, ["chain_window", symbols, "--root", "SPXW", "--center", "4550", "--width", "100"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["root"] == "SPXW"
    strikes = sorted([c["strike"] for c in payload["contracts"]])
    assert 4500 in strikes and 4550 in strikes
    assert all(s < 6000 for s in strikes)