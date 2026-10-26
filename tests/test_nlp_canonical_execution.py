"""Synthetic adapters through canonical execution and disposable replay."""
from dataclasses import replace
from datetime import timedelta
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analytics.nlp_canonical_execution import CanonicalFindingsExecution
from app.analytics.nlp_contribution_sources import ContributionSourceGuard
from app.analytics.nlp_processing import FindingsRunner
from app.analytics.factory import create_analytics_stores
from app.models.text_analysis_jobs import EnrollmentIntent, ExecutionParameters
from app.models.knowledge_contributions import AttemptToken
from app.nlp.profiles import artifact_closure_digest, canonical_bytes, read_profile
from app.persistence.contributions import ContributionRepository
from app.persistence.text_analysis_authority import FixtureTextAnalysisLifecycle, TextAnalysisCurrentEvidence, trust_digest
from app.persistence.text_analysis_jobs import TextAnalysisJobStore
from app.security.runtime_policy import AuthContext
from app.security.text_analysis_policy import encode_run_context
from tests.continuous_analytics_fixture import ACCOUNT, advance, cleanup, insert_message, make_fixture
from tests.test_nlp_processing_publication import ConformanceAdapter
from tests.test_text_analysis_execution_authority import basis as authority_basis

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def execution(tmp_path,authority_basis):
    resources = []
    def make(kind='joint', *, modify=None, fixture_only=True):
        f = make_fixture(tmp_path/kind,conversations=1,messages=1)
        resources.append(f)
        profile = read_profile((ROOT/f'shared/nlp/v2/fixtures/{kind}-profile.json').read_bytes())
        if modify is not None:
            profile = modify(profile)
        auth,authority,_,ref,context,original_policy,evidence,_ = authority_basis
        closure = artifact_closure_digest(tuple(a for a in profile.artifacts if a.role in ('weights','tokenizer','configuration')))
        context = replace(context,creator_account_id=ACCOUNT,algorithm_profile_sha256=profile.identity_digest(),
            algorithm_artifacts_sha256=closure)
        policy = replace(auth.build_runtime_policy(AuthContext('person',ACCOUNT,'creator')),
            identity_authority=original_policy.identity_authority,text_analysis_authority=ref)
        with auth.database.transaction() as db:
            db.execute("INSERT OR REPLACE INTO nlp_dependency_profiles VALUES(?,?,?,'available')",
                (profile.identity_digest(),canonical_bytes(profile),closure))
        evidence = replace(evidence,authorization_epoch=policy.authorization_epoch.value,
            context_sha256=hashlib.sha256(encode_run_context(context)).hexdigest())
        repository = ContributionRepository(f.repositories.database,source_guard=ContributionSourceGuard(),
            clock=lambda:f.clock.now.timestamp())
        jobs = TextAnalysisJobStore(repository)
        parameters = ExecutionParameters(parameter_sha256='1'*64,algorithm_profile_sha256=profile.identity_digest(),
            policy_identity_sha256=context.policy_identity_sha256,algorithm_artifacts_sha256=closure,
            model_or_rules_identity_sha256='2'*64)
        with repository.database.transaction() as db:
            enrollment = jobs.enroll_in(db,EnrollmentIntent(external_key=str(uuid4()),
                run_context=encode_run_context(context),parameters=parameters),policy=policy)
        lifecycle = FixtureTextAnalysisLifecycle(auth.database,repository.database)
        owner_args = dict(enrollment_id=enrollment.enrollment_id,identity_policy=policy,current_evidence=evidence,
            lifecycle=lifecycle,fixture_only=fixture_only)
        owner = CanonicalFindingsExecution(repository,authority,**owner_args)
        adapter = ConformanceAdapter(profile)
        runner = FindingsRunner(f.source,f.stores.projections,authorize=lambda account:None,
            execution_ready=lambda p:True,adapter=adapter,clock=lambda:f.clock.now,
            lease_clock=lambda:f.clock.now.timestamp(),canonical_execution=owner)
        runner.findings.select(ACCOUNT,profile)
        return SimpleNamespace(f=f,auth=auth,authority=authority,policy=policy,evidence=evidence,context=context,
            repository=repository,jobs=jobs,enrollment=enrollment,owner=owner,owner_args=owner_args,
            runner=runner,adapter=adapter,profile=profile)
    yield make
    for resource in resources:
        cleanup(resource)


def rows(x,table):
    with x.repository.database.read() as db:
        return [dict(row) for row in db.execute(f'SELECT * FROM {table}')]


def test_new_child_preparation_composes_module_from_baseline_identity(execution):
    x=execution()
    baseline=replace(x.policy,text_analysis_authority=None)
    x.runner.canonical_execution=CanonicalFindingsExecution(x.repository,x.authority,
        **dict(x.owner_args,identity_policy=baseline))
    assert x.runner.process_one(ACCOUNT)
    assert rows(x,'knowledge_jobs')[0]['state'] == 'committed'


@pytest.mark.parametrize('target',['current','different','retired'])
def test_retained_anchor_checks_exact_frozen_record_current_enrollment(execution,target):
    x=execution()
    assert x.runner.process_one(ACCOUNT)
    frozen=x.runner.frozen_pipeline(ACCOUNT).nlp_findings
    enrollment=x.enrollment.enrollment_id
    with x.repository.database.transaction() as db:
        if target == 'different':
            x.jobs.set_enrollment_mode_in(db,enrollment,'retired',policy=x.policy)
            enrollment=x.jobs.enroll_in(db,EnrollmentIntent(external_key=str(uuid4()),
                run_context=encode_run_context(x.context),parameters=x.owner.parameters),policy=x.policy).enrollment_id
        if target == 'retired':
            x.jobs.set_enrollment_mode_in(db,enrollment,'retired',policy=x.policy)
        else:
            db.execute('INSERT INTO text_analysis_account_current VALUES(?,?,?,?,?)',
                (x.context.installation_id,ACCOUNT,x.context.principal_id,1,enrollment))
    assert x.owner.retained_anchor(x.runner.findings,frozen) is (target == 'current')


def test_terminal_receipt_and_disposable_counter_are_not_retained_anchors(execution):
    def bounded(profile):
        return profile.model_copy(update={'components':tuple(c.model_copy(update={'max_source_codepoints':1}) for c in profile.components)})
    x=execution(modify=bounded)
    assert x.runner.process_one(ACCOUNT)
    assert rows(x,'text_analysis_intents')[0]['terminal_reason'] == 'unsupported_input'
    assert not rows(x,'knowledge_members') and not rows(x,'knowledge_contributions')
    with x.repository.database.transaction() as db:
        db.execute('INSERT INTO text_analysis_account_current VALUES(?,?,?,?,?)',
            (x.context.installation_id,ACCOUNT,x.context.principal_id,1,x.enrollment.enrollment_id))
    with x.runner.findings.database.transaction() as db:
        db.execute('UPDATE nlp_accounts SET completion_revision=1')
    frozen=x.runner.frozen_pipeline(ACCOUNT).nlp_findings
    assert x.runner.findings.state(ACCOUNT)['completion_revision'] == 1
    assert not x.owner.retained_anchor(x.runner.findings,frozen)


@pytest.mark.parametrize('change',['delete','change'])
def test_retained_anchor_refuses_erased_or_changed_source_origin(execution,change):
    from app.persistence.retention import CreatorVaultRetention
    x=execution()
    assert x.runner.process_one(ACCOUNT)
    frozen=x.runner.frozen_pipeline(ACCOUNT).nlp_findings
    with x.repository.database.transaction() as db:
        db.execute('INSERT INTO text_analysis_account_current VALUES(?,?,?,?,?)',
            (x.context.installation_id,ACCOUNT,x.context.principal_id,1,x.enrollment.enrollment_id))
    if change == 'delete':
        CreatorVaultRetention(x.repository.database,clock=lambda:x.f.clock.now).delete_message(ACCOUNT,'m-0-0')
    else:
        with x.repository.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='changed',content_hash='changed' WHERE creator_account_id=? AND message_id='m-0-0'",(ACCOUNT,))
    with pytest.raises(ValueError,match='origin_retired'):
        x.owner.retained_anchor(x.runner.findings,frozen)


@pytest.mark.parametrize('kind',['joint','split'])
def test_actual_runner_seals_before_origin_bound_projection_and_holds_no_worker_transaction(execution,kind,monkeypatch):
    x = execution(kind)
    def before():
        assert rows(x,'knowledge_jobs')[0]['state'] == 'running'
        assert not rows(x,'knowledge_contributions')
        for database in (x.auth.database,x.repository.database):
            with database.read() as db:
                db.execute('PRAGMA busy_timeout=1')
                db.execute('BEGIN IMMEDIATE')
    x.adapter.before = before
    monkeypatch.setattr(x.runner.findings,'complete',lambda *a,**k:pytest.fail('disposable completion'))
    assert x.runner.process_one(ACCOUNT)
    assert len(x.adapter.requests) == len(x.profile.components)
    assert rows(x,'knowledge_jobs')[0]['state'] == 'committed'
    canonical = rows(x,'knowledge_members')[0]['document']
    with x.f.stores.projections.database.read() as db:
        assert bytes(db.execute('SELECT document FROM nlp_records').fetchone()[0]) == canonical
        assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0] == 1


def test_seal_crash_and_projection_loss_replay_without_dependencies_or_inference(execution,monkeypatch):
    x = execution()
    monkeypatch.setattr(x.owner,'replay',lambda *a:(_ for _ in ()).throw(RuntimeError('after seal')))
    with pytest.raises(RuntimeError,match='after seal'):
        x.runner.process_one(ACCOUNT)
    assert rows(x,'knowledge_jobs')[0]['state'] == 'committed'
    with x.f.stores.projections.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0
    calls = len(x.adapter.requests)
    x.runner.canonical_execution = CanonicalFindingsExecution(x.repository,x.authority,**x.owner_args)
    x.runner.execution_ready = lambda p:False
    assert x.runner.process_one(ACCOUNT)
    rebuilt = create_analytics_stores('sqlite',
        projections_path=x.f.stores.projections.database.path.with_name('rebuilt.sqlite3'),
        activation=x.f.repositories.projection_activation,canonical_database=x.repository.database,
        canonical_identity_reader=x.f.source.read_identity,retention_clock=lambda:x.f.clock.now)
    try:
        restarted = FindingsRunner(x.f.source,rebuilt.projections,authorize=lambda a:None,
            execution_ready=lambda p:False,adapter=x.adapter,clock=lambda:x.f.clock.now,
            lease_clock=lambda:x.f.clock.now.timestamp(),canonical_execution=x.runner.canonical_execution)
        restarted.findings.select(ACCOUNT,x.profile)
        assert restarted.process_one(ACCOUNT)
    finally:
        rebuilt.projections.close_retention_scheduler()
    assert len(x.adapter.requests) == calls
    assert len(rows(x,'knowledge_jobs')) == 1


def test_staged_record_reclaimed_without_second_inference(execution,monkeypatch):
    x = execution()
    def stage_then_crash(work,frame=None):
        with x.owner._fence('commit') as policy,x.repository.database.transaction() as db:
            x.repository.stage_member_in(db,work.attempt,0,frame,policy=policy)
            with pytest.raises(ValueError,match='retained_result_exists'):
                x.jobs.finish_licensed_without_output_in(db,work.attempt,reason='record_limit',policy=policy)
        raise RuntimeError('after stage')
    monkeypatch.setattr(x.owner,'seal',stage_then_crash)
    with pytest.raises(RuntimeError,match='after stage'):
        x.runner.process_one(ACCOUNT)
    raw = bytes(rows(x,'knowledge_members')[0]['document'])
    calls = len(x.adapter.requests)
    x.f.clock.now += timedelta(seconds=121)
    x.runner.canonical_execution = CanonicalFindingsExecution(x.repository,x.authority,**x.owner_args)
    x.runner.execution_ready = lambda p:False
    assert x.runner.process_one(ACCOUNT)
    assert len(x.adapter.requests) == calls
    assert bytes(rows(x,'knowledge_members')[0]['document']) == raw
    assert rows(x,'knowledge_jobs')[0]['attempt_fence'] == 2
    assert rows(x,'knowledge_members')[0]['producing_attempt_fence'] == 1


def test_unrelated_additions_preserve_existing_child_slot(execution):
    x = execution()
    assert x.runner.process_one(ACCOUNT)
    original = rows(x,'knowledge_jobs')[0]
    with x.repository.database.transaction() as db:
        insert_message(db,'chat-0','new-source',x.f.clock.now)
        advance(db)
    x.runner.findings.retry(ACCOUNT,rows(x,'knowledge_sources')[0]['source_ref'])
    # Either queue order is allowed; processing both cannot replace the first child.
    assert x.runner.process_one(ACCOUNT)
    x.runner.process_one(ACCOUNT)
    assert any(row['job_id']==original['job_id'] for row in rows(x,'knowledge_jobs'))
    assert len(rows(x,'knowledge_jobs')) == 2
    assert len(x.adapter.requests) == 2


@pytest.mark.parametrize('change',['pause','revoke','cancel','lease','retire'])
def test_dispatch_and_commit_currentness_refusals_never_publish(execution,change):
    x = execution()
    cancellation = [False]
    if change == 'cancel':
        x.runner.cancellation_check = lambda:cancellation[0]
    def before():
        if change == 'pause':
            with x.repository.database.transaction() as db:
                x.jobs.set_enrollment_mode_in(db,x.enrollment.enrollment_id,'paused',policy=x.policy)
        elif change == 'revoke':
            x.authority.revoke(x.context.reference_id)
        elif change == 'lease':
            x.f.clock.now += timedelta(seconds=121)
        elif change == 'cancel':
            cancellation[0] = True
        elif change == 'retire':
            with x.repository.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='changed'")
    x.adapter.before = before
    if change in ('pause','revoke','retire'):
        with pytest.raises(ValueError): x.runner.process_one(ACCOUNT)
    else:
        assert x.runner.process_one(ACCOUNT) is False
    assert not rows(x,'knowledge_contributions')
    with x.f.stores.projections.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0


def test_unsupported_input_is_retained_terminal_gap_and_exact_retry(execution):
    def small(profile):
        value = profile.model_dump(mode='json')
        for component in value['components']: component['max_source_codepoints'] = 1
        return read_profile(__import__('json').dumps(value))
    x = execution(modify=small)
    assert x.runner.process_one(ACCOUNT)
    assert not x.adapter.requests
    terminal = rows(x,'text_analysis_intents')[0]
    assert terminal['state'] == 'failed' and terminal['terminal_reason'] == 'unsupported_input'
    assert not rows(x,'knowledge_members')
    x.runner.findings.retry(ACCOUNT,rows(x,'knowledge_sources')[0]['source_ref'])
    assert x.runner.process_one(ACCOUNT)
    assert len(rows(x,'knowledge_jobs')) == 1 and not x.adapter.requests
    row = rows(x,'knowledge_jobs')[0]
    token = AttemptToken(job_id=row['job_id'],attempt_fence=row['attempt_fence'],
        job_attempt_id=row['job_attempt_id'],lease_expires_at=row['lease_expires_at'])
    with x.owner._fence('commit') as policy,x.repository.database.transaction() as db:
        assert x.jobs.finish_licensed_without_output_in(db,token,reason='unsupported_input',policy=policy).state == 'failed'
        for reason in ('record_limit','worker_failure'):
            with pytest.raises(ValueError):
                x.jobs.finish_licensed_without_output_in(db,token,reason=reason,policy=policy)


def test_record_encoding_limit_is_retained_gap_without_disposable_record(execution,monkeypatch):
    from app.nlp import v2
    x = execution()
    monkeypatch.setattr(v2,'encode_record',lambda record:(_ for _ in ()).throw(ValueError('limit')))
    assert x.runner.process_one(ACCOUNT)
    assert rows(x,'text_analysis_intents')[0]['terminal_reason'] == 'record_limit'
    assert not rows(x,'knowledge_members') and not rows(x,'knowledge_contributions')
    with x.f.stores.projections.database.read() as db:
        assert db.execute('SELECT terminal_reason FROM nlp_jobs').fetchone()[0] == 'record_limit'
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0


def test_worker_failure_remains_failed_task_record_not_empty_success(execution):
    from app.nlp import v2
    x = execution()
    x.runner.adapter = lambda *a:(_ for _ in ()).throw(RuntimeError('worker'))
    assert x.runner.process_one(ACCOUNT)
    raw = bytes(rows(x,'knowledge_members')[0]['document'])
    record = v2.read_record(raw)
    assert {task.result.payload.task for task in record.tasks} == {'message_topics','concept_mentions'}
    assert all(task.result.payload.outcome=='failed' and task.result.payload.reason=='worker_failure' for task in record.tasks)


def test_extraction_only_profile_retains_absent_topic_semantics(execution):
    from app.nlp import v2
    from tests.test_nlp_task_subsets import selected
    x = execution(modify=lambda profile:selected('mentions'))
    assert x.runner.process_one(ACCOUNT)
    record = v2.read_record(bytes(rows(x,'knowledge_members')[0]['document']))
    assert record.schema_version == 'nlp-result.v3'
    assert [task.result.payload.task for task in record.tasks] == ['concept_mentions']


def test_expired_identity_policy_refuses_before_adapter_dispatch(execution):
    x = execution()
    x.owner.policy = replace(x.policy,expires_at=x.auth._clock())
    with pytest.raises(ValueError,match='policy_stale'):
        x.runner.process_one(ACCOUNT)
    assert not x.adapter.requests and not rows(x,'knowledge_contributions')


def test_missing_production_evidence_never_executes_or_falls_back(execution):
    x = execution(fixture_only=False)
    with pytest.raises(ValueError,match='production_provider_unavailable'):
        x.runner.process_one(ACCOUNT)
    assert not x.adapter.requests and not rows(x,'knowledge_contributions')


def test_capacity_refusal_is_not_a_completed_record(execution):
    x = execution()
    x.repository.max_storage_bytes = 1
    with pytest.raises(ValueError,match='storage_full'):
        x.runner.process_one(ACCOUNT)
    assert not x.adapter.requests and not rows(x,'knowledge_jobs')
    with x.f.stores.projections.database.read() as db:
        assert db.execute('SELECT count(*) FROM nlp_records').fetchone()[0] == 0


def identity_only(x):
    return replace(x.policy,text_analysis_authority=None,text_analysis_fence=None)


@pytest.mark.parametrize('mode',['paused','retired'])
def test_identity_only_enrollment_replays_without_module_or_dependency_providers(execution,mode):
    x = execution()
    assert x.runner.process_one(ACCOUNT)
    calls = len(x.adapter.requests)
    original_job = rows(x,'knowledge_jobs')[0]['job_id']
    original_record = bytes(rows(x,'knowledge_members')[0]['document'])
    original_manifest = bytes(rows(x,'knowledge_contributions')[0]['manifest'])
    with x.repository.database.transaction() as db:
        x.jobs.set_enrollment_mode_in(db,x.enrollment.enrollment_id,mode,policy=x.policy)
    x.authority.revoke(x.context.reference_id)
    x.runner.canonical_execution = CanonicalFindingsExecution(x.repository,x.authority,
        enrollment_id=x.enrollment.enrollment_id,identity_policy=identity_only(x))
    assert x.runner.canonical_execution.lifecycle is None
    assert x.runner.canonical_execution.evidence is None
    assert x.runner.canonical_execution.policy.text_analysis_authority is None
    x.runner.adapter = None
    x.runner.execution_ready = lambda p:False
    class AbsentDependencies:
        def ready(self,*args): pytest.fail('dependency readiness on retained replay')
        def retain(self,*args): pytest.fail('dependency acquisition on retained replay')
    x.runner.dependencies = AbsentDependencies()
    source_ref = rows(x,'knowledge_sources')[0]['source_ref']
    x.runner.findings.retry(ACCOUNT,source_ref)
    assert x.runner.process_one(ACCOUNT)
    assert len(x.adapter.requests) == calls
    assert rows(x,'knowledge_jobs')[0]['job_id'] == original_job
    assert bytes(rows(x,'knowledge_members')[0]['document']) == original_record
    assert bytes(rows(x,'knowledge_contributions')[0]['manifest']) == original_manifest
    with x.f.stores.projections.database.read() as db:
        assert bytes(db.execute('SELECT document FROM nlp_records').fetchone()[0]) == original_record
        assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0] == 1


def test_erased_child_receipt_is_found_without_recreating_execution(execution):
    from app.models.knowledge_contributions import SourceMemberSpec
    from app.persistence.retention import CreatorVaultRetention
    x = execution()
    assert x.runner.process_one(ACCOUNT)
    source = rows(x,'knowledge_sources')[0]
    member = SourceMemberSpec.model_validate_json(source['document'])
    job_id = rows(x,'knowledge_jobs')[0]['job_id']
    calls = len(x.adapter.requests)
    CreatorVaultRetention(x.repository.database,clock=lambda:x.f.clock.now).delete_message(ACCOUNT,'m-0-0')
    with x.repository.database.transaction() as db:
        x.jobs.set_enrollment_mode_in(db,x.enrollment.enrollment_id,'retired',policy=x.policy)
        receipt = x.jobs.find_child_in(db,x.enrollment.enrollment_id,member,policy=identity_only(x))
        assert receipt.job_id == job_id and receipt.state == 'result_deleted'
    x.runner.canonical_execution = CanonicalFindingsExecution(x.repository,x.authority,
        enrollment_id=x.enrollment.enrollment_id,identity_policy=identity_only(x))
    x.runner.findings.retry(ACCOUNT,source['source_ref'])
    assert x.runner.process_one(ACCOUNT) is False
    assert len(x.adapter.requests) == calls and not rows(x,'knowledge_jobs')


@pytest.mark.parametrize('unfinished',[False,True])
def test_identity_only_lookup_cannot_authorize_missing_or_unfinished_execution(execution,unfinished):
    from app.analytics.evidence_contracts import EvidenceLocation
    x = execution()
    if unfinished:
        original = x.runner._read(ACCOUNT,EvidenceLocation(conversation_id='chat-0',message_id='m-0-0'),x.runner._budget())
        x.owner.begin(x.f.source,x.profile,original,lease_seconds=120)
    x.runner.canonical_execution = CanonicalFindingsExecution(x.repository,x.authority,
        enrollment_id=x.enrollment.enrollment_id,identity_policy=identity_only(x))
    with pytest.raises(ValueError,match='^text_analysis_current_evidence_missing$'):
        x.runner.process_one(ACCOUNT)
    assert not x.adapter.requests and not rows(x,'knowledge_contributions')
    assert len(rows(x,'knowledge_jobs')) == int(unfinished)
