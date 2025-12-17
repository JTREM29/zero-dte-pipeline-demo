"""Historical JSON collector kept for backward compatibility.

The SQLite-based collector lives in ``iqfeed_sqlite_collector.py``. Execute that
module for the active architecture.
"""

from iqfeed_sqlite_collector import main


if __name__ == "__main__":  # pragma: no cover - simple hand-off
    main()
