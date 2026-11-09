"""Sliding-window rate limit for ``run_command``."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Allow at most ``limit`` calls in any ``window`` seconds (``limit <= 0``: off)."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: deque[float] = deque()

    def try_acquire(self) -> bool:
        if self.limit <= 0:
            return True
        now = self._clock()
        while self._hits and now - self._hits[0] >= self.window:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(now)
        return True

    def retry_after(self) -> float:
        if self.limit <= 0 or len(self._hits) < self.limit:
            return 0.0
        return max(0.0, self.window - (self._clock() - self._hits[0]))
