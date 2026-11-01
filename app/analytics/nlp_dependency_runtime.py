"""Explicit dependency-service ownership for one bound projection store."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import threading

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
            with self.projections.lifecycle_store() as store:
                if store is None:
                    return None
            if store is self._store:
                return None if self._failed else self._binding
            if not self.close(0, before_clear=before_clear):
                return None
            if dependency_service() is not None:
                raise ValueError('findings_dependency_owner_conflict')
            bound = _BoundProjections(self.projections, store)
            findings = FindingsStore(bound.database)
            self._store, self._failed = store, True
            service = self.factory(self.source, findings)
            self._service = service
            self._binding = DependencyBinding(store, bound, findings, service)
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
