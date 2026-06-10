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
    guards the heavy endpoints; constructed directly in tests."""

    def __init__(self, limit: int, timeout: float):
        self.limit = limit
        self.timeout = timeout
        self._sem = asyncio.Semaphore(limit)

    @asynccontextmanager
    async def slot(self):
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.warning("heavy_slot: shedding request — all %d slots busy", self.limit)
            raise CapacityError
        try:
            yield
        finally:
            self._sem.release()


heavy = HeavyLimiter(HEAVY_CONCURRENCY, HEAVY_ACQUIRE_TIMEOUT_S)
