"""
concurrency.py
---------------
A single, process-wide "semaphore" capping how many downloads can run at
the same time, shared between the normal chat flow (handlers.py) and
inline mode (inline_mode.py). Keeping this in its own tiny module avoids
a circular import between the two.

On Railway's Free Plan (very limited CPU/RAM), keeping this number low
(2-3) is essential for stability.

Unlike a plain ``asyncio.Semaphore``, ``DynamicSemaphore`` re-reads its
limit from the database on every acquire, so the Manager Bot's "maximum
simultaneous downloads" setting takes effect immediately without needing
to restart the process.
"""

from __future__ import annotations

import asyncio

import config


class DynamicSemaphore:
    """An asyncio-friendly semaphore whose limit can change at runtime."""

    def __init__(self, default_limit: int) -> None:
        self._default_limit = max(1, default_limit)
        self._count = 0
        self._condition = asyncio.Condition()

    async def _current_limit(self) -> int:
        try:
            import database

            return max(1, await database.get_max_concurrent_downloads())
        except Exception:  # noqa: BLE001 - DB not ready yet / any failure -> safe default
            return self._default_limit

    async def acquire(self) -> None:
        async with self._condition:
            limit = await self._current_limit()
            while self._count >= limit:
                await self._condition.wait()
                limit = await self._current_limit()
            self._count += 1

    async def release(self) -> None:
        async with self._condition:
            self._count = max(0, self._count - 1)
            self._condition.notify_all()

    async def __aenter__(self) -> "DynamicSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


download_semaphore: DynamicSemaphore = DynamicSemaphore(max(1, config.MAX_CONCURRENT_DOWNLOADS))
