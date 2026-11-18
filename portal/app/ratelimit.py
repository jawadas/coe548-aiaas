"""A small in-memory sliding-window rate limiter.

The portal runs as a single uvicorn worker, so process memory is the whole
picture and no shared store is needed. State is lost on restart, which only
ever makes the limits more lenient for a moment.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

LOGIN_PER_IP = (10, 300.0)
LOGIN_PER_EMAIL = (5, 300.0)
SIGNUP_PER_IP = (5, 3600.0)

_SWEEP_EVERY = 1000


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of one check: whether it may proceed and, if not, when to retry."""

    allowed: bool
    remaining: int
    retry_after: int


class SlidingWindowLimiter:
    """Allow at most ``limit`` hits per key in any ``window_seconds`` span.

    Each key keeps the timestamps of its recent accepted hits. A rejected hit
    is not recorded, so hammering a locked key does not extend the lock.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 50_000,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def hit(self, key: str) -> RateLimitResult:
        """Record an attempt for ``key`` if it is within the limit."""
        now = self._clock()
        self._maybe_sweep(now)
        stamps = self._fresh(key, now)
        if len(stamps) >= self.limit:
            return self._rejected(stamps, now)
        stamps.append(now)
        self._hits[key] = stamps
        return RateLimitResult(True, self.limit - len(stamps), 0)

    def check(self, key: str) -> RateLimitResult:
        """Report where ``key`` stands without recording an attempt."""
        now = self._clock()
        stamps = self._fresh(key, now)
        if len(stamps) >= self.limit:
            return self._rejected(stamps, now)
        return RateLimitResult(True, self.limit - len(stamps), 0)

    def reset(self, key: str | None = None) -> None:
        """Forget one key, or everything."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)

    def _fresh(self, key: str, now: float) -> deque[float]:
        stamps = self._hits.get(key) or deque()
        cutoff = now - self.window
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        return stamps

    def _rejected(self, stamps: deque[float], now: float) -> RateLimitResult:
        wait = stamps[0] + self.window - now
        return RateLimitResult(False, 0, max(1, math.ceil(wait)))

    def _maybe_sweep(self, now: float) -> None:
        """Drop idle keys now and then, and cap the table so it cannot grow without bound."""
        self._calls += 1
        full = len(self._hits) >= self._max_keys
        if self._calls % _SWEEP_EVERY and not full:
            return
        cutoff = now - self.window
        self._hits = {
            key: stamps for key, stamps in self._hits.items() if stamps and stamps[-1] > cutoff
        }
        if len(self._hits) >= self._max_keys:
            # Still full of live keys: forget the oldest half rather than grow.
            for key in list(self._hits)[: self._max_keys // 2]:
                del self._hits[key]


@dataclass(slots=True)
class AuthRateLimits:
    """The limiters guarding the unauthenticated endpoints."""

    login_ip: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(*LOGIN_PER_IP)
    )
    login_email: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(*LOGIN_PER_EMAIL)
    )
    signup_ip: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(*SIGNUP_PER_IP)
    )
