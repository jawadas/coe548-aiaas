"""Cached model health.

Checking the model costs a real (tiny) inference call and can take seconds
when the GPU pod is down, so pages never ask the gateway directly. They ask
this service, which remembers the last answer for a short while and makes sure
that any number of simultaneous callers cause a single check.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Callable
from typing import Protocol

from app.gateway import ModelHealth

logger = logging.getLogger(__name__)

ONLINE_TTL = 45.0
OFFLINE_TTL = 15.0


class HealthSource(Protocol):
    """Anything that can check the model, i.e. ``GatewayClient`` or a test double."""

    async def health(self) -> ModelHealth: ...


class ModelStatusService:
    """Wraps ``gateway.health()`` with a TTL cache and single-flight refresh.

    An ``online`` answer is kept for 45 seconds; ``offline`` and ``unknown``
    only for 15, so recovery shows up quickly.

    * ``await get()``: the current health, checking the model if the cached
      answer is too old. What pages and ``/api/status`` use.
    * ``cached``: the last answer without ever triggering a check (``None``
      before the first one). For code that must not wait.
    * ``refresh_in_background()``: start a check now without waiting for it,
      e.g. right after a chat call found the model down.
    """

    def __init__(
        self,
        gateway: HealthSource,
        *,
        online_ttl: float = ONLINE_TTL,
        offline_ttl: float = OFFLINE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._online_ttl = online_ttl
        self._offline_ttl = offline_ttl
        self._clock = clock
        self._cached: ModelHealth | None = None
        self._expires_at = 0.0
        self._refresh: asyncio.Task[ModelHealth] | None = None

    @property
    def cached(self) -> ModelHealth | None:
        """The last known health, however old, without triggering a check."""
        return self._cached

    async def get(self, *, force: bool = False) -> ModelHealth:
        """Current model health, from the cache when it is fresh enough.

        ``force=True`` always checks (joining a check that is already running).
        """
        if not force and self._cached is not None and self._clock() < self._expires_at:
            return self._cached
        # Shielded: a caller that goes away must not cancel the check others wait on.
        return await asyncio.shield(self._start_refresh())

    def refresh_in_background(self) -> None:
        """Start a check now unless one is already running; do not wait for the result."""
        self._start_refresh()

    def invalidate(self) -> None:
        """Forget that the cached answer is fresh, so the next ``get`` checks again."""
        self._expires_at = 0.0

    async def aclose(self) -> None:
        """Cancel a check that is still running. Called when the application stops."""
        task = self._refresh
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _start_refresh(self) -> asyncio.Task[ModelHealth]:
        if self._refresh is None:
            self._refresh = asyncio.create_task(self._check())
        return self._refresh

    async def _check(self) -> ModelHealth:
        try:
            health = await self._gateway.health()
        except Exception:
            # health() reports failures as a state; anything raised here is a bug.
            # Answer "unknown" rather than break every page that shows the status.
            logger.exception("Model health check failed unexpectedly")
            health = ModelHealth("unknown", None, dt.datetime.now(dt.UTC), "Health check failed")
        finally:
            self._refresh = None
        self._cached = health
        self._expires_at = self._clock() + (
            self._online_ttl if health.is_online else self._offline_ttl
        )
        return health
