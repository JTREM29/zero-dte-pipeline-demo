import os
import time
from typing import Optional

import requests


class CanaryNotifier:
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = (webhook_url or os.getenv("TNT_CANARY_WEBHOOK_URL", "")).strip()
        self._last_state: Optional[str] = None  # "OK" | "DOWN" | "DRAINING"
        self._last_post_ts: float = 0.0
        self.min_post_interval_s = int(os.getenv("TNT_CANARY_MIN_POST_INTERVAL_S", "60"))

    def post(self, msg: str, timeout_s: float = 3.0) -> None:
        if not self.webhook_url:
            return
        requests.post(self.webhook_url, json={"content": msg}, timeout=timeout_s).raise_for_status()

    def post_on_change(self, state: str, msg: str) -> bool:
        now = time.time()
        if self._last_state == state:
            return False
        if (now - self._last_post_ts) < self.min_post_interval_s and self._last_state is not None:
            return False
        self.post(msg)
        self._last_state = state
        self._last_post_ts = now
        return True
