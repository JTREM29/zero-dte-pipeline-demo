from __future__ import annotations
import json
from typer.testing import CliRunner
from src.cli import app

runner = CliRunner()


def _parse(raw: str):
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
                    objs.append(json.loads(buf))
                except Exception:
                    pass
                buf = ''
    return objs


def test_market_stream_indicators(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    res = runner.invoke(app, [
        'market_stream', '--symbols', 'X1', '--synthetic', '--ticks', '40', '--interval-secs', '0.02',
        '--window', '15', '--strategy', 'odte_direction', '--indicators', '--rsi-period', '14', '--bb-period', '10', '--alt-vol-period', '10'
    ])
    assert res.exit_code == 0, res.stdout
    objs = _parse(res.stdout)
    emissions = [o for o in objs if o.get('symbol') == 'X1' and 'features' in o]
    assert emissions
    # Look for indicator keys in at least one emission
    assert any('rsi' in e['features'] for e in emissions), 'RSI missing'
    assert any('bb_mid' in e['features'] for e in emissions), 'Bollinger mid missing'
    # Alt vol estimators may appear once enough data collected
    # Not strictly required every run but expect at least one after 40 ticks
    assert any('gk_vol' in e['features'] for e in emissions), 'GK vol missing'
    assert any('rs_vol' in e['features'] for e in emissions), 'RS vol missing'
