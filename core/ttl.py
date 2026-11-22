"""In-memory counters and caches with expiry, for anti-spam / anti-raid hot paths.

Nothing here touches the database or runs a timer. Expired entries are dropped lazily
on access, plus a cheap full sweep every `_SWEEP_EVERY` operations so idle keys cannot
pile up.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Hashable

_SWEEP_EVERY = 1024


class SlidingCounter:
    """Counts events per key inside a rolling time window (seconds)."""

    __slots__ = ("window", "_hits", "_ops")

    def __init__(self, window: float):
        self.window = float(window)
        self._hits: dict[Hashable, deque[float]] = {}
        self._ops = 0

    def hit(self, key: Hashable, now: float | None = None) -> int:
        """Record one event and return how many fall inside the window (including this one)."""
        now = time.monotonic() if now is None else now
        self._ops += 1
        if self._ops >= _SWEEP_EVERY:
            self.sweep(now)
        bucket = self._hits.get(key)
        if bucket is None:
            bucket = self._hits[key] = deque()
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        bucket.append(now)
        return len(bucket)

    def count(self, key: Hashable, now: float | None = None) -> int:
        bucket = self._hits.get(key)
        if not bucket:
            return 0
        cutoff = (time.monotonic() if now is None else now) - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return len(bucket)

    def reset(self, key: Hashable) -> None:
        self._hits.pop(key, None)

    def sweep(self, now: float | None = None) -> None:
        self._ops = 0
        cutoff = (time.monotonic() if now is None else now) - self.window
        dead = [k for k, b in self._hits.items() if not b or b[-1] <= cutoff]
        for key in dead:
            del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)


class TTLDict:
    """A dict whose entries expire `ttl` seconds after they are set."""

    __slots__ = ("ttl", "_data", "_ops")

    def __init__(self, ttl: float):
        self.ttl = float(ttl)
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._ops = 0

    def set(self, key: Hashable, value: Any, ttl: float | None = None, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._ops += 1
        if self._ops >= _SWEEP_EVERY:
            self.sweep(now)
        self._data[key] = (now + (self.ttl if ttl is None else ttl), value)

    def get(self, key: Hashable, default: Any = None, now: float | None = None) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        if item[0] <= (time.monotonic() if now is None else now):
            del self._data[key]
            return default
        return item[1]

    def pop(self, key: Hashable, default: Any = None) -> Any:
        item = self._data.pop(key, None)
        if item is None or item[0] <= time.monotonic():
            return default
        return item[1]

    def __contains__(self, key: Hashable) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def sweep(self, now: float | None = None) -> None:
        self._ops = 0
        now = time.monotonic() if now is None else now
        dead = [k for k, (expires, _) in self._data.items() if expires <= now]
        for key in dead:
            del self._data[key]

    def __len__(self) -> int:
        return len(self._data)


_MISSING = object()
