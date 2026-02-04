"""Convenience entrypoint for the MVP live alerts evaluator.

Users commonly run:
  python run_alerts_mvp_live.py ...

The implementation lives in scripts/run_alerts_mvp_live.py.
"""

from __future__ import annotations

from scripts.run_alerts_mvp_live import main


if __name__ == "__main__":
    raise SystemExit(main())
