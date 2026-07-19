"""Bounded coordination for post-canonical analytics projection work."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import secrets
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, TypeVar, cast
from uuid import uuid4

from app.analytics.errors import (
    AnalyticsError,
    ProjectionBackpressure,
    ProjectionBuildCancelled,
    ProjectionCoordinatorClosed,
    ProjectionStorageUnavailable,
)
from app.analytics.pipeline import (
    AnalyticsPipeline,
    ProjectionCandidate,
)
from app.models.analytics import AvailabilityStatus
from app.transport.ingestion import AccountReadModel


_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class ProjectionScheduleState:
    availability: AvailabilityStatus
    requested_revision: int | None = None
    attempted_revision: int | None = None
    reason_code: str | None = None


class _CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def cancelled(self) -> bool:
        return self._cancelled.is_set()


@dataclass(slots=True)
class _AccountWork:
    requested_revision: int
    attempted_revision: int | None = None
    cancellation: _CancellationToken | None = None


class _ExecutorSaturated(RuntimeError):
    pass


class _OwnedBoundedExecutor:
    """ThreadPoolExecutor with a hard running-plus-queued task bound."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_tasks: int,
        thread_name_prefix: str,
    ) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._slots = threading.BoundedSemaphore(max_tasks)
        self._lock = threading.RLock()
        self._futures: set[concurrent.futures.Future[object]] = set()
        self._shutdown = False

    def submit(
        self, callable_: Callable[[], _Result]
    ) -> concurrent.futures.Future[_Result]:
        if not self._slots.acquire(blocking=False):
            raise _ExecutorSaturated()
        with self._lock:
            if self._shutdown:
                self._slots.release()
                raise ProjectionCoordinatorClosed()
            try:
                future = self._executor.submit(callable_)
            except BaseException:
                self._slots.release()
                raise
            untyped = cast(concurrent.futures.Future[object], future)
            self._futures.add(untyped)
            future.add_done_callback(self._completed)
            return future

    def _completed(self, future: concurrent.futures.Future[object]) -> None:
        with self._lock:
            if future in self._futures:
                self._futures.remove(future)
                self._slots.release()

    def begin_shutdown(self) -> tuple[concurrent.futures.Future[object], ...]:
        """Seal submissions, cancel queued calls, and snapshot active calls."""

        with self._lock:
            self._shutdown = True
            futures = tuple(self._futures)
            self._executor.shutdown(wait=False, cancel_futures=True)
            return futures

    def join(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)


class InProcessProjectionScheduler:
    """Coalesce revisions through a bounded, fixed-concurrency coordinator."""

    def __init__(
        self,
        pipeline: AnalyticsPipeline,
        *,
        worker_count: int = 2,
        queue_capacity: int = 64,
        failure_state_capacity: int | None = None,
    ) -> None:
        if worker_count <= 0:
            raise ValueError("worker_count must be positive")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        self.pipeline = pipeline
        self.worker_count = worker_count
        self.queue_capacity = queue_capacity
        self.failure_state_capacity = (
            failure_state_capacity
            if failure_state_capacity is not None
            else worker_count + queue_capacity
        )
        if self.failure_state_capacity <= 0:
            raise ValueError("failure_state_capacity must be positive")

        self._pending: deque[str] = deque()
        self._work: dict[str, _AccountWork] = {}
        self._failures: OrderedDict[str, ProjectionScheduleState] = OrderedDict()
        self._available: OrderedDict[str, int] = OrderedDict()
        self._workers: set[asyncio.Task[None]] = set()
        self._state_lock = threading.RLock()
        self._publication_locks_guard = threading.RLock()
        self._publication_locks: dict[str, tuple[threading.Lock, int]] = {}
        self._publication_closed = threading.Event()
        self._active_publications = 0
        self._scheduler_owner_id = str(uuid4())
        self._publication_capability = secrets.token_hex(32)
        self._publication_epoch: str | None = None
        self._publication_epoch_storage_serial = -1
        self._projection_storage_failure_serial = 0
        self._epoch_lock = asyncio.Lock()
        self._recovery_requests: dict[str, int] = {}
        self._recovery_task: asyncio.Task[None] | None = None
        self._accepting = True
        self._closed = False
        self._close_result: bool | None = None
        self._detached_worker_count = 0
        self._thread_name_prefix = f"analytics-projection-{id(self):x}"
        self._executor: _OwnedBoundedExecutor | None = _OwnedBoundedExecutor(
            max_workers=worker_count,
            max_tasks=worker_count + queue_capacity,
            thread_name_prefix=self._thread_name_prefix,
        )
        self.pipeline.set_projection_failure_callback(
            self._projection_storage_failed
        )

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    @property
    def retained_account_count(self) -> int:
        """Expose the bounded coordinator footprint for deterministic tests."""

        with self._state_lock:
            return len(self._work) + len(self._failures)

    @property
    def pending_worker_count(self) -> int:
        with self._state_lock:
            return sum(not task.done() for task in self._workers)

    @property
    def executor_thread_count(self) -> int:
        return sum(
            thread.is_alive() and thread.name.startswith(self._thread_name_prefix)
            for thread in threading.enumerate()
        )

    @property
    def detached_worker_count(self) -> int:
        return self._detached_worker_count

    async def _run_owned(self, callable_: Callable[[], _Result]) -> _Result:
        with self._state_lock:
            executor = self._executor
            if self._closed or executor is None:
                raise ProjectionCoordinatorClosed()
            try:
                future = executor.submit(callable_)
            except _ExecutorSaturated as error:
                raise ProjectionBackpressure() from error
        return await asyncio.wrap_future(future)

    async def canonical_account(self, creator_account_id: str) -> AccountReadModel:
        """Read canonical state without blocking the event-loop thread."""

        return await self._run_owned(
            functools.partial(self.pipeline.canonical_account, creator_account_id)
        )

    async def active_projection(
        self, creator_account_id: str, account: AccountReadModel
    ):
        """Read a witness-bound projection without blocking the event loop."""

        return await self._run_owned(
            functools.partial(
                self.pipeline.active_projection,
                creator_account_id,
                account,
            )
        )

    @contextmanager
    def _account_publication(self, creator_account_id: str):
        with self._publication_locks_guard:
            current = self._publication_locks.get(creator_account_id)
            lock, users = current if current is not None else (threading.Lock(), 0)
            self._publication_locks[creator_account_id] = (lock, users + 1)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._publication_locks_guard:
                current = self._publication_locks.get(creator_account_id)
                if current is not None and current[0] is lock:
                    remaining = current[1] - 1
                    if remaining:
                        self._publication_locks[creator_account_id] = (
                            lock,
                            remaining,
                        )
                    else:
                        self._publication_locks.pop(creator_account_id, None)

    def _publish_if_open(self, candidate: ProjectionCandidate) -> None:
        with self._account_publication(candidate.creator_account_id):
            with self._state_lock:
                closed = self._publication_closed.is_set()
                current_epoch = self._publication_epoch
                if closed or candidate.publication_epoch != current_epoch:
                    raise ProjectionCoordinatorClosed()
                self._active_publications += 1
            try:
                self.pipeline.publish_candidate(candidate)
            finally:
                with self._state_lock:
                    self._active_publications -= 1

    async def _ensure_publication_epoch(self) -> str:
        async with self._epoch_lock:
            while True:
                with self._state_lock:
                    storage_serial = self._projection_storage_failure_serial
                    epoch = self._publication_epoch
                    if (
                        epoch is not None
                        and self._publication_epoch_storage_serial == storage_serial
                    ):
                        return epoch
                    if self._publication_closed.is_set():
                        raise ProjectionCoordinatorClosed()
                opened = await self._run_owned(
                    functools.partial(
                        self.pipeline.open_publication_epoch,
                        self._scheduler_owner_id,
                        self._publication_capability,
                        retain_fence_connection=True,
                    )
                )
                with self._state_lock:
                    if self._publication_closed.is_set():
                        close_opened = True
                    elif storage_serial != self._projection_storage_failure_serial:
                        close_opened = False
                    else:
                        self._publication_epoch = opened
                        self._publication_epoch_storage_serial = storage_serial
                        return opened
                if close_opened:
                    self.pipeline.fence_publication_epoch(opened)
                    try:
                        await asyncio.to_thread(
                            self.pipeline.revoke_publication_epoch,
                            opened,
                            self._scheduler_owner_id,
                            self._publication_capability,
                        )
                    except AnalyticsError:
                        pass
                    raise ProjectionCoordinatorClosed()

    def _revoke_and_close_storage(
        self,
        publication_epoch: str | None,
        deadline: float,
    ) -> bool:
        """Persist the close fence without extending the caller's hard deadline."""

        revoked = publication_epoch is None
        if publication_epoch is not None:
            while True:
                try:
                    self.pipeline.revoke_publication_epoch(
                        publication_epoch,
                        self._scheduler_owner_id,
                        self._publication_capability,
                    )
                    revoked = True
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        break
                    # A transient first failure must not strand an otherwise
                    # healthy persisted publication capability in the open state.
                    continue
        try:
            self.pipeline.close_projection_storage()
        except Exception:
            return False
        return revoked

    async def _ensure_projection_storage(self) -> None:
        if not self.pipeline.projection_storage_requires_recovery():
            return
        await self._run_owned(self.pipeline.ensure_projection_storage)

    def _projection_storage_failed(self, creator_account_id: str | None) -> None:
        with self._state_lock:
            self._projection_storage_failure_serial += 1
            self._publication_epoch = None
            self._publication_epoch_storage_serial = -1
            if creator_account_id is None:
                self._available.clear()
            else:
                self._available.pop(creator_account_id, None)

    async def request_recovery(
        self, creator_account_id: str, canonical_revision: int
    ) -> None:
        """Queue one coalesced scheduler-owned repair/rebuild and return."""

        with self._state_lock:
            if self._closed or not self._accepting:
                return
            self._available.pop(creator_account_id, None)
            self._failures.pop(creator_account_id, None)
            self._recovery_requests[creator_account_id] = max(
                canonical_revision,
                self._recovery_requests.get(creator_account_id, canonical_revision),
            )
            task = self._recovery_task
            if task is not None and not task.done():
                return
            loop = asyncio.get_running_loop()
            self._recovery_task = loop.create_task(
                self._recovery_worker(),
                name="analytics-projection-recovery",
            )

    async def _recovery_worker(self) -> None:
        delay = 0.05
        while True:
            with self._state_lock:
                if self._closed:
                    self._recovery_requests.clear()
                    return
                requests = dict(self._recovery_requests)
            if not requests:
                return
            try:
                await self._ensure_projection_storage()
                await self._ensure_publication_epoch()
            except (ProjectionStorageUnavailable, ProjectionBackpressure):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
                continue
            except ProjectionCoordinatorClosed:
                return
            with self._state_lock:
                for account_id, revision in requests.items():
                    if self._recovery_requests.get(account_id) == revision:
                        self._recovery_requests.pop(account_id, None)
            for account_id, revision in requests.items():
                try:
                    await self.schedule(
                        account_id,
                        revision,
                        retry_failed=True,
                    )
                except ProjectionBackpressure:
                    with self._state_lock:
                        self._recovery_requests[account_id] = max(
                            revision,
                            self._recovery_requests.get(account_id, revision),
                        )
            delay = 0.05

    async def start(self, *, recover: bool = True) -> None:
        """Schedule every canonical account that lacks a current projection."""

        with self._state_lock:
            if not self._accepting or self._closed:
                raise ProjectionCoordinatorClosed()
        await self._ensure_projection_storage()
        await self._ensure_publication_epoch()
        if not recover:
            return
        revisions = await self._run_owned(self.pipeline.source.account_revisions)
        for creator_account_id, revision in revisions:
            if await self._projection_is_current(creator_account_id, revision):
                with self._state_lock:
                    self._record_available_locked(creator_account_id, revision)
                continue
            while True:
                try:
                    await self.schedule(
                        creator_account_id,
                        revision,
                        retry_failed=True,
                    )
                    break
                except ProjectionBackpressure:
                    # Startup recovery owns exactly one bounded waiter and retries
                    # only after admitted work has had an opportunity to finish.
                    await asyncio.sleep(0.001)

    async def schedule(
        self,
        creator_account_id: str,
        canonical_revision: int,
        *,
        retry_failed: bool = False,
    ) -> None:
        """Admit immediately, coalesce an account, or reject with backpressure."""

        if canonical_revision < 0:
            raise ValueError("canonical_revision must be non-negative")
        with self._state_lock:
            if not self._accepting or self._closed:
                raise ProjectionCoordinatorClosed()
            existing = self._work.get(creator_account_id)
            if existing is not None:
                existing.requested_revision = max(
                    existing.requested_revision,
                    canonical_revision,
                )
                return
            failure = self._failures.get(creator_account_id)
            if (
                failure is not None
                and failure.attempted_revision == canonical_revision
                and not retry_failed
            ):
                return
            available = self._available.get(creator_account_id)
            if available is not None and available >= canonical_revision:
                return
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if not self._accepting or self._closed:
                raise ProjectionCoordinatorClosed()
            existing = self._work.get(creator_account_id)
            if existing is not None:
                existing.requested_revision = max(
                    existing.requested_revision,
                    canonical_revision,
                )
                return
            failure = self._failures.get(creator_account_id)
            if (
                failure is not None
                and failure.attempted_revision == canonical_revision
                and not retry_failed
            ):
                return
            if len(self._pending) >= self.queue_capacity:
                raise ProjectionBackpressure()
            self._failures.pop(creator_account_id, None)
            self._available.pop(creator_account_id, None)
            self._work[creator_account_id] = _AccountWork(
                requested_revision=canonical_revision
            )
            self._pending.append(creator_account_id)
            self._launch_workers_locked(loop)

    async def _projection_is_current(
        self,
        creator_account_id: str,
        canonical_revision: int,
    ) -> bool:
        return await self._run_owned(
            functools.partial(
                self.pipeline.projection_is_current,
                creator_account_id,
                canonical_revision,
            )
        )

    def _record_available_locked(
        self, creator_account_id: str, revision: int
    ) -> None:
        self._available[creator_account_id] = revision
        self._available.move_to_end(creator_account_id)
        while len(self._available) > self.failure_state_capacity:
            self._available.popitem(last=False)

    def _launch_workers_locked(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._workers = {task for task in self._workers if not task.done()}
        launch_count = min(
            self.worker_count - len(self._workers),
            len(self._pending),
        )
        for _ in range(launch_count):
            task = loop.create_task(
                self._worker(),
                name="analytics-projection-worker",
            )
            self._workers.add(task)
            task.add_done_callback(self._worker_finished)

    def _worker_finished(self, task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        with self._state_lock:
            self._workers.discard(task)
            if self._accepting and self._pending:
                try:
                    loop = task.get_loop()
                    if not loop.is_running():
                        return
                except RuntimeError:
                    return
                self._launch_workers_locked(loop)

    async def _worker(self) -> None:
        while True:
            with self._state_lock:
                if self._closed or not self._pending:
                    return
                creator_account_id = self._pending.popleft()
                work = self._work.get(creator_account_id)
            if work is None:
                continue
            await self._drain_account(creator_account_id, work)

    def _remove_work(self, creator_account_id: str, work: _AccountWork) -> None:
        with self._state_lock:
            if self._work.get(creator_account_id) is work:
                self._work.pop(creator_account_id, None)

    async def _drain_account(
        self,
        creator_account_id: str,
        work: _AccountWork,
    ) -> None:
        while True:
            with self._state_lock:
                if self._closed or self._work.get(creator_account_id) is not work:
                    self._work.pop(creator_account_id, None)
                    return
                attempted_revision = work.requested_revision
                work.attempted_revision = attempted_revision
                token = _CancellationToken()
                work.cancellation = token

            candidate: ProjectionCandidate | None = None
            try:
                await self._ensure_projection_storage()
                publication_epoch = await self._ensure_publication_epoch()
                candidate = await self._run_owned(
                    functools.partial(
                        self.pipeline.build_candidate,
                        creator_account_id,
                        publication_epoch=publication_epoch,
                        cancellation_check=token.cancelled,
                    )
                )
            except asyncio.CancelledError:
                token.cancel()
                self._remove_work(creator_account_id, work)
                raise
            except ProjectionBuildCancelled:
                reason_code = "analytics_projection_cancelled"
            except AnalyticsError as error:
                reason_code = error.code
            except Exception:
                reason_code = "analytics_projection_failed"
            else:
                reason_code = None

            published = False
            if candidate is not None and reason_code is None:
                with self._state_lock:
                    eligible = (
                        not self._closed
                        and self._work.get(creator_account_id) is work
                        and candidate.source_revision >= work.requested_revision
                    )
                if eligible:
                    try:
                        await self._run_owned(
                            functools.partial(self._publish_if_open, candidate)
                        )
                        published = True
                    except AnalyticsError as error:
                        reason_code = error.code
                    except Exception:
                        reason_code = "analytics_projection_failed"

            if candidate is not None and not published:
                try:
                    await self._run_owned(
                        functools.partial(self.pipeline.discard_candidate, candidate)
                    )
                except (ProjectionCoordinatorClosed, ProjectionBackpressure):
                    # Expired leases make abandoned inactive work reclaimable on
                    # the next startup; it is never externally visible.
                    pass

            with self._state_lock:
                work.cancellation = None
                if self._closed or self._work.get(creator_account_id) is not work:
                    self._work.pop(creator_account_id, None)
                    return
                if (
                    reason_code is None
                    and published
                    and candidate is not None
                    and candidate.source_revision >= work.requested_revision
                ):
                    self._work.pop(creator_account_id, None)
                    self._failures.pop(creator_account_id, None)
                    self._record_available_locked(
                        creator_account_id, candidate.source_revision
                    )
                    return
                if work.requested_revision > attempted_revision:
                    continue
                self._work.pop(creator_account_id, None)
                failure = ProjectionScheduleState(
                    availability=AvailabilityStatus.ERROR,
                    requested_revision=work.requested_revision,
                    attempted_revision=attempted_revision,
                    reason_code=reason_code or "analytics_projection_incomplete",
                )
                self._failures[creator_account_id] = failure
                self._failures.move_to_end(creator_account_id)
                while len(self._failures) > self.failure_state_capacity:
                    self._failures.popitem(last=False)
                return

    def state(
        self,
        creator_account_id: str,
        *,
        canonical_revision: int | None = None,
    ) -> ProjectionScheduleState:
        """Return cached coordinator/active state without canonical I/O."""

        with self._state_lock:
            work = self._work.get(creator_account_id)
            if work is not None:
                return ProjectionScheduleState(
                    availability=AvailabilityStatus.BUILDING,
                    requested_revision=work.requested_revision,
                    attempted_revision=work.attempted_revision,
                )
            failure = self._failures.get(creator_account_id)
            if failure is not None:
                return failure
            available_revision = self._available.get(creator_account_id)
            if available_revision is not None and (
                canonical_revision is None
                or available_revision >= canonical_revision
            ):
                return ProjectionScheduleState(
                    AvailabilityStatus.AVAILABLE,
                    requested_revision=available_revision,
                    attempted_revision=available_revision,
                )
        return ProjectionScheduleState(AvailabilityStatus.UNAVAILABLE)

    async def wait(self, creator_account_id: str) -> ProjectionScheduleState:
        """Test/job seam for awaiting already-scheduled work across event loops."""

        while True:
            with self._state_lock:
                active = creator_account_id in self._work
            if not active:
                return self.state(creator_account_id)
            await asyncio.sleep(0.001)

    @staticmethod
    def _cancel_task(task: asyncio.Task[None]) -> None:
        if task.done():
            return
        try:
            loop = task.get_loop()
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop is current_loop:
            task.cancel()
        elif loop.is_running():
            loop.call_soon_threadsafe(task.cancel)

    async def close(self, *, timeout: float = 5.0) -> bool:
        """Close admission/publication first, then join cooperative owned work."""

        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = time.monotonic() + timeout
        with self._state_lock:
            if self._close_result is not None:
                return self._close_result
            self._accepting = False
            self._closed = True
            self._publication_closed.set()
            tasks = tuple(self._workers)
            for work in self._work.values():
                if work.cancellation is not None:
                    work.cancellation.cancel()
            self._pending.clear()
            self._work.clear()
            self._failures.clear()
            self._available.clear()
            self._recovery_requests.clear()
            executor = self._executor
            recovery_task = self._recovery_task
            epoch = self._publication_epoch
            self._publication_epoch = None
            self._publication_epoch_storage_serial = -1

        local_fenced = True
        if epoch is not None:
            try:
                # This is an in-memory, non-I/O fence. It is established before
                # yielding so a detached publication cannot pass its final check.
                self.pipeline.fence_publication_epoch(epoch)
            except Exception:
                local_fenced = False

        storage_task = asyncio.create_task(
            asyncio.to_thread(self._revoke_and_close_storage, epoch, deadline),
            name="analytics-projection-storage-close",
        )

        futures = executor.begin_shutdown() if executor is not None else ()
        for task in tasks:
            self._cancel_task(task)
        if recovery_task is not None:
            self._cancel_task(recovery_task)
        joined_tasks = (
            tasks
            + ((recovery_task,) if recovery_task is not None else ())
            + (storage_task,)
        )

        while time.monotonic() < deadline:
            if all(future.done() for future in futures) and all(
                task.done() for task in joined_tasks
            ):
                break
            await asyncio.sleep(min(0.001, max(0.0, deadline - time.monotonic())))

        joined = all(future.done() for future in futures) and all(
            task.done() for task in joined_tasks
        )
        storage_fenced = False
        if storage_task.done() and not storage_task.cancelled():
            try:
                storage_fenced = bool(storage_task.result())
            except Exception:
                storage_fenced = False
        if joined and executor is not None:
            executor.join()
        detached = sum(not future.done() for future in futures)
        with self._state_lock:
            self._detached_worker_count = detached
            self._workers.clear()
            self._executor = None
            self._close_result = joined and local_fenced and storage_fenced
        return self._close_result

    def abort(self) -> None:
        """Synchronous fail-closed reset seam used by process/test teardown."""

        with self._state_lock:
            if self._closed and self._executor is None:
                return
            self._accepting = False
            self._closed = True
            self._publication_closed.set()
            tasks = tuple(self._workers)
            for work in self._work.values():
                if work.cancellation is not None:
                    work.cancellation.cancel()
            self._pending.clear()
            self._work.clear()
            self._failures.clear()
            self._available.clear()
            self._recovery_requests.clear()
            executor = self._executor
            recovery_task = self._recovery_task
            epoch = self._publication_epoch
            self._publication_epoch = None
            self._publication_epoch_storage_serial = -1
        if epoch is not None:
            try:
                self.pipeline.fence_publication_epoch(epoch)
            except Exception:
                pass
            try:
                self.pipeline.revoke_publication_epoch(
                    epoch,
                    self._scheduler_owner_id,
                    self._publication_capability,
                )
            except Exception:
                pass
        self.pipeline.close_projection_storage()
        futures = executor.begin_shutdown() if executor is not None else ()
        for task in tasks:
            if task.done():
                continue
            loop = task.get_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(task.cancel)
        if recovery_task is not None and not recovery_task.done():
            loop = recovery_task.get_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(recovery_task.cancel)
        with self._state_lock:
            self._detached_worker_count = sum(
                not future.done() for future in futures
            )
            self._workers.clear()
            self._executor = None
            self._close_result = not self._detached_worker_count
