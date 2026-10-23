"""Explicit ownership of optional background findings and publication."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Callable

from app.analytics.errors import ProjectionBuildCancelled, ProjectionCoordinatorClosed, ProjectionStorageUnavailable
from app.analytics.nlp_findings_store import FindingsStore, MAX_ACCOUNTS
from app.analytics.nlp_processing import FindingsRunner
from app.analytics.opaque_refs import account_ref
from app.analytics.query_execution import QuestionCancelled
from app.analytics.scheduling import _OwnedBoundedExecutor
from app.nlp.profiles import read_profile
from app.services.nlp_dependencies import dependency_service, configure_dependency_service

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FindingsConfiguration:
    dependencies: Callable
    authorize: Callable
    clock: Callable = lambda: datetime.now(timezone.utc)
    label_resolver: Callable | None = None
    interval: float = .25

    def __post_init__(self):
        if not all(callable(value) for value in (self.dependencies, self.authorize, self.clock)):
            raise ValueError('findings_configuration_invalid')
        if type(self.interval) not in (int, float) or not .01 <= self.interval <= 60:
            raise ValueError('findings_interval_invalid')


class _BoundDatabase:
    def __init__(self, owner, store):
        self.owner, self.store = owner, store
        self.gate = None

    @contextmanager
    def lease(self):
        with (self.gate if self.gate is not None else nullcontext()), self.owner.lifecycle_store() as current:
            if current is not self.store:
                raise ProjectionStorageUnavailable()
            yield current

    @contextmanager
    def read(self):
        with self.lease() as current, current.database.read() as connection:
            yield connection

    @contextmanager
    def transaction(self, **kwargs):
        with self.lease() as current, current.database.transaction(**kwargs) as connection:
            yield connection


class _BoundProjections:
    def __init__(self, owner, store):
        self.database = _BoundDatabase(owner, store)
        self._graph = _BoundGraph(self.database)

    def __getattr__(self, name):
        with self.database.lease() as store:
            value = getattr(store, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            with self.database.lease() as store:
                return getattr(store, name)(*args, **kwargs)
        return call

    @property
    def graph(self):
        return self._graph

    def _call(self, name, *args, **kwargs):
        return self.__getattr__(name)(*args, **kwargs)

    def get(self, *a, **k): return self._call('get', *a, **k)
    def replace(self, *a, **k): return self._call('replace', *a, **k)
    def clear(self, *a, **k): return self._call('clear', *a, **k)
    def get_artifact(self, *a, **k): return self._call('get_artifact', *a, **k)
    def replace_artifact(self, *a, **k): return self._call('replace_artifact', *a, **k)
    def stage_artifact(self, *a, **k): return self._call('stage_artifact', *a, **k)
    def publish_generation(self, *a, **k): return self._call('publish_generation', *a, **k)
    def discard_generation(self, *a, **k): return self._call('discard_generation', *a, **k)
    def next_projection_generation(self, *a, **k): return self._call('next_projection_generation', *a, **k)
    def open_publication_epoch(self, *a, **k): return self._call('open_publication_epoch', *a, **k)
    def revoke_publication_epoch(self, *a, **k): return self._call('revoke_publication_epoch', *a, **k)
    def fence_publication_epoch(self, *a, **k): return self._call('fence_publication_epoch', *a, **k)


class _BoundGraph:
    def __init__(self, database):
        self.database = database

    def __getattr__(self, name):
        def call(*args, **kwargs):
            with self.database.lease() as store:
                return getattr(store.graph, name)(*args, **kwargs)
        return call


class _AcknowledgedPipeline:
    def __init__(self, base, runner, account, signal, published, current_signal):
        self.base, self.runner, self.account = base, runner, account
        self.signal, self.published = signal, published
        self.current_signal = current_signal

    def __getattr__(self, name):
        return getattr(self.base, name)

    def publish_candidate(self, candidate):
        with self.runner.dependencies.gate:
            self.runner.authorize(self.account)
            if self.current_signal() != self.signal:
                raise ProjectionBuildCancelled()
            result = self.base.publish_candidate(candidate)
            self.runner.findings.acknowledge(self.account, self.signal[-1])
            with self.runner.findings.database.transaction() as db:
                current = db.execute('SELECT selection_epoch,admission_epoch,mode,notification_revision '
                    'FROM nlp_accounts WHERE account_ref=?', (account_ref(self.account),)).fetchone()
                if current is not None and tuple(current) == (*self.signal[:3], self.signal[-1]):
                    self.published[self.account] = self.signal
                    self.published.move_to_end(self.account)
                    while len(self.published) > MAX_ACCOUNTS:
                        self.published.popitem(last=False)
            return result


class FindingsRuntime:
    def __init__(self, source, pipeline, configuration, authorize):
        self.source, self.pipeline = source, pipeline
        self.configuration, self.authorize_account = configuration, authorize
        self._stop = threading.Event()
        self._wake = asyncio.Event()
        self._executor = _OwnedBoundedExecutor(max_workers=1, max_tasks=1,
            thread_name_prefix='analytics-findings')
        self._task = None
        self._future = None
        self._closing = None
        self._service = None
        self._store = None
        self._runner = None
        self._accounts = deque(maxlen=MAX_ACCOUNTS)
        self._published = OrderedDict()
        self._preparation_attempts = set()
        self._after = None
        self._failed = False
        self._joined = False
        self.failure = None

    def start(self, scheduler):
        if self._task is None and not self._stop.is_set():
            self._task = asyncio.create_task(self._run(scheduler), name='analytics-findings')

    def wake(self):
        self._wake.set()

    def begin_stop(self):
        self._stop.set()
        self._wake.set()

    def _authorize(self, account, bound):
        self.authorize_account(account)
        if self._stop.is_set():
            raise QuestionCancelled()
        with bound.database.lease():
            pass

    def _retire(self):
        if self._service is not None:
            if not self._service.close(0):
                return False
        self._clear_binding()
        return True

    def _clear_binding(self):
        if self._service is not None and dependency_service() is self._service:
            configure_dependency_service(None)
        self._service, self._runner, self._store = None, None, None
        self._accounts.clear()
        self._published.clear()
        self._preparation_attempts.clear()
        self._after = None

    def _bind(self):
        with self.pipeline.projections.lifecycle_store() as store:
            if store is None:
                return False
        if store is self._store:
            return not self._failed
        if not self._retire() or self._stop.is_set():
            return False
        if dependency_service() is not None:
            raise ValueError('findings_dependency_owner_conflict')
        bound = _BoundProjections(self.pipeline.projections, store)
        findings = FindingsStore(bound.database)
        self._store = store
        self._failed = True
        service = self.configuration.dependencies(self.source, findings)
        self._service = service
        if service.source is not self.source or service.findings is not findings:
            raise ValueError('findings_dependency_binding_invalid')
        bound.database.gate = service.gate
        service.start()
        configure_dependency_service(service)
        self._runner = FindingsRunner(self.source, bound,
            authorize=lambda account: self._authorize(account, bound),
            execution_ready=service.ready, adapter=None, dependencies=service,
            clock=self.configuration.clock, enrichment=self.pipeline.enrichment,
            label_resolver=self.configuration.label_resolver,
            cancellation_check=self._stop.is_set)
        self._failed = False
        self.failure = None
        return True

    def generation_pipeline(self, account):
        if self._stop.is_set():
            raise ProjectionCoordinatorClosed()
        runner = self._runner
        if runner is None or self._failed:
            return self.pipeline
        try:
            state = runner.findings.state(account)
        except (ValueError, ProjectionStorageUnavailable):
            return self.pipeline
        # Authorization is independent of dependency readiness.
        runner.authorize(account)
        profile = read_profile(state['profile_json'])
        ready = runner.dependencies.ready(profile)
        signal = self._signal(state, ready)
        base = runner.frozen_pipeline(account) if state['mode'] == 'enabled' and ready else self.pipeline
        def current_signal():
            current = runner.findings.state(account)
            return self._signal(current, runner.dependencies.ready(read_profile(current['profile_json'])))
        return _AcknowledgedPipeline(base, runner, account, signal, self._published, current_signal)

    @staticmethod
    def _signal(state, ready):
        return (state['selection_epoch'], state['admission_epoch'], state['mode'], ready,
                state['notification_revision'])

    def _turn(self):
        if self._stop.is_set() or not self._bind():
            return None
        runner = self._runner
        with runner.dependencies.gate:
            selections = runner.findings.selected_profiles()
            selected = {row['account_ref'] for row in selections}
            for account in tuple(self._published):
                if account_ref(account) not in selected:
                    self._published.pop(account, None)
        page = self.source.account_revision_page(after=self._after, limit=64)
        self._after = page[-1][0] if len(page) == 64 else None
        for account, _ in page:
            if account_ref(account) in selected and account not in self._accounts:
                self._accounts.append(account)
        self._accounts = deque((account for account in self._accounts
            if account_ref(account) in selected), maxlen=MAX_ACCOUNTS)
        if not self._accounts:
            return None
        account = self._accounts[0]
        self._accounts.rotate(-1)
        runner.authorize(account)
        if not self.source.account_exists(account):
            self._accounts.remove(account)
            return None
        if runner.findings.state(account)['mode'] == 'enabled':
            state = runner.findings.state(account)
            profile = read_profile(state['profile_json'])
            digest = profile.identity_digest()
            if (not runner.dependencies.ready(profile) and digest not in self._preparation_attempts
                    and len(self._preparation_attempts) < 64):
                self._preparation_attempts.add(digest)
                def cancelled():
                    runner.authorize(account)
                    current = runner.findings.state(account)
                    return (current['mode'] != 'enabled'
                        or current['selection_epoch'] != state['selection_epoch']
                        or current['admission_epoch'] != state['admission_epoch']
                        or read_profile(current['profile_json']).identity_digest() != digest)
                try:
                    runner.dependencies.prepare_installed(digest, cancellation_check=cancelled)
                except Exception:
                    runner.authorize(account)
                    self.failure = 'findings_preparation_unavailable'
            runner.process_one(account)
        state = runner.findings.state(account)
        ready = runner.dependencies.ready(read_profile(state['profile_json']))
        if state['notification_revision'] > 0 and (
                state['acknowledged_revision'] != state['notification_revision']
                or self._published.get(account) != self._signal(state, ready)):
            from app.analytics.identity import source_identity
            return account, source_identity(self.source, account).revision, state['notification_revision']
        return None

    async def _run(self, scheduler):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._future = self._executor.submit(self._turn)
                notice = await asyncio.shield(asyncio.wrap_future(self._future))
                if notice is not None and not self._stop.is_set():
                    await scheduler.request_findings(*notice)
            except Exception:
                if self.failure is None:
                    LOGGER.warning('analytics_findings_event reason_code=processing_unavailable')
                self.failure = 'findings_unavailable'
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._wake.wait(), self.configuration.interval)
                except TimeoutError:
                    pass

    def abort(self):
        self.begin_stop()
        if self._task is not None and not self._task.done():
            return False
        return self._finish_close(0)

    def _finish_close(self, timeout):
        if self._joined:
            return True
        if self._future is not None and not self._future.done():
            return False
        if self._service is None and self._closing is None:
            self._executor.begin_shutdown()
            self._executor.join()
            self._joined = True
            return True
        if self._closing is None:
            self._closing = self._executor.submit(lambda: self._retire_with_timeout(timeout))
        if not self._closing.done():
            return False
        try:
            drained = self._closing.result()
        except Exception:
            drained = False
        self._closing = None
        if not drained:
            return False
        self._executor.begin_shutdown()
        self._executor.join()
        self._joined = True
        return True

    def _retire_with_timeout(self, timeout):
        if self._service is not None and not self._service.close(timeout):
            return False
        self._clear_binding()
        return True

    async def close(self, timeout):
        self.begin_stop()
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if (self._task is None or self._task.done()) and self._finish_close(
                    max(0, deadline - time.monotonic())):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(.01, max(0, deadline-time.monotonic())))
