from __future__ import annotations


def pack_key(symbol: str, expiry_ymd: str) -> str:
    sym = str(symbol or "").strip().upper()
    exp = str(expiry_ymd or "").strip()
    return f"pack:triple:{sym}:{exp}"


def manifest_key(date_ymd: str) -> str:
    d = str(date_ymd or "").strip()
    return f"pack:triple:manifest:{d}"


def build_lock_key(symbol: str, expiry_ymd: str) -> str:
    sym = str(symbol or "").strip().upper()
    exp = str(expiry_ymd or "").strip()
    return f"lock:pack:triple:build:{sym}:{exp}"


def render_lock_key(symbol: str, expiry_ymd: str) -> str:
    sym = str(symbol or "").strip().upper()
    exp = str(expiry_ymd or "").strip()
    return f"lock:pack:triple:render:{sym}:{exp}"
