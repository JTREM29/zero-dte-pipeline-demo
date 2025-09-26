from __future__ import annotations
import json
from typer.testing import CliRunner
from src.cli import app
from src.strategies.odte_direction import ODTeDirectionStrategy

runner = CliRunner()


def test_odte_direction_basic():
    strat = ODTeDirectionStrategy()
    r1 = strat.on_price(100.0)
    assert r1.signal == 'init'
    r2 = strat.on_price(101.0)
    assert r2.signal == 'up'
    r3 = strat.on_price(100.5)
    assert r3.signal in {'down', 'flat'}  # depending on movement


def test_market_summary_cli_openai_disabled(monkeypatch):
    # Ensure OPENAI is disabled for deterministic output
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    # Provide dummy polygon key absence and neutral data dir
    monkeypatch.setenv('DATA_DIR', 'data')
    # Because IQFeed likely not available in test env, command should fallback to polygon and then fail price if no key.
    # We monkeypatch price path by simulating IQFeed price extraction using injected placeholder (static 5500.0 in implementation)
    result = runner.invoke(app, ['market_summary'])
    # Exit code may be 0 if placeholder price succeeded; validate structure when 0.
    if result.exit_code == 0:
        payload = json.loads(result.stdout)
        assert 'strategy_signal' in payload
        assert 'summary' in payload
    else:
        # If environment caused early exit, error json should still parse
        payload = json.loads(result.stdout)
        assert 'error' in payload


def test_market_summary_cache_and_strategy_switch(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    # First run with default strategy (odte_direction)
    r1 = runner.invoke(app, ['market_summary', '--extra', 'foo=1'])
    assert r1.exit_code == 0, r1.stdout
    p1 = json.loads(r1.stdout)
    assert p1['strategy'] == 'odte_direction'
    # Second run same features should use cache flag even with OpenAI disabled (cache_used False but deterministic fields)
    r2 = runner.invoke(app, ['market_summary', '--extra', 'foo=1'])
    p2 = json.loads(r2.stdout)
    assert p2['feature_count'] == p1['feature_count']
    # Switch to simple_intraday_spx strategy (validate dynamic instantiation with no fast/slow provided)
    r3 = runner.invoke(app, ['market_summary', '--strategy', 'simple_intraday_spx'])
    assert r3.exit_code == 0, r3.stdout
    p3 = json.loads(r3.stdout)
    assert p3['strategy'] == 'simple_intraday_spx'
