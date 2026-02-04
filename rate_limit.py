import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict


@dataclass
class LimitResult:
    ok: bool
    retry_after_s: int = 0
    reason: str = ""


class RollingWindowLimiter:
    def __init__(self, max_events: int, window_s: int):
        self.max_events = max_events
        self.window_s = window_s
        self._events: Dict[int, Deque[float]] = {}

    def check(self, user_id: int) -> LimitResult:
        now = time.time()
        q = self._events.setdefault(user_id, deque())

        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()

        if len(q) >= self.max_events:
            retry_after = int((q[0] + self.window_s) - now) + 1
            return LimitResult(ok=False, retry_after_s=max(1, retry_after), reason="rate_limited")

        q.append(now)
        return LimitResult(ok=True)

    def consume(self, user_id: int, cost: int = 1) -> LimitResult:
        """Consume cost credits within the window.
        Returns LimitResult with retry_after_s when over the budget.
        """
        now = time.time()
        q = self._events.setdefault(user_id, deque())

        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()

        cost = max(1, int(cost))
        if len(q) + cost > self.max_events:
            needed = (len(q) + cost) - self.max_events
            idx = max(0, min(len(q) - 1, needed - 1))
            retry_after = int((q[idx] + self.window_s) - now) + 1
            return LimitResult(ok=False, retry_after_s=max(1, retry_after), reason="rate_limited")

        for _ in range(cost):
            q.append(now)
        return LimitResult(ok=True)
