from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    # Keep the canonical implementation in scripts/ so it can be imported by tasks.
    from scripts.golden_path_smoke import main as _main

    return int(_main())


if __name__ == "__main__":
    raise SystemExit(main())
