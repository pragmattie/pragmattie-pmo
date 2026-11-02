"""An in-memory, per-key sliding-window rate limiter (used per client IP on `/auth/login`).

Limits: it lives in one process's memory, so each worker process counts on its own and a restart
forgets every count. That is acceptable for a single local process; a deployment with several
processes or hosts needs a shared store (distributed limiting is not in scope yet).

Only allowed requests are counted, so each key holds at most `limit` timestamps, and keys whose
last hit has left the window are swept out at most once per window, so memory can't grow without
limit.
"""

import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.window = window_seconds
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._next_sweep = 0.0
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        """Count a request for `key` and say whether it is within `limit` per window."""
        now = self.clock()
        cutoff = now - self.window
        with self._lock:
            if now >= self._next_sweep:
                self._sweep(cutoff)
                self._next_sweep = now + self.window
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def _sweep(self, cutoff: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    def __len__(self) -> int:
        """The number of keys held (for tests)."""
        return len(self._hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._next_sweep = 0.0
