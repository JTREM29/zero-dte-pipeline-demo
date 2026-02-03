from __future__ import annotations

import io
import json
import os
import sys
import subprocess
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


PNG_SIG = b"\x89PNG\r\n\x1a\n"

_START = time.time()

# Ensure imports resolve from the repo checkout (delivery.*, etc.), even if the
# process is started without PYTHONPATH set or has an older installed package.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    if _REPO_ROOT and _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
except Exception:
    pass


def _build_id() -> str:
    b = (os.getenv("TNT_BUILD") or "").strip()
    if b:
        return b
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        return (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root)
            .decode("utf-8", errors="ignore")
            .strip()
        )
    except Exception:
        return ""


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "uptime_s": int(time.time() - _START),
            "build": _build_id(),
            "service": "tnt-worker-parity",
        }
    )


async def render_smoke(_: Request) -> Response:
    # Minimal PNG smoke render without touching market-data.
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(3.2, 1.8))
        ax.set_facecolor("#0b0f14")
        fig.patch.set_facecolor("#0b0f14")
        ax.text(0.5, 0.5, "TNT WORKER OK", ha="center", va="center", color="#c9d1d9")
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120)
        plt.close(fig)
        data = buf.getvalue()
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"smoke_render_failed:{type(exc).__name__}:{exc}"},
            status_code=500,
        )

    if not data.startswith(PNG_SIG):
        return JSONResponse({"ok": False, "error": "smoke_not_png"}, status_code=500)
    return Response(content=data, media_type="image/png")


async def render_oi_iv(request: Request) -> Response:
    """Render an OI/IV chart from a deterministic payload.

    Payload contract (mirrors scripts/parity_check_oi_iv.py):
    - strikes: list[float]
    - call_oi: list[float]
    - put_oi: list[float]
    - call_iv: list[float] (IV in %)
    - put_iv: optional (ignored for parity; kept for backward-compat)
    - title: str

    Optional:
    - include_iv_overlay: bool
    - dpi: int
    """

    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8")) if raw else {}

        strikes = payload.get("strikes")
        call_oi = payload.get("call_oi")
        put_oi = payload.get("put_oi")
        call_iv = payload.get("call_iv")
        title = str(payload.get("title") or "OI/IV")
        include_iv = bool(payload.get("include_iv_overlay", True))
        dpi = int(payload.get("dpi") or 150)

        if not isinstance(strikes, list) or not isinstance(call_oi, list) or not isinstance(put_oi, list):
            raise ValueError("missing_arrays")
        labels = [f"{float(s):g}" for s in strikes]
        oi_calls = [float(x) for x in call_oi]
        oi_puts = [float(x) for x in put_oi]
        iv = [float(x) for x in (call_iv or [])]

        from delivery.oi_iv_render import render_oi_iv_png

        def _ensure_text_meta(png_bytes: bytes, updates: dict[str, str]) -> bytes:
            try:
                from PIL import Image
                from PIL.PngImagePlugin import PngInfo

                im = Image.open(io.BytesIO(png_bytes))
                im.load()
                info = getattr(im, "text", None)
                meta = dict(info) if isinstance(info, dict) else {}
                changed = False
                for k, v in (updates or {}).items():
                    if not k:
                        continue
                    if str(meta.get(k) or ""):
                        continue
                    meta[str(k)] = str(v)
                    changed = True
                if not changed:
                    return png_bytes
                pnginfo = PngInfo()
                for k, v in meta.items():
                    if isinstance(k, str) and isinstance(v, str):
                        try:
                            pnginfo.add_text(k, v)
                        except Exception:
                            pass
                out = io.BytesIO()
                im.convert("RGBA").save(out, format="PNG", pnginfo=pnginfo)
                return out.getvalue() or png_bytes
            except Exception:
                return png_bytes

        png = render_oi_iv_png(
            title=title,
            x_labels=labels,
            iv_pct=(iv if include_iv else []),
            oi_calls=oi_calls,
            oi_puts=oi_puts,
            dpi=max(80, min(400, dpi)),
            include_iv_overlay=include_iv,
        )
        if not isinstance(png, (bytes, bytearray)) or not png:
            raise RuntimeError("render_empty")
        data = bytes(png)
        # Add missing (invisible) metadata for CLX-side verification.
        data = _ensure_text_meta(
            data,
            {
                "tnt_oi_iv_layout": "v2",
                "tnt_build": _build_id(),
                "tnt_render_tag": (os.getenv("TNT_RENDER_TAG") or os.getenv("COMPUTERNAME") or "").strip(),
            },
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"bad_payload:{type(exc).__name__}:{exc}"},
            status_code=400,
        )

    if not data.startswith(PNG_SIG):
        return JSONResponse({"ok": False, "error": "not_png"}, status_code=500)

    # No visible stamps here; attribution stays in PNG metadata via delivery.tnt_chart_style.tnt_png_metadata.
    return Response(content=data, media_type="image/png")


routes = [
    Route("/healthz", endpoint=healthz, methods=["GET"]),
    Route("/v1/render/smoke", endpoint=render_smoke, methods=["GET"]),
    Route("/v1/render/oi_iv", endpoint=render_oi_iv, methods=["POST"]),
]


app = Starlette(routes=routes)
