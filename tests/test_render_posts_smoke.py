"""Regression harness guard for autopost payloads."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_render_posts_smoke_goldens() -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "render_posts_smoke.py"
    cmd = [sys.executable, str(script), "--compare", "--all-scenarios"]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if result.returncode != 0:
        detail = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        raise AssertionError(f"render_posts_smoke.py --compare failed\n{detail}")
