"""Synthetic process sessions through retained dependencies and canonical fences."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analytics.nlp_processing import FindingsRunner, execution_slot
from app.nlp import v2
from tests.continuous_analytics_fixture import ACCOUNT, cleanup
from tests.test_nlp_dependencies import Environment, Engine
from tests.test_nlp_canonical_execution import execution, authority_basis, rows

pytestmark = pytest.mark.ci_tier('integration')


class ObservedEngine(Engine):
    def __init__(self):
        super().__init__()
        self.trace = []
        self.active = False
        self.fail_close = False
        self.hook = None

    @contextmanager
    def open(self, profile, available, check, *, deadline=None):
        self.retained_check = check
        self.trace.append(('open', None, deadline))
        self.active = True
        owner = self
        try:
            with super().open(profile, available, check, deadline=deadline) as base:
                class Session:
                    def __getattr__(self, name):
                        def call(request, *args, **kwargs):
                            owner.trace.append((name, getattr(request, 'component_id', None), kwargs.get('deadline')))
                            if owner.hook:
                                owner.hook(name, request, args, kwargs)
                            return getattr(base, name)(request, *args, **kwargs)
                        return call
                yield Session()
        finally:
            self.trace.append(('close', None, None))
            self.active = self.fail_close
            if self.fail_close:
                raise ValueError('nlp_worker_not_drained')

    def drained(self, profile):
        return not self.active

    def close(self, timeout=5):
        return not self.active


@pytest.fixture
def retained(tmp_path):
    e = Environment(tmp_path/'dependencies', 'split')
    e.engine = ObservedEngine()
    e.service.engine = e.engine
    e.selected()
    e.engine.trace.clear()
    e.engine.events.clear()
    try:
        yield e
    finally:
        e.engine.active = e.engine.fail_close = False
        assert e.service.close()
        cleanup(e.fixture)


def request(e, component=0, *, deadline_ms=120_000, text='Invented message'):
    c = e.profile.components[component]
    return v2.WorkerRequest(request_id=uuid4().hex, profile_sha256=e.profile.identity_digest(),
        component_id=c.component_id, component_sha256=c.identity_digest(), tasks=tuple(t.task for t in c.tasks),
        deadline_ms=deadline_ms, items=(v2.WorkerInput(request_item_id='message', text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest()),))


def names(e):
    return [event[0] for event in e.engine.trace]


def test_empty_and_global_busy_do_not_open(retained):
    e = retained
    with e.service.retain(e.profile):
        assert not e.engine.trace
    runner = FindingsRunner(e.fixture.source,e.fixture.stores.projections,authorize=lambda _:None,
        execution_ready=e.service.ready,dependencies=e.service,adapter=None,clock=lambda:e.fixture.clock.now)
    with execution_slot() as acquired:
        assert acquired
        assert runner.process_one(ACCOUNT) is False
    assert not e.engine.trace


def test_reuses_prepared_component_and_reopens_neither_artifacts_nor_worker(retained, monkeypatch):
    e = retained
    reopened = []
    original = e.artifacts.reopen
    def reopen(*args, **kwargs):
        reopened.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(e.artifacts,'reopen',reopen)
    with e.service.retain(e.profile) as execute:
        execute(request(e),lambda:False)
        execute(request(e,text='Another invented message'),lambda:False)
        assert names(e) == ['open','handshake','prepare','analyze','analyze']
    assert names(e) == ['open','handshake','prepare','analyze','analyze','unload','close']
    assert len(reopened) == 1
    with pytest.raises(ValueError,match='nlp_execution_scope_closed'):
        execute(request(e),lambda:False)


def test_component_change_unloads_before_prepare_in_same_worker(retained):
    e = retained
    with e.service.retain(e.profile) as execute:
        execute(request(e,0),lambda:False)
        execute(request(e,1),lambda:False)
    assert names(e) == ['open','handshake','prepare','analyze','unload','prepare','analyze','unload','close']
    assert e.engine.events == [(operation,c.identity_digest()) for c in e.profile.components
        for operation in ('prepare','unload')]


def test_later_requests_only_shorten_first_deadline_and_rebind_callback(retained):
    e = retained
    clock = SimpleNamespace(now=100.)
    e.service.monotonic = lambda:clock.now
    first_live = [True]
    def first():
        assert first_live[0], 'old source callback retained'
        return False
    with e.service.retain(e.profile) as execute:
        execute(request(e,0,deadline_ms=10_000),first)
        first_live[0] = False
        clock.now = 102.
        execute(request(e,1,deadline_ms=3000),lambda:False)
        clock.now = 103.
        execute(request(e,1,deadline_ms=120_000),lambda:False)
    analyses = [event for event in e.engine.trace if event[0]=='analyze']
    assert [event[2] for event in analyses] == [110.,105.,105.]
    assert e.engine.trace[0][2] == 110.


def test_scope_deadline_expires_without_reopening(retained):
    e = retained
    clock = SimpleNamespace(now=10.)
    e.service.monotonic = lambda:clock.now
    with e.service.retain(e.profile) as execute:
        execute(request(e,deadline_ms=1000),lambda:False)
        clock.now = 11.
        with pytest.raises(ValueError,match='nlp_deadline'):
            execute(request(e),lambda:False)
        with pytest.raises(ValueError,match='nlp_execution_scope_broken'):
            execute(request(e),lambda:False)
    assert names(e).count('open') == 1 and names(e)[-2:] == ['unload','close']


@pytest.mark.parametrize('phase',['handshake','prepare','analyze'])
def test_phase_failure_forbids_reuse_and_closes(retained,phase):
    e = retained
    def fail(name,request,args,kwargs):
        if name==phase:
            raise ValueError('nlp_transport_failure')
    e.engine.hook = fail
    with e.service.retain(e.profile) as execute:
        with pytest.raises(ValueError,match='nlp_transport_failure'):
            execute(request(e),lambda:False)
        e.engine.hook = None
        with pytest.raises(ValueError,match='nlp_execution_scope_broken'):
            execute(request(e),lambda:False)
    assert names(e).count('open') == 1 and names(e)[-1] == 'close'


def test_invalid_response_breaks_scope(retained,monkeypatch):
    from tests.test_nlp_processing_publication import ConformanceAdapter
    monkeypatch.setattr(ConformanceAdapter,'__call__',lambda *args:b'{}')
    e = retained
    with e.service.retain(e.profile) as execute:
        with pytest.raises(ValueError):
            execute(request(e),lambda:False)
        with pytest.raises(ValueError,match='nlp_execution_scope_broken'):
            execute(request(e),lambda:False)
    assert names(e).count('open') == 1 and names(e)[-1] == 'close'


@pytest.mark.parametrize('exit_reason',['body','cancel','stop'])
def test_cleanup_ignores_cancelled_source_and_stopping_service(retained,exit_reason):
    e = retained
    cancelled = [False]
    with pytest.raises(RuntimeError,match='body'):
        with e.service.retain(e.profile) as execute:
            execute(request(e),lambda:cancelled[0])
            cancelled[0] = exit_reason=='cancel'
            if exit_reason=='stop':
                e.service.stopping.set()
            raise RuntimeError('body')
    assert names(e)[-2:] == ['unload','close']
    assert not e.engine.active
    assert not [op for op in e.persistence.operations() if op['kind']=='pin' and op['state']=='running']


def test_failed_join_keeps_pin_until_drained(retained):
    e = retained
    e.engine.fail_close = True
    with pytest.raises(ValueError,match='nlp_worker_not_drained'):
        with e.service.retain(e.profile) as execute:
            execute(request(e),lambda:False)
    assert e.engine.active
    assert any(op['kind']=='pin' and op['state']=='running' for op in e.persistence.operations())
    assert not e.service.close(0)


@pytest.mark.parametrize('replay',['sealed','staged'])
def test_canonical_runner_without_adapter_override_and_replay_no_open(retained,execution,monkeypatch,replay):
    e = retained
    x = execution('split',modify=lambda _:e.profile)
    e.service.source, e.service.findings = x.f.source, x.runner.findings
    x.runner.adapter = None
    x.runner.dependencies = e.service
    x.runner.execution_ready = e.service.ready
    original_seal = x.owner.seal
    if replay=='staged':
        def stage_then_crash(work, frame=None):
            with x.owner._fence('commit') as policy,x.repository.database.transaction() as db:
                x.repository.stage_member_in(db,work.attempt,0,frame,policy=policy)
            raise RuntimeError('after stage')
        monkeypatch.setattr(x.owner,'seal',stage_then_crash)
        with pytest.raises(RuntimeError,match='after stage'):
            x.runner.process_one(ACCOUNT)
        monkeypatch.setattr(x.owner,'seal',original_seal)
        x.f.clock.now += timedelta(seconds=121)
    else:
        assert x.runner.process_one(ACCOUNT)
        assert rows(x,'knowledge_jobs')[0]['state']=='committed'
        raw = bytes(rows(x,'knowledge_members')[0]['document'])
        with x.f.stores.database.read() as db:
            assert bytes(db.execute('SELECT document FROM nlp_records').fetchone()[0])==raw
            assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0]==1
        x.runner.findings.retry(ACCOUNT,rows(x,'knowledge_sources')[0]['source_ref'])
    opened = names(e).count('open')
    assert opened == 1
    x.runner.execution_ready = lambda _:False
    assert x.runner.process_one(ACCOUNT)
    assert names(e).count('open') == opened
    assert rows(x,'knowledge_jobs')[0]['state']=='committed'


def test_scope_exit_waits_for_admitted_dispatch_and_preserves_source_check(retained):
    e = retained
    entered, release, closing, finished = (threading.Event() for _ in range(4))
    errors, checks = [], []
    manager = e.service.retain(e.profile)
    execute = manager.__enter__()

    def cancelled():
        checks.append(True)
        return False

    def hook(name, *_):
        if name == 'analyze':
            entered.set()
            assert release.wait(5)
            before = len(checks)
            e.engine.retained_check()
            assert len(checks) == before + 1

    def run():
        try:
            execute(request(e), cancelled)
        except BaseException as error:
            errors.append(error)

    def close():
        closing.set()
        try:
            manager.__exit__(None, None, None)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    e.engine.hook = hook
    worker = threading.Thread(target=run)
    closer = threading.Thread(target=close)
    worker.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert closing.wait(5)
        assert not finished.wait(.1)
        assert 'unload' not in names(e) and 'close' not in names(e)
        assert e.service.pins
    finally:
        release.set()
        worker.join(5)
        if closer.ident is not None:
            closer.join(5)
        else:
            manager.__exit__(None, None, None)
    assert not worker.is_alive() and not closer.is_alive()
    assert not errors
    assert names(e)[-2:] == ['unload', 'close']
    assert not e.service.pins
    with pytest.raises(ValueError, match='nlp_execution_scope_closed'):
        execute(request(e), cancelled)
