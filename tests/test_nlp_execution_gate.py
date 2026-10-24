"""Optional execution refusal preserves baseline and saved-result operations."""

from unittest.mock import Mock
import hashlib

import pytest

from app.analytics import runtime
from app.analytics.nlp_runtime import FindingsConfiguration, FindingsRuntime
from app.core.config import settings
from app.nlp import v2
from app.security.analysis_authorization import require_current_analysis_run
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture
from tests.test_analysis_authorization import _policy
from tests.test_nlp_dependencies import Environment
from tests.test_nlp_processing_publication import runner
from tests.test_nlp_read_projection import ready  # noqa: F401

pytestmark = pytest.mark.ci_tier('integration')


@pytest.mark.parametrize('environment', ['production', 'staging', 'development', 'local'])
def test_default_composition_rejects_findings_before_runtime_construction(
        tmp_path, monkeypatch, environment):
    import app.main as main

    starter = _policy()
    require_current_analysis_run(starter)
    assert starter.commercial_authority.licensed_major_version == 3
    dependencies = Mock(side_effect=AssertionError('dependencies constructed'))
    authorize = Mock(side_effect=lambda account: require_current_analysis_run(starter))
    configuration = FindingsConfiguration(dependencies=dependencies, authorize=authorize)
    constructor = Mock(side_effect=AssertionError('runtime constructed'))
    before = runtime._DEFAULT_CONFIGURATION
    retained = main._findings_configuration
    monkeypatch.setattr(settings, 'environment', environment)
    monkeypatch.setattr(settings, 'identity_binding_source', 'verified_grants')
    monkeypatch.setattr(settings, 'canonical_persistence_backend', 'sqlite')
    monkeypatch.setattr(runtime, 'FindingsRuntime', constructor)

    with pytest.raises(ValueError, match='^findings_execution_unavailable$'):
        runtime.configure_default_analytics_runtime(object(), backend='sqlite', findings=configuration)
    with pytest.raises(ValueError, match='^findings_execution_unavailable$'):
        main.configure_analytics_runtime(findings=configuration)
    assert runtime._DEFAULT_CONFIGURATION is before
    assert main._findings_configuration is retained
    constructor.assert_not_called()
    dependencies.assert_not_called()
    authorize.assert_not_called()


def test_direct_runtime_construction_is_refused_before_executor_allocation(monkeypatch):
    constructor = Mock(side_effect=AssertionError('executor allocated'))
    monkeypatch.setattr(settings, 'environment', 'production')
    monkeypatch.setattr('app.analytics.nlp_runtime._OwnedBoundedExecutor', constructor)
    configuration = FindingsConfiguration(dependencies=lambda *args: None, authorize=lambda account: None)
    with pytest.raises(ValueError, match='^findings_execution_unavailable$'):
        FindingsRuntime(object(), object(), configuration, lambda account: None)
    constructor.assert_not_called()


@pytest.mark.parametrize('entry', ['process_one', '_process_one'])
def test_direct_processing_cannot_dispatch_with_starter_authority(tmp_path, monkeypatch, entry):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        starter = _policy()
        require_current_analysis_run(starter)
        worker, profile, adapter = runner(fixture,
            authorize=lambda account: require_current_analysis_run(starter))
        assert worker.execution_ready(profile)
        monkeypatch.setattr(settings, 'environment', 'production')
        with pytest.raises(ValueError, match='^findings_execution_unavailable$'):
            getattr(worker, entry)(ACCOUNT)
        assert adapter.requests == []
        assert worker.findings.state(ACCOUNT)['completion_revision'] == 0
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('entry', ['retain', 'prepare_installed', '_prepare'])
def test_ready_dependency_service_cannot_open_engine_in_production(tmp_path, monkeypatch, entry):
    environment = Environment(tmp_path, 'split')
    try:
        environment.selected()
        assert environment.service.ready(environment.profile)
        opened = Mock(side_effect=AssertionError('engine opened'))
        monkeypatch.setattr(environment.engine, 'open', opened)
        monkeypatch.setattr(settings, 'environment', 'production')
        assert environment.service.ready(environment.profile)
        with pytest.raises(ValueError, match='^findings_execution_unavailable$'):
            if entry == 'retain':
                with environment.service.retain(environment.profile):
                    pytest.fail('retained executable worker')
            elif entry == 'prepare_installed':
                environment.service.prepare_installed(environment.profile.identity_digest())
            else:
                environment.service._prepare(environment.profile,
                    environment.service.available[environment.profile.identity_digest()], lambda: None)
        opened.assert_not_called()
        assert environment.service.status(ACCOUNT)['configured']
    finally:
        assert environment.service.close()
        cleanup(environment.fixture)


def test_retained_test_executor_cannot_dispatch_after_production_transition(tmp_path, monkeypatch):
    environment = Environment(tmp_path, 'split')
    try:
        environment.selected()
        component = environment.profile.components[0]
        text = 'Synthetic message'
        request = v2.WorkerRequest(request_id='request.synthetic',
            profile_sha256=environment.profile.identity_digest(), component_id=component.component_id,
            component_sha256=component.identity_digest(), tasks=tuple(t.task for t in component.tasks),
            deadline_ms=1000, items=(v2.WorkerInput(request_item_id='message', text=text,
                text_sha256=hashlib.sha256(text.encode()).hexdigest()),))
        with environment.service.retain(environment.profile) as execute:
            opened = Mock(side_effect=AssertionError('engine opened'))
            monkeypatch.setattr(environment.engine, 'open', opened)
            monkeypatch.setattr(settings, 'environment', 'production')
            with pytest.raises(ValueError, match='^findings_execution_unavailable$'):
                execute(request, lambda: False)
            opened.assert_not_called()
    finally:
        assert environment.service.close()
        cleanup(environment.fixture)


def test_test_composition_keeps_explicit_adapter_execution(tmp_path):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        worker, _, adapter = runner(fixture)
        assert worker.process_one(ACCOUNT)
        assert adapter.requests
    finally:
        cleanup(fixture)


def test_production_baseline_processing_needs_no_optional_worker(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        monkeypatch.setattr(settings, 'environment', 'production')
        result = fixture.pipeline.rebuild_account(ACCOUNT)
        assert result.changed
        assert '+nlp.snapshot.v1' not in result.artifact.projection.pipeline_revision
        assert fixture.stores.database.active_generation(ACCOUNT) is not None
    finally:
        cleanup(fixture)


def test_saved_reads_and_inference_free_publication_work_in_production(ready, monkeypatch):
    from app.analytics.nlp_navigation import NavigationRegistry
    from app.models.nlp_literals import LiteralFindingsRequest
    from tests.test_nlp_navigation_read import descriptor, resources, plan as grouped_plan
    from tests.test_nlp_observation_read import plan as observation_plan

    monkeypatch.setattr(settings, 'environment', 'production')
    before = list(ready.runner.adapter.requests)
    flat = ready.resources.findings(ready.policy, ready.request)
    literals = ready.resources.literal_findings(ready.policy,
        LiteralFindingsRequest(**ready.request.model_dump()))
    observations = ready.resources.observation_links(ready.policy, observation_plan(ready))
    with resources(ready, NavigationRegistry((descriptor(ready.profile),))) as owner:
        grouped = owner.grouped_findings(ready.policy, grouped_plan(ready))
    assert flat.tasks == grouped.tasks == observations.tasks
    assert literals.availability == observations.availability == 'available'
    evidence = literals.evidence_page.items[0].reference
    assert ready.resources.resolve(ready.policy, evidence).text
    with ready.fixture.stores.database.read() as db:
        from app.analytics.nlp_findings_store import FrozenFindings
        from app.analytics.opaque_refs import account_ref
        snapshot = db.execute('SELECT snapshot_id FROM nlp_generation_refs WHERE generation_id=?',
            (ready.fixture.stores.database.active_generation(ACCOUNT).generation_id,)).fetchone()[0]
    binding = FrozenFindings(account_ref(ACCOUNT), snapshot)
    ready.runner.publish_literals(ACCOUNT, binding)
    assert ready.runner.backfill_literals(ACCOUNT, binding, maximum=1) == 0
    assert ready.runner.adapter.requests == before
