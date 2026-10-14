"""Bounded owner work remains visible through completion of its actual thread."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock, RLock

from app.persistence.onboarding import OnboardingJourneyUnavailable, require_journey


class OwnerEffects:
    def __init__(self):
        self._tasks = {}
        self._protected = set()
        self._lock = Lock()
        self._journey = ContextVar("onboarding_owner_effect_journey", default=None)
        self._admission_lock = RLock()
        self._admission_guard = lambda _: None
        self._closed = False

    def bind_effect_gate(self, lock, guard):
        with self._admission_lock:
            self._admission_guard = guard
            self._admission_lock = lock

    def close_admission(self):
        with self._admission_lock:
            self._closed = True

    @contextmanager
    def journey(self, journey_id):
        token = self._journey.set(journey_id)
        try:
            yield
        finally:
            self._journey.reset(token)

    def accepted_work(self):
        with self._lock:
            return tuple(sorted(marker for task, marker in self._tasks.items() if not task.done()))

    def accepted_protected_work(self):
        with self._lock:
            return tuple(sorted(marker for task, marker in self._tasks.items()
                if task in self._protected and not task.done()))

    async def run(self, function, *args, journey_id=None, protected=True, **kwargs):
        locator = self._journey.get() if journey_id is None else journey_id
        try:
            marker = "external:" + require_journey(locator)
        except (ValueError, TypeError, AttributeError):
            marker = "external:unknown"
        with self._admission_lock:
            if self._closed:
                raise OnboardingJourneyUnavailable("journey_unavailable")
            self._admission_guard(locator)
            task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            with self._lock:
                self._tasks[task] = marker
                if protected:
                    self._protected.add(task)

        def completed(current):
            if not current.cancelled():
                current.exception()
            with self._lock:
                self._tasks.pop(current, None)
                self._protected.discard(current)

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def drain(self):
        while True:
            with self._lock:
                tasks = tuple(task for task in self._tasks if not task.done())
            if not tasks:
                return
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)
