"""Discord webhook helpers for Zero DTE pipeline."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import requests

from ..config import config

logger = logging.getLogger(__name__)


def _get_webhook_url() -> Optional[str]:
    """Return the configured Discord webhook URL if available."""

    # Primary source: config (env already considered there)
    configured = (config.discord_webhook_url or "").strip()
    if configured:
        return configured

    # Legacy fallbacks for compatibility
    for env_key in ("DISCORD_WEBHOOK_URL", "DISCORD_MORNING_WEBHOOK"):
        value = (os.getenv(env_key) or "").strip()
        if value:
            return value

    return None


def post_message(
    content: str,
    *,
    username: str = "ZeroDTE Bot",
    embeds: Optional[list[Dict[str, Any]]] = None,
) -> bool:
    """Fire-and-forget Discord webhook post. Returns True on success."""

    webhook_url = _get_webhook_url()
    if not webhook_url:
        logger.info("Discord webhook not configured; skipping post")
        return False

    payload: Dict[str, Any] = {
        "content": content[:2000],  # Discord content limit
        "username": username,
    }
    if embeds:
        payload["embeds"] = embeds

    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        if 200 <= resp.status_code < 300:
            logger.info("Posted message to Discord (status=%s)", resp.status_code)
            return True

        logger.warning(
            "Discord post failed: status=%s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        return False

    except Exception as exc:  # noqa: BLE001
        logger.exception("Discord post exception: %s", exc)
        return False
