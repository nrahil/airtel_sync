"""In-memory sliding-window rate limiter.

Single-process (the demo deployment). For multiple workers, back this with Redis;
the interface stays the same. Only *accepted* hits are recorded, so hammering while
blocked does not extend the block.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window = window_seconds
        self._now = now
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = now()

    def hit(self, key: str) -> float | None:
        """Record a request. ``None`` = allowed; otherwise seconds until a slot frees up."""
        with self._lock:
            now = self._now()
            self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(hits[0] + self.window - now, 0.001)
            hits.append(now)
            return None

    def _sweep(self, now: float) -> None:
        """Drop idle keys at most once per window, bounding memory."""
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        cutoff = now - self.window
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._hits)
