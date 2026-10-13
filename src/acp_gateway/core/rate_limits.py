"""Monotonic, process-local admission budget shared across each channel."""

import math
import time
from collections import defaultdict, deque

from acp_gateway.config import RateLimitSettings
from acp_gateway.core.errors import RateLimited


class WorkRateLimiter:
    def __init__(self, settings: RateLimitSettings | None = None) -> None:
        self.settings = (settings or RateLimitSettings()).model_copy(deep=True)
        # Channel names come from trusted adapters, never user-selected threads.
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def check(self, channel: str) -> None:
        now = time.monotonic()
        recent = self._recent[channel]
        cutoff = now - self.settings.window_seconds
        while recent and recent[0] <= cutoff:
            recent.popleft()
        if len(recent) >= self.settings.work_requests:
            raise RateLimited(max(1, math.ceil(recent[0] - cutoff)))
        # No await between checking and reserving: concurrent requests in the
        # core's event loop cannot all consume the last available slot.
        recent.append(now)
