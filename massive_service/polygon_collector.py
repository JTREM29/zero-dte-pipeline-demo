"""Entry point for continuous Polygon ingestion service.

This thin wrapper lets us launch the legacy scripts.polygon_ingest loop via
`python -m massive_service.polygon_collector` while keeping the script
importable elsewhere.  The existing script handles all ingestion logic, so we
only need a reliable import shim and a small amount of process hygiene here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _import_polygon_ingest():
    try:
        from scripts import polygon_ingest  # type: ignore
    except ImportError:  # pragma: no cover - defensive path setup
        project_root = _resolve_project_root()
        sys.path.insert(0, str(project_root))
        from scripts import polygon_ingest  # type: ignore
    return polygon_ingest


def main() -> None:
    if os.getenv("POLYGON_ENABLED", "1") == "0":
        print("[polygon_collector] POLYGON_ENABLED=0, exiting")
        return

    ingest = _import_polygon_ingest()
    try:
        ingest.main()
    except KeyboardInterrupt:  # pragma: no cover - runtime signal handling
        print("[polygon_collector] Interrupted, shutting down")


if __name__ == "__main__":
    main()
