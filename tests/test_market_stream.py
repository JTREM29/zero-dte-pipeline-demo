from __future__ import annotations
import json
from typer.testing import CliRunner
from src.cli import app
from src.analytics.volatility import RollingVolatility

runner = CliRunner()


def test_rolling_volatility_basic():
    rv = RollingVolatility(window=5)
    prices = [100, 101, 102, 101, 100, 99]
    for p in prices:
        rv.update(p)
    snap = rv.snapshot()
    assert snap['count'] >= 5
    assert 'rolling_vol' in snap


def test_market_stream_synthetic(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    # limit ticks and fast interval
    result = runner.invoke(app, [
        'market_stream', '--symbols', 'TESTSYM', '--synthetic', '--ticks', '8', '--interval-secs', '0.2', '--window', '5', '--strategy', 'odte_direction'
    ])
    # The command streams multiple JSON lines; last line is summary object with 'status'
    assert result.exit_code == 0, result.stdout
    raw = result.stdout
    import json as _json
    objs = []
    buf = ''
    depth = 0
    for ch in raw:
        if ch == '{':
            if depth == 0:
                buf = ''
            depth += 1
        if depth > 0:
            buf += ch
        if ch == '}':
            depth -= 1
            if depth == 0 and buf:
                try:
                    objs.append(_json.loads(buf))
                except Exception:
                    pass
                buf = ''
    final_obj = None
    for o in objs:
        if isinstance(o, dict) and o.get('status') == 'completed':
            final_obj = o
    assert final_obj is not None, "Final status object not found"
    assert final_obj['emissions'] > 0
