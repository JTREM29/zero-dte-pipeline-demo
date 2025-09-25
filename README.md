# ZeroDTE-pipeline

A structured Python pipeline for researching, ingesting, and modeling zero-day (0DTE) options data.

## Goals
- Ingest intraday options + underlying market data (e.g., SPX/SPY) from providers (IQFeed, Polygon, etc.)
- Normalize and store raw + enriched datasets
- Feature engineering for strategy research and ML modeling
- Support experimentation with latency-sensitive signals and execution logic

## Initial Layout (subject to evolution)
```
ZeroDTE-pipeline/
  .venv/                # Local virtual environment (ignored)
  main.py               # Entry point / quick orchestrator
  requirements.txt      # Python dependencies
  src/                  # (To be added) package code
  data/                 # (You create) raw/processed datasets (gitignored later if needed)
  notebooks/            # (Optional) exploratory analysis
```

## Getting Started
1. Create / activate the virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Run the entry point:
   ```powershell
   python main.py
   ```

## Next Steps
- Add `src/zero_dte/` package with data fetching & transformations
- Introduce configuration pattern (pydantic or dynaconf)
- Add logging and basic CLI arguments (argparse or Typer)
- Implement data provider abstractions
- Add tests (pytest) and CI workflow

## License
(Choose a license and add a LICENSE file.)
