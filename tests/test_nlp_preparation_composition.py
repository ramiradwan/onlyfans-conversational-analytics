"""Independent preparation owner lifecycle without an execution owner."""
from contextlib import contextmanager
import asyncio
import threading

import pytest

from app.analytics.nlp_dependency_runtime import DependencyConfiguration
from app.analytics.errors import ProjectionStorageUnavailable
from app.services import nlp_dependencies
from tests.test_nlp_dependency_runtime import setup_owner

pytestmark = pytest.mark.ci_tier('integration')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(nlp_dependencies, '_service', None)
    yield
    nlp_dependencies.configure_dependency_service(None)


async def until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(.01)
    pytest.fail('owner did not settle')


@pytest.mark.asyncio
async def test_late_projection_replacement_and_independent_drain():
    owner, projections, events, services = setup_owner()
    first = projections.store
    projections.store = None
    owner.start()
    await asyncio.sleep(.03)
    assert not services
    projections.store = first
    await until(lambda: len(services) == 1)
    assert services[0].starts == 1
    projections.store = projections.new_store('second')
    await until(lambda: len(services) == 2)
    assert nlp_dependencies.dependency_service() is services[1]
    assert await owner.shutdown(1)
    assert nlp_dependencies.dependency_service() is None
    assert await owner.shutdown(0)
    assert not any(t.name.startswith('nlp-dependency-binding') for t in threading.enumerate())


@pytest.mark.asyncio
async def test_blocked_factory_keeps_owned_work_until_drain_and_does_not_block_loop():
    owner, projections, events, services = setup_owner()
    factory = owner.factory
    entered, release = threading.Event(), threading.Event()
    def blocked(*args):
        entered.set()
        assert release.wait(2)
        return factory(*args)
    owner.factory = blocked
    owner.start()
    await until(entered.is_set)
    try:
        assert not await owner.shutdown(.02)
        assert not owner.abort()
        assert not owner._future.done()
    finally:
        release.set()
    assert await owner.shutdown(1)
    assert len(services) == 1 and services[0].starts == 0
    assert nlp_dependencies.dependency_service() is None


def test_independent_configuration_rejects_memory_backend_and_dual_owner():
    from app.analytics.runtime import configure_default_analytics_runtime
    configuration = DependencyConfiguration(lambda *a: pytest.fail('factory called'))
    with pytest.raises(ValueError, match='dependency_configuration_invalid'):
        configure_default_analytics_runtime(object(), backend='memory', dependencies=configuration)


def test_factory_has_no_side_effects_until_projection_binding(monkeypatch, tmp_path):
    from app.services import nlp_setup
    from app.nlp.profiles import BASELINE_RESOURCE_POLICY
    from types import SimpleNamespace
    admission = SimpleNamespace(resource_policy=BASELINE_RESOURCE_POLICY)
    monkeypatch.setattr(nlp_setup, 'load_preparation_release', lambda: admission)
    configuration = nlp_setup.preparation_configuration(lambda: pytest.fail('database opened'), tmp_path)
    assert type(configuration) is DependencyConfiguration
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_analytics_composes_independent_owner_and_shutdown_without_findings(monkeypatch):
    from types import SimpleNamespace
    from app.analytics import runtime
    owner, projections, events, services = setup_owner()
    pipeline = SimpleNamespace(projections=projections, _retention_clock=lambda: 0)
    class Scheduler:
        closed = False
        async def start(self, **kwargs):
            pass
        async def close(self, **kwargs):
            self.closed = True
            return True
        def abort(self):
            self.closed = True
    monkeypatch.setattr(runtime, '_RUNTIMES', {})
    monkeypatch.setattr(runtime, '_DEFAULT_CONFIGURATION', None)
    monkeypatch.setattr(runtime, '_STARTUP_TASK', None)
    monkeypatch.setattr(runtime, '_build_pipeline', lambda *a, **k: pipeline)
    monkeypatch.setattr(runtime, 'QuestionResources', lambda *a, **k: None)
    monkeypatch.setattr(runtime, 'InProcessProjectionScheduler', lambda *a, **k: Scheduler())
    configured = runtime.configure_default_analytics_runtime(owner.source, backend='sqlite',
        dependencies=DependencyConfiguration(owner.factory))
    assert configured.findings is None and configured.dependencies is not None
    assert not services
    await runtime.start_default_analytics_runtime()
    await until(lambda: len(services) == 1)
    assert services[0].starts == 1 and not hasattr(services[0], 'execution_owner')
    assert await runtime.shutdown_default_analytics_runtime(timeout=1)
    assert nlp_dependencies.dependency_service() is None


@pytest.mark.asyncio
async def test_cancelled_binding_task_still_drains_retained_service():
    owner, projections, events, services = setup_owner()
    owner.start()
    await until(lambda: len(services) == 1)
    owner._task.cancel()
    try:
        await owner._task
    except asyncio.CancelledError:
        pass
    assert await owner.shutdown(1)
    assert nlp_dependencies.dependency_service() is None
    assert not any(t.name.startswith('nlp-dependency-binding') for t in threading.enumerate())


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_projection_disappearance_drains_previous_owner_before_rebinding(unavailable):
    owner, projections, events, services = setup_owner()
    owner.start()
    await until(lambda: len(services) == 1)
    lifecycle = projections.lifecycle_store
    held = []
    @contextmanager
    def tracked():
        if unavailable and projections.store is None:
            raise ProjectionStorageUnavailable()
        with lifecycle() as store:
            held.append(True)
            try:
                yield store
            finally:
                held.pop()
    projections.lifecycle_store = tracked
    close = services[0].close
    def checked_close(timeout):
        assert not held
        return close(timeout)
    services[0].close = checked_close
    services[0].drained = False
    projections.store = None
    await until(lambda: any(isinstance(e, tuple) and e[0] == 'close' and not e[2] for e in events))
    assert owner.service is services[0] and nlp_dependencies.dependency_service() is services[0]
    services[0].drained = True
    await until(lambda: owner.service is None)
    assert nlp_dependencies.dependency_service() is None
    projections.store = projections.new_store('replacement')
    await until(lambda: len(services) == 2)
    assert await owner.shutdown(1)


@pytest.mark.asyncio
async def test_main_preserves_foreign_service_and_refuses_total_drain(monkeypatch):
    from types import SimpleNamespace
    import app.main as main
    from app.services import retention_maintenance, signing_parameters
    from app.core import lifecycle_receipts
    calls = []
    foreign = SimpleNamespace(start=lambda: pytest.fail('foreign start'),
                              close=lambda timeout: pytest.fail('foreign close'))
    async def settled(*args, **kwargs):
        return True
    async def stop(*args, **kwargs):
        pass
    monkeypatch.setattr(main, '_dependency_configuration', object())
    monkeypatch.setattr(main, '_grant_refresh', None)
    monkeypatch.setattr(nlp_dependencies, '_service', foreign)
    monkeypatch.setattr(retention_maintenance, 'shutdown_default_retention_maintenance', settled)
    monkeypatch.setattr(signing_parameters, 'stop_signing_parameters', stop)
    monkeypatch.setattr(main.analytics_runtime, 'shutdown_default_analytics_runtime', settled)
    monkeypatch.setattr(main.transport_manager, 'stop', stop)
    monkeypatch.setattr(main.broadcast, 'disconnect', stop)
    monkeypatch.setattr(lifecycle_receipts, 'finish', lambda drained: calls.append(drained))
    await main.start_optional_dependencies()
    await main.shutdown_event()
    assert nlp_dependencies.dependency_service() is foreign
    assert calls == [False] and not main.app.state.runtime_shutdown_drained
