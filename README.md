# Zero DTE Pipeline

A comprehensive 0DTE options trading pipeline integrating multiple data sources (IQFeed, Polygon.io, Alpha Vantage) with market analysis, candidate generation, and automated parameter optimization.

## Features

- **Multi-source Data Connectivity**: IQFeed, Polygon.io with automatic fallback
- **Market Condition Profiler**: Analyzes volatility, momentum, and breadth to select optimal strategies
- **Candidate Generation**: Generates trading candidates for SPX, SPY, QQQ, IWM
- **Intelligent Scoring**: Aligns direction, regime, IV signals, and order flow
- **Configurable Gating**: Risk management with adjustable strictness levels
- **Autotune Optimization**: Automated parameter tuning based on historical performance
- **Historical Data Capture**: Store and replay options chain data for backtesting
- **Timeout Protection**: All external calls wrapped with timeout handling

## Installation

```bash
# Clone the repository
git clone https://github.com/JTREM29/zero-dte-pipeline-demo.git
cd zero-dte-pipeline-demo

# Install dependencies
pip install -r requirements.txt

# Install the package
pip install -e .
```

## Configuration

Copy the example environment file and configure your API keys:

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```ini
# IQFeed Credentials
IQFEED_LOGIN=your_login
IQFEED_PASSWORD=your_password
IQFEED_PRODUCT_ID=your_product_id

# Polygon.io API
POLYGON_API_KEY=your_polygon_key

# Pipeline Settings
GATING_STRICTNESS=moderate  # loose, moderate, strict
DEFAULT_TIMEOUT_SECONDS=30
```

## CLI Usage

### Test Data Connectivity

```bash
# Test all configured data sources
zero-dte test-connectivity

# With JSON output
zero-dte --json-output test-connectivity
```

### Generate Morning Report

```bash
# Generate pre-market analysis report
zero-dte morning-report

# With custom timeout
zero-dte morning-report --timeout 180
```

### Market Condition Analysis

```bash
# Analyze current market conditions
zero-dte market-profile

# Analyze specific symbol
zero-dte market-profile --symbol QQQ
```

### Generate Candidates

```bash
# Generate trading candidates
zero-dte get-candidates --underlying SPY
```

### Capture Historical Data

```bash
# Capture options chains for backtesting
zero-dte capture-chains --underlying SPY --underlying QQQ

# Generate sample data for testing
zero-dte generate-sample-data --days 30
```

### Run Autotune

```bash
# Run parameter optimization
zero-dte autotune --trades 100

# Aggressive mode
zero-dte autotune --trades 200 --aggressive
```

### Show Configuration

```bash
zero-dte show-config
```

## Module Structure

```
zero_dte_pipeline/
├── config.py              # Configuration management
├── data_connectors/       # Data source connectors
│   ├── base.py           # Base connector class
│   ├── iqfeed.py         # IQFeed connector
│   ├── polygon.py        # Polygon.io connector
│   └── unified.py        # Unified connector with fallback
├── candidates/            # Candidate generation and scoring
│   ├── scoring.py        # Scoring logic
│   ├── generator.py      # Candidate generation
│   └── gating.py         # Risk gating
├── profiler/             # Market analysis
│   └── market_profiler.py
├── reports/              # Report generation
│   └── morning_report.py
├── autotune/             # Parameter optimization
│   └── autotune.py
├── historical/           # Historical data management
│   ├── capture.py        # Data capture
│   └── replay.py         # Backtesting replay
└── utils/                # Utilities
    ├── logging.py        # Structured logging
    └── timeout.py        # Timeout wrappers
```

## Key Improvements

### Data Connectivity
- Fixed IQFeed authentication loop with max retry limit
- Proper credential loading from environment and Windows registry
- Automatic fallback between data providers
- Unified test-connectivity CLI

### Morning Report Stability
- All external calls wrapped with timeout protection
- Safe fallback behavior when data is missing
- Partial results returned on timeout instead of hanging

### Candidate Generation
- Reduced hyper-strict gating (configurable strictness levels)
- Support for SPY, QQQ, IWM in addition to SPX
- Improved signal alignment scoring
- Strategy selection based on market conditions

### Observability
- JSON structured logging
- Metrics for rejections, regime unknowns, gate failures
- Debug output available for all CLI commands

## Example: Delta Bucket Bias

```python
import pandas as pd
from zero_dte_pipeline.candidates.scoring import delta_bucket_bias

# options_data expected to have columns: ['strike', 'delta', 'iv', 'type']
options_data = pd.DataFrame({
    'strike': [450, 450, 455, 455],
    'delta': [0.25, -0.25, 0.20, -0.30],
    'iv': [0.18, 0.22, 0.17, 0.21],
    'type': ['call', 'put', 'call', 'put']
})

bias, skew = delta_bucket_bias(options_data, target_delta=0.25)
print(f"Bias: {bias}, Skew: {skew}")
```

## Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=zero_dte_pipeline

# Run specific test file
pytest tests/test_scoring.py -v
```

## License

MIT License
