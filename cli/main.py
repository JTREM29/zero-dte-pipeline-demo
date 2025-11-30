"""Main CLI entry point for Zero DTE Pipeline."""
import asyncio
import json
import sys
from datetime import datetime

import click

from zero_dte_pipeline.config import config


def run_async(coro):
    """Helper to run async functions."""
    return asyncio.get_event_loop().run_until_complete(coro)


@click.group()
@click.option("--debug/--no-debug", default=False, help="Enable debug output")
@click.option("--json-output/--no-json-output", default=False, help="Output in JSON format")
@click.pass_context
def cli(ctx, debug, json_output):
    """Zero DTE Pipeline CLI - 0DTE Options Trading Tools."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    ctx.obj["json_output"] = json_output


@cli.command()
@click.pass_context
def test_connectivity(ctx):
    """Test connectivity to all configured data sources."""
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


@cli.command()
@click.option("--timeout", default=120, help="Total timeout in seconds")
@click.pass_context
def morning_report(ctx, timeout):
    """Generate the morning analysis report."""
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.reports.morning_report import MorningReport
    
    async def run():
        connector = UnifiedDataConnector()
        await connector.connect()
        
        report_gen = MorningReport(connector, total_timeout=timeout)
        result = await report_gen.generate()
        
        await connector.disconnect()
        return result
    
    click.echo("Generating morning report...")
    start = datetime.now()
    
    try:
        result = run_async(run())
        
        if ctx.obj["json_output"]:
            click.echo(json.dumps(result.to_dict(), indent=2, default=str))
        else:
            click.echo(f"\n=== Morning Report ===")
            click.echo(f"Status: {result.status}")
            click.echo(f"Generated: {result.timestamp}")
            
            if result.market_profile:
                mp = result.market_profile
                click.echo(f"\nMarket Condition: {mp.condition.value}")
                click.echo(f"Direction Bias: {mp.direction_bias.value}")
                click.echo(f"Regime: {mp.regime.value}")
                click.echo(f"Volatility Percentile: {mp.volatility_percentile:.1f}")
                click.echo(f"Recommended Strategies: {[s.value for s in mp.recommended_strategies]}")
            
            click.echo(f"\nCandidates Generated: {len(result.candidates)}")
            click.echo(f"Candidates Approved: {len(result.approved_candidates)}")
            
            if result.approved_candidates:
                click.echo("\nTop Approved Candidates:")
                for i, c in enumerate(result.approved_candidates[:5], 1):
                    click.echo(f"  {i}. {c.symbol} ({c.underlying}) - {c.strategy.value}")
                    click.echo(f"     Score: {c.score:.2f}, Confidence: {c.confidence:.2f}")
            
            click.echo(f"\nDuration: {result.summary.get('duration_ms', 0):.0f}ms")
            
    except Exception as e:
        click.echo(f"Error generating report: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--symbol", default="SPY", help="Symbol to profile")
@click.pass_context
def market_profile(ctx, symbol):
    """Analyze current market conditions."""
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.profiler.market_profiler import MarketProfiler
    
    async def run():
        connector = UnifiedDataConnector()
        await connector.connect()
        
        profiler = MarketProfiler(connector)
        profile = await profiler.analyze()
        
        await connector.disconnect()
        return profile
    
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


@cli.command()
@click.option("--underlying", multiple=True, default=["SPY", "QQQ", "IWM"], help="Underlyings to capture")
@click.option("--format", "save_format", default="parquet", type=click.Choice(["parquet", "csv"]))
@click.pass_context
def capture_chains(ctx, underlying, save_format):
    """Download and store options chain data."""
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.historical.capture import HistoricalCapture
    from pathlib import Path
    
    async def run():
        connector = UnifiedDataConnector()
        await connector.connect()
        
        capture = HistoricalCapture(connector, Path("data/historical"))
        results = await capture.capture_all_chains(list(underlying), save_format=save_format)
        
        await connector.disconnect()
        return results
    
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


@cli.command()
@click.option("--days", default=30, help="Days of sample data to generate")
@click.pass_context
def generate_sample_data(ctx, days):
    """Generate sample historical data for testing."""
    from zero_dte_pipeline.historical.replay import HistoricalReplay
    from pathlib import Path
    
    click.echo(f"Generating {days} days of sample data...")
    
    replay = HistoricalReplay(Path("data/historical"))
    replay.generate_sample_data(days=days)
    
    click.echo("Sample data generated successfully.")


@cli.command()
@click.option("--trades", default=100, help="Number of simulated trades")
@click.option("--aggressive/--no-aggressive", default=False, help="Use aggressive optimization")
@click.option("--config-path", default=None, help="Config file to update")
@click.pass_context
def autotune(ctx, trades, aggressive, config_path):
    """Run autotune optimization on simulated or historical trades."""
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


@cli.command()
@click.pass_context
def show_config(ctx):
    """Show current configuration."""
    config_dict = config.to_dict(include_secrets=True)
    
    if ctx.obj["json_output"]:
        click.echo(json.dumps(config_dict, indent=2))
    else:
        click.echo("=== Current Configuration ===\n")
        for key, value in config_dict.items():
            click.echo(f"{key}: {value}")


@cli.command()
@click.option("--underlying", default="SPY", help="Underlying to analyze")
@click.pass_context
def get_candidates(ctx, underlying):
    """Generate and display trading candidates."""
    from zero_dte_pipeline.data_connectors.unified import UnifiedDataConnector
    from zero_dte_pipeline.candidates.generator import CandidateGenerator
    from zero_dte_pipeline.candidates.scoring import (
        CandidateScorer, Direction, Regime, SignalAlignment
    )
    from zero_dte_pipeline.candidates.gating import GatingManager
    
    async def run():
        connector = UnifiedDataConnector()
        await connector.connect()
        
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
        
        await connector.disconnect()
        
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


if __name__ == "__main__":
    cli()
