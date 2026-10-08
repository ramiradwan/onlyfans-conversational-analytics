"""Cooperative background reads without delaying foreground admission."""

from contextlib import contextmanager
from threading import Lock
import time
from typing import Callable

from app.analytics.cancellation import CancellationCheck, check_cancelled

BACKGROUND_RECORDS_PER_SLICE = 64
BACKGROUND_WORK_SLICE_SECONDS = 0.002
BACKGROUND_PAUSE_SECONDS = 0.001


class QuestionWork:
    """Share CPU fairly; source identity and request admission stay authoritative."""

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter,
                 sleep: Callable[[float], None] = time.sleep):
        self._clock, self._sleep = clock, sleep
        self._lock = Lock()
        # The runtime's existing two question slots bound this counter. It is
        # only a pressure hint, so additional resource owners remain visible.
        self._active_foreground = 0

    @contextmanager
    def foreground(self):
        """Notice an already admitted request without waiting for background work."""

        with self._lock:
            self._active_foreground += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_foreground -= 1

    def background(self, *, cancellation_check: CancellationCheck | None = None):
        """Return one scan's consume callback; every pause still advances its work.

        Checkpoints bound records and elapsed work between opportunities to
        yield. A single record or native call remains indivisible. The callback
        owns no transaction, cache, thread, queue, or account state.
        """

        check_cancelled(cancellation_check)
        records = 0
        deadline = self._clock() + BACKGROUND_WORK_SLICE_SECONDS

        def consume():
            nonlocal records, deadline
            records += 1
            now = self._clock()
            if records < BACKGROUND_RECORDS_PER_SLICE and now < deadline:
                return
            records = 0
            check_cancelled(cancellation_check)
            with self._lock:
                active = self._active_foreground > 0
            if active:
                # Never sleep under the counter lock or wait for readers to
                # become idle. Sustained traffic cannot stop scan progress.
                self._sleep(BACKGROUND_PAUSE_SECONDS)
                check_cancelled(cancellation_check)
            deadline = self._clock() + BACKGROUND_WORK_SLICE_SECONDS

        return consume
