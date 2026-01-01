"""TNT Chart Contract (v1.0)

This module encodes the deterministic, template-only chart policy layer.
It does NOT render images; it only produces and validates a "chart spec".

A renderer/poster can later consume the spec and enforce the visual style.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Literal, Mapping, Optional, Sequence, Tuple


ChartContractVersion = Literal["1.0"]
ChartTemplate = Literal[
    "DECISION_ZONE",
    "NO_TRADE_COMPRESSION",
    "MOMENTUM_ACCEPTANCE",
    "EVENT_RISK_OVERLAY",
    "POST_EVENT_REENGAGEMENT",
]

TNT_CHART_CONTRACT_VERSION: ChartContractVersion = "1.0"

# Hard constraints enforced at the spec level (v1.0)
MAX_LINES: int = 5
MAX_ZONES: int = 3
MAX_LABELS: int = 8
MAX_GATES: int = 2

ALLOWED_TIMEFRAMES: tuple[str, ...] = ("1m", "3m", "5m", "15m", "30m", "60m", "1D")

FOOTER_DISCLAIMER: str = "TNT charts are posture & behavior only. No execution guidance."

# Locked label vocabulary (case-sensitive exact matches).
CHART_LABELS_ALLOWED: frozenset[str] = frozenset(
    {
        # Posture Gates
        "Bullish posture permitted only above acceptance",
        "Bearish posture permitted only below breakdown",
        "Neutral posture inside range",
        # Behavior Directives
        "Stand down (low expectancy)",
        "Momentum required",
        "Aggression prohibited",
        "Reduced participation",
        "Event risk: structure unreliable",
        "Wait for acceptance",
        "No-trade zone",
        # Structure Labels
        "Support zone",
        "Resistance zone",
        "Pivot",
        "Prior day high",
        "Prior day low",
        "Acceptance band",
        "Breakdown band",
        "Notes",
    }
)

CHART_LABELS_ALLOWED_BY_TEMPLATE: dict[str, frozenset[str]] = {
    "DECISION_ZONE": frozenset(
        {
            "Support zone",
            "Resistance zone",
            "Pivot",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        }
    ),
    "NO_TRADE_COMPRESSION": frozenset(
        {
            "No-trade zone",
            "Stand down (low expectancy)",
            "Momentum required",
        }
    ),
    "MOMENTUM_ACCEPTANCE": frozenset(
        {
            "Acceptance band",
            "Wait for acceptance",
            "Aggression prohibited",
            "Momentum required",
            "Support zone",
            "Resistance zone",
        }
    ),
    "EVENT_RISK_OVERLAY": frozenset(
        {
            "Event risk: structure unreliable",
            "Reduced participation",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        }
    ),
    "POST_EVENT_REENGAGEMENT": frozenset(
        {
            "Wait for acceptance",
            "Reduced participation",
            "Support zone",
            "Resistance zone",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        }
    ),
}

# Forbidden words/phrases (case-insensitive substring match).
FORBIDDEN_LABEL_SUBSTRINGS: tuple[str, ...] = (
    # Execution / trading language
    "buy",
    "sell",
    "long",
    "short",
    "entry",
    "enter",
    "exit",
    "target",
    "stop",
    "take profit",
    "tp",
    "sl",
    # Options contract language
    "call",
    "put",
    "strike",
    "expiry",
    "expiration",
    "0dte",
    "dte",
    # Performance / hype
    "p&l",
    "profit",
    "gain",
    "win",
    "loss",
    "guarantee",
    "moon",
    "bag",
    # Indicators
    "rsi",
    "macd",
    "ema",
    "sma",
    "vwap",
    "bollinger",
    "stochastic",
)


@dataclass(frozen=True)
class ChartBuildResult:
    spec: Optional[Dict[str, Any]]
    template: Optional[ChartTemplate]


def _coerce_str(val: object) -> Optional[str]:
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _coerce_float(val: object) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:  # noqa: BLE001
        return None


def _get_state_str(state: Mapping[str, Any], *path: str) -> Optional[str]:
    cur: Any = state
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return _coerce_str(cur)


def _get_state_bool(state: Mapping[str, Any], *path: str) -> Optional[bool]:
    cur: Any = state
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    if isinstance(cur, bool):
        return cur
    return None


def _iter_event_like(events: object) -> Iterable[Mapping[str, Any]]:
    if isinstance(events, Mapping):
        yield events
    elif isinstance(events, Sequence) and not isinstance(events, (str, bytes)):
        for item in events:
            if isinstance(item, Mapping):
                yield item


def _has_high_event_in_phase(
    state: Mapping[str, Any],
    *,
    phases: set[str],
) -> bool:
    events = state.get("events")
    for ev in _iter_event_like(events):
        risk = (_coerce_str(ev.get("risk")) or "").upper()
        phase = (_coerce_str(ev.get("phase")) or "").upper()
        if risk == "HIGH" and phase in phases:
            return True
    return False


def select_template(state: Mapping[str, Any]) -> Optional[ChartTemplate]:
    """Select a template deterministically from TNT_STATE.

    This is intentionally conservative: if health is not OK or permissions
    prohibit action, it chooses the most restrictive template.
    """

    data_health = (_get_state_str(state, "meta", "data_health") or "").upper()
    if data_health and data_health != "OK":
        return None

    if _has_high_event_in_phase(state, phases={"PRE", "WINDOW"}):
        return "EVENT_RISK_OVERLAY"
    if _has_high_event_in_phase(state, phases={"POST"}):
        return "POST_EVENT_REENGAGEMENT"

    if _get_state_bool(state, "permissions", "no_trade") is True:
        return "NO_TRADE_COMPRESSION"

    regime = (_get_state_str(state, "posture", "regime") or "").upper()
    if regime == "MOMENTUM":
        return "MOMENTUM_ACCEPTANCE"

    return "DECISION_ZONE"


def _pick_primary_event(state: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    events = state.get("events")
    for ev in _iter_event_like(events):
        risk = (_coerce_str(ev.get("risk")) or "").upper()
        if risk != "HIGH":
            continue
        name = _coerce_str(ev.get("name")) or "EVENT"
        win = ev.get("window_et") if isinstance(ev.get("window_et"), Mapping) else {}
        start = _coerce_str(win.get("start"))
        end = _coerce_str(win.get("end"))
        return {
            "name": name,
            "window_start_et": start,
            "window_end_et": end,
        }
    return None


def _zone(label: str, low: Optional[float], high: Optional[float]) -> Optional[dict[str, Any]]:
    lo = _coerce_float(low)
    hi = _coerce_float(high)
    if lo is None or hi is None:
        return None
    if lo >= hi:
        return None
    return {"label": label, "low": lo, "high": hi}


def _line(label: str, price: Optional[float]) -> Optional[dict[str, Any]]:
    px = _coerce_float(price)
    if px is None:
        return None
    return {"label": label, "price": px}


def build_chart_spec_from_state(state: Mapping[str, Any]) -> ChartBuildResult:
    """Return a deterministic chart spec or None.

    The spec is kept renderer-agnostic and small.
    """

    template = select_template(state)
    if template is None:
        return ChartBuildResult(spec=None, template=None)

    symbol = _get_state_str(state, "context", "symbol") or "SPY"
    tf = _get_state_str(state, "context", "timeframes", "execution") or "5m"
    timestamp_et = _get_state_str(state, "meta", "generated_at_et")

    bias = (_get_state_str(state, "posture", "bias") or "NEUTRAL").upper()
    regime = (_get_state_str(state, "posture", "regime") or "COMPRESSION").upper()
    conviction = (_get_state_str(state, "posture", "conviction") or "LOW").upper()
    aggression = (_get_state_str(state, "permissions", "aggression") or "REDUCED").upper()

    piv = _coerce_float(
        ((state.get("levels") or {}).get("pivots_rth") or {}).get("P") if isinstance(state.get("levels"), Mapping) else None
    )
    r1 = _coerce_float(
        ((state.get("levels") or {}).get("pivots_rth") or {}).get("R1") if isinstance(state.get("levels"), Mapping) else None
    )
    s1 = _coerce_float(
        ((state.get("levels") or {}).get("pivots_rth") or {}).get("S1") if isinstance(state.get("levels"), Mapping) else None
    )

    gates: dict[str, Any] = {}
    if r1 is not None:
        gates["acceptance_up"] = {"price": float(r1)}
    if s1 is not None:
        gates["breakdown_down"] = {"price": float(s1)}

    base_lines = [x for x in (_line("Pivot", piv),) if x is not None]
    support_zone = _zone("Support zone", s1, piv)
    resistance_zone = _zone("Resistance zone", piv, r1)
    base_zones = [z for z in (support_zone, resistance_zone) if z is not None]

    # Under strict label mode (default), only include annotations whose labels are
    # permitted for the selected template.
    allowed_for_template = CHART_LABELS_ALLOWED_BY_TEMPLATE.get(template, CHART_LABELS_ALLOWED)
    base_lines_for_template = [ln for ln in base_lines if ln.get("label") in allowed_for_template]
    base_zones_for_template = [zn for zn in base_zones if zn.get("label") in allowed_for_template]

    template_params: Dict[str, Any] = {
        "lines": base_lines_for_template,
        "zones": base_zones_for_template,
        "gates": gates,
        "labels": [],
    }

    # Template-specific params + labels
    if template == "DECISION_ZONE":
        template_params["labels"] = [
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        ]

    elif template == "NO_TRADE_COMPRESSION":
        # Use S1..R1 as the explicit no-trade box when present.
        no_trade = _zone("No-trade zone", s1, r1)
        if no_trade is not None:
            template_params["no_trade_box"] = {"low": no_trade["low"], "high": no_trade["high"]}
        template_params["breakout_triggers"] = base_lines[:2]
        template_params["labels"] = [
            "No-trade zone",
            "Stand down (low expectancy)",
            "Momentum required",
        ]

    elif template == "MOMENTUM_ACCEPTANCE":
        # Use Pivot..R1 (or S1..Pivot) as an acceptance band when possible.
        acc = None
        if piv is not None and r1 is not None:
            acc = _zone("Acceptance band", min(piv, r1), max(piv, r1))
        if acc is not None:
            template_params["acceptance_band"] = {"low": acc["low"], "high": acc["high"]}
        template_params["labels"] = [
            "Acceptance band",
            "Wait for acceptance",
            "Momentum required",
        ]
        if aggression == "PROHIBITED":
            template_params["labels"].append("Aggression prohibited")

    elif template == "EVENT_RISK_OVERLAY":
        ev = _pick_primary_event(state)
        if ev is not None:
            template_params["event"] = ev
        template_params["behavior_banner"] = "Reduced participation"
        template_params["labels"] = [
            "Event risk: structure unreliable",
            "Reduced participation",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        ]

    elif template == "POST_EVENT_REENGAGEMENT":
        ev = _pick_primary_event(state)
        if ev is not None:
            template_params["event_marker"] = ev
        template_params["post_event_structure"] = base_zones[:2]
        template_params["labels"] = [
            "Wait for acceptance",
            "Reduced participation",
            "Bullish posture permitted only above acceptance",
            "Bearish posture permitted only below breakdown",
        ]

    spec: Dict[str, Any] = {
        "contract_version": TNT_CHART_CONTRACT_VERSION,
        "template": template,
        "symbol": symbol,
        "timeframe": tf,
        "timestamp_et": timestamp_et,
        # Back-compat alias for earlier field name.
        "asof_et": timestamp_et,
        "posture": {"bias": bias, "regime": regime, "conviction": conviction},
        "permissions": {"aggression": aggression},
        "footer": FOOTER_DISCLAIMER,
        "template_params": template_params,
    }

    ok, _msg = validate_chart_spec(spec)
    if not ok:
        return ChartBuildResult(spec=None, template=None)

    return ChartBuildResult(spec=spec, template=template)


def _collect_labels(node: object) -> Iterable[str]:
    if isinstance(node, Mapping):
        for k, v in node.items():
            if k in {"label", "name", "text"}:
                s = _coerce_str(v)
                if s:
                    yield s
            yield from _collect_labels(v)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for item in node:
            yield from _collect_labels(item)


def validate_chart_spec(spec: Mapping[str, Any]) -> Tuple[bool, str]:
    """Validate chart spec against TNT Chart Contract v1.0.

    Notes:
    - Labels are case-sensitive and must match the locked vocabulary exactly.
    - Forbidden phrases are checked via case-insensitive substring match.
    """

    if not isinstance(spec, Mapping):
        return False, "spec not mapping"

    if str(spec.get("contract_version")) != TNT_CHART_CONTRACT_VERSION:
        return False, "contract_version mismatch"

    template = _coerce_str(spec.get("template"))
    allowed_templates = {
        "DECISION_ZONE",
        "NO_TRADE_COMPRESSION",
        "MOMENTUM_ACCEPTANCE",
        "EVENT_RISK_OVERLAY",
        "POST_EVENT_REENGAGEMENT",
    }
    if template not in allowed_templates:
        return False, "invalid template"

    # Required header/footer fields.
    symbol = _coerce_str(spec.get("symbol"))
    if not symbol:
        return False, "missing symbol"

    timeframe = _coerce_str(spec.get("timeframe"))
    if not timeframe:
        return False, "missing timeframe"
    if "," in timeframe or "/" in timeframe:
        return False, "multi-timeframe overlays forbidden"
    if timeframe not in ALLOWED_TIMEFRAMES:
        return False, "invalid timeframe"

    timestamp_et = _coerce_str(spec.get("timestamp_et")) or _coerce_str(spec.get("asof_et"))
    if not timestamp_et:
        return False, "missing timestamp_et"

    posture = spec.get("posture")
    if not isinstance(posture, Mapping):
        return False, "missing posture"
    bias = _coerce_str(posture.get("bias"))
    regime = _coerce_str(posture.get("regime"))
    conviction = _coerce_str(posture.get("conviction"))
    if not (bias and regime and conviction):
        return False, "missing posture summary"

    footer = _coerce_str(spec.get("footer"))
    if footer != FOOTER_DISCLAIMER:
        return False, "missing/invalid footer disclaimer"

    params = spec.get("template_params")
    if not isinstance(params, Mapping):
        return False, "missing template_params"

    strict_labels = spec.get("strict_labels", True)
    if not isinstance(strict_labels, bool):
        strict_labels = True

    # Collect all annotation labels (header/footer excluded).
    label_set: set[str] = set()
    labels_node = params.get("labels")
    if isinstance(labels_node, Sequence) and not isinstance(labels_node, (str, bytes)):
        for item in labels_node:
            if isinstance(item, str):
                label_set.add(item)
            elif isinstance(item, Mapping):
                val = _coerce_str(item.get("label"))
                if val:
                    label_set.add(val)

    lines = params.get("lines")
    if lines is not None:
        if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)):
            return False, "invalid lines"
        if len(lines) > MAX_LINES:
            return False, "too many lines"
        for line in lines:
            if not isinstance(line, Mapping):
                return False, "invalid line item"
            lbl = _coerce_str(line.get("label"))
            if lbl:
                label_set.add(lbl)
            px = line.get("price")
            if px is not None and _coerce_float(px) is None:
                return False, "invalid line price"

    zones = params.get("zones")
    if zones is not None:
        if not isinstance(zones, Sequence) or isinstance(zones, (str, bytes)):
            return False, "invalid zones"
        if len(zones) > MAX_ZONES:
            return False, "too many zones"
        for zone in zones:
            if not isinstance(zone, Mapping):
                return False, "invalid zone item"
            lbl = _coerce_str(zone.get("label"))
            if lbl:
                label_set.add(lbl)
            lo = _coerce_float(zone.get("low"))
            hi = _coerce_float(zone.get("high"))
            if lo is None or hi is None:
                return False, "invalid zone bounds"
            if lo >= hi:
                return False, "zone low>=high"

    banner_val = _coerce_str(params.get("behavior_banner"))
    if banner_val:
        label_set.add(banner_val)

    # Additional zone-like required objects per template.
    def _check_zone_obj(obj: object, *, key: str, label: str) -> Optional[str]:
        if not isinstance(obj, Mapping):
            return f"missing {key}"
        lo = _coerce_float(obj.get("low"))
        hi = _coerce_float(obj.get("high"))
        if lo is None or hi is None:
            return f"invalid {key} bounds"
        if lo >= hi:
            return f"invalid {key} (low>=high)"
        label_set.add(label)
        return None

    # Gates
    gates = params.get("gates")
    if gates is not None and not isinstance(gates, Mapping):
        return False, "invalid gates"
    gate_count = 0
    if isinstance(gates, Mapping):
        for gate_key, gate_obj in gates.items():
            if gate_key not in {"acceptance_up", "breakdown_down"}:
                return False, "invalid gate key"
            if not isinstance(gate_obj, Mapping):
                return False, "invalid gate"
            if _coerce_float(gate_obj.get("price")) is None:
                return False, "invalid gate price"
            gate_count += 1
    if gate_count > MAX_GATES:
        return False, "too many gates"

    # Forbidden substrings + locked vocabulary checks.
    def _label_is_forbidden(label: str) -> Optional[str]:
        lower = label.lower()
        for bad in FORBIDDEN_LABEL_SUBSTRINGS:
            if bad in lower:
                return bad
        return None

    allowed_labels = CHART_LABELS_ALLOWED_BY_TEMPLATE.get(template, CHART_LABELS_ALLOWED) if strict_labels else CHART_LABELS_ALLOWED
    for lbl in label_set:
        if lbl not in allowed_labels:
            return False, "label not in locked vocabulary"
        bad = _label_is_forbidden(lbl)
        if bad:
            return False, f"forbidden phrase in label: {bad}"

    if len(label_set) > MAX_LABELS:
        return False, "too many labels"

    # Template schema gates
    if template == "DECISION_ZONE":
        if timeframe is None:
            return False, "missing timeframe"
        if not isinstance(zones, Sequence) or isinstance(zones, (str, bytes)):
            return False, "missing zones"
        if len(zones) < 1:
            return False, "missing zones"
        if not isinstance(gates, Mapping):
            return False, "missing gates"
        if "acceptance_up" not in gates or "breakdown_down" not in gates:
            return False, "missing required gates"

    if template == "NO_TRADE_COMPRESSION":
        err = _check_zone_obj(params.get("no_trade_box"), key="no_trade_box", label="No-trade zone")
        if err:
            return False, err
        triggers = params.get("breakout_triggers")
        if not isinstance(triggers, Sequence) or isinstance(triggers, (str, bytes)):
            return False, "missing breakout_triggers"
        if not (1 <= len(triggers) <= 2):
            return False, "breakout_triggers out of bounds"

    if template == "MOMENTUM_ACCEPTANCE":
        err = _check_zone_obj(params.get("acceptance_band"), key="acceptance_band", label="Acceptance band")
        if err:
            return False, err
        if "Aggression prohibited" not in label_set and "Wait for acceptance" not in label_set:
            return False, "missing required prohibition label"

    if template == "EVENT_RISK_OVERLAY":
        ev = params.get("event")
        if not isinstance(ev, Mapping):
            return False, "missing event"
        if not _coerce_str(ev.get("name")):
            return False, "missing event.name"
        # Allow banner mode if window timestamps are absent, but prefer both.
        ws = _coerce_str(ev.get("window_start_et"))
        we = _coerce_str(ev.get("window_end_et"))
        if (ws is None) != (we is None):
            return False, "event window partial"

        banner = _coerce_str(params.get("behavior_banner"))
        if banner not in {"Reduced participation", "Stand down (low expectancy)"}:
            return False, "invalid behavior_banner"
        label_set.add(banner)

    if template == "POST_EVENT_REENGAGEMENT":
        evm = params.get("event_marker")
        if not isinstance(evm, Mapping):
            return False, "missing event_marker"
        ws = _coerce_str(evm.get("window_start_et"))
        we = _coerce_str(evm.get("window_end_et"))
        if ws is None or we is None:
            return False, "missing event_marker window"
        struct = params.get("post_event_structure")
        if not isinstance(struct, Sequence) or isinstance(struct, (str, bytes)):
            return False, "missing post_event_structure"
        if not (1 <= len(struct) <= 2):
            return False, "post_event_structure out of bounds"
        if not isinstance(gates, Mapping):
            return False, "missing gates"
        if "acceptance_up" not in gates or "breakdown_down" not in gates:
            return False, "missing required gates"

    # Contradiction checks (limited but deterministic)
    # - If bias bullish, acceptance gate must exist.
    # - If bias bearish, breakdown gate must exist.
    b = (bias or "").upper()
    if isinstance(gates, Mapping):
        if b == "BULLISH" and "acceptance_up" not in gates:
            return False, "bullish bias missing acceptance gate"
        if b == "BEARISH" and "breakdown_down" not in gates:
            return False, "bearish bias missing breakdown gate"

    # - If aggression prohibited and template is MOMENTUM_ACCEPTANCE, must include label.
    perms = spec.get("permissions")
    aggression_val = None
    if isinstance(perms, Mapping):
        aggression_val = _coerce_str(perms.get("aggression"))
    if aggression_val and aggression_val.upper() == "PROHIBITED" and template == "MOMENTUM_ACCEPTANCE":
        if "Aggression prohibited" not in label_set:
            return False, "missing Aggression prohibited label"

    # Also enforce forbidden phrases anywhere in label-ish fields (defense in depth).
    for raw in _collect_labels(spec):
        bad = None
        if isinstance(raw, str):
            bad = next((x for x in FORBIDDEN_LABEL_SUBSTRINGS if x in raw.lower()), None)
        if bad:
            return False, f"forbidden phrase in label: {bad}"

    return True, "OK"
