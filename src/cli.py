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


if __name__ == "__main__":  # pragma: no cover
    app()
