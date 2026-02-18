import argparse
import copy
import difflib
import json
import os
import sys
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SYS_PATH_INSERTED = False
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
    SYS_PATH_INSERTED = True

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

try:
    from delivery.discord_bot import INSIGHTS_TECHNICAL_TERMS as _BOT_TECH_TERMS
except Exception:  # noqa: BLE001 - fail-soft when discord_bot cannot load
    _BOT_TECH_TERMS = None

from zero_dte_pipeline.tech.contracts import (
    ContractConstraints,
    DEFAULT_MAX_CHARS_BY_PAYLOAD,
    DEFAULT_MAX_CHARS_DEFAULT,
    DEFAULT_MAX_EMOJI,
    DEFAULT_MAX_LINES,
    INSIGHTS_TECHNICAL_TERMS,
    contract_violations,
)

OUTPUT_ROOT = ROOT / "tmp" / "rendered"
GOLDEN_ROOT = ROOT / "tests" / "goldens"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FUTURES_FIXTURE_PATH = FIXTURE_DIR / "futures_context_fixture.json"

SCENARIO_ALL = (
    "baseline",
    "futures_missing",
    "futures_stale",
    "participation_confirm",
    "participation_mixed",
    "participation_conflict_high_edge",
)
DEFAULT_SCENARIOS = ("baseline",)
MAX_LINES = int(os.getenv("SMOKE_MAX_LINES", str(DEFAULT_MAX_LINES)))
MAX_EMOJI = int(os.getenv("SMOKE_MAX_EMOJI", str(DEFAULT_MAX_EMOJI)))
MAX_CHARS_DEFAULT = int(os.getenv("SMOKE_MAX_CHARS_DEFAULT", str(DEFAULT_MAX_CHARS_DEFAULT)))
MAX_CHARS_BY_PAYLOAD = {
    "daily_summary": int(os.getenv("SMOKE_MAX_CHARS_DAILY_SUMMARY", str(DEFAULT_MAX_CHARS_BY_PAYLOAD["daily_summary"]))),
    "daily_prep": int(os.getenv("SMOKE_MAX_CHARS_DAILY_PREP", str(DEFAULT_MAX_CHARS_BY_PAYLOAD["daily_prep"]))),
    "after_hours": int(os.getenv("SMOKE_MAX_CHARS_AFTER_HOURS", str(DEFAULT_MAX_CHARS_BY_PAYLOAD["after_hours"]))),
    "pre_market": int(os.getenv("SMOKE_MAX_CHARS_PRE_MARKET", str(DEFAULT_MAX_CHARS_BY_PAYLOAD["pre_market"]))),
    "focus_list": int(os.getenv("SMOKE_MAX_CHARS_FOCUS_LIST", str(DEFAULT_MAX_CHARS_BY_PAYLOAD["focus_list"]))),
    "intraday_update": int(os.getenv("SMOKE_MAX_CHARS_INTRADAY_UPDATE", str(DEFAULT_MAX_CHARS_BY_PAYLOAD["intraday_update"]))),
}
FUTURES_STALE_DELTA = timedelta(hours=24)

PIVOT_FIXTURE: Dict[str, Dict[str, float]] = {
    "SPY": {"P": 679.00, "R1": 681.00, "S1": 677.00, "R2": 683.00, "S2": 675.00},
    "QQQ": {"P": 615.10, "R1": 617.25, "S1": 612.95, "R2": 619.40, "S2": 610.80},
    "ES": {"P": 4834.75, "R1": 4850.50, "S1": 4819.00, "R2": 4866.25, "S2": 4803.25},
}

if isinstance(_BOT_TECH_TERMS, (list, tuple)):
    FORBIDDEN_TERMS: Tuple[str, ...] = tuple(_BOT_TECH_TERMS)
else:
    FORBIDDEN_TERMS = INSIGHTS_TECHNICAL_TERMS


def _contract_constraints() -> ContractConstraints:
    return ContractConstraints(
        max_lines=MAX_LINES,
        max_emoji=MAX_EMOJI,
        max_chars_default=MAX_CHARS_DEFAULT,
        max_chars_by_payload=MAX_CHARS_BY_PAYLOAD,
        forbidden_technical_terms=FORBIDDEN_TERMS,
    )


def _ensure_sys_path() -> None:
    global SYS_PATH_INSERTED
    if SYS_PATH_INSERTED:
        return
    sys.path.insert(0, str(ROOT))
    SYS_PATH_INSERTED = True


def _load_futures_fixture() -> Tuple[dict, datetime]:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    if not FUTURES_FIXTURE_PATH.exists():
        raise FileNotFoundError(f"Missing fixture file: {FUTURES_FIXTURE_PATH}")

    with FUTURES_FIXTURE_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    ctx = data.setdefault("context", {})

    iso_ts = data.get("ts") or ctx.get("computed_ts")
    dirty = False

    if iso_ts:
        try:
            now_et = datetime.fromisoformat(iso_ts)
        except ValueError:
            now_et = datetime.now(ZoneInfo("America/New_York")).replace(microsecond=0)
            iso_ts = now_et.isoformat()
            dirty = True
    else:
        now_et = datetime.now(ZoneInfo("America/New_York")).replace(microsecond=0)
        iso_ts = now_et.isoformat()
        dirty = True

    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        now_et = now_et.astimezone(ZoneInfo("America/New_York"))
        iso_ts = now_et.isoformat()

    session_date = data.get("session_date") or ctx.get("session_date") or now_et.date().isoformat()

    if data.get("ts") != iso_ts:
        data["ts"] = iso_ts
        dirty = True
    if data.get("session_date") != session_date:
        data["session_date"] = session_date
        dirty = True
    if ctx.get("session_date") != session_date:
        ctx["session_date"] = session_date
        dirty = True
    if ctx.get("computed_ts") != iso_ts:
        ctx["computed_ts"] = iso_ts
        dirty = True

    if dirty:
        with FUTURES_FIXTURE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")

    return data, now_et


def _prepare_futures_path(base_data: dict, scenario: str, now_et: datetime) -> Path:
    scenario = scenario.lower()
    if scenario == "baseline":
        return FUTURES_FIXTURE_PATH

    if scenario == "futures_missing":
        missing_path = FIXTURE_DIR / "futures_context_missing.json"
        if missing_path.exists():
            missing_path.unlink()
        return missing_path

    if scenario == "futures_stale":
        stale_data = copy.deepcopy(base_data)
        stale_dt = now_et - FUTURES_STALE_DELTA
        stale_iso = stale_dt.isoformat()
        session_date = stale_dt.date().isoformat()
        ctx = stale_data.setdefault("context", {})
        stale_data["ts"] = stale_iso
        stale_data["session_date"] = session_date
        ctx["session_date"] = session_date
        ctx["computed_ts"] = stale_iso
        stale_path = FIXTURE_DIR / "futures_context_stale.json"
        with stale_path.open("w", encoding="utf-8") as handle:
            json.dump(stale_data, handle, indent=2)
            handle.write("\n")
        return stale_path

    if scenario in {
        "participation_confirm",
        "participation_mixed",
        "participation_conflict_high_edge",
    }:
        return FUTURES_FIXTURE_PATH

    raise ValueError(f"Unsupported scenario: {scenario}")


def _stub_pivots(now_et: datetime) -> Callable[[str], Dict[str, Any]]:
    session_date = now_et.date().isoformat()
    ts_iso = now_et.isoformat()

    def _builder(symbol: str) -> Dict[str, Any]:
        key = symbol.upper()
        piv = PIVOT_FIXTURE.get(key, PIVOT_FIXTURE["SPY"])
        return {
            "ts": ts_iso,
            "session": session_date,
            "H": piv["R2"],
            "L": piv["S2"],
            "C": piv["P"],
            "piv": piv,
        }

    return _builder


def _stub_signal_context(now_et: datetime) -> Callable[[str], Tuple[List[str], Tuple[Any, ...]]]:
    iso_ts = now_et.isoformat()

    def _builder(symbol: str) -> Tuple[str, Tuple[Any, ...]]:
        columns = ["ts", "prob_up", "prob_down", "model_version", "edge"]
        row = (iso_ts, 0.62, 0.38, "fixture-model", 0.12)
        return (columns, row)

    return _builder


def _stub_rolling_accuracy(*_: Any, **__: Any) -> Dict[str, Any]:
    return {"hits": 18, "matured": 24, "considered": 30, "acc": 0.75}


def _stub_latest_signal(now_et: datetime) -> Callable[[str], Dict[str, Any]]:
    iso_ts = now_et.isoformat()

    def _builder(symbol: str) -> Dict[str, Any]:
        return {
            "ts": iso_ts,
            "horizon_min": 5,
            "prob_up": 0.63,
            "prob_down": 0.37,
            "model_version": "fixture-model",
            "meta": "",
        }

    return _builder


def _stub_vix_confirmation(tf: str = "5m") -> Tuple[str, str, Dict[str, Any]]:
    return (
        "BULLISH",
        "Fixture bias: VIX easing with SQQQ lower",
        {"tf": tf, "vix_trend": "down", "sqqq_dir": "down"},
    )


def _build_regime_fixture(now_et: datetime) -> Dict[str, List[str]]:
    iso_date = now_et.date().isoformat()
    return {
        "default": [f"Balance against ON range ({iso_date})."],
        "after_hours": ["Balance with buyers defending ON mid."],
        "pre_market": ["Still balanced; watch for break of ON range."],
        "focus_list": ["Focus on relative strength groups vs ON range."],
        "intraday_update": ["Monitoring for acceptance at key zones to confirm posture."],
    }


def _build_levels_fixture() -> Dict[str, List[Dict[str, str]]]:
    common_levels = [
        {"label": "SPY 677", "note": "Support zone reference."},
        {"label": "QQQ 613", "note": "Support zone reference."},
    ]
    return {
        "default": common_levels,
        "after_hours": common_levels,
        "pre_market": common_levels,
        "focus_list": common_levels,
        "intraday_update": common_levels,
    }


def _build_options_focus_fixture() -> Dict[str, List[Dict[str, str]]]:
    common = [
        {"label": "Gamma", "note": "NORMAL"},
        {"label": "Theta", "note": "NORMAL"},
        {"label": "Vol", "note": "NORMAL"},
        {"label": "Behavior", "note": "Reduced participation until posture confirms."},
    ]
    return {
        "default": common,
        "after_hours": common,
        "pre_market": common,
        "focus_list": common,
        "intraday_update": common,
    }


def _smart_market_iq_fixture(now_et: datetime, scenario: str) -> Dict[str, Any]:
    stamp = now_et.strftime("%Y-%m-%d %H:%M:%S")
    base: Dict[str, Any] = {
        "timestamp_et": stamp,
        "data_quality": "OK",
        "top_gainers": ["NVDA", "AMD", "TSLA"],
        "top_losers": ["XLU", "PG", "KO"],
        "regime": "RISK_ON",
    }

    scenario = scenario.lower()
    if scenario == "participation_mixed":
        base["regime"] = "MIXED"
    elif scenario == "participation_conflict_high_edge":
        base["regime"] = "DEFENSIVE"
    elif scenario == "futures_missing":
        base["data_quality"] = "MISSING"
        base["regime"] = "UNKNOWN"
        base["top_gainers"] = []
        base["top_losers"] = []

    return base


def _extract_participation_gate(context: Optional[Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(context, dict):
        return None
    mp_ctx = context.get("market_participation")
    if not isinstance(mp_ctx, dict):
        return None
    gate = mp_ctx.get("gate")
    return gate if isinstance(gate, dict) else None


def _assert_participation_expectations(
    scenario: str,
    label: str,
    text: str,
    context: Optional[Any],
) -> None:
    scenario = scenario.lower()
    label = label.lower()
    if scenario == "participation_conflict_high_edge" and label in {"daily_prep", "focus_list", "intraday_update"}:
        lowered = text.lower()
        if "market participation conflict" not in lowered:
            raise AssertionError(f"{scenario}:{label} missing conflict reason")
        if "stand-down" not in lowered:
            raise AssertionError(f"{scenario}:{label} missing stand-down language")
        gate = _extract_participation_gate(context)
        if not gate or gate.get("impact") != "STAND_DOWN":
            raise AssertionError(f"{scenario}:{label} expected gate impact STAND_DOWN, got {gate}")
    elif scenario == "participation_mixed" and label in {"focus_list", "intraday_update"}:
        if "Participation caution" not in text:
            raise AssertionError(f"{scenario}:{label} missing participation caution note")
        gate = _extract_participation_gate(context)
        if not gate or gate.get("impact") != "CAUTION":
            raise AssertionError(f"{scenario}:{label} expected gate impact CAUTION, got {gate}")
    elif scenario == "participation_confirm" and label in {"focus_list", "intraday_update"}:
        gate = _extract_participation_gate(context)
        if not gate or gate.get("impact") != "ALLOW":
            raise AssertionError(f"{scenario}:{label} expected gate impact ALLOW, got {gate}")


def _json_default(obj: Any) -> str:
    return str(obj)


def _extract_text_and_context(output: Any) -> Tuple[str, Optional[Any]]:
    context: Optional[Any] = None

    if hasattr(output, "text") and isinstance(getattr(output, "text"), str):
        text_val = getattr(output, "text")
        for attr in ("agent_payload", "payload", "context", "data", "json"):
            candidate = getattr(output, attr, None)
            if isinstance(candidate, (dict, list)):
                context = candidate
                break
        return text_val, context

    if isinstance(output, dict):
        for key in ("text", "content", "message", "rendered", "body"):
            val = output.get(key)
            if isinstance(val, str):
                text_val = val
                break
            if isinstance(val, (list, tuple)):
                text_val = "\n".join(str(item) for item in val)
                break
        else:
            text_val = str(output)

        for key in ("agent_payload", "payload", "context", "data", "json", "details"):
            if isinstance(output.get(key), (dict, list)):
                context = output[key]
                break
        return text_val, context

    if isinstance(output, tuple) and len(output) == 2 and isinstance(output[0], str) and isinstance(output[1], (dict, list)):
        return output[0], output[1]

    if isinstance(output, (list, tuple)):
        return "\n".join(str(item) for item in output), None

    return str(output), None


def _resolve_builder(module: Any, attr: str) -> Optional[Callable[..., Any]]:
    func = getattr(module, attr, None)
    if attr.endswith("_payload"):
        render_attr = attr.replace("_payload", "_render")
        render_func = getattr(module, render_attr, None)
        if callable(render_func):
            return render_func
    return func if callable(func) else None


def _render_and_capture(directory: Path, name: str, builder: Callable[..., Any], *args: Any, **kwargs: Any) -> Optional[Tuple[str, Optional[Any]]]:
    try:
        output = builder(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {name}: {exc}")
        return None

    if output is None:
        print(f"[WARN] {name}: returned None")
        return None

    text, context = _extract_text_and_context(output)

    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / f"{name}.txt"
    out_path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")

    if isinstance(context, (dict, list)):
        context_path = directory / f"{name}.json"
        try:
            with context_path.open("w", encoding="utf-8") as handle:
                json.dump(context, handle, indent=2, default=_json_default)
                handle.write("\n")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Failed to write context for {name}: {exc}")

    print("\n===", name, "===")
    print(text)
    print()
    return text, context


def _handle_golden(
    golden_dir: Path,
    label: str,
    text: str,
    *,
    compare: bool,
    mismatches: List[str],
    scenario: str,
) -> None:
    golden_dir.mkdir(parents=True, exist_ok=True)
    golden_path = golden_dir / f"{label}.txt"
    content = text.rstrip("\n") + "\n"

    if compare and golden_path.exists():
        expected = golden_path.read_text(encoding="utf-8")
        if expected != content:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"expected/{scenario}/{label}.txt",
                    tofile=f"actual/{scenario}/{label}.txt",
                )
            )
            print(f"[DIFF] {scenario}:{label}\n{diff}")
            mismatches.append(f"{scenario}:{label}")
        return

    if not golden_path.exists() or not compare:
        golden_path.write_text(content, encoding="utf-8")
        if compare:
            print(f"[NEW] Golden created for {scenario}:{label}.")


def _enforce_contract(text: str, label: str, scenario: str, context: Optional[Any]) -> None:
    violations = contract_violations(
        text,
        label=label,
        context=context,
        constraints=_contract_constraints(),
    )
    if violations:
        summary = "; ".join(violations[:5])
        raise AssertionError(f"{scenario}:{label} contract violations: {summary}")


def _prepare_modules(fixture_data: dict, now_et: datetime, futures_path: Path, scenario: str) -> Dict[str, Any]:
    _ensure_sys_path()

    os.environ["FUTURES_CONTEXT_PATH"] = str(futures_path)

    bot = import_module("delivery.discord_bot")

    global FORBIDDEN_TERMS
    bot_terms = getattr(bot, "INSIGHTS_TECHNICAL_TERMS", INSIGHTS_TECHNICAL_TERMS)
    if isinstance(bot_terms, (list, tuple)):
        FORBIDDEN_TERMS = tuple(bot_terms)
    else:
        FORBIDDEN_TERMS = INSIGHTS_TECHNICAL_TERMS

    bot.FUTURES_CONTEXT_PATH = str(futures_path)
    setattr(bot, "_DEFAULT_FUTURES_CONTEXT_PATH", Path(futures_path))

    pivot_stub = _stub_pivots(now_et)
    bot.get_latest_daily_pivots = pivot_stub  # type: ignore[assignment]

    bot._now_et = lambda: now_et  # type: ignore[attr-defined]
    bot.vix_sqqq_confirmation = _stub_vix_confirmation  # type: ignore[assignment]
    bot.get_latest_signal = _stub_latest_signal(now_et)  # type: ignore[assignment]
    bot.get_analysis_mode = lambda: "strict"  # type: ignore[assignment]
    bot.get_latest_signal_context = _stub_signal_context(now_et)  # type: ignore[assignment]
    bot.get_vix_context = lambda tf="1m": {"level": 14.2, "ts": now_et.isoformat()}  # type: ignore[assignment]
    bot.vix_gating_action = lambda level: {"mode": "OK", "reason": "fixture", "mult": 1.0}  # type: ignore[assignment]
    bot.explain_no_trade = lambda *args, **kwargs: (  # type: ignore[assignment]
        ["Fixture rationale: awaiting valid signal."],
        "Next check in 10 min.",
    )
    bot._utc_now_iso = lambda: now_et.isoformat()  # type: ignore[assignment]
    bot.rolling_accuracy = _stub_rolling_accuracy  # type: ignore[assignment]

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Optional[ZoneInfo] = None) -> datetime:  # type: ignore[override]
            if tz is None:
                return now_et.replace(tzinfo=None)
            return now_et.astimezone(tz)

    bot.datetime = _FixedDatetime  # type: ignore[assignment]

    if not hasattr(bot, "_set_now_provider"):
        raise RuntimeError("discord_bot missing _set_now_provider hook")
    bot._set_now_provider(lambda: now_et)

    base_last_px_ts = (now_et - timedelta(minutes=1)).replace(second=0, microsecond=0)
    stale_last_px_ts = (now_et - timedelta(minutes=45)).replace(second=0, microsecond=0)
    stale_scenarios = {"futures_missing", "futures_stale"}
    use_stale_prices = scenario in stale_scenarios
    ts_for_prices = stale_last_px_ts if use_stale_prices else base_last_px_ts
    age_minutes = (now_et - ts_for_prices).total_seconds() / 60.0

    def _build_last_price(sym: str, price: float) -> Dict[str, Any]:
        ok = not use_stale_prices
        reason = None if ok else "stale_price_open>10m"
        return {
            "symbol": sym,
            "price": price,
            "asof_et": ts_for_prices,
            "source": "polygon_snapshot_lastTrade",
            "market_state": "OPEN",
            "age_minutes": age_minutes,
            "ok": ok,
            "reason": reason,
        }

    scenario_last_prices: Dict[str, Dict[str, Any]] = {
        "SPY": _build_last_price("SPY", 678.98),
        "QQQ": _build_last_price("QQQ", 614.89),
        "IWM": _build_last_price("IWM", 251.27),
    }

    if not hasattr(bot, "_set_last_price_provider"):
        raise RuntimeError("discord_bot missing _set_last_price_provider hook")

    def _fixture_last_price(symbol: Optional[str]) -> Any:
        key = (symbol or "").upper()
        payload = scenario_last_prices.get(key)
        if payload is None:
            return bot.LastPrice(
                symbol=key,
                price=None,
                asof_et=None,
                source="none",
                market_state="UNKNOWN",
                age_minutes=None,
                ok=False,
                reason="no_fixture",
            )
        return bot.LastPrice(**payload)

    bot._set_last_price_provider(_fixture_last_price)

    regime_fixture = _build_regime_fixture(now_et)
    levels_fixture = _build_levels_fixture()
    options_fixture = _build_options_focus_fixture()

    def _regime_stub(*, post_type: str = "default") -> List[str]:
        key = post_type or "default"
        return regime_fixture.get(key, regime_fixture["default"])

    def _levels_stub(*, post_type: str = "default") -> List[Dict[str, str]]:
        key = post_type or "default"
        return levels_fixture.get(key, levels_fixture["default"])

    def _options_stub(*, post_type: str = "default") -> List[Dict[str, str]]:
        key = post_type or "default"
        return options_fixture.get(key, options_fixture["default"])

    bot.get_regime_snapshot = _regime_stub  # type: ignore[assignment]
    bot.get_levels_focus = _levels_stub  # type: ignore[assignment]
    bot.get_options_focus = _options_stub  # type: ignore[assignment]
    bot.data_is_fresh = lambda *args, **kwargs: True  # type: ignore[assignment]

    smiq_fixture = _smart_market_iq_fixture(now_et, scenario)

    def _smiq_stub() -> Dict[str, Any]:
        return copy.deepcopy(smiq_fixture)

    bot._load_smart_market_iq = _smiq_stub  # type: ignore[assignment]

    return {"bot": bot}


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Render autopost payloads for smoke coverage.")
    parser.add_argument("--compare", action="store_true", help="Compare outputs against stored goldens.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIO_ALL,
        help="Scenario name to render (can be provided multiple times).",
    )
    parser.add_argument(
        "--all-scenarios",
        action="store_true",
        help="Render all predefined scenarios.",
    )
    parsed = parser.parse_args(list(argv) if argv is not None else None)

    fixture_data, now_et = _load_futures_fixture()

    scenarios: List[str]
    if parsed.all_scenarios:
        scenarios = list(SCENARIO_ALL)
    elif parsed.scenario:
        scenarios = list(dict.fromkeys(parsed.scenario))  # preserve order, dedupe
    else:
        scenarios = list(DEFAULT_SCENARIOS)

    mismatches: List[str] = []

    for scenario in scenarios:
        print(f"\n--- Scenario: {scenario} ---")
        futures_path = _prepare_futures_path(fixture_data, scenario, now_et)
        modules = _prepare_modules(fixture_data, now_et, futures_path, scenario)
        bot = modules["bot"]

        builder_specs: List[Tuple[str, str, Iterable[Any], Dict[str, Any]]] = []
        if hasattr(bot, "format_daily_summary"):
            builder_specs.append(("daily_summary", "format_daily_summary", (["SPY", "QQQ"],), {}))
        else:
            print("[SKIP] format_daily_summary not available")
        if hasattr(bot, "build_daily_prep_payload"):
            builder_specs.append(("daily_prep", "build_daily_prep_payload", (["SPY", "QQQ"],), {}))
        else:
            print("[SKIP] build_daily_prep_payload not available")

        optional_targets = [
            ("after_hours", "build_after_hours_payload", (["SPY", "QQQ"],), {}),
            ("pre_market", "build_pre_market_payload", (["SPY", "QQQ"],), {}),
            ("focus_list", "build_focus_list_payload", (["SPY", "QQQ", "IWM"],), {}),
            ("intraday_update", "build_intraday_update_payload", tuple(), {}),
        ]

        builder_specs.extend(optional_targets)

        output_dir = OUTPUT_ROOT / scenario
        golden_dir = GOLDEN_ROOT / scenario

        for label, attr, call_args, kwargs in builder_specs:
            func = _resolve_builder(bot, attr)
            if not func:
                print(f"[SKIP] {attr} not available")
                continue
            result = _render_and_capture(output_dir, label, func, *call_args, **kwargs)
            if result is None:
                continue
            text, context = result
            if scenario.startswith("participation_"):
                _assert_participation_expectations(scenario, label, text, context)
            _enforce_contract(text, label, scenario, context)
            _handle_golden(
                golden_dir,
                label,
                text,
                compare=parsed.compare,
                mismatches=mismatches,
                scenario=scenario,
            )

    if parsed.compare and mismatches:
        print(f"[FAIL] Golden mismatches detected: {', '.join(mismatches)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
