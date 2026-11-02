"""Explicit dependency-service ownership for one bound projection store."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import threading
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from app.analytics.errors import ProjectionStorageUnavailable
from app.analytics.nlp_findings_store import FindingsStore
from app.services.nlp_dependencies import dependency_service, configure_dependency_service


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

    def bind_contribution_origins(self, *args):
        with self.lease() as current:
            current.database.bind_contribution_origins(*args)


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


@dataclass(frozen=True)
class DependencyConfiguration:
    factory: object


@dataclass(frozen=True)
class DependencyBinding:
    store: object
    projections: object
    findings: FindingsStore
    service: object


class DependencyRuntime:
    def __init__(self, source, projections, factory, *, gate=None):
        self.source, self.projections, self.factory = source, projections, factory
        self.gate = gate
        self._lock = threading.RLock()
        self._store = self._service = self._binding = None
        self._failed = False
        self._task = None
        self._executor = self._future = self._closing_future = None
        self._stopping = False
        self.failure = None
        self._drained = False

    def start(self):
        if self._task is None and not self._stopping:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nlp-dependency-binding')
            self._task = asyncio.create_task(self._run(), name='nlp-dependency-binding')

    async def _run(self):
        while not self._stopping:
            try:
                self._future = self._executor.submit(self.bind)
                await asyncio.shield(asyncio.wrap_future(self._future))
            except Exception:
                self.failure = 'nlp_dependency_unavailable'
            if not self._stopping:
                await asyncio.sleep(.25)

    def abort(self):
        if self._drained:
            return True
        self._stopping = True
        if self._task is not None and not self._task.done():
            return False
        if self._future is not None and not self._future.done():
            return False
        if self._closing_future is not None and not self._closing_future.done():
            return False
        drained = self.close(0)
        if drained:
            if self._executor is not None:
                self._executor.shutdown(wait=True)
            self._drained = True
        return drained

    async def shutdown(self, timeout):
        if self._drained:
            return True
        self._stopping = True
        deadline = time.monotonic() + max(0, timeout)
        try:
            if self._task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._task), max(0, deadline-time.monotonic()))
                except asyncio.CancelledError:
                    if not self._task.cancelled() or asyncio.current_task().cancelling():
                        raise
            if self._future is not None and not self._future.done():
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._future)),
                                       max(0, deadline-time.monotonic()))
            if self._executor is None:
                return self.close(0)
            if self._closing_future is None or self._closing_future.done():
                self._closing_future = self._executor.submit(self.close, max(0, deadline-time.monotonic()))
            drained = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self._closing_future)),
                                            max(0, deadline-time.monotonic()))
            if drained:
                self._executor.shutdown(wait=True)
                self._drained = True
            return drained
        except TimeoutError:
            return False

    @property
    def store(self):
        return self._store

    @property
    def service(self):
        return self._service

    @property
    def binding(self):
        return self._binding

    def bind(self, *, before_start=None, before_clear=None):
        with self._lock:
            if self._stopping:
                return None
            try:
                with self.projections.lifecycle_store() as current:
                    store = current
            except ProjectionStorageUnavailable:
                store = None
            if store is None:
                self.close(0, before_clear=before_clear)
                return None
            if store is self._store:
                return None if self._failed else self._binding
            if not self.close(0, before_clear=before_clear):
                return None
            if self._stopping:
                return None
            if dependency_service() is not None:
                raise ValueError('findings_dependency_owner_conflict')
            bound = _BoundProjections(self.projections, store)
            findings = FindingsStore(bound.database)
            self._store, self._failed = store, True
            service = self.factory(self.source, findings)
            self._service = service
            self._binding = DependencyBinding(store, bound, findings, service)
            if self._stopping:
                return None
            if service.source is not self.source or service.findings is not findings:
                raise ValueError('findings_dependency_binding_invalid')
            bound.database.gate = service.gate
            if self.gate is not None and service.gate is not self.gate:
                raise ValueError('findings_execution_gate_mismatch')
            if before_start is not None:
                before_start(self._binding)
            service.start()
            configure_dependency_service(service)
            self._failed = False
            return self._binding

    def close(self, timeout=0, *, before_clear=None):
        with self._lock:
            if self._service is not None and not self._service.close(timeout):
                return False
            if before_clear is not None:
                before_clear()
            if self._service is not None and dependency_service() is self._service:
                configure_dependency_service(None)
            self._store = self._service = self._binding = None
            self._failed = False
            return True
