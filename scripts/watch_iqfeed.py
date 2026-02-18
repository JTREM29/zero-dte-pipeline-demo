"""Simple console watcher that reports whether IQFeed is running (Windows)."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Iterable

# Known IQFeed processes to scan for (case-insensitive).
IQFEED_PROCESSES: set[str] = {
    "iqconnect.exe",
    "iqfeed.exe",
    "iqfeed_launcher.exe",
}


def _tasklist() -> Iterable[str]:
    """Yield lowercase lines from the Windows task list."""
    try:
        output = subprocess.check_output(
            ["tasklist"],
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    return output.lower().splitlines()


def iqfeed_running() -> bool:
    """Return True if any IQFeed process is currently running."""
    for line in _tasklist():
        if any(name in line for name in IQFEED_PROCESSES):
            return True
    return False


def clear_console() -> None:
    """Clear the console on the current platform if possible."""
    command = "cls" if os.name == "nt" else "clear"
    if shutil.which(command):
        os.system(command)


def main() -> None:
    """Loop forever, reporting IQFeed status once per minute."""
    try:
        while True:
            clear_console()
            if iqfeed_running():
                print("⚠️  IQFeed is running ⚠️")
            else:
                print("✅ IQFeed not running")
            time.sleep(60)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
