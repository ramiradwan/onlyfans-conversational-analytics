"""Bounded, account-scoped hints about committed analytics publications."""

from __future__ import annotations

import asyncio
from threading import RLock


class PublicationEvents:
    def __init__(self, maximum=8):
        self._maximum = maximum
        self._lock = RLock()
        self._listeners = set()

    def subscribe(self, account):
        listener = (account, asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            if len(self._listeners) >= self._maximum:
                return None
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener):
        with self._lock:
            self._listeners.discard(listener)

    def publish(self, account):
        with self._lock:
            listeners = tuple(item for item in self._listeners if item[0] == account)
        self._signal(listeners)

    def recheck_sessions(self):
        with self._lock:
            listeners = tuple(self._listeners)
        self._signal(listeners)

    @staticmethod
    def _signal(listeners):
        for _, loop, signal in listeners:
            try:
                loop.call_soon_threadsafe(signal.set)
            except RuntimeError:
                # A closed listener cannot undo an already committed publication.
                continue


publication_events = PublicationEvents()
