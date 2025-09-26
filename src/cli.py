"""Typer CLI entrypoints.
"""
from __future__ import annotations
import typer
import json
from typing import List
from collections import deque
from pathlib import Path
from .config import Settings
from .utils.logging_setup import get_logger
from .datafeeds.polygon_client import PolygonClient, PolygonConfig
from .datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig, IQFeedLevel1Stream
from .strategies.simple_intraday_spx import SimpleIntradaySPXStrategy
from .strategies.odte_direction import ODTeDirectionStrategy
from .analytics.volatility import RollingVolatility
from .strategies.registry import get_strategy, list_strategies
import inspect
from .openai_client import OpenAIWrapper
from .ingestion.polygon_ingestor import PolygonIngestor
from .ingestion.iqfeed_level1_writer import Level1BatchWriter, BatchConfig
from .aggregation.bar_builder import TimeBarAggregator
from .datafeeds.options_chain import filter_chain, parse_occ_symbol
from statistics import mean
from .backtest.engine import BacktestEngine
from .strategies.simple_intraday_spx import SimpleIntradaySPXStrategy
from .utils.options_math import compute_option_metrics
from .utils.metrics_store import append_metric, compact_metrics
from .persistence_signals import SignalWriter
import pandas as pd
import time

app = typer.Typer(help="Zero DTE research & execution pipeline CLI")
LOGGER = get_logger("zero_dte.cli")


@app.command(name="secrets_check")
def secrets_check():
    """Report which critical environment variables / secrets are present (values not shown)."""
    import os
    keys = [
        "POLYGON_API_KEY",
        "OPENAI_API_KEY",
        "IQFEED_USERNAME",
        "IQFEED_PASSWORD",
    ]
    status = {}
    for k in keys:
        v = os.getenv(k)
        status[k] = bool(v)
    # Derived convenience flags similar to Settings
    status["has_polygon"] = status["POLYGON_API_KEY"]
    status["has_openai"] = status["OPENAI_API_KEY"]
    typer.echo(json.dumps(status, indent=2))


def _instantiate_strategy(StratCls, **candidate_kwargs):  # type: ignore[no-untyped-def]
    """Instantiate a strategy class filtering only accepted kwargs.

    This lets us pass generic fast/slow params while allowing strategies
    that don't declare them to still construct successfully.
    """
    try:
        sig = inspect.signature(StratCls)
        params = sig.parameters
        usable = {k: v for k, v in candidate_kwargs.items() if k in params}
        return StratCls(**usable)
    except Exception:  # noqa: BLE001
        return StratCls()  # type: ignore[call-arg]


@app.command(name="snapshot")
def snapshot(symbol: str = typer.Argument("SPX", help="Underlying symbol")):
    """Fetch a simple underlying snapshot via Polygon (placeholder)."""
    settings = Settings()
    # base directory for metrics persistence
    settings_dir = settings.data_dir or "data"
    if not settings.has_polygon:
        typer.echo("Polygon API key missing; export POLYGON_API_KEY or set in .env")
        raise typer.Exit(code=1)
    cfg = PolygonConfig.from_env()
    client = PolygonClient(cfg)
    snap = client.fetch_underlying_snapshot(symbol)
    typer.echo(json.dumps(snap, indent=2))


@app.command(name="demo_strategy")
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


@app.command(name="iqfeed_chain")
def iqfeed_chain(root: str = "SPX"):
    """Demonstrate IQFeed chain fetch placeholder."""
    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)
    chain = client.fetch_demo_chain(root)
    typer.echo(json.dumps(chain, indent=2))


@app.command(name="iqfeed_chain_real")
def iqfeed_chain_real(root: str = typer.Argument("SPX", help="Option root e.g. SPX or SPXW")):
    """Attempt real IQFeed chain request (scaffold). Requires live IQFeed and proper entitlements.

    NOTE: This uses a placeholder OCH command and may need adjustment to official spec.
    """
    cfg = IQFeedConfig.from_env()
    try:
        from .datafeeds.iqfeed_options_real import IQFeedOptionChainClient
        cli = IQFeedOptionChainClient(cfg)
        symbols = cli.request_chain(root)
        typer.echo(json.dumps({"root": root, "count": len(symbols), "symbols": symbols[:50]}, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"error": str(exc), "root": root}))


@app.command(name="iqfeed_greeks_snapshot")
def iqfeed_greeks_snapshot(root: str = typer.Argument("SPX", help="Option root"), limit: int = typer.Option(20, help="Limit number of symbols to request greeks for (scaffold)")):
    """Fetch a crude greeks snapshot scaffold by first requesting chain then issuing watch commands.

    OUTPUT: list of raw lines (no full parsing yet). Assumes demonstration only.
    """
    cfg = IQFeedConfig.from_env()
    try:
        from .datafeeds.iqfeed_options_real import IQFeedOptionChainClient
        chain_client = IQFeedOptionChainClient(cfg)
        syms = chain_client.request_chain(root)
        subset = syms[:limit]
        raw_recs = chain_client.request_greeks_snapshot(subset)
        typer.echo(json.dumps({
            "root": root,
            "requested": len(subset),
            "raw_records": [r.line for r in raw_recs[:limit]],
            "count": len(raw_recs),
        }, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"error": str(exc), "root": root}))


@app.command(name="ensure_dirs")
def ensure_dirs():
    """Ensure critical directories exist (data/, logs/)."""
    for d in [Path("data"), Path("logs")]:
        d.mkdir(parents=True, exist_ok=True)
    typer.echo("Directories ensured.")


@app.command(name="polygon_last_spx")
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


@app.command(name="iqfeed_ping")
def iqfeed_ping():
    """Ping IQFeed Level1 socket."""
    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)
    ok = client.ping()
    typer.echo(json.dumps({"ok": ok}))


@app.command(name="list_strategies")
def list_strategies_cmd():
    """List registered strategy names."""
    names = list_strategies()
    typer.echo(json.dumps({"strategies": names, "count": len(names)}, indent=2))


@app.command(name="market_summary")
def market_summary(
    spx_symbol: str = typer.Option("@SPX.X", help="IQFeed SPX index symbol"),
    strategy: str = typer.Option("odte_direction", help="Strategy name (on_price oriented)"),
    use_cache: bool = typer.Option(True, help="Cache summary for identical feature set within a short TTL"),
    rate_limit_secs: float = typer.Option(5.0, help="Min seconds between OpenAI calls (if enabled)"),
    extra: str = typer.Option("", help="Comma key=val pairs to inject as features"),
    skew: bool = typer.Option(False, help="Include simulated options skew metrics (placeholder chain)"),
):
    """Fetch snapshot (IQFeed → Polygon fallback), run single-tick strategy, produce neutral summary.

    Adds: basic caching, rate limiting, richer features (bid/ask spread, range), optional extra features.
    """
    from time import time as _time
    settings = Settings()
    # Static in-memory module-level caches
    if not hasattr(market_summary, "_last_call_ts"):
        market_summary._last_call_ts = 0.0  # type: ignore[attr-defined]
    if not hasattr(market_summary, "_cache"):
        market_summary._cache = {}  # type: ignore[attr-defined]

    StratCls = get_strategy(strategy)
    strat = _instantiate_strategy(StratCls)

    # Acquire IQFeed snapshot
    price: float | None = None
    iq_snapshot: dict | None = None
    poly_snapshot: dict | None = None
    try:
        cfg = IQFeedConfig.from_env()
        iq_client = IQFeedClient(cfg)
        iq_snapshot = iq_client.lookup_last(spx_symbol)
        if iq_snapshot and isinstance(iq_snapshot, dict):
            lp = iq_snapshot.get("last_price") or iq_snapshot.get("last_trade")
            if isinstance(lp, (int, float)) and lp > 0:
                price = float(lp)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("IQFeed snapshot failure: %s", exc)

    # Polygon fallback
    if price is None and settings.has_polygon:
        try:
            poly_client = PolygonClient(PolygonConfig.from_env())
            poly_snapshot = poly_client.last_trade_spx()
            if poly_snapshot and isinstance(poly_snapshot, dict):
                results = poly_snapshot.get("results")
                if results and isinstance(results, list):
                    price = float(results[0].get("c", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Polygon fallback failed: %s", exc)

    if price is None or price == 0:
        # Offline/test fallback: synthetic placeholder price
        price = 5000.0
        iq_snapshot = iq_snapshot or {}
        iq_snapshot["synthetic"] = True

    # Strategy result
    # Attempt on_price first (preferred for single tick). Fallback to evaluate if missing.
    if hasattr(strat, "on_price"):
        strat_res = strat.on_price(price)  # type: ignore[attr-defined]
        meta = getattr(strat_res, "meta", {}) if strat_res else {}
        sig_name = getattr(strat_res, "signal", "unknown")
    else:
        # Build pseudo market ctx for evaluate path
        meta = {}
        sig_name = "n/a"
        try:
            for s in strat.evaluate({"lastPrice": price}):  # type: ignore[attr-defined]
                meta = getattr(s, "metadata", {})
                sig_name = getattr(s, "name", sig_name)
                break
        except Exception:  # noqa: BLE001
            pass

    # Feature engineering
    features: dict[str, float | int | str] = {"price": price}
    if iq_snapshot:
        bid = iq_snapshot.get("bid"); ask = iq_snapshot.get("ask")
        if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
            spread = ask - bid
            features["bid"] = bid
            features["ask"] = ask
            features["spread"] = spread
            if bid:
                features["spread_bps"] = (spread / bid) * 10000
        day_high = iq_snapshot.get("day_high"); day_low = iq_snapshot.get("day_low")
        if isinstance(day_high, (int, float)) and isinstance(day_low, (int, float)) and day_high > day_low > 0:
            features["day_range"] = day_high - day_low
    # Optional options skew (placeholder simulated chain)
    if skew:
        try:
            from .datafeeds.iqfeed_options import IQFeedOptionsGreeks, skew_signal_from_chain
            og = IQFeedOptionsGreeks()
            chain = og.fetch_chain_greeks(spx_symbol)
            if chain:
                ss = skew_signal_from_chain(chain)
                for k, v in ss.items():
                    if k not in features:
                        features[k] = v  # type: ignore[assignment]
                features["skew_chain_size"] = len(chain)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Skew computation failed: %s", exc)
    # Merge strategy meta (non-colliding)
    for k, v in meta.items():
        if k not in features:
            features[k] = v  # type: ignore[assignment]
    # Extras
    if extra.strip():
        for part in extra.split(','):
            if '=' in part:
                k, v = part.split('=', 1)
                k = k.strip(); v = v.strip()
                if k and v and k not in features:
                    # attempt numeric cast
                    try:
                        if '.' in v:
                            features[k] = float(v)  # type: ignore[assignment]
                        else:
                            features[k] = int(v)  # type: ignore[assignment]
                    except Exception:
                        features[k] = v  # type: ignore[assignment]

    # Caching hash key
    feat_items = sorted(features.items())
    cache_key = str(feat_items)
    now = _time()
    summary = "(OpenAI disabled)"
    used_cache = False
    rate_limited = False
    if settings.has_openai:
        if use_cache and cache_key in market_summary._cache:  # type: ignore[attr-defined]
            summary = market_summary._cache[cache_key]  # type: ignore[index]
            used_cache = True
        else:
            if rate_limit_secs > 0 and (now - market_summary._last_call_ts) < rate_limit_secs:  # type: ignore[attr-defined]
                rate_limited = True
                summary = "(rate-limited; recent summary suppressed)"
            else:
                summarizer = OpenAIWrapper(settings.openai_api_key)
                summary = summarizer.summarize_market(features)
                market_summary._last_call_ts = now  # type: ignore[attr-defined]
                if use_cache:
                    market_summary._cache[cache_key] = summary  # type: ignore[attr-defined]

    typer.echo(json.dumps({
        "symbol": spx_symbol,
        "price": price,
        "strategy": strategy,
        "strategy_signal": sig_name,
        "strategy_meta_keys": list(meta.keys()),
        "features": features,
        "feature_count": len(features),
        "summary": summary,
        "iqfeed_snapshot": bool(iq_snapshot),
        "polygon_fallback_used": (price is not None and iq_snapshot is None),
        "cache_used": used_cache,
        "rate_limited": rate_limited,
    }, indent=2))


@app.command(name="market_stream")
def market_stream(
    symbols: str = typer.Option("@SPX.X", help="Comma-separated IQFeed symbols"),
    strategy: str = typer.Option("odte_direction", help="Strategy name supporting on_price/evaluate"),
    window: int = typer.Option(60, help="Rolling volatility window (ticks)"),
    duration: float = typer.Option(30.0, help="Stream duration seconds (ignored if --ticks provided)"),
    ticks: int = typer.Option(0, help="Stop after N ticks (overrides duration if >0)"),
    persist: bool = typer.Option(True, help="Persist each summary to metrics log (category=market_stream)"),
    persist_signals: bool = typer.Option(False, help="Persist emitted strategy signals via SignalWriter parquet"),
    summarize: bool = typer.Option(True, help="Call OpenAI summarize_market if key available"),
    interval_secs: float = typer.Option(2.0, help="Emit summary at most once per this many seconds"),
    synthetic: bool = typer.Option(False, help="Use synthetic price generator instead of real feed (testing)"),
    seed_price: float = typer.Option(5000.0, help="Starting synthetic price"),
    noise_bps: float = typer.Option(5.0, help="Synthetic price random walk step (bps)"),
    annualize_factor: float = typer.Option(0.0, help="Annualization factor for volatility (e.g. ticks_per_year)"),
    feature_keys: str = typer.Option("", help="Comma whitelist of feature keys to emit (empty=all)"),
    persist_raw: bool = typer.Option(False, help="Persist raw tick JSON (category=market_stream_raw)"),
    correlations: bool = typer.Option(False, help="Compute rolling pairwise correlation across symbols"),
    log_returns: bool = typer.Option(False, help="Use log returns instead of arithmetic for volatility"),
    ewma_alpha: float = typer.Option(0.0, help="If >0, compute EWMA volatility with this alpha (0<alpha<=1)"),
    corr_alert: float = typer.Option(0.0, help="If >0, emit alert when |correlation| exceeds this threshold for any pair"),
    rules: List[str] = typer.Option([], '--rule', help="Alert rule expressions field op value[:label] e.g. price>6000:rich"),
    indicators: bool = typer.Option(False, help="If set, compute RSI, Bollinger, GK/RS vol estimates on primary symbol"),
    rsi_period: int = typer.Option(14, help="RSI period"),
    bb_period: int = typer.Option(20, help="Bollinger period"),
    bb_std: float = typer.Option(2.0, help="Bollinger std multiplier"),
    alt_vol_period: int = typer.Option(20, help="Window for Garman-Klass / Rogers-Satchell volatility"),
    seasonality: bool = typer.Option(False, help="Include month seasonality score/label (heuristic weights)"),
    regime: bool = typer.Option(False, help="Compute regime filters (choppy / vol_event)"),
    chop_threshold: float = typer.Option(61.8, help="Override choppiness threshold for regime (default 61.8)"),
    vol_z_hi: float = typer.Option(1.0, help="Override realized volatility z-score high threshold"),
):
    """Continuously stream market snapshots + strategy evaluation + rolling volatility.

    Uses IQFeedLevel1Stream when available unless --synthetic is set.
    Persists each emitted summary if --persist.
    """
    from time import time as _time, sleep as _sleep
    import random
    import numpy as np
    # Optional indicator helpers (import safely)
    try:
        from .utils.indicators import (
            rsi as _rsi_simple,
            rsi_wilder as _rsi_w,
            bollinger as _bb,
            garman_klass_vol as _gk,
            rogers_satchell_vol as _rs,
        )
    except Exception:  # noqa: BLE001
        _rsi_simple = _rsi_w = _bb = _gk = _rs = None  # type: ignore
    settings = Settings()
    settings_dir = settings.data_dir or "data"
    StratCls = get_strategy(strategy)
    strat = _instantiate_strategy(StratCls)
    # Optional signal writer (parquet) separate from metrics append
    signal_writer = None
    if persist_signals:
        try:
            signal_writer = SignalWriter(settings_dir, strategy)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed initializing SignalWriter: %s", exc)
            signal_writer = None
    # Filters (lazy instantiation to avoid imports when not needed)
    seasonality_filter = None
    regime_filters = None
    if seasonality:
        try:
            from .strategies.filters import SeasonalityFilter  # local import
            seasonality_filter = SeasonalityFilter()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("SeasonalityFilter init failed: %s", exc)
    if regime:
        try:
            from .strategies.filters import RegimeFilters
            regime_filters = RegimeFilters(chop_threshold=chop_threshold, vol_z_hi=vol_z_hi)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("RegimeFilters init failed: %s", exc)
    sym_list = [s.strip() for s in symbols.split(',') if s.strip()]
    primary = sym_list[0]
    vol_calc = RollingVolatility(
        window=window,
        annualize_factor=annualize_factor if annualize_factor > 0 else None,
        log_returns=log_returns,
        ewma_alpha=ewma_alpha if ewma_alpha > 0 else None,
    )
    # If imports failed, disable indicators flag silently
    if indicators and (_rsi_simple is None or _bb is None):  # type: ignore
        indicators = False
    # Multi-symbol rolling price storage for correlation
    price_hist: dict[str, deque] = {sym: deque(maxlen=window) for sym in sym_list}
    last_emit = 0.0
    emitted = 0
    total_ticks = 0
    start = _time()
    summarizer = OpenAIWrapper(settings.openai_api_key) if (summarize and settings.has_openai) else None
    iq_stream = None
    queue_iter = None
    if not synthetic:
        try:
            cfg = IQFeedConfig.from_env()
            iq_stream = IQFeedLevel1Stream(cfg)
            iq_stream.start(sym_list)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Falling back to synthetic due to IQFeed error: %s", exc)
            synthetic = True

    price = seed_price

    def _next_price() -> float:
        nonlocal price
        # Random walk with bounded small drift
        step = price * (noise_bps / 10000.0) * random.uniform(-1, 1)
        price = max(0.01, price + step)
        return price
    # Pre-parse rules
    import operator as _op
    OPS = {
        '>': _op.gt,
        '<': _op.lt,
        '>=': _op.ge,
        '<=': _op.le,
        '==': _op.eq,
        '!=': _op.ne,
    }
    class _Rule:  # lightweight internal rule structure
        def __init__(self, field: str, op, value: float, label: str):
            self.field = field
            self.op = op
            self.value = value
            self.label = label
        def eval(self, feats: dict):
            v = feats.get(self.field)
            if isinstance(v, (int, float)):
                try:
                    if self.op(v, self.value):
                        return True
                except Exception:  # noqa: BLE001
                    return False
            return False
    parsed_rules: list[_Rule] = []
    for expr in rules:
        expr = expr.strip()
        if not expr:
            continue
        label = None
        if ':' in expr:
            expr, label = expr.split(':', 1)
            label = label.strip() or None
        # find op by longest match
        op_token = None
        for candidate in sorted(OPS.keys(), key=len, reverse=True):
            if candidate in expr:
                op_token = candidate
                break
        if not op_token:
            continue
        field, val = expr.split(op_token, 1)
        field = field.strip(); val = val.strip()
        try:
            num = float(val)
        except ValueError:
            continue
        parsed_rules.append(_Rule(field, OPS[op_token], num, label or f"{field}{op_token}{val}"))

    interrupted = False
    try:
        while True:
            now = _time()
            if ticks > 0 and total_ticks >= ticks:
                break
            if duration > 0 and (now - start) >= duration and ticks == 0:
                break
            # Acquire prices
            if synthetic:
                symbol_prices = {sym: _next_price() for sym in sym_list}
            else:
                symbol_prices = {}
                drain_limit = 50
                drained = 0
                while drained < drain_limit:
                    msg = iq_stream.get(timeout=0.05) if iq_stream else None  # type: ignore[assignment]
                    if not msg:
                        break
                    drained += 1
                    sym = msg.get("symbol")
                    if sym not in sym_list:
                        continue
                    lp = msg.get("last_trade") or msg.get("lastPrice") or msg.get("close")
                    if isinstance(lp, (int, float)) and lp > 0:
                        symbol_prices[sym] = float(lp)
                if not symbol_prices:
                    continue
            cur_price = symbol_prices.get(primary)
            if not cur_price:
                continue
            total_ticks += 1
            vol_calc.update(float(cur_price))
            for s, p in symbol_prices.items():
                price_hist[s].append(p)
            # Strategy
            sig_name = "n/a"; meta = {}
            last_signal_obj = None
            if hasattr(strat, "on_price"):
                try:
                    sres = strat.on_price(float(cur_price))  # type: ignore[attr-defined]
                    sig_name = getattr(sres, "signal", sig_name)
                    meta = getattr(sres, "meta", meta)
                    last_signal_obj = sres
                except Exception:
                    pass
            else:
                try:
                    for s in strat.evaluate({"lastPrice": cur_price}):  # type: ignore[attr-defined]
                        sig_name = getattr(s, "name", sig_name)
                        meta = getattr(s, "metadata", meta)
                        last_signal_obj = s
                        break
                except Exception:
                    pass
            if (now - last_emit) < interval_secs:
                continue
            last_emit = now
            emitted += 1
            features = {"price": float(cur_price), **vol_calc.snapshot()}
            # Seasonality (month-based weight)
            if seasonality_filter:
                import datetime as _dt
                m = _dt.datetime.utcnow().month
                try:
                    features["seasonality_score"] = float(seasonality_filter.score(m))  # type: ignore[arg-type]
                    features["seasonality_label"] = seasonality_filter.label(m)
                except Exception:  # noqa: BLE001
                    pass
            # Optional indicators computed only on primary symbol synthetic history
            if indicators:
                ph = np.array(price_hist[primary], dtype=float)
                # Provide placeholder keys so tests can detect presence early
                features.setdefault("rsi", float('nan'))
                features.setdefault("rsi_wilder", float('nan'))
                if ph.size >= rsi_period + 1:
                    # simple RSI
                    try:
                        if _rsi_simple:
                            features["rsi"] = _rsi_simple(ph, rsi_period)  # type: ignore[operator]
                        else:
                            diff = np.diff(ph[-(rsi_period + 1):])
                            gains = np.clip(diff, 0, None)
                            losses = -np.clip(diff, None, 0)
                            avg_gain = gains.mean() if gains.size else 0.0
                            avg_loss = losses.mean() if losses.size else 0.0
                            if avg_loss == 0:
                                features["rsi"] = 100.0
                            else:
                                rs = avg_gain / max(avg_loss, 1e-12)
                                features["rsi"] = 100 - (100 / (1 + rs))
                    except Exception:  # noqa: BLE001
                        pass
                    # Wilder RSI
                    try:
                        if _rsi_w:
                            features["rsi_wilder"] = _rsi_w(ph, rsi_period)  # type: ignore[operator]
                    except Exception:  # noqa: BLE001
                        pass
                # Bollinger Bands
                features.setdefault("bb_mid", float('nan'))
                features.setdefault("bb_up", float('nan'))
                features.setdefault("bb_low", float('nan'))
                if _bb and ph.size >= bb_period:
                    try:
                        mid, up, lowb = _bb(ph, bb_period, bb_std)  # type: ignore[operator]
                        features["bb_mid"] = mid; features["bb_up"] = up; features["bb_low"] = lowb
                    except Exception:  # noqa: BLE001
                        pass
                # Alternative volatility estimators (approximate OHLC from price path)
                features.setdefault("gk_vol", float('nan'))
                features.setdefault("rs_vol", float('nan'))
                if (_gk or _rs) and ph.size >= alt_vol_period:
                    o = np.roll(ph, 1); o[0] = ph[0]
                    spread = ph * 0.0005
                    h = ph + spread
                    l = ph - spread
                    if _gk:
                        try:
                            features["gk_vol"] = _gk(o, h, l, ph, alt_vol_period)  # type: ignore[operator]
                        except Exception:  # noqa: BLE001
                            pass
                    if _rs:
                        try:
                            features["rs_vol"] = _rs(o, h, l, ph, alt_vol_period)  # type: ignore[operator]
                        except Exception:  # noqa: BLE001
                            pass
            corr_map = {}
            if correlations and len(sym_list) > 1 and all(len(price_hist[s]) >= 5 for s in sym_list):
                def _corr(a, b):
                    n = min(len(a), len(b))
                    if n < 2:
                        return 0.0
                    x = list(a)[-n:]; y = list(b)[-n:]
                    mx = sum(x)/n; my = sum(y)/n
                    num = sum((x[i]-mx)*(y[i]-my) for i in range(n))
                    denx = sum((x[i]-mx)**2 for i in range(n))
                    deny = sum((y[i]-my)**2 for i in range(n))
                    if denx <= 0 or deny <= 0:
                        return 0.0
                    import math as _m
                    return num / (_m.sqrt(denx*deny))
                matrix: dict[str, dict[str, float]] = {}
                for i, a in enumerate(sym_list):
                    for j, b in enumerate(sym_list):
                        if j <= i: continue
                        cval = _corr(price_hist[a], price_hist[b])
                        corr_map[f"corr_{a}_{b}"] = cval
                        matrix.setdefault(a, {})[b] = cval
                        matrix.setdefault(b, {})[a] = cval
                if matrix:
                    features["corr_matrix"] = matrix
            if corr_map:
                features.update(corr_map)
                if corr_alert > 0:
                    for k, v in corr_map.items():
                        if abs(v) >= corr_alert:
                            features.setdefault("alerts", []).append({"type": "corr_threshold", "pair": k, "value": v})
            # Regime filters (after indicators so price history is populated)
            if regime_filters:
                try:
                    ph = np.array(price_hist[primary], dtype=float)
                    if ph.size >= 30:  # require some history
                        spread = ph * 0.0005
                        high_arr = ph + spread
                        low_arr = ph - spread
                        reg = regime_filters.evaluate(high_arr, low_arr, ph)
                        # Merge (avoid overwriting existing keys except intentionally)
                        for rk, rv in reg.items():
                            if rk not in features:
                                features[rk] = rv  # type: ignore[assignment]
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("Regime evaluation failed: %s", exc)
            if parsed_rules:
                for r in parsed_rules:
                    try:
                        if r.eval(features):
                            features.setdefault("alerts", []).append({"type": "rule", "rule": r.label, "field": r.field, "value": features.get(r.field)})
                    except Exception:
                        continue
            for k, v in meta.items():
                if k not in features:
                    features[k] = v
            summary = "(OpenAI disabled)"
            if summarizer:
                summary = summarizer.summarize_market(features)
            if feature_keys.strip():
                allowed = {k.strip() for k in feature_keys.split(',') if k.strip()}
                features = {k: v for k, v in features.items() if k in allowed}
            out = {
                "symbol": primary,
                "ts": now,
                "tick_index": total_ticks,
                "emission_index": emitted,
                "strategy": strategy,
                "signal": sig_name,
                "features": features,
                "vol_window": window,
                "summary": summary,
                "synthetic": synthetic,
                "symbols": sym_list,
                "correlations": corr_map if corr_map else None,
            }
            if persist:
                try:
                    append_metric(settings_dir, "market_stream", out)  # type: ignore[arg-type]
                except Exception as exc:
                    LOGGER.warning("Persist failed: %s", exc)
            if persist and isinstance(features.get("alerts"), list) and features.get("alerts"):
                try:
                    for al in features["alerts"]:
                        append_metric(settings_dir, "alerts", {
                            "symbol": primary,
                            "ts": now,
                            "alert": al,
                            "emission_index": emitted,
                            "source": "market_stream",
                        })  # type: ignore[arg-type]
                except Exception as exc:
                    LOGGER.warning("Persist alerts failed: %s", exc)
            # Persist standalone signal object if requested
            if signal_writer and last_signal_obj is not None:
                try:
                    signal_writer.add(last_signal_obj)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("SignalWriter add failed: %s", exc)
            typer.echo(json.dumps(out))
            if synthetic:
                _sleep(min(0.1, interval_secs / 4))
    except KeyboardInterrupt:  # graceful shutdown
        interrupted = True
    finally:
        if iq_stream:
            iq_stream.stop()
        if 'signal_writer' in locals() and signal_writer is not None:
            try:
                signal_writer.flush()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("SignalWriter flush failed: %s", exc)
        typer.echo(json.dumps({
            "status": "interrupted" if interrupted else "completed",
            "emissions": emitted,
            "ticks": total_ticks,
            "symbol": primary,
            "strategy": strategy,
            "synthetic": synthetic,
            "rules": [getattr(r, 'label', '') for r in parsed_rules],
        }, indent=2))


@app.command(name="_ingest_prev_spx")
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


@app.command(name="iqfeed_stream")
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


@app.command(name="iqfeed_stream_persist")
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


@app.command(name="build_bars")
def build_bars(
    symbol: str,
    interval: float = 1.0,
    date: str = typer.Option(None, help="Date partition YYYY-MM-DD (defaults today)"),
    write: bool = typer.Option(False, help="Persist resulting bars to parquet under data/derived/bars"),
):
    """Aggregate stored Level1 ticks (persisted parquet) into time bars.

    When --write is supplied, bars are persisted to
    data/derived/bars/symbol=SYMBOL/interval=INTERVAL/date=YYYY-MM-DD/part-*.parquet
    """
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
    out: dict = {"symbol": symbol, "interval": interval, "bars": payload}
    if write:
        # Persist
        bars_dir = Path(settings.data_dir or "data") / "derived" / "bars" / f"symbol={symbol}" / f"interval={interval}" / f"date={date}"
        bars_dir.mkdir(parents=True, exist_ok=True)
        import pandas as _pd
        if payload:
            _df = _pd.DataFrame(payload)
            file_path = bars_dir / "part-000.parquet"
            _df.to_parquet(file_path, index=False)
            out["path"] = str(bars_dir)
            out["rows"] = len(_df)
        else:
            out["path"] = str(bars_dir)
            out["rows"] = 0
    typer.echo(json.dumps(out, indent=2))


@app.command(name="live_strategy")
def live_strategy(symbols: str = typer.Argument("SPX", help="Comma-separated symbols"), duration: float = 20.0,
                  fast: int = typer.Option(5, help="Fast MA window"), slow: int = typer.Option(20, help="Slow MA window"),
                  persist: bool = typer.Option(False, help="Persist emitted signals to parquet"),
                  strategy: str = typer.Option("simple_intraday_spx", help="Strategy name (see list_strategies)")):
    """Run the simple strategy live on streaming Level1 prices (demonstration)."""
    cfg = IQFeedConfig.from_env()
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    stream = IQFeedLevel1Stream(cfg)
    StratCls = get_strategy(strategy)
    strat = _instantiate_strategy(StratCls, fast=fast, slow=slow)
    results = []
    writer = None
    settings = Settings()
    if persist:
        writer = SignalWriter(settings.data_dir or "data", "simple_intraday_spx")  # type: ignore[arg-type]
    stream.start(syms)
    start = time.time()
    while (time.time() - start) < duration:
        msg = stream.get(timeout=0.5)
        if not msg:
            continue
        # adapt message to strategy context
        ctx = {"symbol": msg.get("symbol"), "lastPrice": msg.get("last_trade") or msg.get("last")}
        for sig in strat.evaluate(ctx):
            results.append(sig.to_dict())
            if writer:
                writer.add(sig)
    stream.stop()
    if writer:
        writer.flush()
    typer.echo(json.dumps({
        "symbols": syms,
        "signals": results,
        "count": len(results),
    }, indent=2))


@app.command(name="chain_window")
def chain_window(
    symbols: str = typer.Argument(..., help="Comma-separated OCC option symbols"),
    root: str = typer.Option("SPXW", help="Underlying root to filter"),
    center: float = typer.Option(..., help="Center strike (e.g. 4550)"),
    width: float = typer.Option(100, help="Half-width around center to include"),
):
    """Filter a provided list of OCC option symbols into a strike window.

    Example:
        python -m src.cli chain_window "SPXW250925C00045000,SPXW250925P00045500" --root SPXW --center 4550 --width 100
    """
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    contracts = filter_chain(sym_list, root=root, center_strike=center, width=width)
    out = [
        {
            "symbol": c.symbol,
            "root": c.root,
            "expiry": c.expiry.strftime("%Y-%m-%d"),
            "cp": c.call_put,
            "strike": c.strike,
        }
        for c in contracts
    ]
    typer.echo(json.dumps({
        "root": root,
        "center": center,
        "width": width,
        "count": len(out),
        "contracts": out,
    }, indent=2))


@app.command(name="latency_summary")
def latency_summary(symbol: str, date: str = typer.Option(None, help="Partition date YYYY-MM-DD (defaults today)")):
    """Compute basic latency stats (arrival_ts - epoch) from stored Level1 ticks.

    Requires ticks with both 'arrival_ts' and 'epoch' fields (recent ingestion).
    """
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
    import pandas as _pd
    latencies = []
    count = 0
    for f in files:
        df = _pd.read_parquet(f, columns=["symbol", "epoch", "arrival_ts"])  # type: ignore[arg-type]
        sdf = df[df.symbol == symbol]
        if not sdf.empty:
            valid = sdf.dropna(subset=["epoch", "arrival_ts"])  # type: ignore[arg-type]
            if not valid.empty:
                lat_series = (valid["arrival_ts"] - valid["epoch"]).astype(float)
                latencies.extend(lat_series.tolist())
                count += len(lat_series)
    if not latencies:
        typer.echo(json.dumps({"symbol": symbol, "count": 0, "error": "no latency data"}, indent=2))
        return
    lat_sorted = sorted(latencies)
    p50 = lat_sorted[int(0.50 * (len(lat_sorted)-1))]
    p90 = lat_sorted[int(0.90 * (len(lat_sorted)-1))]
    p99 = lat_sorted[int(0.99 * (len(lat_sorted)-1))]
    payload = {
        "symbol": symbol,
        "count": count,
        "mean_ms": mean(latencies) * 1000.0,
        "p50_ms": p50 * 1000.0,
        "p90_ms": p90 * 1000.0,
        "p99_ms": p99 * 1000.0,
        "date": date,
    }
    # Persist metric for longitudinal tracking
    try:
        append_metric(settings.data_dir or "data", "latency", payload)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Latency metric persistence failed: %s", exc)
    typer.echo(json.dumps(payload, indent=2))


@app.command(name="backtest_bars")
def backtest_bars(
    symbol: str,
    interval: float = 1.0,
    date: str = typer.Option(None, help="Date partition (defaults today)"),
    fast: int = typer.Option(5, help="Fast MA window"),
    slow: int = typer.Option(20, help="Slow MA window"),
    persist: bool = typer.Option(False, help="Persist emitted signals"),
    annualize_factor: float = typer.Option(0.0, help="If > 1, annualize Sharpe/Sortino using sqrt(factor)"),
    trades_out: Path = typer.Option(None, help="Optional path to write trade list (.json or .parquet)"),
    equity_out: Path = typer.Option(None, help="Optional path to write equity curve (.json or .parquet)"),
    commission: float = typer.Option(0.0, help="Flat commission per round-trip trade"),
    slippage_bps: float = typer.Option(0.0, help="Slippage (bps) per side applied on entry and exit"),
    quantiles: str = typer.Option("", help="Comma list of extra PnL quantiles (e.g. 0.1,0.2,0.9)"),
    strategy: str = typer.Option("simple_intraday_spx", help="Strategy name"),
):
    """Run the simple strategy over previously built bars (derived parquet) and emit metrics."""
    settings = Settings()
    if date is None:
        date = time.strftime("%Y-%m-%d", time.gmtime())
    bars_dir = Path(settings.data_dir or "data") / "derived" / "bars" / f"symbol={symbol}" / f"interval={interval}" / f"date={date}"
    if not bars_dir.exists():
        typer.echo(json.dumps({"error": f"bars not found: {bars_dir}"}))
        raise typer.Exit(1)
    import pandas as _pd
    parts = sorted(bars_dir.glob("*.parquet"))
    if not parts:
        typer.echo(json.dumps({"error": "no bar parquet files"}))
        raise typer.Exit(1)
    df = _pd.concat([_pd.read_parquet(p) for p in parts], ignore_index=True)
    # Normalize context to strategy expectation (close acts as lastPrice)
    rows = df.to_dict(orient="records")
    # Type: BacktestEngine expects Strategy protocol; casting to Any to avoid cross-module StrategySignal mismatch for static checkers.
    from typing import cast, Any as _Any  # local import to keep global namespace clean
    StratCls = get_strategy(strategy)
    strat = cast(_Any, _instantiate_strategy(StratCls, fast=fast, slow=slow))
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    # Adapt each row so strategy sees 'lastPrice'
    adapted = ({"lastPrice": r["close"], **r} for r in rows)
    af: float | None = annualize_factor if annualize_factor and annualize_factor > 1 else None
    q_list = []
    if quantiles.strip():
        for part in quantiles.split(','):
            p = part.strip()
            if not p:
                continue
            try:
                val = float(p)
            except ValueError:
                continue
            if 0 <= val <= 1:
                q_list.append(val)
    res = engine.run(adapted, collect_signals=persist, annualize_factor=af, quantiles=q_list or None,
                     commission=commission, slippage_bps=slippage_bps)
    if persist and res.collected_signals:
        writer = SignalWriter(settings.data_dir or "data", "simple_intraday_spx")  # type: ignore[arg-type]
        for sig in res.collected_signals:
            writer.add(sig)
        writer.flush()
    # Optional trades export
    if trades_out:
        try:
            trades_out.parent.mkdir(parents=True, exist_ok=True)
            if str(trades_out).endswith('.json'):
                import json as _json
                trades_out.write_text(_json.dumps(res.trades, indent=2))
            elif str(trades_out).endswith('.parquet'):
                import pandas as _pd
                _pd.DataFrame(res.trades).to_parquet(trades_out)  # type: ignore[arg-type]
            else:
                raise ValueError("Unsupported trades_out extension; use .json or .parquet")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to export trades: %s", exc)
    elif persist and res.trades:
        # Default persistence path when not explicitly provided
        default_trades_path = Path(settings.data_dir or "data") / "derived" / "backtests" / "trades" / f"symbol={symbol}" / f"date={date}" / f"fast={fast}_slow={slow}" / "trades.parquet"
        try:
            import pandas as _pd
            default_trades_path.parent.mkdir(parents=True, exist_ok=True)
            _pd.DataFrame(res.trades).to_parquet(default_trades_path)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to persist default trades dataset: %s", exc)

    # Equity curve export
    if equity_out:
        try:
            equity_out.parent.mkdir(parents=True, exist_ok=True)
            if str(equity_out).endswith('.json'):
                import json as _json
                equity_out.write_text(_json.dumps(res.equity_curve, indent=2))
            elif str(equity_out).endswith('.parquet'):
                import pandas as _pd
                _pd.DataFrame({"equity": res.equity_curve}).reset_index(names="step").to_parquet(equity_out)  # type: ignore[arg-type]
            else:
                raise ValueError("Unsupported equity_out extension; use .json or .parquet")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to export equity curve: %s", exc)
    result_payload = {
        "symbol": symbol,
        "interval": interval,
        "date": date,
        "signals": res.signals_emitted,
        "bars": res.bars_processed,
        "total_pnl": res.total_pnl,
        "wins": res.wins,
        "losses": res.losses,
        "max_drawdown": res.max_drawdown,
        "win_rate": res.win_rate,
        "equity_points": len(res.equity_curve),
        "sharpe": res.sharpe,
        "sortino": res.sortino,
        "sharpe_annualized": res.sharpe_annualized,
        "sortino_annualized": res.sortino_annualized,
        "avg_win": res.avg_win,
        "avg_loss": res.avg_loss,
        "profit_factor": res.profit_factor,
        "expectancy": res.expectancy,
        "trade_count": res.trade_count,
        "open_trade": res.open_trade,
        "trades_exported": bool(trades_out),
        "annualize_factor": af or 0.0,
        "avg_mae": res.avg_mae,
        "avg_mfe": res.avg_mfe,
        "calmar": res.calmar,
        "equity_exported": bool(equity_out),
        "pnl_p25": res.pnl_p25,
        "pnl_p50": res.pnl_p50,
        "pnl_p75": res.pnl_p75,
        "pnl_p95": res.pnl_p95,
        "mae_p50": res.mae_p50,
        "mfe_p50": res.mfe_p50,
        "gross_pnl": res.gross_pnl,
        "commission_paid": res.commission_paid,
        "slippage_paid": res.slippage_paid,
        "total_costs": res.total_costs,
        "pnl_quantiles": res.pnl_quantiles,
        "avg_signal_strength": getattr(res, "avg_signal_strength", 0.0),
    }
    try:
        append_metric(settings.data_dir or "data", "backtest", result_payload)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Backtest metric persistence failed: %s", exc)
    typer.echo(json.dumps(result_payload, indent=2))


@app.command(name="option_iv")
def option_iv(spot: float, strike: float, mid: float, days: float = 1.0, put: bool = False):
    """Compute a quick implied volatility estimate for a single option quote."""
    settings = Settings()
    metrics = compute_option_metrics(spot=spot, strike=strike, t_days=days, mid_price=mid, call=not put, rate=settings.risk_free_rate)
    typer.echo(json.dumps({
        "spot": spot,
        "strike": strike,
        "days": days,
        "mid": mid,
        "call": (not put),
        "iv": metrics.iv,
        "delta": metrics.delta,
        "gamma": metrics.gamma,
        "theta": metrics.theta,
        "vega": metrics.vega,
    }, indent=2))


@app.command(name="compact_metrics_cmd")
def compact_metrics_cmd(category: str, date: str = typer.Option(None, help="Date (YYYY-MM-DD)")):
    """Compact metrics JSONL log into a parquet dataset for the given category/date."""
    settings = Settings()
    path = compact_metrics(settings.data_dir or "data", category, date)
    if path is None:
        typer.echo(json.dumps({"category": category, "date": date, "status": "no-data"}, indent=2))
    else:
        typer.echo(json.dumps({"category": category, "date": date, "status": "ok", "path": str(path)}, indent=2))


@app.command(name="compact_alerts")
def compact_alerts(date: str = typer.Option(None, help="Date (YYYY-MM-DD)")):
    """Convenience wrapper to compact the 'alerts' metrics category."""
    settings = Settings()
    path = compact_metrics(settings.data_dir or "data", "alerts", date)
    if path is None:
        typer.echo(json.dumps({"category": "alerts", "date": date, "status": "no-data"}, indent=2))
    else:
        typer.echo(json.dumps({"category": "alerts", "date": date, "status": "ok", "path": str(path)}, indent=2))

@app.command(name="grid_search_ma")
def grid_search_ma(
    symbol: str,
    interval: float = 1.0,
    date: str = typer.Option(None, help="Date partition (defaults today)"),
    fast_min: int = 2,
    fast_max: int = 10,
    slow_min: int = 15,
    slow_max: int = 40,
    step: int = 1,
    annualize_factor: float = 0.0,
    top: int = 10,
    jobs: int = typer.Option(1, help="Parallel workers (processes) for parameter combinations"),
    strategy: str = typer.Option("simple_intraday_spx", help="Strategy name (must accept fast/slow)"),
):
    """Grid search over fast/slow moving average windows for the day’s bars.

    Produces a ranked JSON list (top N by expectancy then Sharpe) with core metrics.
    """
    settings = Settings()
    if date is None:
        date = time.strftime("%Y-%m-%d", time.gmtime())
    bars_dir = Path(settings.data_dir or "data") / "derived" / "bars" / f"symbol={symbol}" / f"interval={interval}" / f"date={date}"
    if not bars_dir.exists():
        typer.echo(json.dumps({"error": f"bars not found: {bars_dir}"}))
        raise typer.Exit(1)
    import pandas as _pd
    parts = sorted(bars_dir.glob("*.parquet"))
    if not parts:
        typer.echo(json.dumps({"error": "no bar parquet files"}))
        raise typer.Exit(1)
    df = _pd.concat([_pd.read_parquet(p) for p in parts], ignore_index=True)
    rows = df.to_dict(orient="records")
    adapted_rows = [{"lastPrice": r["close"], **r} for r in rows]
    results = []
    af = annualize_factor if annualize_factor and annualize_factor > 1 else None
    combos: list[tuple[int, int]] = []
    for f in range(fast_min, fast_max + 1, step):
        for s in range(max(slow_min, f + 1), slow_max + 1, step):
            combos.append((f, s))

    def _eval(params: tuple[int, int]):
        f, s = params
        try:
            StratCls = get_strategy(strategy)
            strat = _instantiate_strategy(StratCls, fast=f, slow=s)
            engine = BacktestEngine(strat)  # type: ignore[arg-type]
            r = engine.run(adapted_rows, collect_signals=False, annualize_factor=af)
            return {
                "fast": f,
                "slow": s,
                "expectancy": r.expectancy,
                "sharpe": r.sharpe,
                "sharpe_ann": r.sharpe_annualized,
                "sortino": r.sortino,
                "pnl": r.total_pnl,
                "win_rate": r.win_rate,
                "profit_factor": r.profit_factor,
                "trade_count": r.trade_count,
            }
        except Exception:  # noqa: BLE001
            return None

    if jobs > 1 and len(combos) > 1:
        import concurrent.futures as _fut
        with _fut.ProcessPoolExecutor(max_workers=jobs) as ex:
            for out in ex.map(_eval, combos):
                if out:
                    results.append(out)
    else:
        for c in combos:
            out = _eval(c)
            if out:
                results.append(out)
    # Rank: expectancy desc, then sharpe desc, then trade_count desc
    ranked = sorted(results, key=lambda r: (r["expectancy"], r["sharpe"], r["trade_count"]), reverse=True)
    typer.echo(json.dumps({
        "symbol": symbol,
        "date": date,
        "interval": interval,
        "searched": len(results),
        "top": ranked[:top],
    }, indent=2))


@app.command(name="backtest_range")
def backtest_range(
    symbol: str,
    interval: float = 1.0,
    start_date: str = typer.Option(..., help="Start date YYYY-MM-DD (inclusive)"),
    end_date: str = typer.Option(..., help="End date YYYY-MM-DD (inclusive)"),
    fast: int = typer.Option(5, help="Fast MA window"),
    slow: int = typer.Option(20, help="Slow MA window"),
    persist: bool = typer.Option(False, help="Persist emitted signals (aggregate run)"),
    annualize_factor: float = typer.Option(0.0, help="If > 1, annualize Sharpe/Sortino using sqrt(factor) for aggregate"),
    trades_out: Path = typer.Option(None, help="Optional path to write aggregate trade list (.json or .parquet)"),
    equity_out: Path = typer.Option(None, help="Optional path to write aggregate equity curve (.json or .parquet)"),
    commission: float = typer.Option(0.0, help="Flat commission per round-trip trade"),
    slippage_bps: float = typer.Option(0.0, help="Slippage (bps) per side applied on entry and exit"),
    quantiles: str = typer.Option("", help="Comma list of extra PnL quantiles (e.g. 0.1,0.2,0.9) for aggregate run"),
    strategy: str = typer.Option("simple_intraday_spx", help="Strategy name"),
):
    """Run the strategy across a date range and produce aggregate + per-day metrics.

    Implementation detail: we run per-day backtests (for daily breakdown) and one aggregate
    run over concatenated bars (chronologically) to compute holistic metrics instead of
    averaging daily stats.
    """
    from datetime import datetime, timedelta
    settings = Settings()
    try:
        sd = datetime.strptime(start_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        typer.echo(json.dumps({"error": "Invalid date format; use YYYY-MM-DD"}))
        raise typer.Exit(1)
    if ed < sd:
        typer.echo(json.dumps({"error": "end_date earlier than start_date"}))
        raise typer.Exit(1)

    # Helper to load bars dataframe for a date
    import pandas as _pd
    base_dir = Path(settings.data_dir or "data") / "derived" / "bars" / f"symbol={symbol}" / f"interval={interval}"

    cur = sd
    from typing import Any as _Any
    all_rows: list[dict[str, _Any]] = []
    daily: list[dict[str, _Any]] = []
    missing: list[str] = []
    af = annualize_factor if annualize_factor and annualize_factor > 1 else None

    # Iterate dates
    while cur <= ed:
        d_str = cur.strftime("%Y-%m-%d")
        day_dir = base_dir / f"date={d_str}"
        if not day_dir.exists():
            missing.append(d_str)
            cur += timedelta(days=1)
            continue
        parts = sorted(day_dir.glob("*.parquet"))
        if not parts:
            missing.append(d_str)
            cur += timedelta(days=1)
            continue
        try:
            df_day = _pd.concat([_pd.read_parquet(p) for p in parts], ignore_index=True)
        except Exception as exc:  # noqa: BLE001
            missing.append(d_str)
            LOGGER.warning("Failed reading bars for %s: %s", d_str, exc)
            cur += timedelta(days=1)
            continue
        rows_day: list[dict[str, _Any]] = df_day.to_dict(orient="records")  # type: ignore[assignment]
        adapted_day = ({"lastPrice": r["close"], **r} for r in rows_day)
        # Per-day engine run (no signal persistence to avoid duplication; we only persist aggregate later)
        from typing import cast as _cast, Any as _Any  # local import
        StratCls = get_strategy(strategy)
        strat_day = _cast(_Any, _instantiate_strategy(StratCls, fast=fast, slow=slow))
        engine_day = BacktestEngine(strat_day)  # type: ignore[arg-type]
        res_day = engine_day.run(adapted_day, collect_signals=False, annualize_factor=None)
        daily.append({
            "date": d_str,
            "bars": res_day.bars_processed,
            "signals": res_day.signals_emitted,
            "pnl": res_day.total_pnl,
            "wins": res_day.wins,
            "losses": res_day.losses,
            "win_rate": res_day.win_rate,
            "trade_count": res_day.trade_count,
            "max_drawdown": res_day.max_drawdown,
            "expectancy": res_day.expectancy,
        })
        # Accumulate rows for aggregate
        all_rows.extend(rows_day)
        cur += timedelta(days=1)

    if not all_rows:
        typer.echo(json.dumps({
            "symbol": symbol,
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
            "error": "no data found in range",
            "missing_dates": missing,
        }, indent=2))
        return

    # Aggregate run
    adapted_all = ({"lastPrice": r["close"], **r} for r in all_rows)
    from typing import cast as _cast2, Any as _Any2
    StratClsAgg = get_strategy(strategy)
    q_list = []
    if quantiles.strip():
        for part in quantiles.split(','):
            p = part.strip()
            if not p:
                continue
            try:
                val = float(p)
            except ValueError:
                continue
            if 0 <= val <= 1:
                q_list.append(val)
    strat = _cast2(_Any2, _instantiate_strategy(StratClsAgg, fast=fast, slow=slow))
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    res = engine.run(adapted_all, collect_signals=persist, annualize_factor=af, quantiles=q_list or None,
                     commission=commission, slippage_bps=slippage_bps)

    if persist and res.collected_signals:
        writer = SignalWriter(settings.data_dir or "data", "simple_intraday_spx")  # type: ignore[arg-type]
        for sig in res.collected_signals:
            writer.add(sig)
        writer.flush()

    # Optional trades export (aggregate only)
    if trades_out:
        try:
            trades_out.parent.mkdir(parents=True, exist_ok=True)
            if str(trades_out).endswith('.json'):
                import json as _json
                trades_out.write_text(_json.dumps(res.trades, indent=2))
            elif str(trades_out).endswith('.parquet'):
                import pandas as _pd
                _pd.DataFrame(res.trades).to_parquet(trades_out)  # type: ignore[arg-type]
            else:
                raise ValueError("Unsupported trades_out extension; use .json or .parquet")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to export aggregate trades: %s", exc)

    # Equity curve export
    if equity_out:
        try:
            equity_out.parent.mkdir(parents=True, exist_ok=True)
            if str(equity_out).endswith('.json'):
                import json as _json
                equity_out.write_text(_json.dumps(res.equity_curve, indent=2))
            elif str(equity_out).endswith('.parquet'):
                import pandas as _pd
                _pd.DataFrame({"equity": res.equity_curve}).reset_index(names="step").to_parquet(equity_out)  # type: ignore[arg-type]
            else:
                raise ValueError("Unsupported equity_out extension; use .json or .parquet")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to export aggregate equity curve: %s", exc)

    aggregate_payload = {
        "symbol": symbol,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
        "dates_processed": len(daily),
        "missing_dates": missing,
        "fast": fast,
        "slow": slow,
        "annualize_factor": af or 0.0,
        "signals": res.signals_emitted,
        "bars": res.bars_processed,
        "total_pnl": res.total_pnl,
        "wins": res.wins,
        "losses": res.losses,
        "win_rate": res.win_rate,
        "trade_count": res.trade_count,
        "max_drawdown": res.max_drawdown,
        "sharpe": res.sharpe,
        "sortino": res.sortino,
        "sharpe_annualized": res.sharpe_annualized,
        "sortino_annualized": res.sortino_annualized,
        "avg_win": res.avg_win,
        "avg_loss": res.avg_loss,
        "profit_factor": res.profit_factor,
        "expectancy": res.expectancy,
        "avg_mae": res.avg_mae,
        "avg_mfe": res.avg_mfe,
        "calmar": res.calmar,
        "pnl_p25": res.pnl_p25,
        "pnl_p50": res.pnl_p50,
        "pnl_p75": res.pnl_p75,
        "pnl_p95": res.pnl_p95,
        "mae_p50": res.mae_p50,
        "mfe_p50": res.mfe_p50,
        "gross_pnl": res.gross_pnl,
        "commission_paid": res.commission_paid,
        "slippage_paid": res.slippage_paid,
        "total_costs": res.total_costs,
        "pnl_quantiles": res.pnl_quantiles,
        "equity_points": len(res.equity_curve),
        "trades_exported": bool(trades_out),
        "equity_exported": bool(equity_out),
        "avg_signal_strength": getattr(res, "avg_signal_strength", 0.0),
    }

    payload = {
        "aggregate": aggregate_payload,
        "daily": daily,
    }

    # Persist aggregate metrics as a separate category for longitudinal analysis
    try:
        append_metric(settings.data_dir or "data", "backtest_range", aggregate_payload)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Backtest range metric persistence failed: %s", exc)

    typer.echo(json.dumps(payload, indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
