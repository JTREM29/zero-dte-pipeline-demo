from __future__ import annotations

from typing import Any, Mapping, Optional


def _coerce_float(val: object) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:
        return None


def _coerce_str(val: object) -> str:
    if val is None:
        return ""
    try:
        return str(val)
    except Exception:
        return ""


def _fmt_num(val: Optional[float], nd: int = 2) -> str:
    if val is None:
        return "n/a"
    try:
        return f"{float(val):.{nd}f}"
    except Exception:
        return "n/a"


def _fmt_signed(val: Optional[float], nd: int = 2) -> str:
    if val is None:
        return "n/a"
    try:
        return f"{float(val):+.{nd}f}"
    except Exception:
        return "n/a"


def format_numeric_bias_pack_from_signal_payload(
    *,
    symbol: str,
    signal_payload: Mapping[str, Any],
    price_context: Optional[Mapping[str, Any]] = None,
    data_quality: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return a deterministic, short "Numeric Bias Pack" block.

    Notes:
    - Uses existing payload fields (bias/edge/conviction/regime/TNT confidence/data quality).
    - Avoids exposing raw internal indicator values unless already present in payload.
    """

    sym = (symbol or "").strip().upper() or "N/A"

    bias = _coerce_str(signal_payload.get("bias") or "NEUTRAL").upper() or "NEUTRAL"
    conviction = _coerce_str(signal_payload.get("conviction") or "n/a").upper() or "n/a"
    confirm = _coerce_str(signal_payload.get("bias_confirm") or "UNKNOWN").upper() or "UNKNOWN"

    edge = _coerce_float(signal_payload.get("edge"))

    signed_edge: Optional[float]
    if bias == "BULL":
        signed_edge = edge
    elif bias == "BEAR":
        signed_edge = -edge if edge is not None else None
    else:
        signed_edge = 0.0 if edge is not None else 0.0

    regime = _coerce_str(signal_payload.get("regime") or "UNKNOWN").upper() or "UNKNOWN"
    session = _coerce_str(signal_payload.get("session") or "").upper()

    tnt_regime = "UNKNOWN"
    tnt_posture = "UNKNOWN"
    tnt_conf = None
    tnt_ctx = signal_payload.get("tnt") if isinstance(signal_payload.get("tnt"), Mapping) else None
    if isinstance(tnt_ctx, Mapping):
        tnt_regime = _coerce_str(tnt_ctx.get("regime") or "UNKNOWN").upper() or "UNKNOWN"
        tnt_posture = _coerce_str(tnt_ctx.get("posture") or "UNKNOWN").upper() or "UNKNOWN"
        tnt_conf = _coerce_float(tnt_ctx.get("confidence"))

    dq_state = "n/a"
    dq = data_quality if isinstance(data_quality, Mapping) else None
    if dq is None and isinstance(signal_payload.get("data_quality"), Mapping):
        dq = signal_payload.get("data_quality")
    if isinstance(dq, Mapping):
        dq_state = _coerce_str(dq.get("technical_state") or dq.get("technical") or "n/a").upper() or "n/a"

    price_mode = ""
    price_age = None
    pc = price_context if isinstance(price_context, Mapping) else None
    if pc is None:
        pc = signal_payload.get("price_context") if isinstance(signal_payload.get("price_context"), Mapping) else None
    if pc is None:
        price_mode = _coerce_str(signal_payload.get("price_mode") or "")
        price_age = _coerce_float(signal_payload.get("price_age_minutes"))
    else:
        price_mode = _coerce_str(pc.get("mode") or "")
        price_age = _coerce_float(pc.get("age_minutes"))

    header = f"🧮 **Numeric Bias Pack** — **{sym}**"
    line1 = (
        "• "
        f"Bias **{bias}** | "
        f"Net **{_fmt_signed(signed_edge, 3)}** | "
        f"Conv **{conviction}** | "
        f"Confirm **{confirm}**"
    )

    bits: list[str] = []
    bits.append(f"Regime **{regime}**")
    bits.append(f"TNT **{tnt_regime}/{tnt_posture}**")
    if tnt_conf is not None:
        bits.append(f"Conf **{_fmt_num(tnt_conf, 2)}**")
    bits.append(f"Data **{dq_state}**")
    if price_mode:
        bits.append(f"Px {price_mode}")
    if isinstance(price_age, (int, float)):
        bits.append(f"age {_fmt_num(price_age, 1)}m")
    if session:
        bits.append(session)

    line2 = "• " + " | ".join(bits)

    return "\n".join([header, line1, line2])


def format_numeric_bias_pack_from_analysis_payload(*, analysis_payload: Mapping[str, Any]) -> Optional[str]:
    """Extract the best-available signal/quality context from an on-demand analyze payload."""

    if not isinstance(analysis_payload, Mapping):
        return None

    symbol = _coerce_str(analysis_payload.get("symbol") or "")
    meta = analysis_payload.get("meta") if isinstance(analysis_payload.get("meta"), Mapping) else {}

    signal_payload = meta.get("signal_payload") if isinstance(meta.get("signal_payload"), Mapping) else None
    if signal_payload is None:
        return None

    price_context = meta.get("price_context") if isinstance(meta.get("price_context"), Mapping) else None
    data_quality = None
    dq = meta.get("data_quality")
    if isinstance(dq, Mapping):
        data_quality = dq

    return format_numeric_bias_pack_from_signal_payload(
        symbol=symbol or "N/A",
        signal_payload=signal_payload,
        price_context=price_context,
        data_quality=data_quality,
    )
