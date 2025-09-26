# ZeroDTE-pipeline

A structured Python pipeline for researching, ingesting, and modeling zero-day (0DTE) options data.

## Features Implemented
- Config management via Pydantic `Settings`
- Polygon REST previous aggregate ingestion (with caching + normalization)
- IQFeed Level1 streaming client (threaded queue + callback)
- Time-based bar aggregation from persisted Level1 ticks
- Options chain OCC symbol parsing + filtering utilities
- Simple strategy producing demo signals (backtest & live demo)
  - Now includes fast/slow moving average crossover (configurable via CLI)
- Backtest execution over derived bars (`backtest_bars` CLI)
- Option math utilities (Black-Scholes pricing & implied volatility estimation)
- Logging (console + rotating file in `logs/pipeline.log`, optional JSON mode)
- Parquet persistence for signals & raw market data (`data/raw/...`, `data/derived/...`)
- Typer CLI (`python -m src.cli --help`)
- OpenAI summarization (optional, if API key set)
- Unit tests & parsing / aggregation tests (`pytest`)
- GitHub Actions CI (lint + tests) [if workflow present]

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
python -m src.cli iqfeed_stream SPX,NDX       # sample real-time quotes (requires IQConnect)
python -m src.cli iqfeed_stream_persist SPX   # stream + micro-batch parquet persistence
python -m src.cli build_bars SPX --interval 1 --date 2025-09-25 --write  # build & store bars
python -m src.cli live_strategy SPX           # run simple strategy live for default duration
python -m src.cli live_strategy SPX --fast 3 --slow 10 --duration 30
python -m src.cli backtest_bars SPX --interval 1
python -m src.cli backtest_bars SPX --interval 1 --fast 3 --slow 10
python -m src.cli live_strategy SPX --persist  # persist streaming signals
python -m src.cli backtest_bars SPX --persist  # persist backtest signals
- Signals Persistence: add --persist to live_strategy or backtest_bars to write parquet under data/derived/signals/strategy=simple_intraday_spx/
python -m src.cli option_iv --spot 4500 --strike 4525 --mid 12.5 --days 0.5
python -m src.cli latency_summary SPX         # summarize ingestion latency
python -m src.cli chain_window "SPXW250925C00045000,SPXW250925P00045500" --root SPXW --center 4550 --width 100
python -m src.cli compact_metrics_cmd backtest # compact metrics logs -> parquet
python -m src.cli market_stream --symbols @SPX.X,NDX.X --synthetic --ticks 30 --correlations --annualize-factor 100000 \
  --log-returns --ewma-alpha 0.2 --corr-alert 0.5  # advanced real-time analytics demo
python -m src.cli compact_alerts               # compact correlation/other alerts to parquet
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
  - Rolling volatility (arithmetic or log returns)
  - Annualized volatility scaling (ticks_per_year factor)
  - EWMA volatility (configurable alpha)
  - Realized variance / realized volatility (per window & annualized)
  - Parkinson volatility approximation from adjacent ticks
  - Multi-symbol rolling correlations with optional alert threshold persistence
  - Feature whitelisting & raw tick persistence for lightweight downstream consumers
  - Optional technical indicators (RSI simple/Wilder, Bollinger Bands, alternative GK/RS volatility estimators)
  - Seasonality (month weight heuristic) & regime filters (choppiness / realized vol z-score) via `--seasonality` / `--regime`
  - Parquet SignalWriter for streaming signals (`--persist-signals` on market_stream) separate from metrics JSONL
    - Simulated options skew metric (placeholder) via `market_summary --skew` producing `skew_score` and `put_call_iv_spread`
- Normalization helper converts Polygon aggregate keys to readable column names
- Ingestion writes parquet to `data/raw/polygon/prev_spx/date=YYYY-MM-DD/part.parquet`

## Next Ideas
- IQFeed options chain lookup integration (currently only parsing helper)
- Exchange timestamp usage instead of wall-clock for bar alignment
- Enhanced PnL attribution & risk metrics in backtest engine
- Level2 (order book) stream ingestion & aggregation
python -m src.cli market_stream --symbols @SPX.X --synthetic --ticks 50 --indicators --seasonality --regime \
  --persist-signals --strategy odte_direction  # include indicators + regime + parquet signal persistence
python -m src.cli market_summary --skew  # include simulated options skew metrics in summary
- Strategy parameter grid search + result persistence
- Persist greeks surface snapshots & IV term structure
- Metrics dashboard / visualization layer (e.g., Panel, Dash, or Streamlit)
- Additional data quality & gap detection utilities
- Live portfolio/risk dashboard (e.g., FastAPI + Web UI)

## License
(Choose a license and add a LICENSE file.)
