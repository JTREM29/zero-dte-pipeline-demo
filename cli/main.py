"""Main CLI entry point for Zero DTE Pipeline."""
import asyncio
import json
import sys
import os
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import click
import pytest

from agents.morning_brief_agent import analyze_brief_with_openai, build_brief
from zero_dte_pipeline.config import config
from zero_dte_pipeline.openai_client import OpenAIClient
from zero_dte_pipeline.reports import build_morning_report as build_simple_morning_report
from zero_dte_pipeline.reports.sentiment import build_agent_sentiments
from data.iqfeed_client import IQFEED_ENABLED
def _ensure_iqfeed_enabled() -> None:
    """Guard commands that require IQFeed connectivity."""
    if not IQFEED_ENABLED:
        raise click.ClickException(
            "IQFeed is disabled by configuration. Set IQFEED_ENABLED=1 or exclude IQFeed-specific commands."
        )



def run_async(coro):
    """Helper to run async functions."""
    return asyncio.run(coro)


def _generate_morning_report(
    timeout: int,
    symbols: Sequence[str],
    target_expiration: Optional[datetime] = None,
):
    """Produce a morning report result for reuse across commands."""
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.reports.morning_report import MorningReport

    async def run():
        async with UnifiedDataConnector() as connector:
            report_gen = MorningReport(
                connector,
                total_timeout=timeout,
                underlyings=list(symbols) if symbols else None,
                primary_expiration=target_expiration,
            )
            return await report_gen.generate()

    return run_async(run())


def _extract_nested(
    data: Mapping[str, Any],
    path: Iterable[str],
    default: Any = None,
) -> Any:
    """Safely extract nested dictionary fields."""
    current: Any = data
    for key in path:
        if isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            return default
    return current


def _find_section(report_dict: Mapping[str, Any], title: str) -> Dict[str, Any]:
    """Return the matching section payload from the serialized report."""
    for section in report_dict.get("sections", []):
        if section.get("title") == title:
            return section
    return {}


def _format_candidate_line(idx: int, candidate: Mapping[str, Any]) -> str:
    """Render a single approved candidate line."""
    symbol = candidate.get("symbol", "-")
    underlying = candidate.get("underlying", "-")
    strategy = candidate.get("strategy", "?")
    direction = candidate.get("direction", "?")
    score = candidate.get("score")
    confidence = candidate.get("confidence")
    strike = candidate.get("strike")
    expiration = candidate.get("expiration")
    parts = [
        f"  {idx}. {symbol} ({underlying})",
        f"strategy={strategy}",
        f"direction={direction}",
    ]
    if strike is not None:
        parts.append(f"strike={strike}")
    if expiration:
        parts.append(f"exp={expiration}")
    if score is not None:
        parts.append(f"score={score:.2f}")
    if confidence is not None:
        parts.append(f"conf={confidence:.2f}")
    return " | ".join(parts)


def _format_morning_brief(
    report_result,
    symbols: Sequence[str],
    top_n: int = 3,
    sentiments: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """Generate a condensed textual morning brief from the report payload."""
    report_dict = report_result.to_dict() if hasattr(report_result, "to_dict") else report_result
    summary = report_dict.get("summary", {})
    market_profile = report_dict.get("market_profile") or {}
    approved = report_dict.get("approved_candidates", []) or []
    timestamp = report_dict.get("timestamp", "unknown time")
    status = report_dict.get("status", "unknown").upper()
    target = ", ".join(symbols) if symbols else "SPX"

    lines = [
        f"Morning Brief | {target} | {timestamp}",
        f"Status={status} | Market={market_profile.get('condition', 'unknown')} "
        f"({market_profile.get('regime', 'unknown')}, {market_profile.get('direction_bias', 'neutral')})",
    ]

    vol = market_profile.get("volatility_percentile")
    confidence = market_profile.get("confidence")
    if vol is not None or confidence is not None:
        lines.append(
            f"Volatility Percentile={vol if vol is not None else '--'} | "
            f"Confidence={confidence if confidence is not None else '--'}"
        )

    strategies = market_profile.get("recommended_strategies") or summary.get("recommended_strategies") or []
    if strategies:
        lines.append(f"Preferred Strategies: {', '.join(strategies)}")

    generated = summary.get("candidates_generated", report_dict.get("candidates_count", 0))
    approved_count = summary.get("candidates_approved", report_dict.get("approved_count", len(approved)))
    lines.append(f"Candidates: {generated} generated / {approved_count} approved")

    connectivity = _find_section(report_dict, "Data Connectivity").get("data", {})
    providers = connectivity.get("connected_providers")
    if providers:
        lines.append(f"Data Sources: {', '.join(providers)}")

    if approved:
        lines.append(f"Top {min(top_n, len(approved))} approvals:")
        for idx, candidate in enumerate(approved[:top_n], 1):
            lines.append(_format_candidate_line(idx, candidate))
    else:
        gating_metrics = _extract_nested(
            _find_section(report_dict, "Candidate Approval"),
            ["data", "gating_metrics"],
            default={},
        )
        approval_rate = gating_metrics.get("approval_rate")
        note = "No approved candidates - gating constraints remain in effect."
        if approval_rate is not None:
            note += f" (approval_rate={approval_rate:.2%})"
        lines.append(note)

    if sentiments:
        lines.append("")
        lines.append("Agent Sentiment")
        for entry in sentiments:
            lines.append(entry["summary"])

    error_note = summary.get("error")
    if error_note:
        lines.append(f"Warnings: {error_note}")

    duration = summary.get("duration_ms")
    if duration is not None:
        lines.append(f"Report duration: {duration:.0f} ms")

    return "\n".join(lines)


@click.group()
@click.option("--debug/--no-debug", default=False, help="Enable debug output")
@click.option("--json-output/--no-json-output", default=False, help="Output in JSON format")
@click.pass_context
def main(ctx, debug, json_output):
    """Zero DTE Pipeline CLI - 0DTE Options Trading Tools."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    ctx.obj["json_output"] = json_output


@main.command()
@click.pass_context
def test_connectivity(ctx):
    """Test connectivity to all configured data sources."""
    _ensure_iqfeed_enabled()
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    
    async def run():
        connector = UnifiedDataConnector()
        results = await connector.test_all_connections()
        await connector.disconnect()
        return results
    
    click.echo("Testing data source connectivity...")
    results = run_async(run())
    
    if ctx.obj["json_output"]:
        click.echo(json.dumps(results, indent=2))
    else:
        click.echo(f"\nTimestamp: {results['timestamp']}")
        click.echo(f"Summary: {results['summary']['connected']}/{results['summary']['total']} sources connected\n")
        
        for provider, data in results["providers"].items():
            status = "✓" if data.get("connected") else "✗"
            click.echo(f"  {status} {provider}")
            if data.get("latency_ms"):
                click.echo(f"    Latency: {data['latency_ms']:.1f}ms")
            if data.get("error"):
                click.echo(f"    Error: {data['error']}")


@main.command(name="morning-report")
@click.option("--symbol", required=True, help="Symbol to analyze")
@click.option(
    "--post-to-discord/--no-post-to-discord",
    default=False,
    help="Post the output to the configured Discord webhook",
)
@click.option(
    "--target-expiry",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    help="Override the 0DTE expiration date (YYYY-MM-DD)",
)
@click.pass_context
def morning_report(ctx, symbol, post_to_discord, target_expiry):
    """Full Morning Report (snapshot, regime, headlines, and candidates)."""
    _ensure_iqfeed_enabled()
    click.echo(f"Generating morning report for: {symbol}...")
    as_json = ctx.obj.get("json_output", False)

    target_expiration = None
    if target_expiry:
        target_expiration = target_expiry.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    try:
        result = build_simple_morning_report(
            symbol,
            as_dict=as_json,
            post_to_discord=post_to_discord,
            primary_expiration=target_expiration,
        )

        if as_json:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            click.echo(result)

    except Exception as exc:
        click.echo(f"Error generating morning report: {exc}", err=True)
        sys.exit(1)


@main.command(name="morning-report-simple")
@click.option("--symbol", required=True, help="Symbol to analyze")
@click.option("--post-discord", is_flag=True, default=False, help="Post TL;DR to Discord")
def morning_report_simple_cmd(symbol: str, post_discord: bool):
    """Short-form morning report that emits the TL;DR summary."""
    _ensure_iqfeed_enabled()
    try:
        payload = build_simple_morning_report(
            symbol,
            as_dict=True,
            post_to_discord=post_discord,
        )

        summary = payload.get("tldr") or payload.get("text") or ""
        click.echo(summary)

    except Exception as exc:
        click.echo(f"Error generating short morning report: {exc}", err=True)
        sys.exit(1)


@main.command(name="morning-brief-core")
@click.option("--timeout", default=120, help="Total timeout in seconds")
@click.option(
    "--symbol",
    "symbols",
    multiple=True,
    default=("QQQ", "IWM", "SPY", "SPX"),
    help="Underlying symbol(s) to include (repeatable)",
)
@click.option("--top", "top_n", default=3, show_default=True, help="Number of approved candidates to list")
@click.pass_context
def morning_brief(ctx, timeout, symbols, top_n):
    """Render condensed morning brief text built on the morning report output."""
    _ensure_iqfeed_enabled()
    targets = ", ".join(symbols) if symbols else "default basket"
    click.echo(f"Generating morning brief for: {targets}...")

    try:
        result = _generate_morning_report(timeout, symbols)
        sentiments = build_agent_sentiments(result, symbols)
        brief_text = _format_morning_brief(result, symbols, top_n=top_n, sentiments=sentiments)

        if ctx.obj["json_output"]:
            payload = {
                "brief": brief_text,
                "timestamp": result.timestamp.isoformat(),
                "status": result.status,
                "market_profile": result.market_profile.to_dict() if result.market_profile else None,
                "summary": result.summary,
                "approved_candidates": [c.to_dict() for c in result.approved_candidates],
                "agent_sentiment": sentiments,
            }
            click.echo(json.dumps(payload, indent=2, default=str))
        else:
            click.echo(brief_text)

    except Exception as e:
        click.echo(f"Error generating morning brief: {e}", err=True)
        sys.exit(1)



@main.command()
@click.option("--symbol", default="SPY", help="Symbol to profile")
@click.pass_context
def market_profile(ctx, symbol):
    """Analyze current market conditions."""
    _ensure_iqfeed_enabled()
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.profiler.market_profiler import MarketProfiler
    
    async def run():
        async with UnifiedDataConnector() as connector:
            profiler = MarketProfiler(connector)
            return await profiler.analyze()
    
    click.echo("Analyzing market conditions...")
    
    try:
        profile = run_async(run())
        
        if ctx.obj["json_output"]:
            click.echo(json.dumps(profile.to_dict(), indent=2))
        else:
            click.echo(f"\n=== Market Profile ===")
            click.echo(f"Condition: {profile.condition.value}")
            click.echo(f"Direction Bias: {profile.direction_bias.value}")
            click.echo(f"Regime: {profile.regime.value}")
            click.echo(f"Volatility Percentile: {profile.volatility_percentile:.1f}")
            click.echo(f"Momentum Score: {profile.momentum_score:.2f}")
            click.echo(f"Breadth Score: {profile.breadth_score:.2f}")
            click.echo(f"Confidence: {profile.confidence:.2f}")
            click.echo(f"\nRecommended Strategies:")
            for s in profile.recommended_strategies:
                click.echo(f"  - {s.value}")
                
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--underlying", multiple=True, default=["SPY", "QQQ", "IWM"], help="Underlyings to capture")
@click.option("--format", "save_format", default="parquet", type=click.Choice(["parquet", "csv"]))
@click.pass_context
def capture_chains(ctx, underlying, save_format):
    """Download and store options chain data."""
    _ensure_iqfeed_enabled()
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.historical.capture import HistoricalCapture
    from pathlib import Path
    
    async def run():
        async with UnifiedDataConnector() as connector:
            capture = HistoricalCapture(connector, Path("data/historical"))
            return await capture.capture_all_chains(list(underlying), save_format=save_format)
    
    click.echo(f"Capturing chains for: {', '.join(underlying)}")
    
    try:
        results = run_async(run())
        
        if ctx.obj["json_output"]:
            output = {k: [str(p) for p in v] for k, v in results.items()}
            click.echo(json.dumps(output, indent=2))
        else:
            for ul, paths in results.items():
                click.echo(f"\n{ul}:")
                for p in paths:
                    click.echo(f"  ✓ {p}")
                    
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--days", default=30, help="Days of sample data to generate")
@click.pass_context
def generate_sample_data(ctx, days):
    """Generate sample historical data for testing."""
    _ensure_iqfeed_enabled()
    from zero_dte_pipeline.historical.replay import HistoricalReplay
    from pathlib import Path
    
    click.echo(f"Generating {days} days of sample data...")
    
    replay = HistoricalReplay(Path("data/historical"))
    replay.generate_sample_data(days=days)
    
    click.echo("Sample data generated successfully.")


@main.command()
@click.option("--trades", default=100, help="Number of simulated trades")
@click.option("--aggressive/--no-aggressive", default=False, help="Use aggressive optimization")
@click.option("--config-path", default=None, help="Config file to update")
@click.pass_context
def autotune(ctx, trades, aggressive, config_path):
    """Run autotune optimization on simulated or historical trades."""
    _ensure_iqfeed_enabled()
    from zero_dte_pipeline.autotune.autotune import CandidateAutotune
    from pathlib import Path
    
    click.echo(f"Running autotune with {trades} simulated trades...")
    click.echo(f"Mode: {'Aggressive' if aggressive else 'Conservative'}")
    
    config_file = Path(config_path) if config_path else None
    tuner = CandidateAutotune(config_path=config_file, aggressive=aggressive)
    
    # Generate simulated trades
    trade_history = tuner.simulate_trades(num_trades=trades)
    
    # Run optimization
    result = tuner.optimize(trade_history, max_iterations=100)
    
    if ctx.obj["json_output"]:
        click.echo(json.dumps(result.to_dict(), indent=2))
    else:
        click.echo(f"\n=== Autotune Results ===")
        click.echo(f"Iterations: {result.iterations}")
        click.echo(f"Best Score: {result.best_score:.4f}")
        click.echo(f"Improvement: {result.improvement:.2%}")
        click.echo(f"Config Updated: {result.config_updated}")
        
        click.echo(f"\nMetrics:")
        for k, v in result.metrics.items():
            click.echo(f"  {k}: {v}")
        
        click.echo(f"\nBest Parameters:")
        click.echo(json.dumps(result.best_params, indent=2))


@main.command()
@click.pass_context
def show_config(ctx):
    """Show current configuration."""
    _ensure_iqfeed_enabled()
    config_dict = config.to_dict(include_secrets=True)
    
    if ctx.obj["json_output"]:
        click.echo(json.dumps(config_dict, indent=2))
    else:
        click.echo("=== Current Configuration ===\n")
        for key, value in config_dict.items():
            click.echo(f"{key}: {value}")


@main.command()
@click.option("--underlying", default="SPY", help="Underlying to analyze")
@click.pass_context
def get_candidates(ctx, underlying):
    """Generate and display trading candidates."""
    _ensure_iqfeed_enabled()
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.candidates.generator import CandidateGenerator
    from zero_dte_pipeline.candidates.scoring import (
        CandidateScorer, Direction, Regime, SignalAlignment
    )
    from zero_dte_pipeline.candidates.gating import GatingManager
    
    async def run():
        async with UnifiedDataConnector() as connector:
            # Create default signals
            signals = SignalAlignment(
                direction=Direction.NEUTRAL,
                direction_confidence=0.5,
                regime=Regime.RANGING,
                regime_confidence=0.5,
                iv_signal=0.0,
                order_flow_signal=0.0,
            )

            generator = CandidateGenerator(connector)
            candidates = await generator.generate_candidates(underlying, signals)

            # Score candidates
            scorer = CandidateScorer()
            scored = scorer.score_candidates(candidates, signals)

            # Gate candidates
            gating = GatingManager()
            results = gating.filter_candidates(scored, max_approved=10)

            return scored, results, scorer.get_metrics(), gating.get_metrics()
    
    click.echo(f"Generating candidates for {underlying}...")
    
    try:
        scored, approved, scoring_metrics, gating_metrics = run_async(run())
        
        if ctx.obj["json_output"]:
            output = {
                "total_candidates": len(scored),
                "approved_candidates": len(approved),
                "candidates": [c.to_dict() for c in scored[:20]],
                "scoring_metrics": scoring_metrics,
                "gating_metrics": gating_metrics,
            }
            click.echo(json.dumps(output, indent=2, default=str))
        else:
            click.echo(f"\nGenerated: {len(scored)} candidates")
            click.echo(f"Approved: {len(approved)} candidates")
            
            click.echo(f"\nScoring Metrics:")
            click.echo(f"  Pass Rate: {scoring_metrics['pass_rate']:.2%}")
            click.echo(f"  Regime Unknown: {scoring_metrics['regime_unknown_count']}")
            
            click.echo(f"\nGating Metrics:")
            click.echo(f"  Approval Rate: {gating_metrics['approval_rate']:.2%}")
            
            if approved:
                click.echo(f"\nTop Approved:")
                for i, r in enumerate(approved[:5], 1):
                    c = r.candidate
                    click.echo(f"  {i}. {c.symbol}")
                    click.echo(f"     Strategy: {c.strategy.value}, Direction: {c.direction.value}")
                    click.echo(f"     Score: {c.score:.2f}, Confidence: {c.confidence:.2f}")
                    
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command(name="last-ticks")
@click.option(
    "--symbol",
    "symbols",
    multiple=True,
    default=("I:SPX", "I:VIX"),
    show_default=True,
    help="Symbol(s) to show from SQLite last_ticks (repeatable)",
)
@click.option("--db-path", default=None, help="Override SQLite path (defaults to DB_PATH or db/tnt.db)")
@click.pass_context
def last_ticks_cmd(ctx, symbols, db_path):
    """Show latest WebSocket/REST tick cache rows from SQLite.

    This is a quick smoke check that the Polygon WS collector is writing into `last_ticks`.
    """

    import sqlite3

    syms = [str(s).strip().upper() for s in (symbols or ()) if str(s or "").strip()]
    if not syms:
        syms = ["I:SPX", "I:VIX"]

    path = db_path or os.getenv("DB_PATH", "db/tnt.db")

    with sqlite3.connect(path, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS last_ticks (
                symbol TEXT PRIMARY KEY,
                price REAL,
                ts TEXT,
                source TEXT,
                recv_ts TEXT
            )
            """
        )

        placeholders = ",".join(["?"] * len(syms))
        rows = cur.execute(
            f"SELECT symbol, price, ts, source, recv_ts FROM last_ticks WHERE symbol IN ({placeholders}) ORDER BY symbol",
            tuple(syms),
        ).fetchall()

    payload = {
        "db_path": path,
        "symbols": syms,
        "rows": [
            {"symbol": r[0], "price": r[1], "ts": r[2], "source": r[3], "recv_ts": r[4]}
            for r in (rows or [])
        ],
    }

    if ctx.obj.get("json_output"):
        click.echo(json.dumps(payload, indent=2, default=str))
        return

    click.echo(f"DB: {path}")
    if not payload["rows"]:
        click.echo("No last_ticks rows found for requested symbols.")
        click.echo("Hint: run the task 'Polygon WS collector: indices (bg)' and wait a few seconds.")
        return

    for r in payload["rows"]:
        click.echo(f"{r['symbol']}: price={r['price']} ts={r['ts']} source={r['source']} recv_ts={r['recv_ts']}")


@click.command("morning-brief")
@click.option(
    "--symbol",
    "symbols",
    multiple=True,
    default=(),
    help="Symbol(s) to analyze (repeatable)",
)
@click.option("--json-output/--no-json-output", default=False, show_default=True)
@click.option("--no-openai", is_flag=True, default=False, help="Skip OpenAI analysis")
def morning_brief_cmd(symbols, json_output: bool, no_openai: bool) -> None:
    """Generate a technical morning brief backed by Massive and IBKR data."""
    requested = [s.upper() for s in symbols] if symbols else None
    brief = asyncio.run(build_brief(symbols=requested))

    if no_openai:
        payload = brief
    else:
        payload = asyncio.run(analyze_brief_with_openai(brief))

    if json_output:
        click.echo(json.dumps(payload, indent=2, default=str))
    elif no_openai:
        click.echo(json.dumps(payload, indent=2, default=str))
    else:
        click.echo(payload["agent_analysis"])


main.add_command(morning_brief_cmd)


@main.command()
@click.option("--symbol", default="SPX", help="Symbol to forecast")
@click.option("--horizon-minutes", default=60, show_default=True, help="Forecast horizon in minutes")
@click.option("--lookback-minutes", default=360, show_default=True, help="Historical window for analysis")
@click.pass_context
def forecast(ctx, symbol, horizon_minutes, lookback_minutes):
    """Generate a short-horizon directional forecast."""
    _ensure_iqfeed_enabled()
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.forecast.forecaster import Forecaster

    async def run():
        async with UnifiedDataConnector() as connector:
            forecaster = Forecaster(
                connector,
                horizon_minutes=horizon_minutes,
                lookback_minutes=lookback_minutes,
            )
            return await forecaster.forecast(symbol)

    click.echo(f"Generating {horizon_minutes}m forecast for {symbol}...")

    try:
        result = run_async(run())

        if ctx.obj["json_output"]:
            click.echo(json.dumps(result.to_dict(), indent=2, default=str))
        else:
            click.echo(f"\nDirection: {result.direction.value}")
            click.echo(f"Score: {result.score:.2f} ({result.confidence.value} confidence)")
            click.echo(f"Expected Move: {result.expected_move_pct:.2f}%")
            click.echo("\nComponents:")
            for comp in result.components:
                click.echo(f"  - {comp.name}: {comp.score:.2f} × {comp.weight:.2f} ({comp.rationale})")
            click.echo("\nLevels:")
            for level, value in result.technical_levels.items():
                click.echo(f"  {level}: {value:.2f}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

@pytest.mark.integration
def test_openai_model_available():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY not set")
    wrapper = OpenAIClient(api_key=key)
    from delivery.state_builder import build_tnt_state

    tnt_state = build_tnt_state(["SPY"], mode="ON_DEMAND")
    out = wrapper.complete(system="Say OK.", prompt="test", tnt_state=tnt_state)
    assert "OK" in out

if __name__ == "__main__":
    main()
