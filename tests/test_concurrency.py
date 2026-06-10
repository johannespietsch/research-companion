"""Tests for the heavy-work limiter that sheds load during a spike."""
import asyncio

import pytest

from bot.concurrency import CapacityError, HeavyLimiter


def test_sheds_when_all_slots_busy():
    async def scenario():
        lim = HeavyLimiter(limit=2, timeout=0.05)
        # Fill both slots and hold them.
        cm1, cm2 = lim.slot(), lim.slot()
        await cm1.__aenter__()
        await cm2.__aenter__()

        # A third caller can't get in within the timeout → shed.
        with pytest.raises(CapacityError):
            async with lim.slot():
                pass

        # Free one slot; now a caller gets in.
        await cm1.__aexit__(None, None, None)
        async with lim.slot():
            pass

        await cm2.__aexit__(None, None, None)

    asyncio.run(scenario())


def test_slot_is_released_after_use():
    async def scenario():
        lim = HeavyLimiter(limit=1, timeout=0.05)
        # Use and release the single slot repeatedly — no leak.
        for _ in range(3):
            async with lim.slot():
                pass
        # Still acquirable afterwards.
        async with lim.slot():
            pass

    asyncio.run(scenario())


def test_slot_released_even_when_body_raises():
    async def scenario():
        lim = HeavyLimiter(limit=1, timeout=0.05)
        with pytest.raises(ValueError):
            async with lim.slot():
                raise ValueError("boom")
        # The exception must not have leaked the slot.
        async with lim.slot():
            pass

    asyncio.run(scenario())


def test_waiter_gets_in_once_a_slot_frees():
    async def scenario():
        lim = HeavyLimiter(limit=1, timeout=1.0)
        cm = lim.slot()
        await cm.__aenter__()

        async def release_soon():
            await asyncio.sleep(0.02)
            await cm.__aexit__(None, None, None)

        # Waiter blocks until release_soon frees the slot — no CapacityError.
        async with asyncio.timeout(0.5):
            await asyncio.gather(
                release_soon(),
                _acquire_and_release(lim),
            )

    async def _acquire_and_release(lim):
        async with lim.slot():
            pass

    asyncio.run(scenario())


def test_acquire_release_pair_frees_the_slot():
    async def scenario():
        lim = HeavyLimiter(limit=1, timeout=0.05)
        await lim.acquire()
        # No slot left → next acquire sheds.
        with pytest.raises(CapacityError):
            await lim.acquire()
        lim.release()
        # Freed → acquire succeeds again.
        await lim.acquire()
        lim.release()

    asyncio.run(scenario())


def test_acquire_timeout_override_beats_the_default():
    async def scenario():
        # Default timeout is tiny, but a generous per-call override lets a
        # caller queue until a slot frees (the job-runner pattern).
        lim = HeavyLimiter(limit=1, timeout=0.01)
        await lim.acquire()

        async def release_soon():
            await asyncio.sleep(0.05)
            lim.release()

        async def queue_with_override():
            await lim.acquire(timeout=1.0)  # waits past the 0.01 default
            lim.release()

        async with asyncio.timeout(0.5):
            await asyncio.gather(release_soon(), queue_with_override())

    asyncio.run(scenario())
