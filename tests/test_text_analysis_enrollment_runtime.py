"""Current canonical enrollment through the existing background runtime."""
import asyncio
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, contextmanager
import hashlib
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analytics import runtime
from app.analytics.nlp_runtime import FindingsConfiguration, FindingsRuntime, _AcknowledgedPipeline
from app.analytics.licensed_pipeline import LicensedAnalyticsPipeline
from app.analytics.errors import ProjectionBuildCancelled, ProjectionUnavailable
from app.analytics.nlp_processing import FindingsRunner
from app.core.config import settings
from app.models.text_analysis_enrollment import EnrollmentBindingSuperseded
from app.security.analysis_authorization import clear_analysis_policies
from app.security.runtime_policy import authorized_account, RuntimeAuthorizationDenied
from app.services.text_analysis_execution import execution_service
from tests.continuous_analytics_fixture import ACCOUNT
from tests.text_analysis_execution_fixture import make_execution_fixture, close_execution_fixture, command

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


async def eventually(predicate,timeout=30):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


def observe_failure(findings, error):
    if (type(error) is ProjectionUnavailable and error.availability == 'building'
            and error.reason_code is None):
        findings.observed_transients.append(error)
    else:
        findings.observed_failures.append(error)


@asynccontextmanager
async def configured_execution_runtime(x,path):
    assert x.dependencies.close()
    def baseline(account):
        policy=x.identity.owner.rebuild(x.identity.identity)
        authorized_account(policy,account)
    configuration=FindingsConfiguration(dependencies=x.construct_dependencies,authorize=baseline,
        execution=x.configuration,clock=lambda:x.f.clock.now,interval=.02)
    with runtime._RUNTIME_LOCK:
        previous_configuration=runtime._DEFAULT_CONFIGURATION
        owned=runtime.configure_default_analytics_runtime(x.source,backend='sqlite',projections_path=path,
            canonical_database=x.repository.database,activation=x.f.repositories.projection_activation,
            findings=configuration)
        owned_configuration=runtime._DEFAULT_CONFIGURATION
    try:
        owned.findings.observed_failures=[]
        owned.findings.observed_transients=[]
        original=owned.findings._turn
        def observed():
            try:
                return original()
            except Exception as error:
                observe_failure(owned.findings,error)
                raise
        owned.findings._turn=observed
        request=owned.scheduler.request_findings
        async def observed_request(*args):
            try:
                return await request(*args)
            except Exception as error:
                observe_failure(owned.findings,error)
                raise
        owned.scheduler.request_findings=observed_request
        generation=owned.scheduler.generation_pipeline
        def observed_generation(account):
            try:
                return generation(account)
            except Exception as error:
                observe_failure(owned.findings,error)
                raise
        owned.scheduler.generation_pipeline=observed_generation
        submit=owned.findings._executor.submit
        def observed_submit(*args,**kwargs):
            try:
                return submit(*args,**kwargs)
            except Exception as error:
                observe_failure(owned.findings,error)
                raise
        owned.findings._executor.submit=observed_submit
        await runtime.start_default_analytics_runtime()
        await eventually(lambda: execution_service() is not None)
        assert runtime.analytics_runtime().findings is owned.findings
        assert execution_service().dependencies is owned.findings._service
        yield owned,execution_service()
    finally:
        with runtime._RUNTIME_LOCK:
            assert runtime._DEFAULT_CONFIGURATION is owned_configuration, 'Execution fixture configuration owner changed'
            assert runtime._RUNTIMES.get(id(x.source)) is owned, 'Execution fixture runtime owner changed'
        assert await runtime.shutdown_default_analytics_runtime(timeout=15)
        with runtime._RUNTIME_LOCK:
            assert runtime._DEFAULT_CONFIGURATION is owned_configuration, 'Execution fixture configuration owner changed'
            assert execution_service() is None
            runtime._DEFAULT_CONFIGURATION=previous_configuration


def published(owned,service):
    if owned.findings.observed_failures:
        raise owned.findings.observed_failures[0]
    try:
        state=service.dependencies.findings.state(ACCOUNT)
        return (state['completion_revision'] >= 2 and state['acknowledged_revision'] == state['notification_revision']
                and owned.pipeline.projections.get(ACCOUNT) is not None)
    except ValueError:
        return False


def canonical(x):
    with x.repository.database.read() as db:
        return [tuple(row) for row in db.execute('SELECT job_id,contribution_id FROM knowledge_contributions ORDER BY job_id')]


def verified_grants(monkeypatch,x):
    monkeypatch.setattr(settings,'identity_binding_source','verified_grants')
    monkeypatch.setattr(settings,'auth_database_path',x.auth.database.path)
    clear_analysis_policies()


@pytest.mark.parametrize('error', [
    RuntimeError('unexpected'), ValueError('unexpected'),
    ProjectionUnavailable(availability='unavailable'),
    ProjectionUnavailable(availability='error'),
    ProjectionUnavailable(availability='building',reason_code='unexpected'),
])
def test_runtime_observer_preserves_unexpected_failures(error):
    findings=SimpleNamespace(observed_failures=[],observed_transients=[])
    observe_failure(findings,error)
    assert findings.observed_failures == [error]
    assert not findings.observed_transients


def queued_enrollment_runtime(error):
    owned = object.__new__(FindingsRuntime)
    old, current = SimpleNamespace(creator_account_id=ACCOUNT), SimpleNamespace(creator_account_id=ACCOUNT)
    polled, bound = [], []
    def locators(**kwargs):
        polled.append(kwargs)
        return [old if len(polled) == 1 else current]
    def bind(locator):
        bound.append(locator)
        if locator is old:
            raise error
        return SimpleNamespace()
    owned._execution_service = SimpleNamespace(current_locators=locators, bind_current=bind)
    owned._locator_page, owned._after_account = deque(), ''
    owned._enrollments = OrderedDict()
    owned.source = SimpleNamespace(account_exists=lambda account: False)
    return owned, old, current, polled, bound


def test_superseded_queued_locator_does_not_block_current_poll():
    owned, old, current, polled, bound = queued_enrollment_runtime(EnrollmentBindingSuperseded())
    assert owned._enrollment_turn() is None
    assert not owned._locator_page
    assert owned._enrollment_turn() is None
    assert len(bound) == 2 and bound[0] is old and bound[1] is current
    assert polled == [dict(after_account='', limit=64)] * 2


@pytest.mark.parametrize('error', [
    ValueError('unexpected'), ValueError('text_analysis_locator_stale'),
    RuntimeAuthorizationDenied('text_analysis_identity_unavailable'),
])
def test_enrollment_poll_preserves_unrelated_failures(error):
    owned, _, _, _, _ = queued_enrollment_runtime(error)
    with pytest.raises(type(error)) as caught:
        owned._enrollment_turn()
    assert caught.value is error


@pytest.mark.parametrize('error', [
    EnrollmentBindingSuperseded(), ValueError('unexpected'), ValueError('text_analysis_locator_stale'),
    RuntimeAuthorizationDenied('text_analysis_identity_unavailable'),
])
def test_superseded_binding_cancels_held_pipeline_without_hiding_other_failures(error):
    owned = object.__new__(FindingsRuntime)
    gate, calls, leases = threading.RLock(), [], []
    @contextmanager
    def lease():
        leases.append(True)
        yield
    def authorize(binding, account):
        assert binding is entry.binding and account == ACCOUNT
        raise error
    runner = SimpleNamespace(dependencies=SimpleNamespace(gate=gate),
        projections=SimpleNamespace(database=SimpleNamespace(lease=lease)),
        findings=SimpleNamespace(state=lambda account: {}))
    entry = SimpleNamespace(binding=object(), runner=runner)
    runner.authorize = lambda account: owned._authorize_enrollment(entry, account)
    owned._stop, owned._gate, owned._service = threading.Event(), gate, None
    owned._runner, owned._failed = None, False
    owned._execution_service = SimpleNamespace(authorize_binding=authorize)
    owned._enrollments = {ACCOUNT: entry}
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('obsolete_pipeline_called')
    base = SimpleNamespace(prepare_questions=forbidden, build_candidate=forbidden, publish_candidate=forbidden)
    held = _AcknowledgedPipeline(base, runner, ACCOUNT, (), {}, lambda: ())
    stale = isinstance(error, EnrollmentBindingSuperseded)
    for call in (lambda: held.prepare_questions(ACCOUNT), lambda: held.build_candidate(ACCOUNT),
                 lambda: held.publish_candidate(object())):
        with pytest.raises(ProjectionBuildCancelled if stale else type(error)) as caught:
            call()
        if not stale:
            assert caught.value is error
    with pytest.raises(ProjectionUnavailable if stale else type(error)) as caught:
        owned.generation_pipeline(ACCOUNT)
    if stale:
        assert caught.value.availability == 'building' and caught.value.reason_code is None
    else:
        assert caught.value is error
    assert not calls and not leases


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['joint','split'])
async def test_canonical_source_invalidation_retries_before_publication(tmp_path,kind,monkeypatch):
    x=make_execution_fixture(tmp_path,kind)
    verified_grants(monkeypatch,x)
    active=threading.local()
    invalidated=[]
    rejected=[]
    original_reconcile=FindingsRunner.reconcile
    original_scope=x.source.open_question_scope

    def reconcile(runner,*args,**kwargs):
        active.reconciling=True
        try:
            return original_reconcile(runner,*args,**kwargs)
        finally:
            active.reconciling=False

    @contextmanager
    def source_scope(*args,**kwargs):
        injected=False
        try:
            with original_scope(*args,**kwargs) as scope:
                yield scope
                if getattr(active,'reconciling',False) and not invalidated:
                    assert not canonical(x)
                    with x.repository.database.transaction() as db:
                        db.execute("UPDATE account_messages SET text=text || ' updated',content_hash='updated-source' "
                                   'WHERE creator_account_id=? AND message_id=?',(ACCOUNT,'m-0-0'))
                        db.execute('UPDATE account_heads SET canonical_revision=canonical_revision+1 '
                                   'WHERE creator_account_id=?',(ACCOUNT,))
                    invalidated.append(scope.identity)
                    injected=True
        except ProjectionUnavailable as error:
            if injected:
                rejected.append(error)
            raise

    monkeypatch.setattr(FindingsRunner,'reconcile',reconcile)
    monkeypatch.setattr(x.source,'open_question_scope',source_scope)
    try:
        first=x.service.command(x.http_policy(),command(x))
        reads=x.source.full_reads
        async with configured_execution_runtime(x,tmp_path/'retry-runtime.sqlite3') as (owned,service):
            await eventually(lambda: published(owned,service))
            assert len(invalidated) == len(rejected) == 1
            assert rejected[0] in owned.findings.observed_transients
            assert not owned.findings.observed_failures
            current=x.source.read_identity(ACCOUNT)
            projection=owned.pipeline.projections.get(ACCOUNT)
            assert current.revision == invalidated[0].revision+1
            assert current.content_digest != invalidated[0].content_digest
            assert projection.source_revision == current.revision
            assert projection.canonical_content_digest == current.content_digest
            assert len(canonical(x)) == 2
            assert service.read_current(x.http_policy()).enrollment_id == first.current.enrollment_id
            assert len(owned.findings._enrollments) == 1
            assert service.dependencies is owned.findings._service
            assert service.dependencies.findings.state(ACCOUNT)['mode'] == 'enabled'
        assert x.source.full_reads == reads
    finally:
        close_execution_fixture(x)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['joint','split'])
async def test_before_first_child_projection_loss_discovers_canonical_enrollment_and_publishes(tmp_path,kind,monkeypatch):
    x=make_execution_fixture(tmp_path,kind)
    verified_grants(monkeypatch,x)
    try:
        first=x.service.command(x.http_policy(),command(x))
        assert not canonical(x)
        reads=x.source.full_reads
        async with configured_execution_runtime(x,tmp_path/'new-runtime.sqlite3') as (owned,service):
            assert type(owned.pipeline) is LicensedAnalyticsPipeline
            try:
                await eventually(lambda: published(owned,service))
            except TimeoutError:
                stacks=[]
                for frame in sys._current_frames().values():
                    calls=[]
                    while frame is not None:
                        if '/app/' in frame.f_code.co_filename.replace('\\','/'):
                            calls.append((frame.f_code.co_name,frame.f_lineno,frame.f_code.co_filename))
                        frame=frame.f_back
                    if calls:
                        stacks.append(calls)
                state=service.dependencies.findings.state(ACCOUNT)
                raise AssertionError(dict(state={key:state[key] for key in ('mode','completion_revision','notification_revision','acknowledged_revision')},
                    failure=owned.findings.failure,children=canonical(x),cached=len(owned.findings._enrollments),stacks=stacks)) from None
            assert len(canonical(x)) == 2
            assert service.read_current(x.http_policy()).enrollment_id == first.current.enrollment_id
            assert len(owned.findings._enrollments) <= 16
            assert owned.findings._executor is not None
            assert service.dependencies.findings.state(ACCOUNT)['mode'] == 'enabled'
        assert x.source.full_reads == reads
    finally:
        close_execution_fixture(x)


@pytest.mark.asyncio
async def test_paused_projection_loss_replays_without_profile_cache_engine_or_module(tmp_path,monkeypatch):
    x=make_execution_fixture(tmp_path)
    verified_grants(monkeypatch,x)
    try:
        x.service.command(x.http_policy(),command(x))
        async with configured_execution_runtime(x,tmp_path/'first-runtime.sqlite3') as (owned,service):
            await eventually(lambda: published(owned,service))
            current=service.read_current(x.http_policy())
            service.command(x.http_policy(),command(x,'pause',current=current))
            saved=canonical(x)
            with x.repository.database.read() as db:
                originals=[hashlib.sha256(bytes(r[0])).hexdigest() for r in db.execute('SELECT document FROM knowledge_members ORDER BY job_id')]
        x.authority.revoke(x.reference.reference_id)
        with x.auth.database.transaction() as db:
            db.execute('DELETE FROM nlp_dependency_profiles')
        def absent(*args,**kwargs):
            pytest.fail('Retained replay opened an Engine')
        monkeypatch.setattr(x.environment.engine,'open',absent)
        async with configured_execution_runtime(x,tmp_path/'paused-runtime.sqlite3') as (owned,service):
            await eventually(lambda: published(owned,service))
            assert canonical(x) == saved
            assert service.dependencies.findings.state(ACCOUNT)['mode'] == 'paused'
            with service.dependencies.findings.database.read() as db:
                derived=sorted(hashlib.sha256(bytes(r[0])).hexdigest() for r in db.execute('SELECT document FROM nlp_records'))
                assert derived == sorted(originals)
                assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0] == 2
    finally:
        close_execution_fixture(x)


@pytest.mark.asyncio
async def test_canonical_owner_is_installed_before_delayed_legacy_selection_restart(tmp_path):
    x=make_execution_fixture(tmp_path)
    try:
        x.service.command(x.http_policy(),command(x))
        x.service.command(x.http_policy(),command(x,'pause'))
        findings=x.dependencies.findings
        epoch=findings.state(ACCOUNT)['selection_epoch']
        key=uuid4().hex
        x.dependencies.persistence.operation(dict(id=key,kind='select',state='pending',account=ACCOUNT,
            source_token=x.source.erasure_source_identity(ACCOUNT),expected=findings.selection_token(ACCOUNT),
            profile=x.profile.identity_digest()))
        path=x.f.stores.database.path
        x.f.stores.projections.close()
        async with configured_execution_runtime(x,path) as (_,service):
            operation=next(op for op in service.dependencies.persistence.operations() if op['id'] == key)
            assert operation['state'] == 'refused'
            state=service.dependencies.findings.state(ACCOUNT)
            assert state['mode'] == 'paused' and state['selection_epoch'] == epoch
    finally:
        close_execution_fixture(x)


@pytest.mark.asyncio
async def test_enrollment_without_retained_record_never_authorizes_starter(tmp_path,monkeypatch):
    x=make_execution_fixture(tmp_path)
    verified_grants(monkeypatch,x)
    try:
        x.service.command(x.http_policy(),command(x))
        x.service.command(x.http_policy(),command(x,'pause'))
        async with configured_execution_runtime(x,tmp_path/'unanchored.sqlite3') as (owned,service):
            await eventually(lambda: ACCOUNT in owned.findings._enrollments)
            selected=owned.findings.generation_pipeline(ACCOUNT)
            assert type(owned.pipeline) is LicensedAnalyticsPipeline
            assert selected.base is owned.pipeline
            with pytest.raises(RuntimeAuthorizationDenied):
                selected.build_candidate(ACCOUNT)
            assert not canonical(x)
    finally:
        close_execution_fixture(x)
