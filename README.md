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

## One-Line Bootstrap (optional PowerShell snippet)
```powershell
# Creates venv, installs deps, copies env template
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt; if (-not (Test-Path .env)) { copy .env.example .env }
```

## CLI Examples
```powershell
python -m src.cli snapshot SPX
python -m src.cli demo_strategy --symbol SPX
python -m src.cli iqfeed_chain SPX
python -m src.cli ensure_dirs
```

## Additional CLI Commands
```powershell
python -m src.cli polygon_last_spx            # raw previous aggregate JSON
python -m src.cli polygon_last_spx --normalize  # normalized bar DataFrame as JSON
python -m src.cli iqfeed_ping                 # test IQFeed socket connectivity
python -m src.cli ingest_prev_spx             # ingest previous SPX aggregate -> parquet
```

## Environment Variables (.env)
| Variable | Purpose |
|----------|---------|
| POLYGON_API_KEY | Polygon data access |
| IQFEED_USERNAME | IQFeed login |
| IQFEED_PASSWORD | IQFeed login |
| IQFEED_HOST | IQFeed desktop host (default 127.0.0.1) |
| IQFEED_PORT_LEVEL1 | Level1/lookup port (default 5009) |
| IQFEED_PORT_ADMIN | Admin port (default 5009) |
| OPENAI_API_KEY  | OpenAI summarization |
| LOG_LEVEL       | Logging verbosity |
| DATA_DIR        | Base data directory (default `data`) |
| LOG_DIR         | Log directory (default `logs`) |

## Tests
```powershell
pytest -q
```

## Implementation Notes
- Polygon requests include simple retry/backoff (exponential linear pattern)
- In-memory TTL cache (~30s) for previous SPX aggregate to reduce API calls
- Normalization helper converts Polygon aggregate keys to readable column names
- Ingestion writes parquet to `data/raw/polygon/prev_spx/date=YYYY-MM-DD/part.parquet`

## Next Ideas
- Real Polygon REST + WebSocket
- IQFeed streaming interface
- Strategy parameterization + backtesting harness
- Feature engineering module
- CI workflow (GitHub Actions)
- Risk management / PnL attribution utilities

## License
(Choose a license and add a LICENSE file.)
