# ZeroDTE-pipeline

A structured Python pipeline for researching, ingesting, and modeling zero-day (0DTE) options data.

## Features Implemented
- Config management via Pydantic `Settings`
- Polygon REST previous aggregate ingestion (with caching + normalization)
- IQFeed Level1 streaming client (threaded queue + callback)
- Time-based bar aggregation from persisted Level1 ticks
- Options chain OCC symbol parsing + filtering utilities
- Hybrid options chain greeks fetch (live IQFeed fundamentals first; simulation fallback) with bucketed SPX skew metric (±25Δ / ±50Δ)
- Black-Scholes pricing & implied volatility solver (Newton) exposed via CLI
- Simple & blended ODTE strategies (includes fast/slow MA + skew factor)
- Backtest execution over derived bars (`backtest_bars` CLI)
- Range backtesting over date spans with rolling context (`backtest_range` CLI)
- Size-aware backtest engine (supports `size_update` signals; costs and PnL scale by size)
- Logging (console + rotating file in `logs/pipeline.log`, optional JSON mode)
- Parquet persistence for signals & raw market data (`data/raw/...`, `data/derived/...`)
- Typer CLI (`python -m src.cli --help`)
- OpenAI summarization (optional, if API key set)
- OpenAI Agents integration (agents ping/demo; resilient invocation and JSONL run logs)
- Unit tests (sockets, chain parsing, greeks math, skew buckets, CLI) (`pytest`)
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
  tests/
  data/
  logs/
  main.py
```

## Quickstart (Windows PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env  # fill in keys

If you only want a minimal smoke run without filling all keys, you can leave IQFeed / Polygon blank; the pipeline will fall back to synthetic/simulated paths where possible. Live options greeks & skew require IQFeed desktop running (see next section).
1. Install IQFeed Desktop (iqconnect) from DTN.
2. Ensure your account has the Options entitlement (US Equity & Index options + greeks/fundamentals).
3. Launch IQFeed (iqconnect.exe) and log in BEFORE running any live commands here.
4. Keep the client open; do not close the small connection dialog while scripts run.
5. (Optional) In IQFeed Client Settings:
  - Set Lookup / Level1 ports if you customized them. Defaults typically: 5009 (Level1/lookup combo). Adjust env vars if different.
6. Verify connectivity:
  ```powershell
  python -m src.cli iqfeed_ping
  ```
  You should see a version / ok response. If it hangs or errors, check firewall and that iqconnect is logged in.

### Environment Variables for IQFeed
Set in `.env` or your shell:
```
IQFEED_USERNAME=your_login
IQFEED_PASSWORD=your_password
IQFEED_HOST=127.0.0.1
IQFEED_PORT_LEVEL1=5009
IQFEED_PORT_LOOKUP=5009  # if you split ports, set accordingly
```

### Live vs Fallback Behavior
The greeks/chain module attempts a live fetch first (needs iqconnect running & entitlement). If:
- Connection fails OR
- Missing fieldnames / fundamentals time out

It transparently falls back to a simulated chain (pricing via Black-Scholes with heuristic IV surface). Logs will indicate fallback so you can distinguish data provenance.

To force awareness of the data source, watch log lines containing `greeks_live` or `sim_fallback` or extend the CLI with a `--live-only` flag (planned).

### Persisting Environment Variables
You can either keep secrets in a local `.env` (loaded automatically if `python-dotenv` is installed) or set them in your shell/user profile.

PowerShell (session only):
```powershell
$Env:POLYGON_API_KEY = "YOUR_REAL_KEY"
python main.py
```

Persistent (new terminals after this) using `setx`:
```powershell
setx POLYGON_API_KEY "YOUR_REAL_KEY"
```

Using the template:
```powershell
copy .env.example .env
# edit .env to insert keys safely (never commit .env)
```

To verify a key is loaded:
```powershell
python -c "import os; print(bool(os.getenv('POLYGON_API_KEY')))"
```

Rotate keys immediately if they are accidentally exposed (revoke old, issue new, update `.env`).

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
python -m src.cli iqfeed_greeks_live SPX --limit 12  # live greeks snapshot (falls back if needed)
python -m src.cli ensure_dirs

# Ensemble Gate (nowcast + micro)
python -m src.cli ensemble_gate --expiry 2025-10-06 --tsfm-mode chronos --device dml

# Intraday prediction (explicit inputs)
python -m src.cli predict_intraday `
  --ticks logs/rocket_ticks_2025-10-06_poly.csv `
  --alerts logs/rocket_alerts_20251006.jsonl `
  --out logs/rocket_alerts_20251006_ensemble.jsonl `
  --backend chronos --device dml --horizon-min 10

# Write a small close timestamp artifact
python -m src.cli stamp_close --out logs/close_stamp.json --label close
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

# Build bars with advanced options
python -m src.cli build_bars SPX --interval 1 --date 2025-09-26 --enforce-continuous --watermark-delay 0.5 --outlier-mode winsorize
python -m src.cli live_strategy SPX           # run simple strategy live for default duration
python -m src.cli live_strategy SPX --fast 3 --slow 10 --duration 30
python -m src.cli backtest_bars SPX --interval 1
python -m src.cli backtest_bars SPX --interval 1 --fast 3 --slow 10
python -m src.cli live_strategy SPX --persist  # persist streaming signals
python -m src.cli backtest_bars SPX --persist  # persist backtest signals
- Signals Persistence: add --persist to live_strategy or backtest_bars to write parquet under data/derived/signals/strategy=simple_intraday_spx/
python -m src.cli option_price --spot 4500 --strike 4525 --call --days 0.5 --iv 0.12
python -m src.cli option_iv_solve --spot 4500 --strike 4525 --mid 12.5 --days 0.5 --call
python -m src.cli latency_summary SPX         # summarize ingestion latency
python -m src.cli chain_window "SPXW250925C00045000,SPXW250925P00045500" --root SPXW --center 4550 --width 100
python -m src.cli compact_metrics_cmd backtest # compact metrics logs -> parquet
python -m src.cli market_stream --symbols @SPX.X,NDX.X --synthetic --ticks 30 --correlations --annualize-factor 100000 \
  --log-returns --ewma-alpha 0.2 --corr-alert 0.5  # advanced real-time analytics demo
python -m src.cli compact_alerts               # compact correlation/other alerts to parquet
python -m src.cli iqfeed_greeks_live SPX --limit 20 --json  # structured greeks output
python -m src.cli market_summary --skew        # includes live-or-fallback skew buckets & score
python -m src.cli market_summary --skew --skew-live-only  # require live greeks (skip if unavailable)
python -m src.cli market_summary --skew --skew-buckets "0.2,0.35,0.5" --skew-tol 0.04 --skew-scale 0.18 --persist-skew
python -m src.cli market_summary --skew --write-skew-parquet  # writes to data/derived/skew/date=YYYY-MM-DD/
python -m src.cli market_summary --skew --skew-parquet-out data/derived/skew/custom/path.parquet
python -m src.cli iqfeed_greeks_live 5000 --live-only --buckets "0.2,0.35,0.5" --tol 0.04 --scale 0.18 --persist
python -m src.cli iqfeed_greeks_live 5000 --write-parquet  # writes quotes to data/derived/greeks_live/date=YYYY-MM-DD/
python -m src.cli iqfeed_greeks_live 5000 --parquet-out data/derived/greeks_live/custom/path.parquet
python -m src.cli compact_greeks_live          # compact greeks_live metrics to parquet
python -m src.cli compact_skew                 # compact skew metrics to parquet
python -m src.cli iqfeed_diagnostics           # quick connectivity + fieldnames + chain + optional stream check
python -m src.cli plot_skew                    # render skew_score timeseries PNG for today
python -m src.cli plot_greeks data/derived/greeks_live/date=2025-09-26/part-*.parquet  # IV vs strike scatter
python -m src.cli agents_ping                    # lightweight agents path check
python -m src.cli agent_demo --backend agents    # quick demo run via OpenAI Agents SDK

# Stream with time-bar aggregation
python -m src.cli market_stream --symbols @SPX.X --duration 20 --bars-interval 1 --bars-watermark-delay 0.5 --bars-emit
# Persist streaming bars to parquet
python -m src.cli market_stream --symbols @SPX.X --duration 20 --bars-interval 1 --bars-watermark-delay 0.5 --bars-write

# Ensemble model (experimental; requires torch)
python -m src.cli train_ensemble --window 64 --horizon 1 --epochs 3 --n 2048 --out-path models/ensemble.ckpt
python -m src.cli predict_ensemble --ckpt models/ensemble.ckpt --window 64 --horizon 1 --n 512

## Ensemble from bars dataset (experimental)

Two layouts are supported for loading training data:
- Compact (preferred): `data/derived/bars_compact/symbol=SYMBOL/interval=INTERVAL/date=YYYY-MM-DD.parquet`
- Raw derived fallback: `data/derived/bars/symbol=SYMBOL/interval=INTERVAL/date=YYYY-MM-DD[/hour=HH]/part-*.parquet`

The loader returns the `close` series ordered by `start_ts`. You can set a custom base path with `DATA_DIR` env or rely on the default `data`.

Normalization options: `none`, `returns`, `log_returns`, `zscore`.

Examples (PowerShell):

```powershell
# Train on bars between dates (uses compact if available, else raw derived)
python -m src.cli train_ensemble_bars SPX 1.0 --start-date 2025-09-22 --end-date 2025-09-26 `
  --window 64 --horizon 1 --epochs 3 --batch-size 64 --normalize returns `
  --out-path models/ensemble_bars.ckpt

# Predict using a previously saved checkpoint (apply same normalization used in training)
python -m src.cli predict_ensemble_bars --ckpt models/ensemble_bars.ckpt SPX 1.0 `
  --start-date 2025-09-25 --end-date 2025-09-26 --window 64 --horizon 1 --normalize returns

# Validate a checkpoint loads and forward pass works (returns output shape)
python -m src.cli check_ensemble_ckpt models/ensemble_bars.ckpt --window 64 --horizon 1
```

Config file override (JSON):

```json
{
  "epochs": 5,
  "lr": 0.0005,
  "batch_size": 32
}
```

```powershell
python -m src.cli train_ensemble_bars SPX 1.0 --start-date 2025-09-22 --end-date 2025-09-26 `
  --config train_cfg.json --out-path models/ensemble_bars_cfg.ckpt
```

Notes:
- `returns`/`log_returns` drop the first element; ensure your `--window` is feasible for the resulting series length.
- Keep `--normalize` consistent between train and predict.
- If VS Code flags torch import unresolved, ensure the selected Python interpreter matches the one where you installed `requirements.txt`.
- On Windows with RTX 50xx cards lacking CUDA wheel support, prefer `--device dml` (DirectML). You can run `python -m src.cli gpu_probe` to see backend availability.
```

### Backtest Range (rolling context + entries-only filter)
```powershell
# Run strategy across a date range; attach rolling windows and month to market_ctx
python -m src.cli backtest_range SPX --start-date 2025-09-22 --end-date 2025-09-26 `
  --strategy odte_composite --window-len 120 --signals-filter entries `
  --annualize-factor 100000 --commission 2 --slippage-bps 10
```

Notes:
- `--window-len` attaches `high_window`, `low_window`, `close_window`, and `month` per bar.
- `--signals-filter entries` keeps only enter/exit signals for trade toggling; continuous signals (e.g., `odte_score`) are ignored for PnL.
- The engine is size-aware: strategies can emit `size_update` to set a fractional position size in [0,1]. PnL and costs scale by that size.

#### Greeks factor in backtests
- Sources supported via `--greeks-source`:
  - `auto` (default): persisted greeks → compute-on-the-fly → proxy.
  - `compute`: compute daily snapshot from options chain greeks (live-first, sim fallback).
  - `proxy`: use volatility/choppiness proxies only.
  - `none`: disable greeks factor.
- Compute mode and tuning:
  - `--greeks-compute-mode` rr|fly|slope|composite (default: composite)
  - `--greeks-compute-kwargs` key=val or JSON (e.g., `w_rr=0.6,w_fly=0.2,w_slope=0.2,rr_scale=0.2`)

Examples:
```powershell
# Auto (prefer persisted greeks; else compute; else proxy)
python -m src.cli backtest_range SPX --start-date 2025-09-25 --end-date 2025-09-26 --greeks-source auto --greeks-proxy vol_z

# Force compute with composite weights
python -m src.cli backtest_range SPX --start-date 2025-09-26 --end-date 2025-09-26 `
  --greeks-source compute --greeks-compute-mode composite `
  --greeks-compute-kwargs "w_rr=0.6,w_fly=0.2,w_slope=0.2,rr_scale=0.2,fly_scale=0.2,slope_scale=0.2"

# Force proxy fallback (vol_z or chop)
python -m src.cli backtest_range SPX --start-date 2025-09-26 --end-date 2025-09-26 --greeks-source proxy --greeks-proxy vol_z
```

#### Schedule greeks snapshots (persist for later backtests)
```powershell
# Capture composite greeks at 09:30 ET and exit
python -m src.cli greeks_schedule 5250 --times 09:30 --tz America/New_York --max-runs 1 --mode composite

# Capture at open/mid/close daily and keep running
python -m src.cli greeks_schedule 5250 --times "09:30,12:00,15:45" --tz America/New_York --repeat-daily --mode composite
```
Persisted snapshots are written to `data/metrics/greeks_factor/date=YYYY-MM-DD/metrics.log` (and compact parquet under `metrics_compact/greeks_factor/...`). The backtester will auto-align the nearest snapshot across the session.
```

### Greeks Provenance Summary
Summarize which greeks source would be used per day in a date range without running a strategy. This inspects only persisted metrics and bar availability.

```powershell
# Basic summary (auto prefers persisted then compute)
python -m src.cli greeks_provenance SPX --interval 1.0 --start-date 2024-08-01 --end-date 2024-08-05 --greeks-source auto

# Export to parquet (suffix-driven)
python -m src.cli greeks_provenance SPX --interval 1.0 --start-date 2024-08-01 --end-date 2024-08-05 --out C:\temp\prov.parquet

# Force export format regardless of suffix
python -m src.cli greeks_provenance SPX --interval 1.0 --start-date 2024-08-01 --end-date 2024-08-05 --out C:\temp\prov.out --format parquet
```

Output includes:


### Compact Provenance files into a dataset
Combine many partitioned provenance files into one compact dataset for analysis/dashboards.

```powershell
# Default compaction across all partitions -> data/derived/provenance/greeks_source_compact/prov_compact_all.parquet
python -m src.cli greeks_provenance_compact

# Windowed compaction with explicit parquet output
python -m src.cli greeks_provenance_compact --start-date 2025-09-01 --end-date 2025-09-28 --out data/derived/provenance/greeks_source_compact/prov_compact_sep.parquet

# CSV output with format override
python -m src.cli greeks_provenance_compact --start-date 2025-09-21 --end-date 2025-09-28 --out C:\temp\prov_compact.csv --format csv

# Grouped summary by symbol/interval/source with separate CSV
python -m src.cli greeks_provenance_compact --start-date 2025-09-01 --end-date 2025-09-28 `
  --out data/derived/provenance/greeks_source_compact/prov_compact_sep.parquet `
  --group-by symbol,interval,greeks_source_used `
  --grouped-out C:\temp\prov_grouped.csv --grouped-format csv
```

Deduplication policy per (symbol, interval, date):
- bars: max
- persisted_available: any(True)
- greeks_records: max
- greeks_source_used: mode with priority persisted > compute > proxy > none

The compaction command also returns summary stats in its JSON output:
- `days_by_source`: counts of days per chosen source
- `bars_by_source`: total bars per source across the compacted window
- `persisted_days`: number of days where persisted greeks existed
- `greeks_records_total`: total persisted greeks records aggregated

### Register daily provenance summary task (Windows)
Extend the scheduler to also run a daily provenance summary over the last N days.

```powershell
# Register snapshot task as usual, and also a provenance summary at 11:59 PM for the last 7 days
.\scripts\register_greeks_task.ps1 -Price 5250 -Times "09:30,12:00,15:45" -Mode composite -MaxRuns 1 -RegisterProvenanceTask -ProvTime 23:59

# Customize provenance options
.\scripts\register_greeks_task.ps1 -RegisterProvenanceTask -ProvSymbol SPX -ProvInterval 1.0 -ProvDays 14 -ProvTime 23:15 -ProvenanceTaskName ZeroDTE_GreeksProv14d
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
| IQFEED_PORT_LOOKUP | Dedicated lookup/chain/greeks port (if different) |
| OPENAI_API_KEY  | OpenAI summarization |
| LOG_LEVEL       | Logging verbosity |
| DATA_DIR        | Base data directory (default `data`) |
| LOG_DIR         | Log directory (default `logs`) |

## Tests
```powershell
pytest -q
```
The backtest engine includes a unit test verifying size-aware behavior (gross/net PnL and costs scale with `size_update`).

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
  - SPX skew metric via delta buckets (±25Δ / ±50Δ) -> normalized `skew_score` and raw spreads (live-first, sim fallback)
    - Outputs include `skew_provenance` and top-level `price_provenance` for easy filtering
- Normalization helper converts Polygon aggregate keys to readable column names
- Ingestion writes parquet to `data/raw/polygon/prev_spx/date=YYYY-MM-DD/part.parquet`

  ### Logging
  - UTC timestamps in JSONL logs are timezone-aware (ISO8601 with Z) to avoid deprecation warnings.

## Next Ideas
- Live-only enforcement flag for greeks/skew commands
- Persist greeks surface snapshots & IV term structure (parquet partitioned by timestamp)
- Robust IQFeed reconnection / heartbeat & metrics
- Vectorized greeks surface generation for faster bucket scans
- Configurable skew bucket set & scaling factor
- Chain caching layer (reduce duplicate lookups intraday)
- Exchange timestamp usage instead of wall-clock for bar alignment
- Enhanced PnL attribution & risk metrics in backtest engine
- Level2 (order book) stream ingestion & aggregation
python -m src.cli market_stream --symbols @SPX.X --synthetic --ticks 50 --indicators --seasonality --regime \
  --persist-signals --strategy odte_direction  # include indicators + regime + parquet signal persistence
python -m src.cli market_summary --skew  # include live-or-fallback skew metrics in summary
- Strategy parameter grid search + result persistence
- Metrics dashboard / visualization layer (e.g., Panel, Dash, or Streamlit)
- Additional data quality & gap detection utilities
- Live portfolio/risk dashboard (e.g., FastAPI + Web UI)

## Troubleshooting
| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Connection refused / timeout on iqfeed commands | iqconnect not running or firewall blocking | Start IQFeed desktop; allow through firewall; verify host/port env vars |
| Empty greeks / immediate fallback logged | Options entitlement missing OR fundamentals delay | Confirm entitlement with DTN; retry after ensuring login is stable |
| High latency on chain request | Network / overloaded local machine | Reduce limit size; ensure no antivirus interference |
| IV solve fails to converge | Mid price inconsistent with model / extreme moneyness | Provide better initial guess (future flag) or verify price integrity |

Enable debug logging:
```powershell
$Env:LOG_LEVEL = "DEBUG"; python -m src.cli iqfeed_greeks_live SPX --limit 8
```

Review log file at `logs/pipeline.log` for detailed stack traces, fallback reasons, and socket message diagnostics.

## Daily pre-market automation (Windows)
You can launch a live session automatically each day and summarize results with the provided PowerShell scripts:

1) Manual one-off start
- `scripts/start_live_session.ps1` wraps the `live_bars_test` command, builds a timestamped stem, and runs the post-run watcher.
- Example (PowerShell):
  - .\scripts\start_live_session.ps1 -Symbol SPY -IntervalSec 10 -Minutes 65 -Device auto -UseHybrid -RunWatcher

2) Register a scheduled task
- `scripts/register_live_task.ps1` creates a Windows Task Scheduler job that runs `start_live_session.ps1` daily (or weekdays only).
- Examples:
  - .\scripts\register_live_task.ps1 -Time "09:25" -Symbol SPY -IntervalSec 10 -Minutes 65 -Device auto -Weekdays
  - .\scripts\register_live_task.ps1 -TaskName ZeroDTE_Live_SPX -Time "09:20" -Symbol SPX -IntervalSec 5 -Minutes 90 -UseHybrid:$true -UseEnsemble:$false -StemPrefix premarket

Notes
- The task runs with highest privileges and uses the repository root as the working directory.
- GPU selection: `-Device auto` will prefer CUDA when available and fall back to DirectML (`dml`) on Windows, else CPU.
- After the run completes, the watcher writes `<stem>_summary.json` and triggers interpretability/causal probes when possible.
- Manage tasks with Task Scheduler (taskschd.msc), or via PowerShell:
  - Get-ScheduledTask -TaskName "ZeroDTE_LiveSession"
  - Unregister-ScheduledTask -TaskName "ZeroDTE_LiveSession" -Confirm:$false

## IV Surface metrics CLI

Compute ATM IV, 25d risk reversal, butterfly, skew slope, and expected move from a quotes file.

Expected columns (parquet or JSON):
- right: 'C' or 'P'
- strike: float
- iv: implied volatility (annualized, e.g., 0.20)
- delta: signed option delta (calls positive, puts negative)
- ttm: time to maturity in years (optional; nearest tenor is selected if multiple exist)

Example (PowerShell):

```powershell
python -m src.cli iv_surface_metrics `
  --quotes-file data/derived/greeks_live/date=2025-09-26/part-0001.parquet `
  --target-delta 0.25 `
  --target-ttm 0.0192 `
  --spot 100 `
  --days 7
```

Outputs a JSON summary with ATM, RR, butterfly, slope, and expected move (pct/abs).

## License
(Choose a license and add a LICENSE file.)
