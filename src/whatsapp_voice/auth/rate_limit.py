"""Per-caller rate limiting.

Two reasons this exists, and only the second is about abuse:

1. **Cost.** Every message spends speech-to-text minutes and LLM tokens. A phone
   left face-down in a pocket, sending voice notes into a chat, is a real way to
   run up a bill overnight.
2. **Containment.** If a handset is lost, a limit bounds how much can be pulled
   out of it before someone notices.

A sliding window rather than a fixed one: a fixed window lets a caller send the
whole quota at 10:59 and the whole quota again at 11:00, which is exactly the burst
it is meant to prevent.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque


class SharedRateLimiter:
    """A fixed window, counted in the shared store.

    Coarser than the sliding window below — a caller can send the quota at the
    end of one window and again at the start of the next — but it is *correct
    across workers*, which the sliding one is not. With several processes the
    sliding limiter enforces `limit × workers`, and a limit that is silently
    three times what you configured is worse than one that is slightly lumpy.

    Used whenever a store is configured; the sliding window remains the default
    for a single process.
    """

    def __init__(self, store, limit: int = 20, window_s: float = 3600.0) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._store = store
        self._limit = limit
        self._window_s = window_s

    def check(self, key: str) -> bool:
        # One atomic increment. A read-then-write would let concurrent workers
        # both see room and both proceed.
        window = int(time.time() // self._window_s)
        return self._store.incr(f"rate:{key}:{window}", ttl_s=self._window_s) <= self._limit

    def remaining(self, key: str) -> int:
        window = int(time.time() // self._window_s)
        used = self._store.get(f"rate:{key}:{window}") or 0
        return max(0, self._limit - int(used))

    def retry_after_s(self, key: str) -> float:
        window_start = (int(time.time() // self._window_s)) * self._window_s
        return max(0.0, window_start + self._window_s - time.time())


class SlidingWindowRateLimiter:
    """At most `limit` events per `window_s`, per key.

    In-memory, so each process keeps its own counts — correct for one worker.
    A deployment with several needs `SharedRateLimiter`, or the effective limit
    is `limit × workers`. `store.guard_worker_count` refuses to start in that
    configuration rather than letting it pass quietly.
    """

    def __init__(self, limit: int = 20, window_s: float = 3600.0,
                 capacity: int = 1000) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._window_s = window_s
        self._capacity = capacity
        self._events: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str) -> bool:
        """Record an event and report whether it is within the limit.

        Rejected events are NOT recorded. Otherwise a caller who keeps trying
        during a cooldown would keep pushing their own window forward and never
        get back in — a lockout instead of a limit.
        """
        now = time.monotonic()
        window = self._window_for(key)

        cutoff = now - self._window_s
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self._limit:
            return False

        window.append(now)
        return True

    def remaining(self, key: str) -> int:
        """How many more events this key may send right now. Does not consume one."""
        now = time.monotonic()
        window = self._events.get(key)
        if window is None:
            return self._limit
        live = sum(1 for stamp in window if stamp > now - self._window_s)
        return max(0, self._limit - live)

    def retry_after_s(self, key: str) -> float:
        """Seconds until the oldest event leaves the window. 0 if not limited."""
        window = self._events.get(key)
        if not window or len(window) < self._limit:
            return 0.0
        return max(0.0, self._window_s - (time.monotonic() - window[0]))

    def reset(self, key: str) -> None:
        self._events.pop(key, None)

    def _window_for(self, key: str) -> deque[float]:
        window = self._events.get(key)
        if window is None:
            window = deque()
            self._events[key] = window
        self._events.move_to_end(key)

        while len(self._events) > self._capacity:
            self._events.popitem(last=False)
        return window
