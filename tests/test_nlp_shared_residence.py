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
def retained(tmp_path,request):
    kind = getattr(request,'param','split')
    e = Environment(tmp_path/'dependencies', 'joint' if kind=='mentions' else kind)
    e.engine = ObservedEngine()
    e.service.engine = e.engine
    e.selected()
    if kind=='mentions':
        import json
        from app.nlp.profiles import canonical_bytes,read_profile
        from app.nlp.profile_authentication import SUBSET_PROFILE_JWS_TYPE
        from tests.test_nlp_profile_authentication import sign
        document = e.profile.model_dump(mode='json')
        document.update(schema_version='nlp-profile.v3',revision='mentions')
        document['bindings'] = [b for b in document['bindings'] if b['task']=='concept_mentions']
        component = document['components'][0]
        component['tasks'] = [task for task in component['tasks'] if task['task']=='concept_mentions']
        component['executed_labels'] = [label for label in component['executed_labels'] if label['task']=='concept_mentions']
        e.profile = read_profile(json.dumps(document))
        e.approved.add(e.profile.identity_digest())
        e.objects['profiles/'+e.profile.identity_digest()] = sign(canonical_bytes(e.profile),typ=SUBSET_PROFILE_JWS_TYPE)
        e.service.learn(e.catalog(sequence=2))
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


@pytest.mark.parametrize('phase',['cancelled','remaining','request'])
def test_licensed_fallback_does_not_catch_preflight_errors(retained,monkeypatch,phase):
    from app.analytics import nlp_processing
    e = retained
    calls,checks = [],[]
    def preflight():
        checks.append(True)
        if len(checks)==1:
            raise ValueError('synthetic_preflight_refusal')
        return False
    cancelled = preflight if phase=='cancelled' else lambda:False
    remaining = preflight if phase=='remaining' else lambda:120000
    if phase=='request':
        monkeypatch.setattr(v2,'validate_request',lambda *args:preflight())
    with pytest.raises(ValueError,match='synthetic_preflight_refusal'):
        nlp_processing.infer_source(e.profile,SimpleNamespace(text='Original invented message'),
            lambda *args:calls.append(args),cancelled=cancelled,remaining_ms=remaining)
    assert not calls and len(checks)==1




@pytest.fixture
def turn_sources(retained,execution):
    from tests.continuous_analytics_fixture import insert_message,advance
    e = retained
    x = execution('joint' if len(e.profile.components)==1 else 'split',modify=lambda _:e.profile)
    with x.f.repositories.database.transaction() as db:
        insert_message(db,'chat-0','second',x.f.clock.now-timedelta(days=2),1,text='Second invented photo')
        advance(db)
    e.service.source,e.service.findings = x.f.source,x.runner.findings
    x.runner.adapter,x.runner.dependencies,x.runner.execution_ready = None,e.service,e.service.ready
    x.runner.reconcile(ACCOUNT)
    return e,x


def saved_records(x):
    with x.f.stores.database.read() as db:
        return [v2.read_record(bytes(row[0])) for row in db.execute('SELECT document FROM nlp_records')]


def observe_claims(x,monkeypatch):
    claims = []
    original = x.runner.findings.claim
    def claim(*args,**kwargs):
        if claims:
            assert len(saved_records(x))==1
        value = original(*args,**kwargs)
        if value is not None:
            claims.append(value)
        return value
    monkeypatch.setattr(x.runner.findings,'claim',claim)
    return claims


def test_turn_reuses_boundary_component_and_commits_before_second_claim(turn_sources,monkeypatch):
    e,x = turn_sources
    claims = observe_claims(x,monkeypatch)
    calls = []
    e.engine.hook = lambda name,request,*_:calls.append(request) if name=='analyze' else None
    assert x.runner.process_batch(ACCOUNT)
    mention = next(c.component_id for c in x.profile.components if c.tasks[0].task=='concept_mentions')
    topic = next(c.component_id for c in x.profile.components if c.tasks[0].task=='message_topics')
    assert [r.component_id for r in calls]==[mention,topic,topic,mention]
    assert names(e).count('open')==1 and names(e).count('prepare')==3
    assert len(claims)==2 and claims[0].owner != claims[1].owner
    assert claims[1].expires_at <= claims[0].expires_at
    records = saved_records(x)
    assert len(records)==2 and len({record.source.source_ref for record in records})==2
    assert {record.tasks[0].result.text_sha256 for record in records}=={call.items[0].text_sha256 for call in calls}
    assert all(len({task.result.text_sha256 for task in record.tasks})==1 for record in records)
    assert all([task.result.payload.task for task in record.tasks]==[b.task for b in x.profile.bindings] for record in records)
    assert all(row['state']=='committed' for row in rows(x,'knowledge_jobs'))
    assert all(row['ordinal']==0 for row in rows(x,'knowledge_sources'))
    with x.f.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0]==2


@pytest.mark.parametrize('failure_at',[2,3,4])
def test_turn_transport_failure_preserves_only_complete_source(turn_sources,monkeypatch,failure_at):
    e,x = turn_sources
    claims = observe_claims(x,monkeypatch)
    calls = []
    def fail(name,request,*_):
        if name=='analyze':
            calls.append(request)
            if len(calls)==failure_at:
                raise ValueError('nlp_transport_failure')
        elif name=='unload' and len(calls)==failure_at:
            assert x.runner.findings.valid_claim(claims[-1])
    e.engine.hook = fail
    with pytest.raises(ValueError,match='nlp_transport_failure'):
        x.runner.process_batch(ACCOUNT)
    records = saved_records(x)
    assert len(records)==(0 if failure_at==2 else 1)
    assert len(claims)==(1 if failure_at==2 else 2)
    assert len(calls)==failure_at and names(e).count('open')==1 and names(e)[-1]=='close'
    assert sum(row['state']=='committed' for row in rows(x,'knowledge_jobs'))==len(records)
    with x.f.stores.database.read() as db:
        assert not db.execute("SELECT 1 FROM nlp_jobs WHERE state='running'").fetchone()
    if records:
        assert records[0].source.source_ref==claims[0].source_ref
        assert all(task.result.payload.outcome=='assessed_no_match' for task in records[0].tasks)


def test_turn_failed_later_prepare_never_reopens_or_loses_first_record(turn_sources,monkeypatch):
    e,x = turn_sources
    claims = observe_claims(x,monkeypatch)
    prepares = []
    def fail(name,request,*_):
        if name=='prepare':
            prepares.append(request)
            if len(prepares)==3:
                raise ValueError('synthetic_prepare_failure')
    e.engine.hook = fail
    with pytest.raises(ValueError,match='synthetic_prepare_failure'):
        x.runner.process_batch(ACCOUNT)
    assert len(claims)==2 and len(saved_records(x))==1
    assert names(e).count('open')==1 and names(e)[-1]=='close'


def test_turn_source_reread_refusal_stops_without_reclaim_or_worker(turn_sources,monkeypatch):
    e,x = turn_sources
    claims = observe_claims(x,monkeypatch)
    read,calls = x.runner._read,[]
    def changed(*args):
        calls.append(True)
        return None if len(calls)==2 else read(*args)
    monkeypatch.setattr(x.runner,'_read',changed)
    assert x.runner.process_batch(ACCOUNT) is False
    assert len(claims)==1 and not saved_records(x) and not e.engine.trace


@pytest.mark.parametrize('refusal',['authority','cancel','expiry'])
def test_turn_refusal_during_first_source_never_seals_partial_or_claims_second(turn_sources,monkeypatch,refusal):
    e,x = turn_sources
    claims = observe_claims(x,monkeypatch)
    calls,stop = [],[False]
    def authorize(_):
        if stop[0] and refusal=='authority':
            raise ValueError('synthetic_authority_refusal')
    x.runner.authorize = authorize
    x.runner.cancellation_check = lambda:stop[0] and refusal=='cancel'
    def stop_first(name,request,*_):
        if name=='analyze':
            calls.append(request)
            if len(calls)==1:
                if refusal=='expiry':
                    x.f.clock.now += timedelta(seconds=121)
                else:
                    stop[0] = True
    e.engine.hook = stop_first
    expected = ValueError if refusal=='authority' else AssertionError
    with pytest.raises(expected):
        x.runner.process_batch(ACCOUNT)
    assert not saved_records(x) and len(calls)==1 and len(claims)==1
    assert names(e)[-1]=='close'


def test_turn_common_deadline_and_global_contention(turn_sources,monkeypatch):
    from app.analytics import nlp_processing
    e,x = turn_sources
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(nlp_processing,'time',SimpleNamespace(monotonic=lambda:clock.now))
    claims = observe_claims(x,monkeypatch)
    calls = []
    def advance(name,request,*_):
        if name=='analyze':
            calls.append(request)
            clock.now += 20
    e.engine.hook = advance
    with execution_slot() as acquired:
        assert acquired and x.runner.process_batch(ACCOUNT) is False
    assert not e.engine.trace and not claims
    assert x.runner.process_batch(ACCOUNT)
    assert len(claims)==2 and len(calls)==4
    assert calls[2].deadline_ms <= 80_000 and calls[3].deadline_ms <= 60_000
    assert claims[1].expires_at-claims[0].expires_at <= -40


def test_turn_first_source_expiry_does_not_claim_it_again(turn_sources,monkeypatch):
    from app.analytics import nlp_processing
    e,x = turn_sources
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(nlp_processing,'time',SimpleNamespace(monotonic=lambda:clock.now))
    claims = observe_claims(x,monkeypatch)
    e.engine.hook = lambda name,*_:setattr(clock,'now',221.0) if name=='analyze' else None
    with pytest.raises(AssertionError):
        x.runner.process_batch(ACCOUNT)
    assert len(claims)==1 and not saved_records(x)


@pytest.mark.parametrize('retained',['joint','mentions'],indirect=True)
def test_turn_single_component_profiles_keep_one_preparation(turn_sources,monkeypatch):
    e,x = turn_sources
    claims = observe_claims(x,monkeypatch)
    assert x.runner.process_batch(ACCOUNT)
    assert len(saved_records(x))==2 and len(claims)==2
    assert names(e).count('prepare')==1 and names(e).count('open')==1
    assert all(len(record.tasks)==len(x.profile.bindings) for record in saved_records(x))


@pytest.mark.parametrize('replay',['first','all'])
def test_turn_replay_first_does_not_imply_prepared_component(turn_sources,monkeypatch,replay):
    e,x = turn_sources
    assert x.runner.process_batch(ACCOUNT,maximum=1 if replay=='first' else 2)
    records = saved_records(x)
    for record in records:
        x.runner.findings.retry(ACCOUNT,record.source.source_ref)
    e.engine.trace.clear()
    calls = []
    e.engine.hook = lambda name,request,*_:calls.append(request) if name=='analyze' else None
    if replay=='all':
        x.runner.execution_ready = lambda _:False
    assert x.runner.process_batch(ACCOUNT)
    assert len(saved_records(x))==2
    if replay=='all':
        assert not e.engine.trace
    else:
        assert [x.profile.component(r.component_id).tasks[0].task for r in calls]==['concept_mentions','message_topics']
        assert names(e).count('open')==1


def test_turn_staged_replay_needs_no_worker(turn_sources,monkeypatch):
    e,x = turn_sources
    seal = x.owner.seal
    def stage(work,frame=None):
        with x.owner._fence('commit') as policy,x.repository.database.transaction() as db:
            x.repository.stage_member_in(db,work.attempt,0,frame,policy=policy)
        raise RuntimeError('after stage')
    monkeypatch.setattr(x.owner,'seal',stage)
    with pytest.raises(RuntimeError,match='after stage'):
        x.runner.process_batch(ACCOUNT)
    monkeypatch.setattr(x.owner,'seal',seal)
    x.f.clock.now += timedelta(seconds=121)
    e.engine.trace.clear()
    x.runner.execution_ready = lambda _:False
    assert x.runner.process_batch(ACCOUNT)
    assert not e.engine.trace and len(saved_records(x))==1



def test_turn_uses_existing_per_claim_fairness_and_database_barrier(turn_sources,monkeypatch):
    e,x = turn_sources
    store = x.runner.findings
    with x.f.stores.database.transaction() as db:
        refs = [row[0] for row in db.execute('SELECT source_ref FROM nlp_jobs ORDER BY source_ref')]
        db.execute("UPDATE nlp_jobs SET lane='live' WHERE source_ref=?",(refs[0],))
        db.execute("UPDATE nlp_jobs SET lane='backfill' WHERE source_ref=?",(refs[1],))
        db.execute('UPDATE nlp_accounts SET live_claims=2')
    original,lanes = store.claim,[]
    def claim(*args,**kwargs):
        value = original(*args,**kwargs)
        if value:
            with x.f.stores.database.read() as db:
                assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state='running'").fetchone()[0]==1
                lanes.append(db.execute('SELECT lane FROM nlp_jobs WHERE source_ref=?',(value.source_ref,)).fetchone()[0])
        return value
    monkeypatch.setattr(store,'claim',claim)
    assert x.runner.process_batch(ACCOUNT)
    assert lanes==['live','backfill']


def test_turn_competing_claim_between_sources_keeps_first_commit(turn_sources,monkeypatch):
    e,x = turn_sources
    original,calls,foreign = x.runner.findings.claim,[],[]
    def claim(*args,**kwargs):
        calls.append(True)
        if len(calls)==2:
            assert len(saved_records(x))==1
            foreign.append(original(*args,**kwargs))
        return original(*args,**kwargs)
    monkeypatch.setattr(x.runner.findings,'claim',claim)
    try:
        assert x.runner.process_batch(ACCOUNT)
        assert foreign[0] is not None and len(saved_records(x))==1
        assert names(e).count('analyze')==2
    finally:
        for owned in foreign:
            x.runner.findings.release(owned)


def test_turn_rechecks_authority_before_claiming_second_source(turn_sources,monkeypatch):
    e,x = turn_sources
    claims = observe_claims(x,monkeypatch)
    def authorize(_):
        if saved_records(x):
            raise ValueError('synthetic_authority_refusal')
    x.runner.authorize = authorize
    with pytest.raises(ValueError,match='synthetic_authority_refusal'):
        x.runner.process_batch(ACCOUNT)
    assert len(saved_records(x))==1 and len(claims)==1


def test_turn_retains_typed_failures_without_breaking_scope(turn_sources,monkeypatch):
    import json
    from tests.test_nlp_processing_publication import ConformanceAdapter
    e,x = turn_sources
    original = ConformanceAdapter.__call__
    def typed_failure(self,request,cancelled):
        document = json.loads(original(self,request,cancelled))
        for result in document['items'][0]['tasks']:
            result['payload'].update(outcome='failed',reason='worker_failure',coverage={
                'source_codepoints':len(request.items[0].text),'examined':[],
                'excluded':[{'start':0,'end':len(request.items[0].text)}],'limits_reached':[]})
        return json.dumps(document).encode()
    monkeypatch.setattr(ConformanceAdapter,'__call__',typed_failure)
    assert x.runner.process_batch(ACCOUNT)
    assert len(saved_records(x))==2 and names(e).count('open')==1
    assert all(task.result.payload.outcome=='failed' for record in saved_records(x) for task in record.tasks)
