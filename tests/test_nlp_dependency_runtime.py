"""Dependency binding, retirement and projection lease ownership."""
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from app.analytics.errors import ProjectionStorageUnavailable
from app.analytics.nlp_dependency_runtime import DependencyRuntime
from app.analytics.nlp_runtime import FindingsConfiguration, FindingsRuntime
from app.services import nlp_dependencies

pytestmark = pytest.mark.ci_tier('integration')


class Projections:
    def __init__(self):
        self.store = self.new_store('first')

    @staticmethod
    def new_store(name):
        return SimpleNamespace(database=object(), graph=SimpleNamespace(read=lambda: name))

    @contextmanager
    def lifecycle_store(self):
        yield self.store


class Service:
    def __init__(self, source, findings, events, *, gate=None):
        self.source, self.findings = source, findings
        self.gate = gate if gate is not None else threading.RLock()
        self.events = events
        self.drained = True
        self.start_error = None
        self.starts = 0

    def start(self):
        self.events.append('start')
        self.starts += 1
        if self.start_error is not None:
            raise self.start_error

    def close(self, timeout):
        self.events.append(('close', timeout, self.drained))
        return self.drained


@pytest.fixture(autouse=True)
def isolated_registration(monkeypatch):
    assert nlp_dependencies.dependency_service() is None
    monkeypatch.setattr(nlp_dependencies, '_service', None)
    yield
    nlp_dependencies.configure_dependency_service(None)


def setup_owner(*, gate=None):
    projections, source, events, services = Projections(), object(), [], []

    def factory(actual_source, findings):
        service = Service(actual_source, findings, events, gate=gate)
        services.append(service)
        return service

    return DependencyRuntime(source, projections, factory, gate=gate), projections, events, services


def test_one_service_registration_and_callback_order():
    owner, projections, events, services = setup_owner()

    def attach(binding):
        assert nlp_dependencies.dependency_service() is None
        assert binding.service is services[0]
        events.append('attach')

    binding = owner.bind(before_start=attach)
    assert owner.bind() is binding
    assert len(services) == services[0].starts == 1
    assert events == ['attach', 'start']
    assert nlp_dependencies.dependency_service() is services[0]
    other = DependencyRuntime(owner.source, projections, lambda *a: pytest.fail('duplicate factory'))
    with pytest.raises(ValueError, match='findings_dependency_owner_conflict'):
        other.bind()
    assert nlp_dependencies.dependency_service() is services[0]

    def detach():
        assert nlp_dependencies.dependency_service() is services[0]
        assert events[-1] == ('close', 2, True)
        events.append('detach')

    assert owner.close(2, before_clear=detach)
    assert events[-1] == 'detach'
    assert owner.store is owner.service is owner.binding is None
    assert nlp_dependencies.dependency_service() is None


def test_store_replacement_waits_for_drain_and_invalidates_old_leases():
    owner, projections, events, services = setup_owner()
    previous = owner.bind()
    assert previous.projections.graph.read() == 'first'
    services[0].drained = False
    projections.store = projections.new_store('second')
    detached = []
    assert owner.bind(before_clear=lambda: detached.append(True)) is None
    assert len(services) == 1 and not detached
    assert owner.binding is previous
    assert nlp_dependencies.dependency_service() is services[0]
    with pytest.raises(ProjectionStorageUnavailable):
        previous.projections.graph.read()
    services[0].drained = True
    current = owner.bind(before_clear=lambda: detached.append(True))
    assert detached == [True] and len(services) == 2
    assert current.projections.graph.read() == 'second'
    assert nlp_dependencies.dependency_service() is services[1]
    assert owner.close()


def test_failed_start_is_sticky_and_retained_until_drained():
    owner, projections, events, services = setup_owner()
    original = owner.factory

    def failing(*args):
        service = original(*args)
        service.start_error = RuntimeError('start failed')
        service.drained = False
        return service

    owner.factory = failing
    with pytest.raises(RuntimeError, match='start failed'):
        owner.bind()
    failed = owner.binding
    assert owner.bind() is None and services[0].starts == 1
    projections.store = projections.new_store('replacement')
    detached = []
    assert owner.bind(before_clear=lambda: detached.append(True)) is None
    assert owner.binding is failed and not detached and len(services) == 1
    assert nlp_dependencies.dependency_service() is None
    assert not owner.close(before_clear=lambda: detached.append(True))
    services[0].drained = True
    assert owner.close(before_clear=lambda: detached.append(True))
    assert detached == [True] and owner.binding is None


@pytest.mark.parametrize('invalid', ['source', 'findings', 'gate'])
def test_invalid_binding_retains_constructed_service_for_close(invalid):
    gate = threading.RLock()
    owner, projections, events, services = setup_owner(gate=gate)
    original = owner.factory

    def factory(*args):
        service = original(*args)
        if invalid == 'gate':
            service.gate = threading.RLock()
        else:
            setattr(service, invalid, object())
        return service

    owner.factory = factory
    with pytest.raises(ValueError, match=('gate_mismatch' if invalid == 'gate' else 'binding_invalid')):
        owner.bind()
    assert owner.service is services[0] and services[0].starts == 0
    assert owner.bind() is None and len(services) == 1
    assert owner.close() and owner.service is None


def test_close_error_retains_registration_and_blocks_replacement():
    owner, projections, events, services = setup_owner()
    previous = owner.bind()
    close = services[0].close

    def failed_close(timeout):
        raise RuntimeError('drain failed')

    services[0].close = failed_close
    projections.store = projections.new_store('replacement')
    detached = []
    with pytest.raises(RuntimeError, match='drain failed'):
        owner.bind(before_clear=lambda: detached.append(True))
    assert owner.binding is previous and len(services) == 1 and not detached
    assert nlp_dependencies.dependency_service() is services[0]
    services[0].close = close
    assert owner.close(before_clear=lambda: detached.append(True))
    assert detached == [True]


def test_foreign_registration_is_not_cleared(monkeypatch):
    owner, _, _, services = setup_owner()
    owner.bind()
    foreign = object()
    monkeypatch.setattr(nlp_dependencies, '_service', foreign)
    assert owner.close()
    assert nlp_dependencies.dependency_service() is foreign


@pytest.mark.asyncio
async def test_findings_shutdown_clears_failed_factory_binding_without_service():
    projections, source, attempts = Projections(), object(), []

    def failing(*args):
        attempts.append(True)
        raise RuntimeError('factory failed')

    configuration = FindingsConfiguration(dependencies=failing, authorize=lambda account: None)
    runtime = FindingsRuntime(source, SimpleNamespace(projections=projections), configuration, lambda account: None)
    with pytest.raises(RuntimeError, match='factory failed'):
        runtime._bind()
    assert runtime._store is projections.store and runtime._service is None
    assert not runtime._bind() and attempts == [True]
    assert await runtime.close(2)
    assert runtime._store is runtime._service is None
    assert runtime._joined


def test_before_start_error_retains_unstarted_service_and_does_not_retry():
    owner, _, events, services = setup_owner()
    callbacks = []

    def attach(binding):
        callbacks.append(binding)
        raise RuntimeError('attach failed')

    with pytest.raises(RuntimeError, match='attach failed'):
        owner.bind(before_start=attach)
    assert services[0].starts == 0 and owner.service is services[0]
    assert owner.bind(before_start=attach) is None
    assert len(callbacks) == len(services) == 1
    assert nlp_dependencies.dependency_service() is None
    assert owner.close()


def test_before_clear_error_preserves_registration_for_close_retry():
    owner, _, events, services = setup_owner()
    binding = owner.bind()

    def detach():
        assert events[-1] == ('close', 0, True)
        raise RuntimeError('detach failed')

    with pytest.raises(RuntimeError, match='detach failed'):
        owner.close(before_clear=detach)
    assert owner.binding is binding and owner.service is services[0]
    assert nlp_dependencies.dependency_service() is services[0]
    cleared = []
    assert owner.close(before_clear=lambda: cleared.append(True))
    assert cleared == [True] and owner.store is None
    assert nlp_dependencies.dependency_service() is None


@pytest.mark.asyncio
async def test_retirement_callback_stop_prevents_replacement_factory():
    projections, services = Projections(), []
    source = object()

    def factory(actual, findings):
        service = Service(actual, findings, [])
        services.append(service)
        return service

    configuration = FindingsConfiguration(dependencies=factory, authorize=lambda account: None)
    runtime = FindingsRuntime(source, SimpleNamespace(projections=projections), configuration, lambda account: None)
    runtime._dependencies.bind()
    projections.store = projections.new_store('replacement')
    runtime._clear_binding = runtime.begin_stop
    assert not runtime._bind()
    assert len(services) == 1 and runtime._store is None
    assert nlp_dependencies.dependency_service() is None
    assert await runtime.close(2)
