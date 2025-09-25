# ZeroDTE-pipeline

A structured Python pipeline for researching, ingesting, and modeling zero-day (0DTE) options data.

## Features Implemented
- Config management via Pydantic `Settings`
- Polygon + IQFeed client placeholders
- Simple strategy producing demo signals
- Logging (console + rotating file in `logs/pipeline.log`)
- Optional OpenAI summarization of signals
- Parquet persistence for signals (`data/signals/`)
- Typer CLI (`python -m src.cli --help`)
- Smoke tests (`pytest`)

## Layout
```
ZeroDTE-pipeline/
  src/
    config.py
    cli.py
    persistence.py
    openai_client.py
    datafeeds/
    strategies/
    utils/
  tests/
  data/
  logs/
  main.py
```

## Quickstart
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env  # fill in keys
python main.py
```

## CLI Examples
```powershell
python -m src.cli snapshot SPX
python -m src.cli demo_strategy --symbol SPX
python -m src.cli iqfeed_chain SPX
python -m src.cli ensure_dirs
```

## Environment Variables (.env)
| Variable | Purpose |
|----------|---------|
| POLYGON_API_KEY | Polygon data access |
| IQFEED_USERNAME | IQFeed login |
| IQFEED_PASSWORD | IQFeed login |
| OPENAI_API_KEY  | OpenAI summarization |
| LOG_LEVEL       | Logging verbosity |
| DATA_DIR        | Base data directory (default `data`) |

## Tests
```powershell
pytest -q
```

## Next Ideas
- Real Polygon REST + WebSocket
- IQFeed streaming interface
- Strategy parameterization + backtesting harness
- Feature engineering module
- CI workflow (GitHub Actions)
- Risk management / PnL attribution utilities

## License
(Choose a license and add a LICENSE file.)
