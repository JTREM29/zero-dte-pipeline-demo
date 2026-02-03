from __future__ import annotations

import io
import json
import os
import sys
import struct
import subprocess
import time
import traceback
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


def _sha256_file(path: str) -> str:
    try:
        import hashlib

        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _png_with_text_meta(png_bytes: bytes, updates: dict[str, str]) -> bytes:
    """Inject/override PNG tEXt chunks (keyword\0text) without extra deps."""

    try:
        import zlib

        if not isinstance(png_bytes, (bytes, bytearray)):
            return bytes(png_bytes)
        data = bytes(png_bytes)
        if not data.startswith(PNG_SIG):
            return data

        # Parse chunks; keep everything but remove existing tEXt for the keys we override.
        i = len(PNG_SIG)
        out = bytearray(PNG_SIG)
        keys = {str(k) for k in (updates or {}).keys() if str(k)}

        def _read_u32(b: bytes, off: int) -> int:
            return int.from_bytes(b[off : off + 4], "big", signed=False)

        while i + 8 <= len(data):
            ln = _read_u32(data, i)
            typ = data[i + 4 : i + 8]
            j = i + 8
            k = j + ln
            crc_end = k + 4
            if crc_end > len(data):
                break
            chunk_data = data[j:k]

            if typ == b"tEXt" and keys:
                try:
                    nul = chunk_data.find(b"\x00")
                    if nul > 0:
                        keyword = chunk_data[:nul].decode("latin-1", errors="ignore")
                        if keyword in keys:
                            i = crc_end
                            continue
                except Exception:
                    pass

            # If this is IEND, inject our tEXt chunks right before it.
            if typ == b"IEND":
                for kk, vv in (updates or {}).items():
                    if not kk:
                        continue
                    key_b = str(kk).encode("latin-1", errors="ignore")
                    val_b = str(vv).encode("latin-1", errors="ignore")
                    text_data = key_b + b"\x00" + val_b
                    out += struct.pack(">I", len(text_data))
                    out += b"tEXt"
                    out += text_data
                    crc = zlib.crc32(b"tEXt" + text_data) & 0xFFFFFFFF
                    out += struct.pack(">I", crc)

            out += data[i:crc_end]
            i = crc_end

        if out.startswith(PNG_SIG) and len(out) > len(PNG_SIG):
            return bytes(out)
        return data
    except Exception:
        try:
            return bytes(png_bytes)
        except Exception:
            return png_bytes


async def healthz(_: Request) -> JSONResponse:
    oi_path = os.path.join(_REPO_ROOT, "delivery", "oi_iv_render.py")
    return JSONResponse(
        {
            "ok": True,
            "uptime_s": int(time.time() - _START),
            "build": _build_id(),
            "service": "tnt-worker-parity",
            "oi_iv_render_sha": (_sha256_file(oi_path)[:12] if oi_path else ""),
            "module_file": __file__,
            "repo_root": _REPO_ROOT,
            "python": sys.version.split(" ")[0],
            "executable": sys.executable,
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
        tb = traceback.format_exc()
        if len(tb) > 6000:
            tb = tb[:6000] + "\n... (truncated)"
        return JSONResponse(
            {
                "ok": False,
                "error": f"smoke_render_failed:{type(exc).__name__}:{exc}",
                "traceback": tb,
                "cwd": os.getcwd(),
                "executable": sys.executable,
                "python": sys.version.split(" ")[0],
            },
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

        # Import the renderer explicitly from this checkout to avoid sys.path ambiguity.
        import importlib.util

        oi_path = os.path.join(_REPO_ROOT, "delivery", "oi_iv_render.py")
        spec = importlib.util.spec_from_file_location("tnt_delivery_oi_iv_render", oi_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("renderer_spec_failed")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        render_oi_iv_png = getattr(mod, "render_oi_iv_png", None)
        if not callable(render_oi_iv_png):
            raise RuntimeError("renderer_missing")

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
        # Inject/override invisible metadata for CLX-side verification (no Pillow required).
        data = _png_with_text_meta(
            data,
            {
                "tnt_oi_iv_layout": "v2",
                "tnt_build": _build_id(),
                "tnt_render_tag": (os.getenv("TNT_RENDER_TAG") or os.getenv("COMPUTERNAME") or "").strip(),
            },
        )
    except Exception as exc:
        tb = traceback.format_exc()
        if len(tb) > 6000:
            tb = tb[:6000] + "\n... (truncated)"
        return JSONResponse(
            {
                "ok": False,
                "error": f"bad_payload:{type(exc).__name__}:{exc}",
                "traceback": tb,
                "cwd": os.getcwd(),
                "executable": sys.executable,
                "python": sys.version.split(" ")[0],
            },
            status_code=400,
        )

    if not data.startswith(PNG_SIG):
        return JSONResponse({"ok": False, "error": "not_png"}, status_code=500)

    # No visible stamps here; attribution stays in PNG metadata via delivery.tnt_chart_style.tnt_png_metadata.
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "X-TNT-Service": "tnt-worker-parity",
            "X-TNT-Build": _build_id(),
            "X-TNT-OI-IV-Layout": "v2",
        },
    )


routes = [
    Route("/healthz", endpoint=healthz, methods=["GET"]),
    Route("/v1/render/smoke", endpoint=render_smoke, methods=["GET"]),
    Route("/v1/render/oi_iv", endpoint=render_oi_iv, methods=["POST"]),
]


app = Starlette(routes=routes)
