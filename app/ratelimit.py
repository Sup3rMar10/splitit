from __future__ import annotations

import time
from collections import defaultdict


class RateLimiter:
    """Token bucket per key: `capacity` burst, refilled at `per_sec`. In memory and per process."""

    def __init__(self, capacity: float, per_sec: float, clock=time.monotonic):
        self.capacity = capacity
        self.per_sec = per_sec
        self.clock = clock
        self.state: dict[str, tuple[float, float]] = defaultdict(lambda: (capacity, clock()))

    def allow(self, key: str) -> bool:
        tokens, last = self.state[key]
        now = self.clock()
        tokens = min(self.capacity, tokens + (now - last) * self.per_sec)
        if tokens < 1:
            self.state[key] = (tokens, now)
            return False
        self.state[key] = (tokens - 1, now)
        return True
