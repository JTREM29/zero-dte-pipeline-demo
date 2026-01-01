from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence

CONTRACT_VERSION = "2025-12-17a"
AUTOPOST_CONTRACT_VERSION = CONTRACT_VERSION

INSIGHTS_TECHNICAL_TERMS: tuple[str, ...] = (
    "sma",
    "ema",
    "bull flag",
    "bear flag",
    "head and shoulders",
    "inverse head and shoulders",
    "trend strength",
    "atr",
)

DEFAULT_MAX_LINES = 60
DEFAULT_MAX_EMOJI = 24
DEFAULT_MAX_CHARS_DEFAULT = 1900
DEFAULT_MAX_CHARS_BY_PAYLOAD: Dict[str, int] = {
    "daily_summary": 1600,
    "daily_prep": 1900,
    "after_hours": 1900,
    "pre_market": 1700,
    "focus_list": 1500,
    "intraday_update": 900,
}

_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_DISCLAIMER_LINE = "_Not financial advice._"

_OPTIONAL_SECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("extra_notes", ("educational", "extra note", "extra notes")),
    ("secondary_setup", ("secondary setup", "secondary play", "secondary plan")),
    ("verbose_context", ("signal details",)),
    ("indicator_list", ("market confirmation",)),
)


def sanitize_render_text(text: str) -> str:
    """Collapse excessive blank lines and trim trailing whitespace."""

    if not text:
        return ""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _TRAILING_WS_RE.sub("", normalized)
    normalized = _MULTI_NEWLINE_RE.sub("\n\n", normalized)
    normalized = normalized.lstrip("\n").rstrip()
    return normalized


def _drop_section_by_keywords(lines: list[str], keywords: tuple[str, ...]) -> tuple[list[str], bool]:
    lowered = [line.lower() for line in lines]
    target_idx: Optional[int] = None
    for idx, line_lower in enumerate(lowered):
        if any(keyword in line_lower for keyword in keywords):
            target_idx = idx
            break

    if target_idx is None:
        return lines, False

    start = target_idx
    while start > 0 and not lines[start - 1].strip():
        start -= 1

    end = target_idx + 1
    total = len(lines)
    while end < total and lines[end].strip():
        end += 1
    while end < total and not lines[end].strip():
        end += 1

    new_lines = lines[:start] + lines[end:]
    return new_lines, True


def enforce_line_cap(text: str, *, max_lines: int = DEFAULT_MAX_LINES) -> str:
    """Ensure the render respects the max line cap with deterministic fallbacks."""

    if max_lines <= 0:
        max_lines = DEFAULT_MAX_LINES

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()

    disclaimer_lower = _DISCLAIMER_LINE.lower()
    has_disclaimer = any(line.strip().lower() == disclaimer_lower for line in lines)
    working_lines = [line for line in lines if line.strip().lower() != disclaimer_lower]

    budget = max_lines - 1  # reserve one line for the disclaimer
    if budget < 1:
        budget = max_lines

    for _name, keywords in _OPTIONAL_SECTION_KEYWORDS:
        if len(working_lines) <= budget:
            break
        working_lines, removed = _drop_section_by_keywords(working_lines, keywords)
        if not removed:
            continue

    truncated = False
    if len(working_lines) > budget:
        trimmed_budget = max(budget - 1, 0)
        working_lines = working_lines[:trimmed_budget]
        working_lines.append("…(trimmed to fit Discord limits)")
        truncated = True

    while working_lines and not working_lines[-1].strip():
        working_lines.pop()

    if not has_disclaimer or truncated or not working_lines or working_lines[-1].strip().lower() != disclaimer_lower:
        working_lines.append(_DISCLAIMER_LINE)

    result = "\n".join(working_lines)
    return result


@dataclass(frozen=True)
class ContractConstraints:
    max_lines: int = DEFAULT_MAX_LINES
    max_emoji: int = DEFAULT_MAX_EMOJI
    max_chars_default: int = DEFAULT_MAX_CHARS_DEFAULT
    max_chars_by_payload: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_MAX_CHARS_BY_PAYLOAD)
    )
    forbidden_technical_terms: Sequence[str] = INSIGHTS_TECHNICAL_TERMS


def _count_emoji(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch).startswith("So"))


def _find_key_recursive(obj: Any, target: str) -> Optional[Any]:
    if isinstance(obj, dict):
        if target in obj:
            return obj[target]
        for value in obj.values():
            found = _find_key_recursive(value, target)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key_recursive(item, target)
            if found is not None:
                return found
    return None


def _context_has_pattern(context: Any, name: str) -> bool:
    if context is None:
        return False
    root = _find_key_recursive(context, "pattern_candidates")
    if not isinstance(root, dict):
        return False
    for sym_data in root.values():
        if not isinstance(sym_data, dict):
            continue
        by_tf = sym_data.get("by_tf")
        if not isinstance(by_tf, dict):
            continue
        for candidates in by_tf.values():
            if not isinstance(candidates, list):
                continue
            for cand in candidates:
                if isinstance(cand, dict) and cand.get("name") == name:
                    return True
    return False


def _context_has_sma(context: Any, timeframe: str, length: str) -> bool:
    if context is None:
        return False
    root = _find_key_recursive(context, "technical_state")
    if not isinstance(root, dict):
        return False
    for sym_data in root.values():
        if not isinstance(sym_data, dict):
            continue
        timeframes = sym_data.get("timeframes")
        if not isinstance(timeframes, dict):
            continue
        tf_state = timeframes.get(timeframe)
        if not isinstance(tf_state, dict):
            continue
        sma_map = tf_state.get("sma")
        if isinstance(sma_map, dict) and length in sma_map:
            return True
    return False

def _context_has_indicator(context: Any, indicator: str) -> bool:
    if context is None:
        return False
    indicator_lower = indicator.lower()
    root = _find_key_recursive(context, "technical_state")
    if not isinstance(root, dict):
        return False
    for sym_data in root.values():
        if not isinstance(sym_data, dict):
            continue
        timeframes = sym_data.get("timeframes")
        if not isinstance(timeframes, dict):
            continue
        for tf_state in timeframes.values():
            if not isinstance(tf_state, dict):
                continue
            indicator_map = tf_state.get(indicator_lower)
            if isinstance(indicator_map, dict) and indicator_map:
                return True
    return False


def _text_has_directional_bias(text: str) -> bool:
    for line in text.splitlines():
        lower = line.lower()
        if "bias" not in lower:
            continue
        if any(token in lower for token in ("bull", "bear", "up", "down", "long", "short")):
            if not any(token in lower for token in ("neutral", "unknown", "flat", "balanced", "range")):
                return True
    return False


def _extract_option_strikes(text: str) -> set[str]:
    strikes: set[str] = set()
    full_pattern = re.compile(r"\b([A-Z]{2,5})\s*(\d{2,5}(?:\.\d{1,2})?)([cCpP])\b")
    for sym, strike, side in full_pattern.findall(text):
        strikes.add(f"{sym.upper()} {strike}{side.upper()}")
    return strikes


def _context_option_strikes(context: Any) -> set[str]:
    if context is None:
        return set()
    sections = _find_key_recursive(context, "sections")
    if not isinstance(sections, dict):
        return set()
    options_focus = sections.get("options_focus")
    if not isinstance(options_focus, (list, tuple)):
        return set()
    noted: set[str] = set()
    for item in options_focus:
        if isinstance(item, str):
            noted.update(_extract_option_strikes(item))
        elif isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str):
                    noted.update(_extract_option_strikes(value))
        elif isinstance(item, (list, tuple)):
            for value in item:
                if isinstance(value, str):
                    noted.update(_extract_option_strikes(value))
    return noted


def contract_violations(
    text: str,
    *,
    label: str,
    context: Optional[Any],
    constraints: ContractConstraints = ContractConstraints(),
) -> list[str]:
    raw_text = text or ""
    lines = raw_text.rstrip("\n").splitlines()
    violations: list[str] = []

    if len(lines) > constraints.max_lines:
        violations.append(
            f"exceeds max lines ({len(lines)} > {constraints.max_lines})"
        )

    emoji_count = _count_emoji(raw_text)
    if emoji_count > constraints.max_emoji:
        violations.append(
            f"exceeds emoji budget ({emoji_count} > {constraints.max_emoji})"
        )

    if raw_text.count("📊 Futures Context —") > 1:
        violations.append("contains duplicate Futures sections")

    max_chars = constraints.max_chars_by_payload.get(label, constraints.max_chars_default)
    if len(raw_text) > max_chars:
        violations.append(f"exceeds max chars ({len(raw_text)} > {max_chars})")

    if "\n\n\n" in raw_text:
        violations.append("contains 3+ consecutive blank lines")

    trailing = [idx + 1 for idx, line in enumerate(lines) if line.endswith(" ")]
    if trailing:
        violations.append(
            f"has trailing spaces on lines {trailing[:10]}"
        )

    if raw_text.count("**") % 2 != 0:
        violations.append("has unbalanced bold markers (**)")

    if raw_text.count("```") % 2 != 0:
        violations.append("has unbalanced code fences (```)")

    if raw_text and raw_text[0] == "\n":
        violations.append("begins with a blank line")

    if raw_text.lower().count("not financial advice") > 1:
        violations.append("repeats Not financial advice disclaimer")

    heading_counts: Dict[str, int] = {}
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        first = stripped[0]
        if 0x1F300 <= ord(first) <= 0x1FAFF:
            heading_counts[stripped] = heading_counts.get(stripped, 0) + 1

    duplicates = [heading for heading, count in heading_counts.items() if count > 1]
    if duplicates:
        violations.append(
            f"contains duplicate heading lines: {duplicates[:5]}"
        )

    lower = raw_text.lower()

    mention_ema = "ema" in lower
    ema_supported = _context_has_indicator(context, "ema")
    if mention_ema and not ema_supported:
        violations.append("references 'ema' without technical_state ema data")

    if "bull flag" in lower and not _context_has_pattern(context, "bull_flag"):
        violations.append("mentions bull flag without pattern_candidates entry")

    sma_tokens = ("sma 200", "200 sma", "20/50/200 sma", "20 50 200 sma")
    if any(token in lower for token in sma_tokens) and not _context_has_sma(context, "1D", "200"):
        violations.append("references SMA 200 without technical_state 1D SMA 200")

    if context is not None and _text_has_directional_bias(lower) and "invalidation" not in lower:
        violations.append("missing invalidation guard for directional bias")

    mentioned_strikes = _extract_option_strikes(raw_text)
    if mentioned_strikes:
        allowed_strikes = _context_option_strikes(context)
        missing = sorted(mentioned_strikes - allowed_strikes)
        if missing:
            violations.append(
                f"references option strikes not present in options_focus: {missing[:5]}"
            )

    meta = _find_key_recursive(context, "meta") if context is not None else None
    tech_quality: Optional[str] = None
    if isinstance(meta, dict):
        dq = meta.get("data_quality")
        if isinstance(dq, dict):
            value = dq.get("technical_state")
            if isinstance(value, str):
                tech_quality = value.upper()
        elif isinstance(dq, str):
            tech_quality = dq.upper()

    if tech_quality and tech_quality != "FRESH":
        lowered_terms = tuple(
            term.lower() for term in constraints.forbidden_technical_terms if term.lower() != "ema"
        )
        for term in lowered_terms:
            if term in lower:
                violations.append(
                    f"references '{term}' while technical_state quality is {tech_quality}"
                )
                break

    return violations


_OF_FORBIDDEN_TERMS = (
    "dte",
    "expiry",
    "expiration",
    "strike",
    "contracts",
)

_OF_EXECUTION_VERBS = (
    "buy",
    "sell",
    "enter",
    "load",
    "add",
    "trim",
)

_OF_OPTION_NEIGHBOR_RE = re.compile(r"(?i)(?:\b\d+\s*(?:call|put|strike|exp)\b|\b(?:call|put|strike|exp)\s*\d+)")


def validate_options_framework(text: str) -> list[str]:
    """Return contract violations specific to the Options Framework section."""

    if not text:
        return []

    violations: list[str] = []
    lowered = text.lower()

    if "$" in text:
        violations.append("contains '$' symbol")

    for term in _OF_FORBIDDEN_TERMS:
        if term in lowered:
            violations.append(f"contains forbidden term '{term}'")
            break

    if _OF_OPTION_NEIGHBOR_RE.search(text):
        violations.append("contains digits adjacent to option terms")

    for verb in _OF_EXECUTION_VERBS:
        pattern = rf"(?i)\b{verb}\b"
        if re.search(pattern, text):
            violations.append(f"contains execution verb '{verb}'")
            break

    return violations
