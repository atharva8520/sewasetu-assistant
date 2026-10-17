# -*- coding: utf-8 -*-
"""
Sliding-window rate limiter per authenticated subject.
Rule: 60 calls per 10 minutes per subject with a friendly message.
"""

import time
from collections import defaultdict, deque
import threading


class RateLimiter:
    def __init__(self, max_calls: int = 60, window_seconds: float = 600.0):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, subject: str) -> tuple[bool, str | None]:
        """
        Check if the subject is within rate limits.
        Returns:
            (allowed: bool, message: str | None)
        """
        if not subject:
            return True, None

        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            q = self._calls[subject]
            # Evict timestamps older than the sliding window
            while q and q[0] < cutoff:
                q.popleft()

            if len(q) >= self.max_calls:
                msg = (
                    "You have made several requests recently (limit: 60 calls per 10 minutes). "
                    "Please wait a few minutes before trying again."
                )
                return False, msg

            q.append(now)
            return True, None
