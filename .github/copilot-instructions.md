# ZeroDTE Pipeline Copilot Instructions

## Overview
This is a live 0DTE options trading pipeline integrating multiple market data sources. It processes market data into trading signals via ML models and agent decisions.

## Architecture
- **Data Ingestion**: `src/datafeeds/` handles raw data from APIs (ticks, bars, options chains).
- **Feature Engineering**: `src/features/` and `src/greeks/` compute derived metrics, hybrid bars (time+volume), IV surfaces.
- **Modeling**: Autogluon ensemble models in `models/AutogluonModels/` for predictions.
- **Agent**: `src/agent/` makes trading decisions based on signals, risk gates.
- **Execution**: `src/execution/` handles order placement, slippage simulation.
- **Observability**: `src/observability/` with latency probes, anomaly scans, circuit breakers.
- **CLI**: `src/cli/` for commands like backtesting, live signals, reports.
- **Bots**: Root-level scripts (`spxw_alert_bot.py`) for Discord alerts.

## Data Flow
Raw data (`data/raw/`) → Derived features (`data/derived/`) → Signals (`data/signals/`) → Agent decisions → Logs (`logs/agent_alerts_SYMBOL_DATE.jsonl`, `agent_backtest_DATE.json`)

## Key Patterns
- **Hybrid Bars**: Combine time and volume bars for better signals.
- **Stress Testing**: Simulate market stress via `stress_hybrid_bars` command.
- **Provenance Audit**: Track data lineage with `provenance_audit`.
- **Ensemble Evaluation**: Ensure model diversity with `ensemble_eval`.
- **Circuit Breakers**: Risk controls via `circuit_breaker_status`.

## Workflows
- **Testing**: `pytest -q` for all tests; specific: `pytest tests/test_cli_backtest_smoke_realism.py`
- **Live Bars**: `python -m src.cli live_bars_test --symbol SPY --interval 1.0 --minutes 10`
- **Stress Run**: `python -m src.cli stress_hybrid_bars logs/live_SPY_10s_... --symbol SPY --window 64`
- **Morning Report**: `python -m src.cli morning_report` (see `docs/morning_report_schema.md`)
- **Backtest**: Compare live vs historical with `strategy_compare`

## Conventions
- **Configs**: JSON in `config/` (e.g., `0dte_candidate_config.json` with params like `quality_threshold`, `regime_whitelist`)
- **Logs**: JSONL for alerts (`agent_alerts_SPX_2025-10-01.jsonl`), JSON for backtests
- **Data Storage**: Parquet for compact metrics, CSV for raw logs
- **File Naming**: `agent_backtest_START_END.json`, `live_SYMBOL_INTERVAL_DATE_bars.csv`
- **CLI Args**: `--symbol SPY`, `--window 16`, `--horizon 1`, `--ckpt models/hybrid_bars.ckpt`

## Dependencies
- APIs: Market data sources (with watchdogs where applicable)
- ML: Autogluon for ensemble models
- Bots: Discord integration for alerts
- Environment: Python with packages in requirements (assume standard ML stack)

## Notes
- Focus on risk management: circuit breakers, slippage sim, drawdown limits
- Test thoroughly: many CLI tests for realism and safety
- Unclear: Exact slippage parameters; need to check `slippage_sim` implementation for calibration details.