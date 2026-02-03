from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Quality gate: OI/IV renderer invariants")
    ap.add_argument("--out", default="artifacts/oi_iv_quality_gate.png", help="Output PNG path")
    args = ap.parse_args()

    # Deterministic payload, intentionally includes negatives + NaNs to ensure clamping works.
    x_labels = [str(680 + i) for i in range(25)]
    oi_calls = [0, 10, -5, 50, 75, 0, 12, 8, 0, 200, 0, 1, 2, 3, 4, 5, 0, 900, 0, 7, 8, 9, 10, 0, 11]
    oi_puts = [5, 0, 20, 0, -1, 55, 0, 0, 19, 0, 0, 3, 0, 4, 0, 5, 0, 0, 800, 0, 7, 0, 9, 0, 0]
    iv_pct = [18.0 + (i % 5) * 0.2 for i in range(25)]

    try:
        from delivery.oi_iv_render import render_oi_iv_png
    except Exception as exc:
        print(f"error: import render_oi_iv_png failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    png = render_oi_iv_png(
        title="QUALITY-GATE — OI/IV deterministic sample",
        x_labels=x_labels,
        iv_pct=iv_pct,
        oi_calls=oi_calls,
        oi_puts=oi_puts,
        dpi=150,
        include_iv_overlay=True,
    )
    if not isinstance(png, (bytes, bytearray)) or not png:
        print("error: renderer returned empty png", file=sys.stderr)
        return 3

    # Validate metadata flags (stability rails).
    try:
        from scripts.png_text_probe import read_png_text_metadata

        meta = read_png_text_metadata(bytes(png))
    except Exception as exc:
        print(f"error: failed to read png metadata: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    def req(key: str, expected: str) -> bool:
        got = (meta.get(key) or "").strip()
        if got != expected:
            print(f"FAIL meta {key} expected={expected!r} got={got!r}")
            return False
        return True

    ok = True
    ok &= req("tnt_oi_iv_layout", "v2")
    ok &= req("tnt_oi_iv_bar_calls", "2")
    ok &= req("tnt_oi_iv_ylim_bottom", "0")
    ok &= req("tnt_oi_iv_palette", "option_b")
    ok &= req("tnt_oi_iv_watermark", "1")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(png))

    if ok:
        print(f"PASS wrote={out.as_posix()}")
        return 0

    print(f"WROTE (but FAIL) wrote={out.as_posix()}")
    return 10


if __name__ == "__main__":
    raise SystemExit(main())
