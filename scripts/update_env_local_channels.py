"""Update repo-root .env.local with TNT channel IDs and toggles.

Safe to run repeatedly. It preserves existing lines (including secrets) and only
adds/updates specific keys.

Usage:
    .venv/Scripts/python.exe scripts/update_env_local_channels.py
"""

from __future__ import annotations

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_LOCAL = PROJECT_ROOT / ".env.local"

# Values provided by operator (non-secret channel IDs).
UPDATES: dict[str, str] = {
    "DISCORD_CANARY_CHANNEL_ID": "1451014818827079750",
    "TNT_CANARY_CHANNEL_ID": "1451014818827079750",
    "ASK_TNT_CHANNEL_ID": "1462986002506186926",
    "TNT_ASK_CHANNEL_ID": "1462986002506186926",
    "TNT_ASK_CHANNEL_ONLY_ENABLED": "1",
    "TNT_ASK_CHANNEL_COOLDOWN_SEC": "6",
    "BOT_ALERT_CHANNEL_ID": "1458633494107525173",
    "CALENDAR_EARNINGS_CHANNEL_ID": "1462948320229200065",
    "TNT_CALENDAR_EARNINGS_CHANNEL_ID": "1462948320229200065",
    "EARNINGS_POST_CHANNEL_ID": "1462948320229200065",
    "EARNINGS_AUTOPOST_ENABLED": "1",
    "NEWS_POST_CHANNEL_ID": "1462982126982140056",
    "NEWS_BROADCAST_CHANNEL_ID": "1462982126982140056",
    "NEWS_BROADCAST_ENABLED": "1",
    "OPS_RECAP_CHANNEL_ID": "1451014818827079750",
    "TNT_OPS_CHANNEL_ID": "1451014818827079750",
}

ALIAS_FROM = "EARNINGS_AUTPOST_ENABLED"  # common typo


def main() -> int:
    if not ENV_LOCAL.exists() or not ENV_LOCAL.is_file():
        raise SystemExit(f"Missing {ENV_LOCAL}")

    text = ENV_LOCAL.read_text(encoding="utf-8", errors="ignore").splitlines(True)

    pat = re.compile(r"^(\s*)(?:export\s+)?([A-Z0-9_]+)\s*=.*$")

    seen: set[str] = set()
    out: list[str] = []

    for line in text:
        m = pat.match(line.rstrip("\n"))
        if not m:
            out.append(line)
            continue

        indent, key = m.group(1), m.group(2)
        if key == ALIAS_FROM:
            key = "EARNINGS_AUTOPOST_ENABLED"

        if key in UPDATES:
            out.append(f"{indent}{key}={UPDATES[key]}\n")
            seen.add(key)
        else:
            out.append(line)

    missing = [k for k in UPDATES.keys() if k not in seen]
    if missing:
        out.append("\n# --- TNT channels / toggles (managed) ---\n")
        for k in missing:
            out.append(f"{k}={UPDATES[k]}\n")

    ENV_LOCAL.write_text("".join(out), encoding="utf-8")

    print(f"updated_keys={len(UPDATES)} added={len(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
