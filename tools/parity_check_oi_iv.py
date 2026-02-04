"""Compatibility wrapper.

The repo's parity checker lives in scripts/parity_check_oi_iv.py, but some runbooks call tools/parity_check_oi_iv.py.
This wrapper preserves that command path without duplicating logic.
"""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    target = repo_root / "scripts" / "parity_check_oi_iv.py"
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
