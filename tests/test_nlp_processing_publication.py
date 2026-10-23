"""Invented worker responses through durable claims and real publication CAS."""
import copy
import asyncio
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pytest

from app.analytics.nlp_findings_store import FindingsStore, checked_snapshot, check_generation, source_binding
from app.analytics.nlp_processing import FindingsRunner
from app.analytics.identity import source_identity
from app.nlp import v2
from app.nlp.profiles import AnalysisProfile
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup, advance, insert_message

pytestmark = pytest.mark.ci_tier('integration')
FIXTURES=Path(__file__).resolve().parents[1]/'shared/nlp/v2/fixtures'


class ConformanceAdapter:
    def __init__(self, profile):
        self.profile,self.requests,self.before=profile,[],None

    def __call__(self, request, cancelled):
        self.requests.append(request)
        if self.before:
            self.before()
        assert not cancelled()
        component=self.profile.component(request.component_id)
        tasks=[]
        for task in component.tasks:
            payload={'task':task.task,'outcome':'assessed_no_match','reason':'none','findings':[],
                'coverage':{'source_codepoints':len(request.items[0].text),
                    'examined':[{'start':0,'end':len(request.items[0].text)}],
                    'excluded':[],'limits_reached':[]}}
            tasks.append(dict(component_id=component.component_id,component_sha256=component.identity_digest(),
                text_sha256=request.items[0].text_sha256,payload=payload))
        return json.dumps(dict(request_id=request.request_id,profile_sha256=request.profile_sha256,
            protocol='nlp-worker.v2',component_id=request.component_id,component_sha256=request.component_sha256,
            items=[dict(request_item_id='message',tasks=tasks)])).encode()



@pytest.fixture
def fixture(tmp_path):
    value=make_fixture(tmp_path,conversations=1,messages=2)
    yield value
    cleanup(value)


def runner(f, kind='joint', **kwargs):
    profile=AnalysisProfile.model_validate_json((FIXTURES/f'{kind}-profile.json').read_bytes())
    adapter=ConformanceAdapter(profile)
    r=FindingsRunner(f.source,f.stores.projections,authorize=kwargs.pop('authorize',lambda account:None),
        execution_ready=kwargs.pop('execution_ready',lambda profile:True),adapter=adapter,
        clock=lambda:f.clock.now,enrichment=f.pipeline.enrichment,lease_clock=lambda:f.clock.now.timestamp(),**kwargs)
    r.findings.select(ACCOUNT,profile)
    return r,profile,adapter


@pytest.mark.parametrize('change', ['selection', 'admission'])
def test_claim_does_not_execute_under_an_earlier_selected_epoch(fixture, monkeypatch, change):
    r, profile, adapter = runner(fixture)
    claim = r.findings.claim

    def changed(account):
        if change == 'selection':
            r.findings.select(account, profile.model_copy(update={'revision': 'changed'}))
        else:
            r.findings.mode(account, 'paused')
            r.findings.mode(account, 'enabled')
        return claim(account)

    monkeypatch.setattr(r.findings, 'claim', changed)
    assert r.process_one(ACCOUNT) is False
    assert not adapter.requests


def snapshot(r,f):
    binding=r.findings.freeze(ACCOUNT,source_identity(f.source,ACCOUNT))
    with f.stores.database.read() as db:
        return checked_snapshot(db,binding)


@pytest.mark.parametrize('kind',['joint','split'])
def test_completion_republishes_unchanged_source_with_exact_snapshot_and_witness(fixture,kind):
    r,profile,adapter=runner(fixture,kind)
    r.reconcile(ACCOUNT)
    pending=r.publish_pending(ACCOUNT)
    assert pending.changed
    old=fixture.stores.database.active_generation(ACCOUNT).generation_id
    old_projection=pending.artifact.projection
    assert r.process_one(ACCOUNT)
    assert r.process_one(ACCOUNT)
    completed=r.publish_pending(ACCOUNT)
    assert completed.changed
    generation=fixture.stores.database.active_generation(ACCOUNT).generation_id
    assert generation!=old
    assert completed.artifact.projection.source_revision==old_projection.source_revision
    assert completed.artifact.projection.pipeline_config_digest!=old_projection.pipeline_config_digest
    assert fixture.pipeline.base_pipeline_config_digest==fixture.pipeline.pipeline_config_digest
    assert len(adapter.requests)==2*len(profile.components)
    with fixture.stores.database.read() as db:
        row=db.execute('SELECT * FROM projection_generations WHERE generation_id=?',(generation,)).fetchone()
        check_generation(db,row,current=True)
        binding=db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',(generation,)).fetchone()
        assert binding
        witness=fixture.stores.projections.activation.get(generation)
        assert witness.pipeline_config_digest==row['pipeline_config_digest']
    assert all(t['availability']=='current' for t in snapshot(r,fixture)['snapshot']['tasks'])
    assert r.publish_pending(ACCOUNT) is None


@pytest.mark.parametrize('race',['completion','aba','withdrawal','removal'])
def test_candidate_cannot_publish_after_findings_input_changes(fixture,race):
    r,profile,_=runner(fixture)
    pipeline=r.frozen_pipeline(ACCOUNT)
    candidate=pipeline.build_candidate(ACCOUNT)
    if race=='completion':
        assert r.process_one(ACCOUNT)
    elif race=='aba':
        r.findings.select(ACCOUNT,profile)
        r.findings.select(ACCOUNT,profile)
    else:
        r.findings.mode(ACCOUNT,'withdrawn' if race=='withdrawal' else 'removed')
    with pytest.raises(ValueError,match='nlp_snapshot_stale'):
        pipeline.publish_candidate(candidate)
    assert fixture.stores.database.active_generation(ACCOUNT) is None


def test_same_transaction_final_cas_rejects_completion_after_canonical_witness(fixture,monkeypatch):
    r,_,_=runner(fixture)
    pipeline=r.frozen_pipeline(ACCOUNT)
    candidate=pipeline.build_candidate(ACCOUNT)
    original=fixture.stores.projections._checkpoint
    def checkpoint(stage,generation):
        if stage=='canonical_completed':
            assert r.process_one(ACCOUNT)
        return original(stage,generation)
    monkeypatch.setattr(fixture.stores.projections,'_checkpoint',checkpoint)
    with pytest.raises(ValueError,match='nlp_snapshot_stale'):
        pipeline.publish_candidate(candidate)
    assert fixture.stores.database.active_generation(ACCOUNT) is None


def test_pause_and_temporary_readiness_loss_keep_valid_findings(fixture):
    ready=[True]
    r,_,_=runner(fixture,execution_ready=lambda profile:ready[0])
    while r.process_one(ACCOUNT):
        pass
    before=snapshot(r,fixture)
    ready[0]=False
    assert not r.process_one(ACCOUNT)
    assert snapshot(r,fixture)==before
    r.findings.mode(ACCOUNT,'paused')
    assert not r.process_one(ACCOUNT)
    paused = copy.deepcopy(before)
    paused['read_inventory']['processing_mode'] = 'paused'
    assert snapshot(r,fixture)==paused
    assert r.publish_pending(ACCOUNT).changed


@pytest.mark.parametrize('invalid',['edit','delete','barrier_account','barrier_conversation','barrier_message','barrier_participant','expiry','authorization'])
def test_late_source_or_authorization_change_never_commits_findings(fixture,invalid):
    allowed=[True]
    def authorize(account):
        if not allowed[0]:
            raise PermissionError('denied')
    r,_,adapter=runner(fixture,authorize=authorize)
    def change():
        if invalid=='authorization':
            allowed[0]=False
        elif invalid=='expiry':
            fixture.clock.now+=timedelta(days=100)
        else:
            with fixture.repositories.database.transaction() as db:
                if invalid=='edit':
                    db.execute("UPDATE account_messages SET text='Changed' WHERE creator_account_id=?",(ACCOUNT,))
                    advance(db)
                elif invalid=='delete':
                    db.execute('UPDATE account_messages SET is_deleted=1 WHERE creator_account_id=?',(ACCOUNT,))
                    advance(db)
                else:
                    scope=invalid.removeprefix('barrier_')
                    with fixture.stores.database.read() as derived:
                        message_id=derived.execute("SELECT s.message_id FROM nlp_jobs j JOIN nlp_sources s USING(account_ref,source_ref) WHERE j.state='running'").fetchone()[0]
                    key={'account':'*','conversation':'chat-0','message':message_id,'participant':'synthetic-fan'}[scope]
                    db.execute("INSERT INTO deletion_barriers(creator_account_id,scope_kind,scope_key,deletion_revision,deleted_at,provenance) VALUES(?,?,?,1,?,'example')",(ACCOUNT,scope,key,fixture.clock.now.isoformat()))
    adapter.before=change
    try:
        assert not r.process_one(ACCOUNT)
    except PermissionError:
        assert invalid=='authorization'
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0]==0


def test_worker_failure_is_partial_and_is_not_retried_as_empty_success(fixture):
    r,_,_=runner(fixture)
    r.adapter=lambda *args: (_ for _ in ()).throw(RuntimeError('worker failed'))
    assert r.process_one(ACCOUNT)
    assert r.process_one(ACCOUNT)
    assert not r.process_one(ACCOUNT)
    assert all(t['availability']=='partial' for t in snapshot(r,fixture)['snapshot']['tasks'])
    with fixture.stores.database.read() as db:
        records=[v2.read_record(bytes(row[0])) for row in db.execute('SELECT document FROM nlp_records')]
    assert all(t.result.payload.outcome=='failed' for record in records for t in record.tasks)


@pytest.mark.parametrize('outcome',['uncertain','unsupported','partial','failed'])
def test_independent_task_outcomes_survive_persistence(fixture,outcome):
    r,profile,adapter=runner(fixture,'split')
    def respond(request,cancelled):
        raw=json.loads(adapter(request,cancelled))
        if request.tasks==('concept_mentions',):
            payload=raw['items'][0]['tasks'][0]['payload']
            payload['outcome']=outcome
            payload['reason']={'uncertain':'below_threshold','unsupported':'unsupported_input',
                'partial':'limit_reached','failed':'worker_failure'}[outcome]
            if outcome in ('partial','unsupported'):
                payload['coverage']['excluded']=payload['coverage']['examined']
                payload['coverage']['examined']=[]
        return json.dumps(raw).encode()
    r.adapter=respond
    while r.process_one(ACCOUNT):
        pass
    snap=snapshot(r,fixture)
    assert {t['task']:t['availability'] for t in snap['snapshot']['tasks']}=={
        'message_topics':'current','concept_mentions':'partial'}
    with fixture.stores.database.read() as db:
        records=[v2.read_record(bytes(row[0])) for row in db.execute('SELECT document FROM nlp_records')]
    assert all(next(t.result.payload.outcome for t in record.tasks if t.result.payload.task=='concept_mentions')==outcome for record in records)


@pytest.mark.parametrize('kind',['joint','split'])
def test_nonempty_topics_and_source_spans_reach_generation_snapshot(fixture,kind):
    r,profile,adapter=runner(fixture,kind)
    def respond(request,cancelled):
        raw=json.loads(adapter(request,cancelled))
        descriptors={t.task:t for t in profile.component(request.component_id).tasks}
        for task in raw['items'][0]['tasks']:
            payload=task['payload']
            payload['outcome']='assessed_findings'
            category=descriptors[payload['task']].enabled_ids[0]
            payload['findings']=([{'topic_id':category}] if payload['task']=='message_topics' else
                [{'concept_id':category,'span':{'start':0,'end':6}},
                 {'concept_id':category,'span':{'start':7,'end':14}}])
        return json.dumps(raw).encode()
    r.adapter=respond
    while r.process_one(ACCOUNT):
        pass
    run=r.publish_pending(ACCOUNT)
    assert run.changed
    with fixture.stores.database.read() as db:
        generation=fixture.stores.database.active_generation(ACCOUNT).generation_id
        row=db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',(generation,)).fetchone()
        from app.analytics.nlp_findings_store import FrozenFindings
        frozen=checked_snapshot(db,FrozenFindings(row['account_ref'],row['snapshot_id']))
        records=[v2.read_record(bytes(db.execute('SELECT document FROM nlp_records WHERE record_digest=?',(key,)).fetchone()[0])) for key in frozen['records']]
    assert len(records)==2
    for record in records:
        topics=next(t.result.payload for t in record.tasks if t.result.payload.task=='message_topics')
        mentions=next(t.result.payload for t in record.tasks if t.result.payload.task=='concept_mentions')
        assert len(topics.findings)==1
        assert [(m.span.start,m.span.end) for m in mentions.findings]==[(0,6),(7,14)]
        assert record.profile.identity_digest()==profile.identity_digest()


def test_restart_reopens_real_database_and_recovers_claim(fixture):
    from app.analytics.database import ProjectionsDatabase
    r,_,_=runner(fixture)
    r.reconcile(ACCOUNT)
    now=[1.0]
    r.findings.clock=lambda:now[0]
    claim=r.findings.claim(ACCOUNT)
    reopened=ProjectionsDatabase(fixture.stores.database.path)
    recovered=FindingsStore(reopened,clock=lambda:now[0])
    assert recovered.claim(ACCOUNT) is None
    now[0]=122
    new=recovered.claim(ACCOUNT)
    assert new.owner!=claim.owner
    assert not recovered.valid_claim(claim)


def test_duplicate_completion_and_restart_redelivery(fixture):
    r,profile,adapter=runner(fixture)
    r.reconcile(ACCOUNT)
    claim=r.findings.claim(ACCOUNT)
    from app.analytics.evidence_contracts import EvidenceLocation
    source=r._read(ACCOUNT,EvidenceLocation(conversation_id=claim.conversation_id,message_id=claim.message_id),r._budget())
    from tests.test_nlp_record_validation import case, frame
    # Use a source-compatible no-match record produced through the adapter path.
    r.findings.mode(ACCOUNT,'paused')
    r.findings.mode(ACCOUNT,'enabled')
    assert r.process_one(ACCOUNT)
    state=r.findings.state(ACCOUNT)
    with fixture.stores.database.read() as db:
        job=db.execute("SELECT * FROM nlp_jobs WHERE state='done'").fetchone()
        raw=bytes(db.execute('SELECT document FROM nlp_records WHERE record_digest=?',(job['record_digest'],)).fetchone()[0])
    completed=replace(claim,source_ref=job['source_ref'],source_version=job['source_version'],owner=job['owner'],
        selection_epoch=job['selection_epoch'],admission_epoch=job['admission_epoch'])
    source=r._read(ACCOUNT,EvidenceLocation(conversation_id=claim.conversation_id,message_id=claim.message_id),r._budget())
    assert not r.findings.complete(completed,profile,source,raw)
    assert r.findings.state(ACCOUNT)['completion_revision']==state['completion_revision']
    restarted=FindingsRunner(fixture.source,fixture.stores.projections,authorize=lambda a:None,
        execution_ready=lambda p:True,adapter=adapter,clock=lambda:fixture.clock.now,enrichment=fixture.pipeline.enrichment)
    assert restarted.publish_pending(ACCOUNT).changed
    assert restarted.publish_pending(ACCOUNT) is None


def test_expired_stolen_claim_cannot_complete_and_recovery_is_bounded(fixture):
    r,_,_=runner(fixture)
    r.reconcile(ACCOUNT)
    now=[1.0]
    r.findings.clock=lambda:now[0]
    first=r.findings.claim(ACCOUNT,deadline_seconds=120)
    assert r.findings.claim(ACCOUNT) is None
    now[0]=120.5
    assert r.findings.valid_claim(first)
    now[0]=121.1
    stolen=r.findings.claim(ACCOUNT)
    assert stolen and stolen.owner!=first.owner
    assert not r.findings.valid_claim(first)
    from app.analytics.evidence_contracts import EvidenceLocation
    from tests.test_nlp_record_validation import case, frame
    source=r._read(ACCOUNT,EvidenceLocation(conversation_id=first.conversation_id,message_id=first.message_id),r._budget())
    profile,_,document=case(message=source)
    with pytest.raises(ValueError,match='nlp_claim_stale'):
        r.findings.complete(first,profile,source,frame(document))
    now[0]+=121
    assert r.findings.claim(ACCOUNT) is not None  # Another source can progress.
    with fixture.stores.database.read() as db:
        assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state='abandoned'").fetchone()[0]==1


def test_deleted_generation_binding_is_not_a_valid_baseline(fixture):
    r,_,_=runner(fixture)
    run=r.publish_pending(ACCOUNT)
    generation=fixture.stores.database.active_generation(ACCOUNT).generation_id
    with fixture.stores.database.transaction() as db:
        db.execute('DELETE FROM nlp_generation_refs WHERE generation_id=?',(generation,))
    with fixture.stores.database.read() as db:
        row=db.execute('SELECT * FROM projection_generations WHERE generation_id=?',(generation,)).fetchone()
        with pytest.raises(ValueError,match='binding_missing'):
            check_generation(db,row)
    with pytest.raises(Exception):
        _=run.artifact


def test_zero_eligible_explicit_current_coverage(fixture):
    r,_,_=runner(fixture)
    with fixture.repositories.database.transaction() as db:
        db.execute('UPDATE account_messages SET is_deleted=1 WHERE creator_account_id=?',(ACCOUNT,))
        advance(db)
    assert r.reconcile(ACCOUNT)
    snap=snapshot(r,fixture)
    assert snap['records']==[]
    assert all(t['availability']=='current' for t in snap['snapshot']['tasks'])
    assert all(c['eligible']==c['completed']==c['missing']==0 and c['scan_complete'] for c in snap['coverage'].values())


def test_reconciliation_continues_across_work_budget_and_queue_is_bounded(fixture):
    r,_,_=runner(fixture)
    with fixture.repositories.database.transaction() as db:
        for i in range(300):
            insert_message(db,'chat-0',f'p-{i:04}',fixture.clock.now-timedelta(days=2),i)
        advance(db)
    assert not r.reconcile(ACCOUNT,maximum=64)
    assert all(t['availability']=='pending' for t in snapshot(r,fixture)['snapshot']['tasks'])
    while not r.reconcile(ACCOUNT,maximum=64):
        pass
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_sources').fetchone()[0]==302
        assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state='pending'").fetchone()[0]==256


def test_baseline_digest_and_existing_content_receipt_remain_unchanged(fixture):
    from app.analytics.validation_receipt import content_stamp
    assert fixture.pipeline.pipeline_config_digest==fixture.pipeline.base_pipeline_config_digest
    before=fixture.pipeline.pipeline_config_digest
    fixture.pipeline.project_account(ACCOUNT)
    baseline_calls=[a.calls for a in fixture.analyzers]
    with fixture.stores.database.read() as db:
        assert content_stamp(db) is not None
    r,_,_=runner(fixture)
    r.publish_pending(ACCOUNT)
    assert fixture.pipeline.pipeline_config_digest==before
    assert [a.calls for a in fixture.analyzers]==baseline_calls


@pytest.mark.asyncio
async def test_real_scheduler_republishes_unchanged_canonical_and_restart_redelivers(fixture):
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.models.analytics import AvailabilityStatus
    r,profile,adapter=runner(fixture)
    scheduler=InProcessProjectionScheduler(fixture.pipeline,generation_pipeline=r.frozen_pipeline)
    try:
        await scheduler.start(recover=False)
        assert await r.notify_scheduler(scheduler,ACCOUNT)
        assert (await scheduler.wait(ACCOUNT)).availability==AvailabilityStatus.AVAILABLE
        old=fixture.stores.database.active_generation(ACCOUNT).generation_id
        assert r.process_one(ACCOUNT)
        assert await r.notify_scheduler(scheduler,ACCOUNT)
        assert (await scheduler.wait(ACCOUNT)).availability==AvailabilityStatus.AVAILABLE
        assert fixture.stores.database.active_generation(ACCOUNT).generation_id!=old
        assert not await r.notify_scheduler(scheduler,ACCOUNT)
        assert r.process_one(ACCOUNT)  # Commit a notification and lose its delivery.
    finally:
        assert await scheduler.close(timeout=10)
    restarted=FindingsRunner(fixture.source,fixture.stores.projections,authorize=lambda a:None,
        execution_ready=lambda p:True,adapter=adapter,clock=lambda:fixture.clock.now,enrichment=fixture.pipeline.enrichment)
    scheduler=InProcessProjectionScheduler(fixture.pipeline,generation_pipeline=restarted.frozen_pipeline)
    try:
        await scheduler.start(recover=False)
        assert await restarted.notify_scheduler(scheduler,ACCOUNT)
        assert (await scheduler.wait(ACCOUNT)).availability==AvailabilityStatus.AVAILABLE
        assert not await restarted.notify_scheduler(scheduler,ACCOUNT)
    finally:
        assert await scheduler.close(timeout=10)


@pytest.mark.asyncio
async def test_completion_during_scheduler_build_is_not_lost(fixture,monkeypatch):
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.models.analytics import AvailabilityStatus
    from threading import Event
    r,_,_=runner(fixture)
    built,release=Event(),Event()
    original=fixture.stores.projections._checkpoint
    once=[False]
    def checkpoint(stage,generation):
        if stage=='validated' and not once[0]:
            once[0]=True
            built.set()
            assert release.wait(10)
        return original(stage,generation)
    monkeypatch.setattr(fixture.stores.projections,'_checkpoint',checkpoint)
    scheduler=InProcessProjectionScheduler(fixture.pipeline,generation_pipeline=r.frozen_pipeline)
    try:
        await scheduler.start(recover=False)
        await r.notify_scheduler(scheduler,ACCOUNT)
        assert await asyncio.to_thread(built.wait,10)
        assert await asyncio.to_thread(r.process_one,ACCOUNT)
        await r.notify_scheduler(scheduler,ACCOUNT)
        release.set()
        assert (await scheduler.wait(ACCOUNT)).availability==AvailabilityStatus.AVAILABLE
        state=r.findings.state(ACCOUNT)
        assert state['notification_revision']==state['acknowledged_revision']
    finally:
        release.set()
        assert await scheduler.close(timeout=10)


def test_cached_currentness_does_not_parse_records_again_and_expiry_still_refuses(fixture,monkeypatch):
    r,_,_=runner(fixture)
    while r.process_one(ACCOUNT):
        pass
    pipeline=r.frozen_pipeline(ACCOUNT)
    pipeline.project_account(ACCOUNT)
    assert pipeline.projection_is_current(ACCOUNT,1)
    def forbidden(*args,**kwargs):
        raise AssertionError('record reread')
    monkeypatch.setattr(v2,'read_record',forbidden)
    assert pipeline.projection_is_current(ACCOUNT,1)
    fixture.clock.now+=timedelta(days=100)
    assert not pipeline.projection_is_current(ACCOUNT,1)


def test_expired_candidate_and_revoked_authorization_cannot_activate(fixture,monkeypatch):
    allowed=[True]
    def authorize(account):
        if not allowed[0]:
            raise PermissionError('denied')
    r,_,_=runner(fixture,authorize=authorize)
    pipeline=r.frozen_pipeline(ACCOUNT)
    candidate=pipeline.build_candidate(ACCOUNT)
    original=fixture.stores.projections._checkpoint
    def checkpoint(stage,generation):
        if stage=='canonical_completed':
            allowed[0]=False
        return original(stage,generation)
    monkeypatch.setattr(fixture.stores.projections,'_checkpoint',checkpoint)
    with pytest.raises(PermissionError):
        pipeline.publish_candidate(candidate)
    assert fixture.stores.database.active_generation(ACCOUNT) is None


def test_expiry_without_source_revision_change_refuses_publication(fixture):
    from app.analytics.errors import CanonicalRevisionChanged
    r,_,_=runner(fixture)
    assert r.process_one(ACCOUNT)
    pipeline=r.frozen_pipeline(ACCOUNT)
    candidate=pipeline.build_candidate(ACCOUNT)
    revision=source_identity(fixture.source,ACCOUNT).revision
    fixture.clock.now=candidate.reference.retention_due_at
    assert source_identity(fixture.source,ACCOUNT).revision==revision
    with pytest.raises(CanonicalRevisionChanged):
        pipeline.publish_candidate(candidate)
    assert fixture.stores.database.active_generation(ACCOUNT) is None


def test_expiry_between_admission_and_final_cas_is_refused(fixture,monkeypatch):
    r,_,_=runner(fixture)
    assert r.process_one(ACCOUNT)
    pipeline=r.frozen_pipeline(ACCOUNT)
    candidate=pipeline.build_candidate(ACCOUNT)
    original=fixture.stores.projections._checkpoint
    def checkpoint(stage,generation):
        if stage=='canonical_completed':
            fixture.clock.now=candidate.reference.retention_due_at
        return original(stage,generation)
    monkeypatch.setattr(fixture.stores.projections,'_checkpoint',checkpoint)
    with pytest.raises(ValueError,match='nlp_source_expired'):
        pipeline.publish_candidate(candidate)
    assert fixture.stores.database.active_generation(ACCOUNT) is None


def test_oversized_input_is_explicit_partial_without_calling_adapter(fixture):
    r,profile,adapter=runner(fixture)
    profile=profile.model_copy(update={'components':tuple(c.model_copy(update={'max_source_codepoints':1}) for c in profile.components)})
    r.findings.select(ACCOUNT,profile)
    assert r.process_one(ACCOUNT)
    assert r.process_one(ACCOUNT)
    assert not r.process_one(ACCOUNT)
    assert adapter.requests==[]
    snap=snapshot(r,fixture)
    assert all(t['availability']=='partial' for t in snap['snapshot']['tasks'])
    assert all(c['terminal_reasons']=={'unsupported_input':2} for c in snap['coverage'].values())


@pytest.mark.asyncio
async def test_scheduler_revocation_cannot_publish_frozen_input(fixture):
    from app.analytics.scheduling import InProcessProjectionScheduler
    r,_,_=runner(fixture)
    scheduler=InProcessProjectionScheduler(fixture.pipeline,generation_pipeline=r.frozen_pipeline)
    await scheduler.start(recover=False)
    pipeline=r.frozen_pipeline(ACCOUNT)
    candidate=pipeline.build_candidate(ACCOUNT,publication_epoch=scheduler._publication_epoch)
    assert await scheduler.close(timeout=10)
    with pytest.raises(Exception):
        pipeline.publish_candidate(candidate)
    assert fixture.stores.database.active_generation(ACCOUNT) is None
    state=r.findings.state(ACCOUNT)
    assert state['acknowledged_revision']<state['notification_revision']


def test_live_backfill_fairness_and_explicit_retry(fixture):
    r,_,_=runner(fixture)
    with fixture.repositories.database.transaction() as db:
        for i in range(5):
            insert_message(db,'chat-0',f'live-{i}',fixture.clock.now-timedelta(minutes=i),10+i)
        advance(db)
    lanes=[]
    original=r.findings.claim
    def claim(*args,**kwargs):
        value=original(*args,**kwargs)
        if value:
            with fixture.stores.database.read() as db:
                lanes.append(db.execute('SELECT lane FROM nlp_jobs WHERE source_ref=?',(value.source_ref,)).fetchone()[0])
        return value
    r.findings.claim=claim
    for _ in range(4):
        assert r.process_one(ACCOUNT)
    assert lanes==['live','live','live','backfill']
    with fixture.stores.database.read() as db:
        completed=db.execute("SELECT source_ref FROM nlp_jobs WHERE state='done' LIMIT 1").fetchone()[0]
    before=r.findings.state(ACCOUNT)
    assert r.findings.retry(ACCOUNT,completed)
    assert r.findings.state(ACCOUNT)['availability_revision']==before['availability_revision']+1


def test_new_live_work_enters_an_already_full_backfill_queue(fixture):
    r,_,_=runner(fixture)
    with fixture.repositories.database.transaction() as db:
        for i in range(260):
            insert_message(db,'chat-0',f'old-{i:04}',fixture.clock.now-timedelta(days=2),i)
        advance(db)
    r.reconcile(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        insert_message(db,'chat-0','fresh-live',fixture.clock.now,500)
        advance(db)
    r.reconcile(ACCOUNT)
    claim=r.findings.claim(ACCOUNT)
    assert claim.message_id=='fresh-live'
    with fixture.stores.database.read() as db:
        assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state IN ('pending','running')").fetchone()[0]==256


def test_temporary_loss_during_execution_releases_owned_claim(fixture):
    ready=[True]
    r,_,adapter=runner(fixture,execution_ready=lambda profile:ready[0])
    adapter.before=lambda:ready.__setitem__(0,False)
    assert not r.process_one(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert not db.execute("SELECT 1 FROM nlp_jobs WHERE state='running'").fetchone()
    ready[0]=True
    adapter.before=None
    assert r.process_one(ACCOUNT)


def test_adapter_diagnostics_are_not_attached_to_authorization_refusal(fixture):
    allowed=[True]
    def authorize(account):
        if not allowed[0]:
            raise PermissionError('denied')
    r,_,_=runner(fixture,authorize=authorize)
    def fail(*args):
        allowed[0]=False
        raise RuntimeError('Invented adapter diagnostic')
    r.adapter=fail
    with pytest.raises(PermissionError) as caught:
        r.process_one(ACCOUNT)
    assert caught.value.__context__ is None


@pytest.mark.parametrize('invalid',[123,'\ud800'])
def test_unchecked_source_boundary_refuses_without_disclosing_input(invalid):
    from tests.test_nlp_record_validation import source
    unchecked=source().model_copy(update={'text':invalid})
    with pytest.raises(ValueError,match='^nlp_source_invalid$') as caught:
        source_binding(unchecked)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_snapshot_verification_deadline_does_not_change_saved_results(fixture,monkeypatch):
    import app.analytics.nlp_findings_store as storage
    r,_,_=runner(fixture)
    assert r.process_one(ACCOUNT)
    binding=r.findings.freeze(ACCOUNT,source_identity(fixture.source,ACCOUNT))
    before=r.findings.state(ACCOUNT)
    monkeypatch.setattr(storage,'VERIFICATION_SECONDS',-1)
    with fixture.stores.database.read() as db:
        with pytest.raises(ValueError,match='nlp_findings_deadline'):
            checked_snapshot(db,binding)
    assert r.findings.state(ACCOUNT)==before


def test_record_size_refusal_remains_partial(fixture,monkeypatch):
    r,_,_=runner(fixture)
    def refuse(record):
        from app.nlp.contracts import NlpBoundaryError
        raise NlpBoundaryError('nlp_frame_too_large')
    monkeypatch.setattr(v2,'encode_record',refuse)
    assert r.process_one(ACCOUNT)
    snap=snapshot(r,fixture)
    assert all(c['terminal_reasons']=={'record_limit':1} for c in snap['coverage'].values())
    assert all(t['availability']=='partial' for t in snap['snapshot']['tasks'])


def test_locator_pages_use_existing_message_primary_key(fixture):
    with fixture.repositories.database.read() as db:
        plan=' '.join(str(tuple(row)) for row in db.execute('EXPLAIN QUERY PLAN SELECT chat_id,message_id FROM account_messages '
            'WHERE creator_account_id=? AND message_id>? ORDER BY message_id LIMIT 64',(ACCOUNT,'')))
    assert 'SEARCH' in plan and 'TEMP B-TREE' not in plan


@pytest.mark.parametrize('late',[False,True])
def test_split_components_share_one_total_deadline(fixture,late):
    r,_,adapter=runner(fixture,'split')
    def timed(request,cancelled):
        raw=adapter(request,cancelled)
        fixture.clock.now+=timedelta(seconds=70 if len(adapter.requests)==1 else 51 if late else 49)
        return raw
    r.adapter=timed
    assert r.process_one(ACCOUNT) is (not late)
    assert [q.deadline_ms for q in adapter.requests]==[120_000,50_000]
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0]==(0 if late else 1)


@pytest.mark.parametrize('kind',['joint','split'])
def test_incremental_and_full_rebuild_converge_for_same_frozen_findings(fixture,kind):
    from app.analytics.nlp_findings_store import FrozenFindings
    r,profile,adapter=runner(fixture,kind)
    def nonempty(request,cancelled):
        raw=json.loads(adapter(request,cancelled))
        descriptors={t.task:t for t in profile.component(request.component_id).tasks}
        for task in raw['items'][0]['tasks']:
            payload=task['payload']
            category=descriptors[payload['task']].enabled_ids[0]
            payload.update(outcome='assessed_findings',findings=[
                {'topic_id':category} if payload['task']=='message_topics' else
                {'concept_id':category,'span':{'start':7,'end':14}}])
        return json.dumps(raw).encode()
    r.adapter=nonempty
    assert r.process_one(ACCOUNT)
    r.publish_pending(ACCOUNT)
    assert r.process_one(ACCOUNT)
    pipeline=r.frozen_pipeline(ACCOUNT)
    incremental=pipeline.publish_candidate(pipeline.build_candidate(ACCOUNT))
    incremental_artifact=incremental.artifact
    old=fixture.stores.database.active_generation(ACCOUNT).generation_id
    with fixture.stores.database.read() as db:
        old_binding=dict(db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',(old,)).fetchone())
        frozen=FrozenFindings(old_binding['account_ref'],old_binding['snapshot_id'])
        before=checked_snapshot(db,frozen)
    calls=len(adapter.requests)
    full=pipeline.publish_candidate(pipeline.build_candidate(ACCOUNT,force=True,full_rebuild=True))
    assert len(adapter.requests)==calls  # Projection rebuilds consume frozen results only.
    assert full.artifact==incremental_artifact
    new=fixture.stores.database.active_generation(ACCOUNT).generation_id
    assert new!=old
    with fixture.stores.database.read() as db:
        assert dict(db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',(old,)).fetchone())==old_binding
        current=db.execute('SELECT * FROM nlp_generation_refs WHERE generation_id=?',(new,)).fetchone()
        assert current['snapshot_id']==old_binding['snapshot_id']
        after=checked_snapshot(db,frozen)
    assert after==before
    assert len(after['records'])==2
    assert all(t['availability']=='current' for t in after['snapshot']['tasks'])
    assert after['coverage']==before['coverage']
    assert [(t['findings_sha256'],t['coverage_sha256']) for t in after['snapshot']['tasks']]==[
        (t['findings_sha256'],t['coverage_sha256']) for t in before['snapshot']['tasks']]


@pytest.mark.parametrize('corruption',['record','snapshot','snapshot_bytes','account','reference'])
def test_corruption_invalidates_existing_content_proof(fixture,corruption):
    from app.analytics.validation_receipt import content_stamp
    r,_,_=runner(fixture)
    assert r.process_one(ACCOUNT)
    pipeline=r.frozen_pipeline(ACCOUNT)
    run=pipeline.project_account(ACCOUNT)
    assert pipeline.projection_is_current(ACCOUNT,1)
    with fixture.stores.database.transaction() as db:
        before=content_stamp(db)
        if corruption=='record':
            # Indexed snapshot ownership refuses ordinary record deletion.
            with pytest.raises(Exception):
                db.execute('DELETE FROM nlp_records')
            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='nlp_records_immutable'").fetchone()[0]
            db.execute('DROP TRIGGER nlp_records_immutable')
            db.execute("UPDATE nlp_records SET document=X'7b7d'")
            db.execute(trigger)
        elif corruption=='snapshot':
            # Foreign keys refuse deleting a generation's referenced manifest.
            with pytest.raises(Exception):
                db.execute('DELETE FROM nlp_snapshots')
            return
        elif corruption=='snapshot_bytes':
            trigger=db.execute("SELECT sql FROM sqlite_master WHERE name='nlp_snapshots_immutable'").fetchone()[0]
            db.execute('DROP TRIGGER nlp_snapshots_immutable')
            row=db.execute('SELECT document FROM nlp_snapshots LIMIT 1').fetchone()
            corrupted=json.loads(row[0])
            corrupted['coverage']['message_topics']['eligible']+=1
            db.execute('UPDATE nlp_snapshots SET document=?',(json.dumps(corrupted).encode(),))
            db.execute(trigger)
        elif corruption=='account':
            db.execute('UPDATE nlp_accounts SET selection_epoch=selection_epoch+1')
        else:
            db.execute('DELETE FROM nlp_generation_refs')
        if corruption!='account':
            assert content_stamp(db)!=before
    if corruption!='account':
        generation=fixture.stores.database.active_generation(ACCOUNT).generation_id
        with pytest.raises(Exception):
            fixture.stores.projections._validate_persisted_generation(generation)
    else:
        assert run.artifact.projection.source_revision==1
    assert not pipeline.projection_is_current(ACCOUNT,1)


def test_external_cancellation_reaches_first_split_component_poll(fixture):
    from app.analytics.query_execution import QuestionCancelled
    stopping = [False]
    polls, requests = [], []
    r, _, adapter = runner(fixture, 'split', cancellation_check=lambda: stopping[0])

    def execute(request, cancelled):
        requests.append(request)
        stopping[0] = True
        polls.append(cancelled())
        if polls[-1]:
            raise RuntimeError('cancelled')
        return adapter(request, cancelled)

    r.adapter = execute
    try:
        completed = r.process_one(ACCOUNT)
    except QuestionCancelled:
        completed = False
    assert polls == [True]
    assert completed is False
    assert len(requests) == 1
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM nlp_jobs WHERE state='running'").fetchone()


def test_external_cancellation_callback_error_is_not_worker_failure(fixture):
    active = [False]
    raised = [False]
    error = RuntimeError('cancellation callback failed')
    requests = []

    def cancellation_check():
        if active[0] and not raised[0]:
            raised[0] = True
            raise error
        return False

    r, _, adapter = runner(fixture, 'split', cancellation_check=cancellation_check)

    def execute(request, cancelled):
        requests.append(request)
        active[0] = True
        cancelled()
        return adapter(request, cancelled)

    r.adapter = execute
    with pytest.raises(RuntimeError, match='^cancellation callback failed$') as caught:
        r.process_one(ACCOUNT)
    assert caught.value is error
    assert caught.value.__context__ is None
    assert len(requests) == 1
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM nlp_jobs WHERE state='running'").fetchone()



@pytest.mark.parametrize('revoke_authorization', [False, True])
def test_external_cancellation_remains_observed_after_one_true_poll(fixture, revoke_authorization):
    active = [False]
    signaled = [False]
    allowed = [True]
    requests, polls = [], []

    def cancellation_check():
        if active[0] and not signaled[0]:
            signaled[0] = True
            return True
        return False

    def authorize(account):
        if not allowed[0]:
            raise PermissionError('denied')

    r, _, adapter = runner(fixture, 'split', authorize=authorize, cancellation_check=cancellation_check)

    def execute(request, cancelled):
        requests.append(request)
        active[0] = True
        polls.append(cancelled())
        if polls[-1]:
            if revoke_authorization:
                allowed[0] = False
            raise RuntimeError('cancelled')
        return adapter(request, cancelled)

    r.adapter = execute
    if revoke_authorization:
        with pytest.raises(PermissionError, match='^denied$') as caught:
            r.process_one(ACCOUNT)
        assert caught.value.__context__ is None
    else:
        assert r.process_one(ACCOUNT) is False
    assert polls == [True]
    assert len(requests) == 1
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM nlp_jobs WHERE state='running'").fetchone()
