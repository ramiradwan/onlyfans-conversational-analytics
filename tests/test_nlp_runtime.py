"""Configured background findings through the ordinary analytics lifecycle."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.analytics import runtime
from app.analytics.errors import ProjectionCoordinatorClosed, ProjectionStorageUnavailable
from app.analytics.nlp_runtime import FindingsConfiguration
from app.analytics.identity import source_identity
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.models.insights import FindingsRequest
from app.security.local_sessions import build_runtime_policy
from app.security.runtime_policy import AuthContext
from app.services.nlp_dependencies import dependency_service
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture
from tests.test_nlp_dependencies import Environment

pytestmark = pytest.mark.ci_tier('integration')


async def eventually(predicate, timeout=15):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.02)


@pytest.mark.asyncio
async def test_configured_split_processing_publishes_through_runtime_and_reader(tmp_path):
    e = Environment(tmp_path, 'split')
    e.selected()
    assert e.service.close()
    original_open = e.engine.open

    def factory(source, findings):
        assert source is e.fixture.source
        e.findings = findings
        e.service = e.construct()
        return e.service

    # The provider returns invented findings for both independently bound tasks.
    @contextmanager
    def nonempty(*args, **kwargs):
        with original_open(*args, **kwargs) as session:
            analyze = session.analyze
            def result(request, cancelled, **bounds):
                response = json.loads(analyze(request, cancelled, **bounds))
                component = e.profile.component(request.component_id)
                for task, descriptor in zip(response['items'][0]['tasks'], component.tasks):
                    payload = task['payload']
                    payload.update(outcome='assessed_findings', findings=[
                        {'topic_id': descriptor.enabled_ids[0]} if descriptor.task == 'message_topics'
                        else {'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}}])
                return json.dumps(response).encode()
            session.analyze = result
            yield session
    e.engine.open = nonempty
    configuration = FindingsConfiguration(dependencies=factory, authorize=lambda account: None,
        clock=lambda: e.fixture.clock.now, label_resolver=None, interval=.02)
    try:
        owned = runtime.configure_default_analytics_runtime(e.fixture.source, backend='sqlite',
            projections_path=tmp_path/'analytics.sqlite3', canonical_path=tmp_path/'canonical.sqlite3',
            activation=e.fixture.repositories.projection_activation, findings=configuration)
        assert owned.pipeline.projections.database is None
        await runtime.start_default_analytics_runtime()
        publication_state = {}
        def published():
            nonlocal publication_state
            publication_state = e.findings.state(ACCOUNT)
            return (publication_state['completion_revision'] == 2 and
                publication_state['acknowledged_revision'] == publication_state['notification_revision'])
        await eventually(published)
        projection = owned.pipeline.projections.get(ACCOUNT)
        policy = build_runtime_policy(AuthContext('principal.synthetic', ACCOUNT, 'creator',
            platform_creator_id='platform.synthetic', session_id='session.synthetic'))
        response = await asyncio.to_thread(owned.questions.findings, policy, FindingsRequest(
            expected_generation=projection.projection_generation,
            expected_projection_digest=projection.projection_digest))
        assert {task.task for task in response.tasks} == {'message_topics', 'concept_mentions'}
        assert all(task.rows for task in response.tasks)
        assert all(task.complete_messages == 2 for task in response.tasks), {
            'revisions': {key: publication_state[key] for key in (
                'completion_revision', 'acknowledged_revision', 'notification_revision')},
            'tasks': [{key: getattr(task, key) for key in (
                'task', 'availability', 'eligible_messages', 'complete_messages',
                'pending_messages', 'failed_messages', 'partial_messages',
                'unsupported_messages', 'uncertain_messages')} for task in response.tasks],
        }
    finally:
        assert await runtime.shutdown_default_analytics_runtime(timeout=10)
        e.service.close()
        cleanup(e.fixture)


@asynccontextmanager
async def configured(tmp_path, *, authorize=lambda account: None, hook=None, setup=None):
    e = Environment(tmp_path, 'split')
    e.selected()
    assert e.service.close()
    if setup is not None:
        setup(e)
    original_open = e.engine.open
    e.calls = []

    @contextmanager
    def observed(*args, **kwargs):
        with original_open(*args, **kwargs) as session:
            analyze = session.analyze
            def execute(request, cancelled, **bounds):
                e.calls.append(request.component_id)
                if hook is not None:
                    hook(e, request, cancelled)
                return analyze(request, cancelled, **bounds)
            session.analyze = execute
            yield session
    e.engine.open = observed

    def factory(source, findings):
        assert source is e.fixture.source
        e.findings = findings
        e.service = e.construct()
        return e.service
    configuration = FindingsConfiguration(dependencies=factory, authorize=authorize,
        clock=lambda: e.fixture.clock.now, interval=.02)
    try:
        e.owned = runtime.configure_default_analytics_runtime(e.fixture.source, backend='sqlite',
            projections_path=tmp_path/'analytics.sqlite3', canonical_path=tmp_path/'canonical.sqlite3',
            activation=e.fixture.repositories.projection_activation, findings=configuration)
        await runtime.start_default_analytics_runtime()
        yield e
    finally:
        assert await runtime.shutdown_default_analytics_runtime(timeout=10)
        cleanup(e.fixture)


@pytest.mark.asyncio
async def test_default_runtime_owns_no_optional_worker(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        owned = runtime.configure_default_analytics_runtime(f.source, backend='sqlite',
            projections_path=tmp_path/'analytics.sqlite3', canonical_path=tmp_path/'canonical.sqlite3',
            activation=f.repositories.projection_activation)
        await runtime.start_default_analytics_runtime()
        await owned.scheduler.wait(ACCOUNT)
        assert owned.findings is None
        assert dependency_service() is None
        assert owned.pipeline.projections.get(ACCOUNT) is not None
    finally:
        assert await runtime.shutdown_default_analytics_runtime(timeout=10)
        cleanup(f)


@pytest.mark.asyncio
async def test_restart_redelivers_completed_notifications_without_reprocessing(tmp_path):
    async with configured(tmp_path) as e:
        await eventually(lambda: e.findings.state(ACCOUNT)['completion_revision'] == 2 and
            e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision'])
        before = list(e.calls)
        configuration = runtime._DEFAULT_CONFIGURATION.findings
        previous_service = e.service
        assert await runtime.shutdown_default_analytics_runtime(timeout=10)
        with e.fixture.stores.database.transaction() as db:
            db.execute('UPDATE nlp_accounts SET acknowledged_revision=0')
        e.owned = runtime.configure_default_analytics_runtime(e.fixture.source, backend='sqlite',
            projections_path=tmp_path/'analytics.sqlite3', canonical_path=tmp_path/'canonical.sqlite3',
            activation=e.fixture.repositories.projection_activation, findings=configuration)
        await runtime.start_default_analytics_runtime()
        await eventually(lambda: dependency_service() is e.service and e.service is not previous_service and
            e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision'] and
            e.owned.findings._published.get(ACCOUNT, (None,))[0] is not None)
        await e.owned.scheduler.wait(ACCOUNT)
        assert e.calls == before
        assert e.service.ready(e.profile)
        assert '+nlp.snapshot.v1' in e.owned.pipeline.projections.get(ACCOUNT).pipeline_revision


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['paused', 'withdrawn', 'removed'])
async def test_optional_mode_change_publishes_baseline_and_acknowledges(tmp_path, mode):
    async with configured(tmp_path) as e:
        await eventually(lambda: e.findings.state(ACCOUNT)['completion_revision'] == 2 and
            e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision'])
        e.findings.mode(ACCOUNT, mode)
        e.owned.findings.wake()
        await eventually(lambda: e.owned.findings._published.get(ACCOUNT, (None, None, None))[2] == mode and
            e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision'])
        await e.owned.scheduler.wait(ACCOUNT)
        assert '+nlp.snapshot.v1' not in e.owned.pipeline.projections.get(ACCOUNT).pipeline_revision
        before = list(e.calls)
        await asyncio.sleep(.12)
        assert e.calls == before
        assert e.owned.scheduler.state(ACCOUNT).reason_code is None


@pytest.mark.asyncio
async def test_shutdown_retains_blocked_owner_and_does_not_lock_projections(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def block(e, request, cancelled):
        entered.set()
        assert release.wait(8)
    try:
        async with configured(tmp_path, hook=block) as e:
            await eventually(entered.is_set)
            async with asyncio.timeout(2):
                assert await e.owned.scheduler.canonical_revision(ACCOUNT) == 1
            assert not await runtime.shutdown_default_analytics_runtime(timeout=.03)
            assert runtime._RUNTIMES[id(e.fixture.source)] is e.owned
            assert dependency_service() is e.service
            assert not e.owned.scheduler.closed
            previous_configuration = runtime._DEFAULT_CONFIGURATION
            previous_startup = runtime._STARTUP_TASK
            with pytest.raises(ProjectionCoordinatorClosed):
                runtime.configure_default_analytics_runtime(e.fixture.source, backend='sqlite',
                    projections_path=tmp_path/'analytics.sqlite3', canonical_path=tmp_path/'canonical.sqlite3',
                    activation=e.fixture.repositories.projection_activation,
                    findings=replace(previous_configuration.findings, interval=.03))
            assert runtime._DEFAULT_CONFIGURATION is previous_configuration
            assert runtime._STARTUP_TASK is previous_startup
            runtime.reset_analytics_runtimes()
            assert runtime._RUNTIMES[id(e.fixture.source)] is e.owned
            with pytest.raises(ProjectionCoordinatorClosed):
                runtime.configure_default_analytics_runtime(object(), backend='memory')
            release.set()
            assert await runtime.shutdown_default_analytics_runtime(timeout=5)
            assert len(e.calls) == 1
            with e.fixture.stores.database.read() as db:
                assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state='running'").fetchone()[0] == 0
            assert dependency_service() is None
            assert e.owned.findings._joined
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['authorization', 'selection', 'withdrawal'])
async def test_change_during_first_component_prevents_later_dispatch(tmp_path, change):
    entered, release = threading.Event(), threading.Event()
    allowed = [True]
    def authorize(account):
        if not allowed[0]:
            raise PermissionError('analysis_unavailable')
    def block(e, request, cancelled):
        entered.set()
        assert release.wait(8)
    try:
        async with configured(tmp_path, authorize=authorize, hook=block) as e:
            await eventually(entered.is_set)
            if change == 'authorization':
                allowed[0] = False
            elif change == 'selection':
                e.findings.select(ACCOUNT, e.profile.model_copy(update={'revision': 'changed'}))
            else:
                e.service.learn(e.catalog(sequence=2, withdrawals=(e.profile.identity_digest(),)))
            release.set()
            await eventually(lambda: e.owned.findings._future.done())
            e.owned.findings.begin_stop()
            assert await runtime.shutdown_default_analytics_runtime(timeout=5)
            assert len(e.calls) == 1
            with e.fixture.stores.database.read() as db:
                assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state='running'").fetchone()[0] == 0
    finally:
        release.set()


def test_account_pages_cover_registry_in_order_and_bound_queries(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        with f.repositories.database.transaction() as db:
            for i in range(70):
                db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES(?,?,?)',
                    (f'page-{i:03}', i, f.clock.now.isoformat()))
        collected, after = [], None
        while True:
            page = f.source.account_revision_page(after=after, limit=7)
            assert len(page) <= 7
            if not page:
                break
            collected.extend(page)
            after = page[-1][0]
        assert collected == f.source.account_revisions()
        assert f.source.account_revision_page(after=after, limit=64) == []
        assert f.source.account_revision_page(after='zz-end') == []
    finally:
        cleanup(f)


@pytest.mark.parametrize('arguments', [{'limit': True}, {'limit': 0}, {'limit': 65}, {'limit': 1.5},
    {'after': ''}, {'after': 1}, {'after': 'x'*1025}])
def test_account_pages_reject_invalid_arguments(tmp_path, arguments):
    f = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        with pytest.raises(ValueError, match='analytics_account_page_invalid'):
            f.source.account_revision_page(**arguments)
    finally:
        cleanup(f)


@pytest.mark.asyncio
async def test_replaced_store_invalidates_inference_and_bound_graph(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def block(e, request, cancelled):
        entered.set()
        assert release.wait(8)
    try:
        async with configured(tmp_path, hook=block) as e:
            await eventually(entered.is_set)
            owner = e.owned.pipeline.projections
            previous_runner = e.owned.findings._runner
            previous_service = e.service
            owner._mark_failed(previous_runner.projections.database.store, None)
            await e.owned.scheduler._ensure_projection_storage()
            with pytest.raises(ProjectionStorageUnavailable):
                previous_runner.projections.graph.partition_revision('unused')
            with pytest.raises(ProjectionStorageUnavailable):
                with previous_runner.findings.database.read():
                    pytest.fail('obsolete owner admitted')
            release.set()
            await eventually(lambda: dependency_service() is e.service and e.service is not previous_service)
            assert len(e.calls) == 1
            assert previous_service.stopping.is_set()
            assert e.owned.findings._runner.projections.database.store is not previous_runner.projections.database.store
    finally:
        release.set()


@pytest.mark.asyncio
async def test_lazy_findings_read_rejects_unbound_scope_and_replacement(tmp_path, monkeypatch):
    async with configured(tmp_path) as e:
        await eventually(lambda: e.findings.state(ACCOUNT)['completion_revision'] == 2 and
            e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision'])
        await e.owned.scheduler.wait(ACCOUNT)
        owner = e.owned.pipeline.projections
        budget = QuestionBudget(QuestionLimits(wall_clock_ms=5000))
        with owner.question_publications(ACCOUNT, budget) as publication:
            with pytest.raises(ValueError, match='publication_scope_binding_changed'):
                publication.findings(None)
            with pytest.raises(ValueError, match='publication_scope_binding_changed'):
                publication('other', source_identity(e.fixture.source, ACCOUNT), budget)
            snapshot, _, _ = publication(ACCOUNT, source_identity(e.fixture.source, ACCOUNT), budget)
            store = e.owned.findings._store
            def replaced(*args, **kwargs):
                owner._mark_failed(store, None)
                return 'obsolete'
            monkeypatch.setattr(store, 'read_findings_snapshot', replaced)
            with pytest.raises(ProjectionStorageUnavailable):
                publication.findings(snapshot)


@pytest.mark.parametrize('kind', ['before_access', 'once', 'exception', 'swallowed_exception'])
def test_preparation_cancellation_never_publishes_readiness(tmp_path, monkeypatch, kind):
    e = Environment(tmp_path, 'split')
    e.selected()
    e.service.available.clear()
    error = RuntimeError('cancel_callback_error')
    trigger = [kind == 'before_access']
    def cancelled():
        if trigger[0]:
            trigger[0] = False
            if kind in ('exception', 'swallowed_exception'):
                raise error
            return True
        return False
    if kind == 'before_access':
        monkeypatch.setattr(e.artifacts, 'reopen', lambda *a, **k: pytest.fail('artifact access after cancellation'))
    else:
        def prepared(profile, available, check):
            trigger[0] = True
            if kind == 'once':
                with pytest.raises(ValueError, match='nlp_cancelled'):
                    check()
            elif kind == 'swallowed_exception':
                with pytest.raises(RuntimeError) as swallowed:
                    check()
                assert swallowed.value is error
            else:
                check()
        monkeypatch.setattr(e.service, '_prepare', prepared)
    try:
        with pytest.raises(RuntimeError if kind in ('exception', 'swallowed_exception') else ValueError) as caught:
            e.service.prepare_installed(e.profile.identity_digest(), cancellation_check=cancelled)
        if kind in ('exception', 'swallowed_exception'):
            assert caught.value is error
        assert not e.service.ready(e.profile)
        assert not e.service.pins
    finally:
        assert e.service.close()
        cleanup(e.fixture)


@pytest.mark.asyncio
async def test_failed_preparation_is_not_repeated_and_baseline_remains_available(tmp_path):
    async with configured(tmp_path) as e:
        # The hook affects the lifecycle's preparation, after invented installation.
        e.engine.before_prepare = lambda: (_ for _ in ()).throw(ValueError('prepare_failed'))
        await eventually(lambda: e.owned.findings.failure == 'findings_preparation_unavailable')
        before = e.engine.prepares
        await asyncio.sleep(.12)
        assert e.engine.prepares == before
        assert not e.calls
        await e.owned.scheduler.wait(ACCOUNT)
        assert '+nlp.snapshot.v1' not in e.owned.pipeline.projections.get(ACCOUNT).pipeline_revision


def test_main_rejected_configuration_preserves_retained_configuration(monkeypatch):
    import app.main as main
    before = main._findings_configuration
    with pytest.raises(ValueError, match='findings_configuration_invalid'):
        main.configure_analytics_runtime(findings=object())
    assert main._findings_configuration is before
    def refused(*args, **kwargs):
        raise ProjectionCoordinatorClosed()
    monkeypatch.setattr(runtime, 'configure_default_analytics_runtime', refused)
    with pytest.raises(ProjectionCoordinatorClosed):
        main.configure_analytics_runtime(findings=object())
    assert main._findings_configuration is before


@pytest.mark.asyncio
async def test_inventory_and_preparation_final_check_use_consistent_lock_order(tmp_path, monkeypatch):
    async with configured(tmp_path) as e:
        await eventually(lambda: e.findings.state(ACCOUNT)['completion_revision'] == 2 and
            e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision'])
        await e.owned.scheduler.wait(ACCOUNT)
        runner = e.owned.findings._runner
        owner = e.owned.pipeline.projections
        preparing, inventory, resume, final = (threading.Event() for _ in range(4))

        class TimedLock:
            def __init__(self, lock, gate=False):
                self.lock, self.gate = lock, gate
            def __getattr__(self, name):
                return getattr(self.lock, name)
            def __enter__(self):
                if self.gate and final.is_set() and threading.current_thread().name.startswith('prepare'):
                    acquired = self.lock.acquire(timeout=.05)
                    resume.set()
                    assert acquired or self.lock.acquire(timeout=2)
                else:
                    assert self.lock.acquire(timeout=2)
                return self
            def __exit__(self, *error):
                self.lock.release()

        gate = TimedLock(e.service.gate, gate=True)
        monkeypatch.setattr(e.service, 'gate', gate)
        monkeypatch.setattr(runner.projections.database, 'gate', gate)
        monkeypatch.setattr(owner, '_lock', TimedLock(owner._lock))
        original_ready = e.service.ready
        checks = [0]
        def ready(profile):
            if threading.current_thread().name.startswith('inventory'):
                checks[0] += 1
                if checks[0] == 2:
                    inventory.set()
                    assert resume.wait(3)
            return original_ready(profile)
        monkeypatch.setattr(e.service, 'ready', ready)
        def prepared(profile, available, check):
            preparing.set()
            assert inventory.wait(3)
            final.set()
        monkeypatch.setattr(e.service, '_prepare', prepared)

        def run():
            try:
                with ThreadPoolExecutor(1, thread_name_prefix='prepare') as preparation, \
                        ThreadPoolExecutor(1, thread_name_prefix='inventory') as scanning:
                    warmup = preparation.submit(e.service.prepare_installed, e.profile.identity_digest(),
                        cancellation_check=lambda: runner.authorize(ACCOUNT) or False)
                    assert preparing.wait(3)
                    scan = scanning.submit(runner._admit_inventory, ACCOUNT)
                    scan.result(timeout=6)
                    warmup.result(timeout=6)
            finally:
                resume.set()
                inventory.set()
        await asyncio.to_thread(run)
        assert checks[0] >= 2


@pytest.mark.asyncio
async def test_selected_accounts_rotate_between_bounded_jobs(tmp_path):
    local = threading.local()
    seen = []
    other = 'second-account'
    def authorize(account):
        local.account = account
    def hook(e, request, cancelled):
        seen.append(local.account)
    def setup(e):
        with e.fixture.repositories.database.transaction() as db:
            for table in ('account_heads', 'account_chats', 'account_messages'):
                columns = [row[1] for row in db.execute(f'PRAGMA table_info({table})')]
                copied = ','.join('?' if name == 'creator_account_id' else name for name in columns)
                db.execute(f'INSERT INTO {table}({",".join(columns)}) SELECT {copied} FROM {table} '
                    'WHERE creator_account_id=?', (other, ACCOUNT))
        e.findings.select(other, e.profile)
    async with configured(tmp_path, authorize=authorize, hook=hook, setup=setup) as e:
        await eventually(lambda: all(e.findings.state(account)['completion_revision'] == 2
            for account in (ACCOUNT, other)), timeout=30)
        assert seen == [other, other, ACCOUNT, ACCOUNT, other, other, ACCOUNT, ACCOUNT]


@pytest.mark.asyncio
async def test_readiness_change_republishes_baseline_and_saved_findings(tmp_path):
    async with configured(tmp_path) as e:
        await eventually(lambda: e.findings.state(ACCOUNT)['completion_revision'] == 2 and
            e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision'])
        await e.owned.scheduler.wait(ACCOUNT)
        before = list(e.calls)
        e.engine.present = False
        e.owned.findings.wake()
        await eventually(lambda: e.owned.findings._published.get(ACCOUNT, (None,)*4)[3] is False)
        await e.owned.scheduler.wait(ACCOUNT)
        assert '+nlp.snapshot.v1' not in e.owned.pipeline.projections.get(ACCOUNT).pipeline_revision
        await asyncio.sleep(.12)
        assert e.owned.findings._published[ACCOUNT][3] is False
        assert e.findings.state(ACCOUNT)['acknowledged_revision'] == e.findings.state(ACCOUNT)['notification_revision']
        e.engine.present = True
        e.owned.findings.wake()
        await eventually(lambda: e.owned.findings._published.get(ACCOUNT, (None,)*4)[3] is True)
        await e.owned.scheduler.wait(ACCOUNT)
        assert '+nlp.snapshot.v1' in e.owned.pipeline.projections.get(ACCOUNT).pipeline_revision
        assert e.calls == before
