"""Simple Discord webhook bridge for the CLI."""
from __future__ import annotations

import textwrap
from typing import Any, Dict

from zero_dte_pipeline.discord import post_message


def post_morning_brief_to_discord(
    symbol: str,
    brief_markdown: str,
    snapshot: Dict[str, Any],
) -> None:
    """Post the morning brief to Discord via a basic webhook."""

    title = f"📈 Morning Brief for {symbol.upper()}"
    content = textwrap.dedent(
        f"""{title}

{brief_markdown}
"""
    )

    success = post_message(content[:1900], username="ZeroDTE Morning Brief")
    if not success:
        print("Discord webhook not configured or post failed; see logs for details.")
