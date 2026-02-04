from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_figsize(raw: str) -> tuple[float, float] | None:
    s = (raw or "").strip()
    if not s:
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) != 2:
        return None
    try:
        w_in = float(parts[0])
        h_in = float(parts[1])
    except Exception:
        return None
    if w_in <= 0.0 or h_in <= 0.0:
        return None
    return (w_in, h_in)


def main() -> int:
    ap = argparse.ArgumentParser(description="One-shot local OI/IV render (premium v2) to dial in tick/layout.")
    ap.add_argument("--out", default="", help="Output PNG path (default: artifacts/oi_iv_local_<ts>.png)")
    ap.add_argument("--dpi", type=int, default=150, help="Render DPI")
    ap.add_argument("--figsize", default="", help="Override figsize in inches as 'W,H' (e.g. '11.25,5.4')")
    ap.add_argument("--bottom", type=float, default=None, help="Set TNT_OI_IV_LAYOUT_BOTTOM for this run")
    ap.add_argument(
        "--bbox-tight",
        action="store_true",
        help="Enable TNT_OI_IV_SAVE_BBOX_TIGHT=1 for this run (may change parity pixels)",
    )
    ap.add_argument("--n", type=int, default=25, help="Number of strikes")
    ap.add_argument("--spot", type=float, default=687.71, help="Spot price used only for title text")
    args = ap.parse_args()

    if args.bottom is not None:
        os.environ["TNT_OI_IV_LAYOUT_BOTTOM"] = str(float(args.bottom))
    if bool(args.bbox_tight):
        os.environ["TNT_OI_IV_SAVE_BBOX_TIGHT"] = "1"
    if args.figsize:
        os.environ["TNT_OI_IV_FIGSIZE"] = str(args.figsize).strip()

    n = max(5, min(80, int(args.n)))
    strikes = [float(680 + i) for i in range(n)]

    # Deterministic-ish curve so you can see if walls/caps are readable.
    call_oi = [float(1200 + (i * 140) + (3000 if i > int(n * 0.65) else 0)) for i in range(n)]
    put_oi = [float(1600 + (i * 110) + (2800 if i < int(n * 0.35) else 0)) for i in range(n)]

    # Mild smile for IV overlay.
    mid = (n - 1) / 2.0
    iv = [float(18.0 + 0.045 * ((i - mid) ** 2)) for i in range(n)]

    title = f"OI/IV — SPY — exp 2026-02-03 — ±$10 Window | Top {min(25, n)} — Spot {float(args.spot):.2f}  — contracts 40"

    try:
        from delivery.oi_iv_render import render_oi_iv_png
    except Exception as exc:
        print(f"error: failed to import renderer: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    labels = [f"{s:g}" for s in strikes]

    png = render_oi_iv_png(
        title=title,
        x_labels=labels,
        iv_pct=iv,
        oi_calls=call_oi,
        oi_puts=put_oi,
        dpi=int(args.dpi),
        include_iv_overlay=True,
        figsize=_parse_figsize(args.figsize) if args.figsize else None,
    )
    if not isinstance(png, (bytes, bytearray)) or not png:
        print("error: renderer returned empty png", file=sys.stderr)
        return 3

    out_path = (args.out or "").strip()
    if not out_path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = str(Path("artifacts") / f"oi_iv_local_{ts}.png")

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(png))

    # Print the embedded PNG tEXt metadata (layout/build/tag).
    try:
        from scripts.png_text_probe import read_png_text_metadata

        meta = read_png_text_metadata(bytes(png))
        print(f"wrote: {path.as_posix()}")
        print("tnt_oi_iv_layout", (meta.get("tnt_oi_iv_layout") or "").strip() or None)
        print("tnt_build", (meta.get("tnt_build") or "").strip() or None)
        print("tnt_render_tag", (meta.get("tnt_render_tag") or "").strip() or None)
        print("meta_keys", sorted(list(meta.keys()))[:50])
    except Exception:
        print(f"wrote: {path.as_posix()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
