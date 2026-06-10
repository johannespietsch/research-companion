"""Bound concurrent heavy work so a traffic spike can't pile unbounded load
onto the single Fly machine.

The expensive request paths (fetch + transcode/transcribe + 1-2 LLM calls) hold
CPU, RAM, and an LLM slot for seconds at a time. Without a cap, a Product-Hunt-
style burst lets dozens run at once on 2 shared CPUs / 1 GB → OOM or every
request crawling past the Worker's ~25s budget and timing out together.

A semaphore caps how many run concurrently; callers that can't get a slot
within a short wait are shed fast (HTTP 503 upstream) instead of queuing
forever. The limit and wait are env-tunable so they can be raised after a
vertical VM bump without a redeploy.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Max concurrent heavy requests. Sized for a 2-CPU / 1 GB box; raise it (env)
# after scaling the VM up for a launch.
HEAVY_CONCURRENCY = int(os.getenv("HEAVY_CONCURRENCY", "8"))
# How long a request waits for a slot before being shed. Kept well under the
# Worker's ~25s budget so the caller gets a clean 503, not a timeout.
HEAVY_ACQUIRE_TIMEOUT_S = float(os.getenv("HEAVY_ACQUIRE_TIMEOUT_S", "2"))


class CapacityError(Exception):
    """No heavy-work slot freed up within the wait budget — shed the request."""


class HeavyLimiter:
    """A semaphore + bounded-wait acquire. One process-wide instance (`heavy`)
    guards every heavy path; constructed directly in tests.

    `acquire`/`release` are used to wrap a long body that has its own
    try/finally (the URL pipeline); `slot()` is the same thing as a context
    manager for callers that don't (e.g. file uploads)."""

    def __init__(self, limit: int, timeout: float):
        self.limit = limit
        self.timeout = timeout
        self._sem = asyncio.Semaphore(limit)

    async def acquire(self, timeout: float | None = None) -> None:
        """Wait up to `timeout` (default `self.timeout`) for a slot; raise
        CapacityError if none frees up. A larger timeout lets a caller queue
        (e.g. the polling job runner) rather than shed (synchronous web)."""
        wait = self.timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=wait)
        except asyncio.TimeoutError:
            logger.warning("heavy: shedding work — all %d slots busy", self.limit)
            raise CapacityError

    def release(self) -> None:
        self._sem.release()

    @asynccontextmanager
    async def slot(self, timeout: float | None = None):
        await self.acquire(timeout)
        try:
            yield
        finally:
            self.release()


heavy = HeavyLimiter(HEAVY_CONCURRENCY, HEAVY_ACQUIRE_TIMEOUT_S)
