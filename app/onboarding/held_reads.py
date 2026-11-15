"""Read challenges fetched ahead of the proof read that spends them. A challenge authorizes nothing."""
from __future__ import annotations

import asyncio

LIMIT = 64


def _retrieve(task):
    if not task.cancelled():
        task.exception()


class HeldReads:
    """At most one pending challenge fetch per operation. The read waits for it rather than fetching again."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def hold(self, key, fetch) -> bool:
        if key in self._tasks:
            return False
        if len(self._tasks) >= LIMIT:
            self._tasks.pop(next(iter(self._tasks))).cancel()
        task = asyncio.ensure_future(fetch())
        task.add_done_callback(_retrieve)
        self._tasks[key] = task
        return True

    async def take(self, key):
        """The held challenge for this key, or None when there is none or its fetch failed."""
        task = self._tasks.pop(key, None)
        if task is None:
            return None
        await asyncio.wait({task})
        if task.cancelled() or task.exception() is not None:
            return None
        return task.result()

    def close(self):
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
