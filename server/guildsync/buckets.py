"""Token buckets for the HTTPS front's rate limits (docs/SYNC-PROTOCOL.md "Cost")."""

import threading

IDLE_S = 3600.0  # a bucket untouched this long is full again, so pruning it changes nothing


class Buckets:
    """Token buckets by id: `take` returns 0 when allowed, else the seconds until one token is back."""

    def __init__(self, capacity: float, per_s: float) -> None:
        self.capacity, self.per_s = capacity, per_s
        self._state: dict[str, tuple[float, float]] = {}  # id -> (tokens, last update)
        self._lock = threading.Lock()
        self._pruned = 0.0

    def take(self, id: str, now: float) -> float:
        with self._lock:
            if now - self._pruned >= 60.0:
                self._prune(now)
            tokens, last = self._state.get(id, (self.capacity, now))
            tokens = min(self.capacity, tokens + max(now - last, 0.0) * self.per_s)
            if tokens >= 1.0:
                self._state[id] = (tokens - 1.0, now)
                return 0.0
            self._state[id] = (tokens, now)
            return (1.0 - tokens) / self.per_s

    def prune(self, now: float) -> None:
        with self._lock:
            self._prune(now)

    def _prune(self, now: float) -> None:
        self._pruned = now
        self._state = {k: v for k, v in self._state.items() if now - v[1] <= IDLE_S}
