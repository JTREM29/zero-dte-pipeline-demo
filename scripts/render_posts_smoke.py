import argparse
import copy
import difflib
import json
import os
import sys
import unicodedata
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
SYS_PATH_INSERTED = False
OUTPUT_ROOT = ROOT / "tmp" / "rendered"
GOLDEN_ROOT = ROOT / "tests" / "goldens"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FUTURES_FIXTURE_PATH = FIXTURE_DIR / "futures_context_fixture.json"

SCENARIO_ALL = ("baseline", "futures_missing", "futures_stale")
DEFAULT_SCENARIOS = ("baseline",)
MAX_LINES = int(os.getenv("SMOKE_MAX_LINES", "60"))
MAX_EMOJI = int(os.getenv("SMOKE_MAX_EMOJI", "24"))
FUTURES_STALE_DELTA = timedelta(hours=24)

PIVOT_FIXTURE: Dict[str, Dict[str, float]] = {
    "SPY": {"P": 4832.25, "R1": 4846.25, "S1": 4818.25, "R2": 4860.25, "S2": 4804.25},
    "QQQ": {"P": 415.10, "R1": 417.25, "S1": 412.95, "R2": 419.40, "S2": 410.80},
    "ES": {"P": 4834.75, "R1": 4850.50, "S1": 4819.00, "R2": 4866.25, "S2": 4803.25},
}


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
        "intraday_update": ["Monitoring for VWAP hold to confirm bias."],
    }


def _build_levels_fixture() -> Dict[str, List[Dict[str, str]]]:
    common_levels = [
        {"label": "SPY 4820", "note": "ON low cluster."},
        {"label": "QQQ 415", "note": "Weekly value area high."},
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
        {"label": "SPX 4800c", "note": "OI build; watch gamma flip."},
        {"label": "QQQ 420p", "note": "Hedging flow picking up."},
    ]
    return {
        "default": common,
        "after_hours": common,
        "pre_market": common,
        "focus_list": common,
        "intraday_update": common,
    }


def _render_and_capture(directory: Path, name: str, builder: Callable[..., Any], *args: Any, **kwargs: Any) -> Optional[str]:
    try:
        output = builder(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {name}: {exc}")
        return None

    if output is None:
        print(f"[WARN] {name}: returned None")
        return None

    if isinstance(output, (list, tuple)):
        text = "\n".join(str(item) for item in output)
    else:
        text = str(output)

    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / f"{name}.txt"
    out_path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")

    print("\n===", name, "===")
    print(text)
    print()
    return text


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


def _count_emoji(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch).startswith("So"))


def _enforce_contract(text: str, label: str, scenario: str) -> None:
    lines = text.rstrip("\n").splitlines()
    if len(lines) > MAX_LINES:
        raise AssertionError(f"{scenario}:{label} exceeds max lines ({len(lines)} > {MAX_LINES})")

    emoji_count = _count_emoji(text)
    if emoji_count > MAX_EMOJI:
        raise AssertionError(f"{scenario}:{label} exceeds emoji budget ({emoji_count} > {MAX_EMOJI})")

    if text.count("📊 Futures Context —") > 1:
        raise AssertionError(f"{scenario}:{label} contains duplicate Futures sections")


def _prepare_modules(fixture_data: dict, now_et: datetime, futures_path: Path) -> Dict[str, Any]:
    _ensure_sys_path()

    os.environ["FUTURES_CONTEXT_PATH"] = str(futures_path)

    bot = import_module("delivery.discord_bot")

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
        modules = _prepare_modules(fixture_data, now_et, futures_path)
        bot = modules["bot"]

        builders: List[Tuple[str, Callable[..., Any], Iterable[Any], Dict[str, Any]]] = []
        if hasattr(bot, "format_daily_summary"):
            builders.append(("daily_summary", bot.format_daily_summary, (["SPY", "QQQ"],), {}))
        else:
            print("[SKIP] format_daily_summary not available")
        if hasattr(bot, "build_daily_prep_payload"):
            builders.append(("daily_prep", bot.build_daily_prep_payload, (["SPY", "QQQ"],), {}))
        else:
            print("[SKIP] build_daily_prep_payload not available")

        optional_targets = [
            ("after_hours", "build_after_hours_payload", ("SPY", "QQQ"), {}),
            ("pre_market", "build_pre_market_payload", ("SPY", "QQQ"), {}),
            ("focus_list", "build_focus_list_payload", ("SPY", "QQQ", "IWM"), {}),
            ("intraday_update", "build_intraday_update_payload", tuple(), {}),
        ]

        for label, attr, args, kwargs in optional_targets:
            func = getattr(bot, attr, None)
            if callable(func):
                arg_obj = (list(args),) if args else tuple()
                builders.append((label, func, arg_obj, kwargs))
            else:
                print(f"[SKIP] {attr} not available")

        output_dir = OUTPUT_ROOT / scenario
        golden_dir = GOLDEN_ROOT / scenario

        for label, func, call_args, kwargs in builders:
            text = _render_and_capture(output_dir, label, func, *call_args, **kwargs)
            if text is None:
                continue
            _enforce_contract(text, label, scenario)
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
