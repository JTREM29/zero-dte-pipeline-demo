"""Typer CLI entrypoints.
"""
from __future__ import annotations
import typer
import json
from typing import List
from pathlib import Path
from .config import Settings
from .utils.logging_setup import get_logger
from .datafeeds.polygon_client import PolygonClient, PolygonConfig
from .datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig, IQFeedLevel1Stream
from .strategies.simple_intraday_spx import SimpleIntradaySPXStrategy
from .openai_client import OpenAIWrapper
from .ingestion.polygon_ingestor import PolygonIngestor
from .ingestion.iqfeed_level1_writer import Level1BatchWriter, BatchConfig
from .aggregation.bar_builder import TimeBarAggregator
import pandas as pd

app = typer.Typer(help="Zero DTE research & execution pipeline CLI")
LOGGER = get_logger("zero_dte.cli")


@app.command()
def snapshot(symbol: str = typer.Argument("SPX", help="Underlying symbol")):
    """Fetch a simple underlying snapshot via Polygon (placeholder)."""
    settings = Settings()
    if not settings.has_polygon:
        typer.echo("Polygon API key missing; export POLYGON_API_KEY or set in .env")
        raise typer.Exit(code=1)
    cfg = PolygonConfig.from_env()
    client = PolygonClient(cfg)
    snap = client.fetch_underlying_snapshot(symbol)
    typer.echo(json.dumps(snap, indent=2))


@app.command()
def demo_strategy(symbol: str = "SPX"):
    """Run the simple strategy and optionally summarize signals with OpenAI."""
    settings = Settings()
    poly_client = None
    if settings.has_polygon:
        try:
            poly_client = PolygonClient(PolygonConfig.from_env())
            snap = poly_client.fetch_underlying_snapshot(symbol)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Polygon fetch failed: %s", exc)
            snap = {"symbol": symbol, "lastPrice": 0.0}
    else:
        snap = {"symbol": symbol, "lastPrice": 0.0}

    strat = SimpleIntradaySPXStrategy()
    signals = [s.to_dict() for s in strat.evaluate(snap)]

    if settings.has_openai:
        summarizer = OpenAIWrapper(settings.openai_api_key)
        summary = summarizer.summarize_signals(signals)
    else:
        summary = "(OpenAI disabled)"

    typer.echo(json.dumps({"snapshot": snap, "signals": signals, "summary": summary}, indent=2))


@app.command()
def iqfeed_chain(root: str = "SPX"):
    """Demonstrate IQFeed chain fetch placeholder."""
    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)
    chain = client.fetch_demo_chain(root)
    typer.echo(json.dumps(chain, indent=2))


@app.command()
def ensure_dirs():
    """Ensure critical directories exist (data/, logs/)."""
    for d in [Path("data"), Path("logs")]:
        d.mkdir(parents=True, exist_ok=True)
    typer.echo("Directories ensured.")


@app.command()
def polygon_last_spx(normalize: bool = typer.Option(False, help="Return normalized DataFrame-like JSON"), cache: bool = True):
    """Fetch previous SPX aggregate via Polygon (with retry + optional cache)."""
    settings = Settings()
    if not settings.has_polygon:
        typer.echo("Polygon API key missing")
        raise typer.Exit(1)
    client = PolygonClient(PolygonConfig.from_env())
    data = client.last_trade_spx(use_cache=cache)
    if not data:
        typer.echo("{}")
        raise typer.Exit(1)
    if normalize:
        df = client.normalize_prev_agg(data)
        if df is None:
            typer.echo("{}")
        else:
            typer.echo(df.to_json(orient="records"))
    else:
        typer.echo(json.dumps(data, indent=2))


@app.command()
def iqfeed_ping():
    """Ping IQFeed Level1 socket."""
    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)
    ok = client.ping()
    typer.echo(json.dumps({"ok": ok}))


@app.command()
def ingest_prev_spx(normalize: bool = True, no_cache: bool = False):
    """Ingest previous SPX aggregate bar from Polygon into parquet (partitioned by date)."""
    settings = Settings()
    if not settings.has_polygon:
        typer.echo("Polygon API key missing")
        raise typer.Exit(1)
    ing = PolygonIngestor(settings.data_dir or "data")  # type: ignore[arg-type]
    res = ing.ingest_prev_spx(normalize=normalize, use_cache=not no_cache)
    typer.echo(json.dumps({
        "symbol": res.symbol,
        "rows": res.rows,
        "path": str(res.path) if res.path else None,
        "normalized": res.normalized,
        "ts": res.ts,
        "cached": res.cached,
    }, indent=2))


@app.command()
def iqfeed_stream(symbols: str = typer.Argument("SPX", help="Comma-separated symbols to watch"), samples: int = 5, timeout: float = 10.0):
    """Stream real-time Level1 quotes from a running IQFeed client (requires local IQConnect)."""
    cfg = IQFeedConfig.from_env()
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    collected: list[dict] = []

    def _on_msg(msg: dict):  # noqa: D401
        if msg.get("symbol") in syms:
            collected.append(msg)

    stream = IQFeedLevel1Stream(cfg, on_message=_on_msg)
    stream.start(syms)
    import time as _t
    start = _t.time()
    while len(collected) < samples and (_t.time() - start) < timeout:
        m = stream.get(timeout=0.5)
        if m and m.get("symbol") in syms:
            # ensure capture in addition to callback
            if m not in collected:
                collected.append(m)
    stream.stop()
    typer.echo(json.dumps({
        "symbols": syms,
        "received": len(collected),
        "messages": collected[:samples],
    }, indent=2))


@app.command()
def iqfeed_stream_persist(symbols: str = typer.Argument("SPX", help="Comma-separated symbols"), duration: float = 30.0, flush_secs: float = 2.0, max_rows: int = 3000):
    """Stream Level1 quotes and persist to parquet micro-batches for the given duration (seconds)."""
    settings = Settings()
    cfg = IQFeedConfig.from_env()
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    writer = Level1BatchWriter(BatchConfig(base_dir=Path(settings.data_dir or "data"), flush_secs=flush_secs, max_rows=max_rows))  # type: ignore[arg-type]

    def _on(msg: dict):  # noqa: D401
        if msg.get("symbol") in syms:
            writer.add(msg)

    stream = IQFeedLevel1Stream(cfg, on_message=_on)
    stream.start(syms)
    import time as _t
    start = _t.time()
    while (_t.time() - start) < duration:
        m = stream.get(timeout=0.5)
        if m:
            if m.get("symbol") in syms:
                writer.add(m)
    stream.stop()
    writer.flush()
    typer.echo(json.dumps({
        "symbols": syms,
        "status": "completed",
        "duration_sec": duration,
        "target_dir": str(Path(settings.data_dir or "data") / "raw" / "iqfeed" / "level1"),
    }, indent=2))


@app.command()
def build_bars(symbol: str, interval: float = 1.0, date: str = typer.Option(None, help="Date partition YYYY-MM-DD (defaults today)")):
    """Aggregate stored Level1 ticks (persisted parquet) into time bars."""
    settings = Settings()
    base = Path(settings.data_dir or "data") / "raw" / "iqfeed" / "level1"
    if date is None:
        date = time.strftime("%Y-%m-%d", time.gmtime())
    part_dir = base / f"date={date}"
    if not part_dir.exists():
        typer.echo(json.dumps({"error": f"partition {part_dir} missing"}))
        raise typer.Exit(1)
    files = sorted(part_dir.glob("*.parquet"))
    if not files:
        typer.echo(json.dumps({"error": "no parquet files"}))
        raise typer.Exit(1)
    agg = TimeBarAggregator(interval_sec=interval)
    import time as _t
    bars_out = []
    for f in files:
        df = pd.read_parquet(f)
        for rec in df.to_dict(orient="records"):
            if rec.get("symbol") != symbol:
                continue
            bars = agg.add_tick(rec)  # type: ignore[arg-type]
            if bars:
                bars_out.extend(bars)
    # Flush remainder
    bars_out.extend(agg.flush())
    payload = [
        {
            "symbol": b.symbol,
            "start_ts": b.start_ts,
            "end_ts": b.end_ts,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "trades": b.trades,
        }
        for b in bars_out
    ]
    typer.echo(json.dumps({"symbol": symbol, "interval": interval, "bars": payload}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
