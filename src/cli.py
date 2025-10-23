"""Typer CLI entrypoints.
"""
from __future__ import annotations
"""
CLI entrypoint: load environment from .env if present so secrets persist per-repo.
"""

# Load .env early so pydantic Settings and direct os.getenv can see values.
# Skip during pytest (subprocess tests control env) or when ZERO_DTE_LOAD_DOTENV=0.
try:
    from dotenv import load_dotenv  # type: ignore
    import os as _os
    if (_os.getenv("PYTEST_CURRENT_TEST") is None) and (_os.getenv("ZERO_DTE_LOAD_DOTENV", "1").lower() not in ("0", "false")):
        # Prefer repo-local .env over any pre-set shell variables to avoid stale/mismatched secrets
        load_dotenv(override=True)
except Exception:
    # dotenv is optional; ignore if missing or failing
    pass
# moved further below after app is defined and other commands
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
from .ingestion.news_sentiment_realtime import polygon_news_sentiment as _news_sentiment_rt
from .datafeeds.iqfeed_news import NewsStream as _NewsStream
from .ingestion.iqfeed_level1_writer import Level1BatchWriter, BatchConfig
from .aggregation.bar_builder import TimeBarAggregator
from .datafeeds.options_chain import filter_chain, parse_occ_symbol
from statistics import mean
import math
from .backtest.engine import BacktestEngine
from .strategies.simple_intraday_spx import SimpleIntradaySPXStrategy
from .utils.options_math import compute_option_metrics
from .utils.metrics_store import append_metric, compact_metrics
from .persistence_signals import SignalWriter
import pandas as pd
import time
from .utils.bs_greeks import price as bs_price, delta as bs_delta, gamma as bs_gamma, vega as bs_vega, theta as bs_theta, rho as bs_rho, implied_vol_newton
from .analytics.greeks_factor import compute_greeks_norm
from .analytics.iv_surface import Quote as _IVQuote, atm_iv as _atm_iv, risk_reversal as _rr, butterfly as _fly, skew_slope as _slope, expected_move as _exp_move
try:
    # Optional: deep ensemble model (PyTorch). CLI commands will guard import errors.
    from .models.ensemble_vae_lstm_transformer import TrainConfig as _EnCfg, train_ensemble as _train_ens, predict_ensemble as _pred_ens
    _HAS_TORCH = True
except Exception:  # noqa: BLE001
    _HAS_TORCH = False
try:
    from .models.dataset_bars import load_bars_series as _load_bars_series
    _HAS_DATASET = True
except Exception:  # noqa: BLE001
    _HAS_DATASET = False
try:
    from .models.preprocess import normalize_series as _normalize_series
    _HAS_PREPROC = True
except Exception:  # noqa: BLE001
    _HAS_PREPROC = False
try:
    from .models.hybrid_garch_dl import (
        HybridConfig as _HyCfg,
        build_features_from_prices as _hy_build_feats,
        train_hybrid_from_returns as _hy_train,
        predict_hybrid_from_returns as _hy_predict,
        save_hybrid_checkpoint as _hy_save,
        load_hybrid_checkpoint as _hy_load,
    )
    _HAS_HYBRID = True
except Exception:  # noqa: BLE001
    _HAS_HYBRID = False
try:
    from .nlp.sentiment import score_text as _score_text
    _HAS_SENT = True
except Exception:  # noqa: BLE001
    _HAS_SENT = False
try:
    from .reports.html_report import render_walkforward_report as _render_report
    from .reports.html_report import render_interpretability_report as _render_interpret
    from .reports.html_report import render_stress_report as _render_stress
    from .analysis.stress import default_scenarios as _stress_scenarios
    from .reports.iv_skew_plot import plot_iv_skew as _plot_iv_skew
    _HAS_REPORTS = True
except Exception:  # noqa: BLE001
    _HAS_REPORTS = False
try:
    from .ensemble_gate import run_ensemble_gate as _run_ensemble_gate
    _HAS_ENSEMBLE = True
except Exception:  # noqa: BLE001
    _HAS_ENSEMBLE = False
try:
    from .greeks.chain_aggregate import aggregate_chain as _aggregate_chain
    _HAS_CHAIN_AGG = True
except Exception:  # noqa: BLE001
    _HAS_CHAIN_AGG = False
try:
    from .pipelines.stacked_nowcast import run_stacked_nowcast as _run_stacked_nowcast
    _HAS_STACKED = True
except Exception:  # noqa: BLE001
    _HAS_STACKED = False
try:
    from .tools.update_eod_close import main as _update_eod_close
    _HAS_EOD_UPDATE = True
except Exception:  # noqa: BLE001
    _HAS_EOD_UPDATE = False

app = typer.Typer(help="Zero DTE research & execution pipeline CLI")
LOGGER = get_logger("zero_dte.cli")

# Helpers for robust option handling and safe device selection
def _opt_val(val, default: str = "auto") -> str:
    """Return a plain string for Typer OptionInfo or raw values.
    When CLI functions are called directly in tests, Typer doesn't resolve defaults,
    so parameters may be OptionInfo objects. This normalizes to a string.
    """
    try:
        if isinstance(val, str):
            return val
        # Typer OptionInfo
        dv = getattr(val, "default", None)
        if isinstance(dv, str):
            return dv
    except Exception:
        pass
    return default


def _pick_device(flag: str):
    """Choose a compute device.
    - 'cpu': always CPU
    - 'cuda': use CUDA only if available AND a tiny op works, else fallback
    - 'auto': prefer CUDA, then DirectML (Windows), otherwise CPU
    - 'dml'/'directml': force DirectML if available (torch-directml)

    Returns a device string suitable for torch.tensor(..., device=ret).
    """
    f = (flag or "auto").lower()
    if f == "cpu":
        return "cpu"
    # Try CUDA first
    try:
        import torch as _torch
        def _cuda_works() -> bool:
            if not _torch.cuda.is_available():
                return False
            try:
                _ = _torch.ones(1, device="cuda") * 1.0
                _ = _.exp().cpu()
                return True
            except Exception:
                return False
        if f == "cuda":
            return "cuda" if _cuda_works() else "cpu"
        # Optional DirectML fallback (Windows, requires torch-directml)
        def _get_dml_device():
            try:
                import torch_directml as _tdml  # type: ignore
                return _tdml.device()
            except Exception:
                return None
        def _dml_works():
            dml = _get_dml_device()
            if dml is None:
                return None
            try:
                x = _torch.ones(1, device=dml)
                y = x.exp().to("cpu")
                _ = float(y.item())
                return dml
            except Exception:
                return None
        if f in ("dml", "directml"):
            return _get_dml_device() or "cpu"
        # auto: prefer CUDA, then DML, else CPU
        if _cuda_works():
            return "cuda"
        dml_dev = _dml_works()
        if dml_dev is not None:
            return dml_dev
        return "cpu"
    except Exception:
        return "cpu"

@app.command(name="gpu_probe")
def gpu_probe_cli():
    """Print GPU/Torch diagnostic info and suggest next steps for RTX 5090 (or other GPUs)."""
    import json as _json
    info: dict[str, object] = {}
    try:
        import torch as _torch
        info.update({
            "torch_version": getattr(_torch, "__version__", None),
            "torch_compiled_with_cuda": bool(getattr(_torch.version, "cuda", None)),
            "torch_cuda_version": getattr(_torch.version, "cuda", None),
            "cuda_is_available": bool(_torch.cuda.is_available()),
        })
        if _torch.cuda.is_available():
            try:
                name = _torch.cuda.get_device_name(0)
                cap = _torch.cuda.get_device_capability(0)
                info.update({"cuda_device_name": name, "cuda_compute_capability": cap})
                # tiny op sanity
                ok = True
                try:
                    _ = _torch.ones(1, device="cuda") * 1.0
                    _ = _.exp().cpu()
                    info["cuda_tiny_op_ok"] = True
                except Exception as _e:
                    info["cuda_tiny_op_ok"] = False
                    info["cuda_tiny_op_error"] = str(_e)
            except Exception as _e:
                info["cuda_query_error"] = str(_e)
        # DirectML probe (optional)
        try:
            import torch_directml as _tdml  # type: ignore
            dml = _tdml.device()
            try:
                _ = _torch.ones(1, device=dml).exp().to("cpu")
                info["directml_available"] = True
                info["directml_tiny_op_ok"] = True
            except Exception as _e:
                info["directml_available"] = True
                info["directml_tiny_op_ok"] = False
                info["directml_tiny_op_error"] = str(_e)
        except Exception:
            info["directml_available"] = False
    except Exception as _e:
        info["torch_import_error"] = str(_e)
    # Guidance
    guidance = []
    if info.get("cuda_is_available") and info.get("cuda_tiny_op_ok") is False:
        guidance.append("Your GPU is visible but the current PyTorch build does not contain kernels for it. Install a newer PyTorch build with CUDA that supports your GPU's compute capability (see pytorch.org/get-started).")
    if info.get("directml_available") and info.get("directml_tiny_op_ok"):
        guidance.append("DirectML is available as an alternative backend on Windows. You can run models on device='dml' if CUDA is unsupported.")
    if not guidance:
        guidance.append("If you have an RTX 5090 (Blackwell) and see 'sm_120 not compatible', upgrade to the latest PyTorch (CUDA 12.x+) or nightly that includes sm_120 support.")
    info["guidance"] = guidance
    typer.echo(_json.dumps(info, indent=2))

def _normalize_agent_result(res) -> str:
    """Best-effort extraction of textual content from an Agents result object.

    Order of precedence:
      1. output_text
      2. final_output
      3. aggregated text from final_output_messages / messages
      4. first entry in raw_responses (if list/dict)
      5. str(res)
    """
    try:
        for attr in ("output_text", "final_output"):
            val = getattr(res, attr, None)
            if val:
                return str(val)
        # Collect messages
        for msg_attr in ("final_output_messages", "messages"):
            msgs = getattr(res, msg_attr, None)
            if msgs and isinstance(msgs, (list, tuple)):
                collected: list[str] = []
                for m in msgs:
                    if isinstance(m, str):
                        collected.append(m)
                    elif isinstance(m, dict):
                        c = m.get("content") or m.get("text") or m.get("value")
                        if c:
                            collected.append(str(c))
                if collected:
                    return "\n".join(collected)
        raw = getattr(res, "raw_responses", None)
        if raw:
            if isinstance(raw, list) and raw:
                first = raw[0]
                if isinstance(first, dict):
                    # Try common fields
                    for k in ("output_text", "text", "content"):
                        if first.get(k):
                            return str(first[k])
                return str(first)
        return str(res)
    except Exception:  # noqa: BLE001
        return str(res)

def _ensure_sync(result, timeout: float | None = None):
    """Ensure a possibly-coroutine result is executed and returned.

    If result is a coroutine and no running loop: run with asyncio.run and optional timeout.
    If loop is running, attempt asyncio.wait_for via current loop and return awaited value.
    """
    import inspect as _inspect
    if not _inspect.iscoroutine(result):
        return result
    import asyncio as _asyncio
    async def _await_it():  # type: ignore
        if timeout is not None:
            return await _asyncio.wait_for(result, timeout=timeout)
        return await result
    try:
        return _asyncio.run(_await_it())
    except RuntimeError:
        # Already inside loop
        loop = _asyncio.get_event_loop()
        if timeout is not None:
            return loop.run_until_complete(_asyncio.wait_for(result, timeout=timeout))  # type: ignore[arg-type]
        return loop.run_until_complete(result)  # type: ignore[arg-type]

def _log_agent_run(meta: dict, text: str, result_obj=None):
    """Append structured agent run info to logs/agents_runs.jsonl (best-effort)."""
    try:
        import os as _os, json as _json
        from datetime import datetime as _dt, timezone as _tz
        path = Path("logs") / "agents_runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(meta)
        payload["timestamp"] = _dt.now(_tz.utc).isoformat()
        payload["text_len"] = len(text or "")
        # Light introspection of result object for counts
        if result_obj is not None:
            for attr in ("messages", "final_output_messages", "raw_responses"):
                val = getattr(result_obj, attr, None)
                if isinstance(val, (list, tuple)):
                    payload[f"{attr}_count"] = len(val)
        with path.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass

def _agent_invoke(agent, prompt: str, *, retries: int = 0, backoff: float = 1.0):
    """Attempt to invoke an Agents SDK Agent across legacy & current interfaces with optional retry.

    Invocation Order (each attempt):
      1. Direct method names: run/invoke/execute/call/chat/__call__ (positional, then keyword input=)
      2. Fallback to default AgentRunner async run

    Retries only trigger for transient / rate-limit style errors (simple heuristic on message).
    Returns underlying result (may be RunResult, str, or coroutine)."""
    import inspect as _inspect
    import time as _time
    import math as _math

    def _is_transient(err: Exception) -> bool:
        msg = str(err).lower()
        return any(w in msg for w in ("rate limit", "429", "timeout", "temporarily unavailable", "overloaded"))

    attempt = 0
    last_exc: Exception | None = None
    while True:
        try:
            # Try direct callable methods
            for name in ("run", "invoke", "execute", "call", "chat", "__call__"):
                if hasattr(agent, name):
                    fn = getattr(agent, name)
                    try:
                        return fn(prompt)
                    except TypeError as _e:
                        last_exc = _e
                        try:
                            return fn(input=prompt)
                        except Exception as _e2:  # noqa: BLE001
                            last_exc = _e2
                    except Exception as _e3:  # noqa: BLE001
                        last_exc = _e3
                        continue
            # Async runner fallback
            try:  # pragma: no cover
                import asyncio as _asyncio  # type: ignore
                from agents.run import get_default_agent_runner  # type: ignore

                async def _do():  # type: ignore
                    runner = get_default_agent_runner()
                    if runner is None:
                        from agents.run import set_default_agent_runner, AgentRunner  # type: ignore
                        set_default_agent_runner(AgentRunner())
                        runner = get_default_agent_runner()
                    return await runner.run(agent, prompt)

                try:
                    return _asyncio.run(_do())
                except RuntimeError:
                    return _do()
            except Exception as _e4:  # noqa: BLE001
                last_exc = _e4
            # If we got here without returning, raise last exception
            if last_exc:
                raise last_exc
            raise RuntimeError("No callable agent method found (methods + async runner failed)")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= retries or not _is_transient(exc):
                break
            sleep_for = backoff * (2 ** attempt)
            # Cap exponential growth to reasonable window (e.g. 8s)
            sleep_for = min(sleep_for, 8.0)
            _time.sleep(sleep_for)
            attempt += 1
            continue
    # Exhausted attempts
    assert last_exc is not None
    raise last_exc


@app.command(name="secrets_check")
def secrets_check(
    keys: str = typer.Option("", help="Comma-separated env var names to check (default core set)"),
    require: str = typer.Option("", help="Comma-separated required keys; non-zero exit if any missing"),
    mask: bool = typer.Option(False, help="If set, include masked forms of present keys"),
):
    """Report presence of environment secrets (Polygon, OpenAI, IQFeed, etc.).

    Examples:
      python -m src.cli secrets_check --keys POLYGON_API_KEY,OPENAI_API_KEY
      python -m src.cli secrets_check --require POLYGON_API_KEY,OPENAI_API_KEY
      python -m src.cli secrets_check --mask
    """
    import os
    default_keys = ["POLYGON_API_KEY", "OPENAI_API_KEY", "IQFEED_USERNAME", "IQFEED_PASSWORD"]
    if keys.strip():
        target_keys = [k.strip() for k in keys.split(',') if k.strip()]
    else:
        target_keys = default_keys
    status: dict[str, object] = {}
    missing: list[str] = []
    for k in target_keys:
        v = os.getenv(k)
        present = bool(v)
        status[k] = present
        if mask and present and isinstance(v, str):
            # Mask: first 6 chars + '...' + last 4 (if long enough)
            if len(v) > 12:
                status[f"{k}_masked"] = f"{v[:6]}...{v[-4:]}"
            else:
                status[f"{k}_masked"] = "(present)"
        if not present:
            missing.append(k)
    # Derived convenience flags
    if "POLYGON_API_KEY" in target_keys:
        status["has_polygon"] = bool(os.getenv("POLYGON_API_KEY"))
    if "OPENAI_API_KEY" in target_keys:
        status["has_openai"] = bool(os.getenv("OPENAI_API_KEY"))
    required_list = [r.strip() for r in require.split(',') if r.strip()] if require.strip() else []
    unmet = [r for r in required_list if not os.getenv(r)]
    if unmet:
        status["error"] = {
            "missing_required": unmet,
            "message": "One or more required keys missing",
        }
        typer.echo(json.dumps(status, indent=2))
        raise typer.Exit(code=1)
    typer.echo(json.dumps(status, indent=2))


@app.command(name="stacked_nowcast")
def stacked_nowcast_cli(
    ticks_csv: str = typer.Argument(..., help="rocket_ticks_YYYY-MM-DD_poly.csv"),
    out_alerts: str = typer.Option(..., help="Output alerts JSONL path"),
    train_days: int = typer.Option(5, help="How many prior days to include if files exist in the same folder"),
    use_xgb: bool = typer.Option(False, help="Use XGBoost backend if available for micro model"),
    near_pct: float = typer.Option(0.06, help="Moneyness near percent"),
    index_trend_min_bp: float = typer.Option(15.0, help="Index trend gate in basis points over 60 minutes"),
    sentiment_csv: str = typer.Option(None, help="Optional CSV with ts,text,sentiment to blend/gate alerts"),
    sentiment_weight: float = typer.Option(0.0, help="If >0, blend stack prob with sentiment scaled by this weight"),
    sentiment_gate: float = typer.Option(0.0, help="If >0, require |sentiment| >= gate near alert ts"),
):
    if not _HAS_STACKED:
        typer.echo(json.dumps({"ok": False, "error": "stacked pipeline unavailable"}, indent=2))
        raise typer.Exit(1)
    res = _run_stacked_nowcast(
        ticks_csv=ticks_csv,
        out_alerts=out_alerts,
        train_days=train_days,
        use_xgb=use_xgb,
        near_pct=near_pct,
        index_trend_min_bp=index_trend_min_bp,
        sentiment_csv=sentiment_csv,
        sentiment_weight=sentiment_weight,
        sentiment_gate=sentiment_gate,
    )
    typer.echo(json.dumps(res, indent=2))


@app.command(name="news_sentiment")
def news_sentiment_cli(
    symbol: str = typer.Argument("I:SPX", help="Polygon symbol (e.g., I:SPX)"),
    date: str = typer.Argument(..., help="YYYY-MM-DD (UTC day window)"),
    out_csv: str = typer.Option("logs/news_sentiment.csv", help="Output CSV with ts,text,sentiment,label"),
    backend: str = typer.Option("openai", help="Sentiment backend: openai|finbert"),
):
    """Fetch Polygon news for a day and score sentiment with OpenAI or FinBERT."""
    try:
        from .ingestion.polygon_news import news_to_sentiment_csv as _news_to_sentiment_csv
        res = _news_to_sentiment_csv(symbol, date, out_csv, backend=backend)
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2)); raise typer.Exit(1)
    typer.echo(json.dumps(res, indent=2))


@app.command(name="news_sentiment_now")
def news_sentiment_now(
    tickers: str = typer.Argument(..., help="Comma-separated tickers (e.g., SPY,QQQ,TSLA)"),
    lookback_min: int = typer.Option(30, help="Lookback window in minutes"),
    limit: int = typer.Option(40, help="Max news items to fetch"),
):
    """Compute quick lexicon-based sentiment for recent Polygon news across multiple tickers.

    Requires POLYGON_API_KEY; returns zeros if missing.
    """
    try:
        syms = [s.strip() for s in tickers.split(',') if s.strip()]
        res = _news_sentiment_rt(syms, lookback_min=lookback_min, limit=limit)
        typer.echo(json.dumps({"ok": True, "lookback_min": lookback_min, "scores": res}, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)} , indent=2)); raise typer.Exit(1)


@app.command(name="news_watch")
def news_watch(
    tickers: str = typer.Option("SPX,QQQ,IWM", help="Comma-separated tickers to follow (mapped to proxies)"),
    seconds: int = typer.Option(20, help="Run duration in seconds"),
    port: int = typer.Option(0, help="News TCP port; 0 uses IQFEED_NEWS_PORT or disables socket connect"),
    log_jsonl: str = typer.Option("", help="Optional JSONL log path for headlines"),
    sources: str = typer.Option("", help="Comma-separated news sources to allow (e.g., BENZINGA,COMTEX). If empty, no source filter."),
    print_interval: float = typer.Option(2.0, help="Seconds between window printouts"),
):
    """Lightweight smoke test for NewsStream: follow symbols, compute rolling sentiment, and print windows."""
    try:
        from pathlib import Path as _P
        syms = [s.strip().upper() for s in str(tickers).split(',') if s.strip()]
        ns = _NewsStream(port=(None if int(port or 0) == 0 else int(port)), log_jsonl=(_P(log_jsonl) if log_jsonl else None))
        ns.start()
        ns.follow(syms)
        if sources.strip():
            ns.set_feeds([s.strip() for s in sources.split(',') if s.strip()])
        t0 = time.time()
        last_print = 0.0
        while (time.time() - t0) < float(seconds):
            now = time.time()
            if (now - last_print) >= float(print_interval):
                w = ns.windows()
                typer.echo(json.dumps({"t": round(now - t0, 1), "windows": w, "sources": (sources or None)}))
                last_print = now
            time.sleep(0.1)
        # final snapshot
        final_w = ns.windows()
        typer.echo(json.dumps({"ok": True, "seconds": seconds, "windows": final_w, "sources": (sources or None)}, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2)); raise typer.Exit(1)
    finally:
        try:
            ns.stop()
        except Exception:
            pass


@app.command(name="update_eod_close")
def update_eod_close_cli(
    eod_json: str = typer.Argument(..., help="eod_pnl_YYYY-MM-DD_*.json"),
    symbol: str = typer.Option("SPX", help="IQFeed symbol (e.g., SPX)"),
    date: str = typer.Option(..., help="YYYY-MM-DD (ET)"),
):
    if not _HAS_EOD_UPDATE:
        typer.echo(json.dumps({"ok": False, "error": "EOD updater unavailable"}, indent=2))
        raise typer.Exit(1)
    # Reuse the script's main via an argv shim
    import sys as _sys
    argv = ["update_eod_close", eod_json, "--symbol", symbol, "--date", date]
    old = _sys.argv
    try:
        _sys.argv = argv
        _update_eod_close()  # this prints json result
    finally:
        _sys.argv = old


@app.command(name="chain_aggregate")
def chain_aggregate_cli(
    input_path: str = typer.Argument(..., help="Options chain snapshot file (CSV or JSONL)"),
    out_csv: str = typer.Option("logs/chain_agg.csv", help="Output CSV path for aggregated per-minute features"),
):
    """Aggregate an options chain stream into per-minute features (atm_iv, rr25, term_slope, gex, dex, charm_proxy)."""
    if not _HAS_CHAIN_AGG:
        typer.echo(json.dumps({"ok": False, "error": "chain aggregator unavailable"}, indent=2))
        raise typer.Exit(1)
    import pandas as _pd
    from pathlib import Path as _P
    p = _P(input_path)
    if not p.exists():
        typer.echo(json.dumps({"ok": False, "error": f"input not found: {input_path}"}, indent=2))
        raise typer.Exit(1)
    # Load CSV or JSONL
    if p.suffix.lower() == ".csv":
        df = _pd.read_csv(p)
    else:
        # assume JSONL
        df = _pd.read_json(p, lines=True)
    out = _aggregate_chain(df)
    _P(out_csv).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_csv, index=False)
    typer.echo(json.dumps({"ok": True, "rows": int(len(out)), "out": out_csv}, indent=2))


@app.command(name="secrets_persist")
def secrets_persist(keys: str = typer.Option("OPENAI_API_KEY", help="Comma-separated env var names to persist into .env (from current environment)")):
    """Persist selected environment variables into a local .env file.

    This writes only the specified keys using their CURRENT values from the process environment.
    The .env file is git-ignored and will be auto-loaded by this CLI.
    """
    import os
    from pathlib import Path
    from typing import List
    try:
        from dotenv import set_key  # type: ignore
    except Exception:
        typer.echo(json.dumps({"ok": False, "error": "python-dotenv not installed"}, indent=2))
        raise typer.Exit(1)

    dest = Path(".env")
    if not dest.exists():
        dest.write_text("# Local environment for ZeroDTE-pipeline (git-ignored)\n")

    def _mask(val: str) -> str:
        if not val:
            return ""
        if len(val) <= 8:
            return "***"
        return f"{val[:4]}***{val[-4:]}"

    names: List[str] = [k.strip() for k in keys.split(",") if k.strip()]
    updated: List[str] = []
    missing: List[str] = []
    masked: dict[str, str] = {}
    for name in names:
        val = os.getenv(name)
        if not val:
            missing.append(name)
            continue
        set_key(str(dest), name, val)
        updated.append(name)
        masked[name] = _mask(val)

    typer.echo(json.dumps({
        "ok": True,
        "path": str(dest.resolve()),
        "updated": updated,
        "missing": missing,
        "preview": masked,
    }, indent=2))


@app.command(name="report_walkforward")
def report_walkforward_cli(
    csv_path: str = typer.Argument(..., help="CSV output from walkforward_compare_bars"),
    metrics_json: str = typer.Option(None, help="Metrics JSON path (defaults to *_metrics.json)"),
    out_html: str = typer.Option(None, help="Output HTML (defaults to same stem with .html in logs)")
):
    """Generate a compact HTML report for a walk-forward run.

    Looks for sibling PNGs with default naming to embed.
    """
    import json as _json
    from pathlib import Path as _P
    if not _HAS_REPORTS:
        typer.echo(_json.dumps({"ok": False, "error": "report module unavailable"}, indent=2)); raise typer.Exit(1)
    p = _P(csv_path)
    if not p.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"not found: {csv_path}"}, indent=2)); raise typer.Exit(1)
    if not metrics_json:
        metrics_json = str(p.with_name(p.stem + "_metrics.json"))
    if not out_html:
        out_html = str(p.with_suffix(".html"))
    html = _render_report(csv_path, metrics_json, title=p.stem)
    _P(out_html).write_text(html, encoding="utf-8")
    typer.echo(_json.dumps({"ok": True, "html": out_html}, indent=2))


@app.command(name="report_interpretability")
def report_interpretability_cli(
    pd_csv: str | None = typer.Option(None, help="Partial dependence CSV (optional; autodetect latest logs/*_pd.csv)"),
    pd_png: str | None = typer.Option(None, help="Partial dependence PNG (optional; autodetect matching *_pd.png)"),
    lofo_csv: str | None = typer.Option(None, help="LOFO CSV (optional; autodetect latest logs/*_lofo.csv)"),
    cc_csv: str | None = typer.Option(None, help="Cross-correlation CSV (optional; autodetect latest logs/*_cc.csv)"),
    cc_png: str | None = typer.Option(None, help="Cross-correlation PNG (optional; autodetect matching *_cc.png)"),
    strat_csv: str | None = typer.Option(None, help="Stratified response CSV (optional; autodetect latest logs/*_strat.csv)"),
    strat_png: str | None = typer.Option(None, help="Stratified response PNG (optional; autodetect matching *_strat.png)"),
    title: str = typer.Option("Interpretability & Causal Diagnostics", help="Report title"),
    out_html: str | None = typer.Option(None, help="Output HTML path (defaults to logs/interpretability_report.html)"),
):
    """Generate a compact HTML interpretability report bundling PD, LOFO, and causal outputs.

    If paths are not provided, the latest artifacts will be autodetected from the logs/ folder.
    """
    import json as _json
    from pathlib import Path as _P
    logs = _P("logs"); logs.mkdir(parents=True, exist_ok=True)

    def _latest(pattern: str):
        try:
            files = list(logs.glob(pattern))
            if not files:
                return None
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return str(files[0])
        except Exception:
            return None

    # Autodetect missing inputs
    if not pd_csv:
        pd_csv = _latest("*_pd.csv")
    if not pd_png and pd_csv:
        cand = _P(pd_csv).with_suffix("")
        alt = str(cand).replace("_pd", "_pd") + ".png"  # same stem, png
        if _P(alt).exists():
            pd_png = alt
    if not lofo_csv:
        lofo_csv = _latest("*_lofo.csv")
    if not cc_csv:
        cc_csv = _latest("*_cc.csv")
    if not cc_png and cc_csv:
        cc_png = str(_P(cc_csv).with_suffix(".png"))
    if not strat_csv:
        strat_csv = _latest("*_strat.csv")
    if not strat_png and strat_csv:
        strat_png = str(_P(strat_csv).with_suffix(".png"))

    html = _render_interpret(
        title=title,
        pd_csv=pd_csv,
        pd_png=pd_png,
        lofo_csv=lofo_csv,
        cc_csv=cc_csv,
        cc_png=cc_png,
        strat_csv=strat_csv,
        strat_png=strat_png,
    )
    if not out_html:
        out_html = str(logs / "interpretability_report.html")
    _P(out_html).write_text(html, encoding="utf-8")
    typer.echo(_json.dumps({
        "ok": True,
        "html": out_html,
        "inputs": {"pd_csv": pd_csv, "pd_png": pd_png, "lofo_csv": lofo_csv, "cc_csv": cc_csv, "cc_png": cc_png, "strat_csv": strat_csv, "strat_png": strat_png}
    }, indent=2))


@app.command(name="secrets_write")
def secrets_write(pairs: str = typer.Option(..., help="Comma or semicolon separated key=value pairs to write into .env (e.g. OPENAI_API_KEY=sk-...,OPENAI_PROJECT=proj_...)")):
    """Write secrets directly into .env from provided key=value pairs.

    This bypasses the need to first export environment variables. The .env file is git-ignored
    and auto-loaded by this CLI on startup.
    """
    from pathlib import Path as _P
    try:
        from dotenv import set_key  # type: ignore
    except Exception:
        typer.echo(json.dumps({"ok": False, "error": "python-dotenv not installed"}, indent=2))
        raise typer.Exit(1)

    dest = _P(".env")
    if not dest.exists():
        dest.write_text("# Local environment for ZeroDTE-pipeline (git-ignored)\n")

    def _parse_pairs(s: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if not s:
            return out
        # split on comma or semicolon
        raw_parts = []
        for sep in [',', ';']:
            if sep in s:
                raw_parts = [p for chunk in s.split(';') for p in chunk.split(',')]
                break
        if not raw_parts:
            raw_parts = [s]
        for part in raw_parts:
            part = part.strip()
            if not part or '=' not in part:
                continue
            k, v = part.split('=', 1)
            k = k.strip(); v = v.strip()
            if k:
                # Strip surrounding quotes if user included them
                if (v.startswith("\"") and v.endswith("\"")) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                out.append((k, v))
        return out

    kvs = _parse_pairs(pairs)
    if not kvs:
        typer.echo(json.dumps({"ok": False, "error": "no valid key=value pairs provided"}, indent=2))
        raise typer.Exit(1)

    def _mask(val: str) -> str:
        if not val:
            return ""
        if len(val) <= 8:
            return "***"
        return f"{val[:4]}***{val[-4:]}"

    updated: list[str] = []
    preview: dict[str, str] = {}
    for k, v in kvs:
        set_key(str(dest), k, v)
        updated.append(k)
        preview[k] = _mask(v)

    typer.echo(json.dumps({
        "ok": True,
        "path": str(dest.resolve()),
        "updated": updated,
        "preview": preview,
    }, indent=2))


@app.command(name="train_ensemble")
def train_ensemble_cli(
    window: int = typer.Option(64, help="Sliding window length"),
    horizon: int = typer.Option(1, help="Forecast horizon (steps)"),
    epochs: int = typer.Option(3, help="Training epochs (keep small for smoke)"),
    lr: float = typer.Option(1e-3, help="Learning rate"),
    batch_size: int = typer.Option(64, help="Batch size"),
    n: int = typer.Option(1024, help="Synthetic series length to train on (temporary)"),
    out_path: str = typer.Option("models/ensemble.ckpt", help="Where to save the trained model (torch.save)"),
    device: str = typer.Option("auto", help="cpu|cuda|auto"),
    precision: str = typer.Option("auto", help="fp32|amp|bf16|auto")
):
    """Train the VAE+LSTM+Transformer ensemble on a small synthetic series and save checkpoint.

    Note: This is a minimal on-ramp. Replace synthetic data with real features later.
    """
    import json as _json
    if not _HAS_TORCH:
        typer.echo(_json.dumps({"ok": False, "error": "torch not installed; install torch then retry"}, indent=2))
        raise typer.Exit(1)
    import torch as _torch
    from pathlib import Path as _Path

    # Synthetic series: noisy sine
    t = _torch.arange(0, n, dtype=_torch.float32)
    series = _torch.sin(2 * 3.14159 * t / 50.0) + 0.1 * _torch.randn_like(t)
    dev = _pick_device(_opt_val(device))
    # RNNs (LSTM/GRU) are not supported on some Windows DirectML builds; prefer CPU unless CUDA is explicitly available
    if not (isinstance(dev, str) and dev.startswith("cuda")):
        dev = "cpu"
    prec = _opt_val(precision, "auto")
    cfg = _EnCfg(window=window, horizon=horizon, batch_size=batch_size, epochs=epochs, lr=lr, device=dev, precision=prec)
    model = _train_ens(series, cfg)
    _Path("models").mkdir(parents=True, exist_ok=True)
    # Save with metadata envelope
    from datetime import datetime as _dt, timezone as _tz
    ckpt = {
        "state_dict": model.state_dict(),
        "meta": {
            "source": "synthetic",
            "window": window,
            "horizon": horizon,
            "created": _dt.now(_tz.utc).isoformat(),
        },
    }
    _torch.save(ckpt, out_path)
    typer.echo(_json.dumps({"ok": True, "path": out_path, "meta": ckpt["meta"]}, indent=2))


@app.command(name="predict_ensemble")
def predict_ensemble_cli(
    ckpt: str = typer.Option("models/ensemble.ckpt", help="Model checkpoint path"),
    window: int = typer.Option(64, help="Window used by the model"),
    horizon: int = typer.Option(1, help="Forecast horizon"),
    n: int = typer.Option(256, help="Synthetic series length to predict from"),
    device: str = typer.Option("auto", help="cpu|cuda|auto")
):
    """Load an ensemble checkpoint and produce a small synthetic prediction (smoke test)."""
    import json as _json
    if not _HAS_TORCH:
        typer.echo(_json.dumps({"ok": False, "error": "torch not installed; install torch then retry"}, indent=2))
        raise typer.Exit(1)
    import torch as _torch
    from pathlib import Path as _Path
    from .models.ensemble_vae_lstm_transformer import EnsembleModel as _Ens

    t = _torch.arange(0, n, dtype=_torch.float32)
    series = _torch.sin(2 * 3.14159 * t / 50.0) + 0.1 * _torch.randn_like(t)
    dev = _pick_device(_opt_val(device))
    if not (isinstance(dev, str) and dev.startswith("cuda")):
        dev = "cpu"
    model = _Ens(window=window, horizon=horizon).to(dev)
    if not _Path(ckpt).exists():
        typer.echo(_json.dumps({"ok": False, "error": f"checkpoint not found: {ckpt}"}, indent=2))
        raise typer.Exit(1)
    loaded = _torch.load(ckpt, map_location="cpu")
    if isinstance(loaded, dict) and "state_dict" in loaded:
        model.load_state_dict(loaded["state_dict"])
    else:
        model.load_state_dict(loaded)
    yhat = _pred_ens(model, series, window=window, horizon=horizon)
    typer.echo(_json.dumps({"ok": True, "pred": [float(v) for v in yhat.view(-1)]}, indent=2))


@app.command(name="train_ensemble_bars")
def train_ensemble_bars_cli(
    symbol: str = typer.Argument(..., help="Symbol, e.g., SPX"),
    interval: float = typer.Argument(..., help="Bar interval seconds, e.g., 1.0"),
    start_date: str = typer.Option(..., help="Start date YYYY-MM-DD"),
    end_date: str = typer.Option(..., help="End date YYYY-MM-DD"),
    window: int = typer.Option(64, help="Sliding window length"),
    horizon: int = typer.Option(1, help="Forecast horizon (steps)"),
    epochs: int = typer.Option(3, help="Training epochs"),
    lr: float = typer.Option(1e-3, help="Learning rate"),
    batch_size: int = typer.Option(64, help="Batch size"),
    out_path: str = typer.Option("models/ensemble_bars.ckpt", help="Checkpoint path"),
    prefer_compact: bool = typer.Option(True, help="Prefer bars_compact dataset if present"),
    normalize: str = typer.Option("none", help="Normalization: none|returns|log_returns|zscore"),
    config: str = typer.Option(None, help="Optional JSON file with training params to override flags"),
    device: str = typer.Option("auto", help="cpu|cuda|auto"),
    precision: str = typer.Option("auto", help="fp32|amp|bf16|auto"),
):
    """Train the ensemble on close prices loaded from parquet bars (compact or raw derived)."""
    import json as _json
    if not _HAS_TORCH:
        typer.echo(_json.dumps({"ok": False, "error": "torch not installed"}, indent=2)); raise typer.Exit(1)
    if not _HAS_DATASET:
        typer.echo(_json.dumps({"ok": False, "error": "dataset loader unavailable"}, indent=2)); raise typer.Exit(1)
    import torch as _torch, json as _json
    settings = Settings()
    series_pd = _load_bars_series(settings.data_dir or "data", symbol, interval, start_date, end_date, field="close", prefer_compact=prefer_compact)
    if normalize and normalize.lower() != "none":
        if not _HAS_PREPROC:
            typer.echo(_json.dumps({"ok": False, "error": "preprocess not available"}, indent=2)); raise typer.Exit(1)
        series_pd = _normalize_series(series_pd, mode=normalize)
    if len(series_pd) < (window + horizon + 1):
        typer.echo(_json.dumps({"ok": False, "error": "not enough bars for given window/horizon"}, indent=2)); raise typer.Exit(1)
    series = _torch.tensor(series_pd.values, dtype=_torch.float32)
    # Load overrides from config if provided
    if config:
        try:
            with open(config, "r", encoding="utf-8") as f:
                cfg_json = _json.load(f)
            window = int(cfg_json.get("window", window))
            horizon = int(cfg_json.get("horizon", horizon))
            epochs = int(cfg_json.get("epochs", epochs))
            batch_size = int(cfg_json.get("batch_size", batch_size))
            lr = float(cfg_json.get("lr", lr))
        except Exception as e:  # noqa: BLE001
            typer.echo(_json.dumps({"ok": False, "error": f"invalid config: {e}"}, indent=2)); raise typer.Exit(1)
    dev = _pick_device(_opt_val(device))
    if not (isinstance(dev, str) and dev.startswith("cuda")):
        dev = "cpu"
    prec = _opt_val(precision, "auto")
    cfg = _EnCfg(window=window, horizon=horizon, batch_size=batch_size, epochs=epochs, lr=lr, device=dev, precision=prec)
    model = _train_ens(series, cfg)
    from pathlib import Path as _Path
    _Path("models").mkdir(parents=True, exist_ok=True)
    from datetime import datetime as _dt, timezone as _tz
    ckpt = {
        "state_dict": model.state_dict(),
        "meta": {
            "source": "bars",
            "symbol": symbol,
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
            "window": window,
            "horizon": horizon,
            "normalize": normalize or "none",
            "prefer_compact": bool(prefer_compact),
            "rows": int(len(series_pd)),
            "created": _dt.now(_tz.utc).isoformat(),
        },
    }
    _torch.save(ckpt, out_path)
    typer.echo(_json.dumps({"ok": True, "rows": int(len(series_pd)), "ckpt": out_path, "meta": ckpt["meta"]}, indent=2))


@app.command(name="predict_ensemble_bars")
def predict_ensemble_bars_cli(
    ckpt: str = typer.Option("models/ensemble_bars.ckpt", help="Checkpoint path"),
    symbol: str = typer.Argument(..., help="Symbol, e.g., SPX"),
    interval: float = typer.Argument(..., help="Bar interval seconds"),
    start_date: str = typer.Option(..., help="Start date YYYY-MM-DD"),
    end_date: str = typer.Option(..., help="End date YYYY-MM-DD"),
    window: int = typer.Option(64, help="Window length"),
    horizon: int = typer.Option(1, help="Forecast horizon"),
    prefer_compact: bool = typer.Option(True, help="Prefer bars_compact dataset if present"),
    normalize: str = typer.Option("none", help="Normalization: none|returns|log_returns|zscore"),
    enforce_meta: bool = typer.Option(False, help="If set, require checkpoint metadata to match window/horizon/normalize"),
    device: str = typer.Option("auto", help="cpu|cuda|auto"),
):
    """Predict using an ensemble checkpoint over bars data (last window in range)."""
    import json as _json
    if not _HAS_TORCH:
        typer.echo(_json.dumps({"ok": False, "error": "torch not installed"}, indent=2)); raise typer.Exit(1)
    if not _HAS_DATASET:
        typer.echo(_json.dumps({"ok": False, "error": "dataset loader unavailable"}, indent=2)); raise typer.Exit(1)
    import torch as _torch
    from pathlib import Path as _Path
    from .models.ensemble_vae_lstm_transformer import EnsembleModel as _Ens
    series_pd = _load_bars_series(Settings().data_dir or "data", symbol, interval, start_date, end_date, field="close", prefer_compact=prefer_compact)
    if normalize and normalize.lower() != "none":
        if not _HAS_PREPROC:
            typer.echo(_json.dumps({"ok": False, "error": "preprocess not available"}, indent=2)); raise typer.Exit(1)
        series_pd = _normalize_series(series_pd, mode=normalize)
    if len(series_pd) < (window + horizon):
        typer.echo(_json.dumps({"ok": False, "error": "not enough bars for given window/horizon"}, indent=2)); raise typer.Exit(1)
    dev = _pick_device(_opt_val(device))
    if not (isinstance(dev, str) and dev.startswith("cuda")):
        dev = "cpu"
    model = _Ens(window=window, horizon=horizon).to(dev)
    if not _Path(ckpt).exists():
        typer.echo(_json.dumps({"ok": False, "error": f"checkpoint not found: {ckpt}"}, indent=2)); raise typer.Exit(1)
    loaded = _torch.load(ckpt, map_location="cpu")
    meta = None
    if isinstance(loaded, dict) and "state_dict" in loaded:
        model.load_state_dict(loaded["state_dict"])
        meta = loaded.get("meta")
    else:
        model.load_state_dict(loaded)
    if enforce_meta and meta is not None:
        mismatches = {}
        if int(meta.get("window", window)) != int(window):
            mismatches["window"] = {"ckpt": meta.get("window"), "arg": window}
        if int(meta.get("horizon", horizon)) != int(horizon):
            mismatches["horizon"] = {"ckpt": meta.get("horizon"), "arg": horizon}
        ck_norm = str(meta.get("normalize", "none")).lower()
        if ck_norm != (normalize or "none").lower():
            mismatches["normalize"] = {"ckpt": ck_norm, "arg": normalize}
        if mismatches:
            typer.echo(_json.dumps({"ok": False, "error": "metadata mismatch", "mismatches": mismatches, "meta": meta}, indent=2))
            raise typer.Exit(1)
    series = _torch.tensor(series_pd.values, dtype=_torch.float32)
    yhat = _pred_ens(model, series, window=window, horizon=horizon)
    typer.echo(_json.dumps({"ok": True, "pred": [float(v) for v in yhat.view(-1)], "rows": int(len(series_pd)), "meta": meta}, indent=2))


@app.command(name="check_ensemble_ckpt")
def check_ensemble_ckpt(
    ckpt: str = typer.Argument(..., help="Checkpoint to validate"),
    window: int = typer.Option(64, help="Expected window length for the model"),
    horizon: int = typer.Option(1, help="Expected forecast horizon"),
):
    """Load a checkpoint and run a minimal forward pass to confirm integrity."""
    import json as _json
    if not _HAS_TORCH:
        typer.echo(_json.dumps({"ok": False, "error": "torch not installed"}, indent=2)); raise typer.Exit(1)
    import torch as _torch
    from pathlib import Path as _Path
    from .models.ensemble_vae_lstm_transformer import EnsembleModel as _Ens
    p = _Path(ckpt)
    if not p.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"not found: {ckpt}"}, indent=2)); raise typer.Exit(1)
    model = _Ens(window=window, horizon=horizon)
    loaded = _torch.load(p, map_location="cpu")
    meta = None
    if isinstance(loaded, dict) and "state_dict" in loaded:
        model.load_state_dict(loaded["state_dict"])
        meta = loaded.get("meta")
    else:
        model.load_state_dict(loaded)
    with _torch.no_grad():
        x = _torch.randn(1, window, 1)
        yhat, _ = model(x)
    typer.echo(_json.dumps({"ok": True, "shape": list(yhat.shape), "meta": meta}, indent=2))


@app.command(name="walkforward_compare_bars")
def walkforward_compare_bars_cli(
    symbol: str = typer.Argument(..., help="Symbol, e.g., SPX"),
    interval: float = typer.Argument(..., help="Bar interval seconds"),
    start_date: str = typer.Option(..., help="Start date YYYY-MM-DD"),
    end_date: str = typer.Option(..., help="End date YYYY-MM-DD"),
    window: int = typer.Option(64, help="Window length"),
    horizon: int = typer.Option(1, help="Forecast horizon"),
    prefer_compact: bool = typer.Option(True, help="Prefer bars_compact dataset if present"),
    normalize: str = typer.Option("log_returns", help="Normalization for ensemble and hybrid"),
    ensemble_ckpt: str = typer.Option("models/ensemble_bars.ckpt", help="Path to ensemble checkpoint (will train if missing)"),
    hybrid_ckpt: str = typer.Option("models/hybrid_bars.ckpt", help="Path to hybrid checkpoint (will train if missing)"),
    epochs: int = typer.Option(2, help="Epochs to use if training ckpts is needed"),
    metrics_window: int = typer.Option(50, help="Rolling window for metrics plots (set 0 to skip)"),
    price_metrics: bool = typer.Option(True, help="Compute price-level metrics using one-step price forecast"),
    confusion: bool = typer.Option(True, help="Compute directional confusion matrix for each model"),
    out_stem: str = typer.Option(None, help="Custom output stem for artifacts (filename stem only)"),
    sentiment_csv: str = typer.Option(None, help="Optional CSV with datetime,text columns for sentiment aggregation"),
    sentiment_backend: str = typer.Option("finbert", help="finbert|openai"),
    sentiment_weight: float = typer.Option(0.0, help="If >0, blend return predictions with sentiment score scaled by this weight"),
    sentiment_as_feature: bool = typer.Option(False, help="If True, include sentiment as an extra feature for the hybrid model (retrain if needed)"),
    sentiment_as_feature_str: str = typer.Option(None, "--sentiment-as-feature-str", help="Alternative: pass 'true'/'false' to set sentiment_as_feature"),
    n_lags: int = typer.Option(0, help="Add up to N lags of r, sigma, and sentiment (if used) as additional features"),
    sentiment_ffill_seconds: int = typer.Option(None, help="Cap forward-fill horizon in seconds for sentiment alignment (default unlimited)"),
    sentiment_half_life: float = typer.Option(0.0, help="If >0, apply exponential half-life decay (seconds) to forward-filled sentiment values"),
    sentiment_resample: str = typer.Option(None, help="Optional pandas resample rule for sentiment (e.g., 'S','5S','1min'); default derived from bar interval"),
):
    """Walk-forward compare Hybrid (GARCH+DL) vs Ensemble on bars data; export CSV and PNG plot."""
    import json as _json
    from pathlib import Path as _Path
    import numpy as _np
    import pandas as _pd
    if not (_HAS_DATASET and _HAS_PREPROC and _HAS_TORCH and _HAS_HYBRID):
        typer.echo(_json.dumps({"ok": False, "error": "missing deps (dataset/preproc/torch/hybrid)"}, indent=2)); raise typer.Exit(1)
    # Load bars
    series_pd = _load_bars_series(Settings().data_dir or "data", symbol, interval, start_date, end_date, field="close", prefer_compact=prefer_compact)
    # Build features for hybrid
    r_h, sigma = _hy_build_feats(series_pd, normalize=normalize or "log_returns")
    # Utility to align sentiment to a target index with optional decay and ffill cap
    def _align_sent(s_raw, target_index):
        import numpy as _np_al
        import pandas as _pd_al
        if s_raw is None or len(s_raw) == 0:
            return None
        # Determine resample rule
        rule = sentiment_resample
        if not rule:
            try:
                sec = float(interval)
                rule = f"{int(max(1, round(sec)))}s"
            except Exception:
                rule = "s"
        s_res = s_raw.resample(rule).median()
        # Compute decayed, capped forward fill onto target_index
        out_vals = []
        # Precompute numpy arrays for asof search
        s_idx = s_res.index
        for ts in target_index:
            try:
                # last observation at or before ts
                t0 = s_idx.asof(ts)
            except Exception:
                t0 = None
            if t0 is None or t0 is _pd_al.NaT:
                out_vals.append(0.0)
                continue
            val = float(s_res.loc[t0]) if t0 in s_res.index else 0.0
            # delta seconds
            delta = (ts - t0).total_seconds() if hasattr(ts, 'to_pydatetime') or hasattr(ts, 'tzinfo') else (ts - t0).total_seconds()
            # cap ffill horizon
            if sentiment_ffill_seconds is not None and delta > float(sentiment_ffill_seconds):
                out_vals.append(0.0)
                continue
            # apply decay if requested
            if sentiment_half_life and sentiment_half_life > 0:
                hl = float(sentiment_half_life)
                decay = 0.5 ** (float(delta) / hl)
                val = val * decay
            out_vals.append(val)
        return _pd_al.Series(out_vals, index=target_index)

    # Optional: load & align sentiment to returns index for use as a feature
    sent_r = None
    if sentiment_csv and _HAS_SENT:
        try:
            import pandas as _pd_s
            p = _Path(sentiment_csv)
            if p.exists():
                df_news = _pd_s.read_csv(p)
                if "datetime" in df_news.columns and "text" in df_news.columns:
                    df_news["datetime"] = _pd_s.to_datetime(df_news["datetime"], errors="coerce", utc=True).dt.tz_convert(None)
                    from .config import Settings as _S
                    api_key = _S().openai_api_key if sentiment_backend.lower() == "openai" else None
                    scores: list[float] = []
                    for txt in df_news["text"].astype(str).tolist():
                        try:
                            r = _score_text(txt, backend=sentiment_backend, api_key=api_key)
                            scores.append(float(r.score))
                        except Exception:
                            scores.append(0.0)
                    df_news["score"] = scores
                    s = df_news.dropna(subset=["datetime"]).set_index("datetime")["score"].sort_index()
                    sent_r = _align_sent(s, r_h.index)
        except Exception:
            sent_r = None
    # Normalize alternative boolean string if provided (tolerant input)
    if sentiment_as_feature_str is not None:
        _val = str(sentiment_as_feature_str).strip().lower()
        if _val in ("1", "true", "yes", "y", "on"):  # enable
            sentiment_as_feature = True
        elif _val in ("0", "false", "no", "n", "off"):  # disable
            sentiment_as_feature = False

    # Train Hybrid if needed
    from pathlib import Path as _P
    if not _P(hybrid_ckpt).exists():
        cfg = _HyCfg(window=window, horizon=horizon, epochs=epochs, batch_size=64, lr=1e-3, device="cpu")
        model_h, meta_h = _hy_train(r_h, sigma, cfg, normalize=normalize or "log_returns", extra_feat=(sent_r if sentiment_as_feature else None), n_lags=n_lags)
        _P("models").mkdir(parents=True, exist_ok=True)
        _hy_save(hybrid_ckpt, model_h, {
            **meta_h,
            "source": "bars", "symbol": symbol, "interval": interval,
            "start_date": start_date, "end_date": end_date,
            "prefer_compact": bool(prefer_compact),
        })
    model_h, meta_h = _hy_load(hybrid_ckpt)
    # If sentiment as feature requested but checkpoint doesn't match input size, retrain quickly
    try:
        need_feat3 = bool(sentiment_as_feature and sent_r is not None)
        ck_inp = int(meta_h.get("input_size", 2)) if isinstance(meta_h, dict) else 2
        if (need_feat3 and ck_inp != 3) or int(meta_h.get("n_lags", 0) or 0) != int(n_lags or 0):
            cfg = _HyCfg(window=window, horizon=horizon, epochs=max(1, epochs), batch_size=64, lr=1e-3, device="cpu")
            model_h, meta_h = _hy_train(r_h, sigma, cfg, normalize=normalize or "log_returns", extra_feat=sent_r if need_feat3 else None, n_lags=n_lags)
            _hy_save(hybrid_ckpt, model_h, {
                **meta_h,
                "source": "bars", "symbol": symbol, "interval": interval,
                "start_date": start_date, "end_date": end_date,
                "prefer_compact": bool(prefer_compact),
            })
    except Exception:
        pass
    # Prepare ensemble series
    series_norm = _normalize_series(series_pd, mode=normalize) if (normalize and normalize.lower() != "none") else series_pd
    import torch as _torch
    from .models.ensemble_vae_lstm_transformer import EnsembleModel as _Ens
    if not _P(ensemble_ckpt).exists():
        # Train quickly
        s = _torch.tensor(series_norm.values, dtype=_torch.float32)
        if len(s) < (window + horizon + 1):
            typer.echo(_json.dumps({"ok": False, "error": "not enough data for ensemble"}, indent=2)); raise typer.Exit(1)
        cfg_e = _EnCfg(window=window, horizon=horizon, batch_size=64, epochs=epochs, lr=1e-3, device="cpu")
        model_e = _train_ens(s, cfg_e)
        _P("models").mkdir(parents=True, exist_ok=True)
        _torch.save({"state_dict": model_e.state_dict(), "meta": {"window": window, "horizon": horizon, "normalize": normalize or "none"}}, ensemble_ckpt)
    # Load ensemble
    model_e = _Ens(window=window, horizon=horizon)
    loaded = _torch.load(ensemble_ckpt, map_location="cpu")
    model_e.load_state_dict(loaded.get("state_dict", loaded))
    model_e.eval()
    # Walk-forward predictions
    # Hybrid over returns: indices of r_h after drops
    r_idx = _pd.Series(r_h.values, index=r_h.index)
    sig_idx = _pd.Series(sigma.values, index=sigma.index)
    Lh = len(r_idx)
    preds_h = []
    ts_h = []
    # When using lagged features, ensure we have at least `window` rows AFTER dropna in the lagged DF.
    # Start the walk-forward offset at n_lags so r/s slices are long enough (window + n_lags) initially.
    start_i = int(max(0, n_lags or 0))
    for i in range(start_i, Lh - window - horizon + 1):
        r_slice = r_idx.iloc[: i + window]
        s_slice = sig_idx.iloc[: i + window]
        if sentiment_as_feature and (sent_r is not None):
            x_slice = sent_r.iloc[: i + window]
            yhat = _hy_predict(model_h, r_slice, s_slice, window=window, horizon=horizon, extra_feat=x_slice, n_lags=n_lags)
        else:
            yhat = _hy_predict(model_h, r_slice, s_slice, window=window, horizon=horizon, n_lags=n_lags)
        preds_h.append(float(yhat.reshape(-1)[-1]))
        ts_h.append(r_idx.index[i + window + horizon - 1])
    # Ensemble over normalized series
    s_norm = _torch.tensor(series_norm.values, dtype=_torch.float32)
    Le = len(s_norm)
    preds_e = []
    ts_e = []
    for j in range(0, Le - window - horizon + 1):
        seg = s_norm[: j + window]
        y_e = _pred_ens(model_e, seg, window=window, horizon=horizon)
        preds_e.append(float(y_e.view(-1)[-1]))
        # Map timestamps: use underlying series index; when normalize=returns/log_returns index is shorter
        # If series_norm aligns with original, use its index
        idx = series_norm.index
        ts_e.append(idx[j + window + horizon - 1])
    # Actual returns for comparison (for horizon=1)
    actual_r = r_idx.iloc[window + horizon - 1 : ]  # align start
    # Align to common timestamps
    df = _pd.DataFrame({
        "actual_return": actual_r.values,
    }, index=actual_r.index)
    df["pred_hybrid"] = _pd.Series(preds_h, index=_pd.Index(ts_h))
    df["pred_ensemble"] = _pd.Series(preds_e, index=_pd.Index(ts_e))
    df = df.dropna()
    # Optional sentiment enrichment/blend
    sent_series = None
    if sentiment_csv and _HAS_SENT:
        try:
            import pandas as _pd_s
            import os as _os_s
            p = _Path(sentiment_csv)
            if p.exists():
                df_news = _pd_s.read_csv(p)
                # Expect columns: datetime, text
                if "datetime" in df_news.columns and "text" in df_news.columns:
                    df_news["datetime"] = _pd_s.to_datetime(df_news["datetime"], errors="coerce", utc=True).dt.tz_convert(None)
                    # Score each row (fast path: apply; consider batching in future)
                    from .config import Settings as _S
                    api_key = _S().openai_api_key if sentiment_backend.lower() == "openai" else None
                    scores: list[float] = []
                    for txt in df_news["text"].astype(str).tolist():
                        try:
                            r = _score_text(txt, backend=sentiment_backend, api_key=api_key)
                            scores.append(float(r.score))
                        except Exception:
                            scores.append(0.0)
                    df_news["score"] = scores
                    s = df_news.dropna(subset=["datetime"]).set_index("datetime")["score"].sort_index()
                    sent_series = _align_sent(s, df.index)
        except Exception:
            sent_series = None

    if sentiment_weight and sent_series is not None:
        try:
            import numpy as _np_b
            # Blend predictions: y' = y + w * sent
            df["pred_hybrid_blend"] = df["pred_hybrid"].to_numpy() + float(sentiment_weight) * sent_series.to_numpy()
            df["pred_ensemble_blend"] = df["pred_ensemble"].to_numpy() + float(sentiment_weight) * sent_series.to_numpy()
        except Exception:
            pass

    # Metrics
    def _metrics(y_true, y_pred):
        y_true = _np.asarray(y_true)
        y_pred = _np.asarray(y_pred)
        mae = float(_np.mean(_np.abs(y_true - y_pred)))
        mse = float(_np.mean((y_true - y_pred) ** 2))
        rmse = float(_np.sqrt(mse))
        corr = float(_np.corrcoef(y_true, y_pred)[0, 1]) if len(y_true) > 1 else float('nan')
        # directional accuracy (ignore zeros)
        s_true = _np.sign(y_true)
        s_pred = _np.sign(y_pred)
        mask = s_true != 0
        if mask.any():
            hit = float(_np.mean((s_true[mask]) == (s_pred[mask])))
        else:
            hit = float('nan')
        return {"mae": mae, "mse": mse, "rmse": rmse, "corr": corr, "hitrate": hit}

    m_h = _metrics(df["actual_return"], df["pred_hybrid"]) if "pred_hybrid" in df else {}
    m_e = _metrics(df["actual_return"], df["pred_ensemble"]) if "pred_ensemble" in df else {}
    m_hb = _metrics(df["actual_return"], df["pred_hybrid_blend"]) if "pred_hybrid_blend" in df else None
    m_eb = _metrics(df["actual_return"], df["pred_ensemble_blend"]) if "pred_ensemble_blend" in df else None

    # Export
    out_dir = _Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_stem or f"walkforward_compare_{symbol}_{interval}_{start_date}_{end_date}"
    csv_path = out_dir / f"{stem}.csv"
    png_path = out_dir / f"{stem}.png"
    metrics_path = out_dir / f"{stem}_metrics.json"
    df.to_csv(csv_path)
    # Optional price-level metrics: one-step price prediction using previous actual price
    price_metrics_obj = None
    price_png_path = out_dir / f"{stem}_prices.png"
    if price_metrics:
        try:
            import numpy as _np_local
            import matplotlib.pyplot as _plt2
            # actual price aligned at return timestamps
            px = series_pd.reindex(df.index)
            px_prev = series_pd.shift(1).reindex(df.index)
            if normalize and normalize.lower() == "returns":
                f = lambda r: (1.0 + r)
            else:
                f = lambda r: _np_local.exp(r)
            pred_px_h = px_prev.values * f(df["pred_hybrid"].values)
            pred_px_e = px_prev.values * f(df["pred_ensemble"].values)
            pred_px_hb = px_prev.values * f(df["pred_hybrid_blend"].values) if "pred_hybrid_blend" in df else None
            pred_px_eb = px_prev.values * f(df["pred_ensemble_blend"].values) if "pred_ensemble_blend" in df else None
            # Metrics
            def _pm(y_true, y_pred):
                y_true = _np_local.asarray(y_true)
                y_pred = _np_local.asarray(y_pred)
                mae = float(_np_local.mean(_np_local.abs(y_true - y_pred)))
                mse = float(_np_local.mean((y_true - y_pred) ** 2))
                rmse = float(_np_local.sqrt(mse))
                return {"mae": mae, "mse": mse, "rmse": rmse}
            pm_h = _pm(px.values, pred_px_h)
            pm_e = _pm(px.values, pred_px_e)
            pm_hb = _pm(px.values, pred_px_hb) if pred_px_hb is not None else None
            pm_eb = _pm(px.values, pred_px_eb) if pred_px_eb is not None else None
            price_metrics_obj = {"hybrid": pm_h, "ensemble": pm_e, "hybrid_blend": pm_hb, "ensemble_blend": pm_eb}
            # Plot
            _plt2.figure(figsize=(10,5))
            _plt2.plot(_np_local.asarray(df.index), _np_local.asarray(px.values), label="actual_price", alpha=0.7)
            _plt2.plot(_np_local.asarray(df.index), _np_local.asarray(pred_px_h), label="pred_price_hybrid", alpha=0.8)
            _plt2.plot(_np_local.asarray(df.index), _np_local.asarray(pred_px_e), label="pred_price_ensemble", alpha=0.8)
            if pred_px_hb is not None:
                _plt2.plot(_np_local.asarray(df.index), _np_local.asarray(pred_px_hb), label="pred_price_hybrid_blend", alpha=0.7)
            if pred_px_eb is not None:
                _plt2.plot(_np_local.asarray(df.index), _np_local.asarray(pred_px_eb), label="pred_price_ensemble_blend", alpha=0.7)
            _plt2.legend(); _plt2.title(stem + " prices (one-step)")
            _plt2.tight_layout(); _plt2.savefig(price_png_path)
        except Exception:  # noqa: BLE001
            price_metrics_obj = None

    # Confusion matrices (directional)
    conf_obj = None
    if confusion:
        try:
            import numpy as _np_c
            def _conf(y_true, y_pred):
                s_true = _np_c.sign(_np_c.asarray(y_true))
                s_pred = _np_c.sign(_np_c.asarray(y_pred))
                mask = s_true != 0
                s_true = s_true[mask]
                s_pred = s_pred[mask]
                tp = int(((s_true == 1) & (s_pred == 1)).sum())
                tn = int(((s_true == -1) & (s_pred == -1)).sum())
                fp = int(((s_true == -1) & (s_pred == 1)).sum())
                fn = int(((s_true == 1) & (s_pred == -1)).sum())
                total = int(len(s_true))
                acc = float((tp + tn) / total) if total else float('nan')
                return {"tp": tp, "tn": tn, "fp": fp, "fn": fn, "total": total, "acc": acc}
            conf_obj = {
                "hybrid": _conf(df["actual_return"], df["pred_hybrid"]),
                "ensemble": _conf(df["actual_return"], df["pred_ensemble"]),
            }
            if "pred_hybrid_blend" in df:
                conf_obj["hybrid_blend"] = _conf(df["actual_return"], df["pred_hybrid_blend"])
            if "pred_ensemble_blend" in df:
                conf_obj["ensemble_blend"] = _conf(df["actual_return"], df["pred_ensemble_blend"])
        except Exception:  # noqa: BLE001
            conf_obj = None

    # Rolling metrics plot (MAE and hit-rate)
    rolling_png_path = out_dir / f"{stem}_rolling.png"
    made_rolling = False
    if metrics_window and metrics_window > 1 and len(df) >= metrics_window:
        try:
            import numpy as _np_r
            import pandas as _pd_r
            import matplotlib.pyplot as _plt3
            err_h = _np_r.abs(df["actual_return"].to_numpy() - df["pred_hybrid"].to_numpy())
            err_e = _np_r.abs(df["actual_return"].to_numpy() - df["pred_ensemble"].to_numpy())
            mae_h = _pd_r.Series(err_h, index=df.index).rolling(metrics_window).mean()
            mae_e = _pd_r.Series(err_e, index=df.index).rolling(metrics_window).mean()
            # hit-rate rolling
            s_true = _np_r.sign(df["actual_return"].to_numpy())
            s_h = _np_r.sign(df["pred_hybrid"].to_numpy())
            s_e = _np_r.sign(df["pred_ensemble"].to_numpy())
            hit_h = _pd_r.Series((s_true == s_h).astype(float), index=df.index).rolling(metrics_window).mean()
            hit_e = _pd_r.Series((s_true == s_e).astype(float), index=df.index).rolling(metrics_window).mean()
            _plt3.figure(figsize=(10,6))
            _plt3.subplot(2,1,1)
            _plt3.plot(_np_r.asarray(mae_h.index), _np_r.asarray(mae_h.values), label="MAE hybrid"); _plt3.plot(_np_r.asarray(mae_e.index), _np_r.asarray(mae_e.values), label="MAE ensemble"); _plt3.legend(); _plt3.title(f"Rolling MAE (window={metrics_window})")
            _plt3.subplot(2,1,2)
            _plt3.plot(_np_r.asarray(hit_h.index), _np_r.asarray(hit_h.values), label="Hit-rate hybrid"); _plt3.plot(_np_r.asarray(hit_e.index), _np_r.asarray(hit_e.values), label="Hit-rate ensemble")
            if "pred_hybrid_blend" in df:
                s_hb = _np_r.sign(df["pred_hybrid_blend"].to_numpy())
                hit_hb = _pd_r.Series((s_true == s_hb).astype(float), index=df.index).rolling(metrics_window).mean()
                _plt3.plot(_np_r.asarray(hit_hb.index), _np_r.asarray(hit_hb.values), label="Hit-rate hybrid_blend")
            if "pred_ensemble_blend" in df:
                s_eb = _np_r.sign(df["pred_ensemble_blend"].to_numpy())
                hit_eb = _pd_r.Series((s_true == s_eb).astype(float), index=df.index).rolling(metrics_window).mean()
                _plt3.plot(_np_r.asarray(hit_eb.index), _np_r.asarray(hit_eb.values), label="Hit-rate ensemble_blend")
            _plt3.legend(); _plt3.title(f"Rolling hit-rate (window={metrics_window})")
            _plt3.tight_layout(); _plt3.savefig(rolling_png_path)
            made_rolling = True
        except Exception:  # noqa: BLE001
            made_rolling = False
    # Write metrics JSON once
    try:
        import json as _json_local
        with open(metrics_path, "w", encoding="utf-8") as f:
            _json_local.dump({
                "hybrid": m_h,
                "ensemble": m_e,
                "hybrid_blend": m_hb,
                "ensemble_blend": m_eb,
                "price": price_metrics_obj,
                "confusion": conf_obj,
            }, f, indent=2)
    except Exception:  # noqa: BLE001
        pass
    # Plot
    try:
        import matplotlib.pyplot as _plt
        _plt.figure(figsize=(10, 5))
        _plt.plot(_np.asarray(df.index), _np.asarray(df["actual_return"].values), label="actual_return", alpha=0.7)
        _plt.plot(_np.asarray(df.index), _np.asarray(df["pred_hybrid"].values), label="pred_hybrid", alpha=0.8)
        _plt.plot(_np.asarray(df.index), _np.asarray(df["pred_ensemble"].values), label="pred_ensemble", alpha=0.8)
        if "pred_hybrid_blend" in df:
            _plt.plot(_np.asarray(df.index), _np.asarray(df["pred_hybrid_blend"].values), label="pred_hybrid_blend", alpha=0.7)
        if "pred_ensemble_blend" in df:
            _plt.plot(_np.asarray(df.index), _np.asarray(df["pred_ensemble_blend"].values), label="pred_ensemble_blend", alpha=0.7)
        _plt.legend(); _plt.title(stem)
        _plt.tight_layout(); _plt.savefig(png_path)
        made_png = True
    except Exception as exc:  # noqa: BLE001
        made_png = False
    typer.echo(_json.dumps({
        "ok": True,
        "rows": int(len(df)),
        "csv": str(csv_path),
        "png": str(png_path) if made_png else None,
        "rolling_png": str(rolling_png_path) if made_rolling else None,
        "metrics": {"hybrid": m_h, "ensemble": m_e, "hybrid_blend": m_hb, "ensemble_blend": m_eb},
        "metrics_price": price_metrics_obj,
        "confusion": conf_obj,
        "meta": {"window": window, "horizon": horizon, "normalize": normalize, "n_lags": int(n_lags or 0), "sentiment": {"csv": sentiment_csv, "backend": sentiment_backend, "weight": float(sentiment_weight), "as_feature": bool(sentiment_as_feature), "ffill_seconds": sentiment_ffill_seconds, "half_life": sentiment_half_life, "resample": sentiment_resample}},
    }, indent=2))


@app.command(name="sentiment_score")
def sentiment_score_cli(
    text: str = typer.Argument(..., help="Text to score"),
    backend: str = typer.Option("finbert", help="finbert|openai"),
):
    """Score a single text snippet and print JSON with score,label,backend."""
    import json as _json
    if not _HAS_SENT:
        typer.echo(_json.dumps({"ok": False, "error": "sentiment module unavailable"}, indent=2)); raise typer.Exit(1)
    settings = Settings()
    api_key = settings.openai_api_key if backend.lower() == "openai" else None
    try:
        res = _score_text(text, backend=backend, api_key=api_key)
        typer.echo(_json.dumps({"ok": True, **res.as_dict()}, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(_json.dumps({"ok": False, "error": str(exc)}, indent=2)); raise typer.Exit(1)


@app.command(name="sentiment_from_csv")
def sentiment_from_csv_cli(
    csv_path: str = typer.Argument(..., help="CSV with datetime,text columns"),
    backend: str = typer.Option("finbert", help="finbert|openai"),
    out_path: str = typer.Option("logs/sentiment_scored.csv", help="Output CSV path with added score,label"),
):
    """Batch score sentiment for a CSV of news/headlines tweets.

    Expects columns: datetime,text. Datetime will be parsed and preserved.
    """
    import pandas as _pd
    import json as _json
    from pathlib import Path as _P
    if not _P(csv_path).exists():
        typer.echo(_json.dumps({"ok": False, "error": f"not found: {csv_path}"}, indent=2)); raise typer.Exit(1)
    if not _HAS_SENT:
        typer.echo(_json.dumps({"ok": False, "error": "sentiment module unavailable"}, indent=2)); raise typer.Exit(1)
    df = _pd.read_csv(csv_path)
    if "text" not in df.columns:
        typer.echo(_json.dumps({"ok": False, "error": "expected 'text' column"}, indent=2)); raise typer.Exit(1)
    settings = Settings()
    api_key = settings.openai_api_key if backend.lower() == "openai" else None
    scores = []
    labels = []
    for txt in df["text"].astype(str).tolist():
        try:
            r = _score_text(txt, backend=backend, api_key=api_key)
            scores.append(float(r.score)); labels.append(r.label)
        except Exception:
            scores.append(0.0); labels.append("neutral")
    df["score"] = scores; df["label"] = labels
    _P(out_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    typer.echo(_json.dumps({"ok": True, "rows": int(len(df)), "out": out_path}, indent=2))


@app.command(name="train_hybrid_bars")
def train_hybrid_bars_cli(
    symbol: str = typer.Argument(..., help="Symbol, e.g., SPX"),
    interval: float = typer.Argument(..., help="Bar interval seconds, e.g., 1.0"),
    start_date: str = typer.Option(..., help="Start date YYYY-MM-DD"),
    end_date: str = typer.Option(..., help="End date YYYY-MM-DD"),
    window: int = typer.Option(64, help="Sliding window length"),
    horizon: int = typer.Option(1, help="Forecast horizon (steps)"),
    epochs: int = typer.Option(3, help="Training epochs"),
    lr: float = typer.Option(1e-3, help="Learning rate"),
    batch_size: int = typer.Option(64, help="Batch size"),
    out_path: str = typer.Option("models/hybrid_bars.ckpt", help="Checkpoint path"),
    prefer_compact: bool = typer.Option(True, help="Prefer bars_compact dataset if present"),
    normalize: str = typer.Option("log_returns", help="Return type: log_returns|returns"),
    device: str = typer.Option("auto", help="cpu|cuda|auto"),
    precision: str = typer.Option("auto", help="fp32|amp|bf16|auto"),
):
    """Train Hybrid GARCH+DL on close prices from bars.

    It computes returns and GARCH volatility as features and trains a small LSTM to predict next-step returns.
    """
    import json as _json
    from pathlib import Path as _Path
    if not _HAS_HYBRID:
        typer.echo(_json.dumps({"ok": False, "error": "hybrid model unavailable; ensure arch and torch are installed"}, indent=2)); raise typer.Exit(1)
    settings = Settings()
    series_pd = _load_bars_series(settings.data_dir or "data", symbol, interval, start_date, end_date, field="close", prefer_compact=prefer_compact)
    if len(series_pd) < (window + horizon + 10):
        typer.echo(_json.dumps({"ok": False, "error": "not enough bars for given window/horizon"}, indent=2)); raise typer.Exit(1)
    # Build features
    r, sigma = _hy_build_feats(series_pd, normalize=normalize or "log_returns")
    dev = _pick_device(_opt_val(device))
    prec = _opt_val(precision, "auto")
    cfg = _HyCfg(window=window, horizon=horizon, batch_size=batch_size, epochs=epochs, lr=lr, device=dev, precision=prec)
    model, meta = _hy_train(r, sigma, cfg, normalize=normalize or "log_returns")
    # enrich meta with source
    meta.update({
        "source": "bars",
        "symbol": symbol,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
        "prefer_compact": bool(prefer_compact),
        "rows_prices": int(len(series_pd)),
        "rows_used": int(len(r)),
    })
    _Path("models").mkdir(parents=True, exist_ok=True)
    _hy_save(out_path, model, meta)
    typer.echo(_json.dumps({"ok": True, "rows": int(len(r)), "ckpt": out_path, "meta": meta}, indent=2))


@app.command(name="predict_hybrid_bars")
def predict_hybrid_bars_cli(
    ckpt: str = typer.Option("models/hybrid_bars.ckpt", help="Checkpoint path"),
    symbol: str = typer.Argument(..., help="Symbol, e.g., SPX"),
    interval: float = typer.Argument(..., help="Bar interval seconds"),
    start_date: str = typer.Option(..., help="Start date YYYY-MM-DD"),
    end_date: str = typer.Option(..., help="End date YYYY-MM-DD"),
    window: int = typer.Option(64, help="Window length"),
    horizon: int = typer.Option(1, help="Forecast horizon"),
    prefer_compact: bool = typer.Option(True, help="Prefer bars_compact dataset if present"),
    normalize: str = typer.Option("log_returns", help="Return type: log_returns|returns"),
    enforce_meta: bool = typer.Option(True, help="Require checkpoint metadata to match window/horizon/normalize"),
    device: str = typer.Option("auto", help="cpu|cuda|auto"),
):
    """Predict next-step returns using Hybrid GARCH+DL over bars data (uses last window in range)."""
    import json as _json
    from pathlib import Path as _Path
    if not _HAS_HYBRID:
        typer.echo(_json.dumps({"ok": False, "error": "hybrid model unavailable"}, indent=2)); raise typer.Exit(1)
    if not _Path(ckpt).exists():
        typer.echo(_json.dumps({"ok": False, "error": f"checkpoint not found: {ckpt}"}, indent=2)); raise typer.Exit(1)
    model, meta = _hy_load(ckpt)
    dev = _pick_device(_opt_val(device))
    try:
        model = model.to(dev)
    except Exception:
        pass
    if enforce_meta and meta is not None:
        mismatches = {}
        if int(meta.get("window", window)) != int(window):
            mismatches["window"] = {"ckpt": meta.get("window"), "arg": window}
        if int(meta.get("horizon", horizon)) != int(horizon):
            mismatches["horizon"] = {"ckpt": meta.get("horizon"), "arg": horizon}
        ck_norm = str(meta.get("normalize", "log_returns")).lower()
        if ck_norm != (normalize or "log_returns").lower():
            mismatches["normalize"] = {"ckpt": ck_norm, "arg": normalize}
        if mismatches:
            typer.echo(_json.dumps({"ok": False, "error": "metadata mismatch", "mismatches": mismatches, "meta": meta}, indent=2))
            raise typer.Exit(1)
    series_pd = _load_bars_series(Settings().data_dir or "data", symbol, interval, start_date, end_date, field="close", prefer_compact=prefer_compact)
    r, sigma = _hy_build_feats(series_pd, normalize=normalize or "log_returns")
    if len(r) < (window + horizon):
        typer.echo(_json.dumps({"ok": False, "error": "not enough bars for given window/horizon"}, indent=2)); raise typer.Exit(1)
    yhat = _hy_predict(model, r, sigma, window=window, horizon=horizon)
    typer.echo(_json.dumps({"ok": True, "pred": [float(v) for v in yhat.reshape(-1)], "rows": int(len(r)), "meta": meta}, indent=2))


@app.command(name="live_bars_test")
def live_bars_test_cli(
    symbol: str = typer.Option("SPX", help="Symbol to stream from IQFeed (e.g., SPX)"),
    interval: float = typer.Option(1.0, help="Bar interval seconds, e.g., 1.0"),
    minutes: int = typer.Option(65, help="Duration to run in minutes"),
    window: int = typer.Option(64, help="Prediction window length"),
    horizon: int = typer.Option(1, help="Forecast horizon"),
    device: str = typer.Option("auto", help="Compute device: auto|cpu|cuda|dml (DirectML)"),
    use_hybrid: bool = typer.Option(True, help="Run Hybrid (GARCH+DL) predictions"),
    use_ensemble: bool = typer.Option(False, help="Run Ensemble predictions"),
    hybrid_ckpt: str = typer.Option("models/hybrid_bars.ckpt", help="Hybrid checkpoint; auto-train if missing and sufficient data"),
    ensemble_ckpt: str = typer.Option("models/ensemble_bars.ckpt", help="Ensemble checkpoint; auto-train if missing and sufficient data"),
    out_stem: str = typer.Option(None, help="Output stem for artifacts (defaults to live_{symbol}_{interval}s_{YYYYmmdd_HHMM})"),
    # Live sentiment options
    use_sentiment: bool = typer.Option(False, help="Include live sentiment as extra feature (to match hybrid checkpoints with extra features)"),
    sentiment_feed: str = typer.Option(None, help="CSV file that is appended with live text; expects columns ts,text or timestamp,text"),
    sentiment_backend: str = typer.Option("finbert", help="Sentiment backend: finbert|openai"),
    sentiment_ffill_seconds: int = typer.Option(300, help="Carry last sentiment forward up to this many seconds"),
    sentiment_half_life: int = typer.Option(180, help="Half-life in seconds for exponential decay of last sentiment (0 to disable)"),
    n_lags_override: int = typer.Option(-1, help="Override hybrid n_lags for extra feature; default uses checkpoint meta if available"),
    # Online adaptation options (continual learning)
    online_adapt: bool = typer.Option(False, help="Enable online/continual adaptation of the Hybrid model during the run"),
    adapt_interval_seconds: float = typer.Option(30.0, help="How often to run a tiny adaptation step (seconds)"),
    adapt_window: int = typer.Option(512, help="Number of recent bars to build the adaptation dataset from"),
    adapt_steps: int = typer.Option(1, help="Gradient steps per adaptation event (keep small)"),
    adapt_lr: float = typer.Option(1e-4, help="Learning rate for online adaptation"),
    adapt_freeze_body: bool = typer.Option(True, help="If set, only fine-tune the output head; keeps LSTM body frozen"),
    adapt_grad_clip: float = typer.Option(1.0, help="Clip gradient norm to this value during adaptation (0 to disable)"),
    adapt_guard_factor: float = typer.Option(1.5, help="Guard rail: if post-adapt loss > factor * pre-loss, roll back weights"),
    adapt_log_jsonl: str = typer.Option("logs/live_adapt.jsonl", help="Write per-adaptation metrics to this JSONL file (set empty to disable)"),
    adapt_ckpt_minutes: float = typer.Option(10.0, help="Every N minutes, save a checkpoint of the adapted Hybrid model (<=0 to disable)"),
    adapt_ckpt_dir: str = typer.Option("models", help="Directory to save periodic adapted checkpoints"),
    # Exogenous / cross-asset options
    exo_csv: str = typer.Option(None, "--exo-csv", help="Comma-separated path(s) to exogenous CSVs"),
    exo_resample: str = typer.Option(None, help="Resample rule for exogenous series (e.g., 'S','5S','1min')"),
    exo_ffill_seconds: int = typer.Option(300, help="Forward-fill cap in seconds for exogenous alignment"),
    exo_half_life: float = typer.Option(0.0, help="Half-life (seconds) for exponential decay of exogenous values (0 to disable)"),
    exo_method: str = typer.Option("zsum", help="Combine method for multiple exogenous series: zsum|wsum"),
    exo_weights: str = typer.Option(None, help="JSON mapping name->weight when using wsum"),
    exo_as_feature: bool = typer.Option(False, help="Attempt to include exogenous composite as extra feature (requires checkpoint trained with extra feature)"),
    exo_weight_blend: float = typer.Option(0.0, help="If >0, blend hybrid prediction with exogenous composite: y = y + w * exo"),
):
    """Stream live IQFeed Level1 quotes, aggregate to bars, and produce rolling predictions for a fixed duration.

    Requirements: IQFeed Windows client running locally with Level1 permissions.
    Outputs: logs CSVs for bars and predictions, plus a JSON summary when done.
    """
    import json as _json
    import time as _time
    from pathlib import Path as _P
    import pandas as _pd
    import numpy as _np

    # Live feed preparation
    cfg = IQFeedConfig.from_env()
    from .aggregation.bar_builder import TimeBarAggregator
    from .ingestion.iqfeed_level1_writer import Level1BatchWriter, BatchConfig

    # Prepare output naming
    from datetime import datetime as _dt
    ts_tag = _dt.now().strftime("%Y%m%d_%H%M")
    # Create a clean interval tag (e.g., 1s, 5s, 0_5s)
    try:
        _ival = float(interval)
        if abs(_ival - int(_ival)) < 1e-9:
            interval_tag = f"{int(_ival)}s"
        else:
            # Use underscore as decimal separator to avoid filesystem issues
            interval_tag = (f"{_ival:.3f}".rstrip('0').rstrip('.')).replace('.', '_') + "s"
    except Exception:
        interval_tag = f"{interval}s"
    stem = out_stem or f"live_{symbol}_{interval_tag}_{ts_tag}"
    logs_dir = _P("logs"); logs_dir.mkdir(parents=True, exist_ok=True)
    bars_csv = logs_dir / f"{stem}_bars.csv"
    preds_csv = logs_dir / f"{stem}_preds.csv"

    # Storage and state
    bars: list[dict] = []
    preds: list[dict] = []
    series_close = _pd.Series(dtype=float)

    # Optional models
    model_h = None; meta_h = None
    model_e = None
    if use_hybrid and _HAS_HYBRID:
        try:
            if _P(hybrid_ckpt).exists():
                model_h, meta_h = _hy_load(hybrid_ckpt)
                # Use robust device selection
                dev_h = _pick_device(device)
                try:
                    model_h = model_h.to(dev_h)
                except Exception:
                    pass
        except Exception:
            model_h = None; meta_h = None
    if use_ensemble and _HAS_TORCH:
        try:
            from .models.ensemble_vae_lstm_transformer import EnsembleModel as _Ens
            model_e = _Ens(window=window, horizon=horizon)
            if _P(ensemble_ckpt).exists():
                import torch as _torch
                loaded = _torch.load(ensemble_ckpt, map_location="cpu")
                model_e.load_state_dict(loaded.get("state_dict", loaded))
                # Use robust device selection
                dev_e = _pick_device(device)
                try:
                    model_e = model_e.to(dev_e)
                except Exception:
                    pass
                model_e.eval()
            else:
                model_e = None
        except Exception:
            model_e = None

    # Setup aggregator and writer
    writer = Level1BatchWriter(BatchConfig(base_dir=_P(Settings().data_dir or "data")))
    agg = TimeBarAggregator(interval_sec=float(interval))

    end_time = _time.time() + float(minutes) * 60.0
    # Live sentiment cache (times and scores), maintained by polling a CSV feed
    from bisect import bisect_right as _bisect_right
    sent_times: list[float] = []  # epoch seconds (float)
    sent_scores: list[float] = []
    sent_seen: set[str] = set()
    next_sent_poll: float = 0.0

    def _poll_sentiment_feed():
        nonlocal sent_times, sent_scores, sent_seen
        if not (use_sentiment and sentiment_feed and _HAS_SENT):
            return
        try:
            p = _P(sentiment_feed)
            if not p.exists():
                return
            df = _pd.read_csv(p)
            if df.empty:
                return
            # Identify columns
            text_col = None
            for c in ("text", "headline", "title", "content"):
                if c in df.columns:
                    text_col = c; break
            t_col = None
            for c in ("ts", "timestamp", "time", "datetime", "date"):
                if c in df.columns:
                    t_col = c; break
            if text_col is None or t_col is None:
                return
            for _, row in df.iterrows():
                try:
                    txt = str(row[text_col])
                    if not txt or txt.strip() == "" or txt.strip().lower() == "nan":
                        continue
                    ts_raw = row[t_col]
                    # Parse timestamp
                    t = None
                    try:
                        # If already numeric-like (int/float) treat as epoch seconds
                        if isinstance(ts_raw, (int, float)):
                            t = _pd.to_datetime(float(ts_raw), unit="s", utc=True)
                        else:
                            s = str(ts_raw).strip()
                            # numeric string? allow integer/float epoch seconds
                            if s.replace('.', '', 1).isdigit():
                                t = _pd.to_datetime(float(s), unit="s", utc=True)
                            else:
                                t = _pd.to_datetime(s, utc=True, errors="coerce")
                    except Exception:
                        t = _pd.NaT
                    if t is None or _pd.isna(t):
                        continue
                    key = f"{t.value}_{hash(txt)}"
                    if key in sent_seen:
                        continue
                    sent_seen.add(key)
                    # Score
                    try:
                        res = _score_text(txt, backend=sentiment_backend)
                        sc_raw = (res.get("score") if isinstance(res, dict) else getattr(res, "score", None))
                        if sc_raw is None or _pd.isna(sc_raw):
                            continue
                        sc = float(sc_raw)
                    except Exception as _e:
                        LOGGER.debug("sentiment score failed: %s", _e)
                        continue
                    sent_times.append(t.timestamp())
                    sent_scores.append(sc)
                except Exception as _e:
                    LOGGER.debug("sentiment row parse failed: %s", _e)
                    continue
            # Keep only recent items (last ~5000) to bound memory
            if len(sent_times) > 5000:
                sent_times = sent_times[-5000:]
                sent_scores = sent_scores[-5000:]
        except Exception as _e:
            LOGGER.debug("poll_sentiment_feed error: %s", _e)

    def _align_sentiment(index: _pd.Index) -> _pd.Series:
        # Returns decayed, forward-filled sentiment values aligned to index
        if not (use_sentiment and sent_times and sent_scores):
            return _pd.Series(index=index, dtype=float)
        out_vals: list[float | None] = []
        fcap = max(0, int(sentiment_ffill_seconds or 0))
        hl = max(0, int(sentiment_half_life or 0))
        for t in index:
            ts = t.timestamp()
            pos = _bisect_right(sent_times, ts)
            if pos <= 0:
                out_vals.append(None); continue
            t0 = sent_times[pos - 1]
            age = ts - t0
            if fcap and age > fcap:
                out_vals.append(None); continue
            sc = sent_scores[pos - 1]
            if hl > 0 and age > 0:
                # exponential decay by half-life
                import math as _math
                sc = sc * _math.exp(-_math.log(2) * age / hl)
            out_vals.append(sc)
        s = _pd.Series(out_vals, index=index, dtype=float)
        return s
    # Message handler pushes to writer and aggregator
    def _on_tick(msg: dict):
        nonlocal series_close
        try:
            writer.add(msg)
            completed = agg.add_tick(msg)
            if not completed:
                return
            # Append completed bars and attempt predictions
            for bar in completed:
                row = {
                    "symbol": bar.symbol,
                    "start_ts": bar.start_ts,
                    "end_ts": bar.end_ts,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "trades": bar.trades,
                }
                bars.append(row)
                # Update close series
                idx = _pd.to_datetime(int(bar.end_ts), unit="s")
                # append price using concat to avoid dtype/index warnings
                try:
                    series_close = _pd.concat([series_close, _pd.Series([bar.close], index=[idx])])
                except Exception as _e_series:
                    LOGGER.debug("series_close concat failed: %s", _e_series)
                    return
                # Make predictions (on last window)
                if len(series_close) >= (window + horizon + 1):
                    try:
                        last_px = series_close.tail(max(600, window + horizon + 10))
                        # Hybrid prediction
                        y_h = None
                        x_feat_val = None
                        # Build exogenous composite if configured
                        exo_val = None
                        exo_series = None
                        _exo_aligned_cache = None
                        # Build list of exogenous CSVs from flag or policy
                        exo_paths = [p.strip() for p in (exo_csv or '').split(',') if p.strip()]
                        if not exo_paths:
                            # Try policy auto-load: config/exo_policy.json
                            try:
                                import json as _json_exo
                                from pathlib import Path as _PathExo
                                pol_path = _PathExo("config") / "exo_policy.json"
                                if pol_path.exists():
                                    pol = _json_exo.loads(pol_path.read_text(encoding="utf-8"))
                                    key = f"{symbol}@{int(interval)}s" if isinstance(interval, (int, float)) else f"{symbol}@{interval}"
                                    prof = pol.get(key) or pol.get("default") or {}
                                    exo_paths = list(prof.get("paths", []) or [])
                                    if not exo_resample and prof.get("resample"):
                                        exo_resample = prof.get("resample")
                                    if (exo_ffill_seconds is None or exo_ffill_seconds == 0) and (prof.get("ffill_seconds") is not None):
                                        exo_ffill_seconds = int(prof.get("ffill_seconds"))  # type: ignore[arg-type]
                                    if (exo_half_life is None or exo_half_life == 0) and (prof.get("half_life") is not None):
                                        exo_half_life = float(prof.get("half_life"))  # type: ignore[arg-type]
                                    if not exo_method and prof.get("method"):
                                        exo_method = prof.get("method")
                                    if not exo_weights and prof.get("weights"):
                                        exo_weights = _json_exo.dumps(prof.get("weights"))
                            except Exception as _e_exopol:
                                LOGGER.debug("exo policy load failed: %s", _e_exopol)
                        if exo_paths:
                            try:
                                from .features.exogenous import read_series_csv as _read_exo, align_series_to_index as _align_exo, combine_signals as _combine_exo
                                exo_map = {}
                                for path in exo_paths:
                                    try:
                                        s = _read_exo(path)
                                        if not s.empty:
                                            exo_map[path] = s
                                    except Exception as _e_ex:
                                        LOGGER.debug("exo read failed %s: %s", path, _e_ex)
                                if exo_map:
                                    # Align each to last_px index
                                    aligned = {k: _align_exo(v, _pd.DatetimeIndex(last_px.index), resample=exo_resample, ffill_seconds=exo_ffill_seconds, half_life=exo_half_life) for k, v in exo_map.items()}
                                    weights = None
                                    if (exo_method or "").lower() == "wsum" and exo_weights:
                                        try:
                                            import json as _json_w
                                            weights = _json_w.loads(exo_weights)
                                        except Exception:
                                            weights = None
                                    exo_series, _ = _combine_exo(aligned, method=(exo_method or "zsum").lower(), weights=weights, target_index=last_px.index)  # type: ignore[arg-type]
                                    exo_val = float(exo_series.iloc[-1]) if len(exo_series) else None
                            except Exception as _e_exo:
                                LOGGER.debug("exogenous processing failed: %s", _e_exo)
                        if model_h is not None:
                            r_h, sigma = _hy_build_feats(last_px, normalize="log_returns")
                            extra_feat = None
                            n_lags = 0
                            # Assemble composite extra feature from sentiment and/or exogenous, if checkpoint supports it
                            if (use_sentiment or exo_as_feature) and (meta_h and isinstance(meta_h, dict)):
                                # Adopt n_lags from meta or override
                                n_lags = int(meta_h.get("n_lags", 0)) if meta_h else 0
                                if n_lags_override is not None and int(n_lags_override) >= 0:
                                    n_lags = int(n_lags_override)
                                # Compute expected input size with and without extra feature
                                base_feats = 1 + 2 * int(max(0, n_lags))  # sigma + r_lags + sigma_lags
                                want_extra = False
                                comp_series = None
                                # Build sentiment aligned series if requested
                                sent_series = None
                                if use_sentiment:
                                    xs = _align_sentiment(_pd.DatetimeIndex(last_px.index))
                                    sent_series = xs
                                    x_feat_val = float(xs.iloc[-1]) if _pd.notna(xs.iloc[-1]) else None
                                # Combine with exo if available
                                if exo_series is not None and (use_sentiment and sent_series is not None):
                                    try:
                                        import numpy as _np_comb
                                        # z-score both and sum for a single composite
                                        a = sent_series.to_numpy(dtype=float)
                                        b = exo_series.to_numpy(dtype=float)
                                        az = (a - _np_comb.nanmean(a)) / (_np_comb.nanstd(a) + 1e-12)
                                        bz = (b - _np_comb.nanmean(b)) / (_np_comb.nanstd(b) + 1e-12)
                                        comp = _np_comb.nanmean(_np_comb.column_stack([az, bz]), axis=1)
                                        comp_series = _pd.Series(comp, index=last_px.index)
                                    except Exception:
                                        comp_series = sent_series
                                elif sent_series is not None:
                                    comp_series = sent_series
                                elif exo_series is not None:
                                    comp_series = exo_series
                                # Decide if we can include as feature
                                if comp_series is not None:
                                    expect_inp = base_feats + (1 + int(max(0, n_lags)))  # + x and its lags
                                    in_sz = int(meta_h.get("input_size", base_feats))
                                    if int(in_sz) == int(expect_inp):
                                        extra_feat = comp_series
                                        want_extra = True
                                    else:
                                        want_extra = False
                            if len(r_h) >= (window + horizon):
                                y_h = _hy_predict(model_h, r_h, sigma, window=window, horizon=horizon, extra_feat=extra_feat, n_lags=n_lags)
                                # Optional exogenous blend on output if not used as feature
                                if y_h is not None and exo_weight_blend and exo_val is not None and not (extra_feat is not None):
                                    try:
                                        import numpy as _np_bl
                                        y_arr = y_h.reshape(-1)
                                        y_arr[-1] = float(y_arr[-1]) + float(exo_weight_blend) * float(exo_val)
                                        y_h = y_arr
                                    except Exception:
                                        pass
                        # Ensemble prediction
                        y_e = None
                        if model_e is not None and _HAS_TORCH:
                            import torch as _torch
                            series_norm = _normalize_series(last_px, mode="log_returns") if _HAS_PREPROC else last_px
                            s = _torch.tensor(series_norm.values, dtype=_torch.float32)
                            if len(s) >= (window + horizon):
                                from .models.ensemble_vae_lstm_transformer import predict_ensemble as _pred_ens
                                try:
                                    dev_e = next(model_e.parameters()).device
                                    s = s.to(dev_e)
                                except Exception:
                                    pass
                                y_e = _pred_ens(model_e, s, window=window, horizon=horizon)
                        preds.append({
                            "ts": idx.isoformat(),
                            "pred_hybrid": float(y_h.reshape(-1)[-1]) if (y_h is not None) else None,
                            "pred_ensemble": float(y_e.view(-1)[-1]) if (y_e is not None) else None,
                            "sentiment": x_feat_val,
                            "exo": exo_val,
                            "pred_hybrid_exo_blend": (float(y_h.reshape(-1)[-1]) + float(exo_weight_blend) * float(exo_val)) if (y_h is not None and exo_weight_blend and exo_val is not None and extra_feat is None) else None,
                        })
                    except Exception as _e_pred:
                        LOGGER.debug("prediction step failed: %s", _e_pred)
                        return
        except Exception as _e_tick:
            LOGGER.debug("_on_tick error: %s", _e_tick)
            return

    # Online adaptation state
    last_adapt = 0.0
    last_ckpt_time = 0.0
    model_h_opt = None
    def _maybe_adapt(now_ts: float):  # light continual learning for Hybrid
        nonlocal model_h_opt, last_adapt, last_ckpt_time
        if not (online_adapt and model_h is not None):
            return
        if (now_ts - last_adapt) < float(adapt_interval_seconds):
            return
        # Need enough data
        if len(series_close) < max(int(adapt_window), int(window) + int(horizon) + 5):
            return
        try:
            import torch as _torch
            from torch.utils.data import DataLoader as _DL
            # Build recent slice
            px = series_close.tail(int(adapt_window) + int(window) + int(horizon) + 5)
            r_ad, s_ad = _hy_build_feats(px, normalize="log_returns")
            # Extra feature alignment if enabled
            extra_feat = None
            n_lags = 0
            if use_sentiment:
                xs = _align_sentiment(px.index)
                extra_feat = xs
                if meta_h and isinstance(meta_h, dict):
                    n_lags = int(meta_h.get("n_lags", 0))
                if n_lags_override is not None and int(n_lags_override) >= 0:
                    n_lags = int(n_lags_override)
            # Build dataset
            try:
                from .models.hybrid_garch_dl import make_dataset_from_returns as _hy_ds
            except Exception:
                # Fallback: minimal dataset using last window only (very small update)
                _torch.nn.utils.clip_grad_norm_ if False else None  # no-op to satisfy linter
                return
            ds, _, _ = _hy_ds(r_ad, s_ad, window=int(window), horizon=int(horizon), extra_feat=extra_feat, n_lags=int(n_lags))
            if len(ds) <= 2:
                return
            dl = _DL(ds, batch_size=32, shuffle=True)
            loss_fn = None
            # Freeze body if requested
            if adapt_freeze_body:
                try:
                    for p in getattr(model_h, "rnn", model_h).parameters():
                        p.requires_grad = False
                except Exception:
                    pass
            # Optimizer
            if model_h_opt is None:
                model_h_opt = _torch.optim.Adam(filter(lambda p: p.requires_grad, model_h.parameters()), lr=float(adapt_lr))
            # Snapshot weights for potential rollback
            snapshot = {k: v.clone().detach() for k, v in model_h.state_dict().items()}
            # Pre-loss on small sample
            def _eval_loss(max_batches: int = 2) -> float:
                if model_h is not None:
                    model_h.eval()
                lf = _torch.nn.MSELoss()
                losses = []
                with _torch.no_grad():
                    for bi, (xb, yb) in enumerate(dl):
                        if bi >= max_batches:
                            break
                        try:
                            dev_h = next(model_h.parameters()).device
                            xb = xb.to(dev_h, non_blocking=True)
                            yb = yb.to(dev_h, non_blocking=True)
                        except Exception:
                            pass
                        pred = model_h(xb)
                        losses.append(float(lf(pred, yb)))
                return float(sum(losses) / max(1, len(losses)))
            pre_loss = _eval_loss()
            # Train small steps
            model_h.train()
            lf = _torch.nn.MSELoss()
            steps_done = 0
            for epoch in range(int(max(1, adapt_steps))):
                for xb, yb in dl:
                    try:
                        dev_h = next(model_h.parameters()).device
                        xb = xb.to(dev_h, non_blocking=True)
                        yb = yb.to(dev_h, non_blocking=True)
                    except Exception:
                        pass
                    model_h_opt.zero_grad()
                    pred = model_h(xb)
                    loss = lf(pred, yb)
                    loss.backward()
                    try:
                        if float(adapt_grad_clip) > 0:
                            _torch.nn.utils.clip_grad_norm_(model_h.parameters(), float(adapt_grad_clip))
                    except Exception:
                        pass
                    model_h_opt.step()
                    steps_done += 1
                    if steps_done >= int(adapt_steps):
                        break
                if steps_done >= int(adapt_steps):
                    break
            post_loss = _eval_loss()
            # Guard rail: roll back if degraded too much
            action = "kept"
            if pre_loss > 0 and post_loss > pre_loss * float(adapt_guard_factor):
                try:
                    # Roll back weights
                    model_h.load_state_dict(snapshot)
                    # Recreate optimizer to clear momentum from bad step
                    model_h_opt = _torch.optim.Adam(filter(lambda p: p.requires_grad, model_h.parameters()), lr=float(adapt_lr))
                    action = "rollback"
                except Exception:
                    action = "error"
            # Unfreeze (restore) for next forward
            if adapt_freeze_body:
                try:
                    for p in getattr(model_h, "rnn", model_h).parameters():
                        p.requires_grad = True
                except Exception:
                    pass
            # Log adaptation event
            try:
                if adapt_log_jsonl:
                    from datetime import datetime as _dt, timezone as _tz
                    log_path = _P(adapt_log_jsonl)
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    payload = {
                        "ts": _dt.now(_tz.utc).isoformat(),
                        "symbol": symbol,
                        "window": int(window),
                        "horizon": int(horizon),
                        "n_lags": int(n_lags) if 'n_lags' in locals() else 0,
                        "use_sentiment": bool(use_sentiment),
                        "pre_loss": float(pre_loss),
                        "post_loss": float(post_loss),
                        "steps": int(steps_done),
                        "action": action,
                    }
                    with log_path.open("a", encoding="utf-8") as f:
                        f.write(_json.dumps(payload) + "\n")
            except Exception as _e_log:
                LOGGER.debug("adapt log failed: %s", _e_log)
            # Periodic checkpoint save
            try:
                if float(adapt_ckpt_minutes) > 0:
                    now = _time.time()
                    if (now - last_ckpt_time) >= float(adapt_ckpt_minutes) * 60.0:
                        ckdir = _P(adapt_ckpt_dir); ckdir.mkdir(parents=True, exist_ok=True)
                        from datetime import datetime as _dt
                        ts_tag = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
                        ck_path = ckdir / f"hybrid_live_adapt_{symbol}_{str(interval).replace('.', '')}s_{ts_tag}.ckpt"
                        try:
                            # Derive meta for save
                            meta = dict(meta_h) if isinstance(meta_h, dict) else {}
                            meta.update({
                                "window": int(window),
                                "horizon": int(horizon),
                                "normalize": meta.get("normalize", "log_returns"),
                                "n_lags": int(n_lags) if 'n_lags' in locals() else int(meta.get("n_lags", 0)),
                                "source": "live_adapt",
                                "symbol": symbol,
                                "interval": float(interval),
                            })
                        except Exception:
                            meta = {"window": int(window), "horizon": int(horizon), "normalize": "log_returns", "n_lags": int(n_lags) if 'n_lags' in locals() else 0, "source": "live_adapt", "symbol": symbol, "interval": float(interval)}
                        _hy_save(str(ck_path), model_h, meta)
                        last_ckpt_time = now
            except Exception as _e_ck:
                LOGGER.debug("adapt checkpoint save failed: %s", _e_ck)
            # Mark adapt time
            last_adapt = now_ts
        except Exception as _e_adapt:
            LOGGER.debug("online adapt failed: %s", _e_adapt)
            return

    # Start stream
    try:
        stream = IQFeedLevel1Stream(cfg, on_message=_on_tick)
        stream.start([symbol])
    except Exception as exc:
        typer.echo(_json.dumps({"ok": False, "error": f"Failed to start IQFeed stream: {exc}"}, indent=2))
        raise typer.Exit(1)

    # Main loop
    try:
        while _time.time() < end_time:
            # Pump queue passively via callback; finalize overdue bars proactively
            _time.sleep(0.25)
            # Poll sentiment feed periodically (~every 2s)
            if use_sentiment and _time.time() >= next_sent_poll:
                _poll_sentiment_feed()
                next_sent_poll = _time.time() + 2.0
            try:
                _ = agg.finalize_until(_time.time())
            except Exception:
                pass
            # Online adapt periodically
            if online_adapt:
                _maybe_adapt(_time.time())
    finally:
        try:
            stream.stop()
        except Exception:
            pass
        # Final flushes
        try:
            writer.flush()
        except Exception:
            pass
        try:
            for b in agg.flush():
                bars.append({
                    "symbol": b.symbol,
                    "start_ts": b.start_ts,
                    "end_ts": b.end_ts,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                    "trades": b.trades,
                })
        except Exception:
            pass

    # Save outputs
    try:
        if bars:
            _pd.DataFrame(bars).to_csv(bars_csv, index=False)
        if preds:
            _pd.DataFrame(preds).to_csv(preds_csv, index=False)
    except Exception:
        pass
    typer.echo(_json.dumps({
        "ok": True,
        "symbol": symbol,
        "interval": float(interval),
        "minutes": int(minutes),
        "bars_csv": str(bars_csv) if bars else None,
        "preds_csv": str(preds_csv) if preds else None,
        "rows_bars": int(len(bars)),
        "rows_preds": int(len(preds)),
        "note": "Ensure IQFeed client is running locally and symbol is valid for your feed.",
    }, indent=2))


@app.command(name="option_price")
def option_price(
    spot: float = typer.Argument(..., help="Spot price"),
    strike: float = typer.Argument(..., help="Strike price"),
    t: float = typer.Argument(..., help="Time to expiry in years (e.g. 0.25)"),
    sigma: float = typer.Argument(..., help="Volatility (e.g. 0.2=20%)"),
    right: str = typer.Option("C", help="Option right C or P"),
    r: float = typer.Option(0.0, help="Risk-free rate (cont. comp)"),
    q: float = typer.Option(0.0, help="Dividend/carry yield"),
):
    """Compute Black-Scholes price and greeks."""
    right_u = right.upper()
    if right_u not in ("C", "P"):
        typer.echo(json.dumps({"error": "right must be C or P"}))
        raise typer.Exit(1)
    px = bs_price(spot, strike, r, sigma, t, right_u, q)
    out = {
        "S": spot, "K": strike, "T": t, "sigma": sigma, "right": right_u, "r": r, "q": q,
        "price": px,
        "delta": bs_delta(spot, strike, r, sigma, t, right_u, q),
        "gamma": bs_gamma(spot, strike, r, sigma, t, q),
        "vega": bs_vega(spot, strike, r, sigma, t, q),
        "theta_per_year": bs_theta(spot, strike, r, sigma, t, right_u, q),
        "theta_per_day": bs_theta(spot, strike, r, sigma, t, right_u, q) / 365.0,
        "rho": bs_rho(spot, strike, r, sigma, t, right_u, q),
    }
    typer.echo(json.dumps(out, indent=2))


@app.command(name="option_iv_solve")
def option_iv_solve(
    target: float = typer.Argument(..., help="Observed option premium"),
    spot: float = typer.Argument(..., help="Spot price"),
    strike: float = typer.Argument(..., help="Strike price"),
    t: float = typer.Argument(..., help="Time to expiry in years"),
    right: str = typer.Option("C", help="Option right C or P"),
    r: float = typer.Option(0.0, help="Risk-free rate"),
    q: float = typer.Option(0.0, help="Dividend/carry yield"),
    initial: float = typer.Option(0.2, help="Initial vol guess"),
):
    """Solve for implied volatility from observed premium."""
    right_u = right.upper()
    if right_u not in ("C", "P"):
        typer.echo(json.dumps({"error": "right must be C or P"}))
        raise typer.Exit(1)
    iv = implied_vol_newton(target, spot, strike, r, t, right_u, q, initial=initial)
    typer.echo(json.dumps({
        "target_price": target, "S": spot, "K": strike, "T": t, "right": right_u, "r": r, "q": q,
        "initial_guess": initial, "implied_vol": iv
    }, indent=2))


@app.command(name="interpret_hybrid_live")
def interpret_hybrid_live_cli(
    bars_csv: str = typer.Argument(..., help="CSV of live bars (from live_bars_test)"),
    preds_csv: str = typer.Option(None, help="CSV of live predictions (from live_bars_test)"),
    feature: str = typer.Option("sentiment", help="Feature for PD: sentiment|r|sigma or any column in feature DF"),
    window: int = typer.Option(64, help="Window used for hybrid predictions"),
    horizon: int = typer.Option(1, help="Horizon used for hybrid predictions"),
    out_stem: str = typer.Option(None, help="Output filename stem for artifacts"),
    n_lags: int = typer.Option(-1, help="Number of lags used in the checkpoint (-1 = autodetect from ckpt meta)"),
    ckpt: str = typer.Option("models/hybrid_bars.ckpt", help="Hybrid checkpoint to use for PD predictions"),
):
    """Compute a simple Partial Dependence curve for the hybrid model on a recent live slice.

    Uses the last window from bars CSV to build [r, sigma, (optional x)] features and probes `feature`.
    Saves PD CSV and a PNG plot in logs/.
    """
    import json as _json
    import numpy as _np
    import pandas as _pd
    from pathlib import Path as _P
    from .analysis.interpretability import partial_dependence as _pd_func
    from .models.hybrid_garch_dl import build_features_from_prices as _build, predict_hybrid_from_returns as _pred, load_hybrid_checkpoint as _hy_load
    logs = _P("logs"); logs.mkdir(parents=True, exist_ok=True)
    bdf = _pd.read_csv(bars_csv)
    if "end_ts" in bdf.columns:
        idx = _pd.to_datetime(bdf["end_ts"], unit="s")
    else:
        idx = _pd.to_datetime(bdf["ts"]) if "ts" in bdf.columns else _pd.RangeIndex(len(bdf))
    px = _pd.Series(bdf["close"].astype(float).values, index=idx)
    r, s = _build(px, normalize="log_returns")
    # sentiment from preds if present
    x = None
    if preds_csv:
        try:
            pdf = _pd.read_csv(preds_csv)
            if "ts" in pdf.columns and "sentiment" in pdf.columns:
                ss = _pd.Series(pdf["sentiment"].astype(float).values, index=_pd.to_datetime(pdf["ts"]))
                x = ss.reindex(r.index, method="ffill")
        except Exception:
            x = None
    # build feature DF consistent with hybrid
    feat_df = _pd.DataFrame({"r": r, "sigma": s})
    if x is not None:
        feat_df["x"] = x
    if feature not in feat_df.columns:
        typer.echo(_json.dumps({"ok": False, "error": f"feature '{feature}' not in {list(feat_df.columns)}"}, indent=2)); raise typer.Exit(1)
    # predictor wrapper: use last window slice each call
    def _predict(df: _pd.DataFrame) -> _np.ndarray:
        rr = _pd.Series(df["r"].values, index=df.index)
        ss = _pd.Series(df["sigma"].values, index=df.index)
        xx = _pd.Series(df["x"].values, index=df.index) if "x" in df.columns else None
        if len(df) < (window + horizon):
            return _np.array([_np.nan])
        try:
            return _pred(model_h, rr, ss, window=window, horizon=horizon, extra_feat=xx, n_lags=n_lags)
        except Exception:
            return _np.array([_np.nan])
    # load model
    model_path = ckpt
    if not _P(model_path).exists():
        typer.echo(_json.dumps({"ok": False, "error": f"checkpoint not found: {model_path}"}, indent=2)); raise typer.Exit(1)
    model_h, meta = _hy_load(model_path)
    # adopt n_lags from meta if requested
    if int(n_lags) < 0:
        try:
            n_lags = int(meta.get("n_lags", 0)) if isinstance(meta, dict) else 0
        except Exception:
            n_lags = 0
    grid = None
    # Choose sensible probing grid based on observed quantiles
    if feature == "sentiment" and x is not None:
        xv = feat_df["x"].dropna()
        if len(xv) > 0:
            lo, hi = float(xv.quantile(0.05)), float(xv.quantile(0.95))
            grid = _np.linspace(lo, hi, 25)
    elif feature in ("r", "sigma"):
        sv = feat_df[feature].dropna()
        if len(sv) > 0:
            lo, hi = float(sv.quantile(0.05)), float(sv.quantile(0.95))
            if lo == hi:
                hi = lo + 1e-6
            grid = _np.linspace(lo, hi, 25)
    pd_df = _pd_func(lambda XF: _predict(XF.iloc[-(window + horizon + 5):]), feat_df.dropna(), feature, grid=grid)
    stem = out_stem or ("pd_" + _P(bars_csv).stem)
    csv_out = logs / f"{stem}_pd.csv"
    png_out = logs / f"{stem}_pd.png"
    pd_df.to_csv(csv_out, index=False)
    # quick plot
    try:
        import matplotlib.pyplot as _plt
        _plt.figure(figsize=(6, 4))
        _plt.plot(pd_df["x"], pd_df["y"], marker="o")
        _plt.title(f"Partial Dependence: {feature}")
        _plt.xlabel(feature); _plt.ylabel("mean pred")
        _plt.tight_layout(); _plt.savefig(png_out, dpi=120); _plt.close()
    except Exception:
        pass
    typer.echo(_json.dumps({"ok": True, "pd_csv": str(csv_out), "pd_png": str(png_out), "rows": int(len(pd_df))}, indent=2))


@app.command(name="interpret_walkforward")
def interpret_walkforward_cli(
    csv_path: str = typer.Argument(..., help="Walk-forward CSV with columns y_true,y_pred_hybrid and features if saved"),
    target_col: str = typer.Option("y_true", help="Ground truth column"),
    pred_col: str = typer.Option("y_pred_hybrid", help="Prediction column"),
    top_k: int = typer.Option(10, help="Max features to report"),
    out_csv: str = typer.Option(None, help="Output CSV for LOFO report"),
):
    """Run LOFO importance for a walk-forward result CSV.

    Expects the CSV to include feature columns used by the predictor. This wraps a simple functional predictor
    that maps X->y_pred using a local linear fit between pred_col and features as a proxy.
    """
    import json as _json
    import numpy as _np
    import pandas as _pd
    from pathlib import Path as _P
    from .analysis.interpretability import lofo_importance as _lofo
    from .analysis.metrics import mae as _mae
    df = _pd.read_csv(csv_path)
    if target_col not in df.columns or pred_col not in df.columns:
        typer.echo(_json.dumps({"ok": False, "error": "Missing target or pred columns"}, indent=2)); raise typer.Exit(1)
    # Heuristic: treat all numeric columns except target/pred as candidate features
    num_cols = [c for c in df.columns if c not in (target_col, pred_col) and _pd.api.types.is_numeric_dtype(df[c])]
    if not num_cols:
        typer.echo(_json.dumps({"ok": False, "error": "No numeric feature columns found"}, indent=2)); raise typer.Exit(1)
    X = df[num_cols].copy().replace([_np.inf, -_np.inf], _np.nan)
    X = X.ffill().fillna(0.0)
    y_true = _np.asarray(df[target_col].astype(float).values, dtype=float)
    # Simple local predictor: linear regression of pred_col on X (proxy for the model mapping)
    beta_arr = None
    try:
        import numpy.linalg as _la
        X_ = _np.c_[_np.ones(len(X)), X.values]
        y_ = df[pred_col].astype(float).values
        beta_arr, *_rest = _la.lstsq(_np.asarray(X_, dtype=float), _np.asarray(y_, dtype=float), rcond=None)
    except Exception:
        beta_arr = None
    def _predict_fn_lofo(Xdf):
        if beta_arr is not None:
            Xd = _np.c_[_np.ones(len(Xdf)), _np.asarray(Xdf.values, dtype=float)]
            return Xd @ beta_arr
        return _np.asarray(df[pred_col].astype(float).values[: len(Xdf)], dtype=float)
    lofo = _lofo(_predict_fn_lofo, X, y_true, metric=_mae)
    if out_csv is None:
        out_csv = str(_P(csv_path).with_name(_P(csv_path).stem + "_lofo.csv"))
    lofo.head(int(top_k)).to_csv(out_csv, index=False)
    typer.echo(_json.dumps({"ok": True, "lofo_csv": out_csv, "features": num_cols[: int(top_k)]}, indent=2))


@app.command(name="causal_probe")
def causal_probe_cli(
    bars_csv: str = typer.Argument(..., help="CSV of bars with close and timestamp info"),
    preds_csv: str = typer.Option(None, help="Preds CSV with ts and sentiment for alignment"),
    max_lag: int = typer.Option(20, help="Max lag for cross-correlation"),
    out_stem: str = typer.Option(None, help="Output stem for artifacts"),
):
    """Run causal diagnostics: cross-correlation, Granger causality, and stratified response for sentiment vs next return."""
    import json as _json
    from pathlib import Path as _P
    import numpy as _np
    import pandas as _pd
    from .analysis.causal import cross_correlation as _cc, granger_causality as _gc, stratified_response as _sr
    logs = _P("logs"); logs.mkdir(parents=True, exist_ok=True)
    bdf = _pd.read_csv(bars_csv)
    if "end_ts" in bdf.columns:
        idx = _pd.to_datetime(bdf["end_ts"], unit="s")
    else:
        idx = _pd.to_datetime(bdf["ts"]) if "ts" in bdf.columns else _pd.RangeIndex(len(bdf))
    px = _pd.Series(bdf["close"].astype(float).values, index=idx)
    r = _pd.Series(_np.log(px / px.shift(1))).replace([_np.inf, -_np.inf], _np.nan).dropna()
    # get sentiment
    s = None
    if preds_csv:
        try:
            pdf = _pd.read_csv(preds_csv)
            if "ts" in pdf.columns and "sentiment" in pdf.columns:
                s = _pd.Series(pdf["sentiment"].astype(float).values, index=_pd.to_datetime(pdf["ts"]))
                s = s.reindex(r.index, method="ffill")
        except Exception:
            s = None
    if s is None or s.dropna().empty:
        typer.echo(_json.dumps({"ok": False, "error": "No sentiment series available"}, indent=2)); raise typer.Exit(1)
    # Cross-corr sentiment vs returns
    cc = _cc(s, r, max_lag=int(max_lag))
    stem = out_stem or ("causal_" + _P(bars_csv).stem)
    cc_csv = logs / f"{stem}_cc.csv"; cc.to_csv(cc_csv, index=False)
    # Granger: does sentiment help predict returns?
    gc = _gc(s, r, max_lag=5)
    # Stratified response: avg next return by sentiment quantile
    y_next = r.shift(-1).reindex(s.index)
    sr = _sr(s, y_next, bins=10)
    sr_csv = logs / f"{stem}_strat.csv"; sr.to_csv(sr_csv, index=False)
    # Optional quick plots
    png_cc = logs / f"{stem}_cc.png"; png_sr = logs / f"{stem}_strat.png"
    try:
        import matplotlib.pyplot as _plt
        _plt.figure(figsize=(6, 3)); _plt.stem(cc["lag"], cc["corr"]); _plt.title("Cross-correlation: sentiment vs returns"); _plt.tight_layout(); _plt.savefig(png_cc, dpi=120); _plt.close()
        _plt.figure(figsize=(6, 3)); _plt.bar(range(len(sr)), sr["mean_y"]); _plt.title("Mean next return by sentiment bin"); _plt.tight_layout(); _plt.savefig(png_sr, dpi=120); _plt.close()
    except Exception:
        pass
    typer.echo(_json.dumps({"ok": True, "cc_csv": str(cc_csv), "gc": gc, "strat_csv": str(sr_csv), "cc_png": str(png_cc), "strat_png": str(png_sr)}, indent=2))


@app.command(name="exo_align")
def exo_align_cli(
    bars_csv: str = typer.Argument(..., help="CSV of bars with close and timestamp info (from live_bars_test)"),
    exo_csv: str | None = typer.Option(None, help="Comma-separated exogenous CSV paths; if omitted, try config/exo_policy.json"),
    symbol: str | None = typer.Option(None, help="Symbol for policy auto-load (e.g., SPY). If omitted, infer from bars_csv name when possible"),
    interval: str | None = typer.Option(None, help="Interval tag for policy key (e.g., '1s'). If omitted, infer from bars_csv name when possible"),
    resample: str | None = typer.Option(None, help="Resample rule for exogenous series (e.g., 'S','5S','1min')"),
    ffill_seconds: int = typer.Option(300, help="Forward-fill cap in seconds for exogenous alignment"),
    half_life: float = typer.Option(0.0, help="Half-life (seconds) for exponential decay of exogenous values (0 to disable)"),
    method: str = typer.Option("zsum", help="Combine method for multiple exogenous series: zsum|wsum"),
    weights: str | None = typer.Option(None, help="JSON mapping name->weight when using wsum"),
    out_stem: str | None = typer.Option(None, help="Output stem; defaults to exoalign_{bars_stem}"),
    plot: bool = typer.Option(True, help="Write a quick alignment plot PNG"),
):
    """Preview alignment of exogenous series to bar timestamps and build a composite.

    Saves a CSV with aligned individual series and the composite, plus an optional plot in logs/.
    """
    import json as _json
    from pathlib import Path as _P
    import pandas as _pd
    import numpy as _np

    logs = _P("logs"); logs.mkdir(parents=True, exist_ok=True)
    p = _P(bars_csv)
    if not p.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"not found: {bars_csv}"}, indent=2)); raise typer.Exit(1)
    bdf = _pd.read_csv(p)
    if "end_ts" in bdf.columns:
        idx = _pd.to_datetime(bdf["end_ts"], unit="s")
    else:
        idx = _pd.to_datetime(bdf["ts"]) if "ts" in bdf.columns else _pd.RangeIndex(len(bdf))
    px = _pd.Series(bdf["close"].astype(float).values, index=idx)

    # Resolve exogenous paths: explicit list or from policy
    paths: list[str] = []
    if exo_csv:
        paths = [s.strip() for s in exo_csv.split(',') if s.strip()]
    if not paths:
        # attempt policy auto-load using symbol@interval
        try:
            pol_path = _P("config") / "exo_policy.json"
            if pol_path.exists():
                pol = _json.loads(pol_path.read_text(encoding="utf-8"))
                sym = symbol
                ival = interval
                # Infer from file name like live_SPY_1s_YYYY... if missing
                if not sym or not ival:
                    stem = p.stem
                    parts = stem.split('_')
                    # try pattern: live_{symbol}_{interval}_{ts}_bars
                    if len(parts) >= 4 and parts[0] == "live":
                        sym = sym or parts[1]
                        ival = ival or parts[2]
                key = f"{sym}@{ival}" if (sym and ival) else None
                prof = pol.get(key) if key else None
                if not prof:
                    prof = pol.get("default")
                if prof:
                    paths = list(prof.get("paths", []) or [])
                    if not resample and prof.get("resample"):
                        resample = prof.get("resample")
                    if (ffill_seconds is None or int(ffill_seconds) == 0) and (prof.get("ffill_seconds") is not None):
                        ffill_seconds = int(prof.get("ffill_seconds"))
                    if (half_life is None or float(half_life) == 0.0) and (prof.get("half_life") is not None):
                        half_life = float(prof.get("half_life"))
                    if not method and prof.get("method"):
                        method = prof.get("method")
                    if not weights and prof.get("weights"):
                        weights = _json.dumps(prof.get("weights"))
        except Exception as _e_pol:
            LOGGER.debug("exo_align policy load failed: %s", _e_pol)

    if not paths:
        typer.echo(_json.dumps({"ok": False, "error": "No exogenous paths provided or found via policy"}, indent=2)); raise typer.Exit(1)

    # Read, align, and combine
    try:
        from .features.exogenous import read_series_csv as _read_exo, align_series_to_index as _align_exo, combine_signals as _combine_exo
        exo_map: dict[str, _pd.Series] = {}
        for path in paths:
            try:
                s = _read_exo(path)
                if not s.empty:
                    exo_map[_P(path).stem] = s
            except Exception as _e_read:
                LOGGER.debug("exo_align read failed %s: %s", path, _e_read)
        if not exo_map:
            typer.echo(_json.dumps({"ok": False, "error": "All exogenous series failed to load or were empty"}, indent=2)); raise typer.Exit(1)
        target_index = _pd.DatetimeIndex(px.index)
        aligned = {k: _align_exo(v, target_index, resample=resample, ffill_seconds=ffill_seconds, half_life=half_life) for k, v in exo_map.items()}
        w = None
        if (method or "zsum").lower() == "wsum" and weights:
            try:
                w = _json.loads(weights)
            except Exception:
                w = None
        comp, aligned_out = _combine_exo(aligned, method=(method or "zsum").lower(), weights=w, target_index=target_index)
        df_out = _pd.DataFrame({**{k: aligned_out[k].values for k in sorted(aligned_out.keys())}, "exo_comp": comp.values}, index=target_index)
        df_out.index.name = "ts"
        stem = out_stem or ("exoalign_" + p.stem)
        csv_out = logs / f"{stem}_exo.csv"
        df_out.to_csv(csv_out)
        png_out = None
        if plot:
            try:
                import matplotlib.pyplot as _plt
                _plt.figure(figsize=(8, 4))
                # z-normalize for comparability
                def _zn(a):
                    a = _np.asarray(a, dtype=float)
                    return (a - _np.nanmean(a)) / (_np.nanstd(a) + 1e-12)
                for k in sorted(aligned_out.keys()):
                    _plt.plot(df_out.index, _zn(df_out[k]), alpha=0.6, label=k)
                _plt.plot(df_out.index, _zn(df_out["exo_comp"]), lw=2.0, label="exo_comp")
                _plt.legend(loc="best", fontsize=8)
                _plt.title("Exogenous alignment (z-scored)")
                _plt.tight_layout()
                png_out = logs / f"{stem}_exo.png"
                _plt.savefig(png_out, dpi=120)
                _plt.close()
            except Exception as _e_plot:
                LOGGER.debug("exo_align plot failed: %s", _e_plot)
        typer.echo(_json.dumps({"ok": True, "csv": str(csv_out), "png": (str(png_out) if png_out else None), "series": list(sorted(aligned_out.keys()))}, indent=2))
    except Exception as _e:
        typer.echo(_json.dumps({"ok": False, "error": str(_e)}, indent=2)); raise typer.Exit(1)


@app.command(name="make_exo_from_bars")
def make_exo_from_bars_cli(
    bars_csv: str = typer.Argument(..., help="CSV of bars with close and timestamp info (from live_bars_test)"),
    method: str = typer.Option("rv", help="Derived exo method: rv (rolling realized vol), zret (z-scored returns)"),
    window: int = typer.Option(60, help="Window length (in bars) for rolling ops"),
    out_csv: str | None = typer.Option(None, help="Output CSV path (defaults to logs/{bars_stem}_exo_sample.csv)"),
):
    """Create a simple exogenous series from bars for pipeline validation.

    Outputs a CSV with columns: ts,value. Useful for testing exo alignment and blends when you don't yet have real cross-asset feeds.
    """
    import json as _json
    import numpy as _np
    import pandas as _pd
    from pathlib import Path as _P

    p = _P(bars_csv)
    if not p.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"not found: {bars_csv}"}, indent=2)); raise typer.Exit(1)
    bdf = _pd.read_csv(p)
    if "end_ts" in bdf.columns:
        idx = _pd.to_datetime(bdf["end_ts"], unit="s")
    else:
        idx = _pd.to_datetime(bdf["ts"]) if "ts" in bdf.columns else _pd.RangeIndex(len(bdf))
    px = _pd.Series(_pd.to_numeric(bdf["close"], errors="coerce").astype(float).values, index=idx)
    r = _pd.Series(_np.log(px / px.shift(1)), index=idx).replace([_np.inf, -_np.inf], _np.nan)

    method_l = (method or "rv").lower()
    if method_l == "zret":
        # z-scored returns over rolling window
        m = r.rolling(window).mean()
        s = r.rolling(window).std()
        exo = (r - m) / (s + 1e-12)
    else:
        # realized volatility proxy: sqrt of rolling mean of r^2
        exo = (r.pow(2).rolling(window).mean()).pow(0.5)

    df_out = _pd.DataFrame({"ts": _pd.to_datetime(exo.index).astype("datetime64[ns]"), "value": exo.values})
    df_out = df_out.dropna()
    logs = _P("logs"); logs.mkdir(parents=True, exist_ok=True)
    if out_csv is None:
        out_csv = str(logs / f"{p.stem}_exo_sample.csv")
    df_out.to_csv(out_csv, index=False)
    typer.echo(_json.dumps({"ok": True, "out_csv": out_csv, "rows": int(len(df_out)), "method": method_l, "window": int(window)}, indent=2))


@app.command(name="greeks_factor")
def greeks_factor(
    underlying_price: float = typer.Argument(..., help="Underlying price (float); live chain attempted if IQFeed available else simulated"),
    mode: str = typer.Option("rr", help="Factor mode: rr|fly|slope|composite"),
    # RR params
    target_abs_delta: float = typer.Option(0.25, help="Target absolute delta for RR and default wing bucket"),
    tol: float = typer.Option(0.05, help="Delta tolerance for bucket selection"),
    scale: float = typer.Option(0.20, help="Normalization scale for rr/fly/slope components"),
    # Butterfly params
    atm_abs_delta: float = typer.Option(0.5, help="ATM absolute delta for butterfly"),
    # Slope params
    slope_outer: float = typer.Option(0.10, help="Outer delta for slope"),
    slope_inner: float = typer.Option(0.25, help="Inner delta for slope"),
    # Composite weights and overrides
    w_rr: float = typer.Option(0.5, help="Weight for RR in composite"),
    w_fly: float = typer.Option(0.25, help="Weight for butterfly in composite"),
    w_slope: float = typer.Option(0.25, help="Weight for slope in composite"),
    rr_scale: float = typer.Option(0.20, help="RR component scale in composite"),
    fly_scale: float = typer.Option(0.20, help="Butterfly component scale in composite"),
    slope_scale: float = typer.Option(0.20, help="Slope component scale in composite"),
    persist: bool = typer.Option(False, help="Persist factor to metrics (category=greeks_factor)"),
    persist_date: str = typer.Option(None, help="Override persistence date YYYY-MM-DD (defaults to UTC today)"),
    persist_ts: float = typer.Option(None, help="Override persistence timestamp (epoch seconds); default now"),
):
    """Compute a greeks-derived normalized factor (greeks_norm) from an option chain.

    Attempts a live IQFeed chain using Level1 fundamentals when available and falls back to a simulation.
    """
    from .datafeeds.iqfeed_options import IQFeedOptionsGreeks
    settings = Settings()
    og = IQFeedOptionsGreeks()
    chain = og.fetch_chain_greeks(underlying_price) or []
    # Build kwargs per mode
    m = (mode or "rr").lower()
    kwargs = {}
    if m == "rr":
        kwargs = {"target_abs_delta": target_abs_delta, "tol": tol, "scale": scale}
    elif m == "fly":
        kwargs = {"target_abs_delta": target_abs_delta, "atm_abs_delta": atm_abs_delta, "tol": tol, "scale": scale}
    elif m == "slope":
        kwargs = {"outer_delta": slope_outer, "inner_delta": slope_inner, "tol": tol, "scale": scale}
    elif m == "composite":
        kwargs = {
            "rr_delta": target_abs_delta,
            "fly_delta": target_abs_delta,
            "atm_abs_delta": atm_abs_delta,
            "slope_outer": slope_outer,
            "slope_inner": slope_inner,
            "tol": tol,
            "rr_scale": rr_scale,
            "fly_scale": fly_scale,
            "slope_scale": slope_scale,
            "w_rr": w_rr,
            "w_fly": w_fly,
            "w_slope": w_slope,
        }
    else:
        kwargs = {"target_abs_delta": target_abs_delta, "tol": tol, "scale": scale}
    res = compute_greeks_norm(chain, mode=mode, **kwargs)
    payload = {
        "underlying_price": underlying_price,
        "source": og.last_source or "unknown",
        "mode": mode,
        **res,
    }
    if persist:
        try:
            append_metric(settings.data_dir or "data", "greeks_factor", payload, date=persist_date, ts=persist_ts)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Persist greeks_factor failed: %s", exc)
    typer.echo(json.dumps(payload, indent=2))


@app.command(name="rocket_backtest_range")
def rocket_backtest_range(
    start: str = typer.Argument(..., help="Start date YYYY-MM-DD (inclusive)"),
    end: str = typer.Argument(..., help="End date YYYY-MM-DD (inclusive)"),
    timespan: str = typer.Option("minute", help="Aggregate timespan for Polygon ticks: minute|second"),
    mult: int = typer.Option(1, help="Timespan multiplier (1=1min, etc)"),
    # replay gates
    pct_thresh_cheap: float = typer.Option(0.35, help="Percent rise threshold for rockets starting < $0.50"),
    pct_thresh: float = typer.Option(0.2, help="Percent rise threshold for rockets starting >= $0.50"),
    uptick_ratio: float = typer.Option(0.5, help="Required fraction of upticks in the window [0-1]"),
    window_sec: float = typer.Option(1200.0, help="Lookback seconds for momentum window (e.g., 20min for 1m bars)"),
    debounce_sec: float = typer.Option(300.0, help="Minimum seconds between alerts per symbol"),
    require_index_trend: bool = typer.Option(False, help="Require index trend alignment during replay"),
    index_trend_window_sec: float = typer.Option(300.0, help="Index trend window seconds"),
    index_trend_min_bp: float = typer.Option(5.0, help="Min index trend in basis points"),
    # pnl exits
    exit_mode: str = typer.Option("last", help="Exit mode when using ticks: last|cutoff|ptsl|trailing"),
    exit_cutoff: str = typer.Option(None, help="Cutoff time (America/New_York) for exit when mode=cutoff, e.g. 15:59:30"),
    cheap_price: float = typer.Option(0.5, help="Cheap vs standard threshold for PT/SL/trailing"),
    pt_cheap: float = typer.Option(1.0, help="PT for cheap"),
    pt_std: float = typer.Option(0.5, help="PT for standard"),
    sl_cheap: float = typer.Option(0.5, help="SL for cheap"),
    sl_std: float = typer.Option(0.35, help="SL for standard"),
    trail_dd_cheap: float = typer.Option(0.4, help="Trailing DD for cheap"),
    trail_dd_std: float = typer.Option(0.25, help="Trailing DD for standard"),
    min_hold_sec: float = typer.Option(30.0, help="Min hold seconds before exits"),
    slippage_abs: float = typer.Option(0.05, help="Absolute slippage per side ($)"),
    slippage_bps: float = typer.Option(0.0, help="Percent slippage per side"),
    fees_per_contract: float = typer.Option(0.65, help="Per-contract fee (open+close each)"),
    price_source: str = typer.Option("mid", help="Price source for non-ticks exits: mid|last|close"),
    side: str = typer.Option("buy", help="Position side: buy|sell"),
    # agent policy
    use_agent_exit_policy: bool = typer.Option(False, help="Enable agent exit policy for tick exits"),
    features_jsonl: Path = typer.Option(None, help="Optional features JSONL for policy decisions"),
    # filters
    min_entry: float = typer.Option(None, help="Min entry mid to include a trade"),
    max_entry: float = typer.Option(None, help="Max entry mid to include a trade"),
    include_hours: str = typer.Option(None, help="Include entries only if entry_ts within HH:MM-HH:MM (US/Eastern)"),
    # outputs
    out_json: Path = typer.Option(None, help="Aggregate backtest output JSON (default logs/rocket_backtest_<start>_<end>.json)"),
):
    """Backtest a date range using Polygon ticks -> rocket_replay -> options_eod_pnl and aggregate daily summaries."""
    import datetime as _dt
    from datetime import timedelta as _td

    def _daterange(s: _dt.date, e: _dt.date):
        d = s
        while d <= e:
            yield d
            d += _td(days=1)

    s = _dt.date.fromisoformat(start)
    e = _dt.date.fromisoformat(end)
    results: list[dict] = []
    agg = {"days": 0, "trades": 0, "wins": 0, "losses": 0, "gross_pnl": 0.0}

    for d in _daterange(s, e):
        day = d.isoformat()
        # 1) ticks
        try:
            build_ticks_from_polygon.callback(expiry=day, out_csv=None, timespan=timespan, mult=mult, near_pct=0.08, otm_percent=0.12, max_symbols=150, rth_only=True)
        except Exception:
            pass
        ticks_csv = Path(f"logs/rocket_ticks_{day}_poly.csv")
        # 2) replay
        alerts_path = Path(f"logs/rocket_ticks_{day}_poly_replay.jsonl")
        if ticks_csv.exists():
            try:
                rocket_replay.callback(
                    ticks_csv=ticks_csv,
                    out=alerts_path,
                    min_price=0.05,
                    pct_thresh_cheap=pct_thresh_cheap,
                    pct_thresh=pct_thresh,
                    uptick_ratio=uptick_ratio,
                    window_sec=window_sec,
                    debounce_sec=debounce_sec,
                    max_spread_abs=0.20,
                    max_spread_frac=0.75,
                    require_index_trend=require_index_trend,
                    index_trend_window_sec=index_trend_window_sec,
                    index_trend_min_bp=index_trend_min_bp,
                )
            except Exception:
                pass
        # 3) pnl
        daily_out = Path(f"logs/eod_pnl_{day}.json")
        try:
            options_eod_pnl.callback(
                expiry=day,
                alerts_path=alerts_path,
                out_json=daily_out,
                out_csv=Path(f"logs/eod_pnl_{day}.csv"),
                price_source=price_source,
                side=side,
                size=1,
                multiplier=100.0,
                fallback_polygon=True,
                near_pct=0.08,
                otm_percent=0.15,
                otm_tiers="0.15,0.25,0.35",
                exit_from_ticks=ticks_csv,
                exit_cutoff=exit_cutoff,
                exit_mode=exit_mode,
                cheap_price=cheap_price,
                pt_cheap=pt_cheap,
                pt_std=pt_std,
                sl_cheap=sl_cheap,
                sl_std=sl_std,
                trail_dd_cheap=trail_dd_cheap,
                trail_dd_std=trail_dd_std,
                min_hold_sec=min_hold_sec,
                slippage_abs=slippage_abs,
                slippage_bps=slippage_bps,
                fees_per_contract=fees_per_contract,
                min_entry=min_entry,
                max_entry=max_entry,
                use_agent_exit_policy=use_agent_exit_policy,
                features_jsonl=features_jsonl,
                    include_hours=include_hours,
                    allow_empty=True,
            )
        except Exception:
            pass
        # 4) collect summary
        try:
            if daily_out.exists():
                j = json.loads(daily_out.read_text(encoding="utf-8"))
                summary = j.get("summary") or j
                summary["date"] = day
                results.append(summary)
                agg["days"] += 1
                agg["trades"] += int(summary.get("trades") or 0)
                agg["wins"] += int(summary.get("wins") or 0)
                agg["losses"] += int(summary.get("losses") or 0)
                agg["gross_pnl"] += float(summary.get("gross_pnl") or 0.0)
        except Exception:
            pass

    agg["avg_pnl"] = (agg["gross_pnl"] / agg["trades"]) if agg["trades"] else 0.0
    agg["win_rate"] = (agg["wins"] / agg["trades"]) if agg["trades"] else 0.0

    if out_json is None:
        out_json = Path(f"logs/rocket_backtest_{start}_{end}.json")
    try:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps({"ok": True, "range": [start, end], "aggregate": agg, "days": results}, indent=2), encoding="utf-8")
    except Exception:
        pass
    typer.echo(json.dumps({"ok": True, "range": [start, end], "aggregate": agg, "out": str(out_json)}, indent=2))

@app.command(name="plot_iv_skew")
def plot_iv_skew_cli(
    csv_path: str = typer.Argument(..., help="CSV with risk_reversal/rr and/or butterfly/fly time series"),
    out_png: str | None = typer.Option(None, help="Output PNG path (defaults to <csv>_ivskew.png)"),
    title: str | None = typer.Option(None, help="Plot title"),
):
    """Plot IV skew metrics (risk reversal, butterfly) over time from a CSV."""
    try:
        out = _plot_iv_skew(csv_path, out_png=out_png, title=title)
        typer.echo(json.dumps({"ok": True, "png": out}, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        raise typer.Exit(1)


@app.command(name="stress_hybrid_bars")
def stress_hybrid_bars_cli(
    bars_csv: str = typer.Argument(..., help="CSV of bars (with end_ts and close) to serve as baseline prices"),
    symbol: str = typer.Option(None, help="Underlying symbol for policy lookup (e.g., SPY)"),
    interval: str = typer.Option(None, help="Bar interval label for policy lookup (e.g., 1s, 10s, 1m)"),
    window: int = typer.Option(64, help="Window length for hybrid predictions"),
    horizon: int = typer.Option(1, help="Forecast horizon"),
    normalize: str = typer.Option("log_returns", help="Normalization used in hybrid r/s features"),
    ckpt: str = typer.Option("models/hybrid_bars.ckpt", help="Hybrid checkpoint path"),
    out_stem: str = typer.Option(None, help="Output stem for artifacts (defaults to stress_<bars_stem>)"),
    out_dir: str = typer.Option("logs", help="Directory to write outputs to (default: logs)"),
    # Plots
    plots: bool = typer.Option(True, help="If true, save quick overlay plots of y_true vs y_pred for scenarios"),
    plots_max: int = typer.Option(6, help="Max number of scenario plots per model"),
    plots_price: bool = typer.Option(False, help="If true, save baseline vs scenario price overlay plots"),
    # Guardrails
    guard_abs: str = typer.Option(None, help='JSON: {"mae_max":..., "rmse_max":..., "corr_min":..., "hitrate_min":...}'),
    guard_delta: str = typer.Option(None, help='JSON: deltas vs baseline {"mae_max":..., "rmse_max":..., "corr_drop_max":..., "hitrate_drop_max":...}'),
    guard_abs_file: str = typer.Option(None, help="Path to JSON file with absolute guardrail limits"),
    guard_delta_file: str = typer.Option(None, help="Path to JSON file with delta guardrail limits"),
    fail_on_violation: bool = typer.Option(False, help="Exit with code 2 if any guardrail violation occurs"),
    # Ensemble parity (optional)
    ensemble_ckpt: str = typer.Option(None, help="If set, also run stress on the ensemble model using this checkpoint"),
    # Scenarios and outputs
    scenario_names: str = typer.Option(None, help="Comma-separated list of scenario names to run (subset of defaults)"),
    save_residuals: bool = typer.Option(False, help="If true, write per-step residuals to CSV"),
    seed: int = typer.Option(None, help="Random seed for deterministic stress (noise bursts, etc.)"),
):
    """Run adversarial/robustness simulations against the Hybrid model on a baseline price series.

    Produces per-scenario metrics (MAE, RMSE, Corr, hit-rate) and saves a CSV under logs/.
    """
    import json as _json
    import numpy as _np
    import pandas as _pd
    from pathlib import Path as _P
    from .models.hybrid_garch_dl import build_features_from_prices as _build, predict_hybrid_from_returns as _pred, load_hybrid_checkpoint as _hy_load
    from .analysis.metrics import mae as _mae, rmse as _rmse
    p = _P(bars_csv)
    if not p.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"not found: {bars_csv}"}, indent=2)); raise typer.Exit(1)
    bdf = _pd.read_csv(p)
    # Build baseline px series
    if "end_ts" in bdf.columns:
        idx = _pd.to_datetime(bdf["end_ts"], unit="s")
    elif "ts" in bdf.columns:
        idx = _pd.to_datetime(bdf["ts"], errors="coerce")
    else:
        idx = _pd.RangeIndex(len(bdf))
    px = _pd.Series(_pd.to_numeric(bdf["close"], errors="coerce").astype(float).values, index=idx).dropna()
    # Seed for deterministic scenarios
    if seed is not None:
        try:
            import random as _random
            _random.seed(int(seed))
            _np.random.seed(int(seed))
        except Exception:
            pass
    # Load models
    if not _P(ckpt).exists():
        typer.echo(_json.dumps({"ok": False, "error": f"checkpoint not found: {ckpt}"}, indent=2)); raise typer.Exit(1)
    model_h, meta = _hy_load(ckpt)
    model_e = None
    if ensemble_ckpt:
        try:
            from .models.ensemble_vae_lstm_transformer import EnsembleModel as _Ens
            import torch as _torch
            if not _P(ensemble_ckpt).exists():
                typer.echo(_json.dumps({"ok": False, "error": f"ensemble ckpt not found: {ensemble_ckpt}"}, indent=2)); raise typer.Exit(1)
            model_e = _Ens(window=window, horizon=horizon)
            loaded = _torch.load(ensemble_ckpt, map_location="cpu")
            model_e.load_state_dict(loaded.get("state_dict", loaded))
            model_e.eval()
        except Exception as _e:
            model_e = None
    # Build scenarios
    scenarios_all = _stress_scenarios(px)
    if scenario_names:
        wanted = {s.strip() for s in str(scenario_names).split(",") if s.strip()}
        scenarios_map = {k: v for k, v in scenarios_all.items() if k in wanted}
    else:
        scenarios_map = scenarios_all
    # Optional price overlay plotting helper
    def _plot_price_overlay(stem_path: _P, scenario_name: str, base_px: _pd.Series, sc_px: _pd.Series):
        try:
            import matplotlib.pyplot as _plt
            import pandas as _pd_pl
            _plt.figure(figsize=(6,3))
            # Align indexes
            dfp = _pd_pl.concat({"baseline": base_px, scenario_name: sc_px}, axis=1).dropna()
            _plt.plot(dfp.index.to_numpy(), dfp["baseline"].to_numpy(), label="baseline", alpha=0.8)
            _plt.plot(dfp.index.to_numpy(), dfp[scenario_name].to_numpy(), label=scenario_name, alpha=0.8)
            _plt.legend(); _plt.title(f"price overlay - {scenario_name}")
            _plt.tight_layout()
            out_png = stem_path.parent / f"{stem_path.stem}_price_{scenario_name}.png"
            _plt.savefig(out_png, dpi=120); _plt.close()
            return str(out_png)
        except Exception:
            return None
    rows = []
    residual_rows = [] if save_residuals else None
    plot_count: dict[str, int] = {"hybrid": 0, "ensemble": 0}
    def _plot_series(stem_path: _P, model_name: str, scenario_name: str, y_true_arr, y_pred_arr):
        nonlocal plot_count
        try:
            import numpy as _np_pl
            import matplotlib.pyplot as _plt
            if plot_count.get(model_name, 0) >= int(max(0, plots_max)):
                return None
            _plt.figure(figsize=(6,3))
            x = _np_pl.arange(len(y_true_arr))
            _plt.plot(x, y_true_arr, label="y_true", alpha=0.8)
            _plt.plot(x, y_pred_arr, label="y_pred", alpha=0.8)
            _plt.legend(); _plt.title(f"{model_name} - {scenario_name}")
            _plt.tight_layout()
            out_png = stem_path.parent / f"{stem_path.stem}_{model_name}_{scenario_name}.png"
            _plt.savefig(out_png, dpi=120); _plt.close()
            plot_count[model_name] = plot_count.get(model_name, 0) + 1
            return str(out_png)
        except Exception:
            return None

    for name, px_s in scenarios_map.items():
        try:
            r, s = _build(px_s, normalize=normalize or "log_returns")
            if len(r) < (window + horizon):
                rows.append({"model": "hybrid", "scenario": name, "rows": int(len(r)), "mae": None, "rmse": None, "corr": None, "hitrate": None})
                if model_e is not None:
                    rows.append({"model": "ensemble", "scenario": name, "rows": int(len(r)), "mae": None, "rmse": None, "corr": None, "hitrate": None})
                continue
            # Walk-forward predictions on scenario series
            y_true = []
            y_pred = []
            t_idx = []
            for i in range(0, len(r) - window - horizon + 1):
                rs = r.iloc[: i + window]
                ss = s.iloc[: i + window]
                yh = _pred(model_h, rs, ss, window=window, horizon=horizon)
                y_pred.append(float(yh.reshape(-1)[-1]))
                k = i + window + horizon - 1
                y_true.append(float(r.iloc[k]))
                t_idx.append(r.index[k])
            y_true = _np.asarray(y_true, dtype=float)
            y_pred = _np.asarray(y_pred, dtype=float)
            # Metrics
            mae = float(_mae(y_true, y_pred))
            rmse = float(_rmse(y_true, y_pred))
            corr = float(_np.corrcoef(y_true, y_pred)[0, 1]) if len(y_true) > 1 else float("nan")
            s_true = _np.sign(y_true); s_pred = _np.sign(y_pred)
            mask = s_true != 0
            hit = float(_np.mean((s_true[mask]) == (s_pred[mask]))) if mask.any() else float("nan")
            rows.append({"model": "hybrid", "scenario": name, "rows": int(len(y_true)), "mae": mae, "rmse": rmse, "corr": corr, "hitrate": hit})
            # Residuals (hybrid)
            if residual_rows is not None and len(t_idx) == len(y_true):
                for tt, yt, yp in zip(t_idx, y_true, y_pred):
                    residual_rows.append({"model": "hybrid", "scenario": name, "t": str(tt), "y_true": float(yt), "y_pred": float(yp), "resid": float(yt - yp)})
            # Optional plot
            if plots:
                _plot_series(_P(out_dir) / (out_stem or f"stress_{p.stem}"), "hybrid", name, y_true, y_pred)
            if plots_price:
                _plot_price_overlay(_P(out_dir) / (out_stem or f"stress_{p.stem}"), name, px, px_s)
            # Ensemble parity
            if model_e is not None:
                try:
                    from .models.preprocess import normalize_series as _norm_series
                    # Normalize price series for ensemble
                    series_norm = _norm_series(px_s, mode=normalize) if (normalize and normalize.lower() != "none") else px_s
                    s_norm = _np.asarray(series_norm.values, dtype=float)
                    y_true_e = []
                    y_pred_e = []
                    t_idx_e = []
                    from .models.ensemble_vae_lstm_transformer import predict_ensemble as _pred_ens
                    import torch as _torch
                    tseries = _torch.tensor(s_norm, dtype=_torch.float32)
                    Le = len(tseries)
                    for j in range(0, Le - window - horizon + 1):
                        seg = tseries[: j + window]
                        y_e = _pred_ens(model_e, seg, window=window, horizon=horizon)
                        y_pred_e.append(float(y_e.view(-1)[-1]))
                        kk = j + window + horizon - 1
                        y_true_e.append(float(r.iloc[kk]))
                        t_idx_e.append(r.index[kk])
                    y_true_e = _np.asarray(y_true_e, dtype=float)
                    y_pred_e = _np.asarray(y_pred_e, dtype=float)
                    mae_e = float(_mae(y_true_e, y_pred_e)) if len(y_true_e) else None
                    rmse_e = float(_rmse(y_true_e, y_pred_e)) if len(y_true_e) else None
                    corr_e = float(_np.corrcoef(y_true_e, y_pred_e)[0, 1]) if len(y_true_e) > 1 else float("nan")
                    s_true_e = _np.sign(y_true_e); s_pred_e = _np.sign(y_pred_e)
                    mask_e = s_true_e != 0
                    hit_e = float(_np.mean((s_true_e[mask_e]) == (s_pred_e[mask_e]))) if mask_e.any() else float("nan")
                    rows.append({"model": "ensemble", "scenario": name, "rows": int(len(y_true_e)), "mae": mae_e, "rmse": rmse_e, "corr": corr_e, "hitrate": hit_e})
                    if residual_rows is not None and len(t_idx_e) == len(y_true_e):
                        for tt, yt, yp in zip(t_idx_e, y_true_e, y_pred_e):
                            residual_rows.append({"model": "ensemble", "scenario": name, "t": str(tt), "y_true": float(yt), "y_pred": float(yp), "resid": float(yt - yp)})
                    if plots:
                        _plot_series(_P(out_dir) / (out_stem or f"stress_{p.stem}"), "ensemble", name, y_true_e, y_pred_e)
                except Exception:
                    rows.append({"model": "ensemble", "scenario": name, "rows": 0, "mae": None, "rmse": None, "corr": None, "hitrate": None})
        except Exception as _e:
            rows.append({"model": "hybrid", "scenario": name, "error": str(_e)})
            if model_e is not None:
                rows.append({"model": "ensemble", "scenario": name, "error": str(_e)})
    out_dir_p = _P(out_dir); out_dir_p.mkdir(parents=True, exist_ok=True)
    stem = out_stem or f"stress_{p.stem}"
    out_csv = out_dir_p / f"{stem}_metrics.csv"
    df_out = _pd.DataFrame(rows)
    # Guardrails evaluation
    violations = []
    try:
        import json as _json_local
        # Load inline and file-based guard configs; file overrides inline keys when both present
        def _load_json_file(path_str: str | None):
            if not path_str:
                return None
            try:
                return _json_local.loads(_P(path_str).read_text(encoding="utf-8"))
            except Exception:
                return None
        guard_abs_obj = _json_local.loads(guard_abs) if guard_abs else None
        guard_abs_file_obj = _load_json_file(guard_abs_file)
        if guard_abs_file_obj:
            guard_abs_obj = {**(guard_abs_obj or {}), **guard_abs_file_obj}
        guard_delta_obj = _json_local.loads(guard_delta) if guard_delta else None
        guard_delta_file_obj = _load_json_file(guard_delta_file)
        if guard_delta_file_obj:
            guard_delta_obj = {**(guard_delta_obj or {}), **guard_delta_file_obj}
        # Policy auto-load from config/stress_policy.json if still missing
        if (guard_abs_obj is None or guard_delta_obj is None):
            try:
                from pathlib import Path as _PathLocal
                pol_path = _PathLocal("config") / "stress_policy.json"
                if pol_path.exists():
                    pol = _json_local.loads(pol_path.read_text(encoding="utf-8"))
                    key = None
                    if symbol and interval:
                        key = f"{symbol}@{interval}"
                    prof = (pol.get(key) if key else None) or pol.get("default") or {}
                    if guard_abs_obj is None and "abs" in prof:
                        guard_abs_obj = prof["abs"]
                    if guard_delta_obj is None and "delta" in prof:
                        guard_delta_obj = prof["delta"]
            except Exception:
                pass
        if (guard_abs_obj or guard_delta_obj) and not df_out.empty:
            # Evaluate per model baseline if model col exists, else single baseline
            models = sorted(df_out["model"].dropna().unique().tolist()) if "model" in df_out.columns else [None]
            for m in models:
                sub = df_out[df_out["model"] == m] if m is not None else df_out
                base = sub[sub["scenario"] == "baseline"].head(1)
                b = base.iloc[0] if not base.empty else None
                for _, row in sub.iterrows():
                    if row.get("error") or _pd.isna(row.get("mae")):
                        continue
                    # Absolute
                    if guard_abs_obj:
                        if "mae_max" in guard_abs_obj and float(row["mae"]) > float(guard_abs_obj["mae_max"]):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "mae", "val": float(row["mae"]), "limit": float(guard_abs_obj["mae_max"])})
                        if "rmse_max" in guard_abs_obj and float(row["rmse"]) > float(guard_abs_obj["rmse_max"]):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "rmse", "val": float(row["rmse"]), "limit": float(guard_abs_obj["rmse_max"])})
                        if "corr_min" in guard_abs_obj and _pd.notna(row["corr"]) and float(row["corr"]) < float(guard_abs_obj["corr_min"]):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "corr", "val": float(row["corr"]), "limit": float(guard_abs_obj["corr_min"])})
                        if "hitrate_min" in guard_abs_obj and _pd.notna(row["hitrate"]) and float(row["hitrate"]) < float(guard_abs_obj["hitrate_min"]):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "hitrate", "val": float(row["hitrate"]), "limit": float(guard_abs_obj["hitrate_min"])})
                    # Delta vs baseline
                    if guard_delta_obj and b is not None and _pd.notna(b.get("mae")):
                        if "mae_max" in guard_delta_obj and float(row["mae"]) - float(b["mae"]) > float(guard_delta_obj["mae_max"]):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "mae_delta", "val": float(row["mae"]) - float(b["mae"]), "limit": float(guard_delta_obj["mae_max"])})
                        if "rmse_max" in guard_delta_obj and float(row["rmse"]) - float(b["rmse"]) > float(guard_delta_obj["rmse_max"]):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "rmse_delta", "val": float(row["rmse"]) - float(b["rmse"]), "limit": float(guard_delta_obj["rmse_max"])})
                        if "corr_drop_max" in guard_delta_obj and _pd.notna(row["corr"]) and (_pd.notna(b["corr"]) and (float(b["corr"]) - float(row["corr"])) > float(guard_delta_obj["corr_drop_max"])):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "corr_drop", "val": float(b["corr"]) - float(row["corr"]), "limit": float(guard_delta_obj["corr_drop_max"])})
                        if "hitrate_drop_max" in guard_delta_obj and _pd.notna(row["hitrate"]) and (_pd.notna(b["hitrate"]) and (float(b["hitrate"]) - float(row["hitrate"])) > float(guard_delta_obj["hitrate_drop_max"])):
                            violations.append({"model": m or "hybrid", "scenario": row["scenario"], "metric": "hitrate_drop", "val": float(b["hitrate"]) - float(row["hitrate"]), "limit": float(guard_delta_obj["hitrate_drop_max"])})
    except Exception:
        violations = []

    df_out.to_csv(out_csv, index=False)
    # Residuals output
    if residual_rows is not None:
        out_res = out_dir_p / f"{stem}_residuals.csv"
        _pd.DataFrame(residual_rows).to_csv(out_res, index=False)
    payload = {"ok": True, "csv": str(out_csv), "scenarios": list(scenarios_map.keys())}
    if violations:
        # Write guard summary JSON
        guard_path = out_dir_p / f"{stem}_guard.json"
        import json as _json_dump
        guard_path.write_text(_json_dump.dumps({"violations": violations}, indent=2), encoding="utf-8")
        payload["guard"] = {"violations": violations, "json": str(guard_path)}
        if fail_on_violation:
            typer.echo(_json.dumps(payload, indent=2))
            raise typer.Exit(2)
    typer.echo(_json.dumps(payload, indent=2))


@app.command(name="report_stress")
def report_stress_cli(
    metrics_csv: str = typer.Argument(..., help="CSV produced by stress_hybrid_bars"),
    out_html: str = typer.Option(None, help="Output HTML path (defaults to same stem .html in logs/)"),
    title: str = typer.Option("Adversarial Stress Test Summary", help="Report title"),
):
    import json as _json
    from pathlib import Path as _P
    p = _P(metrics_csv)
    if not p.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"not found: {metrics_csv}"}, indent=2)); raise typer.Exit(1)
    html = _render_stress(metrics_csv, title=title)
    if not out_html:
        out_html = str(p.with_suffix(".html"))
    _P(out_html).write_text(html, encoding="utf-8")
    typer.echo(_json.dumps({"ok": True, "html": out_html}, indent=2))


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


@app.command(name="polygon_index_options")
def polygon_index_options(
    underlying: str = typer.Argument("I:SPX", help="Polygon index underlying, e.g., I:SPX"),
    expiration_date: str = typer.Option("", help="Optional expiry YYYY-MM-DD to filter contracts"),
    limit: int = typer.Option(1000, help="Page size per request"),
    max_pages: int = typer.Option(30, help="Maximum number of pages to fetch"),
    out: str = typer.Option("", help="Optional JSON output path"),
):
    """Fetch index options contracts from Polygon and report counts.

    Example:
      python -m src.cli polygon_index_options I:SPX --expiration-date 2025-10-01 --max-pages 10 --out logs/polygon_spx_2025-10-01.json
    """
    settings = Settings()
    if not settings.has_polygon:
        typer.echo("Polygon API key missing; export POLYGON_API_KEY or set in .env")
        raise typer.Exit(code=1)
    cfg = PolygonConfig.from_env()
    client = PolygonClient(cfg)
    exp = expiration_date or None
    data = client.fetch_index_options_contracts(underlying=underlying, expiration_date=exp, limit=limit, max_pages=max_pages)
    if not data:
        typer.echo(json.dumps({"ok": False, "error": "no data"}, indent=2))
        return
    payload = {
        "ok": True,
        "underlying": underlying,
        "expiration_date": exp,
        "count": data.get("count", 0),
        "symbols_sample": (data.get("symbols") or [])[:50],
    }
    if out:
        try:
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
    typer.echo(json.dumps(payload, indent=2))


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


@app.command(name="spxw_report")
def spxw_report(
    expiry: str = typer.Argument(..., help="Expiry date YYYY-MM-DD"),
    out: str = typer.Option("logs/spxw_report.json", help="Output report path (JSON)"),
    include_agent: bool = typer.Option(True, help="Include agent textual summary when OpenAI is configured"),
    otm_percent: float = typer.Option(0.15, help="Far OTM threshold as percent above/below ATM to tag 'rockets'"),
):
    """Build an SPXW options report: chain from Polygon, compute greeks/IV/expected move, add seasonality and optional agent summary.

    - Chain: Polygon contracts for underlying SPX and given expiry
    - Pricing: Basic greeks via Black-Scholes using rough IV from bid/ask mid or a fallback
    - Seasonality: 10y index daily seasonality stats from Polygon I:SPX
    - Agent: optional summary paragraph synthesizing direction, skew, and rocket candidates
    """
    settings = Settings()
    if not settings.has_polygon:
        typer.echo(json.dumps({"ok": False, "error": "POLYGON_API_KEY not set"}, indent=2))
        raise typer.Exit(1)
    poly = PolygonClient(PolygonConfig.from_env())

    # 1) Contracts
    contracts = poly.fetch_index_options_contracts(underlying="SPX", expiration_date=expiry, limit=1000, max_pages=20) or {}
    symbols: list[str] = contracts.get("symbols") or []
    # 2) Attempt a rough ATM using index prev close (fallback midpoint of range)
    idx_prev = poly.last_trade_spx(use_cache=False)
    idx_close = None
    try:
        if isinstance(idx_prev, dict) and idx_prev.get("results"):
            idx_close = idx_prev["results"][0].get("c")
    except Exception:
        idx_close = None
    if not idx_close:
        idx_close = 5000.0  # conservative fallback

    # 3) Compute basic metrics for a curated subset
    # Keep near-ATM ±5% and far OTM 'rockets' (> otm_percent)
    def _occ_to_fields(sym: str):
        from .datafeeds.options_chain import parse_occ_symbol as _parse
        c = _parse(sym)
        return c

    # Normalize symbols by stripping Polygon's 'O:' prefix for OCC parsing
    def _norm(sym: str) -> str:
        return sym[2:] if isinstance(sym, str) and sym.startswith("O:") else sym
    parsed = [_occ_to_fields(_norm(s)) for s in symbols]
    parsed = [c for c in parsed if c]
    # Tag rocket candidates
    rockets: list[str] = []
    curated_syms: list[str] = []
    for c in parsed:
        k = float(c.strike)
        cp = c.call_put
        moneyness = (k - idx_close) / idx_close
        is_near = abs(moneyness) <= 0.05
        is_rocket = (cp == 'C' and moneyness >= otm_percent) or (cp == 'P' and -moneyness >= otm_percent)
        if is_near or is_rocket:
            curated_syms.append(c.symbol)
            if is_rocket:
                rockets.append(c.symbol)

    # Pull snapshots for curated symbols (best-effort; cap to 200 to keep fast)
    curated_syms = curated_syms[:200]
    quotes: list[dict] = []
    for s in curated_syms:
        snap = poly.fetch_option_snapshot(f"O:{s}") or {}
        q = {"symbol": s, "raw": snap}
        # Heuristic mid IV / price extraction
        try:
            legs = snap.get("results", {}).get("details", {})
        except Exception:
            legs = {}
        # Basic mid price
        try:
            book = snap.get("results", {}).get("last_quote", {})
            bid = float(book.get("bid", 0) or 0)
            ask = float(book.get("ask", 0) or 0)
            mid = (bid + ask) / 2.0 if bid and ask else (bid or ask or 0.0)
        except Exception:
            bid = ask = mid = 0.0
        q.update({"bid": bid, "ask": ask, "mid": mid})
        quotes.append(q)

    # 3b) Derive ATM IV, RR25, and expected move (best-effort)
    def _parse_occ(sym: str):
        from .datafeeds.options_chain import parse_occ_symbol as _parse
        return _parse(sym)

    def _snap_iv_delta(sym: str):
        try:
            snap = poly.fetch_option_snapshot(f"O:{sym}") or {}
            res = snap.get("results") or {}
            g = res.get("greeks") or {}
            iv = g.get("implied_volatility") or g.get("iv") or g.get("impliedVolatility")
            d = g.get("delta") or g.get("Delta")
            return (float(iv), float(d) if d is not None else None) if iv is not None else None
        except Exception:
            return None

    from .analytics.iv_surface import Quote as _IVQ, atm_iv as _ATM, risk_reversal as _RR, expected_move as _EM
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    iv_quotes: list[_IVQ] = []
    # Use prev close as spot proxy for EM fallbacks
    _spot = float(idx_close)
    # Prioritize symbols near ATM; widen if too few
    parsed_cur = [(_parse_occ(s), s) for s in curated_syms]
    parsed_cur = [(c, s) for c, s in parsed_cur if c]
    def _moneyness(c):
        try:
            return abs((float(c.strike) - _spot) / max(1.0, _spot))
        except Exception:
            return 1e9
    near_syms = [s for c, s in sorted(parsed_cur, key=lambda t: _moneyness(t[0])) if _moneyness(c) <= 0.02]
    if len(near_syms) < 30:
        near_syms = [s for c, s in sorted(parsed_cur, key=lambda t: _moneyness(t[0])) if _moneyness(c) <= 0.05]
    near_syms = near_syms[:120]

    # Helper to recover mid from snapshot -> quotes -> trades
    def _latest_mid(sym: str):
        try:
            snap = poly.fetch_option_snapshot(f"O:{sym}") or {}
            last_q = (snap.get("results") or {}).get("last_quote", {})
            bid = float(last_q.get("bid") or last_q.get("bp") or 0.0)
            ask = float(last_q.get("ask") or last_q.get("ap") or 0.0)
            mid = (bid + ask) / 2.0 if bid and ask else (bid or ask or 0.0)
            if not mid:
                last_t = (snap.get("results") or {}).get("last_trade", {})
                lt_px = float(last_t.get("price") or last_t.get("p") or 0.0)
                if lt_px > 0:
                    mid = lt_px
            if mid and (bid or ask):
                return (mid, bid, ask)
        except Exception:
            pass
        try:
            end_dt = _dt.now(_tz.utc)
            start_dt = end_dt - _td(minutes=360)
            qdf = poly.fetch_option_quotes(sym, start=start_dt, end=end_dt)
            if qdf is not None and not qdf.empty:
                qdf = qdf.sort_values("timestamp")
                usable = qdf[(qdf["bid"] > 0) & (qdf["ask"] > 0)]
                if not usable.empty:
                    last = usable.iloc[-1]
                    b = float(last["bid"]); a = float(last["ask"])
                    m = (b + a) / 2.0
                    if m > 0:
                        return (m, b, a)
            tdf = poly.fetch_option_trades(sym, start=start_dt, end=end_dt)
            if tdf is not None and not tdf.empty:
                tdf = tdf.sort_values("timestamp")
                px = float(tdf.iloc[-1]["price"])  # last trade
                if px > 0:
                    return (px, 0.0, 0.0)
        except Exception:
            pass
        return None

    # Harvest snapshot greeks first, else compute from mid price if possible
    for s in near_syms:  # focused near-ATM set
        info = _parse_occ(s)
        if not info:
            continue
        right = (info.call_put or "C").upper()[:1]
        strike = float(info.strike)
        sv = _snap_iv_delta(s)
        if sv is not None:
            iv, d = sv
            if 0.01 <= iv <= 3.0:
                # If delta missing, approximate from BS
                from .utils.bs_greeks import delta as _dlt
                dlt = float(d) if d is not None else float(_dlt(S=_spot, K=strike, r=0.0, T=1/252, right=("C" if right == "C" else "P"), sigma=iv, q=0.0))
                iv_quotes.append(_IVQ(right=right, strike=strike, iv=float(iv), delta=dlt, ttm=1/252))
            continue
        # price-based fallback from the queued quotes list
        mid_bid_ask = _latest_mid(s)
        if not mid_bid_ask:
            continue
        mid, _b, _a = mid_bid_ask
        try:
            from .utils.bs_greeks import implied_vol_newton as _ivsolve, delta as _dlt
            right_lit = "C" if right == "C" else "P"
            iv = float(_ivsolve(target_price=mid, S=_spot, K=strike, r=0.0, T=1/252, right=right_lit, initial=0.35))
            if 0.01 <= iv <= 3.0:
                dlt = float(_dlt(S=_spot, K=strike, r=0.0, T=1/252, right=right_lit, sigma=iv, q=0.0))
                iv_quotes.append(_IVQ(right=right, strike=strike, iv=iv, delta=dlt, ttm=1/252))
        except Exception:
            pass

    atm_iv = _ATM(iv_quotes, target_ttm_years=1/252) if iv_quotes else None
    rr25 = _RR(iv_quotes, abs_delta=0.25, target_ttm_years=1/252) if iv_quotes else None
    em_1sig = _EM(spot=_spot, iv_atm=atm_iv or 0.0, days=1.0, z=1.0) if atm_iv else None

    # Fallback: if IV metrics are missing, try cached compute_iv_em output in logs
    if (atm_iv is None) or (rr25 is None) or (em_1sig is None):
        try:
            import os, glob, json
            pattern = os.path.join("logs", f"iv_em_{expiry}*.json")
            candidates = glob.glob(pattern)
            if candidates:
                latest = max(candidates, key=os.path.getmtime)
                with open(latest, "r", encoding="utf-8") as fh:
                    cached = json.load(fh) or {}
                c_atm = cached.get("atm_iv")
                c_rr = cached.get("rr25")
                c_em = cached.get("expected_move_1sigma")
                if atm_iv is None and (c_atm is not None):
                    try:
                        atm_iv = float(c_atm)
                    except Exception:
                        pass
                if rr25 is None and (c_rr is not None):
                    try:
                        rr25 = float(c_rr)
                    except Exception:
                        pass
                if em_1sig is None and (c_em is not None):
                    em_1sig = c_em
        except Exception:
            pass

    # 4) Seasonality (10y)
    seas = poly.fetch_index_daily_aggs("I:SPX", years=10)
    seas_summary = None
    if isinstance(seas, pd.DataFrame) and not seas.empty:
        # Day-of-week and month seasonal averages
        dow = seas.groupby("dow")["ret"].agg(["mean", "std", "count"]).reset_index().to_dict(orient="records")
        mon = seas.groupby("month")["ret"].agg(["mean", "std", "count"]).reset_index().to_dict(orient="records")
        seas_summary = {"dow": dow, "month": mon}

    # 5) Optional agent summary
    agent_text = None
    if include_agent and settings.has_openai:
        try:
            wrapper = OpenAIWrapper(settings.openai_api_key)
            sample = quotes[:30]
            prompt = {
                "context": {
                    "expiry": expiry,
                    "atm_close": idx_close,
                    "rocket_threshold_pct": otm_percent,
                    "rocket_candidates": rockets[:20],
                },
                "observations": sample,
                "seasonality": seas_summary or {},
            }
            agent_text = wrapper.summarize_market(prompt, max_tokens=250)
        except Exception as exc:  # noqa: BLE001
            agent_text = f"(agent unavailable: {exc})"

    report = {
        "ok": True,
        "expiry": expiry,
        "contracts_total": len(symbols),
        "curated_count": len(curated_syms),
        "rockets_count": len(rockets),
        "rockets_sample": rockets[:30],
        "idx_close_guess": idx_close,
        "quotes_sample": quotes[:25],
        "iv_metrics": {"atm_iv": atm_iv, "rr25": rr25, "expected_move_1sigma": em_1sig},
        "seasonality": seas_summary,
        "agent": agent_text,
    }
    try:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        pass
    typer.echo(json.dumps({k: report[k] for k in ("ok","expiry","contracts_total","curated_count","rockets_count")}, indent=2))


@app.command(name="rocket_watch")
def rocket_watch(
    expiry: str = typer.Argument(..., help="Expiry date YYYY-MM-DD (SPXW weeklies)"),
    minutes: int = typer.Option(60, help="Duration to watch (minutes)"),
    poll: float = typer.Option(3.0, help="Polling seconds between symbol batches"),
    preset: str = typer.Option(None, help="Optional preset for tuned gate defaults (e.g., 'mimic_ex_post')", case_sensitive=False),
    otm_percent: float = typer.Option(0.12, help="Far OTM threshold as percent from ATM (e.g., 0.12 = 12%)"),
    near_pct: float = typer.Option(0.08, help="Near-ATM band percentage for inclusion"),
    min_price: float = typer.Option(0.05, help="Minimum mid price to consider a rocket candidate active"),
    batch_size: int = typer.Option(60, help="Symbols to poll per batch (round-robin)"),
    max_curated: int = typer.Option(200, help="Maximum curated symbols to track"),
    notify: bool = typer.Option(True, help="Desktop notifications via PowerShell script when alerts fire"),
    out: str = typer.Option("logs/rocket_alerts.jsonl", help="Output alerts JSONL path"),
    # New tunables for momentum gate
    pct_thresh_cheap: float = typer.Option(0.7, help="Percent rise threshold for rockets starting below $0.50 (e.g., 0.7=+70%)"),
    pct_thresh: float = typer.Option(0.4, help="Percent rise threshold for rockets starting at/above $0.50 (e.g., 0.4=+40%)"),
    uptick_ratio: float = typer.Option(0.5, help="Required fraction of upticks in the window [0-1]"),
    window_sec: float = typer.Option(75.0, help="Lookback seconds for momentum window"),
    debounce_sec: float = typer.Option(180.0, help="Minimum seconds between alerts per symbol"),
    record_ticks: str = typer.Option("", help="Optional CSV path to record per-tick mids for offline replay (e.g., logs/rocket_ticks_<expiry>.csv)"),
    record_all_hours: bool = typer.Option(False, help="If true, record tick rows even outside include-hours (useful for after-hours smoke tests)"),
    include_hours: str = typer.Option("09:35-16:00", help="US/Eastern windows to allow alerts (e.g., 'HH:MM-HH:MM;HH:MM-HH:MM')"),
    midday_conditional_hours: str = typer.Option("", help="Optional US/Eastern window(s) allowed only when abs(index trend) >= index_trend_min_bp (e.g., '11:45-12:30')"),
    max_spread_frac: float = typer.Option(0.005, help="Max (ask-bid)/mid fraction (e.g., 0.005 = 0.5%)"),
    max_spread_abs: float = typer.Option(0.20, help="Max absolute spread in $; 0 uses dynamic bucket caps"),
    min_nbbo_size: float = typer.Option(30.0, help="Min both bid_size and ask_size to consider liquid"),
    quote_age_ms_max: float = typer.Option(250.0, help="Max quote age in milliseconds using bid/ask time fields if present"),
    news_pause_threshold: float = typer.Option(0.3, help="If abs(latest sentiment_score) >= threshold, pause alerts"),
    news_cooldown_sec: float = typer.Option(300.0, help="Cool-down seconds after a sentiment pause is triggered"),
    require_index_trend: bool = typer.Option(True, help="Require SPX trend alignment with option direction (calls up, puts down)"),
    index_trend_window_sec: float = typer.Option(60.0, help="SPX trend lookback window seconds"),
    index_trend_min_bp: float = typer.Option(15.0, help="Minimum absolute SPX trend over window in basis points (0.01% = 1bp)"),
    require_vwap_alignment: bool = typer.Option(True, help="Require SPX price relative to intraday VWAP to align with direction (calls above, puts below)"),
    vwap_min_margin_bp: float = typer.Option(0.0, help="If >0, require SPX to clear VWAP by at least this many basis points in the direction of the trade (calls above, puts below)"),
    use_indicator_bias: bool = typer.Option(True, help="Gate on MACD/RSI/EMA bias (calls need macd>signal, rsi>=bull_min, close>=ema; puts opposite)"),
    rsi_bull_min: float = typer.Option(48.0, help="Minimum RSI for bullish bias (calls)"),
    rsi_bear_max: float = typer.Option(52.0, help="Maximum RSI for bearish bias (puts)"),
    # RSI floors tied to VIX strength for calls
    rsi_bull_min_extra_if_vix_not_down: float = typer.Option(2.0, help="Add this to rsi_bull_min if VIX is not trending down enough for calls"),
    rsi_bull_min_relax_if_vix_down_strong: float = typer.Option(1.0, help="Subtract this from rsi_bull_min if VIX is strongly down for calls"),
    vix_strong_down_min_bp: float = typer.Option(12.0, help="Threshold (bp) to consider VIX strongly down for RSI relaxation"),
    require_news_directional_bias: bool = typer.Option(True, help="Require alignment with latest news agent directional_bias when fresh/confident"),
    news_bias_min_conf: float = typer.Option(0.5, help="Minimum confidence required for news directional_bias to be enforced [0-1]"),
    news_bias_staleness_sec: float = typer.Option(1800.0, help="Consider news directional_bias only if generated within this many seconds"),
    ensure_both_rights: bool = typer.Option(True, help="Ensure curated set includes both calls and puts per strike"),
    # VIX + indicator gates parity with replay
    use_vix_trend: bool = typer.Option(True, help="Require VIX trend to align with option direction (VIX up -> puts, VIX down -> calls)"),
    vix_puts_only: bool = typer.Option(False, help="If true, enforce VIX gate only for puts (calls ignore VIX gate)"),
    vix_trend_window_sec: float = typer.Option(60.0, help="Lookback seconds for VIX trend window"),
    vix_trend_min_bp: float = typer.Option(8.0, help="Minimum absolute VIX trend in basis points to enforce gate"),
    # Expected Move (EM) gate (parity with replay; uses index trend window for EM horizon)
    em_gate: bool = typer.Option(False, help="If true, require absolute SPX move over the trend window to exceed EM threshold (bp * multiplier)"),
    em_bp: float = typer.Option(4.0, help="Baseline expected-move threshold in basis points for EM gate"),
    em_mult: float = typer.Option(1.0, help="Multiplier applied to EM bp threshold (e.g., 0.8/1.0/1.2)"),
    # Light EM when trend is marginal (additional guardrail for calls)
    marginal_trend_extra_em_gate: bool = typer.Option(False, help="If true, apply a lighter EM threshold when index trend is only marginally above the minimum bp for calls"),
    marginal_trend_window_bp: float = typer.Option(1.0, help="Window (in bp) above the minimum trend requirement considered 'marginal' for applying the light EM gate"),
    em_light_bp: float = typer.Option(2.0, help="Light EM threshold in basis points used when trend is marginal and marginal_trend_extra_em_gate is enabled"),
    # Greeks-only mode (optional)
    greeks_only: bool = typer.Option(False, help="If true, bypass momentum/index/news gates and alert solely by greeks thresholds"),
    delta_min: float = typer.Option(0.02, help="Minimum absolute delta to include (e.g., 0.02)"),
    delta_max: float = typer.Option(0.30, help="Maximum absolute delta to include (e.g., 0.30)"),
    iv_min: float = typer.Option(0.0, help="Minimum implied volatility to include (0 disables)"),
    iv_max: float = typer.Option(10.0, help="Maximum implied volatility to include (10 disables)"),
    gamma_max: float = typer.Option(0.0, help="If > 0, require gamma <= this value (0 disables)"),
    vega_max: float = typer.Option(0.0, help="If > 0, require vega <= this value (0 disables)"),
    use_polygon_quotes: bool = typer.Option(False, help="Use Polygon option snapshots for bid/ask when IQFeed quotes unavailable or as preferred source"),
    # New: require minimum day volume from Polygon snapshot when available (ignored for IQFeed-only)
    min_day_volume: float = typer.Option(0.0, help="If >0, require Polygon snapshot day.volume >= this (only when use_polygon_quotes)"),
    warmup_min: float = typer.Option(20.0, help="Minutes after regular session open to relax day-volume gate (NBBO quality still required)"),
    # Dynamic risk sizing recommendations embedded in alert payloads
    use_alert_size_reco: bool = typer.Option(True, help="Include size_reco in alerts for downstream PnL sizing"),
    put_pos_trend_size: float = typer.Option(0.5, help="If cp='P' and index trend>0, recommend this size multiplier (0 to skip)"),
    put_neg_trend_size: float = typer.Option(1.0, help="If cp='P' and index trend<=0, recommend this size multiplier (e.g., 0.5 to halve)"),
    call_neg_trend_size: float = typer.Option(1.0, help="If cp='C' and index trend<0, recommend this size multiplier (0 to skip)"),
    # Advanced diagnostics & adaptive features
    diagnostics_out: str = typer.Option("", help="If set, write JSON summary of gate diagnostics at end of session"),
    enable_probes: bool = typer.Option(False, help="Log hypothetical blocked call momentum probes"),
    probe_out: str = typer.Option("logs/rocket_probes.jsonl", help="Probe JSONL path (if enable_probes)"),
    # Live-only IQFeed-assisted microstructure guardrails (optional)
    use_nbbo_imbalance_gate: bool = typer.Option(False, help="If true, require NBBO size to favor the trade side (ask>=bid for calls, bid>=ask for puts) by a ratio"),
    min_ask_over_bid_ratio_calls: float = typer.Option(1.0, help="Minimum ask_size/bid_size ratio for calls when imbalance gate is enabled (>=1 means ask_size >= bid_size)"),
    min_bid_over_ask_ratio_puts: float = typer.Option(1.0, help="Minimum bid_size/ask_size ratio for puts when imbalance gate is enabled (>=1 means bid_size >= ask_size)"),
    min_quote_rate_hz: float = typer.Option(0.0, help="If >0, require per-symbol quote rate (quotes/sec over last 10s) to be at least this value"),
    adapt_calls: bool = typer.Option(False, help="Adaptive relaxation ladder for CALL gates if zero call alerts"),
    adapt_no_call_minutes: str = typer.Option("20,35,50", help="Minute marks after start to advance adaptive call relax stages"),
    adapt_index_trend_bp_seq: str = typer.Option("2,1,0", help="Sequence of index_trend_min_bp values per adaptive stage"),
    adapt_disable_indicator_stage: int = typer.Option(2, help="Stage at which to disable indicator bias for calls"),
    adapt_disable_vix_stage: int = typer.Option(3, help="Stage at which to disable VIX gate for calls"),
    adapt_disable_trend_stage: int = typer.Option(4, help="Stage at which to disable index trend gate for calls"),
    index_trend_smooth_sec: float = typer.Option(0.0, help="If >0 apply simple smoothing horizon for index trend bp calc"),
):
    """Watch SPXW for far OTM 'rocket' bursts using IQFeed quotes and Polygon chain discovery.

    Alerts when very cheap OTM options show rapid mid-price momentum. Writes JSONL alerts and optional desktop notifications.
    """
    import subprocess, sys as _sys, time as _time
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path

    settings = Settings()
    # Agent-only decision replaces trend/VWAP/RSI/VIX gates; keep safety, momentum, debounce
    use_agent_decider = True
    # Local defaults for preset-tunable flags that may not be declared as parameters in this command variant
    # These are referenced inside some preset branches; set sane defaults here to avoid UnboundLocalError
    use_global_index_trend = False
    require_sma_alignment = True
    # Normalize default alerts path to date-stamped convention if caller used the default
    try:
        from pathlib import Path as _Path
        _out_path = _Path(out)
        if not out or _out_path.as_posix().endswith("logs/rocket_alerts.jsonl"):
            out = f"logs/rocket_alerts_{expiry.replace('-', '')}.jsonl"
    except Exception:
        # Best-effort; if anything goes wrong, keep provided path
        pass
    # Ensure alerts file exists as early as possible so downstream ops can detect runs
    try:
        _early_alerts_path = _Path(out)
        _early_alerts_path.parent.mkdir(parents=True, exist_ok=True)
        if not _early_alerts_path.exists():
            _early_alerts_path.write_text("", encoding="utf-8")
    except Exception:
        # Non-fatal: continue even if early creation fails
        pass
    # --- Optional: Start a light Polygon WS ATM slice to warm quotes (before heavy setup) ---
    _poly_ws = None
    if poly_ws_light:
        try:
            from src.datafeeds.polygon_ws import PolygonWS as _PolygonWS
            import atexit as _atexit
            _poly_ws = _PolygonWS()
            _uls = [s.strip().upper() for s in (poly_ws_underlyings or "SPY,QQQ,IWM").split(",") if s.strip()]
            seen_ul = set(); _ul_list = []
            for s in _uls:
                if s and s not in seen_ul:
                    _ul_list.append(s); seen_ul.add(s)
            if _ul_list:
                try:
                    _poly_ws.start_stocks(_ul_list)
                except Exception:
                    pass
            try:
                _poly_ws.start_indices(["I:SPX", "I:VIX"])  # regime inputs
            except Exception:
                pass
            try:
                _ = _poly_ws.start_options_atm_slice(
                    _ul_list,
                    days_ahead=int(poly_ws_days_ahead),
                    max_expirations=int(poly_ws_max_expirations),
                    per_type_strikes=int(poly_ws_per_type_strikes),
                    batch_size=int(poly_ws_batch_size),
                    sleep_between=float(poly_ws_sleep_between),
                    active_only=True,
                    max_total=4000,
                )
            except Exception:
                pass
            # Ensure cleanup on process exit
            def _stop_ws():
                try:
                    _poly_ws.stop_all()
                except Exception:
                    pass
            _atexit.register(_stop_ws)
        except Exception:
            _poly_ws = None

    # Apply tuned defaults for known presets; only override when values are at their defaults
    _preset = (preset or "").strip().lower() if preset else None
    if _preset == "quick_win":
            # Targeted windows
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:45;11:45-15:00"
            # Make 11:45-12:30 conditional on trend being on
            if not midday_conditional_hours:
                midday_conditional_hours = "11:45-12:30"
            # Momentum tuning for sparse cadence
            if float(min_price) == 0.05:
                min_price = 0.10
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.05
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.02
            if float(window_sec) == 75.0:
                window_sec = 300.0
            if float(uptick_ratio) == 0.5:
                uptick_ratio = 0.2
            # Debounce unchanged
            # Liquidity/spread
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.10
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.15
            # Trend/EM/VIX/Indicators
            if bool(require_index_trend) is not False:
                require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 120.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 3.0
            if bool(em_gate) is not False:
                em_gate = True
            if float(em_bp) == 4.0:
                em_bp = 3.0
            if float(em_mult) == 1.0:
                em_mult = 1.0
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 6.0
            # Prefer VIX puts-only in this preset so calls aren't blocked by VIX
            if bool(vix_puts_only) is not True:
                vix_puts_only = True
            if bool(use_indicator_bias) is not False:
                use_indicator_bias = True
            if bool(require_sma_alignment) is not False:
                require_sma_alignment = True
            # Day volume off for diagnostics/live ramp
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
    elif _preset == "mimic_ex_post":
            # Contract selection: keep both rights and broad coverage
            ensure_both_rights = True
            # Momentum defaults tuned for quick entries and follow-through
            if float(min_price) == 0.05:
                min_price = 0.30
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.40
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.25
            if float(window_sec) == 75.0:
                window_sec = 60.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 180.0
            # RTH windows emphasizing early push and midday trend windows
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:45;11:45-15:00"
            # Liquidity
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.015
            # Note: min_nbbo_size and quote_age_ms_max are live-only tunables; not applicable in replay
            # Index/VWAP/Indicators
            require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 60.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 12.0
            require_vwap_alignment = True
            use_indicator_bias = True
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 50.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 50.0
            # News: keep but less restrictive by confidence
            if float(news_bias_min_conf) == 0.5:
                news_bias_min_conf = 0.6
            if float(news_bias_staleness_sec) == 1800.0:
                news_bias_staleness_sec = 1200.0
            # VIX trend alignment
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 6.0
            # EM gate: keep default 3-4bp horizon if enabled explicitly by caller
            # (do not force-enable here; allow explicit CLI to control)
            # Greeks band stays at 0.02-0.30 per requirements
            # (Removed auto day-volume safeguard to respect explicit 0 => no volume filter)
    elif _preset == "calls_loose_directional":
            # Objective: produce call alerts on mild bullish drift days where strict trend/SMA gates suppress entries.
            # Strategy: lower momentum thresholds vs mimic_ex_post, widen spreads slightly, relax trend bp, drop SMA alignment,
            # keep indicator + VIX trend (full alignment), disable min day volume (discovery), keep EM gate optional.
            ensure_both_rights = True
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:45;11:45-15:15"
            if float(min_price) == 0.05:
                min_price = 0.15
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.18  # require +18% for sub-0.50
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.12        # require +12% for >=0.50
            if float(window_sec) == 75.0:
                window_sec = 180.0       # longer window to accumulate soft drift
            if float(debounce_sec) == 180.0:
                debounce_sec = 120.0
            # Spreads: allow moderate widening to capture candidates earlier
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.18
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.12
            # Trend + VIX + indicators (retain alignment but soften threshold)
            require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 90.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 2.0  # much lower bp requirement for gentle up moves
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            use_indicator_bias = True
            # Relax RSI bands slightly to avoid blocking neutral zones
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 47.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 53.0
            # Disable SMA alignment to prevent double trend suppression
            require_sma_alignment = False
            # Volume: allow discovery
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
            # Keep EM gate off unless explicitly requested (let mild moves through)
            if bool(em_gate) is True:
                em_gate = False
    elif _preset == "calls_reversal_vix_crush":
            # Objective: capture post-morning liquidation reversals where SPX stabilizes and VIX begins a sustained bleed.
            # Approach: require recent negative-to-positive inflection (we simulate via very low positive bp + allow earlier negative trend),
            # enforce strong negative VIX trend, disable SMA & EM gates, lighten momentum and spreads moderately.
            ensure_both_rights = True
            if include_hours == "09:35-16:00":
                include_hours = "10:05-11:30;12:30-15:10"  # skip chaotic first 30m, focus on reversal + midday continuation
            if float(min_price) == 0.05:
                min_price = 0.12
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.16
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.10
            if float(window_sec) == 75.0:
                window_sec = 150.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 90.0
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.18
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.10
            # Trend: keep requirement but very low bp to allow early curl; rely on VIX confirmation
            require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 75.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 1.0
            # VIX: strict negative trend (down) with larger magnitude (crush)
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 6.0  # demand >=6bp absolute over window with direction alignment (down for calls)
            vix_puts_only = False
            # Indicators: keep but relax RSI to neutral band, MACD still needs crossover, EMA alignment retained
            use_indicator_bias = True
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 46.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 54.0
            require_sma_alignment = False
            # Disable EM gate for reversal catch
            if bool(em_gate) is True:
                em_gate = False
            # Volume discovery on
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
    elif _preset == "greeks_min_gates":
            # Replay minimal guardrails: prioritize greeks-only, disable heavy gates, keep light VIX trend
            # Liquidity relaxation (preserve explicit overrides)
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.020
            # Heavy gates off unless explicitly disabled already
            if bool(require_index_trend) is True:
                require_index_trend = False
            if bool(em_gate) is True:
                em_gate = False
            if bool(use_global_index_trend) is True:
                use_global_index_trend = False
            if bool(use_indicator_bias) is True:
                use_indicator_bias = False
            if bool(require_sma_alignment) is True:
                require_sma_alignment = False
            # Keep VIX alignment but lighter threshold if at default
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            # Enable greeks-only and focus delta band if defaults
            greeks_only = True
            if float(delta_min) == 0.02:
                delta_min = 0.04
            if float(delta_max) == 0.30:
                delta_max = 0.35
            # (Removed auto day-volume safeguard to respect explicit 0)
    elif _preset == "greeks_min_gates":
            # Minimal guardrails preset: greeks-only with light VIX gate, relaxed liquidity; disable heavy trend/VWAP/news gates
            greeks_only = True
            # Liquidity relaxations (keep reasonable bounds)
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.020
            if float(min_nbbo_size) == 30.0:
                min_nbbo_size = 5.0
            if float(quote_age_ms_max) == 250.0:
                quote_age_ms_max = 500.0
            # Disable heavier index/VWAP/news constraints
            require_index_trend = False
            require_vwap_alignment = False
            use_indicator_bias = False
            require_news_directional_bias = False
            # Keep VIX alignment but slightly relax threshold
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            # Narrow greeks band modestly to focus on tradable deltas
            if float(delta_min) == 0.02:
                delta_min = 0.04
            if float(delta_max) == 0.30:
                delta_max = 0.35
            # (Removed auto day-volume safeguard to respect explicit 0)
    elif _preset == "greeks_min_gates":
            # Minimal guardrails for replay: greeks-only selection with light VIX; disable heavy trend/EM/indicator gates
            greeks_only = True
            # Relax spread fraction slightly; keep absolute cap unchanged
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.20
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.020
            # Disable index trend and EM gating in replay to avoid over-filtering
            require_index_trend = False
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 60.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 12.0
            em_gate = False
            use_global_index_trend = False
            # Keep VIX alignment with relaxed threshold
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            # Disable indicator bias requirements
            use_indicator_bias = False
            require_sma_alignment = False
            # Narrow greeks band modestly to focus on tradable deltas
            if float(delta_min) == 0.02:
                delta_min = 0.04
            if float(delta_max) == 0.30:
                delta_max = 0.35
            # (Removed auto day-volume safeguard to respect explicit 0)
    elif _preset == "greeks_min_gates":
            # Minimal guardrails for replay: greeks-only selection with light VIX; disable heavy trend/EM/indicator gates
            greeks_only = True
            # Relax spread fraction slightly; keep absolute cap unchanged
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.20
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.020
            # Disable index trend and EM gating in replay to avoid over-filtering
            require_index_trend = False
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 60.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 12.0
            em_gate = False
            use_global_index_trend = False
            # Keep VIX alignment with relaxed threshold
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            # Disable indicator bias requirements
            use_indicator_bias = False
            require_sma_alignment = False
            # Narrow greeks band modestly to focus on tradable deltas
            if float(delta_min) == 0.02:
                delta_min = 0.04
            if float(delta_max) == 0.30:
                delta_max = 0.35
            # (Removed auto day-volume safeguard to respect explicit 0)
    if _preset == "greeks_min_gates":
            # Minimal guardrails preset for replay: prioritize greeks-only path with light VIX, disable heavy filters
            # Liquidity: relax fractional spread a bit if at default; keep abs cap as-is
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.020
            # Heavy gates off by default under this preset (only flip if currently the default True)
            if bool(require_index_trend) is True:
                require_index_trend = False
            if bool(em_gate) is True:
                em_gate = False
            if bool(use_global_index_trend) is True:
                use_global_index_trend = False
            if bool(use_indicator_bias) is True:
                use_indicator_bias = False
            if bool(require_sma_alignment) is True:
                require_sma_alignment = False
            # Keep VIX alignment but slightly relax threshold if at default
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            # Enable greeks-only selection
            greeks_only = True
            # Focus on tradable deltas if at default range
            if float(delta_min) == 0.02:
                delta_min = 0.04
            if float(delta_max) == 0.30:
                delta_max = 0.35
    elif _preset == "open_loose":
            # Market open ramp: very permissive momentum gates, disable heavy filters, keep a light VIX alignment.
            # Windows: focus on the first 45 minutes and midday continuation if default
            ensure_both_rights = True
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:30;11:45-15:10"
            # Momentum/price thresholds (loose)
            if float(min_price) == 0.05:
                min_price = 0.10
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.12
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.08
            if float(window_sec) == 75.0:
                window_sec = 150.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 90.0
            # Liquidity: allow slightly wider spreads and older quotes at open
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.20
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.025
            if float(min_nbbo_size) == 30.0:
                min_nbbo_size = 5.0
            if float(quote_age_ms_max) == 250.0:
                quote_age_ms_max = 500.0
            # Disable heavier index/VWAP/news/indicator constraints
            require_index_trend = False
            require_vwap_alignment = False
            use_indicator_bias = False
            require_news_directional_bias = False
            # Keep VIX alignment but relax threshold
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 3.0
            vix_puts_only = False
            # Disable EM gate at open to avoid suppressing early probes
            if bool(em_gate) is True:
                em_gate = False
            # Keep volume safeguard off unless explicitly provided
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
    # End preset tuning block
    # 1) Discover chain via Polygon (SPX underlying)
    symbols: list[str] = []
    poly_client_global = None
    try:
        if settings.has_polygon:
            poly_client = PolygonClient(PolygonConfig.from_env())
            poly_client_global = poly_client
            # Primary: fetch with explicit expiry filter
            chain = poly_client.fetch_index_options_contracts(underlying="SPX", expiration_date=expiry, limit=1000, max_pages=20) or {}
            symbols = chain.get("symbols") or []
            # Merge index-underlying contracts too (I:SPX), dedupe
            try:
                chain_idx = poly_client.fetch_index_options_contracts(underlying="I:SPX", expiration_date=expiry, limit=1000, max_pages=20) or {}
                syms_idx = chain_idx.get("symbols") or []
                if syms_idx:
                    symbols = list(dict.fromkeys((symbols or []) + syms_idx))
            except Exception:
                pass
            # Fallback: if empty, fetch all then filter locally by expiry
            if not symbols:
                all_chain = poly_client.fetch_index_options_contracts(underlying="SPX", expiration_date=None, limit=1000, max_pages=20) or {}
                all_syms = all_chain.get("symbols") or []
                if all_syms:
                    from .datafeeds.options_chain import parse_occ_symbol as _parse_occ
                    def _norm(sym: str) -> str:
                        return sym[2:] if isinstance(sym, str) and sym.startswith("O:") else sym
                    target = expiry
                    filtered = []
                    for s in all_syms:
                        c = _parse_occ(_norm(s))
                        if not c:
                            continue
                        if c.expiry.strftime("%Y-%m-%d") == target:
                            filtered.append(s)
                    symbols = filtered
        else:
            LOGGER.warning("Polygon API key missing; chain discovery limited.")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Polygon chain fetch failed: %s", exc)
        symbols = []

    if not symbols:
        # Synthetic fallback: build a curated OCC list around ATM and far OTM without Polygon chain
        # Requires idx_close estimate first
        idx_close = None
        try:
            if settings.has_polygon:
                prev = poly_client.last_trade_spx(use_cache=False)
                if isinstance(prev, dict) and prev.get("results"):
                    idx_close = prev["results"][0].get("c")
        except Exception:
            idx_close = None
        if not idx_close:
            idx_close = 5000.0

        def _round_step(x: float, step: int = 5, up: bool = False) -> int:
            import math as _math
            return int((_math.ceil if up else _math.floor)(x / step) * step)

        def _occ_strike_field(k: int) -> str:
            # Encode as 8-digit integer of strike*1000 (index standard)
            return f"{k*1000:08d}"

        def _occ_symbol(root: str, ymd: str, cp: str, strike_int: int) -> str:
            return f"{root}{ymd}{cp}{_occ_strike_field(strike_int)}"

        # Build near band and rocket bands
        yy = int(expiry[2:4]); mm = int(expiry[5:7]); dd = int(expiry[8:10])
        ymd = f"{yy:02d}{mm:02d}{dd:02d}"
        near_lo = _round_step(idx_close * (1 - near_pct))
        near_hi = _round_step(idx_close * (1 + near_pct), up=True)
        call_lo = _round_step(idx_close * (1 + otm_percent), up=True)
        call_hi = _round_step(idx_close * 1.30, up=True)
        put_hi = _round_step(idx_close * (1 - otm_percent))
        put_lo = _round_step(max(5.0, idx_close * 0.75))

        gen: list[str] = []
        # Near band both calls/puts
        for k in range(near_lo, near_hi + 1, 5):
            gen.append(_occ_symbol("SPXW", ymd, "C", k))
            gen.append(_occ_symbol("SPXW", ymd, "P", k))
        # Rocket calls
        for k in range(call_lo, call_hi + 1, 5):
            gen.append(_occ_symbol("SPXW", ymd, "C", k))
        # Rocket puts (descending)
        for k in range(put_hi, put_lo - 1, -5):
            gen.append(_occ_symbol("SPXW", ymd, "P", k))

        # Deduplicate and cap
        seen = set(); symbols = []
        for s in gen:
            if s in seen:
                continue
            seen.add(s); symbols.append(s)
        symbols = symbols[:max_curated]
        # Note: downstream will parse and build IQFeed short symbols from these
        if not symbols:
            typer.echo(json.dumps({"ok": False, "error": "no contracts from Polygon and synthetic fallback empty"}, indent=2))
            raise typer.Exit(1)

    # 2) Index close as ATM anchor
    idx_close = None
    try:
        if settings.has_polygon:
            prev = poly_client.last_trade_spx(use_cache=False)
            if isinstance(prev, dict) and prev.get("results"):
                idx_close = prev["results"][0].get("c")
    except Exception:
        idx_close = None
    if not idx_close:
        idx_close = 5000.0

    # 3) Curate near-ATM and far OTM 'rockets'
    from .datafeeds.options_chain import parse_occ_symbol as _parse_occ
    def _norm(sym: str) -> str:
        return sym[2:] if isinstance(sym, str) and sym.startswith("O:") else sym
    parsed = [_parse_occ(_norm(s)) for s in symbols]
    parsed = [c for c in parsed if c]
    curated: list[str] = []
    for c in parsed:
        k = float(c.strike); cp = c.call_put
        m = (k - idx_close) / idx_close
        near = abs(m) <= near_pct
        rocket_zone = (cp == 'C' and m >= otm_percent) or (cp == 'P' and -m >= otm_percent)
        if near or rocket_zone:
            curated.append(c.symbol)
    curated = curated[:max_curated]
    # Optional: enforce both rights per strike
    if ensure_both_rights and curated:
        try:
            seen: set[str] = set(curated)
            add: list[str] = []
            for occ in list(curated):
                c = _parse_occ(occ)
                if not c:
                    continue
                k = int(getattr(c, "strike", 0) or 0)
                right = getattr(c, "call_put", None)
                if right not in ("C", "P"):
                    continue
                other = "P" if right == "C" else "C"
                yy = int(expiry[2:4]); mm = int(expiry[5:7]); dd = int(expiry[8:10])
                ymd = f"{yy:02d}{mm:02d}{dd:02d}"
                alt = f"SPXW{ymd}{other}{k*1000:08d}"
                if alt not in seen:
                    add.append(alt); seen.add(alt)
            curated = (curated + add)[:max_curated]
        except Exception:
            pass

    # Startup sanity log (after curated finalized)
    try:
        _startup = {
            "startup_sanity": True,
            "ts": __import__("datetime").datetime.utcnow().isoformat()+"Z",
            "expiry": expiry,
            "symbols_raw": len(symbols),
            "curated_count": len(curated),
            "pct_thresh_cheap": pct_thresh_cheap,
            "pct_thresh": pct_thresh,
            "window_sec": window_sec,
            "uptick_ratio": uptick_ratio,
            "min_day_volume": min_day_volume,
            "use_polygon_quotes": use_polygon_quotes,
            "require_index_trend": require_index_trend,
            "vix_puts_only": vix_puts_only,
            "em_gate": em_gate,
        }
        typer.echo(json.dumps(_startup, separators=(",", ":")))
    except Exception:
        pass
    if not curated:
        try:
            # Build and persist a minimal summary so observability artifacts are always present
            _alerts_path = _Path(out)
            _summary = {
                "ok": True,
                "watched": 0,
                "scanned": 0,
                "alerts": 0,
                "alerts_calls": 0,
                "alerts_puts": 0,
                "note": "no curated symbols under filters",
                "contracts": len(symbols),
                "alerts_path": str(_alerts_path),
                "ticks_path": None,
            }
            typer.echo(json.dumps(_summary, indent=2))
            # Stats file next to alerts
            try:
                _stats_path = _alerts_path.with_name(f"{_alerts_path.stem}_stats.json")
                _stats_path.write_text(json.dumps(_summary, indent=2), encoding="utf-8")
            except Exception:
                pass
            # Diagnostics file if requested
            if diagnostics_out:
                try:
                    _dp = _Path(diagnostics_out)
                    _dp.parent.mkdir(parents=True, exist_ok=True)
                    _dp.write_text(json.dumps(_summary, indent=2), encoding="utf-8")
                except Exception:
                    pass

        except Exception:
            # As a last resort, emit the basic note and return
            typer.echo(json.dumps({"ok": True, "note": "no curated symbols under filters", "contracts": len(symbols)}, indent=2))
        return

    # 4) Watch with IQFeed lookup_last

    cfg = IQFeedConfig.from_env()
    iq = IQFeedClient(cfg)
    alerts_path = _Path(out)
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure alerts file exists early for diagnostics even if no alerts are written
    try:
        if not alerts_path.exists():
            alerts_path.write_text("", encoding="utf-8")
    except Exception:
        pass
    # Live diagnostics counters
    stats_live: dict[str, int | float] = {
        "alerts": 0,
        "alerts_calls": 0,
        "alerts_puts": 0,
        "outside_hours_blocked": 0,
        "news_pause_blocked": 0,
        "midday_conditional_blocked": 0,
        "day_volume_blocked": 0,
        "mid_zero_blocked": 0,
    "mid_cached_used": 0,
        "spread_frac_blocked": 0,
        "spread_abs_blocked": 0,
        "nbbo_size_blocked": 0,
    "nbbo_imbalance_blocked": 0,
    "quote_rate_blocked": 0,
        "quote_age_blocked": 0,
        "day_volume_grace_skipped": 0,
        "trend_evaluated": 0,
        "trend_blocked": 0,
        "trend_blocked_low_bp": 0,
        "trend_blocked_wrong_dir": 0,
        "vwap_blocked": 0,
        "vwap_margin_blocked": 0,
        "ind_evaluated": 0,
        "ind_blocked": 0,
        "rsi_vix_guard_blocked": 0,
        "vix_evaluated": 0,
        "vix_blocked": 0,
        "em_evaluated": 0,
        "em_blocked": 0,
        "marginal_trend_em_blocked": 0,
        "debounce_blocked": 0,
        # Momentum reasons
        "momentum_min_price": 0,
        "momentum_too_short": 0,
        "momentum_upticks": 0,
        "momentum_pct": 0,
        # Probes / adaptive
        "probe_logged": 0,
        "adapt_stage": 0,
    }

    # Reason tally map
    reason_counts: dict[str, int] = {}
    def _tally(reason: str):
        try:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        except Exception:
            pass

    # Probes
    # Per-reason probe counts and conversions
    probe_reason_counts: dict[str, int] = {}
    probe_reason_converted: dict[str, int] = {}
    probes_pending: dict[str, list[tuple[float, str]]] = {}

    def _write_probe(rec: dict):
        if not enable_probes:
            return
        try:
            import time as __t
            sym = str(rec.get("symbol") or "")
            rsn = str(rec.get("reason") or "")
            # store a best-effort epoch for conversion windows
            ts_epoch = float(rec.get("ts_epoch") or __t.time())
            p = _Path(probe_out)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            stats_live["probe_logged"] = int(stats_live.get("probe_logged", 0)) + 1
            if rsn:
                probe_reason_counts[rsn] = probe_reason_counts.get(rsn, 0) + 1
            if sym and rsn:
                lst = probes_pending.setdefault(sym, [])
                lst.append((ts_epoch, rsn))
        except Exception:
            pass

    def _mark_probe_converted(sym: str, now_ts: float, window_sec: float = 300.0):
        try:
            if sym not in probes_pending:
                return
            win_start = now_ts - float(window_sec)
            remaining: list[tuple[float, str]] = []
            for t0, rsn in probes_pending.get(sym, []):
                if t0 >= win_start:
                    probe_reason_converted[rsn] = probe_reason_converted.get(rsn, 0) + 1
                else:
                    remaining.append((t0, rsn))
            if remaining:
                probes_pending[sym] = remaining
            else:
                probes_pending.pop(sym, None)
        except Exception:
            pass

    # Adaptive configuration parsing
    adapt_stage = 0
    try:
        adapt_minutes = [int(x.strip()) for x in adapt_no_call_minutes.split(',') if x.strip()]
    except Exception:
        adapt_minutes = []
    try:
        adapt_bp_seq = [float(x.strip()) for x in adapt_index_trend_bp_seq.split(',') if x.strip()]
    except Exception:
        adapt_bp_seq = []
    session_start_utc = time.time()
    # Early-session grace window for Polygon day volume gating (seconds)
    day_volume_grace_sec = 20 * 60.0
    # Smoothing buffer for index price values
    from collections import deque as _dq
    idx_price_buffer: _dq[tuple[float,float]] = _dq(maxlen=4000)
    # Per-symbol recent quote timestamps (for quote-rate gating)
    quote_ts_map: dict[str, _dq[float]] = {}
    # Short-lived cache of last non-zero mid per symbol to bridge brief quote gaps
    last_mid_cache: dict[str, tuple[float, float]] = {}

    state: dict[str, list[tuple[float, float]]] = {}  # sym -> list of (ts, mid)
    last_alert: dict[str, float] = {}
    last_pause_until: float = 0.0
    # Cached news directional bias
    news_ctx = {"bias": "neutral", "conf": 0.0, "fresh": False, "ts": 0.0}
    news_next_refresh: float = 0.0
    # Index context (SPX) cache
    idx_ctx: dict[str, float] = {"last": 0.0, "vwap": 0.0, "ret_bp": 0.0, "ema": 0.0, "macd": 0.0, "macd_signal": 0.0, "rsi": 50.0}
    idx_next_refresh: float = 0.0
    # Pre-parse expiry date (for time-to-expiry in greeks)
    import datetime as _gre_dt
    try:
        _exp_dt_date = _gre_dt.date(int(expiry[0:4]), int(expiry[5:7]), int(expiry[8:10]))
    except Exception:
        _exp_dt_date = None
    # Compute today's RTH session open timestamp (09:30 ET) in UTC for warmup handling
    try:
        from zoneinfo import ZoneInfo as _ZI
        import datetime as __dt
        _now_et = __dt.datetime.now(tz=_ZI('America/New_York'))
        _open_et = __dt.datetime(_now_et.year, _now_et.month, _now_et.day, 9, 30, tzinfo=_ZI('America/New_York'))
        session_open_utc = _open_et.astimezone(_ZI('UTC')).timestamp()
    except Exception:
        session_open_utc = 0.0
    # Helper: within allowed hours (US/Eastern)
    def _within_hours(now_utc: float) -> bool:
        if not include_hours:
            return True
        try:
            from zoneinfo import ZoneInfo as _ZI
            import datetime as __dt
            dt_et = __dt.datetime.fromtimestamp(now_utc, tz=_ZI('UTC')).astimezone(_ZI('America/New_York'))
            windows = [w.strip() for w in str(include_hours).split(';') if w.strip()]
            for w in windows:
                p = w.split('-')
                if len(p) != 2:
                    continue
                h0, h1 = p[0].strip(), p[1].strip()
                t0 = __dt.datetime(dt_et.year, dt_et.month, dt_et.day, int(h0[0:2]), int(h0[3:5]), tzinfo=_ZI('America/New_York'))
                t1 = __dt.datetime(dt_et.year, dt_et.month, dt_et.day, int(h1[0:2]), int(h1[3:5]), tzinfo=_ZI('America/New_York'))
                if t0 <= dt_et <= t1:
                    return True
            return False
        except Exception:
            return True
    # Helper: check news sentiment summary for pause
    # Helper: check if a given UTC time falls within a provided window string in ET
    def _in_window_str(now_utc: float, window_str: str) -> bool:
        if not window_str:
            return False
        try:
            from zoneinfo import ZoneInfo as _ZI
            import datetime as __dt
            dt_et = __dt.datetime.fromtimestamp(now_utc, tz=_ZI('UTC')).astimezone(_ZI('America/New_York'))
            windows = [w.strip() for w in str(window_str).split(';') if w.strip()]
            for w in windows:
                p = w.split('-')
                if len(p) != 2:
                    continue
                h0, h1 = p[0].strip(), p[1].strip()
                t0 = __dt.datetime(dt_et.year, dt_et.month, dt_et.day, int(h0[0:2]), int(h0[3:5]), tzinfo=_ZI('America/New_York'))
                t1 = __dt.datetime(dt_et.year, dt_et.month, dt_et.day, int(h1[0:2]), int(h1[3:5]), tzinfo=_ZI('America/New_York'))
                if t0 <= dt_et <= t1:
                    return True
            return False
        except Exception:
            return False
    def _should_pause_for_news(now_utc: float) -> bool:
        nonlocal last_pause_until
        if float(news_pause_threshold) <= 0:
            return False
        # If already paused, respect timer
        if now_utc < last_pause_until:
            return True
        # Read latest daily summary file
        try:
            import datetime as __dt
            from pathlib import Path as _P
            d = __dt.datetime.fromtimestamp(now_utc).strftime('%Y%m%d')
            p = _P('logs') / f'news_agent_summary_{d}.json'
            if not p.exists():
                return False
            import json as __json
            obj = __json.loads(p.read_text(encoding='utf-8'))
            score = float(obj.get('sentiment_score', 0.0) or 0.0)
            if abs(score) >= float(news_pause_threshold):
                last_pause_until = now_utc + float(news_cooldown_sec)
                return True
        except Exception:
            return False
        return False

    # Helper: refresh news directional bias context from today's agent summary
    def _refresh_news_ctx(now_utc: float):
        nonlocal news_ctx, news_next_refresh
        if now_utc < news_next_refresh:
            return
        news_next_refresh = now_utc + 60.0
        try:
            import datetime as __dt
            from pathlib import Path as _P
            d = __dt.datetime.fromtimestamp(now_utc).strftime('%Y%m%d')
            p = _P('logs') / f'news_agent_summary_{d}.json'
            if not p.exists():
                news_ctx = {"bias": "neutral", "conf": 0.0, "fresh": False, "ts": 0.0}
                return
            import json as __json
            obj = __json.loads(p.read_text(encoding='utf-8'))
            db = obj.get('directional_bias') or {}
            bias = (db.get('bias') or 'neutral').lower() if isinstance(db, dict) else 'neutral'
            try:
                conf = float(db.get('confidence') or 0.0) if isinstance(db, dict) else 0.0
            except Exception:
                conf = 0.0
            gen_raw = obj.get('generated_at')
            ts = 0.0
            try:
                if isinstance(gen_raw, str) and gen_raw:
                    # robust ISO parse
                    s = gen_raw.replace('Z', '+00:00')
                    ts = __dt.datetime.fromisoformat(s).timestamp()
            except Exception:
                ts = now_utc
            fresh = (now_utc - ts) <= float(news_bias_staleness_sec)
            news_ctx = {"bias": bias, "conf": conf, "fresh": bool(fresh), "ts": float(ts)}
        except Exception:
            news_ctx = {"bias": "neutral", "conf": 0.0, "fresh": False, "ts": 0.0}

    def _refresh_index_ctx(now_utc: float):
        nonlocal idx_ctx, idx_next_refresh
        if now_utc < idx_next_refresh:
            return
        try:
            from .datafeeds.polygon_client import PolygonClient, PolygonConfig
            poly_client_local = None
            try:
                poly_client_local = PolygonClient(PolygonConfig.from_env())
            except Exception:
                poly_client_local = None
            if not poly_client_local:
                idx_next_refresh = now_utc + 60.0
                return
            df = poly_client_local.fetch_index_intraday_aggs(ticker="I:SPX", timespan="minute", mult=1, lookback_days=1)
            if df is None or df.empty:
                idx_next_refresh = now_utc + 60.0
                return
            # Compute today RTH VWAP and 60s trend
            import pytz as _pytz
            from datetime import time as _tcls
            et = _pytz.timezone('America/New_York')
            df["et"] = df["dt"].dt.tz_convert(et)
            d0 = df["et"].dt.date.iloc[-1]
            start_t = _tcls(9,30); end_t = _tcls(16,0)
            dft = df[(df["et"].dt.date == d0) & (df["et"].dt.time >= start_t) & (df["et"].dt.time <= end_t)]
            if dft.empty:
                dft = df
            # VWAP
            try:
                vol = dft["volume"].astype(float)
                px = dft["close"].astype(float)
                vw = float((px * vol).sum() / vol.clip(lower=1e-9).sum()) if vol.sum() > 0 else float(px.iloc[-1])
            except Exception:
                vw = float(dft["close"].astype(float).iloc[-1])
            last_px = float(dft["close"].astype(float).iloc[-1])
            # Trend over window
            try:
                import pandas as _pd
                cutoff = dft["dt"].iloc[-1] - _pd.Timedelta(seconds=float(index_trend_window_sec))
                prior = dft[dft["dt"] <= cutoff]
                if not prior.empty:
                    p0 = float(prior["close"].astype(float).iloc[-1])
                    ret = (last_px - p0) / p0 if p0 > 0 else 0.0
                else:
                    # fallback to last 2 bars
                    p0 = float(dft["close"].astype(float).iloc[-2]) if len(dft) >= 2 else last_px
                    ret = (last_px - p0) / p0 if p0 > 0 else 0.0
            except Exception:
                ret = 0.0
            # Indicators snapshot (EMA/MACD/RSI)
            ema_v = 0.0; macd_v = 0.0; macd_sig_v = 0.0; rsi_v = 50.0
            try:
                ind = poly_client_local.compute_indicators(dft)
                if isinstance(ind, dict):
                    ema_v = float(ind.get("ema", ema_v)) if ind.get("ema") is not None else ema_v
                    macd_v = float(ind.get("macd", macd_v)) if ind.get("macd") is not None else macd_v
                    macd_sig_v = float(ind.get("macd_signal", macd_sig_v)) if ind.get("macd_signal") is not None else macd_sig_v
                    rsi_v = float(ind.get("rsi", rsi_v)) if ind.get("rsi") is not None else rsi_v
            except Exception:
                pass
            idx_ctx = {"last": last_px, "vwap": vw, "ret_bp": ret * 10000.0, "ema": ema_v, "macd": macd_v, "macd_signal": macd_sig_v, "rsi": rsi_v}
        finally:
            idx_next_refresh = now_utc + 60.0

    # Cached VIX series (from Polygon) for live VIX trend gate
    vix_df_live = None
    def _ensure_vix_loaded(ts_hint: float | None) -> None:
        nonlocal vix_df_live, poly_client_global
        if vix_df_live is not None:
            return
        try:
            from .datafeeds.polygon_client import PolygonClient, PolygonConfig
            poly_client_local = poly_client_global or PolygonClient(PolygonConfig.from_env())
            df_vx = poly_client_local.fetch_index_intraday_aggs(ticker="I:VIX", timespan="minute", mult=1, lookback_days=2)
            if (df_vx is not None) and (not df_vx.empty):
                import pytz as _pytz
                from datetime import time as _tcls
                et = _pytz.timezone('America/New_York')
                df_vx["et"] = df_vx["dt"].dt.tz_convert(et)
                # pick session date from hint or last
                if ts_hint is not None:
                    from zoneinfo import ZoneInfo as _ZI
                    import datetime as __dt
                    _d = __dt.datetime.fromtimestamp(ts_hint, tz=_ZI('UTC')).astimezone(_ZI('America/New_York')).date()
                else:
                    _d = df_vx["et"].dt.date.iloc[-1]
                start_t = _tcls(9,30); end_t = _tcls(16,0)
                dfv = df_vx[(df_vx["et"].dt.date == _d) & (df_vx["et"].dt.time >= start_t) & (df_vx["et"].dt.time <= end_t)]
                vix_df_live = dfv.set_index("dt") if not dfv.empty else df_vx.set_index("dt")
        except Exception:
            vix_df_live = None

    def _vix_ok_live(ts: float, cp: str | None) -> bool:
        if not use_vix_trend:
            return True
        _ensure_vix_loaded(ts)
        if vix_df_live is None:
            return True
        try:
            import pandas as _pd
            t = _pd.to_datetime(ts, unit="s", utc=True)
            idx = vix_df_live.index
            pos = idx.searchsorted(t, side="right") - 1
            if pos < 0:
                return True
            now_px = float(vix_df_live["close"].iloc[pos])
            cutoff = t - _pd.Timedelta(seconds=float(vix_trend_window_sec))
            pos0 = idx.searchsorted(cutoff, side="right") - 1
            if pos0 < 0:
                pos0 = 0
            prev_px = float(vix_df_live["close"].iloc[pos0])
            if prev_px <= 0:
                return True
            ret = (now_px - prev_px) / prev_px
            min_req = float(vix_trend_min_bp) / 10000.0
            # If configured to apply VIX only to puts, calls bypass this check
            if bool(vix_puts_only) and isinstance(cp, str) and cp.upper() == 'C':
                return True
            if isinstance(cp, str) and cp.upper() == 'C':
                return (ret <= -min_req)
            if isinstance(cp, str) and cp.upper() == 'P':
                return (ret >= min_req)
            return True
        except Exception:
            return True
    # Optional recorder
    _rec_path = _Path(record_ticks) if record_ticks else None
    _rec_f = None
    if _rec_path:
        try:
            _rec_path.parent.mkdir(parents=True, exist_ok=True)
            # Write header if new file
            new_file = not _rec_path.exists()
            _rec_f = _rec_path.open("a", encoding="utf-8", newline="")
            if new_file:
                # Include optional volume column placeholder for future use; replay parser handles missing volume gracefully
                _rec_f.write("ts,symbol,short,bid,ask,mid,strike,cp,atm\n")
                _rec_f.flush()
        except Exception:
            _rec_f = None

    def _notify(title: str, msg: str):
        if not notify:
            return
        try:
            ps1 = _Path("scripts") / "notify_trade.ps1"
            if ps1.exists():
                subprocess.Popen([
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1),
                    "-Title", title, "-Message", msg
                ])
        except Exception:
            pass

    def _append_alert(payload: dict):
        try:
            with alerts_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # Greeks computation helper (on-the-fly BS greeks from mid/spot/expiry)
    def _time_to_expiry_days(now_utc: float) -> float:
        try:
            if _exp_dt_date is None:
                return 0.0
            from zoneinfo import ZoneInfo as _ZI
            # Assume expiration at 16:00 ET (RTH close)
            et = _ZI('America/New_York')
            exp_local = _gre_dt.datetime(_exp_dt_date.year, _exp_dt_date.month, _exp_dt_date.day, 16, 0, 0, tzinfo=et)
            exp_utc = exp_local.astimezone(_ZI('UTC'))
            rem = max(0.0, (exp_utc.timestamp() - now_utc))
            return rem / 86400.0
        except Exception:
            return 0.0

    def _greeks_ok(now_utc: float, mid: float, strike: float, cp: str | None) -> tuple[bool, dict]:
        # Determine spot from latest idx_ctx if available; fallback to initial idx_close
        try:
            spot = float(idx_ctx.get("last") or 0.0)
        except Exception:
            spot = 0.0
        if spot <= 0:
            try:
                spot = float(idx_close)
            except Exception:
                spot = 0.0
        if spot <= 0 or mid <= 0 or not isinstance(strike, (int, float)) or strike <= 0:
            return False, {}
        t_days = _time_to_expiry_days(now_utc)
        if t_days <= 0:
            return False, {}
        try:
            call_flag = (str(cp).upper() == 'C') if cp is not None else True
            om = compute_option_metrics(spot=spot, strike=float(strike), t_days=t_days, mid_price=float(mid), call=call_flag, rate=0.0)
        except Exception:
            return False, {}
        # Establish greeks with fallbacks if IV solve fails
        iv_val = om.iv
        delta_val = om.delta
        gamma_val = om.gamma
        vega_val = om.vega
        if iv_val is None or delta_val is None:
            try:
                from .utils.bs_greeks import implied_vol_newton as _iv_newton, delta as _bs_delta, gamma as _bs_gamma, vega as _bs_vega
                t_years = max(0.0, float(t_days) / 365.0)
                right = 'C' if ((str(cp).upper() == 'C') if cp is not None else True) else 'P'
                iv2 = _iv_newton(target_price=float(mid), S=float(spot), K=float(strike), r=0.0, T=t_years, right=right, q=0.0, initial=0.6)
                if not isinstance(iv2, (int, float)) or iv2 <= 0:
                    iv2 = 0.6
                iv_val = iv2
                delta_val = _bs_delta(float(spot), float(strike), 0.0, float(iv2), t_years, right, 0.0)
                gamma_val = _bs_gamma(float(spot), float(strike), 0.0, float(iv2), t_years, 0.0)
                vega_val = _bs_vega(float(spot), float(strike), 0.0, float(iv2), t_years, 0.0)
            except Exception:
                return False, {}
        # Apply thresholds
        try:
            a_delta = abs(float(delta_val)) if delta_val is not None else 0.0
        except Exception:
            a_delta = 0.0
        if a_delta < float(delta_min) or a_delta > float(delta_max):
            return False, {"iv": iv_val, "delta": delta_val, "gamma": gamma_val, "vega": vega_val}
        if float(iv_min) > 0.0 and (iv_val is None or float(iv_val) < float(iv_min)):
            return False, {"iv": iv_val, "delta": delta_val, "gamma": gamma_val, "vega": vega_val}
        if float(iv_max) < 10.0 and (iv_val is None or float(iv_val) > float(iv_max)):
            return False, {"iv": iv_val, "delta": delta_val, "gamma": gamma_val, "vega": vega_val}
        if float(gamma_max) > 0.0 and (gamma_val is None or float(gamma_val) > float(gamma_max)):
            return False, {"iv": iv_val, "delta": delta_val, "gamma": gamma_val, "vega": vega_val}
        if float(vega_max) > 0.0 and (vega_val is None or float(vega_val) > float(vega_max)):
            return False, {"iv": iv_val, "delta": delta_val, "gamma": gamma_val, "vega": vega_val}
        return True, {"iv": iv_val, "delta": delta_val, "gamma": gamma_val, "vega": vega_val}

    def _should_alert(sym: str, ts: float, mid: float, strike: float, cp: str | None) -> tuple[bool, str]:
        # Momentum over configurable window: price > min_price, strong percent change, and sustained upticks
        series = state.setdefault(sym, [])
        series.append((ts, mid))
        # keep last N seconds
        cutoff = ts - float(window_sec)
        series[:] = [(t, m) for (t, m) in series if t >= cutoff]
        # Require a minimal number of points in the window. In fully permissive modes
        # (no uptick requirement and zero percent thresholds), allow 2 points; otherwise require 4.
        try:
            _fully_permissive = (float(uptick_ratio) <= 0.0) and (float(pct_thresh) <= 0.0) and (float(pct_thresh_cheap) <= 0.0)
        except Exception:
            _fully_permissive = False
        _min_pts = 2 if _fully_permissive else 4
        if mid < min_price:
            return False, "min_price"
        if len(series) < _min_pts:
            return False, "too_short"
        # 1) Recent upticks
        ups = sum(1 for i in range(1, len(series)) if series[i][1] > series[i-1][1])
        # Dynamic uptick requirement based on ratio; no hard-coded floor of 3 to allow permissive modes
        try:
            steps = max(1, (len(series) - 1))
            # Ceil to make the requirement intuitive: e.g., 0.5 with 3 steps => 2 ups
            import math as _math
            required_ups = int(_math.ceil(float(uptick_ratio) * steps))
            # Guardrails: cap at steps and bottom out at 0 to honor fully permissive settings
            if required_ups < 0:
                required_ups = 0
            if required_ups > steps:
                required_ups = steps
        except Exception:
            required_ups = 0
        if ups < required_ups:
            return False, "upticks"
        # 2) Percent change vs oldest in window
        m0 = series[0][1]
        if m0 <= 0:
            return False, "pct"
        pct = (mid - m0) / m0
        # Require at least configured rise for cheap options vs otherwise
        thresh = float(pct_thresh_cheap) if m0 < 0.5 else float(pct_thresh)
        if pct < thresh:
            return False, "pct"
        # Debounce per symbol (no more than one alert every 5 minutes)
        la = last_alert.get(sym, 0.0)
        if ts - la < float(debounce_sec):
            return False, "debounce"
        last_alert[sym] = ts
        return True, ""

    # Pre-map fields for strikes/cp
    meta: dict[str, tuple[float, str]] = {c.symbol: (float(c.strike), c.call_put) for c in parsed if c.symbol in curated}
    batch_idx = 0
    end_time = _time.time() + (minutes * 60)
    scanned = 0
    while _time.time() < end_time:
        # current batch slice
        if not curated:
            break
        start = batch_idx * batch_size
        stop = start + batch_size
        batch = curated[start:stop]
        if not batch:
            batch_idx = 0
            continue
        batch_idx += 1
        now = _time.time()
        # Use a single, consistent wall-clock timestamp for all per-symbol checks below
        # This ensures warmup window and mid-cache TTL use the same reference and avoids
        # referencing now_utc before assignment in earlier branches.
        now_utc = now
        for s in batch:
            scanned += 1
            # IQFeed short symbol
            try:
                # Our OCC symbol is like SPXWYYMMDDC00000000; IQFeed short uses "SPXWYYDDM<strike>"
                # Here we derive short from parsed meta: yymmdd and strike; reuse formatter
                # We'll reconstruct via parse_occ and _format_spxw_short_symbol
                c = _parse_occ(s)
                if not c:
                    continue
                yy = c.expiry.year % 100
                dd = c.expiry.day
                m_letter = _opra_month_letter(c.expiry.month, c.call_put == 'C')
                short = f"SPXW{yy:02d}{dd:02d}{m_letter}{int(c.strike)}"
                # Choose quote source
                res = None
                if use_polygon_quotes:
                    try:
                        if not poly_client_global and settings.has_polygon:
                            poly_client_global = PolygonClient(PolygonConfig.from_env())
                        ticker = s if s.startswith("O:") else f"O:{s}"
                        snap = poly_client_global.fetch_option_snapshot(ticker) if poly_client_global else None
                        if isinstance(snap, dict):
                            res = {"_source": "polygon", "_snap": snap}
                    except Exception:
                        res = None
                if res is None:
                    res = iq.lookup_last(short)
                # Safe numeric extraction
                last_px_from_poly = None
                day_vol_ok = True
                if isinstance(res, dict) and res.get("_source") == "polygon":
                    try:
                        r = res.get("_snap", {})
                        rr = r.get("results", {}) if isinstance(r, dict) else {}
                        book = rr.get("last_quote", {}) if isinstance(rr, dict) else {}
                        last_trade = rr.get("last_trade", {}) if isinstance(rr, dict) else {}
                        try:
                            last_px_from_poly = float(last_trade.get("price") or 0)
                        except Exception:
                            last_px_from_poly = None
                        _bv = book.get("bid")
                        _av = book.get("ask")
                        # Day volume gate (Polygon-only)
                        try:
                            if float(min_day_volume) > 0:
                                day = rr.get("day", {}) if isinstance(rr, dict) else {}
                                dv = float(day.get("volume") or 0.0)
                                if dv < float(min_day_volume):
                                    day_vol_ok = False
                        except Exception:
                            pass
                    except Exception:
                        _bv = None; _av = None
                else:
                    _bv = res.get("bid") if isinstance(res, dict) else None
                    _av = res.get("ask") if isinstance(res, dict) else None
                if not day_vol_ok:
                    # Warmup-aware behavior: within warmup window after 09:30 ET, allow low volume
                    # but we will still enforce NBBO quality later in liquidity gates.
                    try:
                        within_warmup = False
                        if session_open_utc > 0:
                            within_warmup = (now_utc - float(session_open_utc)) < (float(warmup_min) * 60.0)
                        if within_warmup:
                            stats_live["day_volume_grace_skipped"] = int(stats_live.get("day_volume_grace_skipped", 0)) + 1
                        else:
                            stats_live["day_volume_blocked"] = int(stats_live.get("day_volume_blocked", 0)) + 1
                            continue
                    except Exception:
                        try:
                            stats_live["day_volume_blocked"] = int(stats_live.get("day_volume_blocked", 0)) + 1
                        except Exception:
                            pass
                        continue
                try:
                    bid = float(_bv) if _bv not in (None, "", "N/A") else 0.0
                except Exception:
                    bid = 0.0
                try:
                    ask = float(_av) if _av not in (None, "", "N/A") else 0.0
                except Exception:
                    ask = 0.0
                # If using Polygon and no NBBO but we have a last trade price, synthesize a mid from last
                if (isinstance(res, dict) and res.get("_source") == "polygon"):
                    if (not bid or not ask) and (last_px_from_poly is not None) and (last_px_from_poly > 0):
                        bid = float(last_px_from_poly)
                        ask = float(last_px_from_poly)
                # If still no usable NBBO/mid after Polygon, fall back to IQFeed one more time
                if (bid <= 0 or ask <= 0):
                    try:
                        iq_res = iq.lookup_last(short)
                    except Exception:
                        iq_res = None
                    if isinstance(iq_res, dict):
                        try:
                            b2 = iq_res.get("bid"); a2 = iq_res.get("ask")
                            lb = float(b2) if b2 not in (None, "", "N/A") else 0.0
                            la = float(a2) if a2 not in (None, "", "N/A") else 0.0
                        except Exception:
                            lb = 0.0; la = 0.0
                        if lb <= 0 or la <= 0:
                            try:
                                lt = iq_res.get("last_trade") or iq_res.get("last_price") or iq_res.get("last")
                                lt = float(lt) if lt not in (None, "", "N/A") else 0.0
                            except Exception:
                                lt = 0.0
                            if lt > 0:
                                lb = lt; la = lt
                        if lb > 0 and la > 0:
                            bid = lb; ask = la
                # If IQFeed response has a last price but no NBBO, synthesize mid from last
                if (isinstance(res, dict) and res.get("_source") != "polygon") and (not bid or not ask):
                    try:
                        last_iq = res.get("last_trade") or res.get("last_price") or res.get("last")
                        last_iq = float(last_iq) if last_iq not in (None, "", "N/A") else 0.0
                    except Exception:
                        last_iq = 0.0
                    if last_iq and last_iq > 0:
                        bid = float(last_iq)
                        ask = float(last_iq)
                mid = (bid + ask) / 2.0 if bid and ask else (bid or ask or 0.0)
                # Time-window bookkeeping and gate (now_utc already set at top of loop)
                # Brief-gap fallback: if mid is zero, try a very recent cached mid (<=1.5s old)
                try:
                    if mid <= 0:
                        ent = last_mid_cache.get(s)
                        if ent is not None:
                            _m, _ts = ent
                            if (now_utc - float(_ts)) <= 1.5 and _m > 0:
                                mid = float(_m)
                                # Synthesize tight NBBO from cached mid to allow spread evaluation later
                                if not bid or bid <= 0:
                                    bid = mid
                                if not ask or ask <= 0:
                                    ask = mid
                                try:
                                    stats_live["mid_cached_used"] = int(stats_live.get("mid_cached_used", 0)) + 1
                                except Exception:
                                    pass
                except Exception:
                    pass
                _ok_hours = _within_hours(now_utc)
                # Determine k/cp metadata now so we can record ticks early
                k, cp = meta.get(s, (None, None))
                if k is None:
                    continue
                # Early tick recorder: write before any liquidity/news/greeks gates
                # When record_all_hours=True, write even if mid==0 and outside include-hours for diagnostics
                if _rec_f is not None and (_ok_hours or bool(record_all_hours)):
                    try:
                        # Prefer dynamic SPX last if available to power replay trend/EM gates; fallback to idx_close
                        _refresh_index_ctx(now_utc)
                        rec_atm = float(idx_ctx.get("last") or idx_close)
                    except Exception:
                        rec_atm = float(idx_close)
                    try:
                        _rec_f.write(f"{now:.3f},{s},{short},{bid:.6f},{ask:.6f},{mid:.6f},{k:.0f},{cp},{rec_atm}\n")
                    except Exception:
                        pass
                # If no usable price and we're not just recording outside hours, skip this symbol
                if mid <= 0 and not bool(record_all_hours):
                    try:
                        stats_live["mid_zero_blocked"] = int(stats_live.get("mid_zero_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                # Update mid cache with a fresh usable mid
                try:
                    if mid > 0:
                        last_mid_cache[s] = (float(mid), float(now_utc))
                except Exception:
                    pass
                # If we're outside include-hours, skip further gating/alerts unless recording-only
                if not _ok_hours:
                    try:
                        stats_live["outside_hours_blocked"] = int(stats_live.get("outside_hours_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                # News sentiment pause gate (skip if greeks-only)
                if not greeks_only and _should_pause_for_news(now_utc):
                    try:
                        stats_live["news_pause_blocked"] = int(stats_live.get("news_pause_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                # Conditional midday window: only act if absolute index trend is on
                if midday_conditional_hours:
                    _refresh_index_ctx(now_utc)
                    try:
                        if _in_window_str(now_utc, midday_conditional_hours):
                            rbp_abs = abs(float(idx_ctx.get("ret_bp") or 0.0))
                            if rbp_abs < float(index_trend_min_bp):
                                try:
                                    stats_live["midday_conditional_blocked"] = int(stats_live.get("midday_conditional_blocked", 0)) + 1
                                except Exception:
                                    pass
                                continue
                    except Exception:
                        pass
                # Liquidity gate: spread fraction and absolute, NBBO sizes, quote age
                # If only one side present but we have mid, synthesize tight NBBO to allow evaluation (common in snapshots)
                if (not bid or not ask) and mid > 0 and isinstance(res, dict) and res.get("_source") == "polygon":
                    bid = mid
                    ask = mid
                spr = (ask - bid) if (ask and bid and ask >= bid) else None
                if spr is None:
                    continue
                frac = (spr / mid) if mid > 0 else 1.0
                if float(max_spread_frac) > 0 and frac > float(max_spread_frac):
                    try:
                        stats_live["spread_frac_blocked"] = int(stats_live.get("spread_frac_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                # Absolute spread caps by price bucket
                abs_cap = float(max_spread_abs) if float(max_spread_abs) > 0 else (0.05 if mid < 2.0 else (0.10 if mid <= 10.0 else 0.20))
                if float(abs_cap) > 0 and spr > float(abs_cap):
                    try:
                        stats_live["spread_abs_blocked"] = int(stats_live.get("spread_abs_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                # NBBO sizes (if provided)
                try:
                    if isinstance(res, dict) and res.get("_source") == "polygon":
                        r = res.get("_snap", {})
                        rr = r.get("results", {}) if isinstance(r, dict) else {}
                        book = rr.get("last_quote", {}) if isinstance(rr, dict) else {}
                        bs = float(book.get('bid_size') or 0)
                        asz = float(book.get('ask_size') or 0)
                    elif isinstance(res, dict):
                        bs = float(res.get('bid_size') or 0)
                        asz = float(res.get('ask_size') or 0)
                    else:
                        bs = 0.0; asz = 0.0
                except Exception:
                    bs = 0.0; asz = 0.0
                if float(min_nbbo_size) > 0 and (bs < float(min_nbbo_size) or asz < float(min_nbbo_size)):
                    try:
                        stats_live["nbbo_size_blocked"] = int(stats_live.get("nbbo_size_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                # Optional: require NBBO size imbalance favoring trade side
                try:
                    if bool(use_nbbo_imbalance_gate) and isinstance(cp, str) and cp in ("C", "P"):
                        if bs > 0 and asz > 0:
                            if cp == "C":
                                ratio = (asz / bs) if bs > 0 else 0.0
                                if ratio < float(min_ask_over_bid_ratio_calls):
                                    try:
                                        stats_live["nbbo_imbalance_blocked"] = int(stats_live.get("nbbo_imbalance_blocked", 0)) + 1
                                    except Exception:
                                        pass
                                    continue
                            elif cp == "P":
                                ratio = (bs / asz) if asz > 0 else 0.0
                                if ratio < float(min_bid_over_ask_ratio_puts):
                                    try:
                                        stats_live["nbbo_imbalance_blocked"] = int(stats_live.get("nbbo_imbalance_blocked", 0)) + 1
                                    except Exception:
                                        pass
                                    continue
                except Exception:
                    pass
                # Optional: per-symbol quote-rate minimum using IQFeed quote timestamps (10s window)
                try:
                    if float(min_quote_rate_hz) > 0 and isinstance(res, dict) and res.get("_source") != "polygon":
                        # Parse latest quote times from IQFeed and track unique updates
                        from .datafeeds.iqfeed_client import _parse_trade_time as __parse_time  # reuse parser
                        bt = res.get('bid_time')
                        at = res.get('ask_time')
                        parsed_times = []
                        if isinstance(bt, str):
                            t = __parse_time(bt)
                            if t:
                                parsed_times.append(float(t))
                        if isinstance(at, str):
                            t2 = __parse_time(at)
                            if t2:
                                parsed_times.append(float(t2))
                        if parsed_times:
                            latest_qt = max(parsed_times)
                            dq = quote_ts_map.get(s)
                            if dq is None:
                                from collections import deque as _dq
                                dq = _dq(maxlen=1000)
                                quote_ts_map[s] = dq
                            # Keep only last ~10 seconds of quote updates
                            try:
                                cutoff = latest_qt - 10.0
                                while dq and dq[0] < cutoff:
                                    dq.popleft()
                            except Exception:
                                pass
                            # Append only if this is a new quote time
                            if (not dq) or (dq[-1] < latest_qt):
                                dq.append(latest_qt)
                            # Compute quotes/sec over the 10s window
                            rate_hz = (len(dq) / 10.0) if dq else 0.0
                            if rate_hz < float(min_quote_rate_hz):
                                try:
                                    stats_live["quote_rate_blocked"] = int(stats_live.get("quote_rate_blocked", 0)) + 1
                                except Exception:
                                    pass
                                continue
                except Exception:
                    pass
                # Quote age using bid/ask time if available
                age_ok = True
                try:
                    if isinstance(res, dict) and res.get("_source") != "polygon":
                        from .datafeeds.iqfeed_client import _parse_trade_time as __parse_time  # reuse parser
                        bt = res.get('bid_time')
                        at = res.get('ask_time')
                        ages = []
                        if isinstance(bt, str):
                            t = __parse_time(bt)
                            if t:
                                ages.append(now_utc - t)
                        if isinstance(at, str):
                            t2 = __parse_time(at)
                            if t2:
                                ages.append(now_utc - t2)
                        if ages:
                            max_age_ms = max(ages) * 1000.0
                            if max_age_ms > float(quote_age_ms_max):
                                age_ok = False
                except Exception:
                    age_ok = True
                if not age_ok:
                    try:
                        stats_live["quote_age_blocked"] = int(stats_live.get("quote_age_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                # k,cp already loaded above for early recording
                # Greeks-only gating path
                if greeks_only:
                    # Ensure we have a fresh spot from Polygon for greeks computation
                    _refresh_index_ctx(now_utc)
                    ok, g = _greeks_ok(now_utc, mid, float(k), cp)
                    if not ok:
                        continue
                    # Apply VIX trend gate in greeks-only mode
                    if not _vix_ok_live(now_utc, cp):
                        continue
                    # Apply indicator bias in greeks-only mode (parity with replay)
                    if use_indicator_bias:
                        ema_v = float(idx_ctx.get("ema") or 0.0)
                        macd_v = float(idx_ctx.get("macd") or 0.0)
                        macd_sig_v = float(idx_ctx.get("macd_signal") or 0.0)
                        rsi_v = float(idx_ctx.get("rsi") or 50.0)
                        last_px = float(idx_ctx.get("last") or 0.0)
                        if ema_v > 0 and last_px > 0:
                            if isinstance(cp, str) and cp.upper() == 'C':
                                if not (macd_v > macd_sig_v and rsi_v >= float(rsi_bull_min) and last_px >= ema_v):
                                    continue
                            if isinstance(cp, str) and cp.upper() == 'P':
                                if not (macd_v < macd_sig_v and rsi_v <= float(rsi_bear_max) and last_px <= ema_v):
                                    continue
                    # Debounce
                    la = last_alert.get(s, 0.0)
                    if now_utc - la < float(debounce_sec):
                        continue
                    last_alert[s] = now_utc
                    # Trend snapshot for sizing recommendation
                    _refresh_index_ctx(now_utc)
                    trend_bp = float(idx_ctx.get("ret_bp") or 0.0)
                    size_reco = 1.0
                    try:
                        if bool(use_alert_size_reco):
                            if isinstance(cp, str) and cp.upper() == 'P':
                                if trend_bp > 0:
                                    size_reco = float(put_pos_trend_size)
                                else:
                                    size_reco = float(put_neg_trend_size)
                            if isinstance(cp, str) and cp.upper() == 'C' and trend_bp < 0:
                                size_reco = float(call_neg_trend_size)
                    except Exception:
                        size_reco = 1.0
                    payload = {
                        "ts": _dt.now(_tz.utc).isoformat(),
                        "symbol": s,
                        "short": short,
                        "mid": mid,
                        "bid": bid,
                        "ask": ask,
                        "strike": k,
                        "cp": cp,
                        "atm": idx_close,
                        "scanned": scanned,
                        "greeks_only": True,
                        "delta": g.get("delta"),
                        "gamma": g.get("gamma"),
                        "vega": g.get("vega"),
                        "iv": g.get("iv"),
                        "trend_bp": trend_bp,
                        "size_reco": size_reco,
                    }
                    _append_alert(payload)
                    try:
                        stats_live["alerts"] = int(stats_live.get("alerts", 0)) + 1
                        if isinstance(cp, str) and cp.upper() == 'C':
                            stats_live["alerts_calls"] = int(stats_live.get("alerts_calls", 0)) + 1
                            _mark_probe_converted(s, now_utc, window_sec=max(180.0, float(window_sec)))
                        elif isinstance(cp, str) and cp.upper() == 'P':
                            stats_live["alerts_puts"] = int(stats_live.get("alerts_puts", 0)) + 1
                    except Exception:
                        pass
                    _notify("SPXW Greeks", f"{s} Δ={float(g.get('delta') or 0):+.2f} IV={(g.get('iv') or 0):.2f}")
                    continue
                # Index gating (trend + VWAP + indicator bias)
                # Disabled when using agent-only decisions
                if (not use_agent_decider) and (require_index_trend or require_vwap_alignment or use_indicator_bias):
                    _refresh_index_ctx(now_utc)
                    # Adaptive: reason-targeted ladder for CALLS
                    call_trend_min_bp_override: float | None = None
                    call_disable_indicator = False
                    call_disable_vix = False
                    call_disable_vwap = False
                    try:
                        if adapt_calls and int(stats_live.get("alerts_calls", 0)) == 0:
                            elapsed_min = int((now_utc - session_start_utc) / 60.0)
                            stage = 0
                            for m in adapt_minutes:
                                if elapsed_min >= int(m):
                                    stage += 1
                            stats_live["adapt_stage"] = stage
                            # dominant blocking reason among probes so far
                            dom_reason = None
                            if probe_reason_counts:
                                dom_reason = max(probe_reason_counts.items(), key=lambda kv: kv[1])[0]
                            if dom_reason:
                                # trend ladder
                                if dom_reason.startswith("trend_") and stage > 0 and adapt_bp_seq:
                                    idx = min(stage-1, len(adapt_bp_seq)-1)
                                    call_trend_min_bp_override = float(adapt_bp_seq[idx])
                                # indicator disablement
                                if dom_reason == "indicator_block_call" and stage >= int(adapt_disable_indicator_stage):
                                    call_disable_indicator = True
                                # vix disablement
                                if dom_reason == "vix_block_call" and stage >= int(adapt_disable_vix_stage):
                                    call_disable_vix = True
                                # vwap disablement tied to later stages
                                if dom_reason == "vwap_block_call" and stage >= max(int(adapt_disable_vix_stage), int(adapt_disable_indicator_stage)):
                                    call_disable_vwap = True
                    except Exception:
                        pass
                    if require_index_trend:
                        rbp = float(idx_ctx.get("ret_bp") or 0.0)
                        # optional smoothing override
                        if index_trend_smooth_sec > 0:
                            try:
                                now_ts = now_utc
                                last_px_val = float(idx_ctx.get("last") or 0.0)
                                if last_px_val > 0:
                                    idx_price_buffer.append((now_ts, last_px_val))
                                wstart = now_ts - float(index_trend_window_sec)
                                recent = [p for (t,p) in idx_price_buffer if t >= wstart]
                                if len(recent) >= 2:
                                    sm_last = sum(recent[-min(len(recent), int(max(2, (index_trend_smooth_sec / max(1,index_trend_window_sec))*len(recent)))):]) / max(1, min(len(recent), int(max(2, (index_trend_smooth_sec / max(1,index_trend_window_sec))*len(recent)))))
                                    # approximate start value
                                    sm_start_candidates = [p for (t,p) in idx_price_buffer if wstart <= t <= wstart + 1.0]
                                    if not sm_start_candidates:
                                        sm_start_candidates = recent[:1]
                                    sm_start = sum(sm_start_candidates)/len(sm_start_candidates)
                                    if sm_start > 0:
                                        rbp = (sm_last - sm_start)/sm_start * 10000.0
                                        idx_ctx["ret_bp_smoothed"] = rbp
                            except Exception:
                                pass
                        try:
                            stats_live["trend_evaluated"] = int(stats_live.get("trend_evaluated", 0)) + 1
                        except Exception:
                            pass
                        # Evaluate with adaptive bp override for calls if present
                        _min_bp = float(index_trend_min_bp)
                        if isinstance(cp, str) and cp.upper() == 'C' and (call_trend_min_bp_override is not None):
                            _min_bp = float(call_trend_min_bp_override)
                        if isinstance(cp, str) and cp.upper() == 'C' and rbp < _min_bp:
                            try:
                                stats_live["trend_blocked"] = int(stats_live.get("trend_blocked", 0)) + 1
                                stats_live["trend_blocked_low_bp"] = int(stats_live.get("trend_blocked_low_bp", 0)) + 1
                            except Exception:
                                pass
                            _tally("trend_low_bp_call")
                            # Probe: if momentum criteria (sans trend) already satisfied, log hypothetical call
                            if enable_probes:
                                try:
                                    series = state.get(s, [])
                                    if series:
                                        # Quick momentum evaluation mirroring _should_alert sans trend gate
                                        ts_now = now_utc
                                        # Filter window
                                        cutoff = ts_now - float(window_sec)
                                        window_series = [(t,m) for (t,m) in series if t >= cutoff]
                                        if window_series:
                                            m0 = window_series[0][1]
                                            ups = sum(1 for i in range(1,len(window_series)) if window_series[i][1] > window_series[i-1][1])
                                            steps = max(1,(len(window_series)-1))
                                            import math as _math
                                            required_ups = int(_math.ceil(float(uptick_ratio)*steps)) if float(uptick_ratio)>0 else 0
                                            if required_ups>steps: required_ups=steps
                                            pct = (window_series[-1][1]-m0)/m0 if m0>0 else -1
                                            thresh = float(pct_thresh_cheap) if m0 < 0.5 else float(pct_thresh)
                                            if m0 >= float(min_price) and len(window_series) >= (2 if (float(uptick_ratio)<=0 and float(pct_thresh)<=0 and float(pct_thresh_cheap)<=0) else 4) and ups >= required_ups and pct >= thresh:
                                                _write_probe({
                                                    "ts": __import__("datetime").datetime.utcnow().isoformat()+"Z",
                                                    "symbol": s,
                                                    "cp": cp,
                                                    "reason": "trend_low_bp_call",
                                                    "trend_bp": rbp,
                                                    "pct": pct,
                                                    "ups": ups,
                                                    "required_ups": required_ups,
                                                    "m0": m0,
                                                    "mid": series[-1][1],
                                                })
                                except Exception:
                                    pass
                            continue
                        if isinstance(cp, str) and cp.upper() == 'P' and rbp > -float(index_trend_min_bp):
                            try:
                                stats_live["trend_blocked"] = int(stats_live.get("trend_blocked", 0)) + 1
                                stats_live["trend_blocked_wrong_dir"] = int(stats_live.get("trend_blocked_wrong_dir", 0)) + 1
                            except Exception:
                                pass
                            _tally("trend_wrong_dir_put")
                            continue
                    if require_vwap_alignment and not (isinstance(cp, str) and cp.upper() == 'C' and call_disable_vwap):
                        last_px = float(idx_ctx.get("last") or 0.0)
                        vw = float(idx_ctx.get("vwap") or 0.0)
                        if last_px and vw:
                            aligned = True
                            if isinstance(cp, str) and cp.upper() == 'C':
                                aligned = (last_px >= vw)
                            elif isinstance(cp, str) and cp.upper() == 'P':
                                aligned = (last_px <= vw)
                            if not aligned:
                                try:
                                    stats_live["vwap_blocked"] = int(stats_live.get("vwap_blocked", 0)) + 1
                                except Exception:
                                    pass
                                if enable_probes and isinstance(cp, str) and cp.upper() == 'C':
                                    try:
                                        series = state.get(s, [])
                                        if series:
                                            ts_now = now_utc
                                            cutoff = ts_now - float(window_sec)
                                            window_series = [(t,m) for (t,m) in series if t >= cutoff]
                                            if window_series:
                                                m0 = window_series[0][1]
                                                ups = sum(1 for i in range(1,len(window_series)) if window_series[i][1] > window_series[i-1][1])
                                                steps = max(1,(len(window_series)-1))
                                                import math as _math
                                                required_ups = int(_math.ceil(float(uptick_ratio)*steps)) if float(uptick_ratio)>0 else 0
                                                if required_ups>steps: required_ups=steps
                                                pct = (window_series[-1][1]-m0)/m0 if m0>0 else -1
                                                thresh = float(pct_thresh_cheap) if m0 < 0.5 else float(pct_thresh)
                                                if m0 >= float(min_price) and len(window_series) >= (2 if (float(uptick_ratio)<=0 and float(pct_thresh)<=0 and float(pct_thresh_cheap)<=0) else 4) and ups >= required_ups and pct >= thresh:
                                                    _write_probe({
                                                        "ts": __import__("datetime").datetime.utcnow().isoformat()+"Z",
                                                        "symbol": s,
                                                        "cp": cp,
                                                        "reason": "vwap_block_call",
                                                        "pct": pct,
                                                        "ups": ups,
                                                        "required_ups": required_ups,
                                                        "m0": m0,
                                                        "mid": window_series[-1][1],
                                                    })
                                    except Exception:
                                        pass
                                continue
                            # Margin requirement if configured
                            try:
                                m_bp = float(vwap_min_margin_bp)
                            except Exception:
                                m_bp = 0.0
                            if m_bp > 0:
                                if isinstance(cp, str) and cp.upper() == 'C':
                                    req = vw * (1.0 + (m_bp / 10000.0))
                                    if last_px < req:
                                        try:
                                            stats_live["vwap_margin_blocked"] = int(stats_live.get("vwap_margin_blocked", 0)) + 1
                                        except Exception:
                                            pass
                                        if enable_probes:
                                            try:
                                                series = state.get(s, [])
                                                if series:
                                                    ts_now = now_utc
                                                    cutoff = ts_now - float(window_sec)
                                                    window_series = [(t,m) for (t,m) in series if t >= cutoff]
                                                    if window_series:
                                                        m0 = window_series[0][1]
                                                        ups = sum(1 for i in range(1,len(window_series)) if window_series[i][1] > window_series[i-1][1])
                                                        steps = max(1,(len(window_series)-1))
                                                        import math as _math
                                                        required_ups = int(_math.ceil(float(uptick_ratio)*steps)) if float(uptick_ratio)>0 else 0
                                                        if required_ups>steps: required_ups=steps
                                                        pct = (window_series[-1][1]-m0)/m0 if m0>0 else -1
                                                        thresh = float(pct_thresh_cheap) if m0 < 0.5 else float(pct_thresh)
                                                        if m0 >= float(min_price) and len(window_series) >= (2 if (float(uptick_ratio)<=0 and float(pct_thresh)<=0 and float(pct_thresh_cheap)<=0) else 4) and ups >= required_ups and pct >= thresh:
                                                            _write_probe({
                                                                "ts": __import__("datetime").datetime.utcnow().isoformat()+"Z",
                                                                "symbol": s,
                                                                "cp": cp,
                                                                "reason": "vwap_margin_block_call",
                                                                "pct": pct,
                                                                "ups": ups,
                                                                "required_ups": required_ups,
                                                                "m0": m0,
                                                                "mid": window_series[-1][1],
                                                            })
                                            except Exception:
                                                pass
                                        continue
                                elif isinstance(cp, str) and cp.upper() == 'P':
                                    req = vw * (1.0 - (m_bp / 10000.0))
                                    if last_px > req:
                                        try:
                                            stats_live["vwap_margin_blocked"] = int(stats_live.get("vwap_margin_blocked", 0)) + 1
                                        except Exception:
                                            pass
                                        continue
                    if use_indicator_bias and not (isinstance(cp, str) and cp.upper() == 'C' and call_disable_indicator):
                        ema_v = float(idx_ctx.get("ema") or 0.0)
                        macd_v = float(idx_ctx.get("macd") or 0.0)
                        macd_sig_v = float(idx_ctx.get("macd_signal") or 0.0)
                        rsi_v = float(idx_ctx.get("rsi") or 50.0)
                        last_px = float(idx_ctx.get("last") or 0.0)
                        # Only apply if we have reasonable values
                        if ema_v > 0 and last_px > 0:
                            if isinstance(cp, str) and cp.upper() == 'C':
                                # VIX-aware RSI floors for calls
                                rsi_req = float(rsi_bull_min)
                                try:
                                    # Compute live VIX trend over the configured window
                                    _ensure_vix_loaded(now_utc)
                                    import pandas as _pd
                                    vbp = None
                                    if vix_df_live is not None:
                                        t = _pd.to_datetime(now_utc, unit="s", utc=True)
                                        idx = vix_df_live.index
                                        pos = idx.searchsorted(t, side="right") - 1
                                        if pos >= 0:
                                            now_v = float(vix_df_live["close"].iloc[pos])
                                            cutoff = t - _pd.Timedelta(seconds=float(vix_trend_window_sec))
                                            pos0 = idx.searchsorted(cutoff, side="right") - 1
                                            if pos0 < 0:
                                                pos0 = 0
                                            prev_v = float(vix_df_live["close"].iloc[pos0])
                                            if prev_v > 0:
                                                vret = (now_v - prev_v) / prev_v
                                                vbp = vret * 10000.0
                                    if vbp is not None:
                                        if vbp <= -float(vix_strong_down_min_bp):
                                            rsi_req = max(0.0, rsi_req - float(rsi_bull_min_relax_if_vix_down_strong))
                                        elif vbp >= -float(vix_trend_min_bp):
                                            rsi_req = rsi_req + float(rsi_bull_min_extra_if_vix_not_down)
                                except Exception:
                                    pass
                                if not (macd_v > macd_sig_v and rsi_v >= rsi_req and last_px >= ema_v):
                                    try:
                                        stats_live["ind_evaluated"] = int(stats_live.get("ind_evaluated", 0)) + 1
                                        stats_live["ind_blocked"] = int(stats_live.get("ind_blocked", 0)) + 1
                                        if rsi_v < rsi_req:
                                            stats_live["rsi_vix_guard_blocked"] = int(stats_live.get("rsi_vix_guard_blocked", 0)) + 1
                                    except Exception:
                                        pass
                                    if enable_probes:
                                        try:
                                            series = state.get(s, [])
                                            if series:
                                                ts_now = now_utc
                                                cutoff = ts_now - float(window_sec)
                                                window_series = [(t,m) for (t,m) in series if t >= cutoff]
                                                if window_series:
                                                    m0 = window_series[0][1]
                                                    ups = sum(1 for i in range(1,len(window_series)) if window_series[i][1] > window_series[i-1][1])
                                                    steps = max(1,(len(window_series)-1))
                                                    import math as _math
                                                    required_ups = int(_math.ceil(float(uptick_ratio)*steps)) if float(uptick_ratio)>0 else 0
                                                    if required_ups>steps: required_ups=steps
                                                    pct = (window_series[-1][1]-m0)/m0 if m0>0 else -1
                                                    thresh = float(pct_thresh_cheap) if m0 < 0.5 else float(pct_thresh)
                                                    if m0 >= float(min_price) and len(window_series) >= (2 if (float(uptick_ratio)<=0 and float(pct_thresh)<=0 and float(pct_thresh_cheap)<=0) else 4) and ups >= required_ups and pct >= thresh:
                                                        _write_probe({
                                                            "ts": __import__("datetime").datetime.utcnow().isoformat()+"Z",
                                                            "symbol": s,
                                                            "cp": cp,
                                                            "reason": "indicator_block_call",
                                                            "pct": pct,
                                                            "ups": ups,
                                                            "required_ups": required_ups,
                                                            "m0": m0,
                                                            "mid": series[-1][1],
                                                        })
                                        except Exception:
                                            pass
                                    continue
                            if isinstance(cp, str) and cp.upper() == 'P':
                                if not (macd_v < macd_sig_v and rsi_v <= float(rsi_bear_max) and last_px <= ema_v):
                                    try:
                                        stats_live["ind_evaluated"] = int(stats_live.get("ind_evaluated", 0)) + 1
                                        stats_live["ind_blocked"] = int(stats_live.get("ind_blocked", 0)) + 1
                                    except Exception:
                                        pass
                                    continue
                # Expected Move gate using SPX ret over the trend window
                if not greeks_only and em_gate:
                    _refresh_index_ctx(now_utc)
                    try:
                        rbp = abs(float(idx_ctx.get("ret_bp") or 0.0))
                        thr_bp = float(em_bp) * float(em_mult)
                        try:
                            stats_live["em_evaluated"] = int(stats_live.get("em_evaluated", 0)) + 1
                        except Exception:
                            pass
                        if rbp < thr_bp:
                            try:
                                stats_live["em_blocked"] = int(stats_live.get("em_blocked", 0)) + 1
                            except Exception:
                                pass
                            continue
                    except Exception:
                        # On any issue computing EM, do not block
                        pass
                # Light EM when trend is only marginally above min bp for calls (guardrail)
                if not greeks_only and marginal_trend_extra_em_gate and isinstance(cp, str) and cp.upper() == 'C':
                    try:
                        _refresh_index_ctx(now_utc)
                        rbp_signed = float(idx_ctx.get("ret_bp") or 0.0)
                        _min_bp_live = float(index_trend_min_bp)
                        # If adaptive override lowered the min bp for calls, respect it for marginal window check
                        if adapt_calls and int(stats_live.get("alerts_calls", 0)) == 0:
                            try:
                                elapsed_min = int((now_utc - session_start_utc) / 60.0)
                                stage = 0
                                for m in adapt_minutes:
                                    if elapsed_min >= int(m):
                                        stage += 1
                                if stage > 0 and adapt_bp_seq:
                                    idx_stage = min(stage-1, len(adapt_bp_seq)-1)
                                    _min_bp_live = float(adapt_bp_seq[idx_stage])
                            except Exception:
                                pass
                        # Apply when trend is positive but within the marginal window above min
                        if rbp_signed >= _min_bp_live and rbp_signed <= (_min_bp_live + float(marginal_trend_window_bp)):
                            # Require a lighter EM
                            rbp_abs = abs(rbp_signed)
                            if rbp_abs < float(em_light_bp):
                                try:
                                    stats_live["marginal_trend_em_blocked"] = int(stats_live.get("marginal_trend_em_blocked", 0)) + 1
                                except Exception:
                                    pass
                                continue
                    except Exception:
                        pass
                # News directional bias gate (align with agent's view if fresh/confident)
                if require_news_directional_bias and float(news_bias_min_conf) > 0:
                    _refresh_news_ctx(now_utc)
                    if news_ctx.get("fresh") and float(news_ctx.get("conf") or 0.0) >= float(news_bias_min_conf):
                        nb = str(news_ctx.get("bias") or "neutral").lower()
                        if isinstance(cp, str):
                            if cp.upper() == 'C' and nb == 'bear':
                                continue
                            if cp.upper() == 'P' and nb == 'bull':
                                continue
                # record tick if enabled (write with dynamic SPX last when available)
                if _rec_f is not None:
                    try:
                        _refresh_index_ctx(now_utc)
                        rec_atm = float(idx_ctx.get("last") or idx_close)
                    except Exception:
                        rec_atm = float(idx_close)
                    try:
                        _rec_f.write(f"{now:.3f},{s},{short},{bid:.6f},{ask:.6f},{mid:.6f},{k:.0f},{cp},{rec_atm}\n")
                    except Exception:
                        pass
                # VIX trend gate on non-greeks path as well (disabled when using agent decisions)
                if (not greeks_only) and (not use_agent_decider):
                    try:
                        stats_live["vix_evaluated"] = int(stats_live.get("vix_evaluated", 0)) + 1
                    except Exception:
                        pass
                    vix_ok_live = True
                    try:
                        if not (isinstance(cp, str) and cp.upper() == 'C' and (call_disable_vix or bool(vix_puts_only))):
                            vix_ok_live = _vix_ok_live(now_utc, cp)
                    except Exception:
                        vix_ok_live = _vix_ok_live(now_utc, cp)
                    if not vix_ok_live:
                        try:
                            stats_live["vix_blocked"] = int(stats_live.get("vix_blocked", 0)) + 1
                        except Exception:
                            pass
                        if enable_probes and isinstance(cp,str) and cp.upper()=="C":
                            try:
                                series = state.get(s, [])
                                if series:
                                    ts_now = now_utc
                                    cutoff = ts_now - float(window_sec)
                                    window_series = [(t,m) for (t,m) in series if t >= cutoff]
                                    if window_series:
                                        m0 = window_series[0][1]
                                        ups = sum(1 for i in range(1,len(window_series)) if window_series[i][1] > window_series[i-1][1])
                                        steps = max(1,(len(window_series)-1))
                                        import math as _math
                                        required_ups = int(_math.ceil(float(uptick_ratio)*steps)) if float(uptick_ratio)>0 else 0
                                        if required_ups>steps: required_ups=steps
                                        pct = (window_series[-1][1]-m0)/m0 if m0>0 else -1
                                        thresh = float(pct_thresh_cheap) if m0 < 0.5 else float(pct_thresh)
                                        if m0 >= float(min_price) and len(window_series) >= (2 if (float(uptick_ratio)<=0 and float(pct_thresh)<=0 and float(pct_thresh_cheap)<=0) else 4) and ups >= required_ups and pct >= thresh:
                                            _write_probe({
                                                "ts": __import__("datetime").datetime.utcnow().isoformat()+"Z",
                                                "symbol": s,
                                                "cp": cp,
                                                "reason": "vix_block_call",
                                                "pct": pct,
                                                "ups": ups,
                                                "required_ups": required_ups,
                                                "m0": m0,
                                                "mid": window_series[-1][1],
                                            })
                            except Exception:
                                pass
                        continue
                # Agent decision gating (replaces trend/VWAP/RSI/VIX gates)
                if use_agent_decider and (not greeks_only):
                    try:
                        # Build lightweight feature context from index snapshot
                        _refresh_index_ctx(now_utc)
                        feats = {}
                        try:
                            feats["bp_60"] = float(idx_ctx.get("ret_bp") or 0.0)
                        except Exception:
                            feats["bp_60"] = 0.0
                        try:
                            feats["rsi_14"] = float(idx_ctx.get("rsi") or 50.0)
                        except Exception:
                            feats["rsi_14"] = 50.0
                        # vwap_dev may be unavailable in this live context; default to 0
                        feats["vwap_dev"] = 0.0
                        # Invoke agent decision; p_micro/p_tsfm not computed here -> defaults inside agent
                        from .agent.decision import get_agent_recommendation as _agent_decide  # type: ignore
                        from .agent.logging import append_jsonl as _append_jsonl_reco, daily_reco_path as _daily_reco_path  # type: ignore
                        reco = _agent_decide(
                            symbol="SPX",
                            features=feats,
                            p_micro=None,
                            p_tsfm=None,
                            allow_sell=True,
                            use_news=True,
                        )
                        # Log reco for audit
                        try:
                            _append_jsonl_reco(_daily_reco_path(), {**reco, "ts": _dt.now(_tz.utc).isoformat()})
                        except Exception:
                            pass
                        want = reco.get("recommendation")
                        if want not in ("CALL", "PUT"):
                            continue
                        want_cp = "C" if want == "CALL" else "P"
                        if not (isinstance(cp, str) and cp.upper() == want_cp):
                            continue
                    except Exception:
                        # If agent path fails, fall back to proceeding (no extra gating)
                        pass

                ok_m, reason_m = _should_alert(s, now, mid, k, cp)
                if not ok_m:
                    try:
                        if reason_m == "min_price":
                            stats_live["momentum_min_price"] = int(stats_live.get("momentum_min_price", 0)) + 1
                        elif reason_m == "too_short":
                            stats_live["momentum_too_short"] = int(stats_live.get("momentum_too_short", 0)) + 1
                        elif reason_m == "upticks":
                            stats_live["momentum_upticks"] = int(stats_live.get("momentum_upticks", 0)) + 1
                        elif reason_m == "pct":
                            stats_live["momentum_pct"] = int(stats_live.get("momentum_pct", 0)) + 1
                        elif reason_m == "debounce":
                            stats_live["debounce_blocked"] = int(stats_live.get("debounce_blocked", 0)) + 1
                    except Exception:
                        pass
                    continue
                else:
                    # Trend snapshot for sizing recommendation
                    _refresh_index_ctx(now_utc)
                    trend_bp = float(idx_ctx.get("ret_bp") or 0.0)
                    size_reco = 1.0
                    try:
                        if bool(use_alert_size_reco):
                            if isinstance(cp, str) and cp.upper() == 'P':
                                if trend_bp > 0:
                                    size_reco = float(put_pos_trend_size)
                                else:
                                    size_reco = float(put_neg_trend_size)
                            if isinstance(cp, str) and cp.upper() == 'C' and trend_bp < 0:
                                size_reco = float(call_neg_trend_size)
                    except Exception:
                        size_reco = 1.0
                    payload = {
                        "ts": _dt.now(_tz.utc).isoformat(),
                        "symbol": s,
                        "short": short,
                        "mid": mid,
                        "bid": bid,
                        "ask": ask,
                        "strike": k,
                        "cp": cp,
                        "atm": idx_close,
                        "scanned": scanned,
                        "trend_bp": trend_bp,
                        "vix_trend_ok": bool(use_vix_trend and _vix_ok_live(now_utc, cp)) if use_vix_trend else True,
                        "indicator_used": bool(use_indicator_bias),
                        "em_gate": bool(em_gate),
                        "adapt_stage": int(stats_live.get("adapt_stage",0)),
                        "reason_codes": list(reason_counts.keys())[:50],
                        "size_reco": size_reco,
                    }
                    _append_alert(payload)
                    try:
                        stats_live["alerts"] = int(stats_live.get("alerts", 0)) + 1
                        if isinstance(cp, str) and cp.upper() == 'C':
                            stats_live["alerts_calls"] = int(stats_live.get("alerts_calls", 0)) + 1
                            _mark_probe_converted(s, now_utc, window_sec=max(180.0, float(window_sec)))
                        elif isinstance(cp, str) and cp.upper() == 'P':
                            stats_live["alerts_puts"] = int(stats_live.get("alerts_puts", 0)) + 1
                    except Exception:
                        pass
                    _notify("SPXW Rocket", f"{s} mid={mid:.2f} (strike {k:.0f})")
            except Exception:
                continue
        _time.sleep(max(0.5, float(poll)))

    # Close recorder if open
    try:
        if _rec_f is not None:
            _rec_f.flush()
            _rec_f.close()
    except Exception:
        pass

    # Build per-reason conversion stats
    try:
        probe_reason_stats = {}
        for r, c in probe_reason_counts.items():
            conv = probe_reason_converted.get(r, 0)
            probe_reason_stats[r] = {"probes": int(c), "converted": int(conv), "conversion_ratio": (float(conv)/c) if c>0 else None}
    except Exception:
        probe_reason_stats = {}

    final_summary = {
        "ok": True,
        "watched": len(curated),
        "scanned": scanned,
        **{k: int(v) if isinstance(v, (int, float)) else v for k, v in stats_live.items()},
        "alerts_path": str(alerts_path),
        "ticks_path": (str(_rec_path) if _rec_path else None),
        "probe_reason_stats": probe_reason_stats,
    }
    if 'reason_counts' in locals():
        final_summary['reasons'] = reason_counts
    # Rolling effectiveness: rough ratio of real alerts to probes (conversion proxy)
    try:
        probes = int(stats_live.get("probe_logged",0))
        alerts = int(stats_live.get("alerts",0))
        final_summary["probe_conversion_ratio"] = (alerts / probes) if probes>0 else None
    except Exception:
        pass
    typer.echo(json.dumps(final_summary, indent=2))
    # Persist stats json
    try:
        stats_path = alerts_path.with_name(f"{alerts_path.stem}_stats.json")
        with stats_path.open("w", encoding="utf-8") as sf:
            json.dump(final_summary, sf, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # Optional diagnostics_out alias
    if diagnostics_out:
        try:
            dp = _Path(diagnostics_out)
            dp.parent.mkdir(parents=True, exist_ok=True)
            dp.write_text(json.dumps(final_summary, indent=2), encoding='utf-8')
        except Exception:
            pass


@app.command(name="rocket_replay")
def rocket_replay(
    ticks_csv: Path = typer.Argument(..., help="CSV of recorded ticks from rocket_watch (ts,symbol,short,bid,ask,mid,strike,cp,atm[,volume])"),
    out: Path = typer.Option(None, help="Output alerts JSONL path (defaults to ticks_csv stem + '_replay.jsonl')"),
    preset: str = typer.Option(None, help="Optional preset for tuned gate defaults (e.g., 'mimic_ex_post')", case_sensitive=False),
    include_hours: str = typer.Option("09:35-16:00", help="US/Eastern time windows 'HH:MM-HH:MM(;HH:MM-HH:MM)' to include entries only if ts within"),
    direction_bias: str = typer.Option(None, help="Optional directional bias filter: bull=calls only, bear=puts only", case_sensitive=False),
    expiry: str = typer.Option(None, help="Expiry date YYYY-MM-DD (used for greeks-only TTE calculation)"),
    min_price: float = typer.Option(0.05, help="Minimum mid price to consider a rocket candidate active"),
    pct_thresh_cheap: float = typer.Option(0.7, help="Percent rise threshold for rockets starting below $0.50 (e.g., 0.7=+70%)"),
    pct_thresh: float = typer.Option(0.4, help="Percent rise threshold for rockets starting at/above $0.50 (e.g., 0.4=+40%)"),
    uptick_ratio: float = typer.Option(0.5, help="Required fraction of upticks in the window [0-1]"),
    window_sec: float = typer.Option(75.0, help="Lookback seconds for momentum window"),
    debounce_sec: float = typer.Option(180.0, help="Minimum seconds between alerts per symbol"),
    max_spread_abs: float = typer.Option(0.20, help="Optional: exclude ticks with bid-ask spread above this absolute $ amount; 0 disables"),
    max_spread_frac: float = typer.Option(0.005, help="Optional: exclude ticks with (ask-bid)/mid above this fraction; ignored if bid/ask missing or mid<=0"),
    min_tick_volume: float = typer.Option(0.0, help="Optional: require per-tick volume >= this value when volume column is present; 0 disables"),
    min_window_cum_volume: float = typer.Option(0.0, help="Optional: require cumulative volume within window >= this value when volume column is present; 0 disables"),
    require_index_trend: bool = typer.Option(True, help="If true, require index (atm) trend alignment with option right within window (calls need uptrend, puts need downtrend)"),
    index_trend_window_sec: float = typer.Option(60.0, help="Lookback seconds for index trend window (uses 'atm' column)"),
    index_trend_min_bp: float = typer.Option(15.0, help="Minimum absolute index trend in basis points (e.g., 15 = 0.15%)"),
    require_vwap_alignment: bool = typer.Option(True, help="Require SPX VWAP alignment (calls above VWAP, puts below)"),
    vwap_min_margin_bp: float = typer.Option(0.0, help="If >0, require SPX to clear VWAP by at least this many basis points in the direction of the trade (calls above, puts below)"),
    em_gate: bool = typer.Option(True, help="If true, require absolute ATM move within window to exceed EM threshold (bp * multiplier)"),
    em_bp: float = typer.Option(4.0, help="Baseline expected-move threshold in basis points"),
    em_mult: float = typer.Option(1.0, help="Multiplier applied to EM bp threshold (e.g., 0.8/1.0/1.2)"),
    # Light EM when trend is marginal (additional guardrail for calls)
    marginal_trend_extra_em_gate: bool = typer.Option(False, help="If true, apply a lighter EM threshold when index trend is only marginally above the minimum bp"),
    marginal_trend_window_bp: float = typer.Option(1.0, help="Window (in bp) above the minimum trend requirement considered 'marginal' for applying the light EM gate"),
    em_light_bp: float = typer.Option(2.0, help="Light EM threshold in basis points used when trend is marginal and marginal_trend_extra_em_gate is enabled"),
    use_global_index_trend: bool = typer.Option(False, help="Use a global ATM timeline shared across symbols for index trend/EM gating (more consistent)"),
    # Greeks-only filters (optional; when enabled, bypass momentum/index/EM and use greeks thresholds only)
    greeks_only: bool = typer.Option(False, help="If true, generate alerts solely based on greeks thresholds"),
    delta_min: float = typer.Option(0.02, help="Minimum absolute delta to include"),
    delta_max: float = typer.Option(0.30, help="Maximum absolute delta to include"),
    iv_min: float = typer.Option(0.0, help="Minimum IV to include (0 disables)"),
    iv_max: float = typer.Option(10.0, help="Maximum IV to include (10 disables)"),
    gamma_max: float = typer.Option(0.0, help="If > 0, require gamma <= this value (0 disables)"),
    vega_max: float = typer.Option(0.0, help="If > 0, require vega <= this value (0 disables)"),
    # New: VIX + indicator gates
    use_vix_trend: bool = typer.Option(True, help="Require VIX trend to align with option direction (VIX up -> puts, VIX down -> calls)"),
    vix_puts_only: bool = typer.Option(False, help="If true, enforce VIX gate only for puts (calls ignore VIX gate)"),
    vix_trend_window_sec: float = typer.Option(60.0, help="Lookback seconds for VIX trend window"),
    # Momentum tuning (diagnostic): minimum samples in window and minimum upticks required
    min_samples: int = typer.Option(4, help="Minimum number of mid samples required within momentum window to evaluate"),
    min_ups: int = typer.Option(3, help="Minimum number of upticks required within momentum window (diagnostic override)"),
    vix_trend_min_bp: float = typer.Option(8.0, help="Minimum absolute VIX trend in basis points to enforce gate"),
    use_indicator_bias: bool = typer.Option(True, help="Require SPX indicator bias alignment (EMA/SMA/MACD/RSI) with option direction"),
    rsi_bull_min: float = typer.Option(48.0, help="Minimum RSI for bullish bias (calls)"),
    rsi_bear_max: float = typer.Option(52.0, help="Maximum RSI for bearish bias (puts)"),
    require_sma_alignment: bool = typer.Option(True, help="If true, also require last >= SMA for calls and last <= SMA for puts"),
    # RSI floors tied to VIX strength (for calls): tighten when VIX-down is weak, relax when VIX-down is strong
    rsi_bull_min_extra_if_vix_not_down: float = typer.Option(2.0, help="Add this to rsi_bull_min if VIX is not trending down enough for calls (makes RSI requirement stricter)"),
    rsi_bull_min_relax_if_vix_down_strong: float = typer.Option(1.0, help="Subtract this from rsi_bull_min if VIX is strongly down for calls (makes RSI requirement looser)"),
    vix_strong_down_min_bp: float = typer.Option(12.0, help="Threshold (bp) to consider VIX strongly down for RSI relaxation"),
    # New: require minimum Polygon day volume per option (snapshot) to include
    min_day_volume: float = typer.Option(0.0, help="If >0, require Polygon snapshot day.volume >= this per symbol (one-time snapshot per symbol)"),
    # Embed size recommendations in replay alerts for downstream PnL testing
    use_alert_size_reco: bool = typer.Option(True, help="Include size_reco in replay alerts for PnL sizing"),
    put_pos_trend_size: float = typer.Option(0.5, help="Replay: if cp='P' and index trend>0, recommend this size multiplier (0 to skip)"),
    put_neg_trend_size: float = typer.Option(1.0, help="Replay: if cp='P' and index trend<=0, recommend this size multiplier (e.g., 0.5 to halve)"),
    call_neg_trend_size: float = typer.Option(1.0, help="Replay: if cp='C' and index trend<0, recommend this size multiplier (0 to skip)"),
):
    """Replay recorded ticks to generate rocket alerts offline with tunable gates.

    Use this to backtest gate changes from a single day of recorded mids without hitting live feeds.
    """
    import csv as _csv
    from datetime import datetime as _dt, timezone as _tz

    if not ticks_csv.exists():
        typer.echo(json.dumps({"ok": False, "error": f"ticks file not found: {ticks_csv}"}, indent=2))
        raise typer.Exit(1)

    # Apply tuned defaults for known presets (only overrides when user hasn't supplied custom values)
    try:
        _preset = (preset or "").strip().lower() if preset else None
        if _preset == "quick_win":
            # Contract selection: keep both rights; same hours focus
            ensure_both_rights = True
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:45;11:45-15:00"
            # Momentum thresholds
            if float(min_price) == 0.05:
                min_price = 0.10
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.05
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.02
            if float(window_sec) == 75.0:
                window_sec = 300.0
            # Debounce unchanged
            # Liquidity (replay supports only spread caps)
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.15
            # Index/VWAP/Indicators
            require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 120.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 3.0
            use_indicator_bias = True
            # VIX
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 6.0
            if bool(vix_puts_only) is not True:
                vix_puts_only = True
            # EM gate: enabled at 3bp
            em_gate = True
            if float(em_bp) == 4.0:
                em_bp = 3.0
            if float(em_mult) == 1.0:
                em_mult = 1.0
            # Day volume: off during ramp unless explicitly set
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
        if _preset == "mimic_ex_post":
            # Focus windows observed in ex-post winners: early scalps + midday trend
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:45;11:45-15:00"
            # Momentum/price and debounce
            if float(min_price) == 0.05:
                min_price = 0.30
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.40
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.25
            if float(window_sec) == 75.0:
                window_sec = 60.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 180.0
            # Liquidity/spread
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.20
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.015
            # Trend/EM/VIX/Indicators
            # Respect explicit user disables: if already False, do not force True
            if bool(require_index_trend) is not False:
                require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 60.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 12.0
            if bool(em_gate) is not False:
                em_gate = True
            if float(em_bp) == 4.0:
                em_bp = 3.0
            if float(em_mult) == 1.0:
                em_mult = 1.0
            # Always use global ATM for consistency in this preset
            use_global_index_trend = True
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 6.0
            if bool(use_indicator_bias) is not False:
                use_indicator_bias = True
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 50.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 50.0
            if bool(require_sma_alignment) is not False:
                require_sma_alignment = True
            # Day volume safeguard
            if float(min_day_volume) == 0.0:
                min_day_volume = 1000.0
        elif _preset == "vix_puts_only_loose":
            # Loosest directional gating that worked in diagnosis: VIX-only (puts-only), no trend/EM/indicator, global ATM enabled
            # Momentum/price and debounce
            if float(min_price) == 0.05:
                min_price = 0.10
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.05
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.02
            if float(window_sec) == 75.0:
                window_sec = 240.0
            if float(uptick_ratio) == 0.5:
                uptick_ratio = 0.0
            if int(min_samples) == 4:
                min_samples = 3
            if int(min_ups) == 3:
                min_ups = 1
            # Liquidity/spread (relaxed)
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.30
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.20
            # Disable heavy gates; enable global ATM for any downstream trend calc
            if bool(require_index_trend) is True:
                require_index_trend = False
            if bool(em_gate) is True:
                em_gate = False
            # Always use global ATM for consistency in this preset
            use_global_index_trend = True
            if bool(use_indicator_bias) is True:
                use_indicator_bias = False
            if bool(require_sma_alignment) is True:
                require_sma_alignment = False
            # VIX only (puts-only) with looser window and threshold
            use_vix_trend = True
            vix_puts_only = True
            if float(vix_trend_window_sec) == 60.0:
                vix_trend_window_sec = 120.0
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 2.0
            # Day volume safeguard off by default (diagnostic/backfill mode)
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
        elif _preset == "vix_puts_only_tight":
            # Tighter variant: keep VIX-only (puts-only) but raise thresholds and enable global ATM index trend alignment
            # Momentum/price and debounce: keep loosened price floor, slightly shorter window if at default
            if float(min_price) == 0.05:
                min_price = 0.10
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.05
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.02
            if float(window_sec) == 75.0:
                window_sec = 240.0
            if float(uptick_ratio) == 0.5:
                uptick_ratio = 0.0
            if int(min_samples) == 4:
                min_samples = 3
            if int(min_ups) == 3:
                min_ups = 1
            # Liquidity/spread: tighten a notch from the loose preset if field is at the default
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.25
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.15
            # Enable index trend alignment using global ATM series with a small threshold for consistency
            if bool(require_index_trend) is not False:
                require_index_trend = True
            # Always use global ATM for consistency in this preset
            use_global_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 120.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 3.0
            # Keep EM and indicator gates off in this step to isolate VIX+index behavior
            if bool(em_gate) is True:
                em_gate = False
            if bool(use_indicator_bias) is True:
                use_indicator_bias = False
            if bool(require_sma_alignment) is True:
                require_sma_alignment = False
            # VIX: apply to puts-only, raise window/threshold over loose
            use_vix_trend = True
            vix_puts_only = True
            if float(vix_trend_window_sec) == 60.0:
                vix_trend_window_sec = 150.0
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            # Day volume safeguard remains off unless explicitly provided
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
        elif _preset == "moderate_momo":
            # Balanced/moderate momentum preset: moderate momentum thresholds, VWAP alignment with small margin,
            # lighter index trend requirement, EM gate on, VIX + indicator alignment enabled but not overly strict.
            # Hours: focus away from the opening 10 minutes and last 45 minutes if default
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:45;11:45-15:15"
            # Momentum/price
            if float(min_price) == 0.05:
                min_price = 0.15
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.28
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.18
            if float(window_sec) == 75.0:
                window_sec = 180.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 120.0
            # Liquidity/spread
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.20
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.020  # allow up to 2% spread fraction
            # Trend/EM/VWAP
            if bool(require_index_trend) is not False:
                require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 90.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 6.0
            # Enable EM with slightly lighter bp
            if bool(em_gate) is not False:
                em_gate = True
            if float(em_bp) == 4.0:
                em_bp = 3.0
            if float(em_mult) == 1.0:
                em_mult = 1.0
            # Use global ATM timeline for consistency
            use_global_index_trend = True
            # VWAP alignment with small margin
            if bool(require_vwap_alignment) is not False:
                require_vwap_alignment = True
            if float(vwap_min_margin_bp) == 0.0:
                vwap_min_margin_bp = 5.0
            # VIX + indicators (moderate thresholds)
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            vix_puts_only = False
            if float(vix_trend_window_sec) == 60.0:
                vix_trend_window_sec = 120.0
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            if bool(use_indicator_bias) is not False:
                use_indicator_bias = True
            # Relax RSI band slightly; SMA alignment off to avoid over-filtering
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 47.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 53.0
            if bool(require_sma_alignment) is True:
                require_sma_alignment = False
            # Volume safeguard left off unless explicitly provided
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
        elif _preset == "calls_loose_directional":
            # Replay variant of calls_loose_directional: broaden candidate set for calls on soft bullish drift days.
            ensure_both_rights = True
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:45;11:45-15:15"
            if float(min_price) == 0.05:
                min_price = 0.15
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.18
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.12
            if float(window_sec) == 75.0:
                window_sec = 180.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 120.0
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.18
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.12
            # Maintain indicator + VIX alignment but lighten thresholds
            if bool(require_index_trend) is not False:
                require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 90.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 2.0
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            if bool(use_indicator_bias) is not False:
                use_indicator_bias = True
            # Relax RSI band to include neutral
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 47.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 53.0
            require_sma_alignment = False
            # Remove EM gate unless explicitly kept
            if bool(em_gate) is True:
                em_gate = False
            # Add guardrails for calls: VWAP margin and light EM when marginal
            if float(vwap_min_margin_bp) == 0.0:
                vwap_min_margin_bp = 10.0
            if bool(marginal_trend_extra_em_gate) is not True:
                marginal_trend_extra_em_gate = True
            if float(em_light_bp) == 2.0:
                em_light_bp = 2.0
            if float(marginal_trend_window_bp) == 1.0:
                marginal_trend_window_bp = 1.0
            # Volume off (discovery)
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
        elif _preset == "calls_reversal_vix_crush":
            # Replay variant targeting reversal + VIX bleed scenarios
            ensure_both_rights = True
            if include_hours == "09:35-16:00":
                include_hours = "10:05-11:30;12:30-15:10"
            if float(min_price) == 0.05:
                min_price = 0.12
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.16
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.10
            if float(window_sec) == 75.0:
                window_sec = 150.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 90.0
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.18
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.10
            if bool(require_index_trend) is not False:
                require_index_trend = True
            if float(index_trend_window_sec) == 60.0:
                index_trend_window_sec = 75.0
            if float(index_trend_min_bp) == 15.0:
                index_trend_min_bp = 1.0
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 6.0
            vix_puts_only = False
            if bool(use_indicator_bias) is not False:
                use_indicator_bias = True
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 46.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 54.0
            if bool(require_sma_alignment) is True:
                require_sma_alignment = False
            if bool(em_gate) is True:
                em_gate = False
            # Guardrails for reversals too
            if float(vwap_min_margin_bp) == 0.0:
                vwap_min_margin_bp = 8.0
            if bool(marginal_trend_extra_em_gate) is not True:
                marginal_trend_extra_em_gate = True
            if float(em_light_bp) == 2.0:
                em_light_bp = 2.0
            if float(marginal_trend_window_bp) == 1.0:
                marginal_trend_window_bp = 1.0
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
        elif _preset == "open_loose":
            # Market open ramp for replay: permissive momentum and liquidity, disable heavy gates, light VIX alignment.
            ensure_both_rights = True
            if include_hours == "09:35-16:00":
                include_hours = "09:45-10:30;11:45-15:10"
            # Momentum thresholds
            if float(min_price) == 0.05:
                min_price = 0.10
            if float(pct_thresh_cheap) == 0.7:
                pct_thresh_cheap = 0.12
            if float(pct_thresh) == 0.4:
                pct_thresh = 0.08
            if float(window_sec) == 75.0:
                window_sec = 150.0
            if float(debounce_sec) == 180.0:
                debounce_sec = 90.0
            # Liquidity/spread (relaxed)
            if float(max_spread_abs) == 0.20:
                max_spread_abs = 0.20
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.025
            # Heavy gates off
            require_index_trend = False
            require_vwap_alignment = False
            use_indicator_bias = False
            if bool(em_gate) is True:
                em_gate = False
            use_global_index_trend = False
            # VIX alignment (light)
            use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 3.0
            vix_puts_only = False
            # Light RSI relax
            if float(rsi_bull_min) == 48.0:
                rsi_bull_min = 47.0
            if float(rsi_bear_max) == 52.0:
                rsi_bear_max = 53.0
            require_sma_alignment = False
            # Volume safeguard off unless explicitly provided
            if float(min_day_volume) == 0.0:
                min_day_volume = 0.0
            
        elif _preset == "greeks_min_gates":
            # Minimal guardrails for replay: greeks-only with light VIX; disable heavy trend/EM/indicator gates
            # Liquidity: relax fractional spread if at default
            if float(max_spread_frac) == 0.005:
                max_spread_frac = 0.020
            # Disable heavy gates when they are at their default True values
            if bool(require_index_trend) is True:
                require_index_trend = False
            if bool(em_gate) is True:
                em_gate = False
            if bool(use_global_index_trend) is True:
                use_global_index_trend = False
            if bool(use_indicator_bias) is True:
                use_indicator_bias = False
            if bool(require_sma_alignment) is True:
                require_sma_alignment = False
            # Keep VIX alignment, slightly relaxed threshold if default
            if bool(use_vix_trend) is not False:
                use_vix_trend = True
            if float(vix_trend_min_bp) == 8.0:
                vix_trend_min_bp = 4.0
            # Greeks-only selection path and delta focus if defaults
            greeks_only = True
            if float(delta_min) == 0.02:
                delta_min = 0.04
            if float(delta_max) == 0.30:
                delta_max = 0.35
            # Day volume safeguard when using snapshots/aggregates
            if float(min_day_volume) == 0.0:
                min_day_volume = 500.0
    except Exception:
        pass

    # Load ticks grouped by symbol
    by_sym: dict[str, list[tuple[float, float, dict]]] = {}
    with ticks_csv.open("r", encoding="utf-8") as f:
        r = _csv.DictReader(f)
        for row in r:
            try:
                ts = float(row.get("ts", "0") or 0)
                sym = row.get("symbol") or row.get("occ") or ""
                if not sym:
                    continue
                mid = float(row.get("mid", "0") or 0)
                # Parse optional volume gracefully
                _vraw = row.get("volume")
                try:
                    _v = float(_vraw) if (_vraw not in (None, "")) else None
                except Exception:
                    _v = None
                meta = {
                    "short": row.get("short"),
                    "bid": float(row.get("bid", "0") or 0),
                    "ask": float(row.get("ask", "0") or 0),
                    "strike": float(row.get("strike", "0") or 0),
                    "cp": row.get("cp"),
                    "atm": float(row.get("atm", "0") or 0),
                    "volume": _v,
                }
                by_sym.setdefault(sym, []).append((ts, mid, meta))
            except Exception:
                continue

    # Prepare state
    alerts_path = out or Path(str(ticks_csv).replace(".csv", "_replay.jsonl"))
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, list[tuple[float, float]]] = {}
    vol_state: dict[str, list[tuple[float, float]]] = {}
    atm_state: dict[str, list[tuple[float, float]]] = {}
    last_alert: dict[str, float] = {}
    # Optional: cache Polygon day volume per symbol for the session
    vol_map: dict[str, float] = {}
    if float(min_day_volume) > 0.0:
        try:
            settings = Settings()
            if settings.has_polygon:
                poly = PolygonClient(PolygonConfig.from_env())
                # Determine session date from earliest tick (US/Eastern)
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    import datetime as __dt
                    all_ts = [rows[0][0] for rows in by_sym.values() if rows]
                    _min_ts = min(all_ts) if all_ts else None
                    et = _ZI('America/New_York')
                    utc = _ZI('UTC')
                    session_date_et = __dt.datetime.fromtimestamp(_min_ts, tz=utc).astimezone(et).date() if _min_ts else None
                    today_et = __dt.datetime.now(tz=et).date()
                    is_today = (session_date_et == today_et) if session_date_et else False
                except Exception:
                    session_date_et = None
                    is_today = False

                # If replaying a past session, compute day volume from historical minute aggregates; else use snapshot day.volume
                if session_date_et and not is_today:
                    # Sum minute "volume" for each option during RTH on the session date using historical aggregates
                    import pytz as _pytz
                    import pandas as _pd
                    from datetime import time as _tcls, datetime as _dt_cls
                    et_tz = _pytz.timezone('America/New_York')
                    # Build UTC bounds for 09:30-16:00 ET of the session date
                    try:
                        dt0_et = et_tz.localize(_dt_cls(session_date_et.year, session_date_et.month, session_date_et.day, 9, 30, 0))
                        dt1_et = et_tz.localize(_dt_cls(session_date_et.year, session_date_et.month, session_date_et.day, 16, 0, 0))
                        start_utc = dt0_et.astimezone(_pytz.UTC)
                        end_utc = dt1_et.astimezone(_pytz.UTC)
                    except Exception:
                        start_utc = None
                        end_utc = None
                    for sym in by_sym.keys():
                        # If we don't have proper UTC bounds, fall back to snapshot volume for this symbol
                        if start_utc is None or end_utc is None:
                            try:
                                snap = poly.fetch_option_snapshot(f"O:{sym}") or {}
                                r = snap.get("results", {}) if isinstance(snap, dict) else {}
                                day = r.get("day", {}) if isinstance(r, dict) else {}
                                vol_map[sym] = float(day.get("volume") or 0.0)
                            except Exception:
                                vol_map[sym] = 0.0
                            continue
                        try:
                            df = poly.fetch_option_aggs(sym, start=start_utc, end=end_utc, timespan="minute", mult=1)
                        except Exception:
                            df = None
                        dv = 0.0
                        try:
                            if df is not None and not df.empty:
                                # Construct UTC datetime column from ms timestamp and filter to RTH window
                                ts_utc = _pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                                if start_utc is not None and end_utc is not None:
                                    mask = (ts_utc >= _pd.Timestamp(start_utc)) & (ts_utc <= _pd.Timestamp(end_utc))
                                    dfr = df.loc[mask]
                                else:
                                    dfr = df
                                if not dfr.empty and "volume" in dfr.columns:
                                    dv = float(_pd.to_numeric(dfr["volume"], errors="coerce").fillna(0.0).sum())
                        except Exception:
                            dv = 0.0
                        vol_map[sym] = dv
                else:
                    # Today (or unknown date): fallback to snapshot day.volume per option
                    for sym in by_sym.keys():
                        try:
                            snap = poly.fetch_option_snapshot(f"O:{sym}") or {}
                            r = snap.get("results", {}) if isinstance(snap, dict) else {}
                            day = r.get("day", {}) if isinstance(r, dict) else {}
                            dv = float(day.get("volume") or 0.0)
                            vol_map[sym] = dv
                        except Exception:
                            vol_map[sym] = 0.0
        except Exception:
            vol_map = {}


    # Lightweight instrumentation counters for gate diagnostics
    stats: dict[str, float | int] = {
        "em_evaluated": 0,
        "em_blocked": 0,
        "em_passed": 0,
        "trend_evaluated": 0,
        "trend_blocked": 0,
        # New: trend fallback diagnostics (use SPX minute bars when ATM timeline is too sparse)
        "trend_fallback_used": 0,
        # New: diagnostics for VIX and indicator gates
        "vix_evaluated": 0,
        "vix_blocked": 0,
        "ind_evaluated": 0,
        "ind_blocked": 0,
        # VWAP diagnostics
        "vwap_blocked": 0,
        "vwap_margin_blocked": 0,
        # New: diagnostics for greeks-only path
        "greeks_evaluated": 0,
        "greeks_blocked": 0,
        # Guardrails diagnostics
        "rsi_vix_guard_blocked": 0,
        "marginal_trend_em_blocked": 0,
        # Probes parity with live watcher
        "probe_logged": 0,
    }

    # Per-reason probe diagnostics (counts and conversions), and pending probes per symbol
    probe_reason_counts: dict[str, int] = {}
    probe_reason_converted: dict[str, int] = {}
    pending_probes_by_sym: dict[str, list[dict]] = {}

    # Probe writer for replay parity (mirrors live rocket_watch naming *_probes.jsonl)
    def _write_probe(rec: dict):  # type: ignore[override]
        try:
            p = alerts_path.with_name(alerts_path.stem + "_probes.jsonl")
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["probe_logged"] = int(stats.get("probe_logged", 0)) + 1
            # Track per-reason counts and keep pending state for conversion tracking
            try:
                r = rec.get("reason") if isinstance(rec, dict) else None
                s = rec.get("symbol") if isinstance(rec, dict) else None
                if isinstance(r, str):
                    probe_reason_counts[r] = int(probe_reason_counts.get(r, 0)) + 1
                if isinstance(s, str):
                    pending = pending_probes_by_sym.setdefault(s, [])
                    pending.append(rec)
            except Exception:
                pass
        except Exception:  # noqa: BLE001
            pass

    def _append_alert(payload: dict):
        try:
            with alerts_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # Session windows gate similar to live watcher (now overridable)
    def _within_hours(ts: float) -> bool:
        try:
            from zoneinfo import ZoneInfo as _ZI
            import datetime as __dt
            dt = __dt.datetime.fromtimestamp(ts, tz=_ZI('UTC')).astimezone(_ZI('America/New_York'))
            windows = [w.strip() for w in include_hours.split(';') if w.strip()]
            for w in windows:
                p = w.split('-')
                if len(p) != 2:
                    continue
                h0, h1 = p[0].strip(), p[1].strip()
                t0 = __dt.datetime(dt.year, dt.month, dt.day, int(h0[0:2]), int(h0[3:5]), tzinfo=_ZI('America/New_York'))
                t1 = __dt.datetime(dt.year, dt.month, dt.day, int(h1[0:2]), int(h1[3:5]), tzinfo=_ZI('America/New_York'))
                if t0 <= dt <= t1:
                    return True
            return False
        except Exception:
            return True

    # Optional directional bias filtering
    _bias = (direction_bias or "").strip().lower() if direction_bias else None

    # Global ATM state (optional) for consistent index trend gating across symbols
    global_atm_state: list[tuple[float, float]] = []

    # Greeks helper: compute BS greeks using recorded ATM as spot
    import datetime as _rdt
    def _tte_days(ts: float) -> float:
        if not expiry:
            return 0.0
        try:
            from zoneinfo import ZoneInfo as _ZI
            y = int(expiry[0:4]); m = int(expiry[5:7]); d = int(expiry[8:10])
            et = _ZI('America/New_York')
            exp_local = _rdt.datetime(y, m, d, 16, 0, 0, tzinfo=et)
            exp_utc = exp_local.astimezone(_ZI('UTC')).timestamp()
            rem = max(0.0, exp_utc - ts)
            return rem / 86400.0
        except Exception:
            return 0.0

    def _greeks_ok(ts: float, mid: float, strike: float | None, cp: str | None, atm: float | None) -> tuple[bool, dict]:
        """Evaluate greeks thresholds; includes a BS fallback if IV solving fails.

        Primary path uses utils.compute_option_metrics. If that fails or returns None greeks,
        fall back to bs_greeks.implied_vol_newton; if still unresolved, use a conservative sigma guess.
        """
        try:
            spot = float(atm) if atm is not None else 0.0
        except Exception:
            spot = 0.0
        if spot <= 0 or mid <= 0 or (strike is None) or float(strike) <= 0:
            return False, {}
        t_days = _tte_days(ts)
        if t_days <= 0:
            return False, {}
        call_flag = (str(cp).upper() == 'C') if cp is not None else True
        # Try robust metrics first
        iv_val = None; d_val = None; g_val = None; v_val = None
        try:
            om = compute_option_metrics(spot=spot, strike=float(strike), t_days=t_days, mid_price=float(mid), call=call_flag, rate=0.0)
            iv_val = float(om.iv) if om.iv is not None else None
            d_val = float(om.delta) if om.delta is not None else None
            g_val = float(om.gamma) if om.gamma is not None else None
            v_val = float(om.vega) if om.vega is not None else None
        except Exception:
            iv_val = None
            d_val = None
            g_val = None
            v_val = None
        # Fallback: derive IV and greeks with BS if needed
        if (iv_val is None) or (d_val is None):
            try:
                T = float(t_days) / 365.0
                right = 'C' if call_flag else 'P'
                sig = implied_vol_newton(target_price=float(mid), S=float(spot), K=float(strike), r=0.0, T=T, right=right, initial=0.4)
                # Guard rails
                if not (sig and sig > 0.0) or math.isnan(sig) or math.isinf(sig):
                    sig = 0.6
                d_val = bs_delta(float(spot), float(strike), 0.0, float(sig), T, right)
                g_val = bs_gamma(float(spot), float(strike), 0.0, float(sig), T)
                v_val = bs_vega(float(spot), float(strike), 0.0, float(sig), T)
                iv_val = float(sig)
            except Exception:
                return False, {}
        # Apply thresholds
        try:
            a_delta = abs(float(d_val))
            if a_delta < float(delta_min) or a_delta > float(delta_max):
                return False, {"iv": iv_val, "delta": d_val, "gamma": g_val, "vega": v_val}
            if float(iv_min) > 0.0 and (iv_val is None or float(iv_val) < float(iv_min)):
                return False, {"iv": iv_val, "delta": d_val, "gamma": g_val, "vega": v_val}
            if float(iv_max) < 10.0 and (iv_val is None or float(iv_val) > float(iv_max)):
                return False, {"iv": iv_val, "delta": d_val, "gamma": g_val, "vega": v_val}
            if float(gamma_max) > 0.0 and (g_val is None or float(g_val) > float(gamma_max)):
                return False, {"iv": iv_val, "delta": d_val, "gamma": g_val, "vega": v_val}
            if float(vega_max) > 0.0 and (v_val is None or float(v_val) > float(vega_max)):
                return False, {"iv": iv_val, "delta": d_val, "gamma": g_val, "vega": v_val}
            return True, {"iv": iv_val, "delta": d_val, "gamma": g_val, "vega": v_val}
        except Exception:
            return False, {}

    # ----- SPX indicator and VIX trend data (from Polygon) loaded lazily on first use -----
    spx_ind_df = None
    vix_df = None
    _ind_cache_date = None  # cache of session date used to load

    def _ensure_indicators_loaded(ts_hint: float | None) -> None:
        nonlocal spx_ind_df, vix_df, _ind_cache_date
        if (spx_ind_df is not None) and (vix_df is not None):
            return
        try:
            # Determine session date (ET) from the provided timestamp hint
            _session_date_et = None
            if ts_hint is not None:
                from zoneinfo import ZoneInfo as _ZI
                import datetime as __dt
                _d = __dt.datetime.fromtimestamp(ts_hint, tz=_ZI('UTC')).astimezone(_ZI('America/New_York')).date()
                _session_date_et = _d
            poly_client_local = PolygonClient(PolygonConfig.from_env())
            df_spx = poly_client_local.fetch_index_intraday_aggs(ticker="I:SPX", timespan="minute", mult=1, lookback_days=2)
            df_vx = poly_client_local.fetch_index_intraday_aggs(ticker="I:VIX", timespan="minute", mult=1, lookback_days=2)
            # Build SPX indicators
            if (df_spx is not None) and (not df_spx.empty):
                import pytz as _pytz
                from datetime import time as _tcls
                et = _pytz.timezone('America/New_York')
                df_spx["et"] = df_spx["dt"].dt.tz_convert(et)
                if _session_date_et is None:
                    _session_date_et = df_spx["et"].dt.date.iloc[-1]
                start_t = _tcls(9,30); end_t = _tcls(16,0)
                dft = df_spx[(df_spx["et"].dt.date == _session_date_et) & (df_spx["et"].dt.time >= start_t) & (df_spx["et"].dt.time <= end_t)]
                if dft.empty:
                    dft = df_spx
                s = dft["close"].astype(float).copy()
                sma = s.rolling(window=20, min_periods=20).mean()
                ema = s.ewm(span=20, adjust=False).mean()
                ema_fast = s.ewm(span=12, adjust=False).mean()
                ema_slow = s.ewm(span=26, adjust=False).mean()
                macd = ema_fast - ema_slow
                macd_sig = macd.ewm(span=9, adjust=False).mean()
                delta = s.diff()
                gain = (delta.clip(lower=0)).ewm(alpha=1.0/14, adjust=False).mean()
                loss = (-delta.clip(upper=0)).ewm(alpha=1.0/14, adjust=False).mean()
                rs = gain / loss.replace({0.0: float('nan')})
                rsi = 100 - (100 / (1 + rs))
                # VWAP (minute intraday) if volume available
                try:
                    if 'volume' in dft.columns:
                        vol_ser = dft['volume'].astype(float).clip(lower=0)
                        cum_vol = vol_ser.cumsum()
                        cum_pv = (s * vol_ser).cumsum()
                        vwap = cum_pv / cum_vol.replace({0.0: float('nan')})
                    else:
                        vwap = s.expanding().mean()
                except Exception:
                    vwap = s.expanding().mean()
                spx_ind_df = dft.set_index("dt").assign(sma20=sma, ema20=ema, macd=macd, macd_signal=macd_sig, rsi=rsi, vwap=vwap, last=s)
            # Build VIX series
            if (df_vx is not None) and (not df_vx.empty):
                import pytz as _pytz
                from datetime import time as _tcls
                et = _pytz.timezone('America/New_York')
                df_vx["et"] = df_vx["dt"].dt.tz_convert(et)
                if _session_date_et is None:
                    _session_date_et = df_vx["et"].dt.date.iloc[-1]
                start_t = _tcls(9,30); end_t = _tcls(16,0)
                dfv = df_vx[(df_vx["et"].dt.date == _session_date_et) & (df_vx["et"].dt.time >= start_t) & (df_vx["et"].dt.time <= end_t)]
                vix_df = dfv.set_index("dt") if not dfv.empty else df_vx.set_index("dt")
            _ind_cache_date = _session_date_et
        except Exception:
            spx_ind_df = None
            vix_df = None

    def _indicators_ok(ts: float, cp: str | None) -> bool:
        if not use_indicator_bias:
            return True
        _ensure_indicators_loaded(ts)
        if spx_ind_df is None or spx_ind_df.empty:
            return True  # can't evaluate
        try:
            import pandas as _pd
            t = _pd.to_datetime(ts, unit="s", utc=True)
            # nearest at-or-before
            idx = spx_ind_df.index
            pos = idx.searchsorted(t, side="right") - 1
            if pos < 0:
                return True
            row = spx_ind_df.iloc[pos]
            last = float(row.get("last") or 0.0)
            ema20 = float(row.get("ema20") or 0.0)
            sma20 = float(row.get("sma20") or 0.0)
            macd_v = float(row.get("macd") or 0.0)
            macd_sig_v = float(row.get("macd_signal") or 0.0)
            rsi_v = float(row.get("rsi") or 50.0)
            # Adjust RSI floor for calls depending on VIX down strength
            adj_rsi_bull_min = float(rsi_bull_min)
            if isinstance(cp, str) and cp.upper() == 'C' and bool(use_vix_trend):
                try:
                    import pandas as _pd2
                    # compute VIX return over the same vix_trend_window_sec
                    if vix_df is not None and not vix_df.empty:
                        ti = _pd2.to_datetime(ts, unit="s", utc=True)
                        idx_v = vix_df.index
                        pos_v = idx_v.searchsorted(ti, side="right") - 1
                        if pos_v >= 0:
                            now_v = float(vix_df["close"].iloc[pos_v])
                            cut_v = ti - _pd2.Timedelta(seconds=float(vix_trend_window_sec))
                            pos0_v = idx_v.searchsorted(cut_v, side="right") - 1
                            if pos0_v < 0:
                                pos0_v = 0
                            prev_v = float(vix_df["close"].iloc[pos0_v])
                            if prev_v > 0:
                                vret = (now_v - prev_v) / prev_v
                                strong_down = (vret <= -(float(vix_strong_down_min_bp)/10000.0))
                                weak_or_not_down = (vret > -(float(vix_trend_min_bp)/10000.0))
                                if strong_down:
                                    adj_rsi_bull_min = max(0.0, adj_rsi_bull_min - float(rsi_bull_min_relax_if_vix_down_strong))
                                elif weak_or_not_down:
                                    adj_rsi_bull_min = adj_rsi_bull_min + float(rsi_bull_min_extra_if_vix_not_down)
                except Exception:
                    pass
            if isinstance(cp, str) and cp.upper() == 'C':
                if macd_v <= macd_sig_v or rsi_v < adj_rsi_bull_min:
                    try:
                        # increment guardrail block if RSI threshold caused the block
                        if rsi_v < adj_rsi_bull_min:
                            stats["rsi_vix_guard_blocked"] = int(stats.get("rsi_vix_guard_blocked", 0)) + 1
                    except Exception:
                        pass
                    return False
                if last and ema20 and last < ema20:
                    return False
                if bool(require_sma_alignment) and last and sma20 and last < sma20:
                    return False
            if isinstance(cp, str) and cp.upper() == 'P':
                if macd_v >= macd_sig_v or rsi_v > float(rsi_bear_max):
                    return False
                if last and ema20 and last > ema20:
                    return False
                if bool(require_sma_alignment) and last and sma20 and last > sma20:
                    return False
            return True
        except Exception:
            return True

    def _indicator_snapshot(ts: float) -> dict:
        """Return a best-effort indicator snapshot near the given UTC timestamp.

        Keys: spx_last, ema20, sma20, macd, macd_signal, rsi
        """
        try:
            _ensure_indicators_loaded(ts)
            if spx_ind_df is None or spx_ind_df.empty:
                return {}
            import pandas as _pd
            t = _pd.to_datetime(ts, unit="s", utc=True)
            idx = spx_ind_df.index
            pos = idx.searchsorted(t, side="right") - 1
            if pos < 0:
                return {}
            row = spx_ind_df.iloc[pos]
            snap = {
                "spx_last": float(row.get("last") or 0.0),
                "spx_vwap": float(row.get("vwap") or 0.0),
                "ema20": float(row.get("ema20") or 0.0),
                "sma20": float(row.get("sma20") or 0.0),
                "macd": float(row.get("macd") or 0.0),
                "macd_signal": float(row.get("macd_signal") or 0.0),
                "rsi": float(row.get("rsi") or 50.0),
            }
            return snap
        except Exception:
            return {}

    def _vix_ok(ts: float, cp: str | None) -> bool:
        if not use_vix_trend:
            return True
        _ensure_indicators_loaded(ts)
        if vix_df is None or vix_df.empty:
            return True
        try:
            import pandas as _pd
            t = _pd.to_datetime(ts, unit="s", utc=True)
            idx = vix_df.index
            pos = idx.searchsorted(t, side="right") - 1
            if pos < 0:
                return True
            now_px = float(vix_df["close"].iloc[pos])
            # find prior at window
            cutoff = t - _pd.Timedelta(seconds=float(vix_trend_window_sec))
            pos0 = idx.searchsorted(cutoff, side="right") - 1
            if pos0 < 0:
                pos0 = 0
            prev_px = float(vix_df["close"].iloc[pos0])
            if prev_px <= 0:
                return True
            ret = (now_px - prev_px) / prev_px
            min_req = float(vix_trend_min_bp) / 10000.0
            # If configured to apply VIX only to puts, calls bypass this check
            if bool(vix_puts_only) and isinstance(cp, str) and cp.upper() == 'C':
                return True
            if isinstance(cp, str) and cp.upper() == 'C':
                # calls want VIX downtrend
                return (ret <= -min_req)
            if isinstance(cp, str) and cp.upper() == 'P':
                # puts want VIX uptrend
                return (ret >= min_req)
            return True
        except Exception:
            return True

    def _trend_bp(ts: float) -> float:
        """Compute SPX trend in basis points over index_trend_window_sec using indicator series (fallback if ATM sparse)."""
        try:
            _ensure_indicators_loaded(ts)
            if spx_ind_df is None or spx_ind_df.empty:
                return 0.0
            import pandas as _pd
            t = _pd.to_datetime(ts, unit="s", utc=True)
            idx = spx_ind_df.index
            pos = idx.searchsorted(t, side="right") - 1
            if pos < 0:
                return 0.0
            now_px = float(spx_ind_df["last"].iloc[pos])
            cutoff = t - _pd.Timedelta(seconds=float(index_trend_window_sec))
            pos0 = idx.searchsorted(cutoff, side="right") - 1
            if pos0 < 0:
                pos0 = 0
            prev_px = float(spx_ind_df["last"].iloc[pos0])
            # Guard against NaNs and non-positive values
            if (prev_px <= 0) or (prev_px != prev_px) or (now_px != now_px):
                return 0.0
            ret = (now_px - prev_px) / prev_px
            # Handle NaN/inf results defensively
            try:
                bp = float(ret) * 10000.0
                if bp != bp or bp == float("inf") or bp == float("-inf"):
                    return 0.0
                return bp
            except Exception:
                return 0.0
        except Exception:
            return 0.0

    def _trend_bp_now(ts: float) -> float:
        """Compute trend bp using the most reliable available source at this ts.

        Preference order:
          1) Global ATM timeline over index_trend_window_sec (if enabled and populated)
          2) Indicator fallback via _trend_bp(ts)
        Always returns a finite float (NaN coerced to 0.0).
        """
        try:
            # Use global ATM state if available for consistent trend sign across symbols
            if bool(use_global_index_trend) and global_atm_state:
                import math as _math
                cut = float(index_trend_window_sec)
                series = [(t, a) for (t, a) in global_atm_state if (ts - t) <= cut and a is not None]
                if len(series) >= 2:
                    a0 = float(series[0][1]); a1 = float(series[-1][1])
                    # Guard against invalid values
                    if (a0 > 0) and (a0 == a0) and (a1 == a1):
                        ret = (a1 - a0) / a0
                        bp = float(ret) * 10000.0
                        if bp == bp and not _math.isinf(bp):
                            # If series range is effectively flat, treat as 0
                            try:
                                rng = max(a for (_, a) in series) - min(a for (_, a) in series)
                                if a0 > 0 and abs(rng / a0) < 1e-6:
                                    return 0.0
                            except Exception:
                                pass
                            return bp
            # Fallback to indicator series
            return _trend_bp(ts) or 0.0
        except Exception:
            return 0.0

    # Same gate logic as live (with optional greeks-only bypass)
    def _should_alert(sym: str, ts: float, mid: float, cp: str | None, atm: float | None, vol: float | None, strike: float | None) -> tuple[bool, dict]:
        if not _within_hours(ts):
            return False, {}
        # Day volume gate (Polygon snapshot), if provided
        if float(min_day_volume) > 0.0 and vol_map:
            dv = float(vol_map.get(sym, 0.0))
            if dv < float(min_day_volume):
                return False, {}
        # Directional bias filter: bull = calls only, bear = puts only
        if _bias in ("bull", "bear") and isinstance(cp, str) and cp in ("C", "P"):
            if _bias == "bull" and cp != "C":
                return False, {}
            if _bias == "bear" and cp != "P":
                return False, {}
        # Greeks-only short-circuit
        if greeks_only:
            stats["greeks_evaluated"] = int(stats.get("greeks_evaluated", 0)) + 1
            ok, g = _greeks_ok(ts, mid, strike, cp, atm)
            if not ok:
                stats["greeks_blocked"] = int(stats.get("greeks_blocked", 0)) + 1
                return False, {}
            # Apply VIX and indicator gates in greeks-only mode as requested
            stats["vix_evaluated"] = int(stats.get("vix_evaluated", 0)) + 1
            if not _vix_ok(ts, cp):
                stats["vix_blocked"] = int(stats.get("vix_blocked", 0)) + 1
                return False, {}
            stats["ind_evaluated"] = int(stats.get("ind_evaluated", 0)) + 1
            if not _indicators_ok(ts, cp):
                stats["ind_blocked"] = int(stats.get("ind_blocked", 0)) + 1
                return False, {}
            la = last_alert.get(sym, 0.0)
            if ts - la < float(debounce_sec):
                return False, {}
            last_alert[sym] = ts
            return True, g
        series = state.setdefault(sym, [])
        series.append((ts, mid))
        cutoff = ts - float(window_sec)
        series[:] = [(t, m) for (t, m) in series if t >= cutoff]
        # Require a minimum number of samples in the window; default 4, configurable via --min-samples
        if mid < min_price or len(series) < int(min_samples):
            return False, {}
        # Volume gating
        if float(min_tick_volume) > 0 and (vol is not None) and (vol < float(min_tick_volume)):
            return False, {}
        if float(min_window_cum_volume) > 0:
            v_series = vol_state.setdefault(sym, [])
            # Keep only within window
            v_series[:] = [(t, v) for (t, v) in v_series if t >= cutoff]
            # Consider window sum only if we have at least one non-None volume in window
            if v_series:
                cum_v = sum(v for (_, v) in v_series if v is not None)
                # if all were None, cum_v will be 0; treat that as missing volume and don't block
                if any(v is not None for (_, v) in v_series):
                    if cum_v < float(min_window_cum_volume):
                        return False, {}
        ups = sum(1 for i in range(1, len(series)) if series[i][1] > series[i-1][1])
        # Require at least min_ups upticks (default 3); can be relaxed for sparse per-symbol timelines
        required_ups = max(int(min_ups), int(float(uptick_ratio) * max(1, (len(series) - 1))))
        if ups < required_ups:
            return False, {}
        m0 = series[0][1]
        if m0 <= 0:
            return False, {}
        pct = (mid - m0) / m0
        thresh = float(pct_thresh_cheap) if m0 < 0.5 else float(pct_thresh)
        if pct < thresh:
            return False, {}
        # Cache momentum window metrics for potential probe logging later if a call gets blocked by downstream gates
        win_m0 = m0; win_pct = pct; win_ups = ups
        win_required_ups = required_ups; win_mid = mid
    # Optional index trend / EM gating using recorded 'atm' series
        # Index trend/EM gates: prefer global ATM state if enabled and available; otherwise per-symbol
        if atm is not None and float(atm) > 0:
            # Maintain per-symbol state for backwards compatibility
            a_series = atm_state.setdefault(sym, [])
            a_series.append((ts, float(atm)))
            # Maintain global state as well (dedup unnecessary here, append as seen)
            if use_global_index_trend:
                global_atm_state.append((ts, float(atm)))
        a_cut = ts - float(index_trend_window_sec)
        # Choose source series for trend computation
        trend_series: list[tuple[float, float]] | None = None
        if use_global_index_trend and global_atm_state:
            # Filter global series to window
            trend_series = [(t, a) for (t, a) in global_atm_state if t >= a_cut]
        else:
            trend_series = [(t, a) for (t, a) in atm_state.get(sym, []) if t >= a_cut]
        # Compute index trend/EM using available timeline; fallback to SPX minute bars if ATM series is sparse
        atr = None
        if trend_series and len(trend_series) >= 2:
            a0 = trend_series[0][1]
            if a0 > 0:
                last_a = trend_series[-1][1]
                atr = (last_a - a0) / a0
                # If ATM timeline appears static (e.g., recordings wrote a constant idx_close), prefer SPX fallback
                try:
                    # consider series movement negligible if range < 1e-6 fraction
                    vals = [a for (_, a) in trend_series]
                    if len(vals) >= 2:
                        rng = (max(vals) - min(vals))
                        if (rng == 0.0) or (a0 > 0 and (rng / a0) < 1e-6):
                            atr = None
                except Exception:
                    pass
        if atr is None:
            # Fallback: use SPX minute bars loaded for indicators (close-to-close over the same window)
            try:
                _ensure_indicators_loaded(ts)
                if spx_ind_df is not None and not spx_ind_df.empty:
                    import pandas as _pd
                    t = _pd.to_datetime(ts, unit="s", utc=True)
                    idx = spx_ind_df.index
                    pos = idx.searchsorted(t, side="right") - 1
                    if pos >= 0:
                        now_px = float(spx_ind_df["last"].iloc[pos])
                        cutoff = t - _pd.Timedelta(seconds=float(index_trend_window_sec))
                        pos0 = idx.searchsorted(cutoff, side="right") - 1
                        if pos0 < 0:
                            pos0 = 0
                        prev_px = float(spx_ind_df["last"].iloc[pos0])
                        if prev_px > 0:
                            atr = (now_px - prev_px) / prev_px
                            stats["trend_fallback_used"] = int(stats.get("trend_fallback_used", 0)) + 1
            except Exception:
                atr = atr  # keep None
        if atr is not None:
            if require_index_trend and isinstance(cp, str) and cp in ("C", "P"):
                stats["trend_evaluated"] = int(stats.get("trend_evaluated", 0)) + 1
                min_req = float(index_trend_min_bp) / 10000.0
                if cp == "C" and atr < min_req:
                    stats["trend_blocked"] = int(stats.get("trend_blocked", 0)) + 1
                    # Probe: calls blocked solely by insufficient index trend bp
                    try:
                        if cp == "C":
                            _write_probe({
                                "ts": ts,
                                "symbol": sym,
                                "cp": cp,
                                "reason": "trend_low_bp_call",
                                "pct": win_pct,
                                "ups": win_ups,
                                "required_ups": win_required_ups,
                                "m0": win_m0,
                                "mid": win_mid,
                                "trend_bp": float(atr) * 10000.0,
                            })
                    except Exception:  # noqa: BLE001
                        pass
                    return False, {}
                if cp == "P" and atr > -min_req:
                    stats["trend_blocked"] = int(stats.get("trend_blocked", 0)) + 1
                    return False, {}
            # Optional marginal-trend extra light EM for calls
            if bool(marginal_trend_extra_em_gate) and isinstance(cp, str) and cp == 'C':
                try:
                    min_req = float(index_trend_min_bp) / 10000.0
                    margin_bp = float(marginal_trend_window_bp) / 10000.0
                    if atr >= min_req and atr < (min_req + margin_bp):
                        thr_light = float(em_light_bp) / 10000.0
                        if abs(atr) < thr_light:
                            stats["marginal_trend_em_blocked"] = int(stats.get("marginal_trend_em_blocked", 0)) + 1
                            return False, {}
                except Exception:
                    pass
            if em_gate:
                stats["em_evaluated"] = int(stats.get("em_evaluated", 0)) + 1
                thr = (float(em_bp) * float(em_mult)) / 10000.0
                if abs(atr) < thr:
                    stats["em_blocked"] = int(stats.get("em_blocked", 0)) + 1
                    return False, {}
                else:
                    stats["em_passed"] = int(stats.get("em_passed", 0)) + 1
        else:
            # No way to compute trend/EM; if trend is required, block to avoid silent bypass
            if require_index_trend and isinstance(cp, str) and cp in ("C", "P"):
                stats["trend_evaluated"] = int(stats.get("trend_evaluated", 0)) + 1
                stats["trend_blocked"] = int(stats.get("trend_blocked", 0)) + 1
                return False, {}
        # VWAP alignment gate (after trend/EM, before VIX/indicator)
        if bool(require_vwap_alignment):
            try:
                _ensure_indicators_loaded(ts)
                if spx_ind_df is not None and not spx_ind_df.empty and isinstance(cp, str) and cp in ("C","P"):
                    import pandas as _pd
                    t = _pd.to_datetime(ts, unit="s", utc=True)
                    idx = spx_ind_df.index
                    pos = idx.searchsorted(t, side="right") - 1
                    if pos >= 0:
                        row = spx_ind_df.iloc[pos]
                        last_px = float(row.get("last") or 0.0)
                        vwap_px = float(row.get("vwap") or 0.0)
                        if last_px > 0 and vwap_px > 0:
                            # base alignment
                            if cp == 'C' and last_px < vwap_px:
                                stats["vwap_blocked"] = int(stats.get("vwap_blocked",0)) + 1
                                try:
                                    _write_probe({
                                        "ts": ts,
                                        "symbol": sym,
                                        "cp": cp,
                                        "reason": "vwap_block_call",
                                        "pct": win_pct,
                                        "ups": win_ups,
                                        "required_ups": win_required_ups,
                                        "m0": win_m0,
                                        "mid": win_mid,
                                        "spx_last": last_px,
                                        "spx_vwap": vwap_px,
                                    })
                                except Exception:  # noqa: BLE001
                                    pass
                                return False, {}
                            if cp == 'P' and last_px > vwap_px:
                                stats["vwap_blocked"] = int(stats.get("vwap_blocked",0)) + 1
                                return False, {}
                            # margin requirement in bp beyond alignment
                            if float(vwap_min_margin_bp) > 0.0:
                                try:
                                    rel = 0.0
                                    if cp == 'C':
                                        rel = (last_px - vwap_px) / vwap_px
                                        if rel < (float(vwap_min_margin_bp)/10000.0):
                                            stats["vwap_margin_blocked"] = int(stats.get("vwap_margin_blocked",0)) + 1
                                            try:
                                                _write_probe({
                                                    "ts": ts,
                                                    "symbol": sym,
                                                    "cp": cp,
                                                    "reason": "vwap_margin_block_call",
                                                    "pct": win_pct,
                                                    "ups": win_ups,
                                                    "required_ups": win_required_ups,
                                                    "m0": win_m0,
                                                    "mid": win_mid,
                                                    "spx_last": last_px,
                                                    "spx_vwap": vwap_px,
                                                    "vwap_margin_bp": float(vwap_min_margin_bp)
                                                })
                                            except Exception:
                                                pass
                                            return False, {}
                                    elif cp == 'P':
                                        rel = (vwap_px - last_px) / vwap_px
                                        if rel < (float(vwap_min_margin_bp)/10000.0):
                                            stats["vwap_margin_blocked"] = int(stats.get("vwap_margin_blocked",0)) + 1
                                            return False, {}
                                except Exception:
                                    pass
            except Exception:
                pass
        # Additional optional VIX + indicator gates for non-greeks path
        stats["vix_evaluated"] = int(stats.get("vix_evaluated", 0)) + 1
        if not _vix_ok(ts, cp):
            stats["vix_blocked"] = int(stats.get("vix_blocked", 0)) + 1
            # Probe: calls blocked by VIX misalignment
            try:
                if cp and cp.upper() == 'C':
                    _write_probe({
                        "ts": ts,
                        "symbol": sym,
                        "cp": cp,
                        "reason": "vix_block_call",
                        "pct": win_pct,
                        "ups": win_ups,
                        "required_ups": win_required_ups,
                        "m0": win_m0,
                        "mid": win_mid,
                    })
            except Exception:  # noqa: BLE001
                pass
            return False, {}
        stats["ind_evaluated"] = int(stats.get("ind_evaluated", 0)) + 1
        if not _indicators_ok(ts, cp):
            stats["ind_blocked"] = int(stats.get("ind_blocked", 0)) + 1
            # Probe: calls blocked by indicator bias
            try:
                if cp and cp.upper() == 'C':
                    _write_probe({
                        "ts": ts,
                        "symbol": sym,
                        "cp": cp,
                        "reason": "indicator_block_call",
                        "pct": win_pct,
                        "ups": win_ups,
                        "required_ups": win_required_ups,
                        "m0": win_m0,
                        "mid": win_mid,
                    })
            except Exception:  # noqa: BLE001
                pass
            return False, {}
        la = last_alert.get(sym, 0.0)
        if ts - la < float(debounce_sec):
            return False, {}
        last_alert[sym] = ts
        return True, {}

    # Iterate in time order across all symbols
    # Build a merged timeline of (ts, sym, mid, meta)
    events: list[tuple[float, str, float, dict]] = []
    for sym, rows in by_sym.items():
        rows.sort(key=lambda x: x[0])
        for ts, mid, meta in rows:
            events.append((ts, sym, mid, meta))
    events.sort(key=lambda x: x[0])

    scanned = 0
    alerts_count = 0
    for ts, sym, mid, meta in events:
        scanned += 1
        # Optional liquidity/spread filters
        try:
            bid = float(meta.get("bid") or 0)
            ask = float(meta.get("ask") or 0)
        except Exception:
            bid = 0.0; ask = 0.0
        if bid and ask and ask > bid and mid > 0:
            spr = ask - bid
            if float(max_spread_abs) > 0 and spr > float(max_spread_abs):
                continue
            frac = spr / mid if mid > 0 else 0.0
            if float(max_spread_frac) > 0 and frac > float(max_spread_frac):
                continue
        cp = meta.get("cp") if isinstance(meta, dict) else None
        atm = meta.get("atm") if isinstance(meta, dict) else None
        try:
            atm = float(atm) if atm is not None else None
        except Exception:
            atm = None
        # Maintain volume series (only if present)
        vol: float | None
        _mv = meta.get("volume") if isinstance(meta, dict) else None
        try:
            vol = float(_mv) if _mv is not None else None
        except Exception:
            vol = None
        if vol is not None:
            vlist = vol_state.setdefault(sym, [])
            vlist.append((ts, vol))
        ok, g = _should_alert(sym, ts, mid, cp, atm, vol, meta.get("strike"))
        if ok:
            # Robust trend computation for sizing: prefer global ATM; fallback to indicator series; coerce NaN to 0
            tbp = _trend_bp_now(ts)
            size_reco = 1.0
            try:
                if bool(use_alert_size_reco):
                    if isinstance(cp, str) and cp.upper() == 'P':
                        # Be conservative on puts in both regimes: skip or reduce on positive trend; reduce even on negative/flat
                        if tbp > 0:
                            size_reco = float(put_pos_trend_size)
                        else:
                            size_reco = float(put_neg_trend_size)
                    if isinstance(cp, str) and cp.upper() == 'C' and tbp < 0:
                        size_reco = float(call_neg_trend_size)
            except Exception:
                size_reco = 1.0
            # Per-reason probe conversion: when a call alert fires, convert any pending call probes for this symbol
            try:
                if isinstance(cp, str) and cp.upper() == 'C':
                    pend = pending_probes_by_sym.get(sym) or []
                    if pend:
                        keep: list[dict] = []
                        for pr in pend:
                            try:
                                if (str(pr.get("cp")).upper() == 'C'):
                                    rr = pr.get("reason")
                                    if isinstance(rr, str):
                                        probe_reason_converted[rr] = int(probe_reason_converted.get(rr, 0)) + 1
                                    # drop converted probe from pending
                                    continue
                            except Exception:
                                pass
                            keep.append(pr)
                        pending_probes_by_sym[sym] = keep
            except Exception:
                pass
            payload = {
                "ts": _dt.fromtimestamp(ts, tz=_tz.utc).isoformat(),
                "symbol": sym,
                "short": meta.get("short"),
                "mid": mid,
                "bid": meta.get("bid"),
                "ask": meta.get("ask"),
                "strike": meta.get("strike"),
                "cp": meta.get("cp"),
                "atm": meta.get("atm"),
                "volume": meta.get("volume"),
                "scanned": scanned,
                "replay": True,
                "greeks_only": bool(greeks_only),
                "delta": g.get("delta") if g else None,
                "gamma": g.get("gamma") if g else None,
                "vega": g.get("vega") if g else None,
                "iv": g.get("iv") if g else None,
                "trend_bp": tbp,
                "size_reco": size_reco,
            }
            # Enrich with indicator snapshot values for downstream analysis
            try:
                ind = _indicator_snapshot(ts)
                if ind:
                    payload.update(ind)
            except Exception:
                pass
            _append_alert(payload)
            alerts_count += 1
    # Ensure file exists even if zero alerts (for downstream tooling)
    try:
        if not alerts_path.exists():
            alerts_path.write_text("", encoding="utf-8")
    except Exception:
        pass

    # Emit a small stats file beside alerts for later analysis
    try:
        stats_payload = {
            "ok": True,
            "ticks": sum(len(v) for v in by_sym.values()),
            "symbols": len(by_sym),
            "scanned": scanned,
            "alerts": alerts_count,
            "alerts_path": str(alerts_path),
            "require_index_trend": bool(require_index_trend),
            "index_trend_window_sec": float(index_trend_window_sec),
            "index_trend_min_bp": float(index_trend_min_bp),
            "require_vwap_alignment": bool(require_vwap_alignment),
            "em_gate": bool(em_gate),
            "em_bp": float(em_bp),
            "em_mult": float(em_mult),
            "em_evaluated": int(stats.get("em_evaluated", 0)),
            "em_blocked": int(stats.get("em_blocked", 0)),
            "em_passed": int(stats.get("em_passed", 0)),
            "trend_evaluated": int(stats.get("trend_evaluated", 0)),
            "trend_blocked": int(stats.get("trend_blocked", 0)),
            "trend_fallback_used": int(stats.get("trend_fallback_used", 0)),
            # New diagnostics
            "vix_evaluated": int(stats.get("vix_evaluated", 0)),
            "vix_blocked": int(stats.get("vix_blocked", 0)),
            "ind_evaluated": int(stats.get("ind_evaluated", 0)),
            "ind_blocked": int(stats.get("ind_blocked", 0)),
            "vwap_blocked": int(stats.get("vwap_blocked", 0)),
            "vwap_margin_blocked": int(stats.get("vwap_margin_blocked", 0)),
            "greeks_evaluated": int(stats.get("greeks_evaluated", 0)),
            "greeks_blocked": int(stats.get("greeks_blocked", 0)),
            "probe_logged": int(stats.get("probe_logged", 0)),
            "rsi_vix_guard_blocked": int(stats.get("rsi_vix_guard_blocked", 0)),
            "marginal_trend_em_blocked": int(stats.get("marginal_trend_em_blocked", 0)),
            # Effective gate flags snapshot for debugging presets
            "use_vix_trend": bool(use_vix_trend),
            "use_indicator_bias": bool(use_indicator_bias),
            "require_sma_alignment": bool(require_sma_alignment),
            "use_global_index_trend": bool(use_global_index_trend),
            "greeks_only": bool(greeks_only),
            "delta_min": float(delta_min),
            "delta_max": float(delta_max),
            "max_spread_frac": float(max_spread_frac),
            "min_day_volume": float(min_day_volume),
            # Guardrail option snapshot
            "vwap_min_margin_bp": float(vwap_min_margin_bp),
            "marginal_trend_extra_em_gate": bool(marginal_trend_extra_em_gate),
            "marginal_trend_window_bp": float(marginal_trend_window_bp),
            "em_light_bp": float(em_light_bp),
            "rsi_bull_min_extra_if_vix_not_down": float(rsi_bull_min_extra_if_vix_not_down),
            "rsi_bull_min_relax_if_vix_down_strong": float(rsi_bull_min_relax_if_vix_down_strong),
            "vix_strong_down_min_bp": float(vix_strong_down_min_bp),
        }
        try:
            probes = int(stats.get("probe_logged", 0))
            stats_payload["probe_conversion_ratio"] = (alerts_count / probes) if probes > 0 else None
            # Emit per-reason probe stats and ratios
            if probe_reason_counts:
                stats_payload["probe_reason_counts"] = {k: int(v) for k, v in probe_reason_counts.items()}
            if probe_reason_converted:
                stats_payload["probe_reason_converted"] = {k: int(v) for k, v in probe_reason_converted.items()}
            if probe_reason_counts:
                ratios: dict[str, float] = {}
                for k, v in probe_reason_counts.items():
                    conv = int(probe_reason_converted.get(k, 0))
                    try:
                        ratios[k] = (conv / int(v)) if int(v) > 0 else None  # type: ignore[assignment]
                    except Exception:
                        ratios[k] = None  # type: ignore[assignment]
                stats_payload["probe_reason_conversion_ratio"] = ratios
        except Exception:  # noqa: BLE001
            pass
        stats_path = alerts_path.with_name(f"{alerts_path.stem}_stats.json")
        with stats_path.open("w", encoding="utf-8") as _sf:
            _sf.write(json.dumps(stats_payload, ensure_ascii=False, indent=2))
        typer.echo(json.dumps(stats_payload, indent=2))
    except Exception:
        typer.echo(json.dumps({
            "ok": True,
            "ticks": sum(len(v) for v in by_sym.values()),
            "symbols": len(by_sym),
            "scanned": scanned,
            "alerts": alerts_count,
            "alerts_path": str(alerts_path)
        }, indent=2))


@app.command(name="build_ticks_from_polygon")
def build_ticks_from_polygon(
    expiry: str = typer.Argument(..., help="Expiry date YYYY-MM-DD (SPXW weeklies)"),
    out_csv: Path = typer.Option(None, help="Output CSV path for ticks (defaults logs/rocket_ticks_<expiry>_poly.csv)"),
    timespan: str = typer.Option("minute", help="Aggregate timespan: minute|second"),
    mult: int = typer.Option(1, help="Timespan multiplier (1=1min, etc)"),
    near_pct: float = typer.Option(0.08, help="Near-ATM band percentage for inclusion"),
    otm_percent: float = typer.Option(0.12, help="Far OTM threshold as percent from ATM"),
    max_symbols: int = typer.Option(150, help="Max curated symbols to fetch for speed"),
    rth_only: bool = typer.Option(True, help="Restrict to 09:30-16:00 US/Eastern for the expiry date"),
    ensure_both_rights: bool = typer.Option(True, help="Ensure both calls and puts per strike are included in curated set"),
    source: str = typer.Option("aggs", help="Data source for ticks: aggs|quotes (quotes = accurate bid/ask mids)", case_sensitive=False),
    quotes_sample_ms: int = typer.Option(60_000, help="For quotes source, sample every N ms to reduce volume (e.g., 60_000=1 min)"),
    delta_filter_min: float = typer.Option(0.0, help="If > 0 and delta_filter_max > 0, include only rows with abs(delta) >= this"),
    delta_filter_max: float = typer.Option(0.0, help="If > 0, include only rows with abs(delta) <= this"),
    dynamic_strikes: bool = typer.Option(False, help="If true, build curated strikes dynamically around intraday ATM across the session"),
    dyn_steps: int = typer.Option(8, help="# of 5-point steps to include on each side of ATM when dynamic_strikes is enabled"),
    dyn_stride_min: int = typer.Option(5, help="Stride (minutes) for sampling index ATM when dynamic_strikes is enabled"),
):
    """Use Polygon historical aggregates to fabricate a ticks CSV (mid proxy) for a given SPXW expiry.

    This enables offline rocket_replay + PnL on past sessions using your Polygon historical data entitlement.
    """
    import csv as _csv
    import math as _math
    import pytz as _pytz
    import datetime as _dt
    import pandas as pd
    # Ensure Polygon is configured
    settings = Settings()
    if not settings.has_polygon:
        typer.echo(json.dumps({"ok": False, "error": "POLYGON_API_KEY not set"}, indent=2))
        raise typer.Exit(1)
    poly = PolygonClient(PolygonConfig.from_env())

    # Build curated list like rocket_watch (or dynamic ATM-based if requested)
    chain_syms: list[str] = []
    if not dynamic_strikes:
        chain = poly.fetch_index_options_contracts(underlying="SPX", expiration_date=expiry, limit=1000, max_pages=20) or {}
        chain_syms = chain.get("symbols") or []
        if not chain_syms:
            chain2 = poly.fetch_index_options_contracts(underlying="I:SPX", expiration_date=expiry, limit=1000, max_pages=20) or {}
            chain_syms = chain2.get("symbols") or []
    prev = poly.last_trade_spx(use_cache=False) or {}
    idx_close = None
    try:
        idx_close = float(((prev.get("results") or [{}])[0]).get("c") or 5000.0)
    except Exception:
        idx_close = 5000.0
    from .datafeeds.options_chain import parse_occ_symbol as _parse_occ
    curated: list[str] = []
    if dynamic_strikes:
        # Generate OCCs around intraday ATM per minute with stride
        try:
            import math as _math
            yy = int(expiry[2:4]); mm = int(expiry[5:7]); dd = int(expiry[8:10])
            ymd = f"{yy:02d}{mm:02d}{dd:02d}"
            idx_df2 = poly.fetch_index_intraday_aggs(ticker="I:SPX", timespan="minute", mult=1, lookback_days=3)
            if idx_df2 is not None and not idx_df2.empty:
                # filter to RTH window of the expiry date in ET
                try:
                    import pytz as _pytz
                    et = _pytz.timezone('America/New_York')
                    idx_df2["et"] = idx_df2["dt"].dt.tz_convert(et)
                    exp_y = int(expiry[0:4]); exp_m = int(expiry[5:7]); exp_d = int(expiry[8:10])
                    from datetime import time as _tcls, date as _date
                    exp_date = _date(exp_y, exp_m, exp_d)
                    start_t = _tcls(9,30); end_t = _tcls(16,0)
                    df_idx = idx_df2[(idx_df2["et"].dt.date == exp_date) & (idx_df2["et"].dt.time >= start_t) & (idx_df2["et"].dt.time <= end_t)].copy()
                    if df_idx.empty:
                        df_idx = idx_df2.copy()
                except Exception:
                    df_idx = idx_df2.copy()
                if not df_idx.empty:
                    df_idx = df_idx.sort_values("dt").iloc[::max(1, int(dyn_stride_min))]
                    for _, r in df_idx.iterrows():
                        try:
                            s = float(r.get("close", 0) or 0)
                        except Exception:
                            continue
                        if s <= 0:
                            continue
                        base = int(_math.floor(s / 5.0) * 5)
                        for step in range(-int(dyn_steps), int(dyn_steps) + 1):
                            k_int = max(5, base + 5 * step)
                            occ_c = f"SPXW{ymd}C{int(k_int)*1000:08d}"
                            occ_p = f"SPXW{ymd}P{int(k_int)*1000:08d}"
                            curated.append(occ_c)
                            curated.append(occ_p)
        except Exception:
            curated = []
        curated = list(dict.fromkeys(curated))
    else:
        def _norm(sym: str) -> str:
            return sym[2:] if isinstance(sym, str) and sym.startswith("O:") else sym
        parsed = [_parse_occ(_norm(s)) for s in chain_syms]
        parsed = [c for c in parsed if c]
        for c in parsed:
            k = float(c.strike); cp = c.call_put
            m = (k - idx_close) / idx_close
            if abs(m) <= near_pct or (cp == 'C' and m >= otm_percent) or (cp == 'P' and -m >= otm_percent):
                curated.append(c.symbol)
    # Fallback to report if no curated found
    if not curated:
        try:
            rep = Path(f"logs/spxw_report_{expiry}.json")
            if rep.exists():
                j = json.loads(rep.read_text(encoding="utf-8"))
                sample = j.get("rockets_sample") or []
                if isinstance(sample, list):
                    curated.extend([s for s in sample if isinstance(s, str)])
        except Exception:
            pass
    # Synthetic OCC fallback if still empty
    if not curated:
        yy = int(expiry[2:4]); mm = int(expiry[5:7]); dd = int(expiry[8:10])
        ymd = f"{yy:02d}{mm:02d}{dd:02d}"
        def _round_step(x: float, step: int = 5, up: bool = False) -> int:
            return int((_math.ceil if up else _math.floor)(x / step) * step)
        near_lo = _round_step(idx_close * (1 - near_pct))
        near_hi = _round_step(idx_close * (1 + near_pct), up=True)
        call_lo = _round_step(idx_close * (1 + otm_percent), up=True)
        call_hi = _round_step(idx_close * 1.30, up=True)
        put_hi = _round_step(idx_close * (1 - otm_percent))
        put_lo = _round_step(max(5.0, idx_close * 0.75))
        gen: list[str] = []
        for k in range(near_lo, near_hi + 1, 5):
            gen.append(f"SPXW{ymd}C{int(k)*1000:08d}")
            gen.append(f"SPXW{ymd}P{int(k)*1000:08d}")
        for k in range(call_lo, call_hi + 1, 5):
            gen.append(f"SPXW{ymd}C{int(k)*1000:08d}")
        for k in range(put_hi, put_lo - 1, -5):
            gen.append(f"SPXW{ymd}P{int(k)*1000:08d}")
        curated = list(dict.fromkeys(gen))
    # Optional: enforce both rights for each curated strike to avoid call-only datasets
    if ensure_both_rights and curated:
        try:
            enriched: set[str] = set(curated)
            for occ in list(curated):
                try:
                    c = _parse_occ(occ)
                except Exception:
                    c = None
                if not c:
                    continue
                k = int(getattr(c, "strike", 0) or 0)
                right = getattr(c, "call_put", None)
                if right not in ("C", "P"):
                    continue
                # build counterpart symbol at same strike
                other = "P" if right == "C" else "C"
                yy = int(expiry[2:4]); mm = int(expiry[5:7]); dd = int(expiry[8:10])
                ymd = f"{yy:02d}{mm:02d}{dd:02d}"
                alt = f"SPXW{ymd}{other}{k*1000:08d}"
                enriched.add(alt)
            curated = list(enriched)
        except Exception:
            # Best-effort; ignore if parsing fails
            pass
    curated = curated[:max_symbols]

    # Define day window in Eastern to UTC
    if rth_only:
        tz = _pytz.timezone("US/Eastern")
        dt0 = tz.localize(_dt.datetime.strptime(expiry + " 09:30:00", "%Y-%m-%d %H:%M:%S"))
        dt1 = tz.localize(_dt.datetime.strptime(expiry + " 16:00:00", "%Y-%m-%d %H:%M:%S"))
        start_utc = dt0.astimezone(_pytz.UTC)
        end_utc = dt1.astimezone(_pytz.UTC)
    else:
        dt0 = _dt.datetime.strptime(expiry + " 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_pytz.UTC)
        dt1 = _dt.datetime.strptime(expiry + " 23:59:59", "%Y-%m-%d %H:%M:%S").replace(tzinfo=_pytz.UTC)
        start_utc = dt0; end_utc = dt1

    # Output path
    if out_csv is None:
        out_csv = Path(f"logs/rocket_ticks_{expiry}_poly.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    wrote = 0; symbols = 0
    # Diagnostics for quotes+delta filtering
    diag_total_eval = 0
    diag_total_pass = 0
    diag_delta_applied = float(delta_filter_max) > 0.0 and float(delta_filter_min) >= 0.0
    diag_greeks_fail = 0
    diag_solver_fallback = 0
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        # include optional volume column at the end for future gating (may be 0 if unavailable)
        w.writerow(["ts","symbol","short","bid","ask","mid","strike","cp","atm","volume"])
        # Build an ATM timeline from Polygon index intraday minute bars for more accurate greeks
        idx_map: dict[int, float] = {}
        try:
            idx_df = poly.fetch_index_intraday_aggs(ticker="I:SPX", timespan="minute", mult=1, lookback_days=3)
            if idx_df is not None and not idx_df.empty:
                # Filter to our window [start_utc, end_utc]
                try:
                    idx_mask = (idx_df["dt"] >= pd.Timestamp(start_utc)) & (idx_df["dt"] <= pd.Timestamp(end_utc))
                    idx_day = idx_df.loc[idx_mask].copy()
                except Exception:
                    idx_day = idx_df.copy()
                if not idx_day.empty:
                    # Bucket to minute and map to close
                    try:
                        # ns per minute
                        ns_per_min = 60_000_000_000
                        buckets = (idx_day["dt"].astype("int64") // ns_per_min).astype("int64")
                        idx_day = idx_day.assign(bucket=buckets)
                        # keep last close per minute bucket
                        gb = idx_day.groupby("bucket")
                        last_close = gb["close"].last()
                        idx_map = {int(k): float(v) for k, v in last_close.items() if pd.notna(v) and isinstance(k, (int, str)) and str(k).isdigit()}
                    except Exception:
                        idx_map = {}
        except Exception:
            idx_map = {}
        # Helper: time-to-expiry in days for a given UTC timestamp
        import datetime as __dt
        def _tte_days(ts: float) -> float:
            try:
                from zoneinfo import ZoneInfo as _ZI
                y = int(expiry[0:4]); m = int(expiry[5:7]); d = int(expiry[8:10])
                et = _ZI('America/New_York')
                exp_local = __dt.datetime(y, m, d, 16, 0, 0, tzinfo=et)
                exp_utc = exp_local.astimezone(_ZI('UTC')).timestamp()
                rem = max(0.0, exp_utc - ts)
                return rem / 86400.0
            except Exception:
                return 0.0

        for sym in curated:
            symbols += 1
            occ = sym
            tkr = f"O:{occ}"
            c = _parse_occ(occ)
            k = float(getattr(c, "strike", 0)) if c else 0.0
            cp = getattr(c, "call_put", None) if c else None
            if (source or "aggs").lower() == "quotes":
                # Accurate mids from historical quotes; sample down to avoid huge files
                try:
                    dfq = poly.fetch_option_quotes(tkr, start=start_utc, end=end_utc, limit=50000, sort="asc")
                except Exception:
                    dfq = None
                if dfq is None or dfq.empty:
                    continue
                # Filter to window and resample by quotes_sample_ms using first tick each bucket
                try:
                    dfq["dt"] = pd.to_datetime(dfq["timestamp"], unit="s", utc=True)
                    mask = (dfq["dt"] >= pd.Timestamp(start_utc)) & (dfq["dt"] <= pd.Timestamp(end_utc))
                    dfx = dfq.loc[mask].copy()
                except Exception:
                    dfx = dfq.copy()
                if dfx.empty:
                    continue
                # Sort and bucket by floor(dt to N ms)
                dfx = dfx.sort_values("dt")
                # floor to nearest quotes_sample_ms
                try:
                    grp = (dfx["dt"].astype("int64") // (quotes_sample_ms * 1_000_000)).astype("int64")
                    dfx = dfx.assign(bucket=grp)
                    # Take first record per bucket
                    dfx = dfx.groupby("bucket", as_index=False).first()
                except Exception:
                    # Fallback: take every Nth row
                    step = max(1, int(len(dfx) / max(1, int(len(dfx) // 1000))))
                    dfx = dfx.iloc[::step]
                for _, row in dfx.iterrows():
                    try:
                        ts = float(pd.Timestamp(row["dt"]).timestamp())
                    except Exception:
                        try:
                            ts = float(row.get("timestamp", 0))
                        except Exception:
                            continue
                    # Resolve ATM from map (minute bucket); fallback to previous idx_close if missing
                    try:
                        bucket_min = int(pd.Timestamp(row["dt"]).value // 60_000_000_000)
                        atm_val = float(idx_map.get(bucket_min, idx_close))
                    except Exception:
                        atm_val = float(idx_close)
                    bid = float(row.get("bid", 0) or 0)
                    ask = float(row.get("ask", 0) or 0)
                    if bid <= 0 and ask <= 0:
                        continue
                    mid = (bid + ask) / 2.0 if bid and ask else (bid or ask)
                    # Optional greeks delta filter
                    if diag_delta_applied:
                        diag_total_eval += 1
                        t_days = _tte_days(ts)
                        call_flag = (str(cp).upper() == 'C') if cp is not None else True
                        # Use quoted option price units directly (per-point), BS operates in same units as spot/strike
                        price_unit = float(mid)
                        # Compute robust delta
                        try:
                            om = compute_option_metrics(spot=float(atm_val), strike=float(k), t_days=float(t_days), mid_price=float(price_unit), call=call_flag, rate=0.0)
                            d_val = float(om.delta) if om.delta is not None else None
                            iv_val = float(om.iv) if om.iv is not None else None
                        except Exception:
                            d_val = None; iv_val = None
                        if d_val is None:
                            # Fallback: implied vol + BS delta
                            try:
                                T = max(0.0, float(t_days) / 365.0)
                                right = 'C' if call_flag else 'P'
                                sig = implied_vol_newton(target_price=float(price_unit), S=float(atm_val), K=float(k), r=0.0, T=T, right=right, initial=0.4)
                                diag_solver_fallback += 1
                                if not (sig and sig > 0.0) or math.isnan(sig) or math.isinf(sig):
                                    sig = 0.6
                                d_val = bs_delta(float(atm_val), float(k), 0.0, float(sig), T, right)
                            except Exception:
                                d_val = None
                        if d_val is None:
                            diag_greeks_fail += 1
                            continue
                        a_delta = abs(float(d_val))
                        if a_delta < float(delta_filter_min) or a_delta > float(delta_filter_max):
                            continue
                        diag_total_pass += 1
                    w.writerow([f"{ts:.3f}", occ, "", f"{bid:.6f}", f"{ask:.6f}", f"{mid:.6f}", f"{k:.0f}", cp or "", f"{atm_val}", "0"])
                    wrote += 1
            else:
                # Aggregates path (close as mid proxy)
                df = poly.fetch_option_aggs(tkr, start=start_utc, end=end_utc, timespan=timespan, mult=int(mult))
                if df is None or df.empty:
                    continue
                # Filter exact window (ms timestamps)
                try:
                    ts_series = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                    mask = (ts_series >= pd.Timestamp(start_utc)) & (ts_series <= pd.Timestamp(end_utc))
                    df2 = df.loc[mask].copy()
                except Exception:
                    df2 = df.copy()
                if df2.empty:
                    continue
                for _, row in df2.iterrows():
                    try:
                        ts = float(pd.Timestamp(row["timestamp"], unit="ms").timestamp())
                    except Exception:
                        # attempt if already numeric seconds
                        try:
                            ts = float(row["timestamp"]) / (1000.0 if float(row["timestamp"]) > 1e12 else 1.0)
                        except Exception:
                            continue
                    close = float(row.get("close", 0) or 0)
                    if close <= 0:
                        continue
                    vol = 0
                    try:
                        vol = int(row.get("volume") or row.get("v") or 0)
                    except Exception:
                        vol = 0
                    w.writerow([f"{ts:.3f}", occ, "", "0.0", "0.0", f"{close:.6f}", f"{k:.0f}", cp or "", f"{idx_close}", f"{vol}"])
                    wrote += 1
    typer.echo(json.dumps({
        "ok": True,
        "symbols": symbols,
        "rows": wrote,
        "out_csv": str(out_csv),
        "delta_filter_applied": bool(diag_delta_applied),
        "rows_evaluated_for_delta": int(diag_total_eval),
        "rows_passed_delta": int(diag_total_pass),
        "greeks_failures": int(diag_greeks_fail),
        "solver_fallbacks": int(diag_solver_fallback),
    }, indent=2))


@app.command(name="agent_intraday_decision")
def agent_intraday_decision(
    expiry: str = typer.Argument(..., help="Expiry date YYYY-MM-DD (SPXW weeklies)"),
    from_report: Path = typer.Option(None, help="Path to a pre-open report JSON to seed features (e.g., logs/spxw_report_<date>.json)"),
    out_json: Path = typer.Option(None, help="Output path for agent decision JSON (default logs/agent_decision_<expiry>.json)"),
    otm_percent: float = typer.Option(0.12, help="OTM percent used to translate bias into contract strike"),
    target_delta: float = typer.Option(0.25, help="Target absolute delta used in greeks factor computation"),
    tol: float = typer.Option(0.05, help="Delta tolerance for greeks factor buckets"),
    use_polygon: bool = typer.Option(True, help="If set, use Polygon for SPX last trade to anchor ATM estimate"),
):
    """Produce neutral agent commentary plus a deterministic option selection derived from greeks and activity.

    This does NOT provide trading advice. It computes a simple bias from greeks_norm and activity density,
    then translates that bias into a hypothetical OCC symbol (for backtests/what-ifs).
    """
    settings = Settings()
    # Seed features from report if provided
    base: dict[str, object] = {}
    if from_report and from_report.exists():
        try:
            base = json.loads(from_report.read_text(encoding="utf-8"))  # type: ignore[assignment]
        except Exception:
            base = {}
    # Anchor ATM from Polygon if requested
    idx_close = None
    if use_polygon and settings.has_polygon:
        try:
            poly_client = PolygonClient(PolygonConfig.from_env())
            prev = poly_client.last_trade_spx(use_cache=False)
            if isinstance(prev, dict) and prev.get("results"):
                idx_close = prev["results"][0].get("c")
        except Exception:
            idx_close = None
        if idx_close is None:
            try:
                _idx = base.get("idx_close_guess") if isinstance(base, dict) else None
                if _idx is not None and isinstance(_idx, (int, float, str)):
                    idx_close = float(_idx)
                else:
                    idx_close = 5000.0
            except Exception:
                idx_close = 5000.0
        # Attempt live greeks snapshot (best-effort)
        greeks_norm = 0.0
        try:
            from .datafeeds.iqfeed_options import IQFeedOptionsGreeks
            from .analytics.greeks_factor import compute_greeks_norm
            og = IQFeedOptionsGreeks()
            if idx_close is not None:
                chain = og.fetch_chain_greeks(float(idx_close)) or []
                res = compute_greeks_norm(chain, mode="composite", target_abs_delta=float(target_delta), tol=float(tol), scale=1.0)
                greeks_norm = float(res.get("greeks_norm", 0.0))
            else:
                greeks_norm = 0.0
        except Exception:
            greeks_norm = 0.0
        # Activity density proxy from report
        try:
            _cc = base.get("curated_count") if isinstance(base, dict) else 0
            curated_count = int(_cc) if _cc is not None else 0
        except Exception:
            curated_count = 0
        try:
            _rc = base.get("rockets_count") if isinstance(base, dict) else 0
            rockets_count = int(_rc) if _rc is not None else 0
        except Exception:
            rockets_count = 0
        density = (rockets_count / max(1, curated_count)) if curated_count else 0.0

    # Deterministic bias (no advisory language): combine greeks_norm and density
    bias = max(-1.0, min(1.0, (0.7 * greeks_norm) + (0.6 * density)))

    # Translate bias into a hypothetical OCC symbol near otm_percent
    from .datafeeds.options_chain import parse_occ_symbol as _parse_occ  # reuse helpers
    # Build OCC
    yy = int(expiry[2:4]); mm = int(expiry[5:7]); dd = int(expiry[8:10])
    ymd = f"{yy:02d}{mm:02d}{dd:02d}"
    strike = float(idx_close) * (1.0 + float(otm_percent) if bias >= 0.2 else (1.0 - float(otm_percent)))
    # round to 5
    import math as _math
    k_int = int(_math.floor(strike / 5.0) * 5)
    right = "C" if bias >= 0.2 else "P"
    occ = f"SPXW{ymd}{right}{k_int*1000:08d}"

    # Agent commentary
    commentary = None
    try:
        from .openai_client import OpenAIWrapper
        wrapper = OpenAIWrapper(api_key=settings.openai_api_key)
        features = {
            "expiry": expiry,
            "idx_close": round(float(idx_close), 2),
            "curated_count": curated_count,
            "rockets_count": rockets_count,
            "density": round(float(density), 4),
            "greeks_norm": round(float(greeks_norm), 4),
            "bias": round(float(bias), 4),
        }
        commentary = wrapper.summarize_market(features)
    except Exception:
        commentary = None

    out = {
        "ok": True,
        "expiry": expiry,
        "idx_close": float(idx_close),
        "features": {
            "curated_count": curated_count,
            "rockets_count": rockets_count,
            "density": float(density),
            "greeks_norm": float(greeks_norm),
            "bias": float(bias),
        },
        "selection": {
            "occ": occ,
            "right": right,
            "strike": k_int,
            "otm_percent": float(otm_percent),
        },
        "agent_commentary": commentary or "",
    }
    if out_json is None:
        out_json = Path(f"logs/agent_decision_{expiry}.json")
    try:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(out, indent=2), encoding="utf-8")
    except Exception:
        pass
    typer.echo(json.dumps({k: out[k] for k in ("ok", "expiry")} | {"occ": out["selection"]["occ"], "bias": out["features"]["bias"]}, indent=2))


@app.command(name="alerts_filter_size")
def alerts_filter_size(
    expiry: str = typer.Option(None, help="Expiry (YYYY-MM-DD). Used to derive default paths when alerts/ticks not provided."),
    alerts_path: Path = typer.Option(None, help="Input rocket alerts JSONL (defaults logs/rocket_alerts_<expiry>.jsonl)"),
    ticks_path: Path = typer.Option(None, help="Ticks CSV from rocket_watch (defaults logs/rocket_ticks_<expiry>.csv)"),
    out_path: Path = typer.Option(None, help="Output filtered alerts JSONL (defaults logs/rocket_alerts_<expiry>_filtered.jsonl)"),
    lookback_sec: float = typer.Option(60.0, help="Seconds of ticks to use before each alert for scoring"),
    ret_cap: float = typer.Option(1.0, help="Cap for option return used in nowcast score normalization (1.0 = 100%)"),
    spread_cap: float = typer.Option(0.01, help="Cap for mean spread fraction used in microstructure score (1% by default)"),
    min_nowcast: float = typer.Option(0.2, help="Minimum nowcast score to keep an alert [0-1]"),
    min_micro: float = typer.Option(0.4, help="Minimum microstructure score to keep an alert [0-1]"),
    base_size_reco: float = typer.Option(1.0, help="Base size factor to scale by score; final size_reco is clamped"),
    min_size_reco: float = typer.Option(0.25, help="Minimum size_reco clamp"),
    max_size_reco: float = typer.Option(1.25, help="Maximum size_reco clamp"),
    nowcast_backend: str = typer.Option("window", help="Nowcast backend: window (current), ewma, chronos (if available), timesfm (placeholder)", case_sensitive=False),
):
    """Score, filter, and size alerts using recent ticks, writing a PnL-compatible JSONL.

    - Nowcast score: combines signed return and uptick ratio over lookback window.
    - Microstructure score: combines 1 - mean(spread/ mid) and availability of mid>0 ticks.
    - Output rows include original alert fields plus nowcast_score, micro_score, and size_reco.
    - Only kept alerts are written, suitable for feeding options_eod_pnl.
    """
    import datetime as _dt
    from datetime import timezone as _tz
    from pathlib import Path as _P

    # Resolve paths
    if alerts_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --alerts-path")
        alerts_path = Path(f"logs/rocket_alerts_{expiry}.jsonl")
    if ticks_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --ticks-path")
        ticks_path = Path(f"logs/rocket_ticks_{expiry}.csv")
    if out_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --out-path")
        out_path = Path(f"logs/rocket_alerts_{expiry}_filtered.jsonl")

    if not alerts_path.exists():
        typer.echo(json.dumps({"ok": False, "error": f"alerts_path not found: {alerts_path}"}, indent=2))
        raise typer.Exit(1)
    if not ticks_path.exists():
        typer.echo(json.dumps({"ok": False, "error": f"ticks_path not found: {ticks_path}"}, indent=2))
        raise typer.Exit(1)

    # Load alerts first
    alerts: list[dict] = []
    symbols: set[str] = set()
    min_needed_by_sym: dict[str, float] = {}
    max_alert_ts: float = 0.0
    try:
        with alerts_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sym = rec.get("symbol") or rec.get("occ")
                if not sym:
                    continue
                ts_str = rec.get("ts")
                try:
                    # Expect ISO8601 with timezone; fallback to naive UTC
                    ts = _dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() if isinstance(ts_str, str) else 0.0
                except Exception:
                    try:
                        ts = float(ts_str) if ts_str is not None else 0.0
                    except Exception:
                        ts = 0.0
                alerts.append(rec | {"_ts": ts})
                symbols.add(sym)
                max_alert_ts = max(max_alert_ts, ts)
                need = ts - float(lookback_sec)
                prev_need = min_needed_by_sym.get(sym)
                if prev_need is None or need < prev_need:
                    min_needed_by_sym[sym] = need
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": f"failed reading alerts: {exc}"}, indent=2))
        raise typer.Exit(1)

    if not alerts:
        typer.echo(json.dumps({"ok": True, "kept": 0, "out_path": str(out_path), "note": "no alerts"}, indent=2))
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("", encoding="utf-8")
        except Exception:
            pass
        return

    # Build ticks index for required window only
    ticks_by_sym: dict[str, list[tuple[float, float, float, float]]] = {s: [] for s in symbols}
    try:
        with ticks_path.open("r", encoding="utf-8") as f:
            header = f.readline()
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 9:
                    continue
                try:
                    ts = float(parts[0])
                except Exception:
                    continue
                sym = parts[1]
                if sym not in symbols:
                    continue
                # Only keep ticks in global window and per-symbol minimum need
                if ts < float(min_needed_by_sym.get(sym, ts)) or ts > max_alert_ts:
                    continue
                try:
                    bid = float(parts[3])
                    ask = float(parts[4])
                    mid = float(parts[5])
                except Exception:
                    bid = ask = mid = 0.0
                ticks_by_sym[sym].append((ts, mid, bid, ask))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": f"failed reading ticks: {exc}"}, indent=2))
        raise typer.Exit(1)

    def _clamp(v: float, lo: float, hi: float) -> float:
        return hi if v > hi else (lo if v < lo else v)

    kept = 0
    # Optional: if a different nowcast backend is requested, initialize it here
    from .utils.nowcast import TSNowcaster as _TSNowcaster  # lazy import
    _use_backend = (nowcast_backend or "window").lower()
    _nc = None
    if _use_backend in ("ewma", "chronos", "timesfm"):
        try:
            _nc = _TSNowcaster(mode=_use_backend)
        except Exception:
            _nc = None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as out_f:
            for rec in alerts:
                sym = rec.get("symbol") or rec.get("occ")
                cp = str(rec.get("cp") or "").upper() if rec.get("cp") is not None else None
                k = float(rec.get("strike") or 0.0)
                atm = float(rec.get("atm") or 0.0)
                ts = float(rec.get("_ts") or 0.0)
                window_start = ts - float(lookback_sec)
                seq = ticks_by_sym.get(sym) or []
                # window filter
                wseq = [t for t in seq if window_start <= t[0] <= ts]
                wseq.sort(key=lambda x: x[0])
                mids = [m for _, m, _, _ in wseq if m and m > 0]
                # Nowcast components
                if len(mids) >= 2:
                    if _nc is not None and _use_backend != "window":
                        # Use TSNowcaster on the window mids; convert to series
                        try:
                            _ser = pd.Series(mids)
                            _res = _nc.nowcast(_ser, horizon_min=int(max(1, lookback_sec // 60)))
                            # Map to 0..1 nowcast score favoring direction by CP
                            p_up = float(_res.get("p_up", 0.5))
                            if isinstance(cp, str) and cp.upper() == 'P':
                                # For puts, prefer down probability
                                nowcast_score = float(_clamp(1.0 - p_up, 0.0, 1.0))
                            else:
                                nowcast_score = float(_clamp(p_up, 0.0, 1.0))
                        except Exception:
                            # Fallback to window-based calc
                            ret = (mids[-1] - mids[0]) / max(1e-8, mids[0])
                            sret = -ret if (isinstance(cp, str) and cp.upper() == 'P') else ret
                            norm_ret = _clamp(sret / float(ret_cap), 0.0, 1.0)
                            upticks = sum(1 for i in range(1, len(mids)) if mids[i] > mids[i-1])
                            uptick_ratio = upticks / max(1, (len(mids)-1))
                            nowcast_score = 0.6 * norm_ret + 0.4 * uptick_ratio
                    else:
                        # Existing window-based calc (default)
                        ret = (mids[-1] - mids[0]) / max(1e-8, mids[0])
                        # calls want positive momentum, puts negative
                        sret = -ret if (isinstance(cp, str) and cp.upper() == 'P') else ret
                        norm_ret = _clamp(sret / float(ret_cap), 0.0, 1.0)
                        upticks = sum(1 for i in range(1, len(mids)) if mids[i] > mids[i-1])
                        uptick_ratio = upticks / max(1, (len(mids)-1))
                        nowcast_score = 0.6 * norm_ret + 0.4 * uptick_ratio
                else:
                    nowcast_score = 0.0
                # Microstructure components
                spreads = []
                avail = 0
                for _, m, b, a in wseq:
                    if m and m > 0:
                        avail += 1
                        if b > 0 and a > 0 and a >= b:
                            spr = (a - b) / max(1e-8, m)
                            spreads.append(spr)
                avail_ratio = (avail / len(wseq)) if wseq else 0.0
                mean_spread = (sum(spreads) / len(spreads)) if spreads else float('inf')
                if mean_spread != float('inf'):
                    micro_spread = 1.0 - _clamp(mean_spread / float(spread_cap), 0.0, 1.0)
                else:
                    micro_spread = 0.0
                micro_score = 0.7 * micro_spread + 0.3 * avail_ratio

                keep = (nowcast_score >= float(min_nowcast)) and (micro_score >= float(min_micro))
                if not keep:
                    continue

                # Size recommendation: scale base by conservative mix of scores
                blend = 0.5 * nowcast_score + 0.5 * micro_score
                sz = float(base_size_reco) * (0.5 + 0.5 * _clamp(blend, 0.0, 1.0))
                sz = _clamp(sz, float(min_size_reco), float(max_size_reco))

                # Compose output alert (preserve original fields)
                out_rec = dict(rec)
                out_rec.pop("_ts", None)
                out_rec["nowcast_score"] = round(nowcast_score, 6)
                out_rec["micro_score"] = round(micro_score, 6)
                out_rec["size_reco"] = float(f"{sz:.4f}")
                out_f.write(json.dumps(out_rec, ensure_ascii=False) + "\n")
                kept += 1
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": f"failed writing output: {exc}"}, indent=2))
        raise typer.Exit(1)

    typer.echo(json.dumps({"ok": True, "kept": kept, "out_path": str(out_path)}, indent=2))


@app.command(name="options_eod_pnl")
def options_eod_pnl(
    expiry: str = typer.Argument(..., help="Expiry date YYYY-MM-DD (matches rocket alerts stem)"),
    alerts_path: Path = typer.Option(None, help="Path to rocket alerts JSONL (defaults logs/rocket_alerts_<expiry>.jsonl)"),
    out_json: Path = typer.Option(None, help="Output JSON summary path (defaults logs/eod_pnl_<expiry>.json)"),
    out_csv: Path = typer.Option(None, help="Output CSV ledger path (defaults logs/eod_pnl_<expiry>.csv)"),
    price_source: str = typer.Option("mid", help="Exit price source preference: mid|last|close", case_sensitive=False),
    side: str = typer.Option("buy", help="Assumed position side at first alert: buy|sell", case_sensitive=False),
    size: int = typer.Option(1, help="# of contracts per symbol for hypothetical PnL"),
    multiplier: float = typer.Option(100.0, help="Contract multiplier (SPX index options=100)"),
    fallback_polygon: bool = typer.Option(True, help="If IQFeed exit quote missing, attempt Polygon snapshot fallback"),
    near_pct: float = typer.Option(0.08, help="Moneyness band for 'near' classification (abs((K-S)/S) <= near_pct)"),
    otm_percent: float = typer.Option(0.15, help="OTM threshold for rocket classification (calls: (K-S)/S>=otm; puts: (S-K)/S>=otm)"),
    otm_tiers: str = typer.Option("0.01,0.02,0.03", help="Comma list of OTM tier boundaries (e.g. '0.01,0.02,0.03') for deeper buckets"),
    exit_from_ticks: Path = typer.Option(None, help="Optional path to ticks CSV to use mid path for exit rules (offline backtest)"),
    exit_cutoff: str = typer.Option(None, help="Optional cutoff time (America/New_York) like '15:59:30' to pick the last tick at/before this time for exit; used with --exit-from-ticks"),
    exit_mode: str = typer.Option("last", help="Exit rule when using --exit-from-ticks: last|cutoff|ptsl|trailing", case_sensitive=False),
    cheap_price: float = typer.Option(0.5, help="Price threshold to classify entries as 'cheap' for PT/SL rules"),
    pt_cheap: float = typer.Option(1.0, help="Profit target for cheap entries (e.g., 1.0 = +100%)"),
    pt_std: float = typer.Option(0.5, help="Profit target for standard entries (e.g., 0.5 = +50%)"),
    sl_cheap: float = typer.Option(0.5, help="Stop loss for cheap entries (e.g., 0.5 = -50%)"),
    sl_std: float = typer.Option(0.35, help="Stop loss for standard entries (e.g., 0.35 = -35%)"),
    trail_dd_cheap: float = typer.Option(0.4, help="Trailing drawdown for cheap entries (fraction from max for longs; from min for shorts)"),
    trail_dd_std: float = typer.Option(0.25, help="Trailing drawdown for standard entries"),
    min_hold_sec: float = typer.Option(30.0, help="Minimum seconds to hold after entry before applying PT/SL/Trailing checks"),
    slippage_abs: float = typer.Option(0.05, help="Absolute slippage applied on both entry and exit ($ per contract). 0 disables."),
    slippage_bps: float = typer.Option(0.0, help="Percent slippage per side (e.g., 0.05 = 5%). Used only if slippage_abs is 0."),
    fees_per_contract: float = typer.Option(0.65, help="Per-contract commission/fee applied on open and close (each)."),
    min_entry: float = typer.Option(None, help="Optional minimum entry mid to include a trade"),
    max_entry: float = typer.Option(None, help="Optional maximum entry mid to include a trade"),
    include_hours: str = typer.Option("09:35-16:00", help="Optional US/Eastern time window 'HH:MM-HH:MM(;HH:MM-HH:MM)' to include entries only if entry_ts within"),
    allow_empty: bool = typer.Option(False, help="If true, write a zero-trade summary instead of exiting when no alerts/entries are found"),
    use_agent_exit_policy: bool = typer.Option(False, help="Enable agent exit policy with tick-based exits (dynamic PT/trail + 15:45 ET cutoff)"),
    features_jsonl: Path = typer.Option(None, help="Optional JSONL with per-symbol features for exit policy (fields: ts,symbol/occ,bp_60,vwap_dev,rsi_14,vix_d,confidence_now,confidence_at_entry,regime)")
):
    """Compute an end-of-day PnL summary for SPXW options based on rocket alerts.

    Assumptions:
    - Entry is the first alert mid price per symbol (one trade per symbol).
    - Exit is the close/last/mid at run time (post-close), preferring --price-source.
    - Side defaults to 'buy'; set --side sell to flip PnL sign.
    """
    import datetime as _dt
    from pathlib import Path as _P
    settings = Settings()

    # Best-effort import for agent self-awareness updates
    try:
        from src.agent.decision import update_self_awareness as _upd_self
    except Exception:  # noqa: BLE001
        _upd_self = None

    # Resolve defaults
    if alerts_path is None:
        alerts_path = Path(f"logs/rocket_alerts_{expiry}.jsonl")
    if out_json is None:
        out_json = Path(f"logs/eod_pnl_{expiry}.json")
    if out_csv is None:
        out_csv = Path(f"logs/eod_pnl_{expiry}.csv")

    psrc = (price_source or "mid").lower()
    pside = (side or "buy").lower()
    if psrc not in ("mid", "last", "close"):
        psrc = "mid"
    if pside not in ("buy", "sell"):
        pside = "buy"

    # Load first-alert entries per OCC symbol
    entries: dict[str, dict] = {}
    if not alerts_path.exists():
        if allow_empty:
            # Create zero-trade summary for missing alerts
            payload = {
                "ok": True,
                "expiry": expiry,
                "assumptions": {"entry": "first-alert-mid", "exit": (price_source or "mid"), "side": (side or "buy"), "size": size, "multiplier": multiplier},
                "entries": 0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "unresolved": 0,
                "gross_pnl": 0.0,
                "avg_pnl": 0.0,
                "win_rate": 0.0,
                "by_cp": {},
                "by_moneyness": {},
                "by_moneyness_tiers": {},
                "near_pct": near_pct,
                "otm_percent": otm_percent,
                "otm_tiers": [],
                "alerts_path": str(alerts_path),
                "out_csv": str(out_csv),
                "out_json": str(out_json),
            }
            try:
                out_json.parent.mkdir(parents=True, exist_ok=True)
                out_json.write_text(json.dumps({"summary": payload, "ledger": []}, indent=2), encoding="utf-8")
            except Exception:
                pass
            typer.echo(json.dumps(payload, indent=2))
            return
        else:
            typer.echo(json.dumps({"ok": False, "error": f"alerts_path not found: {alerts_path}"}, indent=2))
            raise typer.Exit(1)
    try:
        with alerts_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sym = rec.get("symbol") or rec.get("occ")
                if not sym:
                    continue
                # Keep earliest occurrence as entry
                if sym not in entries:
                    # Normalize fields
                    try:
                        entry_mid = float(rec.get("mid", 0) or 0)
                    except Exception:
                        entry_mid = 0.0
                    # Optional size recommendation from alert (e.g., 0.5 to halve puts in positive trend)
                    try:
                        _sr = rec.get("size_reco")
                        size_reco = float(_sr) if _sr is not None else 1.0
                    except Exception:
                        size_reco = 1.0
                    entries[sym] = {
                        "symbol": sym,
                        "short": rec.get("short"),
                        "cp": rec.get("cp"),
                        "strike": rec.get("strike"),
                        "atm": rec.get("atm"),
                        "entry_mid": entry_mid,
                        "entry_ts": rec.get("ts"),
                        "size_reco": size_reco,
                    }
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": f"failed reading alerts: {exc}"}, indent=2))
        raise typer.Exit(1)

    if not entries:
        if allow_empty:
            # Write zero-trade summary artifacts so range backtests can include the day
            payload = {
                "ok": True,
                "expiry": expiry,
                "assumptions": {"entry": "first-alert-mid", "exit": psrc, "side": pside, "size": size, "multiplier": multiplier},
                "entries": 0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "unresolved": 0,
                "gross_pnl": 0.0,
                "avg_pnl": 0.0,
                "win_rate": 0.0,
                "by_cp": {},
                "by_moneyness": {},
                "by_moneyness_tiers": {},
                "near_pct": near_pct,
                "otm_percent": otm_percent,
                "otm_tiers": [],
                "alerts_path": str(alerts_path),
                "out_csv": str(out_csv),
                "out_json": str(out_json),
            }
            try:
                out_json.parent.mkdir(parents=True, exist_ok=True)
                out_json.write_text(json.dumps({"summary": payload, "ledger": []}, indent=2), encoding="utf-8")
            except Exception:
                pass
            # Print summary
            typer.echo(json.dumps(payload, indent=2))
            return
        else:
            typer.echo(json.dumps({"ok": False, "error": "no entries found in alerts"}, indent=2))
            raise typer.Exit(1)

    # Optional exit prices sourced from ticks CSV (use last mid per symbol)
    ticks_exit: dict[str, tuple[float, float]] = {}
    ticks_series: dict[str, list[tuple[float, float]]] = {}
    cutoff_ts: float | None = None
    if exit_from_ticks is not None:
        try:
            import csv as _csv
            try:
                from zoneinfo import ZoneInfo as _ZI
                import datetime as __dt
                if exit_cutoff:
                    parts = exit_cutoff.split(":")
                    hh = int(parts[0]); mm = int(parts[1]); ss = int(parts[2]) if len(parts) > 2 else 0
                    dt_local = __dt.datetime(int(expiry[0:4]), int(expiry[5:7]), int(expiry[8:10]), hh, mm, ss, tzinfo=_ZI("America/New_York"))
                    cutoff_ts = dt_local.astimezone(_ZI("UTC")).timestamp()
            except Exception:
                cutoff_ts = None
            with exit_from_ticks.open("r", encoding="utf-8") as tf:
                rdr = _csv.DictReader(tf)
                tmp: dict[str, tuple[float, float]] = {}
                for row in rdr:
                    sym = row.get("symbol") or row.get("occ")
                    if not sym:
                        continue
                    try:
                        ts = float(row.get("ts") or 0)
                        mid = float(row.get("mid") or 0)
                    except Exception:
                        continue
                    # Build full series for PT/SL, and last<=cutoff for 'last'/'cutoff'
                    ticks_series.setdefault(sym, []).append((ts, mid))
                    if cutoff_ts is not None and ts > cutoff_ts:
                        # still keep in series, but don't consider for 'last' mode
                        pass
                    prev = tmp.get(sym)
                    if prev is None or ts > prev[0]:
                        tmp[sym] = (ts, mid)
                # For 'last', we may later restrict to <= cutoff_ts; for now store absolute last
                ticks_exit = {k: (ts, v) for k, (ts, v) in tmp.items() if v and v > 0}
                # Ensure series sorted
                for k in list(ticks_series.keys()):
                    ticks_series[k].sort(key=lambda x: x[0])
        except Exception:
            ticks_exit = {}

    # Optional features JSONL for agent exit policy
    features_by_sym = {}
    bp60_entry_by_sym: dict[str, float] = {}
    if features_jsonl is not None:
        try:
            import json as _json
            feats_tmp: dict[str, list[dict]] = {}
            with features_jsonl.open("r", encoding="utf-8") as ff:
                for line in ff:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = _json.loads(s)
                    except Exception:
                        continue
                    sym = rec.get("symbol") or rec.get("occ")
                    if not sym:
                        continue
                    try:
                        tsf = float(rec.get("ts") or rec.get("_ts") or 0)
                    except Exception:
                        tsf = 0.0
                    if tsf <= 0:
                        continue
                    rec["_ts"] = tsf
                    feats_tmp.setdefault(sym, []).append(rec)
            for k, lst in feats_tmp.items():
                lst.sort(key=lambda r: float(r.get("_ts") or 0.0))
                features_by_sym[k] = lst
        except Exception:
            features_by_sym = {}
    # Precompute bp_60 at/near entry if features provided
    if features_by_sym:
        try:
            import datetime as __dt
            for sym, info in entries.items():
                et_iso = info.get("entry_ts")
                try:
                    et_ts = __dt.datetime.fromisoformat(str(et_iso).replace('Z','+00:00')).timestamp() if et_iso else None
                except Exception:
                    et_ts = None
                seq = features_by_sym.get(sym) or []
                if seq:
                    pick = None
                    if et_ts is not None:
                        for rec in reversed(seq):
                            try:
                                t = float(rec.get("_ts") or 0)
                                if t <= et_ts:
                                    pick = rec; break
                            except Exception:
                                continue
                    pick = pick or seq[-1]
                    try:
                        v = pick.get("bp_60")
                        if v is not None:
                            bp60_entry_by_sym[sym] = float(v)
                    except Exception:
                        pass
        except Exception:
            pass

    # Prepare clients
    iq = None
    try:
        cfg = IQFeedConfig.from_env()
        iq = IQFeedClient(cfg)
    except Exception as _exc:  # noqa: BLE001
        iq = None
    poly_client = None
    if fallback_polygon and settings.has_polygon:
        try:
            poly_client = PolygonClient(PolygonConfig.from_env())
        except Exception:
            poly_client = None

    def _exit_price(sym_occ: str, short_sym: str | None, entry_mid: float, entry_ts_iso: str | None) -> tuple[float, dict]:
        meta: dict = {"source": None}
        # Helpers for agent policy
        def _is_cutoff_et_1545(t: float) -> bool:
            try:
                from zoneinfo import ZoneInfo as _ZI
                import datetime as __dt
                dt = __dt.datetime.fromtimestamp(float(t), tz=_ZI('America/New_York'))
                return (dt.hour, dt.minute) >= (15, 45)
            except Exception:
                return False
        def _nearest_features(sym: str, t: float) -> dict:
            seq = features_by_sym.get(sym) if features_by_sym else None
            if not seq:
                return {}
            # binary search for last <= t
            lo, hi = 0, len(seq) - 1
            best = None
            while lo <= hi:
                midx = (lo + hi) // 2
                cur = seq[midx]
                cts = float(cur.get("_ts") or 0)
                if cts <= t:
                    best = cur; lo = midx + 1
                else:
                    hi = midx - 1
            return best or {}
        def _log_override(pos: dict, res: dict) -> None:
            try:
                from .agent.exit_integration import log_policy_override as _lpo
                _lpo(pos, res)
            except Exception:
                pass
        # 0) Ticks CSV override (offline backtest)
        if ticks_series and sym_occ in ticks_series:
            # Exit modes using ticks series
            try:
                # parse entry ts
                et = None
                if entry_ts_iso:
                    try:
                        import datetime as __dt
                        et = __dt.datetime.fromisoformat(entry_ts_iso.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        et = None
                series = ticks_series.get(sym_occ, [])
                # filter to after entry
                if et is not None:
                    series2 = [(t, m) for (t, m) in series if t >= et]
                else:
                    series2 = series[:]
                # apply cutoff bound
                if cutoff_ts is not None:
                    series2 = [(t, m) for (t, m) in series2 if t <= cutoff_ts]
                if not series2:
                    # fallback to last<=cutoff or absolute last
                    if cutoff_ts is not None:
                        prior = [(t, m) for (t, m) in series if t <= cutoff_ts]
                        if prior:
                            t0, m0 = prior[-1]
                            return float(m0), {"source": "ticks_mid", "exit_ts": t0}
                    t0, m0 = series[-1]
                    return float(m0), {"source": "ticks_mid", "exit_ts": t0}
                mode = (exit_mode or "last").lower()
                if mode in ("last", "cutoff"):
                    t0, m0 = series2[-1]
                    return float(m0), {"source": "ticks_mid", "exit_ts": t0}
                if mode == "ptsl":
                    cheap = entry_mid < float(cheap_price)
                    base_pt = float(pt_cheap if cheap else pt_std)
                    base_sl = float(sl_cheap if cheap else sl_std)
                    buy_side = (pside == "buy")
                    if not use_agent_exit_policy:
                        if buy_side:
                            pt_px = entry_mid * (1.0 + base_pt)
                            sl_px = entry_mid * (1.0 - base_sl)
                            for t, m in series2:
                                if et is not None and (t - et) < float(min_hold_sec):
                                    continue
                                if m >= pt_px:
                                    return float(m), {"source": "ticks_pt", "exit_ts": t}
                                if m <= sl_px:
                                    return float(m), {"source": "ticks_sl", "exit_ts": t}
                        else:
                            pt_px = entry_mid * (1.0 - base_pt)
                            sl_px = entry_mid * (1.0 + base_sl)
                            for t, m in series2:
                                if et is not None and (t - et) < float(min_hold_sec):
                                    continue
                                if m <= pt_px:
                                    return float(m), {"source": "ticks_pt", "exit_ts": t}
                                if m >= sl_px:
                                    return float(m), {"source": "ticks_sl", "exit_ts": t}
                        t0, m0 = series2[-1]
                        return float(m0), {"source": "ticks_mid", "exit_ts": t0}
                    # Agent-aware dynamic PT/SL and optional trailing with strict 15:45 ET cutoff
                    cur_pt = base_pt
                    cur_sl = base_sl
                    cur_tr = 0.0  # off by default unless policy turns it on
                    overrides_used = 0
                    peak = entry_mid
                    trough = entry_mid
                    for t, m in series2:
                        if et is not None and (t - et) < float(min_hold_sec):
                            continue
                        if use_agent_exit_policy and _is_cutoff_et_1545(t):
                            # log cutoff exit
                            try:
                                pos_cut = {
                                    "symbol": sym_occ,
                                    "side": ("CALL" if str((entries.get(sym_occ) or {}).get("cp") or "C").upper().startswith("C") else "PUT"),
                                    "action": ("BUY" if pside == "buy" else "SELL"),
                                    "pt_std": cur_pt,
                                    "sl_std": cur_sl,
                                    "trail_std": cur_tr,
                                    "overrides_used": overrides_used,
                                }
                                _log_override(pos_cut, {
                                    "overrides_used_next": overrides_used,
                                    "exit_now": True,
                                    "new_pt": None,
                                    "new_trail": None,
                                    "reduce_fraction": None,
                                    "ops_applied": [{"op": "exit_now"}],
                                    "reason": "cutoff_1545",
                                })
                            except Exception:
                                pass
                            return float(m), {"source": "agent_exit_now", "exit_ts": t}
                        # Consult policy
                        feats = _nearest_features(sym_occ, t) if features_by_sym else {}
                        pos = {
                            "symbol": sym_occ,
                            "side": ("CALL" if str((entries.get(sym_occ) or {}).get("cp") or "C").upper().startswith("C") else "PUT"),
                            "action": ("BUY" if pside == "buy" else "SELL"),
                            "pt_std": cur_pt,
                            "sl_std": cur_sl,
                            "trail_std": cur_tr,
                            "overrides_used": overrides_used,
                            "bars_held": int(((t - et) / 60.0) if et is not None else 0),
                            "bp60_at_entry": float(bp60_entry_by_sym.get(sym_occ, feats.get("bp_60") or 0.0)),
                        }
                        if use_agent_exit_policy:
                            try:
                                from .agent.exit_policy import agent_exit_policy as _aep
                                plan = _aep(pos, {**feats, "now_ts": t})
                            except Exception:
                                plan = {"override": False}
                            if plan.get("override"):
                                changed = False
                                ops_val = plan.get("ops") if isinstance(plan, dict) else None
                                _ops = ops_val if isinstance(ops_val, list) else []
                                applied_ops = []
                                for op in _ops:
                                    k = op.get("op")
                                    if k == "exit_now":
                                        # log exit_now before returning
                                        try:
                                            _log_override(pos, {
                                                "overrides_used_next": overrides_used,
                                                "exit_now": True,
                                                "new_pt": None,
                                                "new_trail": None,
                                                "reduce_fraction": None,
                                                "ops_applied": [op],
                                                "reason": plan.get("reason"),
                                            })
                                        except Exception:
                                            pass
                                        return float(m), {"source": "agent_exit_now", "exit_ts": t}
                                    if k == "raise_pt":
                                        try:
                                            npt = float(op.get("pt_std"))
                                            if npt > cur_pt:
                                                cur_pt = npt; changed = True; applied_ops.append(op)
                                        except Exception:
                                            pass
                                    if k == "trail_on":
                                        try:
                                            ntr = float(op.get("trail_std"))
                                            if cur_tr == 0.0 or ntr < cur_tr:
                                                cur_tr = ntr; changed = True; applied_ops.append(op)
                                        except Exception:
                                            pass
                                if changed:
                                    # log applied override bundle
                                    try:
                                        _log_override(pos, {
                                            "overrides_used_next": min(2, overrides_used + 1),
                                            "exit_now": False,
                                            "new_pt": cur_pt if any(o.get("op") == "raise_pt" for o in applied_ops) else None,
                                            "new_trail": cur_tr if any(o.get("op") == "trail_on" for o in applied_ops) else None,
                                            "reduce_fraction": None,
                                            "ops_applied": applied_ops,
                                            "reason": plan.get("reason"),
                                        })
                                    except Exception:
                                        pass
                                    overrides_used = min(2, overrides_used + 1)
                        # Check PT/SL
                        if buy_side:
                            pt_px = entry_mid * (1.0 + cur_pt)
                            sl_px = entry_mid * (1.0 - cur_sl)
                            if m >= pt_px:
                                return float(m), {"source": "ticks_pt", "exit_ts": t}
                            if m <= sl_px:
                                return float(m), {"source": "ticks_sl", "exit_ts": t}
                            # optional trailing
                            if cur_tr > 0:
                                if m > peak:
                                    peak = m
                                if peak > 0 and (peak - m) / peak >= cur_tr:
                                    return float(m), {"source": "ticks_trail", "exit_ts": t}
                        else:
                            pt_px = entry_mid * (1.0 - cur_pt)
                            sl_px = entry_mid * (1.0 + cur_sl)
                            if m <= pt_px:
                                return float(m), {"source": "ticks_pt", "exit_ts": t}
                            if m >= sl_px:
                                return float(m), {"source": "ticks_sl", "exit_ts": t}
                            if cur_tr > 0:
                                if m < trough:
                                    trough = m
                                if trough > 0 and (m - trough) / trough >= cur_tr:
                                    return float(m), {"source": "ticks_trail", "exit_ts": t}
                    t0, m0 = series2[-1]
                    return float(m0), {"source": "ticks_mid", "exit_ts": t0}
                if mode == "trailing":
                    cheap = entry_mid < float(cheap_price)
                    dd = float(trail_dd_cheap if cheap else trail_dd_std)
                    buy_side = (pside == "buy")
                    peak = entry_mid; trough = entry_mid
                    if not use_agent_exit_policy:
                        for t, m in series2:
                            if et is not None and (t - et) < float(min_hold_sec):
                                continue
                            if buy_side:
                                if m > peak:
                                    peak = m
                                if peak > 0 and (peak - m) / peak >= dd:
                                    return float(m), {"source": "ticks_trail", "exit_ts": t}
                            else:
                                if m < trough:
                                    trough = m
                                if trough > 0 and (m - trough) / trough >= dd:
                                    return float(m), {"source": "ticks_trail", "exit_ts": t}
                        t0, m0 = series2[-1]
                        return float(m0), {"source": "ticks_mid", "exit_ts": t0}
                    # Agent-aware trailing (can tighten dd and cut at 15:45 ET)
                    cur_tr = dd
                    overrides_used = 0
                    for t, m in series2:
                        if et is not None and (t - et) < float(min_hold_sec):
                            continue
                        if use_agent_exit_policy and _is_cutoff_et_1545(t):
                            # log cutoff exit
                            try:
                                pos_cut = {
                                    "symbol": sym_occ,
                                    "side": ("CALL" if str((entries.get(sym_occ) or {}).get("cp") or "C").upper().startswith("C") else "PUT"),
                                    "action": ("BUY" if pside == "buy" else "SELL"),
                                    "pt_std": pt_std,
                                    "sl_std": sl_std,
                                    "trail_std": cur_tr,
                                    "overrides_used": overrides_used,
                                }
                                _log_override(pos_cut, {
                                    "overrides_used_next": overrides_used,
                                    "exit_now": True,
                                    "new_pt": None,
                                    "new_trail": None,
                                    "reduce_fraction": None,
                                    "ops_applied": [{"op": "exit_now"}],
                                    "reason": "cutoff_1545",
                                })
                            except Exception:
                                pass
                            return float(m), {"source": "agent_exit_now", "exit_ts": t}
                        feats = _nearest_features(sym_occ, t) if features_by_sym else {}
                        pos = {
                            "symbol": sym_occ,
                            "side": ("CALL" if str((entries.get(sym_occ) or {}).get("cp") or "C").upper().startswith("C") else "PUT"),
                            "action": ("BUY" if pside == "buy" else "SELL"),
                            "pt_std": pt_std,
                            "sl_std": sl_std,
                            "trail_std": cur_tr,
                            "overrides_used": overrides_used,
                            "bars_held": int(((t - et) / 60.0) if et is not None else 0),
                            "bp60_at_entry": float(bp60_entry_by_sym.get(sym_occ, feats.get("bp_60") or 0.0)),
                        }
                        try:
                            from .agent.exit_policy import agent_exit_policy as _aep
                            plan = _aep(pos, {**feats, "now_ts": t})
                        except Exception:
                            plan = {"override": False}
                        if plan.get("override"):
                            changed = False
                            ops_val2 = plan.get("ops") if isinstance(plan, dict) else None
                            _ops2 = ops_val2 if isinstance(ops_val2, list) else []
                            applied_ops2 = []
                            for op in _ops2:
                                if op.get("op") == "exit_now":
                                    try:
                                        _log_override(pos, {
                                            "overrides_used_next": overrides_used,
                                            "exit_now": True,
                                            "new_pt": None,
                                            "new_trail": None,
                                            "reduce_fraction": None,
                                            "ops_applied": [op],
                                            "reason": plan.get("reason"),
                                        })
                                    except Exception:
                                        pass
                                    return float(m), {"source": "agent_exit_now", "exit_ts": t}
                                if op.get("op") == "trail_on":
                                    try:
                                        ntr = float(op.get("trail_std"))
                                        if ntr < cur_tr:
                                            cur_tr = ntr; changed = True; applied_ops2.append(op)
                                    except Exception:
                                        pass
                            if changed:
                                try:
                                    _log_override(pos, {
                                        "overrides_used_next": min(2, overrides_used + 1),
                                        "exit_now": False,
                                        "new_pt": None,
                                        "new_trail": cur_tr,
                                        "reduce_fraction": None,
                                        "ops_applied": applied_ops2,
                                        "reason": plan.get("reason"),
                                    })
                                except Exception:
                                    pass
                                overrides_used = min(2, overrides_used + 1)
                        if buy_side:
                            if m > peak:
                                peak = m
                            if peak > 0 and (peak - m) / peak >= cur_tr:
                                return float(m), {"source": "ticks_trail", "exit_ts": t}
                        else:
                            if m < trough:
                                trough = m
                            if trough > 0 and (m - trough) / trough >= cur_tr:
                                return float(m), {"source": "ticks_trail", "exit_ts": t}
                    t0, m0 = series2[-1]
                    return float(m0), {"source": "ticks_mid", "exit_ts": t0}
            except Exception:
                # fall back to previous logic
                pass
        if ticks_exit and sym_occ in ticks_exit:
            ts_v, px_v = ticks_exit[sym_occ]
            # honor cutoff if provided
            if cutoff_ts is not None and ts_v > cutoff_ts:
                try:
                    prior = [x for x in ticks_series.get(sym_occ, []) if x[0] <= cutoff_ts]
                    if prior:
                        t0, m0 = prior[-1]
                        return float(m0), {"source": "ticks_mid", "exit_ts": t0}
                except Exception:
                    pass
            return float(px_v), {"source": "ticks_mid", "exit_ts": ts_v}
        # 1) IQFeed
        if iq and short_sym:
            try:
                q = iq.lookup_last(short_sym)
                # Normalize numeric fields
                def _flt(x):
                    try:
                        return float(x) if x not in (None, "", "N/A") else 0.0
                    except Exception:
                        return 0.0
                bid = _flt(q.get("bid")) if isinstance(q, dict) else 0.0
                ask = _flt(q.get("ask")) if isinstance(q, dict) else 0.0
                last = _flt(q.get("last_trade") or q.get("last")) if isinstance(q, dict) else 0.0
                close = _flt(q.get("close")) if isinstance(q, dict) else 0.0
                mid = (bid + ask) / 2.0 if bid and ask else (bid or ask or 0.0)
                price = 0.0
                if psrc == "mid":
                    price = mid or last or close
                elif psrc == "last":
                    price = last or mid or close
                else:  # close
                    price = close or last or mid
                if price and price > 0:
                    meta.update({"source": f"iqfeed_{psrc}", "bid": bid, "ask": ask, "last": last, "close": close})
                    return price, meta
            except Exception as _exc:
                meta["iqfeed_error"] = str(_exc)
        # 2) Polygon fallback
        if poly_client:
            try:
                snap = poly_client.fetch_option_snapshot(f"O:{sym_occ}") or {}
                r = snap.get("results", {}) if isinstance(snap, dict) else {}
                book = r.get("last_quote", {}) if isinstance(r, dict) else {}
                bid = float(book.get("bid", 0) or 0)
                ask = float(book.get("ask", 0) or 0)
                mid = (bid + ask) / 2.0 if bid and ask else (bid or ask or 0.0)
                last = float((r.get("last_trade", {}) or {}).get("price", 0) or 0)
                price = mid if psrc == "mid" else (last if psrc == "last" else mid)
                if price and price > 0:
                    meta.update({"source": f"polygon_{psrc}", "bid": bid, "ask": ask, "last": last})
                    return price, meta
            except Exception as _exc:
                meta["polygon_error"] = str(_exc)
        return 0.0, meta

    # Build ledger
    from csv import DictWriter as _DictWriter
    rows: list[dict] = []
    wins = 0; losses = 0
    gross = 0.0
    unresolved = 0
    # Preparse time window if provided
    def _in_hours(ts_iso: str | None) -> bool:
        if not include_hours:
            return True
        try:
            from zoneinfo import ZoneInfo as _ZI
            import datetime as __dt
            if not ts_iso:
                return True
            dt = __dt.datetime.fromisoformat(ts_iso.replace('Z','+00:00')).astimezone(_ZI('America/New_York'))
            # Allow multiple windows separated by ';'
            windows = [w.strip() for w in str(include_hours).split(';') if w.strip()]
            if not windows:
                return True
            for w in windows:
                parts = w.split('-')
                if len(parts) != 2:
                    continue
                h0, h1 = parts[0].strip(), parts[1].strip()
                t0 = __dt.datetime(dt.year, dt.month, dt.day, int(h0[0:2]), int(h0[3:5]), tzinfo=_ZI('America/New_York'))
                t1 = __dt.datetime(dt.year, dt.month, dt.day, int(h1[0:2]), int(h1[3:5]), tzinfo=_ZI('America/New_York'))
                if t0 <= dt <= t1:
                    return True
            return False
        except Exception:
            return True

    for sym, info in entries.items():
        entry = float(info.get("entry_mid") or 0.0)
        if entry <= 0:
            continue
        if min_entry is not None and entry < float(min_entry):
            continue
        if max_entry is not None and entry > float(max_entry):
            continue
        if not _in_hours(info.get("entry_ts")):
            continue
        short = info.get("short")
        px, meta = _exit_price(sym, short, entry, info.get("entry_ts"))
        if px <= 0:
            unresolved += 1
            continue
        ent = entry; ex = px
        if float(slippage_abs) > 0:
            s = float(slippage_abs)
            if pside == "buy": ent += s; ex -= s
            else: ent -= s; ex += s
        elif float(slippage_bps) > 0:
            sp = float(slippage_bps)
            if pside == "buy": ent *= (1.0 + sp); ex *= (1.0 - sp)
            else: ent *= (1.0 - sp); ex *= (1.0 + sp)
        pnl_per = (ex - ent) if pside == "buy" else (ent - ex)
        # Apply size recommendation if provided in alert
        try:
            local_size = float(size) * float(info.get("size_reco") or 1.0)
        except Exception:
            local_size = float(size)
        pnl = pnl_per * float(multiplier) * float(local_size)
        # Subtract fees on open and close
        try:
            pnl -= float(fees_per_contract) * float(local_size) * 2.0
        except Exception:
            pass
        gross += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        # Update agent self-awareness (underlying SPX) best-effort
        if _upd_self is not None:
            try:
                _upd_self("SPX", bool(pnl > 0), cp=str(info.get("cp") or "").upper()[:1] or None)
            except Exception:
                pass
        # moneyness snapshot (best-effort)
        atm = None
        try:
            _atm_raw = info.get("atm")
            if _atm_raw is not None:
                atm = float(_atm_raw)
        except Exception:
            atm = None
        moneyness = None
        try:
            _k_raw = info.get("strike")
            k = None
            if _k_raw is not None:
                k = float(_k_raw)
            if (atm is not None) and (k is not None):
                moneyness = (k - atm) / atm
        except Exception:
            moneyness = None
        rows.append({
            "symbol": sym,
            "short": short,
            "cp": info.get("cp"),
            "strike": info.get("strike"),
            "entry_mid": entry,
            "exit_price": px,
            "side": pside,
            "size": local_size,
            "multiplier": multiplier,
            "pnl": pnl,
            "pnl_per_contract": pnl_per * float(multiplier),
            "pnl_pct": ((px - entry) / entry) if entry else None,
            "entry_ts": info.get("entry_ts"),
            "exit_ts": (
                _dt.datetime.fromtimestamp(float(meta.get("exit_ts")), tz=_dt.timezone.utc).isoformat()
                if isinstance(meta.get("exit_ts"), (int, float)) else None
            ),
            "exit_source": meta.get("source"),
            "atm": atm,
            "moneyness": moneyness,
        })

    # Aggregate summary
    total = len(rows)
    avg_pnl = (gross / total) if total else 0.0
    win_rate = (wins / total) if total else 0.0
    # Stratified stats helpers
    def _agg_init():
        return {"trades": 0, "wins": 0, "losses": 0, "gross_pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0}
    def _agg_add(agg: dict, r: dict):
        agg["trades"] += 1
        pnl = float(r.get("pnl") or 0.0)
        agg["gross_pnl"] += pnl
        if pnl > 0:
            agg["wins"] += 1
        elif pnl < 0:
            agg["losses"] += 1
    def _agg_finalize(agg: dict):
        t = max(1, int(agg["trades"]))
        agg["avg_pnl"] = agg["gross_pnl"] / t
        agg["win_rate"] = (agg["wins"] / t)
        return agg

    by_cp: dict[str, dict] = {}
    by_moneyness: dict[str, dict] = {}
    # Parse OTM tiers
    tiers: list[float] = []
    if isinstance(otm_tiers, str) and otm_tiers.strip():
        for part in otm_tiers.replace(";", ",").split(','):
            p = part.strip()
            if not p:
                continue
            try:
                v = float(p)
                if v > 0:
                    tiers.append(v)
            except Exception:
                continue
    tiers = sorted(set(tiers)) or [float(otm_percent)]
    # Initialize side-aware tier buckets lazily
    by_moneyness_tiers: dict[str, dict] = {}
    for r in rows:
        cp = str(r.get("cp") or "UNK").upper()
        by_cp.setdefault(cp, _agg_init())
        _agg_add(by_cp[cp], r)
        # Buckets: near, rocket_calls, rocket_puts, other
        bucket = "other"
        m = r.get("moneyness")
        if isinstance(m, (int, float)) and r.get("atm"):
            if abs(m) <= float(near_pct):
                bucket = "near"
            else:
                if cp == 'C' and m >= float(otm_percent):
                    bucket = "rocket_calls"
                elif cp == 'P' and (-m) >= float(otm_percent):
                    bucket = "rocket_puts"
        by_moneyness.setdefault(bucket, _agg_init())
        _agg_add(by_moneyness[bucket], r)
        # Deeper OTM tier classification (side-aware)
        if isinstance(m, (int, float)) and r.get("atm"):
            # calls: use positive m; puts: use positive -m
            val = m if cp == 'C' else (-m if cp == 'P' else None)
            if isinstance(val, (int, float)) and val is not None and val >= tiers[0]:
                # Find tier index
                placed = False
                for i in range(len(tiers) - 1):
                    lo = tiers[i]; hi = tiers[i+1]
                    if val >= lo and val < hi:
                        key = f"{'call' if cp=='C' else 'put'}_otm_{int(lo*100)}_{int(hi*100)}"
                        by_moneyness_tiers.setdefault(key, _agg_init())
                        _agg_add(by_moneyness_tiers[key], r)
                        placed = True
                        break
                if not placed:
                    lo = tiers[-1]
                    key = f"{'call' if cp=='C' else 'put'}_otm_{int(lo*100)}_plus"
                    by_moneyness_tiers.setdefault(key, _agg_init())
                    _agg_add(by_moneyness_tiers[key], r)
    # finalize groups
    by_cp = {k: _agg_finalize(v) for k, v in by_cp.items()}
    by_moneyness = {k: _agg_finalize(v) for k, v in by_moneyness.items()}
    by_moneyness_tiers = {k: _agg_finalize(v) for k, v in by_moneyness_tiers.items()}

    payload = {
        "ok": True,
        "expiry": expiry,
        "assumptions": {"entry": "first-alert-mid", "exit": psrc, "side": pside, "size": size, "multiplier": multiplier},
        "entries": len(entries),
        "trades": total,
        "wins": wins,
        "losses": losses,
        "unresolved": unresolved,
        "gross_pnl": gross,
        "avg_pnl": avg_pnl,
        "win_rate": win_rate,
        "by_cp": by_cp,
        "by_moneyness": by_moneyness,
        "by_moneyness_tiers": by_moneyness_tiers,
        "near_pct": near_pct,
        "otm_percent": otm_percent,
        "otm_tiers": tiers,
        "alerts_path": str(alerts_path),
        "out_csv": str(out_csv),
        "out_json": str(out_json),
    }

    # Write outputs
    try:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps({"summary": payload, "ledger": rows[:200]}, indent=2), encoding="utf-8")
    except Exception:
        pass
    try:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        if rows:
            with out_csv.open("w", newline="", encoding="utf-8") as f:
                cols = [
                    "symbol","short","cp","strike","entry_mid","exit_price","side","size","multiplier","pnl","pnl_per_contract","pnl_pct","entry_ts","exit_ts","exit_source"
                ]
                w = _DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k) for k in cols})
    except Exception:
        pass

    typer.echo(json.dumps(payload, indent=2))


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
def polygon_last_spx(
    normalize: bool = typer.Option(False, help="Return normalized DataFrame-like JSON"),
    cache: bool = True,
    quiet: bool = typer.Option(False, help="Suppress Polygon warning logs for unauthorized/not available cases"),
):
    """Fetch previous SPX aggregate via Polygon (with retry + optional cache)."""
    settings = Settings()
    if not settings.has_polygon:
        typer.echo("Polygon API key missing")
        raise typer.Exit(1)
    if quiet:
        try:
            import logging as _logging
            _logging.getLogger("polygon").setLevel(_logging.ERROR)
        except Exception:
            pass
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


@app.command(name="polygon_prev")
def polygon_prev(
    symbol: str = typer.Argument("SPY", help="Ticker (e.g., SPY, I:SPX)"),
    normalize: bool = typer.Option(False, help="Normalize to records JSON"),
    quiet: bool = typer.Option(False, help="Suppress Polygon warning logs for unauthorized/not available cases"),
):
    """Fetch previous aggregate via Polygon for any ticker (helps test entitlements)."""
    settings = Settings()
    if not settings.has_polygon:
        typer.echo(json.dumps({"error": "Polygon API key missing"}, indent=2))
        raise typer.Exit(1)
    if quiet:
        try:
            import logging as _logging
            _logging.getLogger("polygon").setLevel(_logging.ERROR)
        except Exception:
            pass
    client = PolygonClient(PolygonConfig.from_env())
    data = client.last_trade_symbol(symbol)
    if not data:
        typer.echo(json.dumps({"symbol": symbol, "status": "no-data"}, indent=2))
        raise typer.Exit(1)
    if normalize:
        df = client.normalize_prev_agg(data)
        typer.echo(df.to_json(orient="records") if df is not None else "[]")
    else:
        typer.echo(json.dumps(data, indent=2))


@app.command(name="iqfeed_ping")
def iqfeed_ping():
    """Ping IQFeed Level1 socket."""
    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)
    ok = client.ping()
    typer.echo(json.dumps({"ok": ok}))

@app.command(name="iqfeed_last")
def iqfeed_last(symbol: str = typer.Argument(..., help="Symbol to snapshot via IQFeed Level1 (e.g., SPY, ^SPX, SPXW2501J2600)")):
    """Fetch a minimal last-quote snapshot for a symbol via IQFeed Level1 (watch/unwatch)."""
    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)
    res = client.lookup_last(symbol)
    typer.echo(json.dumps(res or {}, indent=2))

def _opra_month_letter(month: int, is_call: bool) -> str:
    # Calls A-L (Jan-Dec), Puts M-X (Jan-Dec)
    letters_call = "ABCDEFGHIJKL"
    letters_put = "MNOPQRSTUVWX"
    if month < 1 or month > 12:
        raise ValueError("month must be 1..12")
    return (letters_call if is_call else letters_put)[month - 1]

def _format_spxw_short_symbol(root: str, dt_str: str, is_call: bool, strike: int) -> str:
    """Format an OPRA-style short option symbol like SPXW2501J2600 for a weekly date.

    Pattern heuristic based on IQFeed UI examples: ROOT + YY + DD + M + STRIKE
      - YY: 2-digit year
      - DD: 2-digit day of month (for weeklies)
      - M : month code letter (A-L calls, M-X puts)
      - STRIKE: integer strike (no decimals)
    """
    import datetime as _dt
    d = _dt.datetime.strptime(dt_str, "%Y-%m-%d").date()
    yy = d.year % 100
    dd = d.day
    m_letter = _opra_month_letter(d.month, is_call)
    return f"{root}{yy:02d}{dd:02d}{m_letter}{int(strike)}"

@app.command(name="iqfeed_chain_guess")
def iqfeed_chain_guess(
    root: str = typer.Argument("SPXW", help="OPRA root, e.g., SPXW"),
    expiry: str = typer.Argument(..., help="Expiry date YYYY-MM-DD (weekly date)"),
    min_strike: int = typer.Option(2000, help="Minimum strike to probe"),
    max_strike: int = typer.Option(7000, help="Maximum strike to probe"),
    step: int = typer.Option(25, help="Strike step size (integer)"),
    calls: bool = typer.Option(True, help="Include calls"),
    puts: bool = typer.Option(True, help="Include puts"),
    timeout: float = typer.Option(0.4, help="Socket timeout per symbol (seconds)"),
    out: str = typer.Option("", help="Optional path to write JSON results to a file"),
):
    """Heuristically enumerate an options chain by probing candidate short symbols via IQFeed watch/unwatch.

    This is a pragmatic fallback when formal chain lookup isn't available. It will try symbols like
    SPXWYYDDM<strike> across a strike range and include those that return a Q-quote.
    """
    cfg = IQFeedConfig.from_env()
    # Temporarily lower timeouts for faster probing
    cfg.timeout = max(0.2, float(timeout))
    client = IQFeedClient(cfg)
    found: list[str] = []
    total = 0
    for k in range(min_strike, max_strike + 1, step):
        syms: list[str] = []
        if calls:
            syms.append(_format_spxw_short_symbol(root, expiry, True, k))
        if puts:
            syms.append(_format_spxw_short_symbol(root, expiry, False, k))
        for s in syms:
            total += 1
            res = client.lookup_last(s)
            if res and isinstance(res, dict) and res.get("last_price") is not None:
                found.append(s)
    payload = {"root": root, "expiry": expiry, "count": len(found), "scanned": total, "symbols": found}
    if out:
        try:
            from pathlib import Path as _Path
            p = _Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as _e:  # noqa: BLE001
            pass
    typer.echo(json.dumps(payload, indent=2))

@app.command(name="iqfeed_symbol_search")
def iqfeed_symbol_search(query: str = typer.Argument(..., help="Symbol or substring to search on IQFeed lookup port")):
    """Run a minimal symbol search against IQFeed lookup port (scaffold)."""
    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)
    res = client.symbol_search(query)
    typer.echo(json.dumps(res, indent=2))

@app.command(name="openai_ping")
def openai_ping(prompt: str = typer.Option("ping", help="Short text to send as a ping payload")):
    """Quickly verify OpenAI connectivity using a minimal chat completion.

    Returns JSON with ok=true on success and includes a snippet of the response.
    """
    settings = Settings()
    if not settings.has_openai:
        typer.echo(json.dumps({"ok": False, "error": "OPENAI_API_KEY not set"}, indent=2))
        return
    try:
        wrapper = OpenAIWrapper(settings.openai_api_key)
        # Reuse summarize_market path to avoid duplicating client code
        out = wrapper.summarize_market({"ping": prompt}, max_tokens=16)
        # Heuristic: treat outputs starting with "OpenAI error:" as failures
        ok = not (isinstance(out, str) and out.lower().startswith("openai "))
        payload = {"ok": ok, "response": out[:120] if isinstance(out, str) else str(out)}
        if not ok and isinstance(out, str) and ("invalid_api_key" in out or "Incorrect API key" in out or "sk-proj-" in out):
            import os as _os
            payload["hint"] = {
                "note": "If using a project-scoped key (sk-proj-), set OPENAI_PROJECT and optionally OPENAI_ORG.",
                "env_project": bool(_os.getenv("OPENAI_PROJECT")),
                "env_org": bool(_os.getenv("OPENAI_ORG")),
            }
        typer.echo(json.dumps(payload, indent=2))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))


@app.command(name="agents_ping")
def agents_ping(
    prompt: str = typer.Option("ping", help="Short text to send to a minimal agents run"),
    model: str = typer.Option("gpt-4o-mini", help="Model for agents test"),
    timeout: float = typer.Option(10.0, help="Timeout seconds for the agents ping"),
    retries: int = typer.Option(0, help="Retries for transient errors"),
    backoff: float = typer.Option(1.0, help="Initial backoff for retries"),
    log_run: bool = typer.Option(False, help="Log the ping to agents_runs.jsonl"),
):
    """Lightweight check of openai-agents path (imports + single quick run)."""
    settings = Settings()
    if not settings.has_openai:
        typer.echo(json.dumps({"ok": False, "error": "OPENAI_API_KEY not set"}, indent=2))
        return
    try:
        import os as _os
        import agents  # type: ignore  # noqa: F401
        from agents.agent import Agent  # type: ignore
        from agents.models.openai_provider import AsyncOpenAI, OpenAIChatCompletionsModel  # type: ignore
        cli = AsyncOpenAI(api_key=settings.openai_api_key, project=_os.getenv("OPENAI_PROJECT"), organization=_os.getenv("OPENAI_ORG"))
        mdl = OpenAIChatCompletionsModel(model, cli)
        ag = Agent(model=mdl, name="ping-agent")
        raw = _agent_invoke(ag, prompt, retries=retries, backoff=backoff)
        res = _ensure_sync(raw, timeout=timeout)
        text = _normalize_agent_result(res)
        typer.echo(json.dumps({"ok": True, "text": text[:200]}, indent=2))
        if log_run:
            _log_agent_run({"cmd": "agents_ping", "model": model}, text, res)
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))


@app.command(name="list_strategies")
def list_strategies_cmd():
    """List registered strategy names."""
    names = list_strategies()
    typer.echo(json.dumps({"strategies": names, "count": len(names)}, indent=2))


@app.command(name="strategy_run")
def strategy_run(
    strategy: str = typer.Option("simple_intraday_spx", help="Registered strategy name"),
    price: float = typer.Option(5000.0, help="Last trade/mark price"),
    size: float = typer.Option(1.0, help="Last trade size / volume for this tick"),
    month: int = typer.Option(0, help="Optional month (1-12) to feed seasonality filters"),
    skew_norm: float = typer.Option(0.0, help="Optional normalized skew [-1,1]"),
    high_window: str = typer.Option("", help="Comma list of recent high prices for regime filters"),
    low_window: str = typer.Option("", help="Comma list of recent low prices for regime filters"),
    close_window: str = typer.Option("", help="Comma list of recent close prices for regime filters"),
    json_only: bool = typer.Option(True, help="Emit only JSON output"),
):
    """Run a single synthetic tick through a registered strategy and emit signals.

    Useful for quick experimentation of composite strategies (e.g. odte_composite)
    without a full backtest harness.
    """
    from .strategies.registry import get_strategy
    try:
        Strat = get_strategy(strategy)
    except KeyError as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)}))
        raise typer.Exit(1)

    # Parse optional windows
    def _parse_series(s: str):
        if not s.strip():
            return []
        try:
            return [float(x) for x in s.split(',') if x.strip()]
        except Exception:
            return []

    ctx = {
        "lastPrice": price,
        "lastSize": size,
        "month": month if month else None,
        "skew_norm": max(-1.0, min(1.0, skew_norm)),
    }
    hw = _parse_series(high_window)
    lw = _parse_series(low_window)
    cw = _parse_series(close_window)
    if hw and lw and cw and len(hw) == len(lw) == len(cw):
        ctx.update({
            "high_window": hw,
            "low_window": lw,
            "close_window": cw,
        })

    strat_instance = Strat() if callable(Strat) else Strat  # naive instantiation
    # Some strategies may require params; user can instead call directly in code for custom init
    out_signals = []
    try:
        res = strat_instance.evaluate(ctx)
        for sig in res:
            try:
                out_signals.append(sig.to_dict())  # type: ignore[attr-defined]
            except Exception:
                # fallback manual serialization
                out_signals.append({k: getattr(sig, k) for k in dir(sig) if not k.startswith('_') and k in ("name", "value", "metadata", "strength")})
        typer.echo(json.dumps({"ok": True, "strategy": strategy, "signals": out_signals}, indent=2 if not json_only else None))
    except Exception as exc:  # noqa: BLE001
        typer.echo(json.dumps({"ok": False, "error": str(exc)}))


@app.command(name="health")
def health(
    live: bool = typer.Option(True, help="Perform real API calls (small, low-cost). If false, only checks env and instantiation."),
    agents_live: bool = typer.Option(False, help="With --live, also perform a tiny Agents SDK call."),
    model: str = typer.Option("gpt-4o-mini", help="Model for OpenAI/Agents tests"),
    verbose: bool = typer.Option(False, help="Include debug previews and version info in the JSON output"),
    json_only: bool = typer.Option(True, help="Ensure strictly JSON output (no extra prints)"),
    summary: bool = typer.Option(False, help="Also print a one-line PASS/FAIL summary when --json-only false"),
    polygon_preview: bool = typer.Option(False, help="Include a tiny Polygon snapshot preview (SPX or fallback) when available"),
    quiet_polygon: bool = typer.Option(False, help="Suppress Polygon 4xx warnings (fallback still attempted)"),
):
    """Check IQFeed, Polygon, and OpenAI connectivity, including Agents import, with latency and hints."""
    import os as _os
    import time as _t
    settings = Settings()
    report: dict[str, object] = {
        "env": {
            "has_openai": settings.has_openai,
            "has_polygon": settings.has_polygon,
            "has_iqfeed_env": bool(_os.getenv("IQFEED_USERNAME") and _os.getenv("IQFEED_PASSWORD")),
            "openai_project": bool(_os.getenv("OPENAI_PROJECT")),
            "openai_org": bool(_os.getenv("OPENAI_ORG")),
        }
    }

    # IQFeed
    iq: dict[str, object] = {"ok": False}
    try:
        t0 = _t.perf_counter()
        cfg = IQFeedConfig.from_env()
        client = IQFeedClient(cfg)
        ok = client.ping() if live else True
        iq.update({"ok": bool(ok), "latency_ms": round((_t.perf_counter() - t0) * 1000, 2)})
    except Exception as exc:  # noqa: BLE001
        iq.update({"ok": False, "error": str(exc)})
    report["iqfeed"] = iq

    # Polygon
    poly: dict[str, object] = {"ok": False}
    if settings.has_polygon:
        try:
            t0 = _t.perf_counter()
            pc = PolygonClient(PolygonConfig.from_env())
            if live:
                data = pc.last_trade_spx(use_cache=True)
                poly_ok = bool(data)
                if polygon_preview and isinstance(data, dict) and data.get("results"):
                    try:
                        r0 = (data.get("results") or [{}])[0]
                        poly["spx_preview"] = {"c": r0.get("c"), "t": r0.get("t")}
                    except Exception:
                        pass
                # If SPX not authorized or missing, try a fallback symbol (default SPY)
                if not poly_ok:
                    fb = getattr(settings, "polygon_index_fallback", "SPY")
                    try:
                        sp = pc.last_trade_symbol(fb)
                        poly_ok = bool(sp)
                        if poly_ok:
                            poly["fallback_symbol"] = fb
                            if polygon_preview and isinstance(sp, dict) and sp.get("results"):
                                try:
                                    r0 = (sp.get("results") or [{}])[0]
                                    poly["fallback_preview"] = {"c": r0.get("c"), "t": r0.get("t")}
                                except Exception:
                                    pass
                    except Exception:
                        pass
            else:
                poly_ok = True
            poly.update({"ok": poly_ok, "latency_ms": round((_t.perf_counter() - t0) * 1000, 2)})
        except Exception as exc:  # noqa: BLE001
            if ("NOT_AUTHORIZED" in str(exc) or "403" in str(exc)) and quiet_polygon:
                # Suppress noisy warning in quiet mode while keeping ok flag context
                current = poly.get("warnings")
                if not isinstance(current, list):
                    current = []
                current.append("polygon 403 suppressed")
                poly["warnings"] = current
            else:
                poly.update({"ok": False, "error": str(exc)})
    else:
        poly.update({"ok": False, "error": "POLYGON_API_KEY missing"})
    report["polygon"] = poly

    # OpenAI (plain client)
    oa: dict[str, object] = {"ok": False}
    if settings.has_openai:
        try:
            t0 = _t.perf_counter()
            if live:
                wrapper = OpenAIWrapper(settings.openai_api_key)
                out = wrapper.summarize_market({"ping": "health"}, max_tokens=8, retries=0)
                ok = not (isinstance(out, str) and out.lower().startswith("openai "))
                if verbose and isinstance(out, str):
                    oa["response_preview"] = out[:120]
            else:
                from openai import OpenAI  # type: ignore
                try:
                    _proj = _os.getenv("OPENAI_PROJECT"); _org = _os.getenv("OPENAI_ORG")
                    if _proj:
                        OpenAI(api_key=settings.openai_api_key, project=_proj)
                    elif _org:
                        OpenAI(api_key=settings.openai_api_key, organization=_org)
                    else:
                        OpenAI(api_key=settings.openai_api_key)
                except TypeError:
                    OpenAI(api_key=settings.openai_api_key)
                ok = True
            oa.update({"ok": bool(ok), "latency_ms": round((_t.perf_counter() - t0) * 1000, 2)})
            if verbose:
                try:
                    import openai as _openai  # type: ignore
                    oa["sdk_version"] = getattr(_openai, "__version__", "unknown")
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            oa.update({"ok": False, "error": str(exc)})
    else:
        oa.update({"ok": False, "error": "OPENAI_API_KEY missing"})
    report["openai"] = oa

    # Agents SDK
    agents_stat: dict[str, object] = {"import_ok": False}
    try:
        t0 = _t.perf_counter()
        import agents  # type: ignore
        agents_stat["import_ok"] = True
        agents_stat["import_latency_ms"] = round((_t.perf_counter() - t0) * 1000, 2)
        if verbose:
            agents_stat["sdk_version"] = getattr(agents, "__version__", "unknown")
        if live and agents_live and settings.has_openai:
            from agents.agent import Agent  # type: ignore
            from agents.models.openai_provider import AsyncOpenAI, OpenAIChatCompletionsModel  # type: ignore
            t1 = _t.perf_counter()
            cli = AsyncOpenAI(api_key=settings.openai_api_key, project=_os.getenv("OPENAI_PROJECT"), organization=_os.getenv("OPENAI_ORG"))
            mdl = OpenAIChatCompletionsModel(model, cli)
            ag = Agent(model=mdl, name="health-check")
            try:
                res = _agent_invoke(ag, "Reply 'ok'.")
                import inspect as _insp_ag
                if _insp_ag.iscoroutine(res):  # pragma: no cover
                    import asyncio as _asyncio_ag
                    try:
                        res = _asyncio_ag.run(res)
                    except RuntimeError:
                        pass
                text = _normalize_agent_result(res)
                agents_stat["live_ok"] = bool(text)
                agents_stat["live_latency_ms"] = round((_t.perf_counter() - t1) * 1000, 2)
                if verbose and isinstance(text, str):
                    agents_stat["text_preview"] = text[:120]
            except Exception as _agent_live_exc:  # noqa: BLE001
                agents_stat["live_ok"] = False
                agents_stat["live_error"] = str(_agent_live_exc)
                agents_stat["live_latency_ms"] = round((_t.perf_counter() - t1) * 1000, 2)
    except Exception as exc:  # noqa: BLE001
        agents_stat["error"] = str(exc)
    report["agents"] = agents_stat

    # Always emit JSON first
    typer.echo(json.dumps(report, indent=2))
    # Optional human summary
    if not json_only and summary:
        statuses = {
            "iqfeed": bool(iq.get("ok")),
            "polygon": bool(poly.get("ok")),
            "openai": bool(oa.get("ok")),
            "agents_import": bool(agents_stat.get("import_ok")),
            "agents_live": (bool(agents_stat.get("live_ok")) if (live and agents_live and settings.has_openai) else True),
        }
        ok_all = all(statuses.values())
        failing = [k for k, v in statuses.items() if not v]
        line = (
            f"HEALTH PASS | iqfeed={statuses['iqfeed']} polygon={statuses['polygon']} openai={statuses['openai']} "
            f"agents_import={statuses['agents_import']} agents_live={statuses['agents_live']}"
            if ok_all else
            f"HEALTH FAIL: {', '.join(failing)}"
        )
        typer.echo(line)


@app.command(name="select_regime")
def select_regime(
    index_trend_window_sec: float = typer.Option(120.0, help="SPX trend lookback seconds for regime decision"),
    index_trend_min_bp: float = typer.Option(3.0, help="Absolute SPX trend threshold (bp) to consider directional"),
    vix_trend_window_sec: float = typer.Option(150.0, help="VIX trend lookback seconds for regime decision"),
    vix_trend_min_bp: float = typer.Option(4.0, help="Absolute VIX trend threshold (bp) to consider directional"),
    news_bias_min_conf: float = typer.Option(0.6, help="Minimum confidence to enforce news directional bias [0-1]"),
    news_bias_staleness_sec: float = typer.Option(3600.0, help="Consider news bias only if within this many seconds"),
):
    """Classify the current intraday regime from SPX/VIX trends, indicators, and (optional) news bias.

    Emits JSON with fields: regime, direction, recommend (flags for rocket_watch), diagnostics, and reasons.
    """
    import datetime as _dtm
    import time as _time
    from pathlib import Path as _P
    settings = Settings()
    # Defaults
    idx_bp = 0.0
    vix_bp = 0.0
    # Use numeric defaults (NaN) to avoid type inference issues, then clean before JSON
    import math as _math
    ind: dict[str, float] = {"ema": _math.nan, "macd": _math.nan, "macd_signal": _math.nan, "rsi": _math.nan, "close": _math.nan}
    news = {"bias": "neutral", "confidence": 0.0, "staleness_sec": None}
    reasons: list[str] = []
    ts_now = _time.time()

    # Fetch intraday SPX/VIX minute bars and compute window trend
    try:
        from .datafeeds.polygon_client import PolygonClient, PolygonConfig
        pc = PolygonClient(PolygonConfig.from_env())
        # SPX
        df_spx = pc.fetch_index_intraday_aggs(ticker="I:SPX", timespan="minute", mult=1, lookback_days=1)
        if df_spx is not None and not df_spx.empty:
            import pandas as _pd
            import pytz as _pytz
            from datetime import time as _tcls
            et = _pytz.timezone('America/New_York')
            df_spx["et"] = df_spx["dt"].dt.tz_convert(et)
            d0 = df_spx["et"].dt.date.iloc[-1]
            dft = df_spx[(df_spx["et"].dt.date == d0) & (df_spx["et"].dt.time >= _tcls(9,30)) & (df_spx["et"].dt.time <= _tcls(16,0))]
            if dft.empty:
                dft = df_spx
            # index trend
            t_now = dft["dt"].iloc[-1]
            cutoff = t_now - _pd.Timedelta(seconds=float(index_trend_window_sec))
            prior = dft[dft["dt"] <= cutoff]
            last_px = float(dft["close"].astype(float).iloc[-1])
            if not prior.empty:
                p0 = float(prior["close"].astype(float).iloc[-1])
            else:
                p0 = float(dft["close"].astype(float).iloc[-2]) if len(dft) >= 2 else last_px
            if p0 > 0:
                idx_bp = (last_px - p0) / p0 * 10000.0
            # indicators snapshot
            try:
                ind_raw = pc.compute_indicators(dft)
                if isinstance(ind_raw, dict):
                    for k in ("ema", "macd", "macd_signal", "rsi", "close"):
                        v = ind_raw.get(k)
                        try:
                            if v is not None:
                                ind[k] = float(v)
                        except Exception:
                            pass
            except Exception:
                pass
        # VIX
        df_vix = pc.fetch_index_intraday_aggs(ticker="I:VIX", timespan="minute", mult=1, lookback_days=2)
        if df_vix is not None and not df_vix.empty:
            import pandas as _pd
            import pytz as _pytz
            from datetime import time as _tcls
            et = _pytz.timezone('America/New_York')
            df_vix["et"] = df_vix["dt"].dt.tz_convert(et)
            d0 = df_vix["et"].dt.date.iloc[-1]
            dft = df_vix[(df_vix["et"].dt.date == d0) & (df_vix["et"].dt.time >= _tcls(9,30)) & (df_vix["et"].dt.time <= _tcls(16,0))]
            if dft.empty:
                dft = df_vix
            t_now = dft.index[-1] if isinstance(dft.index, _pd.DatetimeIndex) else dft["dt"].iloc[-1]
            cutoff = t_now - _pd.Timedelta(seconds=float(vix_trend_window_sec))
            # ensure we use index if set earlier
            idx_series = dft["close"]
            if not isinstance(dft.index, _pd.DatetimeIndex):
                dft = dft.set_index("dt")
                idx_series = dft["close"]
            pos = dft.index.searchsorted(cutoff, side="right") - 1
            if pos < 0:
                pos = 0
            prev_px = float(idx_series.iloc[pos])
            last_px = float(idx_series.iloc[-1])
            if prev_px > 0:
                vix_bp = (last_px - prev_px) / prev_px * 10000.0
    except Exception as _exc:  # noqa: BLE001
        reasons.append(f"polygon_error={_exc}")

    # News agent bias (optional)
    try:
        d = _dtm.datetime.now().strftime("%Y%m%d")
    except Exception:
        d = _time.strftime("%Y%m%d", _time.gmtime())
    try:
        p = _P('logs') / f'news_agent_summary_{d}.json'
        if p.exists():
            import json as _json
            obj = _json.loads(p.read_text(encoding='utf-8'))
            db = obj.get("directional_bias") or {}
            bias = str(db.get("bias") or "neutral").lower()
            conf = float(db.get("confidence") or 0.0)
            gen_at = obj.get("generated_at")
            st_sec = None
            if isinstance(gen_at, str):
                try:
                    t = _dtm.datetime.fromisoformat(gen_at.replace("Z", "+00:00")).timestamp()
                    st_sec = ts_now - float(t)
                except Exception:
                    st_sec = None
            news = {"bias": bias, "confidence": conf, "staleness_sec": st_sec}
    except Exception:
        pass

    # Indicator votes
    bull_votes = 0
    bear_votes = 0
    try:
        def _nz(x):
            try:
                return float(x)
            except Exception:
                return _math.nan
        macd = _nz(ind.get("macd"))
        macd_sig = _nz(ind.get("macd_signal"))
        rsi = _nz(ind.get("rsi"))
        ema = _nz(ind.get("ema"))
        close = _nz(ind.get("close"))
        if not (_math.isnan(macd) or _math.isnan(macd_sig)):
            if macd > macd_sig:
                bull_votes += 1
            elif macd < macd_sig:
                bear_votes += 1
        if not _math.isnan(rsi):
            if float(rsi) >= 50.0:
                bull_votes += 1
            elif float(rsi) <= 50.0:
                bear_votes += 1
        if not (_math.isnan(ema) or _math.isnan(close)):
            if float(close) >= float(ema):
                bull_votes += 1
            elif float(close) <= float(ema):
                bear_votes += 1
    except Exception:
        pass

    # Core regime logic
    vix_up = (vix_bp >= vix_trend_min_bp)
    vix_down = (vix_bp <= -vix_trend_min_bp)
    idx_up = (idx_bp >= index_trend_min_bp)
    idx_down = (idx_bp <= -index_trend_min_bp)

    regime = "neutral_chop"
    direction = "both"
    if vix_up and idx_down:
        regime = "risk_off_puts"; direction = "puts"; reasons.append("vix_up AND spx_down")
    elif vix_down and idx_up:
        regime = "risk_on_calls"; direction = "calls"; reasons.append("vix_down AND spx_up")
    elif idx_down and (bear_votes >= max(1, bull_votes) or vix_up):
        regime = "balanced_bear"; direction = "puts"; reasons.append("spx_down + indicators_bearish")
    elif idx_up and (bull_votes >= max(1, bear_votes) or vix_down):
        regime = "balanced_bull"; direction = "calls"; reasons.append("spx_up + indicators_bullish")
    else:
        reasons.append("choppy or conflicting signals")

    # News override (fresh + confident)
    try:
        fresh = (news.get("staleness_sec") is not None and float(news["staleness_sec"]) <= float(news_bias_staleness_sec))
        if fresh and float(news.get("confidence") or 0.0) >= float(news_bias_min_conf):
            b = str(news.get("bias") or "neutral").lower()
            if b == "bear":
                regime = "risk_off_puts"; direction = "puts"; reasons.append("news_override=bear")
            elif b == "bull":
                regime = "risk_on_calls"; direction = "calls"; reasons.append("news_override=bull")
    except Exception:
        pass

    # Recommendation for rocket_watch flags
    recommend = {
        "preset": "quick_win",
        "require_index_trend": regime != "neutral_chop",
        "use_indicator_bias": regime != "neutral_chop",
        "require_news_directional_bias": regime == "neutral_chop",
        "vix_puts_only": True,  # never block calls on VIX; enforce for puts
        "use_vix_trend": True,
        "vix_trend_window_sec": float(vix_trend_window_sec),
        "vix_trend_min_bp": float(vix_trend_min_bp),
        "index_trend_window_sec": float(index_trend_window_sec),
        "index_trend_min_bp": float(index_trend_min_bp),
    }
    # Clean indicators for JSON (replace NaN with None)
    ind_clean: dict[str, float | None] = {k: (None if (v is None or (isinstance(v, float) and _math.isnan(v))) else float(v)) for k, v in ind.items()}
    payload = {
        "ok": True,
        "regime": regime,
        "direction": direction,
        "recommend": recommend,
        "diagnostics": {
            "idx_bp": round(float(idx_bp), 3),
            "vix_bp": round(float(vix_bp), 3),
            "indicators": ind_clean,
            "indicator_votes": {"bull": bull_votes, "bear": bear_votes},
            "news": news,
        },
        "reasons": reasons,
        "ts": _dtm.datetime.now(_dtm.timezone.utc).isoformat(),
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command(name="market_summary")
def market_summary(
    spx_symbol: str = typer.Option("@SPX.X", help="IQFeed SPX index symbol"),
    strategy: str = typer.Option("odte_direction", help="Strategy name (on_price oriented)"),
    use_cache: bool = typer.Option(True, help="Cache summary for identical feature set within a short TTL"),
    rate_limit_secs: float = typer.Option(5.0, help="Min seconds between OpenAI calls (if enabled)"),
    model: str = typer.Option("gpt-4o-mini", help="Model name for OpenAI/Agents"),
    backend: str = typer.Option("auto", help="Backend to use for summarization: agents, openai, or auto", case_sensitive=False),
    extra: str = typer.Option("", help="Comma key=val pairs to inject as features"),
    skew: bool = typer.Option(False, help="Include options skew metrics (live-first, fallback to sim)"),
    skew_live_only: bool = typer.Option(False, help="If set, require live greeks; fail silently if not available"),
    skew_buckets: str = typer.Option("0.25,0.50", help="Comma list of abs-delta buckets (e.g. 0.25,0.5)"),
    skew_tol: float = typer.Option(0.05, help="Delta bucket tolerance"),
    skew_scale: float = typer.Option(0.20, help="Scaling for normalized skew score (-1..1)"),
    persist_skew: bool = typer.Option(False, help="Persist skew snapshot to metrics log (category=skew)"),
    price_live_only: bool = typer.Option(False, help="Require live IQFeed price; if unavailable, return status without polygon/synthetic fallback"),
    skew_parquet_out: Path = typer.Option(None, help="Optional path to write a one-row skew parquet; overrides default location"),
    write_skew_parquet: bool = typer.Option(False, help="If set, write skew snapshot parquet to data/derived/skew/date=YYYY-MM-DD/"),
):
    """Fetch snapshot (IQFeed -> Polygon fallback), run single-tick strategy, produce neutral summary.

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
    price_provenance = None
    try:
        cfg = IQFeedConfig.from_env()
        iq_client = IQFeedClient(cfg)
        iq_snapshot = iq_client.lookup_last(spx_symbol)
        if iq_snapshot and isinstance(iq_snapshot, dict):
            lp = iq_snapshot.get("last_price") or iq_snapshot.get("last_trade")
            if isinstance(lp, (int, float)) and lp > 0:
                price = float(lp)
                price_provenance = "iqfeed"
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("IQFeed snapshot failure: %s", exc)

    # Polygon fallback (unless explicitly live-only)
    if price is None and not price_live_only and settings.has_polygon:
        try:
            poly_client = PolygonClient(PolygonConfig.from_env())
            poly_snapshot = poly_client.last_trade_spx()
            if poly_snapshot and isinstance(poly_snapshot, dict):
                results = poly_snapshot.get("results")
                if results and isinstance(results, list):
                    price = float(results[0].get("c", 0) or 0)
                    if price and price > 0:
                        price_provenance = "polygon"
            # Fallback if index is not authorized or missing (configurable symbol)
            if (price is None or price == 0) and (not poly_snapshot or poly_snapshot.get("status") == "NOT_AUTHORIZED"):
                fb = getattr(settings, "polygon_index_fallback", "SPY")
                spy_prev = poly_client.last_trade_symbol(fb)
                if spy_prev and isinstance(spy_prev, dict):
                    results = spy_prev.get("results")
                    if results and isinstance(results, list):
                        price = float(results[0].get("c", 0) or 0)
                        if price and price > 0:
                            price_provenance = f"polygon_{fb.lower()}"
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Polygon fallback failed: %s", exc)

    if price is None and price_live_only:
        typer.echo(json.dumps({
            "symbol": spx_symbol,
            "status": "no-live-price",
            "message": "IQFeed snapshot unavailable and --price-live-only set",
        }, indent=2))
        return

    if price is None or price == 0:
        # Offline/test fallback: synthetic placeholder price
        price = 5000.0
        iq_snapshot = iq_snapshot or {}
        iq_snapshot["synthetic"] = True
        price_provenance = price_provenance or "synthetic"

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
    # Optional options skew (live-first with fallback to sim unless --skew-live-only)
    if skew:
        try:
            from .datafeeds.iqfeed_options import IQFeedOptionsGreeks, skew_signal_from_chain
            from .utils.metrics_store import append_metric as _append_metric
            og = IQFeedOptionsGreeks()
            # Prefer live path (numeric price) with fallback handled internally
            chain = og.fetch_chain_greeks(price)
            provenance = getattr(og, "last_source", None)
            if skew_live_only and provenance != "live":
                chain = []  # treat as unavailable
            if chain:
                # Parse buckets
                b_list: list[float] = []
                if skew_buckets.strip():
                    for part in skew_buckets.split(','):
                        p = part.strip()
                        if not p:
                            continue
                        try:
                            b_list.append(float(p))
                        except ValueError:
                            continue
                ss = skew_signal_from_chain(chain, buckets=b_list or None, tol=skew_tol, scale=skew_scale)
                for k, v in ss.items():
                    if k not in features:
                        features[k] = v  # type: ignore[assignment]
                features["skew_chain_size"] = len(chain)
                features["skew_provenance"] = provenance or "unknown"
                if persist_skew:
                    try:
                        _append_metric(settings.data_dir or "data", "skew", {
                            "symbol": spx_symbol,
                            "price": price,
                            "provenance": provenance,
                            "buckets": b_list or [0.25, 0.5],
                            "tol": skew_tol,
                            "scale": skew_scale,
                            "metrics": ss,
                            "chain_size": len(chain),
                        })
                    except Exception as _exc:  # noqa: BLE001
                        LOGGER.debug("Persist skew failed: %s", _exc)
                # Optional parquet snapshot
                if write_skew_parquet or skew_parquet_out is not None:
                    try:
                        import pandas as _pd
                        from pathlib import Path as _P
                        base = _P(settings.data_dir or "data")
                        if skew_parquet_out is None:
                            date = time.strftime("%Y-%m-%d", time.gmtime())
                            out_dir = base / "derived" / "skew" / f"date={date}"
                            out_dir.mkdir(parents=True, exist_ok=True)
                            fname = f"part-{int(time.time()*1000)}.parquet"
                            target = out_dir / fname
                        else:
                            target = _P(skew_parquet_out)
                            target.parent.mkdir(parents=True, exist_ok=True)
                        row = {"ts": time.time(), "symbol": spx_symbol, "price": price, "provenance": provenance or "unknown",
                               "buckets": ",".join(str(b) for b in (b_list or [0.25, 0.5])),
                               "tol": skew_tol, "scale": skew_scale, "chain_size": len(chain), **ss}
                        _pd.DataFrame([row]).to_parquet(target, index=False)  # type: ignore[arg-type]
                        features["skew_parquet_path"] = str(target)
                    except Exception as _exc:  # noqa: BLE001
                        LOGGER.debug("Write skew parquet failed: %s", _exc)
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
    cache_key = str((feat_items, backend.lower(), model))
    now = _time()
    summary = "(OpenAI disabled)"
    used_cache = False
    rate_limited = False
    summary_backend = None
    if settings.has_openai:
        if use_cache and cache_key in market_summary._cache:  # type: ignore[attr-defined]
            cached = market_summary._cache[cache_key]  # type: ignore[index]
            if isinstance(cached, dict):
                summary = cached.get("text", "")  # type: ignore[assignment]
                summary_backend = cached.get("backend")
            else:
                summary = cached  # legacy cache
                summary_backend = "openai"
            used_cache = True
        else:
            if rate_limit_secs > 0 and (now - market_summary._last_call_ts) < rate_limit_secs:  # type: ignore[attr-defined]
                rate_limited = True
                summary = "(rate-limited; recent summary suppressed)"
            else:
                chosen = backend.lower()
                text_out = None
                # Prefer/force agents
                if chosen in ("agents", "auto"):
                    try:
                        from agents.agent import Agent  # type: ignore
                        from agents.models.openai_provider import AsyncOpenAI, OpenAIChatCompletionsModel  # type: ignore
                        import os as _os
                        oai_client = AsyncOpenAI(
                            api_key=settings.openai_api_key,
                            project=_os.getenv("OPENAI_PROJECT"),
                            organization=_os.getenv("OPENAI_ORG"),
                        )
                        # Build neutral prompt like OpenAIWrapper does
                        def _truncate(v):
                            s = str(v)
                            return s[:117] + "..." if len(s) > 120 else s
                        safe_items = []
                        for k, v in list(features.items())[:50]:
                            safe_items.append(f"{k}={_truncate(v)}")
                        feat_str = ", ".join(safe_items)
                        base_prompt = (
                            "Provide a concise, strictly informational one-paragraph summary of these intraday "
                            "features without offering investment advice, recommendations, or forward-looking guarantees. "
                            "Avoid words like 'should', 'recommend', 'profit', or directives. Data: " + feat_str
                        )
                        agents_model = OpenAIChatCompletionsModel(model, oai_client)
                        agent = Agent(model=agents_model, name="market-summary")
                        res = _agent_invoke(agent, base_prompt)
                        text_out = getattr(res, "output_text", None) or str(res)
                        summary_backend = "openai-agents"
                    except Exception as _exc:
                        if chosen == "agents":
                            text_out = f"(agents error: {_exc})"
                            summary_backend = "openai-agents"
                        else:
                            text_out = None  # fall through to OpenAI
                # Fallback or forced openai
                if text_out is None and chosen in ("openai", "auto"):
                    try:
                        summarizer = OpenAIWrapper(settings.openai_api_key)
                        text_out = summarizer.summarize_market(features)
                        summary_backend = "openai"
                    except Exception as _exc2:  # very defensive
                        text_out = f"(openai error: {_exc2})"
                        summary_backend = "openai"
                summary = text_out or "Summary unavailable"
                market_summary._last_call_ts = now  # type: ignore[attr-defined]
                if use_cache:
                    market_summary._cache[cache_key] = {"backend": summary_backend, "text": summary}  # type: ignore[attr-defined]

    typer.echo(json.dumps({
        "symbol": spx_symbol,
        "price": price,
        "strategy": strategy,
        "strategy_signal": sig_name,
        "strategy_meta_keys": list(meta.keys()),
        "features": features,
        "feature_count": len(features),
    "summary": summary,
    "summary_backend": summary_backend,
        "iqfeed_snapshot": bool(iq_snapshot),
        "polygon_fallback_used": (price is not None and iq_snapshot is None),
        "price_provenance": price_provenance or ("polygon" if (price is not None and iq_snapshot is None) else "unknown"),
        "skew_provenance": features.get("skew_provenance"),
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
    heartbeat_secs: float = typer.Option(5.0, help="IQFeed heartbeat ping interval"),
    reconnect_idle_secs: float = typer.Option(20.0, help="Reconnect when idle exceeds this many seconds"),
    # Optional bar aggregation wiring (disabled unless interval > 0)
    bars_interval: float = typer.Option(0.0, help="If > 0, aggregate streaming prices into time bars of this length (sec)"),
    bars_watermark_delay: float = typer.Option(0.0, help="Delay (sec) before finalizing bars to accept late ticks"),
    bars_enforce_continuous: bool = typer.Option(False, help="Emit empty bars for gaps when streaming bars"),
    bars_outlier_mode: str = typer.Option("winsorize", help='Outlier handling for streaming bars: "none", "winsorize", or "clip"'),
    bars_outlier_window: int = typer.Option(50, help="Return window for outlier stats (streaming bars)"),
    bars_outlier_threshold: float = typer.Option(3.0, help="Threshold for outlier clipping/winsorization (streaming bars)"),
    bars_emit: bool = typer.Option(False, help="If set, include the latest finalized bar fields in emitted features"),
    bars_persist: bool = typer.Option(False, help="If set, append finalized bars as metrics (category=bars_stream)"),
    bars_write: bool = typer.Option(False, help="If set, write finalized bars to parquet partitions under data/derived/bars"),
    bars_partition_hour: bool = typer.Option(True, "--bars-partition-hour/--no-bars-partition-hour", help="When writing streaming bars, create hour=HH sub-partitions under each date"),
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
    # Optional bar aggregator for streaming
    bar_agg = None
    if bars_interval and bars_interval > 0:
        try:
            bar_agg = TimeBarAggregator(
                interval_sec=bars_interval,
                watermark_delay=bars_watermark_delay,
                enforce_continuous=bars_enforce_continuous,
                outlier_mode=bars_outlier_mode,
                outlier_window=bars_outlier_window,
                outlier_threshold=bars_outlier_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to initialize streaming bar aggregator: %s", exc)
            bar_agg = None
    if not synthetic:
        try:
            cfg = IQFeedConfig.from_env()
            iq_stream = IQFeedLevel1Stream(cfg, heartbeat_secs=heartbeat_secs, reconnect_idle_secs=reconnect_idle_secs)
            iq_stream.start(sym_list)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Falling back to synthetic due to IQFeed error: %s", exc)
            synthetic = True

    price = seed_price
    # Optional parquet micro-batch buffers for streaming bars keyed by (date, hour)
    from collections import defaultdict as _dd
    bars_rows_buffers: dict[tuple[str, str], list[dict]] = _dd(list)
    bars_last_flush = 0.0
    bars_flush_secs = 2.0
    bars_max_rows = 5000

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
            # Feed bar aggregator using primary symbol's price
            new_bars = []
            finalized_bars = []
            if bar_agg is not None and isinstance(cur_price, (int, float)):
                try:
                    tmsg = {"symbol": primary, "epoch": now, "last_trade": float(cur_price), "last_trade_size": 0}
                    new_bars = bar_agg.add_tick(tmsg)
                    finalized_bars = bar_agg.finalize_until(now)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("Streaming bar aggregation failed: %s", exc)
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
            # If requested, include the last finalized bar fields in features
            if bars_emit and bar_agg is not None:
                last_bar = None
                if finalized_bars:
                    last_bar = finalized_bars[-1]
                elif new_bars:
                    # a bar was finalized due to bucket advance
                    last_bar = new_bars[-1]
                if last_bar is not None:
                    try:
                        features.update({
                            "bar_interval": bars_interval,
                            "bar_start_ts": last_bar.start_ts,
                            "bar_end_ts": last_bar.end_ts,
                            "bar_open": last_bar.open,
                            "bar_high": last_bar.high,
                            "bar_low": last_bar.low,
                            "bar_close": last_bar.close,
                            "bar_volume": last_bar.volume,
                            "bar_trades": last_bar.trades,
                        })
                    except Exception:
                        pass
            if persist:
                try:
                    append_metric(settings_dir, "market_stream", out)  # type: ignore[arg-type]
                except Exception as exc:
                    LOGGER.warning("Persist failed: %s", exc)
            # Optionally persist streaming bars separately as metrics
            if bars_persist and bar_agg is not None:
                try:
                    for bb in (finalized_bars or []):
                        append_metric(settings_dir, "bars_stream", {
                            "symbol": primary,
                            "interval": bars_interval,
                            "start_ts": bb.start_ts,
                            "end_ts": bb.end_ts,
                            "open": bb.open,
                            "high": bb.high,
                            "low": bb.low,
                            "close": bb.close,
                            "volume": bb.volume,
                            "trades": bb.trades,
                        })
                except Exception as exc:
                    LOGGER.debug("Persist bars failed: %s", exc)
            # Optionally write finalized bars to parquet
            if bars_write and bar_agg is not None and (finalized_bars or new_bars):
                try:
                    for bb in (finalized_bars or []):
                        date_key = time.strftime("%Y-%m-%d", time.gmtime(bb.start_ts))
                        hour_key = time.strftime("%H", time.gmtime(bb.start_ts))
                        bars_rows_buffers[(date_key, hour_key)].append({
                            "symbol": primary,
                            "start_ts": bb.start_ts,
                            "end_ts": bb.end_ts,
                            "open": bb.open,
                            "high": bb.high,
                            "low": bb.low,
                            "close": bb.close,
                            "volume": bb.volume,
                            "trades": bb.trades,
                        })
                    # micro-batch by time/size
                    now_ts = time.time()
                    pending_rows = sum(len(v) for v in bars_rows_buffers.values())
                    if (pending_rows >= bars_max_rows) or ((now_ts - bars_last_flush) >= bars_flush_secs):
                        import pandas as _pd
                        if pending_rows:
                            for (dkey, hkey), rows in list(bars_rows_buffers.items()):
                                if not rows:
                                    continue
                                # build target directory with optional hour partition
                                base_dir = Path(settings_dir) / "derived" / "bars" / f"symbol={primary}" / f"interval={bars_interval}" / f"date={dkey}"
                                if bars_partition_hour:
                                    base_dir = base_dir / f"hour={hkey}"
                                base_dir.mkdir(parents=True, exist_ok=True)
                                dfb = _pd.DataFrame(rows)
                                bars_rows_buffers[(dkey, hkey)].clear()
                                fname = f"part-{int(now_ts*1000)}.parquet"
                                dfb.to_parquet(base_dir / fname, index=False)
                            bars_last_flush = now_ts
                except Exception as exc:
                    LOGGER.debug("Bars parquet write failed: %s", exc)
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
        # Final flush of streaming bars parquet buffers if enabled
        if bars_write:
            try:
                import pandas as _pd
                now_ts = time.time()
                for (dkey, hkey), rows in list(bars_rows_buffers.items()):
                    if not rows:
                        continue
                    base_dir = Path(settings_dir) / "derived" / "bars" / f"symbol={primary}" / f"interval={bars_interval}" / f"date={dkey}"
                    if bars_partition_hour:
                        base_dir = base_dir / f"hour={hkey}"
                    base_dir.mkdir(parents=True, exist_ok=True)
                    dfb = _pd.DataFrame(rows)
                    bars_rows_buffers[(dkey, hkey)].clear()
                    fname = f"part-{int(now_ts*1000)}.parquet"
                    dfb.to_parquet(base_dir / fname, index=False)
            except Exception as exc:
                LOGGER.debug("Final bars parquet flush failed: %s", exc)
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
def iqfeed_stream(
    symbols: str = typer.Argument("SPX", help="Comma-separated symbols to watch"),
    samples: int = 5,
    timeout: float = 10.0,
    heartbeat_secs: float = typer.Option(5.0, help="IQFeed heartbeat ping interval"),
    reconnect_idle_secs: float = typer.Option(20.0, help="Reconnect when idle exceeds this many seconds"),
):
    """Stream real-time Level1 quotes from a running IQFeed client (requires local IQConnect)."""
    cfg = IQFeedConfig.from_env()
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    collected: list[dict] = []

    def _on_msg(msg: dict):  # noqa: D401
        if msg.get("symbol") in syms:
            collected.append(msg)

    stream = IQFeedLevel1Stream(cfg, on_message=_on_msg, heartbeat_secs=heartbeat_secs, reconnect_idle_secs=reconnect_idle_secs)
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
def iqfeed_stream_persist(
    symbols: str = typer.Argument("SPX", help="Comma-separated symbols"),
    duration: float = 30.0,
    flush_secs: float = 2.0,
    max_rows: int = 3000,
    heartbeat_secs: float = typer.Option(5.0, help="IQFeed heartbeat ping interval"),
    reconnect_idle_secs: float = typer.Option(20.0, help="Reconnect when idle exceeds this many seconds"),
):
    """Stream Level1 quotes and persist to parquet micro-batches for the given duration (seconds)."""
    settings = Settings()
    cfg = IQFeedConfig.from_env()
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    writer = Level1BatchWriter(BatchConfig(base_dir=Path(settings.data_dir or "data"), flush_secs=flush_secs, max_rows=max_rows))  # type: ignore[arg-type]

    def _on(msg: dict):  # noqa: D401
        if msg.get("symbol") in syms:
            writer.add(msg)

    stream = IQFeedLevel1Stream(cfg, on_message=_on, heartbeat_secs=heartbeat_secs, reconnect_idle_secs=reconnect_idle_secs)
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


@app.command(name="compact_bars")
def compact_bars(
    symbol: str = typer.Argument(..., help="Symbol to compact (e.g., SPX)"),
    interval: float = typer.Argument(..., help="Bar interval seconds (e.g., 1.0)"),
    start_date: str = typer.Option(None, help="Start date YYYY-MM-DD (inclusive)"),
    end_date: str = typer.Option(None, help="End date YYYY-MM-DD (inclusive)"),
    out_dir: str = typer.Option(None, help="Output base directory (defaults to data/derived/bars_compact)"),
):
    """Compact hourly (or multi-part) bar parquet files into a single daily file per date.

    Reads from data/derived/bars/symbol=SYMBOL/interval=INTERVAL/date=YYYY-MM-DD[/hour=HH] and writes
    a single parquet per date under data/derived/bars_compact/symbol=.../interval=.../date=YYYY-MM-DD.parquet
    unless --out-dir is provided.
    """
    settings = Settings()
    base = Path(settings.data_dir or "data") / "derived" / "bars" / f"symbol={symbol}" / f"interval={interval}"
    if not base.exists():
        typer.echo(json.dumps({"error": f"base not found: {base}"}))
        raise typer.Exit(1)
    # Determine date range
    dates = []
    for d in sorted(p.name for p in base.glob("date=*") if p.is_dir()):
        if not d.startswith("date="):
            continue
        dt = d.split("=", 1)[1]
        if start_date and dt < start_date:
            continue
        if end_date and dt > end_date:
            continue
        dates.append(dt)
    if not dates:
        typer.echo(json.dumps({"error": "no matching date partitions"}))
        raise typer.Exit(1)
    import pandas as _pd
    total_rows = 0
    outputs = []
    out_base = Path(out_dir) if out_dir else (Path(settings.data_dir or "data") / "derived" / "bars_compact")
    for dt in dates:
        ddir = base / f"date={dt}"
        # Gather parquet files from hour subdirs if present; else from date dir
        parts = []
        hour_dirs = list(ddir.glob("hour=*") )
        if hour_dirs:
            for h in sorted(hour_dirs):
                parts.extend(sorted(h.glob("*.parquet")))
        else:
            parts.extend(sorted(ddir.glob("*.parquet")))
        if not parts:
            continue
        dfs = []
        for pth in parts:
            try:
                dfp = _pd.read_parquet(pth)
                dfs.append(dfp)
            except Exception:
                continue
        if not dfs:
            continue
        df = _pd.concat(dfs, ignore_index=True)
        # Normalize columns and order
        cols = ["symbol","start_ts","end_ts","open","high","low","close","volume","trades"]
        for c in cols:
            if c not in df.columns:
                df[c] = _pd.Series([None]*len(df))
        df = df[cols].copy()
        # Drop duplicates by start_ts keeping last, sort
        df = df.drop_duplicates(subset=["start_ts"], keep="last").sort_values("start_ts")
        total_rows += len(df)
        # Write out
        dest = out_base / f"symbol={symbol}" / f"interval={interval}" / f"date={dt}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(dest, index=False)
        outputs.append(str(dest))
    typer.echo(json.dumps({
        "symbol": symbol,
        "interval": interval,
        "dates": len(dates),
        "rows": total_rows,
        "outputs": outputs,
        "out_base": str(out_base),
    }, indent=2))
@app.command(name="build_bars")
def build_bars(
    symbol: str,
    interval: float = 1.0,
    date: str = typer.Option(None, help="Date partition YYYY-MM-DD (defaults today)"),
    write: bool = typer.Option(False, help="Persist resulting bars to parquet under data/derived/bars"),
    enforce_continuous: bool = typer.Option(False, help="Emit empty bars for gaps in time buckets"),
    watermark_delay: float = typer.Option(0.0, help="Delay (sec) before finalizing bars to allow late ticks"),
    max_lag_sec: float = typer.Option(None, help="Max age (sec) for accepting out-of-order ticks"),
    outlier_mode: str = typer.Option("winsorize", help='Outlier handling: "none", "winsorize", or "clip"'),
    outlier_window: int = typer.Option(50, help="Rolling window size for return stats (outlier handling)"),
    outlier_threshold: float = typer.Option(3.0, help="Threshold for outlier clipping/winsorization"),
    partition_hour: bool = typer.Option(True, "--partition-hour/--no-partition-hour", help="When writing bars, create hour=HH sub-partitions under each date"),
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
    agg = TimeBarAggregator(
        interval_sec=interval,
        enforce_continuous=enforce_continuous,
        watermark_delay=watermark_delay,
        max_lag_sec=max_lag_sec,
        outlier_mode=outlier_mode,
        outlier_window=outlier_window,
        outlier_threshold=outlier_threshold,
    )
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
        # Apply watermark finalization at file boundary using last tick ts if available
        if len(df) > 0:
            last_ts = df.iloc[-1].get("epoch")
            if isinstance(last_ts, (int, float)):
                bars_out.extend(agg.finalize_until(float(last_ts)))
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
            if partition_hour:
                # Group by hour from start_ts and write to hour=HH partitions
                from collections import defaultdict as _dd
                by_hour: dict[str, list[dict]] = _dd(list)
                for row in payload:
                    h = time.strftime("%H", time.gmtime(row["start_ts"]))
                    by_hour[h].append(row)
                total_rows = 0
                for h, rows in by_hour.items():
                    h_dir = bars_dir / f"hour={h}"
                    h_dir.mkdir(parents=True, exist_ok=True)
                    _df = _pd.DataFrame(rows)
                    file_path = h_dir / f"part-{int(time.time()*1000)}.parquet"
                    _df.to_parquet(file_path, index=False)
                    total_rows += len(_df)
                out["path"] = str(bars_dir)
                out["rows"] = total_rows
            else:
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
    window_len: int = typer.Option(60, help="Rolling window length to attach high/low/close arrays to market_ctx for regime filters"),
    signals_filter: str = typer.Option("auto", help="Filter signals: auto (all), entries (enter_*/exit_* only)"),
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
    # Normalize context to strategy expectation (close acts as lastPrice) and enrich with rolling windows + month
    rows = df.to_dict(orient="records")
    from collections import deque as _deque
    hw: _deque[float] = _deque(maxlen=max(1, window_len))
    lw: _deque[float] = _deque(maxlen=max(1, window_len))
    cw: _deque[float] = _deque(maxlen=max(1, window_len))
    def _get_month(r: dict) -> int:
        import datetime as _dt
        # Try epoch seconds
        ts = r.get("epoch") or r.get("timestamp") or r.get("time")
        try:
            if isinstance(ts, (int, float)):
                return int(_dt.datetime.utcfromtimestamp(float(ts)).month)
            if isinstance(ts, str):
                # Attempt ISO parse
                return int(_dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).month)
        except Exception:
            return 0
        return 0
    adapted = []
    for r in rows:
        h = float(r.get("high", r.get("close", 0.0)))
        l = float(r.get("low", r.get("close", 0.0)))
        c = float(r.get("close", 0.0))
        hw.append(h); lw.append(l); cw.append(c)
        adapted.append({
            "lastPrice": c,
            **r,
            "high_window": list(hw),
            "low_window": list(lw),
            "close_window": list(cw),
            "month": _get_month(r),
        })
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
    # Optional signal filtering wrapper
    if signals_filter.lower() in ("entries", "entry", "enter"):
        allowed = ("enter_long", "exit_long", "enter_short", "exit_short")
        base = strat
        class _Filtered:
            def evaluate(self, market_ctx):  # type: ignore[no-untyped-def]
                for s in base.evaluate(market_ctx):
                    name = getattr(s, "name", None)
                    if name in allowed:
                        yield s
        strat = _Filtered()  # type: ignore[assignment]
        engine = BacktestEngine(strat)  # type: ignore[arg-type]

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
@app.command(name="iv_surface_metrics")
def iv_surface_metrics(
    quotes_path: Path = typer.Argument(..., help="Parquet or JSON lines with option quotes"),
    target_ttm_days: float = typer.Option(None, help="Target maturity in days (choose nearest)"),
    rr_delta: float = typer.Option(0.25, help="Absolute delta for risk reversal and butterfly"),
    slope_delta: float = typer.Option(0.5, help="Center delta for skew slope (absolute)"),
    slope_width: float = typer.Option(0.1, help="Half-window width around delta for slope regression"),
    spot: float = typer.Option(None, help="Spot price for expected move"),
    days: float = typer.Option(None, help="Days for expected move horizon"),
    z: float = typer.Option(1.0, help="Z-score multiplier for expected move (1=1-sigma)"),
):
    """Compute IV surface metrics: ATM IV, RR, butterfly, skew slope, and expected move.

    quotes_path should contain rows with columns: right('C'/'P'), strike, iv, delta, optional ttm(years).
    """
    import pandas as _pd
    p = Path(quotes_path)
    if not p.exists():
        typer.echo(json.dumps({"ok": False, "error": f"not found: {quotes_path}"}, indent=2)); raise typer.Exit(1)
    # Load
    if p.suffix.lower() == ".parquet":
        df = _pd.read_parquet(p)
    else:
        try:
            # Try JSON lines
            df = _pd.read_json(p, lines=True)
        except Exception:
            df = _pd.read_json(p)
    if df.empty:
        typer.echo(json.dumps({"ok": False, "error": "no quotes"}, indent=2)); raise typer.Exit(1)

    # Coerce/rename columns best-effort
    def _col(*names: str):
        for n in names:
            if n in df.columns:
                return n
        return None
    c_right = _col("right", "cp", "side")
    c_strike = _col("strike", "k")
    c_iv = _col("iv", "implied_vol", "sigma")
    c_delta = _col("delta", "opt_delta")
    c_ttm = _col("ttm", "t_years", "maturity_years")
    if not (c_right and c_strike and c_iv):
        typer.echo(json.dumps({"ok": False, "error": "missing columns: right/strike/iv"}, indent=2)); raise typer.Exit(1)

    quotes: list[_IVQuote] = []
    for _, r in df.iterrows():
        try:
            quotes.append(_IVQuote(
                right=str(r[c_right]).upper(),
                strike=float(r[c_strike]),
                iv=float(r[c_iv]),
                delta=(float(r[c_delta]) if (c_delta and (r.get(c_delta) is not None)) else None),
                ttm=(float(r[c_ttm]) if (c_ttm and (r.get(c_ttm) is not None)) else None),
            ))
        except Exception:
            continue

    target_ttm_years = (float(target_ttm_days) / 365.0) if target_ttm_days else None
    atm = _atm_iv(quotes, target_ttm_years=target_ttm_years)
    rr = _rr(quotes, abs_delta=float(rr_delta), target_ttm_years=target_ttm_years)
    fly = _fly(quotes, abs_delta=float(rr_delta), target_ttm_years=target_ttm_years)
    slope = _slope(quotes, around_delta=float(slope_delta), width=float(slope_width), target_ttm_years=target_ttm_years)
    em = None
    if spot is not None and days is not None and atm is not None:
        em = _exp_move(float(spot), float(atm), float(days), z=float(z))
    payload = {
        "ok": True,
        "count": len(quotes),
        "atm_iv": atm,
        "risk_reversal": rr,
        "butterfly": fly,
        "skew_slope": slope,
        "expected_move": em,
        "params": {
            "target_ttm_days": target_ttm_days,
            "rr_delta": rr_delta,
            "slope_delta": slope_delta,
            "slope_width": slope_width,
            "spot": spot,
            "days": days,
            "z": z,
        }
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command(name="backtest_bars_realism")
def backtest_bars_realism(
    symbol: str,
    interval: float = 1.0,
    date: str = typer.Option(None, help="Date partition (defaults today)"),
    fast: int = typer.Option(5, help="Fast MA window"),
    slow: int = typer.Option(20, help="Slow MA window"),
    strategy: str = typer.Option("simple_intraday_spx", help="Strategy name"),
    window_len: int = typer.Option(60, help="Rolling window length to attach high/low/close arrays"),
    signals_filter: str = typer.Option("auto", help="Filter signals: auto (all), entries (enter_*/exit_* only)"),
    annualize_factor: float = typer.Option(0.0, help="If > 1, annualize Sharpe/Sortino using sqrt(factor)"),
    # Costs
    commission: float = typer.Option(0.0, help="Flat commission per round-trip trade"),
    slippage_bps: float = typer.Option(0.0, help="Slippage (bps) per side applied on entry and exit"),
    # Realism profile and overrides
    realism_profile: str = typer.Option("typical", help="none|low|typical|stress"),
    feed_latency_ms: float = typer.Option(None, help="Override feed latency (ms)"),
    order_delay_ms: float = typer.Option(None, help="Override order execution delay (ms)"),
    latency_jitter_ms: float = typer.Option(None, help="Override jitter (ms) applied to feed/order delays"),
    drop_bar_prob: float = typer.Option(None, help="Probability a bar is dropped (0..1)"),
    stale_bar_prob: float = typer.Option(None, help="Probability the observed bar is stale repeat (0..1)"),
    partial_fill_min: float = typer.Option(None, help="Minimum partial fill fraction (0..1)"),
    partial_fill_max: float = typer.Option(None, help="Maximum partial fill fraction (0..1)"),
    seed: int = typer.Option(42, help="RNG seed for reproducibility"),
):
    """Backtest with latency/realism settings to mirror live conditions."""
    from .backtest.engine import RealismConfig  # local import to avoid circulars at module import time
    settings = Settings()
    if date is None:
        date = time.strftime("%Y-%m-%d", time.gmtime())
    import pandas as _pd
    bars_dir = Path(settings.data_dir or "data") / "derived" / "bars" / f"symbol={symbol}" / f"interval={interval}" / f"date={date}"
    if not bars_dir.exists():
        typer.echo(json.dumps({"error": f"bars not found: {bars_dir}"}))
        raise typer.Exit(1)
    parts = sorted(bars_dir.glob("*.parquet"))
    if not parts:
        typer.echo(json.dumps({"error": "no bar parquet files"}))
        raise typer.Exit(1)
    df = _pd.concat([_pd.read_parquet(p) for p in parts], ignore_index=True)
    rows = df.to_dict(orient="records")
    # Build rolling context for strategy expectations
    from collections import deque as _deque
    hw: _deque[float] = _deque(maxlen=max(1, window_len))
    lw: _deque[float] = _deque(maxlen=max(1, window_len))
    cw: _deque[float] = _deque(maxlen=max(1, window_len))
    def _get_month(r: dict) -> int:
        import datetime as _dt
        ts = r.get("epoch") or r.get("timestamp") or r.get("time")
        try:
            if isinstance(ts, (int, float)):
                return int(_dt.datetime.utcfromtimestamp(float(ts)).month)
            if isinstance(ts, str):
                return int(_dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).month)
        except Exception:
            return 0
        return 0
    adapted: list[dict] = []
    for r in rows:
        c = float(r.get("close", r.get("lastPrice", 0.0)))
        h = float(r.get("high", c))
        l = float(r.get("low", c))
        hw.append(h); lw.append(l); cw.append(c)
        adapted.append({
            **r,
            "lastPrice": c,
            "high_window": list(hw),
            "low_window": list(lw),
            "close_window": list(cw),
            "month": _get_month(r),
            # Ensure a timestamp-like key exists for ordering in realism engine
            "timestamp": r.get("epoch") or r.get("end_ts") or r.get("start_ts") or r.get("timestamp") or r.get("time") or 0.0,
        })

    # Strategy instantiation
    from typing import cast, Any as _Any
    StratCls = get_strategy(strategy)
    strat = cast(_Any, _instantiate_strategy(StratCls, fast=fast, slow=slow))
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    # Optional entries filter
    if signals_filter.lower() in ("entries", "entry", "enter"):
        allowed = ("enter_long", "exit_long", "enter_short", "exit_short")
        base = strat
        class _Filtered:
            def evaluate(self, market_ctx):  # type: ignore[no-untyped-def]
                for s in base.evaluate(market_ctx):
                    name = getattr(s, "name", None)
                    if name in allowed:
                        yield s
        strat = _Filtered()  # type: ignore[assignment]
        engine = BacktestEngine(strat)  # type: ignore[arg-type]

    # Build realism config from profile + overrides
    prof = (realism_profile or "typical").lower()
    # default profiles (seconds)
    profiles = {
        "none": dict(feed_latency_secs=0.0, order_delay_secs=0.0, latency_jitter_secs=0.0, drop_bar_prob=0.0, stale_bar_prob=0.0, partial_fill_min=1.0, partial_fill_max=1.0),
        "low": dict(feed_latency_secs=0.050, order_delay_secs=0.050, latency_jitter_secs=0.020, drop_bar_prob=0.0, stale_bar_prob=0.01, partial_fill_min=0.9, partial_fill_max=1.0),
        "typical": dict(feed_latency_secs=0.100, order_delay_secs=0.120, latency_jitter_secs=0.050, drop_bar_prob=0.01, stale_bar_prob=0.02, partial_fill_min=0.7, partial_fill_max=1.0),
        "stress": dict(feed_latency_secs=0.300, order_delay_secs=0.400, latency_jitter_secs=0.150, drop_bar_prob=0.05, stale_bar_prob=0.10, partial_fill_min=0.4, partial_fill_max=0.9),
    }
    base_cfg = profiles.get(prof, profiles["typical"]).copy()
    # Apply overrides (ms->s conversions)
    if feed_latency_ms is not None:
        base_cfg["feed_latency_secs"] = max(0.0, float(feed_latency_ms) / 1000.0)
    if order_delay_ms is not None:
        base_cfg["order_delay_secs"] = max(0.0, float(order_delay_ms) / 1000.0)
    if latency_jitter_ms is not None:
        base_cfg["latency_jitter_secs"] = max(0.0, float(latency_jitter_ms) / 1000.0)
    if drop_bar_prob is not None:
        base_cfg["drop_bar_prob"] = max(0.0, min(1.0, float(drop_bar_prob)))
    if stale_bar_prob is not None:
        base_cfg["stale_bar_prob"] = max(0.0, min(1.0, float(stale_bar_prob)))
    if partial_fill_min is not None:
        base_cfg["partial_fill_min"] = max(0.0, min(1.0, float(partial_fill_min)))
    if partial_fill_max is not None:
        base_cfg["partial_fill_max"] = max(0.0, min(1.0, float(partial_fill_max)))
    cfg = RealismConfig(**base_cfg, seed=int(seed))

    af: float | None = annualize_factor if annualize_factor and annualize_factor > 1 else None
    # Call the dynamically-attached method to avoid static type issues
    _run_realism = getattr(engine, "run_with_realism")
    res = _run_realism(adapted, cfg, collect_signals=False, annualize_factor=af,
                       commission=commission, slippage_bps=slippage_bps)

    payload = {
        "symbol": symbol,
        "interval": interval,
        "date": date,
        "profile": prof,
        "signals": res.signals_emitted,
        "bars": res.bars_processed,
        "gross_pnl": res.gross_pnl,
        "commission_paid": res.commission_paid,
        "slippage_paid": res.slippage_paid,
        "pnl_after_slippage": getattr(res, "pnl_after_slippage", res.gross_pnl - res.slippage_paid),
        "total_pnl": res.total_pnl,
        "wins": res.wins,
        "losses": res.losses,
        "win_rate": res.win_rate,
        "max_drawdown": res.max_drawdown,
        "sharpe": res.sharpe,
        "sortino": res.sortino,
        "trade_count": res.trade_count,
        "avg_mae": res.avg_mae,
        "avg_mfe": res.avg_mfe,
        "calmar": res.calmar,
        "dropped_bars": getattr(res, "dropped_bars", 0),
        "stale_bars": getattr(res, "stale_bars", 0),
        "executed_orders": getattr(res, "executed_orders", 0),
        "partial_fills": getattr(res, "partial_fills", 0),
        "slippage_share_of_gross": getattr(res, "slippage_share_of_gross", 0.0),
        "annualize_factor": af or 0.0,
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command(name="iqfeed_greeks_live")
def iqfeed_greeks_live(
    underlying_price: float = typer.Argument(..., help="Current underlying price (numeric) used for ATM filtering"),
    risk_free: float = typer.Option(0.05, help="Risk free rate for delta backfill"),
    limit: int = typer.Option(20, help="Limit number of quotes returned (after filtering)"),
    json_only: bool = typer.Option(True, help="Emit JSON (always true currently; placeholder for future formatting)"),
    live_only: bool = typer.Option(False, help="If set, fail fast (no output) when live greeks unavailable"),
    buckets: str = typer.Option("0.25,0.50", help="Comma list of abs-delta buckets for skew metric"),
    tol: float = typer.Option(0.05, help="Delta bucket tolerance"),
    scale: float = typer.Option(0.20, help="Skew normalization scale (-1..1 from avg_spread/scale)"),
    persist: bool = typer.Option(False, help="Persist snapshot to metrics log (category=greeks_live)"),
    parquet_out: Path = typer.Option(None, help="Optional path to write quotes parquet; overrides default location"),
    write_parquet: bool = typer.Option(False, help="If set, write quotes parquet to data/derived/greeks_live/date=YYYY-MM-DD/"),
):
    """Attempt a live (best-effort) near-money chain greeks snapshot via IQFeed Level1 fundamentals.

    Falls back to simulation if live chain or fundamentals not available.
    """
    from .datafeeds.iqfeed_options import IQFeedOptionsGreeks, skew_signal_from_chain
    og = IQFeedOptionsGreeks()
    quotes = og.fetch_chain_greeks(underlying_price, risk_free=risk_free) or []
    provenance = getattr(og, "last_source", None)
    if live_only and provenance != "live":
        typer.echo(json.dumps({
            "underlying_price": underlying_price,
            "status": "no-live-data",
            "message": "Live greeks unavailable; retry when IQFeed is running with entitlements.",
        }, indent=2))
        return
    subset = quotes[:limit]
    # Parse buckets
    b_list: list[float] = []
    if buckets.strip():
        for part in buckets.split(','):
            p = part.strip()
            if not p:
                continue
            try:
                b_list.append(float(p))
            except ValueError:
                continue
    skew = skew_signal_from_chain(subset, buckets=b_list or None, tol=tol, scale=scale)
    payload = {
        "underlying_price": underlying_price,
        "risk_free": risk_free,
        "count": len(quotes),
        "returned": len(subset),
        "skew": skew,
        "quotes": [
            {
                "symbol": q.symbol,
                "right": q.right,
                "strike": q.strike,
                "delta": q.delta,
                "iv": q.iv,
            } for q in subset
        ],
        "provenance": provenance or "unknown",
        "buckets": b_list or [0.25, 0.5],
        "tol": tol,
        "scale": scale,
    }
    if persist:
        try:
            from .utils.metrics_store import append_metric as _append_metric
            settings = Settings()
            _append_metric(settings.data_dir or "data", "greeks_live", payload)
        except Exception as _exc:  # noqa: BLE001
            LOGGER.debug("Persist greeks_live failed: %s", _exc)
    # Optional parquet dump of quotes
    if write_parquet or parquet_out is not None:
        try:
            import pandas as _pd
            from pathlib import Path as _P
            settings = Settings()
            base = _P(settings.data_dir or "data")
            if parquet_out is None:
                date = time.strftime("%Y-%m-%d", time.gmtime())
                out_dir = base / "derived" / "greeks_live" / f"date={date}"
                out_dir.mkdir(parents=True, exist_ok=True)
                fname = f"part-{int(time.time()*1000)}.parquet"
                target = out_dir / fname
            else:
                target = _P(parquet_out)
                target.parent.mkdir(parents=True, exist_ok=True)
            now_ts = time.time()
            rows = [{"ts": now_ts, "symbol": q.symbol, "right": q.right, "strike": q.strike, "delta": q.delta, "iv": q.iv,
                     "underlying_price": underlying_price, "risk_free": risk_free, "provenance": provenance or "unknown"} for q in subset]
            if rows:
                _pd.DataFrame(rows).to_parquet(target, index=False)  # type: ignore[arg-type]
                payload["parquet_path"] = str(target)
        except Exception as _exc:  # noqa: BLE001
            LOGGER.debug("Write greeks parquet failed: %s", _exc)

    # Always emit JSON payload for CLI consumers/tests
    if json_only:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload))


@app.command(name="plot_skew")
def plot_skew(
    date: str = typer.Option(None, help="Date YYYY-MM-DD; defaults to today"),
    out_png: Path = typer.Option(None, help="Optional path for output PNG; defaults under data/plots"),
    use_compact: bool = typer.Option(True, help="Use compacted metrics parquet from metrics_compact/skew"),
):
    """Plot skew_score over time for a given day (reads metrics -> compact parquet)."""
    import pandas as _pd
    import time as _t
    settings = Settings()
    if date is None:
        date = _t.strftime("%Y-%m-%d", _t.gmtime())
    # Ensure compact parquet exists
    from .utils.metrics_store import compact_metrics as _compact
    p = _compact(settings.data_dir or "data", "skew", date)
    if p is None:
        typer.echo(json.dumps({"error": "no skew data for date", "date": date}))
        return
    df = _pd.read_parquet(p)
    if "ts" not in df.columns:
        typer.echo(json.dumps({"error": "no ts column in skew metrics", "path": str(p)}))
        return
    df_sorted = df.sort_values("ts")
    # Plot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 5))
    # y-series: prefer nested metrics.skew_score if present; otherwise top-level skew_score
    if "metrics" in df_sorted.columns:
        y = df_sorted["metrics"].apply(lambda m: (m.get("skew_score") if isinstance(m, dict) else None))
    elif "skew_score" in df_sorted.columns:
        y = df_sorted["skew_score"]
    else:
        typer.echo(json.dumps({"error": "no skew_score available in metrics", "path": str(p)}))
        return
    ax.plot(df_sorted["ts"], y, label="skew_score")
    # Try to plot per-bucket spreads if available as top-level columns
    for col in [c for c in df_sorted.columns if c.startswith("spread_") and c.endswith("d")]:
        ax.plot(df_sorted["ts"], df_sorted[col], label=col, alpha=0.6)
    ax.set_title(f"Skew timeseries {date}")
    ax.set_xlabel("timestamp (epoch seconds)")
    ax.set_ylabel("value")
    ax.legend()
    # Output path
    if out_png is None:
        from pathlib import Path as _P
        out_dir = _P(settings.data_dir or "data") / "plots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_png = out_dir / f"skew_{date}.png"
    fig.tight_layout()
    fig.savefig(str(out_png))
    typer.echo(json.dumps({"status": "ok", "png": str(out_png), "rows": int(len(df_sorted))}, indent=2))


@app.command(name="plot_greeks")
def plot_greeks(
    parquet_path: Path = typer.Argument(..., help="Parquet file with greeks quotes (output from iqfeed_greeks_live --write-parquet)"),
    out_png: Path = typer.Option(None, help="Optional path for output PNG; defaults under data/plots"),
):
    """Plot IV vs strike scatter from a greeks quotes parquet file."""
    import pandas as _pd
    df = _pd.read_parquet(parquet_path)
    if df.empty:
        typer.echo(json.dumps({"error": "no rows in parquet", "path": str(parquet_path)}))
        return
    # Plot
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 6))
    for side, marker, color in [("C", "o", "tab:blue"), ("P", "x", "tab:orange")]:
        sdf = df[df.right == side]
        if not sdf.empty:
            ax.scatter(sdf["strike"], sdf["iv"], label=f"{side}", marker=marker, alpha=0.8, color=color)
    up = df["underlying_price"].iloc[0] if "underlying_price" in df.columns and not df.empty else None
    ax.set_title(f"IV vs Strike (S={up})" if up else "IV vs Strike")
    ax.set_xlabel("Strike")
    ax.set_ylabel("IV")
    ax.grid(True, linestyle=":", alpha=0.4)
    ax.legend()
    # Output path
    from pathlib import Path as _P
    if out_png is None:
        settings = Settings()
        out_dir = _P(settings.data_dir or "data") / "plots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_png = out_dir / "greeks_iv_strike.png"
    fig.tight_layout()
    fig.savefig(str(out_png))
    typer.echo(json.dumps({"status": "ok", "png": str(out_png), "rows": int(len(df))}, indent=2))


@app.command(name="greeks_schedule")
def greeks_schedule(
    underlying_price: float = typer.Argument(..., help="Underlying price (float) to seed live/Sim chain fetch"),
    times: str = typer.Option("09:30", help="Comma HH:MM run times in chosen timezone (24h) e.g. '09:30,12:00,15:45'"),
    tz: str = typer.Option("America/New_York", help="IANA timezone for schedule times (uses local if unavailable)"),
    max_runs: int = typer.Option(0, help="If >0, stop after this many executions"),
    repeat_daily: bool = typer.Option(False, help="If set, keep running across days for the provided times"),
    mode: str = typer.Option("composite", help="greeks_factor mode: rr|fly|slope|composite"),
    # RR params
    target_abs_delta: float = typer.Option(0.25, help="Target abs delta for RR & butterfly wings"),
    tol: float = typer.Option(0.05, help="Delta tolerance for bucket selection"),
    scale: float = typer.Option(0.20, help="Normalization scale for rr/fly/slope components"),
    # Butterfly
    atm_abs_delta: float = typer.Option(0.5, help="ATM abs delta for butterfly"),
    # Slope
    slope_outer: float = typer.Option(0.10, help="Outer delta for slope"),
    slope_inner: float = typer.Option(0.25, help="Inner delta for slope"),
    # Composite
    w_rr: float = typer.Option(0.6, help="RR weight"),
    w_fly: float = typer.Option(0.2, help="Butterfly weight"),
    w_slope: float = typer.Option(0.2, help="Slope weight"),
    rr_scale: float = typer.Option(0.20, help="RR scale"),
    fly_scale: float = typer.Option(0.20, help="Butterfly scale"),
    slope_scale: float = typer.Option(0.20, help="Slope scale"),
):
    """Capture greeks_norm snapshots at set times and persist into metrics (category=greeks_factor).

    Uses live IQFeed greeks when available; falls back to simulation automatically.
    Persists with ts set to the scheduled time (UTC epoch) and date partition based on the chosen timezone's date.
    """
    from .datafeeds.iqfeed_options import IQFeedOptionsGreeks
    import time as _t
    import datetime as _dt
    # Resolve timezone (best-effort)
    tzinfo = None
    if tz:
        try:
            from zoneinfo import ZoneInfo  # py>=3.9
            tzinfo = ZoneInfo(tz)
        except Exception:
            tzinfo = None
    def _now_tz():
        return _dt.datetime.now(tzinfo) if tzinfo else _dt.datetime.now()
    def _to_utc_epoch(dt_local: _dt.datetime) -> float:
        if dt_local.tzinfo is None:
            # Treat naive as local time; map to UTC via timestamp()
            return dt_local.timestamp()
        return dt_local.astimezone(_dt.timezone.utc).timestamp()
    # Parse times list
    run_hm: list[tuple[int, int]] = []
    for part in [p.strip() for p in times.replace(";", ",").split(",") if p.strip()]:
        try:
            hh, mm = part.split(":", 1)
            run_hm.append((int(hh), int(mm)))
        except Exception:
            continue
    if not run_hm:
        typer.echo(json.dumps({"ok": False, "error": "No valid times provided"}, indent=2))
        raise typer.Exit(1)
    # Prepare kwargs for compute
    m = (mode or "composite").lower()
    kwargs = {}
    if m == "rr":
        kwargs = {"target_abs_delta": target_abs_delta, "tol": tol, "scale": scale}
    elif m == "fly":
        kwargs = {"target_abs_delta": target_abs_delta, "atm_abs_delta": atm_abs_delta, "tol": tol, "scale": scale}
    elif m == "slope":
        kwargs = {"outer_delta": slope_outer, "inner_delta": slope_inner, "tol": tol, "scale": scale}
    else:  # composite
        kwargs = {
            "rr_delta": target_abs_delta,
            "fly_delta": target_abs_delta,
            "atm_abs_delta": atm_abs_delta,
            "slope_outer": slope_outer,
            "slope_inner": slope_inner,
            "tol": tol,
            "rr_scale": rr_scale,
            "fly_scale": fly_scale,
            "slope_scale": slope_scale,
            "w_rr": w_rr,
            "w_fly": w_fly,
            "w_slope": w_slope,
        }
    settings = Settings()
    og = IQFeedOptionsGreeks()
    runs_done = 0
    # Helper: for given date, compute schedule datetimes
    def _schedule_for_date(base_date: _dt.date):
        out: list[_dt.datetime] = []
        for h, m_ in run_hm:
            try:
                dt_local = _dt.datetime(base_date.year, base_date.month, base_date.day, h, m_, tzinfo=tzinfo)
            except Exception:
                dt_local = _dt.datetime(base_date.year, base_date.month, base_date.day, h, m_)
            out.append(dt_local)
        return sorted(out)
    current_date = _now_tz().date()
    schedule = _schedule_for_date(current_date)
    results = []
    while True:
        now = _now_tz()
        # Find the next due time
        next_dt = next((dt for dt in schedule if dt > now), None)
        if next_dt is None:
            if repeat_daily:
                # move to next day
                current_date = current_date + _dt.timedelta(days=1)
                schedule = _schedule_for_date(current_date)
                continue
            else:
                break
        # Sleep until next time (with small checks to allow Ctrl+C)
        while True:
            now = _now_tz()
            delta = (next_dt - now).total_seconds()
            if delta <= 0:
                break
            _t.sleep(min(1.0, max(0.1, delta / 10.0)))
        # Execute snapshot
        try:
            chain = og.fetch_chain_greeks(underlying_price) or []
            res = compute_greeks_norm(chain, mode=mode, **kwargs)
            gn = float(res.get("greeks_norm", 0.0))
            ts_utc = _to_utc_epoch(next_dt)
            date_part = str(next_dt.date())
            payload = {
                "underlying_price": underlying_price,
                "mode": mode,
                "provenance": og.last_source or "unknown",
                **res,
            }
            try:
                append_metric(settings.data_dir or "data", "greeks_factor", payload, date=date_part, ts=ts_utc)  # type: ignore[arg-type]
            except Exception as _exc:  # noqa: BLE001
                LOGGER.debug("Persist greeks_factor (schedule) failed: %s", _exc)
            out = {"when_local": next_dt.isoformat(), "ts": ts_utc, "date": date_part, "greeks_norm": gn, "mode": mode, "provenance": og.last_source}
            results.append(out)
            typer.echo(json.dumps(out))
            runs_done += 1
            if max_runs and runs_done >= max_runs:
                break
        except KeyboardInterrupt:
            break
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("greeks_schedule execution failed: %s", exc)
            # proceed to next
            runs_done += 1
            if max_runs and runs_done >= max_runs:
                break
    # Final summary
    typer.echo(json.dumps({"status": "completed", "runs": runs_done, "emitted": len(results)}, indent=2))


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


@app.command(name="compact_greeks_live")
def compact_greeks_live(date: str = typer.Option(None, help="Date (YYYY-MM-DD)")):
    """Compact the 'greeks_live' metrics category into parquet for a given day."""
    settings = Settings()
    path = compact_metrics(settings.data_dir or "data", "greeks_live", date)
    if path is None:
        typer.echo(json.dumps({"category": "greeks_live", "date": date, "status": "no-data"}, indent=2))
    else:
        typer.echo(json.dumps({"category": "greeks_live", "date": date, "status": "ok", "path": str(path)}, indent=2))


@app.command(name="compact_skew")
def compact_skew(date: str = typer.Option(None, help="Date (YYYY-MM-DD)")):
    """Compact the 'skew' metrics category into parquet for a given day."""
    settings = Settings()
    path = compact_metrics(settings.data_dir or "data", "skew", date)
    if path is None:
        typer.echo(json.dumps({"category": "skew", "date": date, "status": "no-data"}, indent=2))
    else:
        typer.echo(json.dumps({"category": "skew", "date": date, "status": "ok", "path": str(path)}, indent=2))


@app.command(name="greeks_provenance_compact")
def greeks_provenance_compact(
    start_date: str = typer.Option(None, help="Filter partitions from this date (YYYY-MM-DD) inclusive"),
    end_date: str = typer.Option(None, help="Filter partitions up to this date (YYYY-MM-DD) inclusive"),
    out: Path = typer.Option(None, help="Optional output path; suffix determines format (.parquet/.csv/.json)"),
    format: str = typer.Option(None, help="Explicit output format: parquet|csv|json; overrides suffix"),
    group_by: str = typer.Option("symbol,interval,greeks_source_used", help="Comma-separated columns to group by for summary"),
    grouped_out: Path = typer.Option(None, help="Optional grouped summary output path (counts/bars by group)"),
    grouped_format: str = typer.Option(None, help="Explicit format for grouped_out: parquet|csv|json"),
):
    """Compact derived provenance parquet files into a single aggregated dataset.

    Reads files under data/derived/provenance/greeks_source/date=YYYY-MM-DD/ and concatenates
    the per-day rows. Adds symbol/interval by parsing file names (prov_<symbol>_int=<interval>_*).
    Deduplicates on (symbol, interval, date) choosing:
      - bars: max
      - persisted_available: any(True)
      - greeks_records: max
      - greeks_source_used: mode with tie-breaker persisted > compute > proxy > none
    """
    from pathlib import Path as _P
    import re as _re
    import pandas as _pd
    from datetime import datetime as _dt

    settings = Settings()
    base = _P(settings.data_dir or "data") / "derived" / "provenance" / "greeks_source"
    if not base.exists():
        typer.echo(json.dumps({"status": "no-base", "path": str(base)}))
        return

    # Determine partitions to scan
    def _in_range(part_date: str) -> bool:
        if not start_date and not end_date:
            return True
        try:
            d = _dt.strptime(part_date, "%Y-%m-%d").date()
            if start_date:
                sd = _dt.strptime(start_date, "%Y-%m-%d").date()
                if d < sd:
                    return False
            if end_date:
                ed = _dt.strptime(end_date, "%Y-%m-%d").date()
                if d > ed:
                    return False
            return True
        except Exception:
            return False

    parts: list[_P] = []
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("date="):
            part_date = child.name.split("=", 1)[1]
            if _in_range(part_date):
                parts.append(child)
    parts = sorted(parts)
    if not parts:
        typer.echo(json.dumps({"status": "no-partitions", "path": str(base), "start_date": start_date, "end_date": end_date}, indent=2))
        return

    rows: list[_pd.DataFrame] = []
    rx = _re.compile(r"prov_(?P<symbol>.+?)_int=(?P<int>[0-9.]+)_")
    files_scanned = 0
    for pdir in parts:
        for f in pdir.glob("*.parquet"):
            try:
                df = _pd.read_parquet(f)
                # Add partition and file-derived metadata
                m = rx.search(f.name)
                symbol = m.group("symbol") if m else None
                interval = float(m.group("int")) if (m and m.group("int")) else None
                df = df.copy()
                df["partition_date"] = pdir.name.split("=", 1)[1]
                if symbol is not None:
                    df["symbol"] = symbol
                if interval is not None:
                    df["interval"] = interval
                rows.append(df)
                files_scanned += 1
            except Exception:
                continue

    if not rows:
        typer.echo(json.dumps({"status": "no-rows", "files_scanned": files_scanned}, indent=2))
        return

    df_all = _pd.concat(rows, ignore_index=True)
    # Ensure symbol/interval columns exist (fallback values if missing)
    if "symbol" not in df_all.columns:
        df_all["symbol"] = "UNKNOWN"
    if "interval" not in df_all.columns:
        df_all["interval"] = 1.0

    # Ensure a 'date' column exists for grouping. Prefer an existing 'date' column; otherwise
    # fall back to the partition label if available. Creating the column even for empty frames
    # avoids KeyError from groupby on missing keys.
    if "date" not in df_all.columns:
        if "partition_date" in df_all.columns:
            df_all["date"] = df_all["partition_date"]
        else:
            # Create an empty 'date' column to satisfy the groupby signature
            df_all["date"] = _pd.Series(dtype="object")

    # Backfill expected metric columns if missing so aggregation doesn't fail on empty frames
    if "bars" not in df_all.columns:
        df_all["bars"] = _pd.Series(dtype="int64")
    if "greeks_records" not in df_all.columns:
        df_all["greeks_records"] = _pd.Series(dtype="int64")
    if "greeks_source_used" not in df_all.columns:
        df_all["greeks_source_used"] = _pd.Series(dtype="object")
    if "persisted_available" not in df_all.columns:
        df_all["persisted_available"] = _pd.Series(dtype="bool")

    # Deduplicate and aggregate
    priority = {"persisted": 3, "compute": 2, "proxy": 1, "none": 0}
    def _mode_with_priority(vals: _pd.Series) -> str:
        try:
            vc = vals.value_counts()
            if vc.empty:
                return "none"
            top_count = vc.iloc[0]
            candidates = [v for v, c in vc.items() if c == top_count]
            candidates.sort(key=lambda v: priority.get(str(v), -1), reverse=True)
            return str(candidates[0])
        except Exception:
            return "none"

    # Convert persisted_available to bool
    if "persisted_available" in df_all.columns:
        df_all["persisted_available"] = df_all["persisted_available"].astype(bool)

    grouped = df_all.groupby(["symbol", "interval", "date"], as_index=False).agg({
        "bars": "max",
        "persisted_available": "max" if "persisted_available" in df_all.columns else (lambda s: False),
        "greeks_records": "max" if "greeks_records" in df_all.columns else (lambda s: 0),
        "greeks_source_used": _mode_with_priority if "greeks_source_used" in df_all.columns else (lambda s: "none"),
    })

    # Summary stats
    try:
        days_by_source = grouped["greeks_source_used"].value_counts().to_dict()
    except Exception:
        days_by_source = {}
    try:
        bars_by_source = grouped.groupby("greeks_source_used")["bars"].sum().to_dict()
    except Exception:
        bars_by_source = {}
    persisted_days = int(grouped["persisted_available"].sum()) if "persisted_available" in grouped.columns else 0
    greeks_records_total = int(grouped["greeks_records"].sum()) if "greeks_records" in grouped.columns else 0

    result = {
        "status": "ok",
        "input_partitions": len(parts),
        "files_scanned": files_scanned,
        "rows_in": int(len(df_all)),
        "rows_out": int(len(grouped)),
        "days_by_source": days_by_source,
        "bars_by_source": bars_by_source,
        "persisted_days": persisted_days,
        "greeks_records_total": greeks_records_total,
    }

    # Write output
    if out is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            chosen_fmt = (format or "").strip().lower() if format else None
            suf = out.suffix.lower()
            fmt_to_suffix = {"json": ".json", "parquet": ".parquet", "csv": ".csv"}
            if chosen_fmt in fmt_to_suffix:
                desired = fmt_to_suffix[chosen_fmt]
                if suf != desired:
                    out = out.with_suffix(desired)
                    result["export_note"] = "Output path suffix adjusted to match --format"
                suf = desired
            if suf == ".parquet":
                grouped.to_parquet(out, index=False)
            elif suf == ".csv":
                grouped.to_csv(out, index=False)
            else:
                # Embed data into JSON payload
                result["data"] = grouped.to_dict(orient="records")
                out.write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["out_path"] = str(out)
        except Exception as _exc:  # noqa: BLE001
            result["export_error"] = str(_exc)
            typer.echo(json.dumps(result, indent=2))
            return
    else:
        # Default location
        def _default_name() -> _P:
            rng = "all"
            if start_date or end_date:
                sd = start_date or "min"
                ed = end_date or "max"
                rng = f"{sd}_to_{ed}"
            out_dir = base.parent / "greeks_source_compact"
            out_dir.mkdir(parents=True, exist_ok=True)
            return out_dir / f"prov_compact_{rng}.parquet"
        target = _default_name()
        try:
            grouped.to_parquet(target, index=False)
            result["out_path"] = str(target)
        except Exception as _exc:  # noqa: BLE001
            result["export_error"] = str(_exc)
            result["hint"] = "Specify --out with .csv or .json if parquet write is unavailable"
            typer.echo(json.dumps(result, indent=2))
            return

    # Optional grouped summary output
    if group_by is None:
        group_cols = []
    else:
        group_cols = [c.strip() for c in group_by.split(",") if c.strip()]
    # Retain only valid columns
    group_cols = [c for c in group_cols if c in grouped.columns]
    result["group_by"] = group_cols
    grouped_preview = None
    if group_cols:
        try:
            df_sum = grouped.groupby(group_cols, as_index=False).agg({
                "date": "count",
                "bars": "sum",
                "persisted_available": "sum" if "persisted_available" in grouped.columns else (lambda s: 0),
                "greeks_records": "sum" if "greeks_records" in grouped.columns else (lambda s: 0),
            })
            df_sum = df_sum.rename(columns={"date": "days", "bars": "bars_total", "persisted_available": "persisted_days", "greeks_records": "greeks_records_total"})

            if grouped_out is not None:
                try:
                    grouped_out.parent.mkdir(parents=True, exist_ok=True)
                    chosen_fmt2 = (grouped_format or "").strip().lower() if grouped_format else None
                    suf2 = grouped_out.suffix.lower()
                    fmt_to_suffix2 = {"json": ".json", "parquet": ".parquet", "csv": ".csv"}
                    if chosen_fmt2 in fmt_to_suffix2:
                        desired2 = fmt_to_suffix2[chosen_fmt2]
                        if suf2 != desired2:
                            grouped_out = grouped_out.with_suffix(desired2)
                            result["grouped_export_note"] = "Grouped out suffix adjusted to match --grouped-format"
                        suf2 = desired2
                    if suf2 == ".parquet":
                        df_sum.to_parquet(grouped_out, index=False)
                    elif suf2 == ".csv":
                        df_sum.to_csv(grouped_out, index=False)
                    else:
                        # write JSON containing records only
                        grouped_out.write_text(json.dumps(df_sum.to_dict(orient="records"), indent=2), encoding="utf-8")
                    result["grouped_out_path"] = str(grouped_out)
                except Exception as _exc:  # noqa: BLE001
                    result["grouped_export_error"] = str(_exc)
            # Always include a tiny preview in the JSON (first 50 rows)
            grouped_preview = df_sum.head(50).to_dict(orient="records")
        except Exception as _exc:  # noqa: BLE001
            result["grouped_error"] = str(_exc)
    if grouped_preview is not None:
        result["grouped_preview"] = grouped_preview

    typer.echo(json.dumps(result, indent=2))


@app.command(name="iqfeed_diagnostics")
def iqfeed_diagnostics(
    symbol: str = typer.Option("@SPX.X", help="Symbol to sanity-check Level1 stream"),
    root: str = typer.Option("SPX.XO", help="Option root for chain check (e.g. SPX or SPX.XO)"),
    field_max_lines: int = typer.Option(80, help="Max lines to read when requesting fieldnames"),
    stream_secs: float = typer.Option(2.0, help="Seconds to wait for stream messages"),
    quick: bool = typer.Option(False, help="Skip stream test and only run socket/field/chain checks"),
):
    """Run IQFeed connectivity diagnostics: ping, fieldnames, chain, and optional stream sanity."""
    from .datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig, IQFeedLevel1Stream
    from .datafeeds.iqfeed_fieldmap import request_fieldnames
    from .datafeeds.iqfeed_chains import request_equity_index_chain
    from .datafeeds.iqfeed_sockets import IQSocket, DEFAULT_PORTS, L1Stream
    import time as _t

    cfg = IQFeedConfig.from_env()
    client = IQFeedClient(cfg)

    # 1) Ping
    try:
        ping_ok = client.ping()
    except Exception as exc:  # noqa: BLE001
        ping_ok = False

    # 2) Fieldnames
    try:
        fields = request_fieldnames(max_lines=field_max_lines)
        upd_names = fields.get("update", []) or []
        fund_names = fields.get("fundamental", []) or []
    except Exception as exc:  # noqa: BLE001
        upd_names = []
        fund_names = []

    # 3) Chain
    raw_chain_sample: list[str] = []
    try:
        calls, puts = request_equity_index_chain(root, call_put="pc", months="", strikes_filter="", near=1, include_weeklies=True)
    except Exception as exc:  # noqa: BLE001
        calls, puts = [], []
    # If chain empty, capture a few raw CEO lines for visibility
    if not calls and not puts:
        try:
            cmd = f"CEO,{root},pc,,,,W,2,0,DIAGTAG{root}"
            with IQSocket(port=DEFAULT_PORTS["lookup"]) as lk:
                lk.send(cmd)
                for _ in range(12):  # read up to 12 lines
                    line = lk.readline()
                    if not line:
                        break
                    if line.startswith("C") or line.startswith("S,") or line.startswith("E,"):
                        raw_chain_sample.append(line[:180])
                    if line.endswith("!ENDMSG!"):
                        break
        except Exception:
            pass

    # 4) Stream (optional)
    stream_info: dict = {"tested": False}
    if not quick:
        stream_info["tested"] = True
        try:
            msgs = []
            def _cb(m):
                if m.get("symbol") == symbol:
                    msgs.append(m)
            stream = IQFeedLevel1Stream(cfg, on_message=_cb, heartbeat_secs=3.0, reconnect_idle_secs=10.0)
            stream.start([symbol])
            start = _t.time()
            while (_t.time() - start) < stream_secs and len(msgs) < 3:
                m = stream.get(timeout=0.25)
                if m and m.get("symbol") == symbol:
                    if m not in msgs:
                        msgs.append(m)
            stream.stop()
            keys_seen = sorted({k for m in msgs for k in m.keys()})
            stream_info.update({
                "symbol": symbol,
                "received": len(msgs),
                "keys": keys_seen[:40],
            })
            # If no parsed messages, try raw Level1 sampling to reveal entitlement or error lines
            if stream_info.get("received", 0) == 0:
                raw_lines: list[str] = []
                l1 = L1Stream()
                l1.start()
                try:
                    l1.send(f"w{symbol}")
                    t0 = _t.time()
                    while (_t.time() - t0) < stream_secs and len(raw_lines) < 5:
                        for ln in l1.lines(max_items=10):
                            if ln:
                                raw_lines.append(ln[:180])
                                if len(raw_lines) >= 5:
                                    break
                        _t.sleep(0.05)
                    l1.send(f"r{symbol}")
                finally:
                    l1.stop()
                if raw_lines:
                    stream_info["raw_sample"] = raw_lines
        except Exception as exc:  # noqa: BLE001
            stream_info.update({"error": str(exc)})

    issues: list[str] = []
    if not ping_ok:
        issues.append("ping_failed")
    if not upd_names:
        issues.append("update_fieldnames_missing")
    if not fund_names:
        issues.append("fundamental_fieldnames_missing")
    if not calls and not puts:
        issues.append("chain_symbols_empty")
    if stream_info.get("tested") and stream_info.get("received", 0) == 0:
        issues.append("stream_no_messages")

    suggestions: list[str] = []
    if issues:
        suggestions.append("Ensure IQFeed Desktop (iqconnect.exe) is running and logged in")
        suggestions.append("Confirm Options entitlement for chains/greeks and Level1 is active")
        suggestions.append("Check Windows Firewall allows localhost ports (default 5009)")
        suggestions.append("If ports are customized, set IQFEED_PORT_LEVEL1 and IQFEED_PORT_LOOKUP in .env")

    typer.echo(json.dumps({
        "ping": ping_ok,
        "fieldnames": {"update_count": len(upd_names), "fundamental_count": len(fund_names)},
        "chain": {"root": root, "calls": len(calls), "puts": len(puts), "raw_sample": raw_chain_sample[:5] if raw_chain_sample else []},
        "stream": stream_info,
        "issues": issues,
        "suggestions": suggestions,
    }, indent=2))


@app.command(name="iqfeed_watch_raw")
def iqfeed_watch_raw(symbol: str, secs: float = 5.0, max_lines: int = 50):
    """Collect raw Level1 lines for a symbol for a short window (diagnostics).

    Returns a JSON with a small sample of raw lines to verify watches are producing messages.
    """
    from .datafeeds.iqfeed_sockets import L1Stream
    import time as _t
    raw: list[str] = []
    l1 = L1Stream()
    l1.start()
    try:
        l1.send(f"w{symbol}")
        t0 = _t.time()
        while (_t.time() - t0) < secs and len(raw) < max_lines:
            for ln in l1.lines(max_items=100):
                if ln:
                    raw.append(ln)
                    if len(raw) >= max_lines:
                        break
            _t.sleep(0.02)
        l1.send(f"r{symbol}")
    finally:
        l1.stop()
    typer.echo(json.dumps({"symbol": symbol, "secs": secs, "count": len(raw), "lines": raw[:max_lines]}, indent=2))

@app.command(name="iqfeed_news_watch")
def iqfeed_news_watch(
    secs: float = typer.Option(60.0, help="Seconds to watch news stream"),
    max_lines: int = typer.Option(200, help="Max news messages to collect"),
    filter_fly: bool = typer.Option(False, help="If true, only keep lines containing 'FLY'"),
    out: str = typer.Option("logs/news_watch.jsonl", help="Output JSONL of news lines (ts,line)")
):
    """Stream IQFeed news headlines/stories for a short window.

    Writes JSONL rows with ts and raw line; optionally filter for 'FLY'.
    """
    from .datafeeds.iqfeed_sockets import NewsStream
    import time as _t
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _Path
    ns = NewsStream()
    ns.start()
    start = _t.time()
    lines: list[str] = []
    try:
        while (_t.time() - start) < secs and len(lines) < max_lines:
            for ln in ns.lines(max_items=100):
                if not ln:
                    continue
                if filter_fly and ("FLY" not in ln.upper()):
                    continue
                lines.append(ln)
            _t.sleep(0.05)
    finally:
        ns.stop()
    outp = _Path(out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps({"ts": _dt.now(_tz.utc).isoformat(), "line": ln}) + "\n")
    typer.echo(json.dumps({"ok": True, "count": len(lines), "out": str(outp)}, indent=2))

@app.command(name="news_summarize_agent")
def news_summarize_agent(
    jsonl_path: Path = typer.Argument(..., help="Path to news_watch JSONL (ts,line)"),
    contains: str = typer.Option("", help="Optional substring filter (case-insensitive). Empty = no filter"),
    max_items: int = typer.Option(200, help="Max lines to include in the prompt"),
    out_json: Path = typer.Option(None, help="Output JSON path for agent summary. Defaults to logs/news_agent_summary_<date>.json"),
    model: str = typer.Option("gpt-4o-mini", help="OpenAI chat model for summarization"),
):
    """Summarize captured news for the trading desk using OpenAI.

    - Reads JSONL rows {ts, line}
    - Optionally filters by substring (e.g., 'FLY')
    - Skips protocol lines (S,, O, T, etc.)
    - Builds a concise prompt and requests a summary
    """
    import os
    import json as _json
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        typer.echo(_json.dumps({"ok": False, "error": "OPENAI_API_KEY not set"}, indent=2)); raise typer.Exit(1)
    if not jsonl_path.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"file not found: {jsonl_path}"}, indent=2)); raise typer.Exit(1)
    rows: list[dict] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for ln in f:
            try:
                obj = _json.loads(ln)
                rows.append(obj)
            except Exception:
                continue
    # Filter and clean
    filt = contains.strip().lower()
    msgs: list[str] = []
    import re as _re
    src_counts: dict[str, int] = {}
    for r in rows:
        line = str(r.get("line", ""))
        if not line:
            continue
        # Skip protocol/time lines
        if line.startswith("S,") or line.startswith("T,") or line == "O":
            continue
        if filt and (filt not in line.lower()):
            continue
        # Extract vendor/source code like (CPZ), (CBW), (DT7), etc., if present
        m = _re.match(r"^\(([^\)]+)\)", line)
        if m:
            code = m.group(1).strip().upper()
            src_counts[code] = int(src_counts.get(code, 0)) + 1
        msgs.append(line)
        if len(msgs) >= max_items:
            break
    # Compose prompt
    if not msgs:
        summary_prompt = (
            "No news headlines captured after filtering."
        )
    else:
        bullet_lines = "\n".join(f"- {m[:500]}" for m in msgs)  # cap line length
        summary_prompt = (
            "You are a trading desk assistant. Review these headlines and summarize any items likely to affect SPX today. "
            "Group by theme (macro, mega-cap, sector) and call out tickers when obvious. Keep it to 6-10 bullets.\n\n" + bullet_lines
        )
    # Optional: compute a coarse sentiment score by averaging per-headline scores
    sent_score = 0.0
    sent_n = 0
    try:
        from .openai_client import OpenAIWrapper as _OW
        import os as _os
        _key = _os.getenv("OPENAI_API_KEY")
        ow = _OW(_key)
        # Score up to 50 headlines to bound latency
        for m in msgs[:50]:
            sc = ow.sentiment_score(m, retries=1)
            try:
                sent_score += float(sc)
                sent_n += 1
            except Exception:
                pass
    except Exception:
        sent_score = 0.0; sent_n = 0
    agg_score = (sent_score / sent_n) if sent_n > 0 else 0.0

    # Call OpenAI chat (same pattern as agent_demo fallback)
    ind_snapshot = {}
    directional_bias = {"bias": "neutral", "confidence": 0.0, "reasons": []}
    try:
        from openai import OpenAI
        org = os.getenv("OPENAI_ORG"); proj = os.getenv("OPENAI_PROJECT")
        try:
            if proj:
                client = OpenAI(api_key=key, project=proj)
            elif org:
                client = OpenAI(api_key=key, organization=org)
            else:
                client = OpenAI(api_key=key)
        except TypeError:
            client = OpenAI(api_key=key)
        # Fetch indicator snapshot for I:SPX to provide directional context
        try:
            from .datafeeds.polygon_client import PolygonClient, PolygonConfig
            poly = PolygonClient(PolygonConfig.from_env())
            ind_snapshot = poly.latest_indicator_snapshot("I:SPX") or {}
            if ind_snapshot:
                ind_lines = "\n".join([f"{k.upper()}: {v:.4f}" for k, v in ind_snapshot.items()])
                summary_prompt = (
                    "You are a trading desk assistant. Review these headlines and summarize any items likely to affect SPX today. "
                    "Group by theme (macro, mega-cap, sector) and call out tickers when obvious. Keep it to 6-10 bullets.\n\n"
                    f"Technical context (Polygon minute-based):\n{ind_lines}\n\n" + bullet_lines
                ) if msgs else (
                    "No news headlines captured after filtering." + (f"\nTechnical context:\n{ind_lines}" if ind_snapshot else "")
                )
                # Compute a simple directional bias from indicators (+ sentiment as a nudge)
                try:
                    def _safe_float(val):
                        try:
                            return float(val)
                        except Exception:
                            return None
                    macd = _safe_float(ind_snapshot.get("macd"))
                    macd_sig = _safe_float(ind_snapshot.get("macd_signal"))
                    rsi = _safe_float(ind_snapshot.get("rsi"))
                    ema = _safe_float(ind_snapshot.get("ema"))
                    close = _safe_float(ind_snapshot.get("close"))
                    conds_bull = []
                    conds_bear = []
                    if macd is not None and macd_sig is not None:
                        conds_bull.append(macd > macd_sig)
                        conds_bear.append(macd < macd_sig)
                    if rsi is not None:
                        conds_bull.append(rsi >= 50.0)
                        conds_bear.append(rsi <= 50.0)
                    if close is not None and ema is not None:
                        conds_bull.append(close >= ema)
                        conds_bear.append(close <= ema)
                    bull_score = sum(1 for c in conds_bull if c)
                    bear_score = sum(1 for c in conds_bear if c)
                    if bull_score > bear_score and bull_score > 0:
                        bias = "bull"
                        base_conf = bull_score / max(3, len(conds_bull))
                    elif bear_score > bull_score and bear_score > 0:
                        bias = "bear"
                        base_conf = bear_score / max(3, len(conds_bear))
                    else:
                        bias = "neutral"
                        base_conf = 0.0
                    # Sentiment nudge: +/- 0.1 at most depending on sign
                    try:
                        s = float(agg_score)
                    except Exception:
                        s = 0.0
                    if bias == "bull" and s > 0:
                        base_conf = min(1.0, base_conf + min(0.1, abs(s)))
                    elif bias == "bear" and s < 0:
                        base_conf = min(1.0, base_conf + min(0.1, abs(s)))
                    directional_bias = {
                        "bias": bias,
                        "confidence": round(float(base_conf), 3),
                        "reasons": [
                            f"MACD {'>' if (macd is not None and macd_sig is not None and macd > macd_sig) else '<='} signal" if (macd is not None and macd_sig is not None) else "MACD N/A",
                            f"RSI {rsi:.1f}" if rsi is not None else "RSI N/A",
                            f"Close {'>=' if (close is not None and ema is not None and close >= ema) else '<'} EMA" if (close is not None and ema is not None) else "EMA/Close N/A",
                            f"Sentiment {s:+.2f}",
                        ],
                    }
                except Exception:
                    directional_bias = {"bias": "neutral", "confidence": 0.0, "reasons": []}
        except Exception:
            pass
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": summary_prompt}],
            temperature=0.3,
            max_tokens=700,
        )
        text = resp.choices[0].message.content if getattr(resp, "choices", None) else ""
        ok = True
        error = None
    except Exception as exc:  # noqa: BLE001
        text = ""
        ok = False
        error = str(exc)
    # Attach minimal metadata for downstream gating
    import datetime as _dtm
    payload = {
        "ok": ok,
        "count_input": len(rows),
        "count_used": len(msgs),
        "filtered": bool(filt),
        "filter": filt,
        "model": model,
        "summary": text,
        "error": error,
    "sources": sorted([{ "code": k, "count": int(v) } for k, v in src_counts.items()], key=lambda x: (x["count"], x["code"]), reverse=True) ,
        "sentiment_score": float(agg_score),
        "sentiment_count": int(sent_n),
        "generated_at": _dtm.datetime.now(_dtm.timezone.utc).isoformat(),
        "indicators": ind_snapshot,
        "directional_bias": directional_bias,
    }
    out_dir = Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_json is None:
        import datetime as _dt
        d = _dt.datetime.now().strftime("%Y%m%d")
        out_json = out_dir / f"news_agent_summary_{d}.json"
    try:
        out_json.write_text(_json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass
    typer.echo(_json.dumps({"ok": ok, "out": str(out_json), "used": len(msgs)}, indent=2))


@app.command(name="morning_brief")
def morning_brief(
    out_md: Path = typer.Option(None, help="Output markdown path; defaults to logs/morning_brief_<date>.md"),
    include_news: bool = typer.Option(True, help="Include news summary if a news_watch JSONL exists (or can be summarized)"),
    news_jsonl: Path = typer.Option(None, help="Optional explicit path to logs/news_watch_YYYYMMDD*.jsonl"),
    model: str = typer.Option("gpt-4o-mini", help="OpenAI model used for neutral summary and news summarization"),
    fallback_symbol: str = typer.Option("SPY", help="If Polygon I:SPX is not authorized, fallback symbol for prev bar"),
    include_ranges: bool = typer.Option(True, help="Include IV-based expected move ranges for the session (VIX/VXN/RVX proxies)"),
    include_futures: bool = typer.Option(True, help="Include overnight futures snapshot from IQFeed (@ES#, @NQ#, @RTY#)"),
    include_nowcast: bool = typer.Option(True, help="Include a quick EWMA nowcast (prob up/down, magnitude proxy) over SPY minutes"),
    post_to_discord: bool = typer.Option(False, help="If set, post the morning brief to Discord via webhook"),
    discord_webhook_url: str = typer.Option(None, help="Discord webhook URL. If omitted, reads DISCORD_WEBHOOK_URL env var"),
    discord_username: str = typer.Option("ZeroDTE Morning", help="Username to display when posting to Discord"),
):
    """Generate a premarket morning brief with SPX key levels, indicators, and optional news summary.

    Contents:
    - Previous day high/low/close and classic pivot levels (P, R1, R2, S1, S2)
    - Indicator snapshot from Polygon minute bars (SMA/EMA/MACD/RSI, close)
    - Neutral OpenAI paragraph summarizing the feature context (if OPENAI_API_KEY set)
    - News summary and coarse sentiment (if logs/news_watch_*.jsonl available or provided)
    """
    import os as _os
    import json as _json
    from pathlib import Path as _P
    import datetime as _dtm
    from .datafeeds.polygon_client import PolygonClient, PolygonConfig
    settings = Settings()

    # Resolve output path
    if out_md is None:
        d = _dtm.datetime.now(_dtm.timezone.utc).astimezone().strftime("%Y%m%d")
        out_md = _P("logs") / f"morning_brief_{d}.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)

    # 1) Fetch previous aggregate for SPX (via Polygon) and compute pivot levels
    poly = PolygonClient(PolygonConfig.from_env())
    prev_raw = poly.last_trade_spx(use_cache=False) or {}
    # Fallback to e.g. SPY if not authorized/missing
    if not prev_raw or prev_raw.get("status") == "NOT_AUTHORIZED":
        prev_raw = poly.last_trade_symbol(fallback_symbol, use_cache=False) or {}
    h = l = c = None
    try:
        res = (prev_raw or {}).get("results") or []
        if res:
            row = res[0]
            h = float(row.get("h")) if row.get("h") is not None else None
            l = float(row.get("l")) if row.get("l") is not None else None
            c = float(row.get("c")) if row.get("c") is not None else None
    except Exception:
        h = l = c = None
    piv = {}
    if all(isinstance(x, (int, float)) for x in (h, l, c)) and h is not None and l is not None and c is not None:
        P = (h + l + c) / 3.0
        R1 = 2 * P - l
        S1 = 2 * P - h
        R2 = P + (h - l)
        S2 = P - (h - l)
        piv = {"pivot": P, "R1": R1, "S1": S1, "R2": R2, "S2": S2}

    # 2) Indicator snapshot (minute-based) for I:SPX
    indicators = poly.latest_indicator_snapshot("I:SPX") or {}

    # IV-based expected ranges for today (using VIX/VXN/RVX)
    ranges_md: list[str] = []
    have_ranges = False
    if include_ranges:
        try:
            # Symbols and IV index proxies
            sym_close_map: dict[str, float] = {}
            # Try to get latest close/last trade for SPX proxy I:SPX and ETFs
            def _last_close(sym: str) -> float | None:
                try:
                    if sym == "SPX":
                        # Use Polygon I:SPX minute close snapshot as proxy
                        snap = poly.latest_indicator_snapshot("I:SPX") or {}
                        val = snap.get("close")
                        return float(val) if val is not None else None
                    else:
                        lt = poly.last_trade_symbol(sym, use_cache=False) or {}
                        res = (lt or {}).get("results") or []
                        if res:
                            row = res[0]
                            cpx = row.get("c") if row.get("c") is not None else row.get("p")
                            return float(cpx) if cpx is not None else None
                except Exception:
                    return None
                return None
            for sym in ("SPX","SPY","QQQ","IWM"):
                v = _last_close(sym)
                if isinstance(v, (int, float)):
                    sym_close_map[sym] = float(v)

            iv_map = {"SPX": "I:VIX", "SPY": "I:VIX", "QQQ": "I:VXN", "IWM": "I:RVX"}
            iv_close: dict[str, float] = {}
            for idx in sorted(set(iv_map.values())):
                try:
                    df_iv = poly.fetch_index_daily_aggs(idx, years=1)
                    if df_iv is not None and not getattr(df_iv, "empty", True):
                        close_col = "close" if "close" in df_iv.columns else ("c" if "c" in df_iv.columns else None)
                        if close_col:
                            v = float(_pd.to_numeric(df_iv[close_col], errors="coerce").iloc[-1])
                            iv_close[idx] = v
                except Exception:
                    continue
            def _fmt_pct(x: float | None) -> str:
                try:
                    return f"{float(x)*100:+.2f}%"
                except Exception:
                    return "n/a"
            def _fmt_num(x: float | None) -> str:
                try:
                    return f"{float(x):,.2f}"
                except Exception:
                    return "n/a"
            for sym, cpx in sym_close_map.items():
                idx = iv_map.get(sym)
                if idx and (idx in iv_close) and (cpx and cpx > 0):
                    v = iv_close[idx]
                    sigma1 = (v / 100.0) / (_math.sqrt(252.0))
                    em1 = cpx * sigma1
                    low = cpx - em1
                    high = cpx + em1
                    ranges_md.append(f"- {sym}: Close {_fmt_num(cpx)} | 1σ EM {_fmt_num(em1)} ({_fmt_pct(sigma1)}) → Range {_fmt_num(low)} — {_fmt_num(high)}  [source: {idx}={v:.2f}]")
                    have_ranges = True
        except Exception:
            have_ranges = False

    # 3) Optional: overnight futures via IQFeed (continuous)
    fut_section: list[str] | None = None
    if include_futures:
        try:
            from .datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig
            iq = IQFeedClient(IQFeedConfig.from_env())
            fmap = {"ES": "@ES#", "NQ": "@NQ#", "RTY": "@RTY#"}
            fut_rows: list[dict] = []
            for k, sym in fmap.items():
                snap = iq.lookup_last(sym)
                if not isinstance(snap, dict):
                    continue
                last = snap.get("last_price") or snap.get("last_trade")
                prev = snap.get("close_yest")
                bid = snap.get("bid"); ask = snap.get("ask")
                mid = None
                try:
                    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
                        mid = (float(bid) + float(ask)) / 2.0
                except Exception:
                    mid = None
                if isinstance(last, (int, float)) or isinstance(prev, (int, float)):
                    try:
                        last_f = float(last) if last is not None else None
                        prev_f = float(prev) if prev is not None else None
                    except Exception:
                        last_f = float(last) if isinstance(last, (int, float)) else None
                        prev_f = float(prev) if isinstance(prev, (int, float)) else None
                    ret = (last_f - prev_f) / prev_f if (last_f is not None and prev_f and prev_f != 0) else None
                    fut_rows.append({"name": k, "last": last_f, "prev": prev_f, "ret": ret, "mid": mid})
            if fut_rows:
                fut_section = []
                def _pct(x):
                    try:
                        return f"{float(x)*100:+.2f}%"
                    except Exception:
                        return "n/a"
                def _fmtn(x):
                    try:
                        return f"{float(x):,.2f}"
                    except Exception:
                        return "n/a"
                for r in fut_rows:
                    fut_section.append(
                        f"- {r['name']}: last {_fmtn(r['last'])} | prev {_fmtn(r['prev'])} | change {_pct(r['ret'])}"
                    )
                # feed into features for the agent
                for r in fut_rows:
                    nm = r["name"]
                    if r.get("ret") is not None:
                        features[f"fut_{nm}_ret"] = float(r["ret"])  # type: ignore[arg-type]
                    if r.get("last") is not None:
                        features[f"fut_{nm}_last"] = float(r["last"])  # type: ignore[arg-type]
                    if r.get("prev") is not None:
                        features[f"fut_{nm}_prev"] = float(r["prev"])  # type: ignore[arg-type]
        except Exception:
            fut_section = None
        # Fallback: use Polygon ETF proxies (SPY->ES, QQQ->NQ, IWM->RTY) if IQFeed futures unavailable
        if not fut_section:
            try:
                import datetime as _dt
                import pytz as _pytz
                tz = _pytz.timezone("America/New_York")
                today_et = _dt.datetime.now(_dt.timezone.utc).astimezone(tz).date()
                proxy_map = {"ES": "SPY", "NQ": "QQQ", "RTY": "IWM"}
                rows_p: list[dict] = []
                for name, eq in proxy_map.items():
                    try:
                        prev = poly.last_trade_symbol(eq, use_cache=False) or {}
                        pres = (prev.get("results") or [])
                        prev_close = None
                        if pres:
                            rr = pres[0]
                            prev_close = rr.get("c") if rr.get("c") is not None else rr.get("p")
                        dfm = poly.fetch_intraday_minute_aggs_day(eq, today_et)
                        last_close = None
                        if dfm is not None and not getattr(dfm, "empty", True):
                            import pandas as _pd
                            dfm = dfm.copy()
                            dfm["close"] = _pd.to_numeric(dfm["close"], errors="coerce")
                            dfm = dfm.dropna(subset=["close"])
                            if not dfm.empty:
                                last_close = float(dfm["close"].iloc[-1])
                        if (prev_close is not None) and (last_close is not None):
                            pc = float(prev_close); lc = float(last_close)
                            ret = (lc - pc) / pc if pc else None
                            rows_p.append({"name": name, "last": lc, "prev": pc, "ret": ret})
                    except Exception:
                        continue
                if rows_p:
                    fut_section = []
                    def _pct2(x):
                        try:
                            return f"{float(x)*100:+.2f}%"
                        except Exception:
                            return "n/a"
                    def _fmtn2(x):
                        try:
                            return f"{float(x):,.2f}"
                        except Exception:
                            return "n/a"
                    for r in rows_p:
                        fut_section.append(f"- {r['name']} (proxy): last {_fmtn2(r['last'])} | prev {_fmtn2(r['prev'])} | change {_pct2(r['ret'])}")
                    for r in rows_p:
                        nm = r["name"]
                        if r.get("ret") is not None:
                            features[f"fut_{nm}_ret"] = float(r["ret"])  # type: ignore[arg-type]
                        if r.get("last") is not None:
                            features[f"fut_{nm}_last"] = float(r["last"])  # type: ignore[arg-type]
                        if r.get("prev") is not None:
                            features[f"fut_{nm}_prev"] = float(r["prev"])  # type: ignore[arg-type]
            except Exception:
                pass

    # 4) Neutral feature summary via OpenAI (if configured)
    oai_text = "(OpenAI disabled)"
    features: dict[str, float | int | str] = {}
    if c is not None:
        features["prev_close"] = float(c)
    if h is not None and l is not None:
        try:
            features["prev_range"] = float(h - l)
        except Exception:
            pass
    for k in ("sma", "ema", "macd", "macd_signal", "rsi", "close"):
        if k in indicators and indicators[k] is not None:
            try:
                features[f"ind_{k}"] = float(indicators[k])
            except Exception:
                pass
    for k in ("pivot", "R1", "S1", "R2", "S2"):
        if k in piv:
            try:
                features[f"level_{k.lower()}"] = float(piv[k])
            except Exception:
                pass
    if settings.has_openai:
        try:
            wrapper = OpenAIWrapper(settings.openai_api_key)
            oai_text = wrapper.summarize_market(features)
        except Exception as _exc:  # noqa: BLE001
            oai_text = f"(OpenAI error: {_exc})"

    # 5) Optional: quick nowcast over SPY minute prices
    nowcast_md: list[str] | None = None
    if include_nowcast:
        try:
            from .utils.nowcast import TSNowcaster
            import datetime as _dt
            import pytz as _pytz
            tz = _pytz.timezone("America/New_York")
            today = _dt.datetime.now(_dt.timezone.utc).astimezone(tz).date()
            df_spy = poly.fetch_intraday_minute_aggs_day("SPY", today)
            if df_spy is not None and not getattr(df_spy, "empty", True):
                import pandas as _pd
                ser = _pd.to_numeric(df_spy.get("close"), errors="coerce").dropna()
                if not ser.empty:
                    caster = TSNowcaster(mode="ewma")
                    res = caster.nowcast(ser, horizon_min=30)
                    p_up = float(res.get("p_up", 0.5)); p_dn = float(res.get("p_down", 0.5)); mag = float(res.get("mag", 0.0))
                    # Simple magnitude bands as +/- 1x mag fraction of price
                    last_px = float(ser.iloc[-1])
                    band = last_px * mag
                    low = last_px - band
                    high = last_px + band
                    def _pct(x):
                        try:
                            return f"{float(x)*100:.1f}%"
                        except Exception:
                            return "n/a"
                    def _fmtn(x):
                        try:
                            return f"{float(x):,.2f}"
                        except Exception:
                            return "n/a"
                    nowcast_md = [
                        f"- Prob up: {_pct(p_up)} | Prob down: {_pct(p_dn)} | Mag proxy: {_pct(mag)}",
                        f"- SPY bands (±mag): {_fmtn(low)} — {_fmtn(high)} (last {_fmtn(last_px)})",
                    ]
                    # add features for agent context
                    features["nowcast_p_up"] = p_up
                    features["nowcast_mag"] = mag
        except Exception:
            nowcast_md = None

    # 6) News summary (optional): find latest logs/news_watch_YYYYMMDD*.jsonl and call news_summarize_agent
    news_section = None
    if include_news:
        try:
            if news_jsonl is None:
                # Resolve ET day stamp and pick the most recent file
                import glob as _glob
                import pytz as _pytz
                tz = _pytz.timezone("America/New_York")
                now_et = _dtm.datetime.now(_dtm.timezone.utc).astimezone(tz)
                day_stamp = now_et.strftime("%Y%m%d")
                candidates = sorted(_glob.glob(str(_P("logs") / f"news_watch_{day_stamp}*.jsonl")))
                news_jsonl = _P(candidates[-1]) if candidates else None
            if news_jsonl and news_jsonl.exists():
                # Invoke via subprocess to reuse CLI behavior and avoid duplication
                import subprocess as _sp
                cmd = [
                    _os.environ.get("PYTHON_EXE") or _os.environ.get("VIRTUAL_ENV_PY") or "python",
                    "-m", "src.cli", "news_summarize_agent", str(news_jsonl), "--max-items", "200", "--model", model,
                ]
                proc = _sp.run(cmd, capture_output=True, text=True, check=False)
                out = proc.stdout.strip()
                payload = None
                try:
                    payload = _json.loads(out)
                except Exception:
                    # try to find last JSON object in stdout
                    lines = [ln for ln in out.splitlines() if ln.strip().startswith("{") and ln.strip().endswith("}")]
                    if lines:
                        try:
                            payload = _json.loads(lines[-1])
                        except Exception:
                            payload = None
                if isinstance(payload, dict) and payload.get("ok"):
                    # Load the generated JSON to include summary details
                    gen_path = payload.get("out")
                    if gen_path and _P(gen_path).exists():
                        try:
                            news_obj = _json.loads(_P(gen_path).read_text(encoding="utf-8"))
                            news_section = news_obj
                        except Exception:
                            news_section = None
        except Exception:
            news_section = None

    # 7) Compose markdown
    def _fmt(v):
        try:
            return f"{float(v):,.2f}"
        except Exception:
            return str(v)
    lines_md: list[str] = []
    title_date = _dtm.datetime.now(_dtm.timezone.utc).astimezone().strftime("%Y-%m-%d")
    lines_md.append(f"# Morning Brief {title_date}")
    lines_md.append("")
    lines_md.append("## SPX key levels")
    if h is not None and l is not None and c is not None:
        lines_md.append(f"- Prev close: {_fmt(c)}  |  High: {_fmt(h)}  Low: {_fmt(l)}")
    if piv:
        lines_md.append(f"- Pivot P: {_fmt(piv.get('pivot'))}; R1: {_fmt(piv.get('R1'))}, R2: {_fmt(piv.get('R2'))}; S1: {_fmt(piv.get('S1'))}, S2: {_fmt(piv.get('S2'))}")
        lines_md.append("  - Classic pivots: P=(H+L+C)/3; R1=2P-L; S1=2P-H; R2=P+(H-L); S2=P-(H-L)")
    if indicators:
        lines_md.append("- Indicators (Polygon minute): " + ", ".join(
            f"{k.upper()}={_fmt(v)}" for k, v in indicators.items() if k in ("sma","ema","macd","macd_signal","rsi","close") and v is not None
        ))
    lines_md.append("")
    if fut_section:
        lines_md.append("## Overnight futures (IQFeed)")
        lines_md.extend(fut_section)
        lines_md.append("")
    if nowcast_md:
        lines_md.append("## Nowcast (SPY, ~30m horizon)")
        lines_md.extend(nowcast_md)
        lines_md.append("")
    if have_ranges and ranges_md:
        lines_md.append("## Expected ranges today (IV-based)")
        lines_md.extend(ranges_md)
        lines_md.append("")
    # Derive a simple bull/bear tilt from futures + nowcast + VIX change
    tilt_line = None
    try:
        fut_rets = [float(features[k]) for k in ("fut_ES_ret","fut_NQ_ret","fut_RTY_ret") if k in features]
        fut_avg = sum(fut_rets)/len(fut_rets) if fut_rets else 0.0
        p_up = float(features.get("nowcast_p_up", 0.5))
        # VIX delta proxy via VIX index daily aggs last two closes
        vix_df = poly.fetch_index_daily_aggs("I:VIX", years=1)
        vix_chg = 0.0
        if vix_df is not None and not getattr(vix_df, "empty", True):
            import pandas as _pd
            vix_df = vix_df.copy()
            vix_df["close"] = _pd.to_numeric(vix_df["close"], errors="coerce")
            vix_df = vix_df.dropna(subset=["close"])
            if len(vix_df) >= 2:
                vix_chg = float(vix_df["close"].iloc[-1] - vix_df["close"].iloc[-2]) / float(max(vix_df["close"].iloc[-2], 1e-6))
        # Blend: positive futures + higher p_up bullish; higher VIX bearish
        score = 0.5 + 0.35 * fut_avg + 0.35 * (p_up - 0.5) - 0.3 * vix_chg
        score = float(max(0.0, min(1.0, score)))
        label = "Bullish" if score > 0.55 else ("Bearish" if score < 0.45 else "Neutral")
        tilt_line = f"Market tilt: {label} (score {score:.2f})"
        features["tilt_score"] = score
        features["tilt_label"] = label
    except Exception:
        tilt_line = None
    if tilt_line:
        lines_md.append("## Market tilt")
        lines_md.append(f"- {tilt_line}")
        # Provide a quick legend and provenance for the tilt score
        lines_md.append("  - Legend: <0.45 Bearish, 0.45–0.55 Neutral, >0.55 Bullish; blend of futures ret, nowcast p_up, and VIX delta")
        lines_md.append("")
    lines_md.append("## Neutral summary")
    lines_md.append(oai_text.strip())
    if include_news and news_section:
        lines_md.append("")
        lines_md.append("## News summary")
        try:
            txt = str(news_section.get("summary") or "").strip()
            if txt:
                lines_md.append(txt)
            ss = news_section.get("sentiment_score")
            sc = news_section.get("sentiment_count")
            if ss is not None and sc is not None:
                lines_md.append("")
                lines_md.append(f"Sentiment (avg over {int(sc)} headlines): {float(ss):+.2f}")
            db = news_section.get("directional_bias") or {}
            if isinstance(db, dict) and db:
                bias = db.get("bias")
                conf = db.get("confidence")
                if bias:
                    lines_md.append(f"Bias: {bias} ({conf:.2f} conf)" if isinstance(conf, (int, float)) else f"Bias: {bias}")
        except Exception:
            pass

    md_text = "\n".join(lines_md)
    out_md.write_text(md_text, encoding="utf-8")
    posted = False
    post_status = None
    if post_to_discord:
        try:
            import os as _os
            from .integrations.discord_webhook import post_chunks as _post_chunks
            hook = discord_webhook_url or _os.environ.get("DISCORD_WEBHOOK_URL")
            if hook:
                res = _post_chunks(
                    webhook_url=hook,
                    content=md_text,
                    username=discord_username,
                    prefix=None,
                    suffix=None,
                )
                posted = bool(res.get("ok"))
                post_status = int(res.get("status", 0) or 0)
        except Exception:
            posted = False
            post_status = None
    typer.echo(_json.dumps({
        "ok": True,
        "out": str(out_md),
        "has_pivots": bool(piv),
        "has_indicators": bool(indicators),
        "has_news": bool(news_section) if include_news else False,
        "has_futures": bool(fut_section),
    "has_nowcast": bool(nowcast_md),
    "tilt": features.get("tilt_label"),
    "tilt_score": features.get("tilt_score"),
        "features": features,
        "posted_to_discord": posted,
        "discord_status": post_status,
    }, indent=2))


from pathlib import Path as _P  # ensure _P is defined for annotations and function body

@app.command(name="eod_recap")
def eod_recap_cli(
    date: str = typer.Option(None, help="Trading date YYYY-MM-DD (defaults to today ET)"),
    symbols: str = typer.Option("SPX,SPY,IWM,QQQ", help="Comma-separated list of tickers to recap (SPX uses I:SPX index data)"),
    include_pnl: bool = typer.Option(True, help="Include EOD PnL summary if available"),
    pnl_json: _P = typer.Option(None, help="Optional explicit PnL JSON path (otherwise autodetect logs/eod_pnl_<date>.json or r1_eod_pnl_<date>.json)"),
    out_md: _P = typer.Option(None, help="Output markdown path; defaults to logs/eod_recap_<date>.md"),
    model: str = typer.Option("gpt-4o-mini", help="OpenAI model used for the short 'tomorrow' outlook when available"),
    include_ranges: bool = typer.Option(True, help="Compute IV-based expected move ranges for tomorrow (VIX/VXN/RVX proxies)"),
    include_headlines: bool = typer.Option(True, help="Include top Polygon headlines for the day"),
    news_limit: int = typer.Option(8, help="Max number of headlines to include"),
    post_to_discord: bool = typer.Option(False, help="If set, post the recap to Discord via webhook"),
    discord_webhook_url: str = typer.Option(None, help="Discord webhook URL. If omitted, reads DISCORD_WEBHOOK_URL env var"),
    discord_username: str = typer.Option("ZeroDTE Recap", help="Username to display when posting to Discord"),
):
    """Generate an end-of-day recap and a short outlook for the next session.

    - Summarizes price action for SPX, SPY, IWM, and QQQ (returns, range) from Polygon minute data
    - Optionally includes EOD PnL summary if available
    - If OpenAI is configured, adds a concise 'what to expect tomorrow' paragraph
    """
    import os as _os
    import json as _json
    import datetime as _dt
    import math as _math
    import pandas as _pd
    from .datafeeds.polygon_client import PolygonClient, PolygonConfig
    from .ingestion.polygon_news import fetch_polygon_news as _fetch_news

    settings = Settings()

    # Resolve ET date
    if not date:
        try:
            import pytz as _pytz
            tz = _pytz.timezone("America/New_York")
            date = _dt.datetime.now(_dt.timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
        except Exception:
            date = _dt.date.today().strftime("%Y-%m-%d")
    try:
        day = _dt.datetime.strptime(date, "%Y-%m-%d").date()
    except Exception:
        day = _dt.date.today()

    # Output path
    if out_md is None:
        out_md = _P("logs") / f"eod_recap_{day:%Y-%m-%d}.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)

    # Fetch intraday minute bars and compute summary metrics per symbol
    poly = PolygonClient(PolygonConfig.from_env())
    sym_list = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
    if not sym_list:
        sym_list = ["SPX", "SPY", "IWM", "QQQ"]

    rows: list[dict] = []
    feat: dict[str, float] = {}
    for s in sym_list:
        ticker = "I:SPX" if s == "SPX" else s
        try:
            df = poly.fetch_intraday_minute_aggs_day(ticker=ticker, day=day)  # type: ignore[arg-type]
        except Exception:
            df = None
        if df is None or getattr(df, "empty", True):
            rows.append({"symbol": s, "ok": False})
            continue
        try:
            df = df.copy()
            df["close"] = _pd.to_numeric(df["close"], errors="coerce")
            df["high"] = _pd.to_numeric(df["high"], errors="coerce")
            df["low"] = _pd.to_numeric(df["low"], errors="coerce")
            df = df.dropna(subset=["close"])  # ensure closing prices exist
            if df.empty:
                rows.append({"symbol": s, "ok": False})
                continue
            o = float(df["close"].iloc[0])
            c = float(df["close"].iloc[-1])
            h = float(_pd.to_numeric(df["high"], errors="coerce").max()) if "high" in df.columns else max(o, c)
            l = float(_pd.to_numeric(df["low"], errors="coerce").min()) if "low" in df.columns else min(o, c)
            ret = (c - o) / o if o else 0.0
            rng = (h - l) / o if o else 0.0
            # Simple vol proxy: std of minute returns
            try:
                rets = _pd.to_numeric(df["close"], errors="coerce").pct_change().dropna()
                vol_min = float(rets.std()) if rets.size > 5 else 0.0
            except Exception:
                vol_min = 0.0
            rows.append({
                "symbol": s,
                "open": o,
                "close": c,
                "high": h,
                "low": l,
                "ret": ret,
                "range": rng,
                "vol_min": vol_min,
                "ok": True,
            })
            # accumulate features for model summary
            feat[f"{s}_ret"] = ret
            feat[f"{s}_range"] = rng
            feat[f"{s}_vol_min"] = vol_min
            feat[f"{s}_close"] = c
        except Exception:
            rows.append({"symbol": s, "ok": False})

    # Local format helpers used below
    def _fmt_pct(x: float | None) -> str:
        try:
            return f"{float(x)*100:+.2f}%"
        except Exception:
            return "n/a"

    def _fmt(x: float | None) -> str:
        try:
            return f"{float(x):,.2f}"
        except Exception:
            return "n/a"

    # Expected ranges for tomorrow using IV index proxies (VIX/VXN/RVX)
    ranges_md: list[str] = []
    have_ranges = False
    if include_ranges and rows:
        try:
            # Map asset -> IV index proxy
            iv_map = {
                "SPX": "I:VIX",
                "SPY": "I:VIX",
                "QQQ": "I:VXN",
                "IWM": "I:RVX",
            }
            # Fetch needed IV indices once
            need_set = {iv_map.get(r["symbol"]) for r in rows if r.get("ok")}
            need = sorted([x for x in need_set if isinstance(x, str) and x])
            iv_close: dict[str, float] = {}
            for idx in need:
                try:
                    df_iv = poly.fetch_index_daily_aggs(idx, years=1)
                    if df_iv is not None and not getattr(df_iv, "empty", True):
                        # Prefer row matching the date; fallback to last
                        use = df_iv
                        if "date" in df_iv.columns:
                            try:
                                use = df_iv[_pd.to_datetime(df_iv["date"]).dt.date == day]
                                if use is None or use.empty:
                                    use = df_iv
                            except Exception:
                                use = df_iv
                        close_col = "close" if "close" in use.columns else ("c" if "c" in use.columns else None)
                        if close_col:
                            v = float(_pd.to_numeric(use[close_col], errors="coerce").iloc[-1])
                            iv_close[idx] = v
                except Exception:
                    continue
            for r in rows:
                if not r.get("ok"):
                    continue
                sym = r["symbol"]
                idx = iv_map.get(sym)
                cpx = float(r.get("close") or 0.0)
                if (idx in iv_close) and cpx > 0:
                    v = float(iv_close[idx])
                    # Convert 30d IV to 1-day sigma approximation
                    sigma1 = (v / 100.0) / (_math.sqrt(252.0))
                    em1 = cpx * sigma1
                    low = cpx - em1
                    high = cpx + em1
                    ranges_md.append(f"- {sym}: Close {_fmt(cpx)} | 1σ EM {_fmt(em1)} ({_fmt_pct(sigma1)}) → Range {_fmt(low)} — {_fmt(high)}  [source: {idx}={v:.2f}]")
                    have_ranges = True
                else:
                    # Fallback to realized vol proxy if IV index missing
                    vm = float(r.get("vol_min") or 0.0)
                    if vm > 0 and cpx > 0:
                        em1 = cpx * vm
                        ranges_md.append(f"- {sym}: Close {_fmt(cpx)} | proxy EM {_fmt(em1)} ({_fmt_pct(vm)}) → Range {_fmt(cpx - em1)} — {_fmt(cpx + em1)}  [source: intraday vol]")
                        have_ranges = True
        except Exception:
            have_ranges = False

    # PnL inclusion
    pnl_summary: dict | None = None
    if include_pnl:
        p = None
        if pnl_json is not None and _P(pnl_json).exists():
            p = _P(pnl_json)
        else:
            logs = _P("logs")
            cand = [
                logs / f"eod_pnl_{day:%Y-%m-%d}.json",
                logs / f"eod_pnl_{day:%Y%m%d}.json",
                logs / f"r1_eod_pnl_{day:%Y-%m-%d}.json",
                logs / f"r1_eod_pnl_{day:%Y%m%d}.json",
            ]
            p = next((q for q in cand if q.exists()), None)
        try:
            if p and p.exists():
                obj = _json.loads(p.read_text(encoding="utf-8"))
                s = obj.get("summary") if isinstance(obj, dict) else None
                pnl_summary = s or obj
        except Exception:
            pnl_summary = None


    # Compose markdown
    lines: list[str] = []
    lines.append(f"# EOD Recap {day:%Y-%m-%d}")
    lines.append("")
    lines.append("## Price action (minute bars)")
    for r in rows:
        if not r.get("ok"):
            lines.append(f"- {r.get('symbol')}: data unavailable")
            continue
        lines.append(
            f"- {r['symbol']}: Open {_fmt(r['open'])} → Close {_fmt(r['close'])} ({_fmt_pct(r['ret'])}); Range {_fmt_pct(r['range'])}; Vol(min) {r['vol_min']:.4f}"
        )
    if pnl_summary:
        lines.append("")
        lines.append("## Strategy PnL snapshot")
        try:
            trades = int(pnl_summary.get("trades") or 0)
            wins = int(pnl_summary.get("wins") or 0)
            losses = int(pnl_summary.get("losses") or max(0, trades - wins))
            gross = float(pnl_summary.get("gross_pnl") or 0.0)
            wr = (wins / trades) if trades > 0 else 0.0
            lines.append(f"- Trades: {trades}  |  Wins: {wins}  |  Losses: {losses}  |  Win rate: {wr*100:.1f}%  |  Gross: {gross:+,.2f}")
        except Exception:
            pass

    # Short outlook via OpenAI if available
    outlook = "(OpenAI disabled)"
    if settings.has_openai:
        try:
            wrapper = OpenAIWrapper(settings.openai_api_key)
            # Reuse summarize_market with multi-asset features
            outlook = wrapper.summarize_market({**feat, "context": "eod_recap_next_day_outlook"})
        except Exception as _exc:  # noqa: BLE001
            outlook = f"(OpenAI error: {_exc})"
    # Expected ranges section
    if have_ranges and ranges_md:
        lines.append("")
        lines.append("## Expected ranges for tomorrow (IV-based)")
        lines.append("  - Legend: 1σ expected move uses IV proxies (VIX/VXN/RVX) scaled by 1/√252; rough guide, not a prediction")
        lines.extend(ranges_md)

    # Headlines section
    headlines_used = []
    if include_headlines:
        try:
            # Aggregate Polygon market news for the day across representative symbols
            news_syms = ["I:SPX", "SPY", "QQQ", "IWM"]
            seen = set()
            items: list[tuple[str, str]] = []  # (ts_str, title)
            for ns in news_syms:
                try:
                    got = _fetch_news(ns, date, limit=25, max_pages=1)
                except Exception:
                    got = []
                for it in got:
                    title = (it.title or "").strip()
                    if not title:
                        continue
                    key = title.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    ts_str = it.ts.tz_convert("America/New_York").strftime("%H:%M ET") if hasattr(it.ts, "tz_convert") else str(it.ts)
                    items.append((ts_str, title))
            # Sort by time and cap
            items.sort(key=lambda t: t[0])
            if news_limit and news_limit > 0:
                items = items[: int(news_limit)]
            if items:
                lines.append("")
                lines.append("## Top headlines today")
                for ts_str, title in items:
                    lines.append(f"- [{ts_str}] {title}")
                headlines_used = [t for _, t in items]
        except Exception:
            pass

    lines.append("")
    lines.append("## What to watch tomorrow")
    lines.append(outlook.strip())

    md = "\n".join(lines)
    out_md.write_text(md, encoding="utf-8")

    posted = False
    status_code = None
    if post_to_discord:
        try:
            from .integrations.discord_webhook import post_chunks as _post_chunks
            hook = discord_webhook_url or _os.environ.get("DISCORD_WEBHOOK_URL")
            if hook:
                res = _post_chunks(webhook_url=hook, content=md, username=discord_username, prefix=None, suffix=None)
                posted = bool(res.get("ok"))
                status_code = int(res.get("status", 0) or 0)
        except Exception:
            posted = False
            status_code = None

    typer.echo(_json.dumps({
        "ok": True,
        "out": str(out_md),
        "symbols": sym_list,
        "has_pnl": bool(pnl_summary),
        "has_ranges": bool(have_ranges and ranges_md),
        "headlines_count": int(len(headlines_used) if headlines_used else 0),
        "posted_to_discord": posted,
        "discord_status": status_code,
    }, indent=2))

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
    """Grid search over fast/slow moving average windows for the day's bars.

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
    window_len: int = typer.Option(60, help="Rolling window length for high/low/close arrays attached to market_ctx"),
    signals_filter: str = typer.Option("auto", help="Filter signals: auto (all), entries (enter_*/exit_* only)"),
    strategy_kwargs: str = typer.Option("", help="Optional strategy params as JSON or key=value pairs, e.g. '{\"entry_threshold\":0.3}' or 'entry_threshold=0.3,exit_threshold=0.15'"),
    greeks_source: str = typer.Option("auto", help="Greeks factor source: auto (persisted else compute then proxy), compute (daily snapshot), proxy (force proxy calc), none (disable)"),
    greeks_proxy: str = typer.Option("vol_z", help="Proxy flavor when greeks_source=proxy or fallback: vol_z|chop"),
    greeks_compute_mode: str = typer.Option("composite", help="When greeks_source=compute or auto-fallback, mode: rr|fly|slope|composite"),
    greeks_compute_kwargs: str = typer.Option("", help="Key=val or JSON kwargs for greeks compute (e.g. 'target_abs_delta=0.25,w_rr=0.5,rr_scale=0.2')"),
    greeks_source_out: Path = typer.Option(None, help="Optional path to export per-bar greeks source decisions (.json/.csv/.parquet) for audit"),
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
    from .utils.factor_store import load_greeks_factors, map_greeks_to_rows  # lazy import
    audit_rows: list[dict] = []
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
        # Build rolling windows and month per bar
        from collections import deque as _deque
        hw: _deque[float] = _deque(maxlen=max(1, window_len))
        lw: _deque[float] = _deque(maxlen=max(1, window_len))
        cw: _deque[float] = _deque(maxlen=max(1, window_len))
        def _get_month(r: dict) -> int:
            import datetime as _dt
            ts = r.get("epoch") or r.get("timestamp") or r.get("time")
            try:
                if isinstance(ts, (int, float)):
                    return int(_dt.datetime.utcfromtimestamp(float(ts)).month)
                if isinstance(ts, str):
                    return int(_dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).month)
            except Exception:
                return 0
            return 0
        def _greeks_norm_from_proxy(hw_l: list[float], lw_l: list[float], cw_l: list[float]) -> float:
            # Map selected proxy into [-1,1]; lightweight and robust on small windows
            if greeks_proxy.lower() == "none":
                return 0.0
            import numpy as _np
            try:
                c = _np.asarray(cw_l, dtype=float)
                if c.size < 5:
                    return 0.0
                if greeks_proxy.lower() == "vol_z":
                    # realized vol zscore approximation vs rolling mean vol
                    rets = _np.diff(c) / c[:-1]
                    if rets.size < 5:
                        return 0.0
                    cur = float(_np.std(rets[-5:], ddof=1))
                    hist = _np.array([_np.std(rets[max(i-5,0):i], ddof=1) for i in range(5, len(rets)+1)])
                    if hist.size < 3:
                        return 0.0
                    z = (cur - float(_np.mean(hist))) / max(float(_np.std(hist, ddof=1)), 1e-9)
                    return float(max(-1.0, min(1.0, z / 3.0)))
                if greeks_proxy.lower() == "chop":
                    h = _np.asarray(hw_l, dtype=float)
                    l = _np.asarray(lw_l, dtype=float)
                    if h.size != l.size or h.size < 5:
                        return 0.0
                    rng = float(_np.max(h) - _np.min(l))
                    tr = float(_np.sum(_np.abs(_np.diff(c))))
                    denom = max(rng, 1e-9)
                    chop = 100.0 * math.log10(max(tr,1e-12) / denom) / max(math.log10(max(len(c),2)), 1e-9)
                    # map chop (0..100) to [-1,1]: trending(-1) .. choppy(+1)
                    norm = (min(max(chop, 0.0), 100.0) - 50.0) / 50.0
                    return float(max(-1.0, min(1.0, norm)))
            except Exception:
                return 0.0
            return 0.0
        adapted_day = []
        # Track provenance per day
        src_counts_day = {"persisted": 0, "compute": 0, "proxy": 0, "none": 0}
        # Load greeks factor (if using auto/none)
        greeks_df_day = None
        if greeks_source.lower() in ("auto",):
            try:
                greeks_df_day = load_greeks_factors(settings.data_dir or "data", d_str)  # type: ignore[arg-type]
            except Exception as _exc:
                greeks_df_day = None
        if greeks_source.lower() == "none":
            greeks_df_day = None
        # Build alignment if we have a df
        aligned_gn: list[float | None] = []
        if greeks_df_day is not None and not greeks_df_day.empty:
            try:
                # Use a full-day tolerance so a single daily snapshot applies to all later bars that day
                aligned_gn = map_greeks_to_rows(rows_day, greeks_df_day, tolerance_secs=86400.0)
            except Exception:
                aligned_gn = []
        # Compute a daily greeks snapshot if requested or for auto fallback
        daily_gn_value: float | None = None
        def _parse_compute_kwargs(s: str):
            out: dict[str, object] = {}
            if not s or not s.strip():
                return out
            txt = s.strip()
            if txt.startswith("{") and txt.endswith("}"):
                try:
                    import json as _json
                    d = _json.loads(txt)
                    if isinstance(d, dict):
                        return d  # type: ignore[return-value]
                except Exception:
                    pass
            parts = [seg.strip() for seg in txt.replace(";", ",").split(",") if seg.strip()]
            def _coerce(v: str):
                vl = v.strip()
                if (vl.startswith('"') and vl.endswith('"')) or (vl.startswith("'") and vl.endswith("'")):
                    vl = vl[1:-1]
                low = vl.lower()
                if low in ("true", "false"):
                    return low == "true"
                try:
                    if "." in vl or "e" in vl.lower():
                        return float(vl)
                    return int(vl)
                except Exception:
                    return vl
            for p in parts:
                if "=" not in p:
                    continue
                k, v = p.split("=", 1)
                out[k.strip()] = _coerce(v)
            return out
        need_compute = (greeks_source.lower() == "compute") or (greeks_source.lower() == "auto" and (greeks_df_day is None or not aligned_gn))
        if need_compute:
            try:
                first_close = None
                if rows_day:
                    r0 = rows_day[0]
                    first_close = float(r0.get("close", r0.get("lastPrice", 0.0)))
                if first_close and first_close > 0:
                    from .datafeeds.iqfeed_options import IQFeedOptionsGreeks  # local import
                    og = IQFeedOptionsGreeks()
                    chain = og.fetch_chain_greeks(first_close) or []
                    kwargs = _parse_compute_kwargs(greeks_compute_kwargs)
                    res = compute_greeks_norm(chain, mode=greeks_compute_mode, **kwargs)
                    if isinstance(res, dict) and (res.get("greeks_norm") is not None):
                        daily_gn_value = float(res["greeks_norm"])  # type: ignore[arg-type]
            except Exception:
                daily_gn_value = None
        # Pointer for aligned mapping
        idx_row = -1
        for r in rows_day:
            idx_row += 1
            h = float(r.get("high", r.get("close", 0.0)))
            l = float(r.get("low", r.get("close", 0.0)))
            c = float(r.get("close", 0.0))
            hw.append(h); lw.append(l); cw.append(c)
            # Determine greeks_norm priority: persisted -> compute -> proxy -> 0
            gn_val: float = 0.0
            src_used = "none"
            if greeks_df_day is not None and aligned_gn and idx_row < len(aligned_gn) and aligned_gn[idx_row] is not None:
                try:
                    _v = aligned_gn[idx_row]
                    gn_val = float(_v) if _v is not None else 0.0
                except Exception:
                    gn_val = 0.0
                else:
                    src_counts_day["persisted"] += 1
                    src_used = "persisted"
            elif (greeks_source.lower() == "compute") or (greeks_source.lower() == "auto" and daily_gn_value is not None):
                gn_val = float(daily_gn_value or 0.0)
                src_counts_day["compute"] += 1
                src_used = "compute"
            elif greeks_source.lower() in ("proxy",) or (greeks_source.lower() == "auto"):
                gn_val = _greeks_norm_from_proxy(list(hw), list(lw), list(cw))
                src_counts_day["proxy"] += 1
                src_used = "proxy"
            else:
                src_counts_day["none"] += 1
                src_used = "none"
            adapted_day.append({
                "lastPrice": c,
                **r,
                "high_window": list(hw),
                "low_window": list(lw),
                "close_window": list(cw),
                "greeks_norm": gn_val,
                "month": _get_month(r),
            })
            # Collect audit row
            if greeks_source_out is not None:
                ts = r.get("epoch") or r.get("end_ts") or r.get("start_ts") or r.get("timestamp") or r.get("time")
                audit_rows.append({
                    "symbol": symbol,
                    "interval": interval,
                    "date": d_str,
                    "bar_index": idx_row,
                    "ts": ts,
                    "greeks_source_used": src_used,
                })
        # Per-day engine run (no signal persistence to avoid duplication; we only persist aggregate later)
        from typing import cast as _cast, Any as _Any  # local import
        StratCls = get_strategy(strategy)
        strat_day = _cast(_Any, _instantiate_strategy(StratCls, fast=fast, slow=slow))
        engine_day = BacktestEngine(strat_day)  # type: ignore[arg-type]
        # Optional entries-only filter
        if signals_filter.lower() in ("entries", "entry", "enter"):
            allowed = ("enter_long", "exit_long", "enter_short", "exit_short")
            base = strat_day
            class _FilteredDay:
                def evaluate(self, market_ctx):  # type: ignore[no-untyped-def]
                    for s in base.evaluate(market_ctx):
                        name = getattr(s, "name", None)
                        if name in allowed:
                            yield s
            strat_day = _FilteredDay()  # type: ignore[assignment]
            engine_day = BacktestEngine(strat_day)  # type: ignore[arg-type]

        res_day = engine_day.run(adapted_day, collect_signals=False, annualize_factor=None)
        # Decide dominant source used this day
        _dominant_src = max(src_counts_day.items(), key=lambda kv: kv[1])[0] if src_counts_day else "none"
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
            "greeks_source_used": _dominant_src,
            "greeks_source_counts": src_counts_day,
        })
        # Accumulate adapted rows for aggregate (reuse already computed greeks_norm)
        all_rows.extend(adapted_day)
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


    

    # Aggregate run
    # Rebuild rolling context across full aggregate sequence
    from typing import Any as _Any2
    from collections import deque as _deque2
    hw2: _deque2[float] = _deque2(maxlen=max(1, window_len))
    lw2: _deque2[float] = _deque2(maxlen=max(1, window_len))
    cw2: _deque2[float] = _deque2(maxlen=max(1, window_len))
    def _get_month2(r: dict) -> int:
        import datetime as _dt
        ts = r.get("epoch") or r.get("timestamp") or r.get("time")
        try:
            if isinstance(ts, (int, float)):
                return int(_dt.datetime.utcfromtimestamp(float(ts)).month)
            if isinstance(ts, str):
                return int(_dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).month)
        except Exception:
            return 0
        return 0
    adapted_all_list: list[dict[str, _Any2]] = []
    for r in all_rows:
        # all_rows already contains adapted per-day dicts with windows and greeks_norm
        c = float(r.get("close", r.get("lastPrice", 0.0)))
        h = float(r.get("high", c))
        l = float(r.get("low", c))
        hw2.append(h); lw2.append(l); cw2.append(c)
        adapted_all_list.append({
            **r,
            "lastPrice": c,
            "high_window": list(hw2),
            "low_window": list(lw2),
            "close_window": list(cw2),
            # Preserve existing greeks_norm if present; else compute proxy if requested
            "greeks_norm": r.get("greeks_norm", _greeks_norm_from_proxy(list(hw2), list(lw2), list(cw2)) if greeks_source.lower() in ("proxy",) else 0.0),
            "month": _get_month2(r),
        })
    from typing import cast as _cast2, Any as _Any2
    # Parse optional strategy kwargs (JSON or key=value pairs)
    def _parse_strategy_kwargs(s: str) -> dict[str, _Any2]:
        out: dict[str, _Any2] = {}
        if not s or not s.strip():
            return out
        txt = s.strip()
        # Try JSON first
        if txt.startswith("{") and txt.endswith("}"):
            try:
                import json as _json
                parsed = _json.loads(txt)
                if isinstance(parsed, dict):
                    return parsed  # type: ignore[return-value]
            except Exception:
                pass
        # Fallback to key=value pairs separated by comma/semicolon
        parts: list[str] = []
        for seg in txt.replace(";", ",").split(","):
            seg = seg.strip()
            if seg:
                parts.append(seg)
        def _coerce(v: str):
            vl = v.strip()
            if (vl.startswith('"') and vl.endswith('"')) or (vl.startswith("'") and vl.endswith("'")):
                vl = vl[1:-1]
            # Attempt bool
            low = vl.lower()
            if low in ("true", "false"):
                return low == "true"
            # Attempt int/float
            try:
                if "." in vl or "e" in vl.lower():
                    return float(vl)
                return int(vl)
            except Exception:
                return vl
        for p in parts:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                out[k] = _coerce(v)
        return out

    _user_kwargs = _parse_strategy_kwargs(strategy_kwargs)
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
    # Merge generic fast/slow for strategies that accept them with user-provided kwargs
    _candidate_kwargs = {"fast": fast, "slow": slow}
    if _user_kwargs:
        _candidate_kwargs.update(_user_kwargs)
    strat = _cast2(_Any2, _instantiate_strategy(StratClsAgg, **_candidate_kwargs))
    engine = BacktestEngine(strat)  # type: ignore[arg-type]
    # Optional entries-only filter for aggregate
    if signals_filter.lower() in ("entries", "entry", "enter"):
        allowed = ("enter_long", "exit_long", "enter_short", "exit_short")
        base = strat
        class _FilteredAgg:
            def evaluate(self, market_ctx):  # type: ignore[no-untyped-def]
                for s in base.evaluate(market_ctx):
                    name = getattr(s, "name", None)
                    if name in allowed:
                        yield s
        strat = _FilteredAgg()  # type: ignore[assignment]
        engine = BacktestEngine(strat)  # type: ignore[arg-type]

    res = engine.run(adapted_all_list, collect_signals=persist, annualize_factor=af, quantiles=q_list or None,
                     commission=commission, slippage_bps=slippage_bps)

    if persist and res.collected_signals:
        # Persist under the selected strategy name for clarity
        writer = SignalWriter(settings.data_dir or "data", strategy)  # type: ignore[arg-type]
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

    # Aggregate provenance across days
    prov_totals = {"persisted": 0, "compute": 0, "proxy": 0, "none": 0}
    for d in daily:
        cnt = d.get("greeks_source_counts") or {}
        for k in prov_totals.keys():
            try:
                prov_totals[k] += int(cnt.get(k, 0))
            except Exception:
                pass
    agg_dominant_src = max(prov_totals.items(), key=lambda kv: kv[1])[0] if prov_totals else "none"

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
        "greeks_source_used": agg_dominant_src,
        "greeks_source_counts": prov_totals,
    }

    # Optional export of per-bar greeks source audit
    greeks_source_exported = False
    greeks_source_out_path = None
    if greeks_source_out is not None and audit_rows:
        try:
            greeks_source_out.parent.mkdir(parents=True, exist_ok=True)
            pstr = str(greeks_source_out).lower()
            import pandas as _pd
            df_a = _pd.DataFrame(audit_rows)
            if pstr.endswith('.parquet'):
                df_a.to_parquet(greeks_source_out, index=False)
            elif pstr.endswith('.csv'):
                df_a.to_csv(greeks_source_out, index=False)
            else:
                import json as _json
                greeks_source_out.write_text(_json.dumps(df_a.to_dict(orient='records'), indent=2))
            greeks_source_exported = True
            greeks_source_out_path = str(greeks_source_out)
        except Exception as _exc:  # noqa: BLE001
            LOGGER.warning("Failed exporting greeks_source audit: %s", _exc)

    payload = {
        "aggregate": aggregate_payload,
        "daily": daily,
        "greeks_source_exported": greeks_source_exported,
        "greeks_source_out_path": greeks_source_out_path,
    }

    # Persist aggregate metrics as a separate category for longitudinal analysis
    try:
        append_metric(settings.data_dir or "data", "backtest_range", aggregate_payload)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Backtest range metric persistence failed: %s", exc)

    typer.echo(json.dumps(payload, indent=2))


@app.command(name="greeks_provenance")
def greeks_provenance(
    symbol: str,
    interval: float = 1.0,
    start_date: str = typer.Option(..., help="Start date YYYY-MM-DD (inclusive)"),
    end_date: str = typer.Option(..., help="End date YYYY-MM-DD (inclusive)"),
    greeks_source: str = typer.Option("auto", help="Source policy to evaluate: auto|compute|proxy|none"),
    out: Path = typer.Option(None, help="Optional output path (uses suffix unless --format specified)"),
    format: str = typer.Option(None, help="Explicit output format: json|parquet|csv (overrides file extension)"),
    write_derived: bool = typer.Option(False, help="Also write to data/derived/provenance/greeks_source/date=YYYY-MM-DD/partition"),
):
    """Report which greeks source would be used per day over a range, without running a strategy.

    Logic mirrors backtest_range selection: persisted → compute → proxy → none (for 'auto').
    It does not fetch live chains or compute greeks; it only inspects presence of persisted factors.
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

    import pandas as _pd
    base_dir = Path(settings.data_dir or "data") / "derived" / "bars" / f"symbol={symbol}" / f"interval={interval}"
    daily = []
    missing: list[str] = []
    bars_by_source = {"persisted": 0, "compute": 0, "proxy": 0, "none": 0}
    days_by_source = {"persisted": 0, "compute": 0, "proxy": 0, "none": 0}
    greeks_records_total = 0

    from .utils.factor_store import load_greeks_factors  # lazy import

    cur = sd
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
        except Exception:
            missing.append(d_str)
            cur += timedelta(days=1)
            continue
        bars = int(len(df_day))
        persisted = False
        greeks_records = 0
        try:
            gdf = load_greeks_factors(settings.data_dir or "data", d_str)  # type: ignore[arg-type]
            persisted = bool(gdf is not None and not gdf.empty)
            greeks_records = 0 if (gdf is None or gdf.empty) else int(len(gdf))
        except Exception:
            persisted = False
            greeks_records = 0
        # Choose source according to policy
        gs = greeks_source.lower()
        chosen = "none"
        if gs == "auto":
            chosen = "persisted" if persisted else "compute"
        elif gs == "compute":
            chosen = "compute"
        elif gs == "proxy":
            chosen = "proxy"
        elif gs == "none":
            chosen = "none"
        else:
            chosen = "persisted" if persisted else "compute"

        daily.append({
            "date": d_str,
            "bars": bars,
            "persisted_available": persisted,
            "greeks_source_used": chosen,
            "greeks_records": greeks_records,
        })
        bars_by_source[chosen] = bars_by_source.get(chosen, 0) + bars
        days_by_source[chosen] = days_by_source.get(chosen, 0) + 1
        greeks_records_total += greeks_records
        cur += timedelta(days=1)

    payload = {
        "symbol": symbol,
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
        "dates_processed": len(daily),
        "missing_dates": missing,
        "bars_by_source": bars_by_source,
        "days_by_source": days_by_source,
        "greeks_records_total": greeks_records_total,
        "daily": daily,
    }
    # Optional export
    if out is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            chosen_fmt = (format or "").strip().lower() if format else None
            suf = out.suffix.lower()
            # If explicit format provided and mismatch with suffix, adjust path suffix
            fmt_to_suffix = {"json": ".json", "parquet": ".parquet", "csv": ".csv"}
            if chosen_fmt in fmt_to_suffix:
                desired_suffix = fmt_to_suffix[chosen_fmt]
                if suf != desired_suffix:
                    # Replace suffix with desired
                    out = out.with_suffix(desired_suffix)
                    payload.setdefault("export_note", "Output path suffix adjusted to match --format")
                suf = desired_suffix
            if suf == ".parquet":
                import pandas as _pd
                _pd.DataFrame(daily).to_parquet(out, index=False)  # type: ignore[arg-type]
            elif suf == ".csv":
                import pandas as _pd
                _pd.DataFrame(daily).to_csv(out, index=False)
            else:
                out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            payload["out_path"] = str(out)
        except Exception as _exc:  # noqa: BLE001
            payload.setdefault("export_error", str(_exc))
    # Optional write to derived partition for dashboards (one file covering the range)
    if write_derived:
        try:
            base = Path(settings.data_dir or "data") / "derived" / "provenance" / "greeks_source"
            # Use end_date for partition, include date range in file name
            part_dir = base / f"date={end_date}"
            part_dir.mkdir(parents=True, exist_ok=True)
            fname = f"prov_{symbol}_int={interval}_{start_date}_to_{end_date}.parquet"
            import pandas as _pd
            _pd.DataFrame(daily).to_parquet(part_dir / fname, index=False)  # type: ignore[arg-type]
            payload["derived_out_path"] = str(part_dir / fname)
        except Exception as _exc:  # noqa: BLE001
            payload.setdefault("derived_export_error", str(_exc))
    typer.echo(json.dumps(payload, indent=2))

@app.command(name="agent_demo")
def agent_demo(
    prompt: str = typer.Option("Summarize the market in one sentence.", help="Prompt for the agent/demo"),
    model: str = typer.Option("gpt-4o-mini", help="Model name for OpenAI/Agents"),
    backend: str = typer.Option("auto", help="Backend to use: agents, openai, or auto", case_sensitive=False),
    timeout: float = typer.Option(15.0, help="Max seconds to await an agents run (agents backend only)"),
    retries: int = typer.Option(1, help="Retries for transient agent errors (agents backend)"),
    backoff: float = typer.Option(1.0, help="Initial backoff seconds for retries"),
    log_run: bool = typer.Option(True, help="Persist a JSONL record of the agent run (agents backend)"),
):
    """Demo an AI agent run.

    Tries to use the 'agents' (openai-agents) package if available; otherwise falls back to OpenAI Chat Completions.
    Requires OPENAI_API_KEY in environment.
    """
    import os
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        typer.echo(json.dumps({"error": "OPENAI_API_KEY not set"}, indent=2))
        raise typer.Exit(1)
    backend_lower = backend.lower()
    last_err: Exception | None = None

    # Try openai-agents first if backend allows
    if backend_lower in ("agents", "auto"):
        try:
            import agents  # type: ignore
            from agents.agent import Agent  # type: ignore
            from agents.models.openai_provider import (  # type: ignore
                AsyncOpenAI,
                OpenAIChatCompletionsModel,
            )

            import os as _os
            oai_client = AsyncOpenAI(
                api_key=key,
                project=_os.getenv("OPENAI_PROJECT"),
                organization=_os.getenv("OPENAI_ORG"),
            )
            agents_model = OpenAIChatCompletionsModel(model, oai_client)
            agent = Agent(model=agents_model, name="demo-agent")
            raw_result = _agent_invoke(agent, prompt, retries=retries, backoff=backoff)
            result = _ensure_sync(raw_result, timeout=timeout)
            text = _normalize_agent_result(result)[:2000]
            payload = {"backend": "openai-agents", "ok": True, "text": text}
            typer.echo(json.dumps(payload, indent=2))
            if log_run:
                _log_agent_run({"cmd": "agent_demo", "model": model}, text, result)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            # If a project-scoped key is used without project context, provide hint later

    # Fallback to plain OpenAI if allowed
    if backend_lower in ("openai", "auto"):
        try:
            from openai import OpenAI
            org = os.getenv("OPENAI_ORG")
            proj = os.getenv("OPENAI_PROJECT")
            try:
                if proj:
                    client = OpenAI(api_key=key, project=proj)
                elif org:
                    client = OpenAI(api_key=key, organization=org)
                else:
                    client = OpenAI(api_key=key)
            except TypeError:
                client = OpenAI(api_key=key)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            text = resp.choices[0].message.content if resp and resp.choices else ""
            typer.echo(json.dumps({"backend": "openai", "ok": True, "text": text}, indent=2))
            return
        except Exception as exc2:  # noqa: BLE001
            last_err = exc2

    # If we reach here, emit a consistent error payload
    err_msg = str(last_err) if last_err else "unknown error"
    out = {"backend": backend_lower, "ok": False, "error": err_msg}
    if ("Incorrect API key" in err_msg) or ("invalid_api_key" in err_msg) or ("sk-proj-" in err_msg):
        out["hint"] = {
            "note": "If using a project-scoped key (sk-proj-), set OPENAI_PROJECT and optionally OPENAI_ORG.",
            "env_project": bool(os.getenv("OPENAI_PROJECT")),
            "env_org": bool(os.getenv("OPENAI_ORG")),
        }
    typer.echo(json.dumps(out, indent=2))
    return


@app.command(name="agent_greeks_summary")
def agent_greeks_summary(
    symbol: str = typer.Argument("SPX", help="Symbol label for context only (greeks_factor is stored per-day)"),
    start_date: str = typer.Option(None, help="Start date YYYY-MM-DD (inclusive)"),
    end_date: str = typer.Option(None, help="End date YYYY-MM-DD (inclusive)"),
    date: str = typer.Option(None, help="Single date YYYY-MM-DD (alternative to start/end)"),
    model: str = typer.Option("gpt-4o-mini", help="Model name for OpenAI/Agents"),
    backend: str = typer.Option("auto", help="Backend to use: agents, openai, or auto", case_sensitive=False),
    timeout: float = typer.Option(15.0, help="Max seconds to await an agents run (agents backend only)"),
    retries: int = typer.Option(1, help="Retries for transient agent errors (agents backend)"),
    backoff: float = typer.Option(1.0, help="Initial backoff seconds for retries"),
    max_points: int = typer.Option(200, help="Downsample greeks_norm series to at most this many points"),
    light: bool = typer.Option(False, help="If true, omit sample arrays to reduce payload"),
):
    """Have an AI agent analyze greeks_norm over a date range by injecting data into the prompt.

    Important: The agent does not get direct filesystem access; we load data locally and pass a summarized JSON snippet.
    """
    import os as _os
    import numpy as _np
    import pandas as _pd
    from datetime import datetime as _dt, timedelta as _td
    from .config import Settings as _Settings
    from .utils.factor_store import load_greeks_factors as _load

    key = _os.getenv("OPENAI_API_KEY")
    if not key:
        typer.echo(json.dumps({"ok": False, "error": "OPENAI_API_KEY not set"}, indent=2))
        raise typer.Exit(1)

    # Resolve dates
    if date and (start_date or end_date):
        typer.echo(json.dumps({"ok": False, "error": "Provide either --date or --start-date/--end-date, not both"}, indent=2))
        raise typer.Exit(1)
    if date is None and (start_date is None or end_date is None):
        today = _dt.utcnow().date().isoformat()
        start_date = start_date or today
        end_date = end_date or today

    def _iter_dates(sd: str, ed: str):
        d0 = _dt.strptime(sd, "%Y-%m-%d").date()
        d1 = _dt.strptime(ed, "%Y-%m-%d").date()
        cur = d0
        while cur <= d1:
            yield cur.isoformat()
            cur = cur + _td(days=1)

    dates = [date] if date else list(_iter_dates(start_date, end_date))  # type: ignore[arg-type]
    settings = _Settings()

    # Load greeks per day
    frames: list[_pd.DataFrame] = []
    for d in dates:
        try:
            df = _load(settings.data_dir or "data", d)
        except Exception:
            df = None
        if df is not None and not df.empty:
            f = df.copy()
            f["date"] = d
            frames.append(f)

    if not frames:
        typer.echo(json.dumps({"ok": False, "symbol": symbol, "dates": dates, "error": "No greeks_factor data found"}, indent=2))
        return

    all_df = _pd.concat(frames, ignore_index=True)
    # Downsample
    if max_points > 0 and len(all_df) > max_points:
        idx = _np.linspace(0, len(all_df) - 1, num=max_points, dtype=int)
        all_df = all_df.iloc[idx].reset_index(drop=True)

    # Compute basic stats
    if "greeks_norm" in all_df.columns:
        g = _pd.to_numeric(all_df["greeks_norm"], errors="coerce")
    else:
        g = _pd.Series(dtype=float)
    stats = {
        "points": int(len(all_df)),
        "mean": float(g.mean()) if len(g) else None,
        "min": float(g.min()) if len(g) else None,
        "max": float(g.max()) if len(g) else None,
        "last": float(g.iloc[-1]) if len(g) else None,
    }

    sample_cols = [c for c in ("ts", "date", "greeks_norm") if c in all_df.columns]
    sample = [] if light else all_df[sample_cols].to_dict(orient="records")
    prompt = (
        "You are a concise quant assistant. Analyze this greeks_norm series for " + symbol +
        ". Identify regime, notable shifts, and confidence. Keep it factual and non-advisory.\n\n" +
        json.dumps({"symbol": symbol, "dates": dates, "stats": stats, "sample": sample}, indent=2)
    )

    backend_lower = backend.lower()
    last_err = None
    if backend_lower in ("agents", "auto"):
        try:
            import agents  # type: ignore
            from agents.agent import Agent  # type: ignore
            from agents.models.openai_provider import AsyncOpenAI, OpenAIChatCompletionsModel  # type: ignore
            cli = AsyncOpenAI(api_key=key, project=_os.getenv("OPENAI_PROJECT"), organization=_os.getenv("OPENAI_ORG"))
            mdl = OpenAIChatCompletionsModel(model, cli)
            ag = Agent(model=mdl, name="greeks-agent")
            raw = _agent_invoke(ag, prompt, retries=retries, backoff=backoff)
            res = _ensure_sync(raw, timeout=timeout)
            text = _normalize_agent_result(res)[:2000]
            typer.echo(json.dumps({"backend": "openai-agents", "ok": True, "text": text, "stats": stats, "light": light}, indent=2))
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if backend_lower in ("openai", "auto"):
        try:
            from openai import OpenAI
            org = _os.getenv("OPENAI_ORG"); proj = _os.getenv("OPENAI_PROJECT")
            try:
                if proj:
                    client = OpenAI(api_key=key, project=proj)
                elif org:
                    client = OpenAI(api_key=key, organization=org)
                else:
                    client = OpenAI(api_key=key)
            except TypeError:
                client = OpenAI(api_key=key)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=400,
            )
            text = resp.choices[0].message.content if resp and resp.choices else ""
            typer.echo(json.dumps({"backend": "openai", "ok": True, "text": text, "stats": stats, "light": light}, indent=2))
            return
        except Exception as exc2:  # noqa: BLE001
            last_err = exc2
    err_msg = str(last_err) if last_err else "unknown error"
    out = {"backend": backend_lower, "ok": False, "error": err_msg, "stats": stats}
    if ("Incorrect API key" in err_msg) or ("invalid_api_key" in err_msg) or ("sk-proj-" in err_msg):
        out["hint"] = {
            "note": "If using a project-scoped key (sk-proj-), set OPENAI_PROJECT and optionally OPENAI_ORG.",
            "env_project": bool(_os.getenv("OPENAI_PROJECT")),
            "env_org": bool(_os.getenv("OPENAI_ORG")),
        }
    typer.echo(json.dumps(out, indent=2))

@app.command(name="agent_greeks_chat")
def agent_greeks_chat(
    symbol: str = typer.Argument("SPX"),
    start_date: str = typer.Option(None, help="Start date YYYY-MM-DD (inclusive)"),
    end_date: str = typer.Option(None, help="End date YYYY-MM-DD (inclusive)"),
    date: str = typer.Option(None, help="Single date YYYY-MM-DD"),
    model: str = typer.Option("gpt-4o-mini", help="OpenAI chat model"),
    system_prompt: str = typer.Option("You are a careful quant assistant. Use the greeks tool if needed.", help="System message"),
    max_turns: int = typer.Option(1, help="Max tool-call cycles before returning"),
):
    """Chat with an agent that can call a tool to fetch greeks summary on demand."""
    import os as _os
    import json as _json
    from typing import List, cast
    from openai import OpenAI
    from openai.types.chat import (
        ChatCompletionMessageParam,
        ChatCompletionToolMessageParam,
        ChatCompletionToolParam,
    )
    from .agent_tools import get_greeks_summary

    key = _os.getenv("OPENAI_API_KEY")
    if not key:
        typer.echo(json.dumps({"ok": False, "error": "OPENAI_API_KEY not set"}, indent=2))
        raise typer.Exit(1)

    org = _os.getenv("OPENAI_ORG"); proj = _os.getenv("OPENAI_PROJECT")
    try:
        if proj:
            client = OpenAI(api_key=key, project=proj)
        elif org:
            client = OpenAI(api_key=key, organization=org)
        else:
            client = OpenAI(api_key=key)
    except TypeError:
        client = OpenAI(api_key=key)

    # Prepare initial messages
    user_goal = f"Analyze greeks for {symbol} over the requested window and provide a concise non-advisory summary."
    messages: List[ChatCompletionMessageParam] = cast(List[ChatCompletionMessageParam], [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_goal},
    ])

    # Define tool
    func_def = {
        "name": "get_greeks_summary",
        "description": "Load greeks_norm over a date window and return stats and samples.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"]},
                "max_points": {"type": "integer", "minimum": 50, "maximum": 1000, "default": 400},
            },
            "required": ["symbol"],
        },
    }
    prov_def = {
        "name": "get_provenance_grouped",
        "description": "Summarize provenance by source for a date range using derived partitions.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "interval": {"type": "number", "default": 1.0},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"}
            },
            "required": ["symbol", "start_date", "end_date"],
        },
    }
    intraday_def = {
        "name": "get_greeks_summary_intraday",
        "description": "Summarize today's intraday greeks_norm up to now (UTC).",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "max_points": {"type": "integer", "minimum": 50, "maximum": 1000, "default": 400},
            },
            "required": ["symbol"],
        },
    }
    combo_def = {
        "name": "get_greeks_and_provenance",
        "description": "Return both greeks summary and grouped provenance in one call.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "interval": {"type": "number", "default": 1.0},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "date": {"type": ["string", "null"]},
                "max_points": {"type": "integer", "minimum": 50, "maximum": 1000, "default": 400},
            },
            "required": ["symbol"],
        },
    }
    tools = cast(List[ChatCompletionToolParam], [
        {"type": "function", "function": func_def},      # type: ignore[arg-type]
        {"type": "function", "function": prov_def},       # type: ignore[arg-type]
        {"type": "function", "function": intraday_def},   # type: ignore[arg-type]
        {"type": "function", "function": combo_def},      # type: ignore[arg-type]
    ])

    # Seed a suggestion to call the tool
    messages.append(cast(ChatCompletionMessageParam, {
        "role": "user",
        "content": _json.dumps({
            "hint": "If you need data, call get_greeks_summary with provided dates.",
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
            "date": date,
        }),
    }))

    turns = 0
    tool_called = False
    while turns < max_turns:
        turns += 1
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=500,
        )
        choice = resp.choices[0]
        msg = choice.message
        if getattr(msg, "tool_calls", None):
            # Execute tool(s) locally and append results
            for tc in msg.tool_calls:  # type: ignore[attr-defined]
                ttype = getattr(tc, "type", None)
                if ttype != "function":
                    continue
                fn = getattr(tc, "function", None)
                if not fn:
                    continue
                fname = getattr(fn, "name", "")
                try:
                    args = _json.loads(getattr(fn, "arguments", "{}") or "{}")
                except Exception:
                    args = {}
                if fname == "get_greeks_summary":
                    # default params fallbacks
                    args.setdefault("symbol", symbol)
                    if date:
                        args.setdefault("date", date)
                    else:
                        if start_date:
                            args.setdefault("start_date", start_date)
                        if end_date:
                            args.setdefault("end_date", end_date)
                    result = get_greeks_summary(**args)
                elif fname == "get_provenance_grouped":
                    from .agent_tools import get_provenance_grouped
                    args.setdefault("symbol", symbol)
                    if start_date:
                        args.setdefault("start_date", start_date)
                    if end_date:
                        args.setdefault("end_date", end_date)
                    result = get_provenance_grouped(**args)
                elif fname == "get_greeks_summary_intraday":
                    from .agent_tools import get_greeks_summary_intraday
                    args.setdefault("symbol", symbol)
                    result = get_greeks_summary_intraday(**args)
                elif fname == "get_greeks_and_provenance":
                    from .agent_tools import get_greeks_and_provenance
                    args.setdefault("symbol", symbol)
                    if date:
                        args.setdefault("date", date)
                    else:
                        if start_date:
                            args.setdefault("start_date", start_date)
                        if end_date:
                            args.setdefault("end_date", end_date)
                        args.setdefault("interval", 1.0)
                    result = get_greeks_and_provenance(**args)
                else:
                    continue
                tool_called = True
                tool_msg: ChatCompletionToolMessageParam = cast(ChatCompletionToolMessageParam, {
                    "role": "tool",
                    "tool_call_id": getattr(tc, "id", "tool.1"),
                    "name": fname,
                    "content": _json.dumps(result),
                })
                messages.append(cast(ChatCompletionMessageParam, tool_msg))

            # Loop back for the model to incorporate tool outputs
            continue

        # No tool calls; finalize
        content = msg.content or ""
        typer.echo(json.dumps({"ok": True, "tool_called": tool_called, "text": content}, indent=2))
        return
    # Max turns exhausted, return last content
    typer.echo(json.dumps({"ok": True, "tool_called": tool_called, "text": messages[-1].get("content", "")}, indent=2))


@app.command(name="agent_greeks_combo")
def agent_greeks_combo(
    symbol: str = typer.Argument("SPX"),
    start_date: str = typer.Option(None),
    end_date: str = typer.Option(None),
    date: str = typer.Option(None),
    interval: float = typer.Option(1.0),
    max_points: int = typer.Option(400),
):
    """Return both greeks summary and grouped provenance in one shot (no agent needed)."""
    from .agent_tools import get_greeks_and_provenance
    args = {"symbol": symbol, "interval": interval, "max_points": max_points}
    if date:
        args["date"] = date
    else:
        if start_date:
            args["start_date"] = start_date
        if end_date:
            args["end_date"] = end_date
    res = get_greeks_and_provenance(**args)  # type: ignore[arg-type]
    typer.echo(json.dumps(res, indent=2))


@app.command(name="agent_dashboard")
def agent_dashboard(
    symbol: str = typer.Argument("SPX"),
    start_date: str = typer.Option(None),
    end_date: str = typer.Option(None),
    date: str = typer.Option(None),
    interval: float = typer.Option(1.0),
    max_points: int = typer.Option(400),
    out_html: str = typer.Option("dashboard.html", help="Output HTML file or base name"),
    light: bool = typer.Option(True, help="Omit heavy arrays where possible"),
    open_html: bool = typer.Option(False, "--open", help="Open the HTML after generation"),
    sparkline: bool = typer.Option(True, help="Render an inline sparkline of greeks_norm if sample is available"),
    auto_name: bool = typer.Option(True, help="Auto-include symbol and date range in output filename"),
):
    """Generate a compact HTML dashboard with greeks summary and grouped provenance."""
    from .agent_tools import get_greeks_and_provenance
    args = {"symbol": symbol, "interval": interval, "max_points": max_points}
    if date:
        args["date"] = date
    else:
        if start_date:
            args["start_date"] = start_date
        if end_date:
            args["end_date"] = end_date
    res = get_greeks_and_provenance(**args)  # type: ignore[arg-type]
    if not res.get("ok"):
        typer.echo(json.dumps(res, indent=2))
        raise typer.Exit(1)
    g = res.get("greeks", {})
    p = res.get("provenance", {})
    summ = (g.get("summary") if isinstance(g, dict) else None) or {}
    grouped = (p.get("grouped") if isinstance(p, dict) else None) or []
    # Preserve sample for sparkline; then optionally trim for light mode
    sample_data = []
    if isinstance(summ, dict):
        sample_data = summ.get("sample", []) or []
    if light and isinstance(summ, dict) and "sample" in summ:
        summ = dict(summ)
        summ["sample"] = []
    # Build output path (auto-name if requested)
    from pathlib import Path as _Path
    out_path = _Path(out_html)
    rng_label = None
    if date:
        rng_label = date
    elif start_date or end_date:
        s = start_date or ""
        e = end_date or ""
        rng_label = f"{s}_to_{e}".strip("_")
    else:
        rng_label = ""
    if auto_name:
        base = out_path.stem
        parent = out_path.parent if out_path.parent.as_posix() not in ("", ".") else _Path(".")
        suffix = out_path.suffix or ".html"
        safe_rng = (rng_label or "").replace(":", "-")
        fname = f"{base}_{symbol}{('_' + safe_rng) if safe_rng else ''}{suffix}"
        out_path = parent / fname
    # Create HTML
    title = f"Agent Dashboard - {symbol}"
    html = [
        "<html><head><meta charset='utf-8'><title>" + title + "</title>",
        "<style>body{font-family:Segoe UI,Arial;margin:20px;} table{border-collapse:collapse} th,td{border:1px solid #ddd;padding:6px;} th{background:#f5f5f5}</style>",
        "</head><body>",
        f"<h2>{title}</h2>",
        (f"<div><strong>Window:</strong> {rng_label}</div>" if rng_label else ""),
        "<h3>Greeks Summary</h3>",
        "<pre>" + json.dumps(summ, indent=2) + "</pre>",
    ]
    # Optional sparkline from greeks_norm sample
    if sparkline and sample_data:
        try:
            vals = [float(d.get("greeks_norm")) for d in sample_data if d.get("greeks_norm") is not None]
            if len(vals) >= 2:
                w, h, pad = 360, 72, 6
                vmin = min(vals); vmax = max(vals)
                vrange = (vmax - vmin) or 1.0
                step = (w - 2*pad) / (len(vals) - 1)
                pts = []
                for i, v in enumerate(vals):
                    x = pad + i * step
                    y = pad + (h - 2*pad) * (1 - (v - vmin)/vrange)
                    pts.append((x, y))
                path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
                zero_line = ""
                if vmin <= 0.0 <= vmax:
                    zy = pad + (h - 2*pad) * (1 - (0.0 - vmin)/vrange)
                    zero_line = f"<line x1='{pad}' y1='{zy:.1f}' x2='{w-pad}' y2='{zy:.1f}' stroke='#ccc' stroke-dasharray='3,3'/>"
                html += [
                    "<h3>Greeks Sparkline</h3>",
                    f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}' xmlns='http://www.w3.org/2000/svg'>",
                    f"<rect x='0' y='0' width='{w}' height='{h}' fill='white' stroke='#eee' />",
                    zero_line,
                    f"<path d='{path}' fill='none' stroke='#3b82f6' stroke-width='2' />",
                    "</svg>",
                ]
        except Exception:
            pass
    # Grouped provenance table
    html += [
        "<h3>Provenance Grouped</h3>",
        "<table><thead><tr><th>symbol</th><th>interval</th><th>source</th><th>days</th><th>bars_total</th><th>persisted_days</th><th>greeks_records_total</th></tr></thead><tbody>",
    ]
    for row in grouped:
        html.append(
            f"<tr><td>{row.get('symbol','')}</td><td>{row.get('interval','')}</td><td>{row.get('greeks_source_used','')}</td>"
            f"<td>{row.get('days','')}</td><td>{row.get('bars_total','')}</td><td>{row.get('persisted_days','')}</td><td>{row.get('greeks_records_total','')}</td></tr>"
        )
    html += ["</tbody></table>", "</body></html>"]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    # Optionally open the file
    if open_html:
        try:
            import os as _os, webbrowser as _wb, pathlib as _pl
            p = _pl.Path(out_path).resolve()
            if _os.name == "nt" and hasattr(_os, "startfile"):
                _os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                _wb.open(p.as_uri())
        except Exception:
            pass
    typer.echo(json.dumps({"ok": True, "out": str(out_path), "symbol": symbol, "light": light, "opened": open_html, "sparkline": sparkline, "auto_name": auto_name}, indent=2))


@app.command(name="ensemble_gate")
def ensemble_gate_cli(
    expiry: str = typer.Option(None, help="Expiry (YYYY-MM-DD). Used to derive default paths when alerts/ticks not provided."),
    alerts_path: Path = typer.Option(None, help="Input rocket alerts JSONL (defaults logs/rocket_alerts_<expiry>.jsonl)"),
    ticks_path: Path = typer.Option(None, help="Ticks CSV from rocket_watch (defaults logs/rocket_ticks_<expiry>.csv)"),
    out_path: Path = typer.Option(None, help="Output filtered alerts JSONL (defaults logs/rocket_alerts_<expiry>_ensemble.jsonl)"),
    tsfm_mode: str = typer.Option("ewma", help="Nowcast backend: ewma (default), chronos, timesfm", case_sensitive=False),
    horizon_min: int = typer.Option(10, help="Horizon in minutes for TS nowcast"),
    confidence_thresh: float = typer.Option(0.58, help="Minimum confidence to keep an alert [0-1]"),
    max_size: float = typer.Option(3.0, help="Maximum size multiplier for high-confidence alerts"),
    device: str = typer.Option("auto", help="Compute device for nowcast backends (ewma ignores): auto|cpu|cuda|dml (DirectML)"),
):
    """Run the TS+microstructure ensemble gate to filter and size alerts.

    Writes a PnL-compatible JSONL mirroring the schema used by alerts_filter_size, with
    additional `ensemble` details and `size` field.
    """
    if not _HAS_ENSEMBLE:
        typer.echo(json.dumps({"ok": False, "error": "ensemble module unavailable in this environment"}, indent=2))
        raise typer.Exit(1)

    # Resolve paths like alerts_filter_size
    if alerts_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --alerts-path")
        alerts_path = Path(f"logs/rocket_alerts_{expiry}.jsonl")
    if ticks_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --ticks-path")
        ticks_path = Path(f"logs/rocket_ticks_{expiry}.csv")
    if out_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --out-path")
        out_path = Path(f"logs/rocket_alerts_{expiry}_ensemble.jsonl")

    if not alerts_path.exists():
        typer.echo(json.dumps({"ok": False, "error": f"alerts_path not found: {alerts_path}"}, indent=2))
        raise typer.Exit(1)
    if not ticks_path.exists():
        typer.echo(json.dumps({"ok": False, "error": f"ticks_path not found: {ticks_path}"}, indent=2))
        raise typer.Exit(1)

    res = _run_ensemble_gate(
        ticks_csv=str(ticks_path),
        alerts_in=str(alerts_path),
        out_path=str(out_path),
        tsfm_mode=tsfm_mode.lower(),
        horizon_min=int(horizon_min),
        confidence_thresh=float(confidence_thresh),
        max_size=float(max_size),
        device=device,
    )
    typer.echo(json.dumps(res, indent=2))


@app.command(name="alerts_agent_route")
def alerts_agent_route(
    expiry: str = typer.Option(None, help="Expiry (YYYY-MM-DD). Used to derive default paths when alerts not provided."),
    alerts_path: Path = typer.Option(None, help="Input rocket alerts JSONL (defaults logs/rocket_alerts_<expiry>.jsonl)"),
    out_path: Path = typer.Option(None, help="Output agent-decided alerts JSONL (defaults logs/rocket_alerts_<expiry>_agent.jsonl)"),
    symbol: str = typer.Option("SPX", help="Underlying symbol context passed to agent (SPX/SPXW)"),
    post_to_discord: bool = typer.Option(False, help="If set, post each agent alert to Discord"),
    discord_webhook_url: str = typer.Option(None, help="Discord webhook URL. If omitted, reads DISCORD_WEBHOOK_URL env var"),
    discord_username: str = typer.Option("ZeroDTE Alerts", help="Username to display when posting to Discord"),
):
    """Bypass the ensemble gate and route each alert through the agent-only decision engine.

    For every input alert row, we compute {cp, side, size, confidence} via the agent and write a PnL-compatible JSONL.
    """
    if alerts_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --alerts-path")
        alerts_path = Path(f"logs/rocket_alerts_{expiry}.jsonl")
    if out_path is None:
        if not expiry:
            raise typer.BadParameter("Provide --expiry or --out-path")
        out_path = Path(f"logs/rocket_alerts_{expiry}_agent.jsonl")
    if not alerts_path.exists():
        typer.echo(json.dumps({"ok": False, "error": f"alerts_path not found: {alerts_path}"}, indent=2))
        raise typer.Exit(1)

    try:
        from .agent.recommendation import get_agent_recommendation as _agent_reco
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": f"agent module unavailable: {exc}"}, indent=2))
        raise typer.Exit(1)

    wrote = 0
    # Correlation brake: avoid clustered same-direction opens
    try:
        from .portfolio.correlation import CorrelationBrake as _CorrelationBrake  # type: ignore
        corr_brake = _CorrelationBrake(state_path=Path("logs") / "corr_brake_state.json")
    except Exception:
        corr_brake = None
    hook = None
    if post_to_discord:
        try:
            import os as _os
            hook = discord_webhook_url or _os.environ.get("DISCORD_WEBHOOK_URL")
        except Exception:
            hook = None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with alerts_path.open("r", encoding="utf-8") as fin, out_path.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                alert = json.loads(line)
            except Exception:
                continue
            # Optional L2 snapshot read (best-effort)
            l2_snip = None
            try:
                import json as __json
                import pathlib as __pl
                l2p = __pl.Path("logs") / f"l2_{symbol.upper()}.json"
                if l2p.exists():
                    _txt = l2p.read_text(encoding="utf-8").strip()
                    if _txt:
                        l2_snip = __json.loads(_txt)
            except Exception:
                l2_snip = None
            res = _agent_reco(symbol, l2=l2_snip)
            if not res or not res.get("ok"):
                continue
            dec = res.get("decision", {})
            # Gate using correlation brake before recording BUY entries
            try:
                direction = None
                if isinstance(dec, dict):
                    cp_val = dec.get("cp")
                    if cp_val == "C":
                        direction = "CALL"
                    elif cp_val == "P":
                        direction = "PUT"
                side_val = (dec.get("side") or "").lower() if isinstance(dec, dict) else ""
                if corr_brake and direction and side_val == "buy":
                    if not corr_brake.allow(symbol, direction):
                        # Skip this alert due to correlation brake; log JSONL
                        try:
                            skips = Path("logs") / "corr_brake_skips.jsonl"
                            skips.parent.mkdir(parents=True, exist_ok=True)
                            payload = {
                                "ts": __import__("time").time(),
                                "symbol": symbol,
                                "direction": direction,
                                "source": "alerts_agent_route",
                                "reason": "correlation_brake",
                            }
                            with skips.open("a", encoding="utf-8") as _sk:
                                _sk.write(json.dumps(payload) + "\n")
                        except Exception:
                            pass
                        continue
            except Exception:
                pass
            # Merge minimal fields expected by downstream PnL pipeline
            alert["cp"] = dec.get("cp")
            alert["size"] = dec.get("size")
            alert["confidence"] = dec.get("confidence")
            # Normalize field names if original schema uses 'side' vs 'cp'
            if "side" in alert and alert["side"] in ("C", "P"):
                alert["side"] = alert["cp"]
            fout.write(json.dumps(alert) + "\n")
            wrote += 1
            # Record open after write for durability
            try:
                if corr_brake and side_val == "buy" and direction:
                    corr_brake.on_open(symbol, direction)
            except Exception:
                pass
            # Optional Discord post
            if hook:
                try:
                    from .integrations.discord_webhook import post_message as _post
                    ts = alert.get("ts") or alert.get("time") or alert.get("timestamp")
                    sym = alert.get("symbol") or symbol
                    strike = alert.get("strike") or alert.get("k")
                    cp = alert.get("cp")
                    sz = alert.get("size")
                    conf = alert.get("confidence")
                    msg = f"{ts} | {sym} {strike or ''} {cp} size={sz} conf={conf}"
                    _post(hook, msg.strip(), username=discord_username)
                except Exception:
                    pass
    typer.echo(json.dumps({"ok": True, "alerts_in": str(alerts_path), "alerts_out": str(out_path), "wrote": wrote, "agent_symbol": symbol}, indent=2))


@app.command(name="agent_recommend")
def agent_recommend_cli(
    symbol: str = typer.Argument("SPX", help="Underlying symbol context (SPX/SPXW)"),
    state_path: Path = typer.Option(Path("logs/agent_state.json"), help="Agent state path to read/write"),
):
    """Return an agent-only decision using fast Polygon context (news sentiment + price snapshot).

    Outputs JSON: { ok, symbol, sentiment, price, decision: { cp, side, size, confidence }, state_path }
    """
    try:
        from .agent.recommendation import get_agent_recommendation as _agent_reco
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": f"agent module unavailable: {exc}"}, indent=2))
        raise typer.Exit(1)
    res = _agent_reco(symbol, state_path=str(state_path))
    typer.echo(json.dumps(res, indent=2))


@app.command(name="agent_update_self")
def agent_update_self_cli(
    state_path: Path = typer.Option(Path("logs/agent_state.json"), help="Agent state file to (re)initialize"),
):
    """Initialize or refresh the agent self-awareness state file."""
    try:
        from .agent.recommendation import update_self_awareness as _update
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": f"agent module unavailable: {exc}"}, indent=2))
        raise typer.Exit(1)
    res = _update(str(state_path))
    typer.echo(json.dumps(res, indent=2))


@app.command(name="predict_intraday")
def predict_intraday_cli(
    ticks: str = typer.Option(..., help="Path to tick CSV with price and time columns"),
    alerts: str = typer.Option(..., help="Input alerts JSONL"),
    out: str = typer.Option(..., help="Output JSONL for scored/filtered alerts"),
    backend: str = typer.Option("ewma", help="TS backend: ewma|chronos|timesfm", case_sensitive=False),
    device: str = typer.Option("auto", help="Compute device: auto|cpu|cuda|dml|directml"),
    horizon_min: int = typer.Option(10, help="Forecast horizon in minutes"),
    confidence_thresh: float = typer.Option(0.58, help="Confidence threshold for keeping alerts"),
    max_size: float = typer.Option(3.0, help="Max size multiplier"),
    chain: str | None = typer.Option(None, help="Optional option chain file (CSV or JSONL) to enrich features"),
):
    """Blend TS nowcast and micro classifier to score and filter alerts (intraday)."""
    try:
        from .ensemble.decision import EnsembleDecider  # type: ignore
        from .utils.jsonl import read_jsonl, write_jsonl  # type: ignore
        from .utils.flex_ticks import load_ticks_csv as _load_ticks_flex  # type: ignore
    except Exception:
        typer.echo(json.dumps({"ok": False, "error": "ensemble decider unavailable"}, indent=2))
        raise typer.Exit(1)
    df = _load_ticks_flex(ticks, resample_to="1min", tz="America/New_York").set_index("ts").sort_index()

    # Optional chain enrichment: aggregate chain to per-minute and map to micro-feature inputs
    if chain:
        try:
            import pathlib as _pl
            import json as _json
            from .greeks.chain_aggregate import aggregate_chain as _aggregate_chain  # type: ignore

            def _load_chain_any(_path: str | None) -> pd.DataFrame:
                if not _path:
                    return pd.DataFrame()
                p = _pl.Path(_path)
                if not p.exists():
                    return pd.DataFrame()
                if p.suffix.lower() == ".jsonl":
                    rows: list[dict] = []
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            s = line.strip()
                            if not s:
                                continue
                            try:
                                rows.append(_json.loads(s))
                            except Exception:
                                continue
                    return pd.DataFrame(rows)
                try:
                    return pd.read_csv(p)
                except Exception:
                    return pd.DataFrame()

            _raw = _load_chain_any(chain)
            if not _raw.empty:
                _agg = _aggregate_chain(_raw).set_index("ts").sort_index()
                # Ensure timezone alignment with ET like ticks
                try:
                    _dti = pd.DatetimeIndex(_agg.index)
                    if getattr(_dti, "tz", None) is None:
                        _dti = _dti.tz_localize("America/New_York", nonexistent="shift_forward", ambiguous="infer")
                    else:
                        _dti = _dti.tz_convert("America/New_York")
                    _agg.index = _dti
                except Exception:
                    pass
                _cols6 = ["atm_iv", "rr25", "term_slope", "gex", "dex", "charm_proxy"]
                _join = _agg[[c for c in _cols6 if c in _agg.columns]]
                df = df.join(_join, how="left")
                for _c in _cols6:
                    if _c in df.columns:
                        df[_c] = pd.to_numeric(df[_c], errors="coerce").ffill().fillna(0.0)
                if "atm_iv" in df.columns:
                    _iv = pd.to_numeric(df["atm_iv"], errors="coerce")
                    df["iv"] = _iv.ffill().fillna(0.0)
                    try:
                        df["iv_rank"] = _iv.rank(pct=True, method="average").fillna(0.0)
                    except Exception:
                        df["iv_rank"] = 0.0
                if "gex" in df.columns:
                    df["gamma_proxy"] = pd.to_numeric(df["gex"], errors="coerce").ffill().fillna(0.0)
                if "rr25" in df.columns:
                    df["skew"] = pd.to_numeric(df["rr25"], errors="coerce").ffill().fillna(0.0)
        except Exception:
            # best-effort enrichment; continue silently on failure
            pass

    dev = "dml" if device.lower() == "directml" else device
    decider = EnsembleDecider(
        backend=backend.lower(),  # type: ignore[arg-type]
        device=dev,
        horizon_min=horizon_min,
        confidence_thresh=confidence_thresh,
        max_size=max_size,
    )
    alerts_iter = list(read_jsonl(alerts))
    results = decider.decide(df, alerts_iter)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(out, iter(results))
    typer.echo(json.dumps({
        "ok": True,
        "out": out,
        "backend": backend,
        "device": str(dev),
        "horizon_min": horizon_min,
        "confidence_thresh": confidence_thresh,
        "max_size": max_size,
        "alerts_in": alerts,
        "alerts_out_count": len(results),
    }, indent=2))


@app.command(name="stamp_close")
def stamp_close_cli(
    out: str = typer.Option("logs/close_stamp.json", help="Output JSON path"),
    label: str = typer.Option("close", help="Label to include in payload"),
):
    """Write a small JSON artifact with current UTC timestamp to logs."""
    from datetime import datetime as _dt, timezone as _tz
    payload = {"label": label, "timestamp": _dt.now(_tz.utc).isoformat()}
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    typer.echo(json.dumps({"ok": True, "out": str(p)}, indent=2))


@app.command(name="tsfm_zero_shot")
def tsfm_zero_shot_cli(
    ticks: str = typer.Option(..., help="Path to tick CSV with price and time columns"),
    price_col: str = typer.Option(None, help="Price column (auto-detect if not provided)"),
    time_col: str = typer.Option(None, help="Time column (timestamp/time/ts; auto-detect if not provided)"),
    horizon_min: int = typer.Option(5, help="Forecast horizon in minutes for zero-shot"),
):
    """Return a zero-shot probability-of-up using Chronos if available, else a fallback."""
    import pandas as _pd
    try:
        from .models.tsfm_zero_shot import TSFMZeroShot  # type: ignore
    except Exception as _e:
        typer.echo(json.dumps({"ok": False, "error": f"import error: {_e}"}, indent=2))
        raise typer.Exit(1)

    df = _pd.read_csv(ticks)
    df.columns = [c.strip().lower() for c in df.columns]
    # Time column
    tcol = (time_col or "").strip().lower() if time_col else None
    if not tcol:
        tcol = next((c for c in ("timestamp", "time", "ts") if c in df.columns), None)
    if not tcol:
        typer.echo(json.dumps({"ok": False, "error": "ticks missing timestamp/time/ts column"}, indent=2))
        raise typer.Exit(1)
    s = df[tcol]
    if _pd.api.types.is_numeric_dtype(s):
        vmax = _pd.to_numeric(s, errors="coerce").dropna().max()
        unit = "ms" if (vmax is not None and float(vmax) > 1e12) else "s"
        ts = _pd.to_datetime(s, unit=unit, utc=True)
    else:
        ts = _pd.to_datetime(s, utc=True)
    df = df.assign(_ts=ts).dropna(subset=["_ts"]).set_index("_ts").sort_index()

    # Price column
    pcol = (price_col or "").strip().lower() if price_col else None
    if not pcol:
        pcol = next((c for c in ("price", "mid", "last", "px") if c in df.columns), None)
    if not pcol:
        typer.echo(json.dumps({"ok": False, "error": "ticks missing price/mid/last/px column"}, indent=2))
        raise typer.Exit(1)
    series = _pd.to_numeric(df[pcol], errors="coerce").dropna()
    if series.size < 40:
        typer.echo(json.dumps({"ok": False, "error": "insufficient data (<40 points)"}, indent=2))
        raise typer.Exit(1)

    z = TSFMZeroShot(horizon=horizon_min)
    prob = float(z.prob_up(series))
    mode = "chronos" if getattr(z, "_chronos", False) else "fallback"
    out = {
        "ok": True,
        "prob_up": round(prob, 6),
        "mode": mode,
        "horizon_min": horizon_min,
        "n": int(series.size),
        "start": str(series.index[0]),
        "end": str(series.index[-1]),
    }
    typer.echo(json.dumps(out, indent=2))


@app.command(name="alerts_agent_stream")
def alerts_agent_stream(
    alerts_file: Path = typer.Argument(..., help="Path to a JSONL file to tail for incoming alerts"),
    out_path: Path = typer.Option(None, help="Output JSONL path for agent-decided alerts (appended)"),
    symbol: str = typer.Option("SPX", help="Underlying symbol context (SPX/SPXW)"),
    state_path: Path = typer.Option(Path("logs/agent_state.json"), help="Agent state path to persist across decisions"),
    tail_from_end: bool = typer.Option(True, help="If true, start tailing from EOF; otherwise process existing lines then continue"),
    run_minutes: int = typer.Option(420, help="How long to run the stream before exiting (minutes)"),
    idle_exit_minutes: int = typer.Option(60, help="Exit if no new lines for this many minutes"),
    post_to_discord: bool = typer.Option(False, help="If set, post each agent alert to Discord"),
    discord_webhook_url: str = typer.Option(None, help="Discord webhook URL. If omitted, reads DISCORD_WEBHOOK_URL env var"),
    discord_username: str = typer.Option("ZeroDTE Alerts", help="Username to display when posting to Discord"),
    use_inprocess_l2: bool = typer.Option(True, help="Best-effort: start an in-process IQFeed L2 stream and pass live microstructure metrics to the agent"),
    include_l2_in_output: bool = typer.Option(False, help="If true, attach the latest L2 metrics snapshot used to the output JSON for audit"),
):
    """Tail a JSONL alerts file, route each row through the agent, and optionally post to Discord.

    Intended for live sessions where an upstream process appends alerts to the file.
    """
    import os as _os
    import time as _time
    import json as _json

    start_ts = _time.time()
    last_line_ts = start_ts
    deadline = start_ts + max(1, int(run_minutes)) * 60
    idle_deadline = start_ts + max(1, int(idle_exit_minutes)) * 60

    try:
        from .agent.recommendation import get_agent_recommendation as _agent_reco
    except Exception as exc:
        typer.echo(_json.dumps({"ok": False, "error": f"agent module unavailable: {exc}"}, indent=2))
        raise typer.Exit(1)

    if out_path is None:
        alerts_file_name = alerts_file.name
        out_path = Path("logs") / alerts_file_name.replace(".jsonl", "_agent.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Discord hook resolution
    hook = None
    if post_to_discord:
        hook = discord_webhook_url or _os.environ.get("DISCORD_WEBHOOK_URL")

    # Correlation brake: avoid clustered same-direction opens
    try:
        from .portfolio.correlation import CorrelationBrake as _CorrelationBrake  # type: ignore
        corr_brake = _CorrelationBrake(state_path=Path("logs") / "corr_brake_state.json")
    except Exception:
        corr_brake = None

    # Optional: spin up an in-process L2 stream for live microstructure metrics
    l2_stream = None
    if use_inprocess_l2:
        try:
            from .datafeeds.iqfeed_l2 import L2Stream as _L2Stream  # type: ignore
            l2_stream = _L2Stream()
            l2_stream.start()
            l2_stream.watch(symbol)
        except Exception:
            l2_stream = None

    # Tail loop
    if not alerts_file.exists():
        typer.echo(_json.dumps({"ok": False, "error": f"alerts_file not found: {alerts_file}"}, indent=2))
        raise typer.Exit(1)

    try:
        with alerts_file.open("r", encoding="utf-8") as fin, out_path.open("a", encoding="utf-8") as fout:
            # Seek to end if requested
            if tail_from_end:
                fin.seek(0, 2)
            else:
                fin.seek(0, 0)

            processed = 0
            posted = 0
            while True:
                now = _time.time()
                if now >= deadline:
                    break
                if now >= idle_deadline:
                    break
                pos = fin.tell()
                line = fin.readline()
                if not line:
                    _time.sleep(0.5)
                    continue
                last_line_ts = _time.time()
                idle_deadline = last_line_ts + max(1, int(idle_exit_minutes)) * 60
                line = line.strip()
                if not line:
                    continue
                try:
                    alert = _json.loads(line)
                except Exception:
                    # Reset file pointer on partial line
                    fin.seek(pos)
                    _time.sleep(0.25)
                    continue

                # Optional: fetch latest L2 metric snapshot if available (best-effort)
                l2_snip = None
                # Prefer in-process live L2 if running
                if l2_stream is not None:
                    try:
                        ring = l2_stream.get_ring(symbol)
                        if ring:
                            m = ring[-1]
                            l2_snip = {
                                "ts": float(getattr(m, "ts", 0.0) or 0.0),
                                "symbol": getattr(m, "symbol", symbol),
                                "l2_imbalance_5": float(getattr(m, "l2_imbalance_5", 0.0) or 0.0),
                                "l2_imbalance_10": float(getattr(m, "l2_imbalance_10", 0.0) or 0.0),
                                "best_queue_pressure": float(getattr(m, "best_queue_pressure", 0.0) or 0.0),
                                "sweep_score": float(getattr(m, "sweep_score", 0.0) or 0.0),
                                "quote_staleness_ms": float(getattr(m, "quote_staleness_ms", 0.0) or 0.0),
                            }
                    except Exception:
                        l2_snip = None
                # Fallback to file snapshot if available
                if l2_snip is None:
                    try:
                        # Here we try reading a shared JSON snapshot logs/l2_<symbol>.json if present
                        import json as __json
                        import pathlib as __pl
                        l2p = __pl.Path("logs") / f"l2_{symbol.upper()}.json"
                        if l2p.exists():
                            _txt = l2p.read_text(encoding="utf-8").strip()
                            if _txt:
                                l2_snip = __json.loads(_txt)
                    except Exception:
                        l2_snip = None

                # Agent decision (pass l2 if available)
                res = _agent_reco(symbol, state_path=str(state_path), l2=l2_snip)
                if not res or not res.get("ok"):
                    continue
                dec = res.get("decision", {})
                # Gate using correlation brake before recording BUY entries
                direction = None
                side_val = ""
                try:
                    if isinstance(dec, dict):
                        cp_val = dec.get("cp")
                        if cp_val == "C":
                            direction = "CALL"
                        elif cp_val == "P":
                            direction = "PUT"
                    side_val = (dec.get("side") or "").lower() if isinstance(dec, dict) else ""
                    if corr_brake and direction and side_val == "buy":
                        if not corr_brake.allow(symbol, direction):
                            # Skip this alert due to correlation brake; log JSONL
                            try:
                                skips = Path("logs") / "corr_brake_skips.jsonl"
                                skips.parent.mkdir(parents=True, exist_ok=True)
                                payload = {
                                    "ts": _time.time(),
                                    "symbol": symbol,
                                    "direction": direction,
                                    "source": "alerts_agent_stream",
                                    "reason": "correlation_brake",
                                }
                                with skips.open("a", encoding="utf-8") as _sk:
                                    _sk.write(_json.dumps(payload) + "\n")
                            except Exception:
                                pass
                            continue
                except Exception:
                    pass
                alert["cp"] = dec.get("cp")
                alert["size"] = dec.get("size")
                alert["confidence"] = dec.get("confidence")
                if include_l2_in_output and isinstance(l2_snip, dict):
                    try:
                        alert["l2"] = l2_snip
                    except Exception:
                        pass
                if "side" in alert and alert["side"] in ("C", "P"):
                    alert["side"] = alert["cp"]
                fout.write(_json.dumps(alert) + "\n")
                fout.flush()
                processed += 1

                # Record open after write for durability
                try:
                    if corr_brake and side_val == "buy" and direction:
                        corr_brake.on_open(symbol, direction)
                except Exception:
                    pass

                if hook:
                    try:
                        from .integrations.discord_webhook import post_message as _post
                        ts = alert.get("ts") or alert.get("time") or alert.get("timestamp")
                        sym = alert.get("symbol") or symbol
                        strike = alert.get("strike") or alert.get("k")
                        cp = (alert.get("cp") or dec.get("cp"))
                        sz = (alert.get("size") or dec.get("size"))
                        conf = (alert.get("confidence") or dec.get("confidence"))
                        side_post = (dec.get("side") or alert.get("side") or "").upper()

                        # Build concise rationale
                        why_parts = []
                        try:
                            sent_val = float(res.get("sentiment", 0.0) or 0.0)
                            why_parts.append(f"news_sent={sent_val:+.2f}")
                        except Exception:
                            pass
                        try:
                            if isinstance(l2_snip, dict):
                                qp = float(l2_snip.get("best_queue_pressure", 0.0) or 0.0)
                                imb = float(l2_snip.get("l2_imbalance_5", 0.0) or 0.0)
                                if abs(qp) >= 0.1:
                                    why_parts.append(f"qp={qp:+.2f}")
                                if abs(imb) >= 0.1:
                                    why_parts.append(f"imb5={imb:+.2f}")
                        except Exception:
                            pass
                        try:
                            reg = None
                            if isinstance(res.get("regime"), dict):
                                reg = res["regime"].get("market_regime")
                            reg = reg or dec.get("regime")
                            if reg:
                                why_parts.append(f"regime={reg}")
                        except Exception:
                            pass
                        try:
                            vr = dec.get("veto_reason")
                            if vr:
                                why_parts.append(f"veto={vr}")
                        except Exception:
                            pass
                        # Fallback to decision.reason if present
                        if not why_parts:
                            try:
                                rsn = dec.get("reason")
                                if rsn:
                                    why_parts.append(str(rsn))
                            except Exception:
                                pass

                        head = f"{ts} | {sym} {strike or ''} {cp} {side_post} size={sz} conf={conf}"
                        why = ("why: " + ", ".join(why_parts)) if why_parts else None
                        content = head if not why else (head + "\n" + why)
                        r = _post(hook, content.strip(), username=discord_username)
                        if r.get("ok"):
                            posted += 1
                    except Exception:
                        pass
    finally:
        # Cleanup L2 stream
        try:
            if l2_stream is not None:
                try:
                    l2_stream.unwatch(symbol)
                except Exception:
                    pass
                l2_stream.stop()
        except Exception:
            pass

    typer.echo(_json.dumps({
        "ok": True,
        "alerts_file": str(alerts_file),
        "out": str(out_path),
        "processed": processed,
        "posted": posted,
        "run_minutes": run_minutes,
        "idle_exit_minutes": idle_exit_minutes,
    }, indent=2))


@app.command(name="weekly_outlook")
def weekly_outlook(
    out_md: Path = typer.Option(None, help="Output markdown path; defaults to logs/weekly_outlook_<date>.md"),
    years: int = typer.Option(10, help="Years of history for seasonality"),
    model: str = typer.Option("gpt-4o-mini", help="OpenAI model used for optional summary"),
    include_openai: bool = typer.Option(True, help="Include OpenAI summary if OPENAI_API_KEY set"),
    include_vix: bool = typer.Option(True, help="Include VIX last-week snapshot from Polygon (I:VIX)"),
    econ_json: Path = typer.Option(None, help="Optional JSON/JSONL with upcoming economic events (ET)"),
    earnings_json: Path = typer.Option(None, help="Optional JSON/JSONL with upcoming earnings (ET)"),
    post_to_discord: bool = typer.Option(False, help="If set, post the weekly outlook to Discord"),
    discord_webhook_url: str = typer.Option(None, help="Discord webhook URL. If omitted, reads DISCORD_WEBHOOK_URL env var"),
    discord_username: str = typer.Option("ZeroDTE Weekly", help="Username to display when posting to Discord"),
):
    """Generate a Sunday-night weekly outlook using Polygon daily aggregates."""
    import os as _os
    import json as _json
    import datetime as _dt
    from .datafeeds.polygon_client import PolygonClient, PolygonConfig
    settings = Settings()

    # Out path
    if out_md is None:
        d = _dt.datetime.now(_dt.timezone.utc).astimezone().strftime("%Y%m%d")
        out_md = Path("logs") / f"weekly_outlook_{d}.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)

    poly = PolygonClient(PolygonConfig.from_env())
    dfi = poly.fetch_index_daily_aggs("I:SPX", years=max(2, int(years)))
    if dfi is None or dfi.empty:
        typer.echo(_json.dumps({"ok": False, "error": "failed to fetch index aggregates"}, indent=2))
        raise typer.Exit(1)

    # Last week snapshot
    dfi = dfi.sort_values("date")
    last5 = dfi.tail(5).copy()
    last_week_ret = float(last5["ret"].sum(skipna=True)) if "ret" in last5 else 0.0
    last_close = float(last5["close"].iloc[-1]) if len(last5) else None
    # Pivots from last day
    P = None
    piv = {}
    if len(last5):
        h = float(last5["high"].iloc[-1])
        l = float(last5["low"].iloc[-1])
        c = float(last5["close"].iloc[-1])
        P = (h + l + c) / 3.0
        piv = {"pivot": P, "R1": 2 * P - l, "S1": 2 * P - h, "R2": P + (h - l), "S2": P - (h - l)}

    # Seasonality by weekday (0=Mon)
    wday = dfi.groupby("dow")["ret"].mean().to_dict()
    mon = float(wday.get(0, 0.0) or 0.0)
    tue = float(wday.get(1, 0.0) or 0.0)
    wed = float(wday.get(2, 0.0) or 0.0)
    thu = float(wday.get(3, 0.0) or 0.0)
    fri = float(wday.get(4, 0.0) or 0.0)

    # Current month stats
    cur_month = int(_dt.datetime.now(_dt.timezone.utc).astimezone().month)
    month_means = dfi.groupby("month")["ret"].mean().to_dict()
    month_mean = float(month_means.get(cur_month, 0.0) or 0.0)

    # Next Monday date label in Eastern Time
    try:
        from zoneinfo import ZoneInfo as _ZoneInfo  # py>=3.9
        tz_et = _ZoneInfo("America/New_York")
    except Exception:
        tz_et = None
    now_et = _dt.datetime.now(_dt.timezone.utc).astimezone(tz_et) if tz_et else _dt.datetime.now()
    today_et = now_et.date()
    dow = today_et.weekday()
    offset = (7 - dow) % 7
    if offset == 0:
        offset = 1
    next_monday = today_et + _dt.timedelta(days=offset)
    week_start = _dt.datetime.combine(next_monday, _dt.time.min).replace(tzinfo=tz_et)
    week_end = _dt.datetime.combine(next_monday + _dt.timedelta(days=4), _dt.time.max).replace(tzinfo=tz_et)

    def _pct(x: float) -> str:
        try:
            return f"{x*100:+.2f}%"
        except Exception:
            return str(x)

    lines: list[str] = []
    lines.append(f"# Weekly Outlook for week of {next_monday:%Y-%m-%d} (All times ET)")
    lines.append("")
    lines.append("## Last week snapshot (I:SPX)")
    if len(last5):
        lines.append(f"- Weekly change: {_pct(last_week_ret)}; last close: {last_close:,.2f}")
        lines.append(f"- Last day pivots: P={piv.get('pivot'):.2f} | R1={piv.get('R1'):.2f} R2={piv.get('R2'):.2f} | S1={piv.get('S1'):.2f} S2={piv.get('S2'):.2f}")
    lines.append("")

    # VIX section (optional)
    v_last_week = 0.0  # default VIX weekly change (used in tilt)
    if include_vix:
        try:
            dfv = poly.fetch_index_daily_aggs("I:VIX", years=5)
            if (dfv is None or dfv.empty) and hasattr(poly, "fetch_equity_daily_aggs"):
                dfv = poly.fetch_equity_daily_aggs("VIX", years=5)
        except Exception:
            dfv = None
        if dfv is not None and not dfv.empty:
            dfv = dfv.sort_values("date")
            v5 = dfv.tail(5).copy()
            v_last_week = float(v5["ret"].sum(skipna=True)) if "ret" in v5 else 0.0
            v_last_close = float(v5["close"].iloc[-1]) if len(v5) else None
            lines.append("## VIX snapshot")
            if v_last_close is not None:
                lines.append(f"- VIX weekly change: {_pct(v_last_week)}; last close: {v_last_close:,.2f}")
            lines.append("")

    # Optional economic calendar and earnings
    def _load_events(path: Path) -> list[dict]:
        if not path or not path.exists():
            return []
        try:
            txt = path.read_text(encoding="utf-8").strip()
            items: list[dict] = []
            if not txt:
                return []
            if txt.startswith("["):
                import json as __j
                arr = __j.loads(txt)
                if isinstance(arr, list):
                    items = [x for x in arr if isinstance(x, dict)]
            else:
                # JSONL
                import json as __j
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        obj = __j.loads(ln)
                        if isinstance(obj, dict):
                            items.append(obj)
                    except Exception:
                        continue
            return items
        except Exception:
            return []

    def _parse_dt_et(obj: dict) -> _dt.datetime | None:
        # Accept 'datetime', or 'date' + optional 'time'
        raw = obj.get("datetime") or obj.get("dt")
        if isinstance(raw, str):
            try:
                d = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=tz_et)
                return d.astimezone(tz_et) if tz_et else d
            except Exception:
                pass
        dpart = obj.get("date")
        tpart = obj.get("time") or obj.get("et_time")
        try:
            if isinstance(dpart, str):
                if tpart:
                    d = _dt.datetime.fromisoformat(f"{dpart}T{tpart}")
                else:
                    d = _dt.datetime.fromisoformat(f"{dpart}T00:00:00")
                if d.tzinfo is None:
                    d = d.replace(tzinfo=tz_et)
                return d.astimezone(tz_et) if tz_et else d
        except Exception:
            return None
        return None

    def _fmt_time(d: _dt.datetime | None) -> str:
        if not d:
            return ""
        try:
            return d.strftime("%a %H:%M")
        except Exception:
            return ""

    # Economic calendar section (file -> env -> Polygon)
    econ_source = None
    econ_items = _load_events(econ_json) if econ_json else []
    if econ_items:
        econ_source = "cli"
    if not econ_items:
        env_econ = _os.environ.get("WEEKLY_ECON_JSON")
        if env_econ and Path(env_econ).exists():
            econ_items = _load_events(Path(env_econ))
            if econ_items:
                econ_source = "env"
    if not econ_items and (_os.environ.get("WEEKLY_TRY_POLYGON_ECON", "0").lower() in ("1", "true", "yes")):
        try:
            econ_items = poly.fetch_economic_events_range(week_start.date(), week_end.date()) or []
            if econ_items:
                econ_source = "polygon"
                # Persist for traceability
                try:
                    fn = Path("logs") / f"econ_{next_monday:%Y%m%d}.json"
                    fn.parent.mkdir(parents=True, exist_ok=True)
                    fn.write_text(_json.dumps(econ_items), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            econ_items = []
    if not econ_items:
        # IQFeed fallback (env-file based)
        try:
            from .datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig  # type: ignore
            iq = IQFeedClient(IQFeedConfig.from_env())
            got = iq.fetch_economic_events_range(week_start.date(), week_end.date())
            if got:
                econ_items = got
                econ_source = "iqfeed"
        except Exception:
            pass
    if econ_items:
        week_econ: list[tuple[_dt.datetime, dict]] = []
        for it in econ_items:
            d = _parse_dt_et(it)
            if d and week_start <= d <= week_end:
                week_econ.append((d, it))
        week_econ.sort(key=lambda x: x[0])
        if week_econ:
            lines.append("## Key economic calendar (ET)")
            for d, it in week_econ[:30]:
                title = it.get("title") or it.get("event") or it.get("name") or "Event"
                pri = it.get("priority") or it.get("impact")
                pri_txt = f" [{pri}]" if pri else ""
                lines.append(f"- {_fmt_time(d)}: {title}{pri_txt}")
            lines.append("")

    # Earnings section (file -> env -> Polygon)
    earnings_source = None
    earn_items = _load_events(earnings_json) if earnings_json else []
    if earn_items:
        earnings_source = "cli"
    if not earn_items:
        env_earn = _os.environ.get("WEEKLY_EARNINGS_JSON")
        if env_earn and Path(env_earn).exists():
            earn_items = _load_events(Path(env_earn))
            if earn_items:
                earnings_source = "env"
    if not earn_items and (_os.environ.get("WEEKLY_TRY_POLYGON_EARNINGS", "0").lower() in ("1", "true", "yes")):
        try:
            earn_items = poly.fetch_earnings_range(week_start.date(), week_end.date()) or []
            if earn_items:
                earnings_source = "polygon"
                # Persist for traceability
                try:
                    fn = Path("logs") / f"earnings_{next_monday:%Y%m%d}.json"
                    fn.parent.mkdir(parents=True, exist_ok=True)
                    fn.write_text(_json.dumps(earn_items), encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            earn_items = []
    if not earn_items:
        # IQFeed fallback (env-file based)
        try:
            from .datafeeds.iqfeed_client import IQFeedClient, IQFeedConfig  # type: ignore
            iq = IQFeedClient(IQFeedConfig.from_env())
            got2 = iq.fetch_earnings_range(week_start.date(), week_end.date())
            if got2:
                earn_items = got2
                earnings_source = "iqfeed"
        except Exception:
            pass
    if earn_items:
        week_earn: list[tuple[_dt.datetime, dict]] = []
        for it in earn_items:
            d = _parse_dt_et(it)
            if d and week_start <= d <= week_end:
                week_earn.append((d, it))
        week_earn.sort(key=lambda x: x[0])
        if week_earn:
            lines.append("## Notable earnings this week (ET)")
            for d, it in week_earn[:40]:
                tick = it.get("ticker") or it.get("symbol") or it.get("sym") or ""
                comp = it.get("company") or it.get("name") or ""
                tod = it.get("when") or it.get("session") or it.get("timing") or ""
                desc = " ".join(x for x in [tick, comp] if x).strip()
                suffix = f" ({tod})" if tod else ""
                lines.append(f"- {_fmt_time(d)}: {desc}{suffix}")
            lines.append("")
    # Weekly market tilt (lightweight blend). Positive last week and positive monthly seasonality are bullish; rising VIX is bearish.
    try:
        score = 0.5 + 0.4 * float(last_week_ret) + 0.2 * float(month_mean) - 0.3 * float(v_last_week)
        score = float(max(0.0, min(1.0, score)))
        tilt_label = "Bullish" if score > 0.55 else ("Bearish" if score < 0.45 else "Neutral")
        lines.append("## Market tilt (weekly)")
        lines.append(f"- Tilt: {tilt_label} (score {score:.2f})")
        lines.append("  - Legend: <0.45 Bearish, 0.45–0.55 Neutral, >0.55 Bullish; blend of last week return, monthly seasonality, and VIX weekly change")
        lines.append("")
    except Exception:
        tilt_label = None
        score = None

    lines.append("## 10y weekday seasonality (avg daily returns)")
    lines.append(f"- Mon {_pct(mon)} | Tue {_pct(tue)} | Wed {_pct(wed)} | Thu {_pct(thu)} | Fri {_pct(fri)}")
    lines.append(f"- Current month mean daily return: {_pct(month_mean)}")

    # Optional OpenAI summary
    if include_openai and settings.has_openai:
        try:
            wrapper = OpenAIWrapper(settings.openai_api_key)
            features = {
                "last_week_ret": float(last_week_ret),
                "weekday_mon": mon,
                "weekday_tue": tue,
                "weekday_wed": wed,
                "weekday_thu": thu,
                "weekday_fri": fri,
                "month_mean": month_mean,
                "last_close": last_close,
                "pivot": P,
            }
            summary = wrapper.summarize_market(features)
            lines.append("")
            lines.append("## Outlook summary")
            lines.append(summary.strip())
        except Exception as _exc:  # noqa: BLE001
            lines.append("")
            lines.append(f"(OpenAI error: {_exc})")

    md_text = "\n".join(lines)
    out_md.write_text(md_text, encoding="utf-8")

    posted = False
    post_status = None
    if post_to_discord:
        try:
            from .integrations.discord_webhook import post_chunks as _post_chunks
            hook = discord_webhook_url or _os.environ.get("DISCORD_WEBHOOK_URL")
            if hook:
                res = _post_chunks(hook, md_text, username=discord_username)
                posted = bool(res.get("ok"))
                post_status = int(res.get("status", 0) or 0)
        except Exception:
            posted = False
            post_status = None

    typer.echo(_json.dumps({
        "ok": True,
        "out": str(out_md),
        "posted_to_discord": posted,
        "discord_status": post_status,
        "years": years,
        "econ_source": econ_source,
        "earnings_source": earnings_source,
        "tilt": tilt_label if 'tilt_label' in locals() else None,
        "tilt_score": score if 'score' in locals() else None,
    }, indent=2))


if __name__ == "__main__":
    # Invoke Typer application when module is executed as a script
    app()
