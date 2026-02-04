from __future__ import annotations

import argparse
import json
import os
import posixpath
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    handler.send_response(int(status))
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _guess_mime(path: str) -> str:
    if path.lower().endswith(".png"):
        return "image/png"
    return "application/octet-stream"


class ArtifactHandler(BaseHTTPRequestHandler):
    server_version = "TNTArtifactHTTP/1.0"

    def _token_ok(self) -> bool:
        token = str(getattr(self.server, "token", "") or "").strip()  # type: ignore[attr-defined]
        if not token:
            return True
        got = (self.headers.get("X-TNT-Token") or "").strip()
        return got == token

    def _artifact_root(self) -> Path:
        return Path(str(getattr(self.server, "artifact_dir", ".") or ".")).resolve()  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if not self._token_ok():
            _json(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return

        raw_path = self.path.split("?", 1)[0]
        path = posixpath.normpath(raw_path)

        if path in ("/", ""):
            _json(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "tnt-artifacts",
                    "endpoints": ["/health", "/healthz", "/artifacts/<name>"],
                },
            )
            return

        if path in ("/health", "/healthz"):
            root = self._artifact_root()
            _json(
                self,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "tnt-artifacts",
                    "artifact_dir": str(root),
                    "exists": root.exists(),
                },
            )
            return

        if not path.startswith("/artifacts/"):
            _json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        leaf = path[len("/artifacts/") :]
        leaf = leaf.lstrip("/")
        if not leaf or ".." in leaf or leaf.startswith("/") or "\\" in leaf:
            _json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_name"})
            return

        root = self._artifact_root()
        file_path = (root / leaf).resolve()

        # Prevent path traversal
        try:
            file_path.relative_to(root)
        except Exception:
            _json(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_name"})
            return

        if not file_path.exists() or not file_path.is_file():
            _json(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "missing"})
            return

        try:
            data = file_path.read_bytes()
        except Exception as exc:
            _json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"read_failed:{type(exc).__name__}:{exc}"})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", _guess_mime(leaf))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Keep logs quiet unless explicitly enabled.
        if str(getattr(self.server, "verbose", "") or "").strip():  # type: ignore[attr-defined]
            super().log_message(format, *args)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Serve TNT artifacts over HTTP (Dell-side).")
    p.add_argument("--bind", default=os.getenv("TNT_ARTIFACT_HTTP_BIND", "0.0.0.0"))
    p.add_argument("--port", type=int, default=_env_int("TNT_ARTIFACT_HTTP_PORT", 8787))
    p.add_argument("--dir", default=os.getenv("TNT_ARTIFACTS_DIR", "artifacts"), help="Folder containing PNG artifacts")
    p.add_argument("--token", default=os.getenv("TNT_ARTIFACT_HTTP_TOKEN", ""))
    p.add_argument("--verbose", action="store_true")

    args = p.parse_args(argv)

    artifact_dir = Path(str(args.dir)).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((str(args.bind), int(args.port)), ArtifactHandler)
    httpd.artifact_dir = str(artifact_dir)  # type: ignore[attr-defined]
    httpd.token = str(args.token or "").strip()  # type: ignore[attr-defined]
    httpd.verbose = bool(args.verbose)  # type: ignore[attr-defined]

    print(f"[TNT][ARTIFACTS][HTTP] serving dir={artifact_dir} on http://{args.bind}:{args.port} token={'set' if httpd.token else 'none'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
