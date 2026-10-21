"""Rate-limiting algorithms with an injectable clock.

Every limiter exposes ``check(key, now, cost=1) -> Decision``. ``now`` is passed in
(seconds, float) so behaviour is deterministic in tests and simulations.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float  # seconds until a request of the same cost could succeed (0 if allowed)
    reset_after: float  # seconds until the limiter is back to full capacity


class Limiter:
    """Base class: ``limit`` requests per ``window`` seconds."""

    name = "base"

    def __init__(self, limit: int, window: float):
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = int(limit)
        self.window = float(window)
        self._lock = threading.Lock()

    def check(self, key: str, now: float, cost: int = 1) -> Decision:
        if cost <= 0:
            raise ValueError("cost must be positive")
        with self._lock:
            return self._check(key, now, cost)

    def _check(self, key: str, now: float, cost: int) -> Decision:  # pragma: no cover
        raise NotImplementedError

    def evict_idle(self, now: float) -> int:  # pragma: no cover
        raise NotImplementedError

    def __len__(self) -> int:  # pragma: no cover
        raise NotImplementedError


class FixedWindow(Limiter):
    """Counter reset at aligned window boundaries. Cheap, but allows 2x bursts at edges."""

    name = "fixed_window"

    def __init__(self, limit: int, window: float):
        super().__init__(limit, window)
        self._state: dict[str, tuple[int, int]] = {}  # key -> (window_index, count)

    def _check(self, key, now, cost):
        idx = math.floor(now / self.window)
        end = (idx + 1) * self.window
        cur_idx, count = self._state.get(key, (idx, 0))
        if cur_idx != idx:
            count = 0
        if cost > self.limit or count + cost > self.limit:
            self._state[key] = (idx, count)
            retry = math.inf if cost > self.limit else end - now
            return Decision(False, self.limit, max(self.limit - count, 0), retry, end - now)
        count += cost
        self._state[key] = (idx, count)
        return Decision(True, self.limit, self.limit - count, 0.0, end - now)

    def evict_idle(self, now):
        idx = math.floor(now / self.window)
        dead = [k for k, (i, _) in self._state.items() if i != idx]
        for k in dead:
            del self._state[k]
        return len(dead)

    def __len__(self):
        return len(self._state)


class SlidingWindowLog(Limiter):
    """Exact: stores a timestamp per accepted request. Accurate, O(limit) memory per key."""

    name = "sliding_log"

    def __init__(self, limit: int, window: float):
        super().__init__(limit, window)
        self._state: dict[str, deque[tuple[float, int]]] = {}
        self._used: dict[str, int] = {}

    def _check(self, key, now, cost):
        log = self._state.setdefault(key, deque())
        used = self._used.get(key, 0)
        while log and log[0][0] <= now - self.window:
            used -= log.popleft()[1]
        if cost > self.limit:
            self._used[key] = used
            return Decision(False, self.limit, max(self.limit - used, 0), math.inf, self._reset(log, now))
        if used + cost > self.limit:
            # find when enough weight has expired
            need = used + cost - self.limit
            freed = 0
            retry = self.window
            for ts, w in log:
                freed += w
                if freed >= need:
                    retry = ts + self.window - now
                    break
            self._used[key] = used
            return Decision(False, self.limit, self.limit - used, retry, self._reset(log, now))
        log.append((now, cost))
        used += cost
        self._used[key] = used
        return Decision(True, self.limit, self.limit - used, 0.0, self._reset(log, now))

    def _reset(self, log, now):
        return max(log[-1][0] + self.window - now, 0.0) if log else 0.0

    def evict_idle(self, now):
        dead = [k for k, log in self._state.items() if not log or log[-1][0] <= now - self.window]
        for k in dead:
            self._state.pop(k, None)
            self._used.pop(k, None)
        return len(dead)

    def __len__(self):
        return len(self._state)


class SlidingWindowCounter(Limiter):
    """Approximation: weights the previous fixed window by its overlap with the sliding window."""

    name = "sliding_counter"

    def __init__(self, limit: int, window: float):
        super().__init__(limit, window)
        self._state: dict[str, tuple[int, int, int]] = {}  # key -> (idx, current, previous)

    def _roll(self, key, idx):
        cur_idx, cur, prev = self._state.get(key, (idx, 0, 0))
        if cur_idx == idx:
            return cur, prev
        if cur_idx == idx - 1:
            return 0, cur
        return 0, 0

    def _check(self, key, now, cost):
        idx = math.floor(now / self.window)
        frac = (now - idx * self.window) / self.window
        cur, prev = self._roll(key, idx)
        estimate = prev * (1 - frac) + cur
        remaining = max(int(math.floor(self.limit - estimate)), 0)
        if cost > self.limit or estimate + cost > self.limit:
            self._state[key] = (idx, cur, prev)
            retry = math.inf if cost > self.limit else self._retry(cur, prev, frac, cost)
            return Decision(False, self.limit, remaining, max(retry, 0.0), (1 - frac) * self.window + self.window)
        cur += cost
        self._state[key] = (idx, cur, prev)
        remaining = max(int(math.floor(self.limit - (prev * (1 - frac) + cur))), 0)
        return Decision(True, self.limit, remaining, 0.0, (1 - frac) * self.window + self.window)

    def _retry(self, cur, prev, frac, cost):
        """Earliest wait (no further traffic) until prev*(1-f) + cur + cost <= limit."""
        room = self.limit - cur - cost
        if room >= 0:
            # decay of the previous window alone frees enough, inside this window
            f = 1 - room / prev if prev > 0 else frac
            return max(f - frac, 0.0) * self.window
        # must wait for the next window, where `cur` becomes the decaying previous window
        f = max(1 - (self.limit - cost) / cur, 0.0)
        return (1 - frac + f) * self.window

    def evict_idle(self, now):
        idx = math.floor(now / self.window)
        dead = [k for k, (i, _, _) in self._state.items() if i < idx - 1]
        for k in dead:
            del self._state[k]
        return len(dead)

    def __len__(self):
        return len(self._state)


class TokenBucket(Limiter):
    """Capacity ``limit`` tokens refilled at ``limit/window`` per second. Allows bursts up to capacity."""

    name = "token_bucket"

    def __init__(self, limit: int, window: float):
        super().__init__(limit, window)
        self.rate = self.limit / self.window
        self._state: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)

    def _check(self, key, now, cost):
        tokens, last = self._state.get(key, (float(self.limit), now))
        tokens = min(float(self.limit), tokens + max(now - last, 0.0) * self.rate)
        if cost > self.limit:
            self._state[key] = (tokens, now)
            return Decision(False, self.limit, int(tokens), math.inf, (self.limit - tokens) / self.rate)
        if tokens + 1e-9 < cost:
            self._state[key] = (tokens, now)
            return Decision(False, self.limit, int(tokens), (cost - tokens) / self.rate,
                            (self.limit - tokens) / self.rate)
        tokens = max(tokens - cost, 0.0)
        self._state[key] = (tokens, now)
        return Decision(True, self.limit, int(tokens + 1e-9), 0.0, (self.limit - tokens) / self.rate)

    def evict_idle(self, now):
        # A bucket idle long enough to be full is indistinguishable from a new one.
        dead = [k for k, (t, last) in self._state.items()
                if min(self.limit, t + (now - last) * self.rate) >= self.limit]
        for k in dead:
            del self._state[k]
        return len(dead)

    def __len__(self):
        return len(self._state)


ALGORITHMS = {cls.name: cls for cls in (FixedWindow, SlidingWindowLog, SlidingWindowCounter, TokenBucket)}


def make_limiter(algorithm: str, limit: int, window: float) -> Limiter:
    try:
        return ALGORITHMS[algorithm](limit, window)
    except KeyError:
        raise ValueError(f"unknown algorithm {algorithm!r}; choose from {sorted(ALGORITHMS)}") from None
