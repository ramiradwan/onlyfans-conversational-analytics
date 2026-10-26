"""Versioned enrollment commands against encrypted stores and signed grants."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from app.models.text_analysis_enrollment import EnrollmentCommand, EnrollmentIdentity
from app.models.text_analysis_jobs import EnrollmentIntent, ExecutionParameters
from app.nlp.profiles import artifact_closure_digest
from app.persistence.contributions import retire_scope, storage_high_water
from app.persistence.contributions import ContributionRepository
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention
from app.persistence.text_analysis_authority import FixtureTextAnalysisLifecycle, TextAnalysisCurrentEvidence, trust_digest
from app.persistence.text_analysis_enrollment import MAX_VERSION, TextAnalysisEnrollmentStore
from app.persistence.text_analysis_jobs import TextAnalysisJobStore
from app.security.text_analysis_policy import encode_run_context
from tests.test_text_analysis_jobs import execution, contribution_data, authority_basis
from tests.test_text_analysis_identity import make_identity_fixture

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def enrolled(execution, authority_basis, tmp_path):
    x = execution('license', one=True)
    auth, authority = authority_basis[:2]
    scope = {name: getattr(x.context, name) for name in
             ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}
    identity = make_identity_fixture(tmp_path / 'identity', scope=scope,
        creator_account_id=x.context.creator_account_id, principal_id=x.context.principal_id, authentication=auth)
    closure = artifact_closure_digest(tuple(a for a in x.profile.artifacts if a.role in ('weights', 'tokenizer', 'configuration')))
    x.context = replace(x.context, algorithm_artifacts_sha256=closure)
    x.plan = x.plan.model_copy(update={'algorithm_artifacts_sha256': closure})
    x.intent = x.intent.model_copy(update={'run_context': encode_run_context(x.context)})
    x.policy = replace(identity.owner.rebuild(identity.identity), text_analysis_authority=x.ref)
    with auth.database.transaction() as db:
        db.execute('UPDATE nlp_dependency_profiles SET closure_digest=? WHERE digest=?', (closure, x.profile.identity_digest()))
        version = db.execute('SELECT version FROM text_analysis_authority_references WHERE reference_id=?', (x.ref.reference_id,)).fetchone()[0]
    trust = authority_basis[6].trust
    evidence = TextAnalysisCurrentEvidence(trust, trust_digest(trust), x.policy.authorization_epoch.value,
        version, 2, 'd'*64, 'e'*64, hashlib.sha256(encode_run_context(x.context)).hexdigest(), None,
        x.context.seat_id, x.context.installation_key_id, x.context.installation_key_jkt, True)
    lifecycle = FixtureTextAnalysisLifecycle(auth.database, x.repo.database)

    @contextmanager
    def held():
        with authority.transition_fence(x.policy, x.context, kind='license', phase='start', evidence=evidence,
                                       lifecycle=lifecycle, fixture_only=True) as current:
            yield current
    x.held = held
    x.auth, x.authority, x.evidence = auth, authority, evidence
    x.identity = identity.identity
    x.enrollments = TextAnalysisEnrollmentStore(x.store)
    x.profile_raw = b'\n' + x.profile.model_dump_json(indent=2).encode() + b'\n'
    return x


def command(x, action='enable', *, current=None, key=None):
    if current is None:
        with x.repo.database.read() as db:
            current = x.enrollments.read_current_in(db, policy=x.policy)
    return EnrollmentCommand(command_key=key or str(uuid4()), expected_enrollment_id=current.enrollment_id,
        expected_mode_version=current.mode_version, action=action,
        expected_profile_sha256=x.profile.identity_digest() if action in ('enable', 'replace') else None)


def apply(x, cmd, *, fenced=True):
    payload = {}
    if cmd.action in ('enable', 'replace'):
        params = ExecutionParameters(**{k: v for k, v in x.plan.model_dump().items() if k != 'input'})
        payload = dict(intent=EnrollmentIntent(external_key=cmd.command_key, run_context=x.intent.run_context,
                                              parameters=params), identity=x.identity, profile_bytes=x.profile_raw)
    if fenced:
        with x.held() as policy, x.repo.database.transaction() as db:
            return x.enrollments.apply_command_in(db, cmd, policy=policy, **payload)
    with x.repo.database.transaction() as db:
        return x.enrollments.apply_command_in(db, cmd, policy=x.policy, **payload)


def test_exact_lost_response_does_not_reapply_historical_mode(enrolled):
    x = enrolled
    enable = command(x)
    first = apply(x, enable)
    pause = command(x, 'pause')
    paused = apply(x, pause, fenced=False)
    replay = apply(x, enable, fenced=False)
    assert replay.receipt == first.receipt
    assert replay.current == paused.current and replay.current.state == 'paused'
    assert replay.current.mode_version == 2
    with x.repo.database.read() as db:
        capsule = x.enrollments.read_capsule_in(db, first.current.enrollment_id, policy=x.policy)
        assert capsule.profile_bytes == x.profile_raw
        assert capsule.profile_bytes_sha256 == hashlib.sha256(x.profile_raw).hexdigest()
        assert capsule.intent.parameters.algorithm_profile_sha256 == x.profile.identity_digest()
        assert x.enrollments.read_command_in(db, str(uuid4()), policy=x.policy) is None


def test_changed_key_reuse_and_stale_cas_refuse(enrolled):
    x = enrolled
    enable = command(x)
    apply(x, enable)
    with pytest.raises(ValueError, match='command_conflict'):
        apply(x, enable.model_copy(update={'expected_mode_version': 1}), fenced=False)
    stale = command(x, 'pause')
    apply(x, command(x, 'pause'), fenced=False)
    with pytest.raises(ValueError, match='command_stale'):
        apply(x, stale, fenced=False)


def test_replace_keeps_old_capsule_and_receipts_and_retire_pointer_version(enrolled):
    x = enrolled
    first = apply(x, command(x))
    with x.held() as policy:
        with x.repo.database.transaction() as db:
            child = x.store.prepare_child_in(db,first.current.enrollment_id,x.plan,x.members,
                                            profile_bytes=x.profile_raw,policy=policy)
            x.store.activate_in(db,child.job_id,policy=policy)
        token=x.repo.claim_attempt(child.job_id,expected_fence=0,lease_seconds=60,policy=policy)
        x.repo.stage_member(token,0,x.frames[0],policy=policy)
        manifest=x.repo.commit(token,policy=policy)
    replacement = apply(x, command(x, 'replace'))
    assert replacement.current.enrollment_id != first.current.enrollment_id
    with x.repo.database.read() as db:
        assert x.store.read_enrollment_in(db, first.current.enrollment_id, policy=x.policy)[0].state == 'retired'
        assert x.enrollments.read_capsule_in(db, first.current.enrollment_id, policy=x.policy).profile_bytes == x.profile_raw
        locators = x.enrollments.list_current_locators_in(db, installation_id=x.context.installation_id)
        assert [r.enrollment_id for r in locators] == [replacement.current.enrollment_id]
    assert x.repo.get_committed(child.contribution_id,policy=x.policy) == manifest
    retired = apply(x, command(x, 'retire'), fenced=False)
    assert retired.current.enrollment_id is None and retired.current.mode_version == 3
    newer = apply(x, command(x))
    assert newer.current.mode_version == 4


def test_creation_and_resume_need_live_fence_but_saved_capsule_does_not(enrolled):
    x = enrolled
    with pytest.raises(ValueError, match='fence_missing'):
        apply(x, command(x), fenced=False)
    first = apply(x, command(x))
    apply(x, command(x, 'pause'), fenced=False)
    with pytest.raises(ValueError, match='fence_missing'):
        apply(x, command(x, 'resume'), fenced=False)
    missing = replace(x.policy, text_analysis_authority=None)
    with x.repo.database.read() as db:
        assert x.enrollments.read_capsule_in(db, first.current.enrollment_id, policy=missing).identity == x.identity
    assert apply(x, command(x, 'resume')).current.state == 'enabled'


def test_pause_invalidates_running_attempt_retains_staging_and_producer(enrolled):
    x = enrolled
    parent = apply(x, command(x))
    with x.held() as policy:
        with x.repo.database.transaction() as db:
            child = x.store.prepare_child_in(db, parent.current.enrollment_id, x.plan, x.members,
                profile_bytes=x.profile_raw, policy=policy)
            x.store.activate_in(db, child.job_id, policy=policy)
        token = x.repo.claim_attempt(child.job_id, expected_fence=0, lease_seconds=60, policy=policy)
        x.repo.stage_member(token, 0, x.frames[0], policy=policy)
    apply(x, command(x, 'pause'), fenced=False)
    with x.repo.database.read() as db:
        job = db.execute('SELECT * FROM knowledge_jobs WHERE job_id=?', (child.job_id,)).fetchone()
        staged = db.execute('SELECT * FROM knowledge_members WHERE job_id=?', (child.job_id,)).fetchone()
        assert job['state'] == 'pending' and job['job_attempt_id'] is None and job['lease_expires_at'] is None
        assert staged['producing_attempt_fence'] == token.attempt_fence
        assert staged['producing_job_attempt_id'] == token.job_attempt_id
    apply(x, command(x, 'resume'))
    with x.held() as policy:
        with pytest.raises(ValueError, match='attempt_stale'):
            x.repo.commit(token, policy=policy)
        fresh = x.repo.claim_attempt(child.job_id, expected_fence=token.attempt_fence, lease_seconds=60, policy=policy)
        assert fresh.attempt_fence > token.attempt_fence
        x.repo.commit(fresh, policy=policy)


def test_capacity_failure_rolls_back_even_when_caught_inside_transaction(enrolled):
    x = enrolled
    enable = command(x)
    with x.repo.database.read() as db:
        before = storage_high_water(db)
        size = x.repo._storage(db)
    x.repo.max_storage_bytes = size + len(x.profile_raw) - 1
    params = ExecutionParameters(**{k:v for k,v in x.plan.model_dump().items() if k != 'input'})
    with x.held() as policy, x.repo.database.transaction() as db:
        with pytest.raises(ValueError, match='storage_full'):
            x.enrollments.apply_command_in(db, enable, policy=policy,
                intent=EnrollmentIntent(external_key=enable.command_key,run_context=x.intent.run_context,parameters=params),
                identity=x.identity,profile_bytes=x.profile_raw)
        assert x.enrollments.read_current_in(db, policy=x.policy).mode_version == 0
    with x.repo.database.read() as db:
        assert storage_high_water(db) == before
        assert x.enrollments.read_current_in(db, policy=x.policy).mode_version == 0
    x.repo.max_storage_bytes = 268435456
    assert apply(x, enable).current.mode_version == 1


@pytest.mark.parametrize('table', ['text_analysis_enrollment_capsules', 'text_analysis_enrollment_commands'])
def test_lifetime_metadata_cannot_be_deleted_or_updated(enrolled, table):
    x = enrolled
    apply(x, command(x))
    column = 'profile_bytes_sha256' if table.endswith('capsules') else 'meaning_sha256'
    with x.repo.database.transaction() as db:
        with pytest.raises(Exception, match='immutable'):
            db.execute(f"UPDATE {table} SET {column}=?", ('0'*64,))
        with pytest.raises(Exception, match='retained'):
            db.execute(f'DELETE FROM {table}')


def test_highwater_includes_commands_and_account_erasure_never_replays_old_enable(enrolled):
    x = enrolled
    enable = command(x)
    first = apply(x, enable)
    with x.repo.database.read() as db:
        before = storage_high_water(db)
    apply(x, command(x, 'pause'), fenced=False)
    with x.repo.database.read() as db:
        assert storage_high_water(db) != before
    retention = CreatorVaultRetention(x.repo.database, clock=lambda:x.f.clock.now)
    retention.delete_message(x.context.creator_account_id, 'm-0-0')
    with x.repo.database.read() as db:
        assert x.enrollments.read_current_in(db, policy=x.policy).enrollment_id == first.current.enrollment_id
    retention.delete_all(x.context.creator_account_id)
    replay = apply(x, enable, fenced=False)
    assert replay.receipt == first.receipt
    assert replay.current.enrollment_id is None and replay.current.mode_version == 3
    assert apply(x, command(x)).current.mode_version == 4


@pytest.mark.parametrize('limit', [0, 65, True, 1.0])
def test_locator_page_bounds(enrolled, limit):
    x = enrolled
    with x.repo.database.read() as db, pytest.raises(ValueError, match='page_invalid'):
        x.enrollments.list_current_locators_in(db, installation_id=x.context.installation_id, limit=limit)


def test_version_exhaustion_refuses_without_wrap(enrolled):
    x = enrolled
    apply(x, command(x))
    with x.repo.database.transaction() as db:
        db.execute('UPDATE text_analysis_account_current SET mode_version=?', (MAX_VERSION,))
    with pytest.raises(ValueError, match='version_exhausted'):
        apply(x, command(x, 'pause'), fenced=False)


@pytest.mark.parametrize('field,value', [('role', 'agent'), ('grant_reference_ids', ()),
    ('grant_reference_ids', ('same', 'same'))])
def test_identity_lineage_is_closed(enrolled, field, value):
    values = enrolled.identity.model_dump()
    values[field] = value
    with pytest.raises(ValueError):
        EnrollmentIdentity(**values)


def test_v15_active_capsuleless_enrollment_backfills_unavailable_owner(enrolled, tmp_path):
    x = enrolled
    catalog = tmp_path/'v15'
    catalog.mkdir()
    original = Path(__file__).resolve().parents[1]/'app/persistence/sql'
    for source in original.glob('*.sql'):
        if int(source.name[:4]) <= 15:
            shutil.copyfile(source, catalog/source.name)
    database = CanonicalSQLite(tmp_path/'legacy.sqlite3')
    MigrationRunner(database, migrations_dir=catalog).run()
    jobs = TextAnalysisJobStore(ContributionRepository(database))
    params = ExecutionParameters(**{k:v for k,v in x.plan.model_dump().items() if k != 'input'})
    intent = EnrollmentIntent(external_key=str(uuid4()),run_context=x.intent.run_context,parameters=params)
    with database.transaction() as db:
        legacy = jobs.enroll_in(db, intent, policy=x.policy)
        before = tuple(db.execute('SELECT * FROM text_analysis_enrollments').fetchone())
    shutil.copyfile(original/'0016_text_analysis_enrollment.sql', catalog/'0016_text_analysis_enrollment.sql')
    assert MigrationRunner(database, migrations_dir=catalog).run() == [16]
    store = TextAnalysisEnrollmentStore(jobs)
    with database.read() as db:
        assert tuple(db.execute('SELECT * FROM text_analysis_enrollments').fetchone()) == before
        current = store.read_current_in(db,policy=x.policy)
        assert current.enrollment_id == legacy.enrollment_id and current.mode_version == 1
        assert not current.capsule_available
        assert not store.list_current_locators_in(db,installation_id=x.context.installation_id)
        with pytest.raises(ValueError,match='capsule_unavailable'):
            store.read_capsule_in(db,legacy.enrollment_id,policy=x.policy)
    resume = EnrollmentCommand(command_key=str(uuid4()),expected_enrollment_id=legacy.enrollment_id,
        expected_mode_version=1,action='resume')
    with database.transaction() as db,pytest.raises(ValueError,match='capsule_unavailable'):
        store.apply_command_in(db,resume,policy=x.policy)
    replace_command=EnrollmentCommand(command_key=str(uuid4()),expected_enrollment_id=legacy.enrollment_id,
        expected_mode_version=1,action='replace',expected_profile_sha256=x.profile.identity_digest())
    replacement_intent=intent.model_copy(update={'external_key':replace_command.command_key})
    lifecycle=FixtureTextAnalysisLifecycle(x.auth.database,database)
    with x.authority.transition_fence(x.policy,x.context,kind='license',phase='start',evidence=x.evidence,
            lifecycle=lifecycle,fixture_only=True) as policy,database.transaction() as db:
        result=store.apply_command_in(db,replace_command,policy=policy,intent=replacement_intent,
                                     identity=x.identity,profile_bytes=x.profile_raw)
        assert result.current.enrollment_id != legacy.enrollment_id and result.current.capsule_available
        assert jobs.read_enrollment_in(db,legacy.enrollment_id,policy=x.policy)[0].state == 'retired'


@pytest.mark.parametrize('mutation', ['profile', 'closure', 'identity', 'selection'])
def test_capsule_refuses_independent_binding_substitutions(enrolled, mutation):
    x = enrolled
    cmd = command(x)
    params = ExecutionParameters(**{k:v for k,v in x.plan.model_dump().items() if k != 'input'})
    intent = EnrollmentIntent(external_key=cmd.command_key, run_context=x.intent.run_context, parameters=params)
    identity, raw = x.identity, x.profile_raw
    if mutation == 'profile':
        raw = raw.replace(b'"revision":', b'"unexpected_revision":', 1)
    elif mutation == 'closure':
        intent = intent.model_copy(update={'parameters':params.model_copy(update={'algorithm_artifacts_sha256':'0'*64})})
    elif mutation == 'identity':
        identity = identity.model_copy(update={'installation_key_jkt':'different'})
    else:
        cmd = cmd.model_copy(update={'expected_profile_sha256':'0'*64})
    with x.held() as policy, x.repo.database.transaction() as db:
        with pytest.raises(ValueError):
            x.enrollments.apply_command_in(db,cmd,policy=policy,intent=intent,identity=identity,profile_bytes=raw)
        assert x.enrollments.read_current_in(db,policy=x.policy).mode_version == 0


def test_current_pointer_principal_is_scope_not_another_active_slot(enrolled):
    x = enrolled
    first = apply(x,command(x))
    wrong = replace(x.policy,identity=replace(x.policy.identity,principal_id='other-principal'))
    with x.repo.database.read() as db:
        with pytest.raises(ValueError,match='scope_invalid'):
            x.enrollments.read_current_in(db,policy=wrong)
        with pytest.raises(ValueError,match='scope_invalid'):
            x.enrollments.read_command_in(db,first.receipt.command_key,policy=wrong)


@pytest.mark.parametrize('value', [True, 1.0, -1, MAX_VERSION+1])
def test_command_version_is_strict_and_bounded(enrolled,value):
    values=command(enrolled).model_dump()
    values['expected_mode_version']=value
    with pytest.raises(ValueError):
        EnrollmentCommand(**values)


def locator(x):
    with x.repo.database.read() as db:
        rows=x.enrollments.list_current_locators_in(db,installation_id=x.context.installation_id,limit=1)
        assert len(rows) == 1
        assert not x.enrollments.list_current_locators_in(db,installation_id=x.context.installation_id,
                                                       after_account=rows[0].creator_account_id,limit=1)
        return rows[0]


def test_locator_bootstrap_returns_only_original_identity_for_real_recomposition(enrolled):
    x=enrolled
    first=apply(x,command(x))
    selected=locator(x)
    with x.repo.database.read() as db:
        identity=x.enrollments.read_locator_identity_in(db,selected)
        assert type(identity) is EnrollmentIdentity and identity == x.identity
    from app.security.text_analysis_identity import TextAnalysisIdentityOwner
    rebuilt=TextAnalysisIdentityOwner(x.auth).rebuild(identity)
    assert rebuilt.text_analysis_authority is None and rebuilt.text_analysis_fence is None
    with x.repo.database.read() as db:
        assert x.enrollments.read_capsule_in(db,first.current.enrollment_id,policy=rebuilt).identity == identity
    apply(x,command(x,'pause'),fenced=False)
    with x.repo.database.read() as db:
        with pytest.raises(ValueError,match='locator_stale'):
            x.enrollments.read_locator_identity_in(db,selected)
        paused=x.enrollments.list_current_locators_in(db,installation_id=x.context.installation_id)[0]
        assert x.enrollments.read_locator_identity_in(db,paused) == identity
    apply(x,command(x,'replace'))
    with x.repo.database.read() as db,pytest.raises(ValueError,match='locator_stale'):
        x.enrollments.read_locator_identity_in(db,paused)


@pytest.mark.parametrize('field,value', [('installation_id','wrong'),('creator_account_id','wrong'),
    ('principal_id','wrong'),('enrollment_id','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'),('mode_version',99)])
def test_locator_cannot_substitute_current_scope_or_version(enrolled,field,value):
    x=enrolled
    apply(x,command(x))
    changed=locator(x).model_copy(update={field:value})
    with x.repo.database.read() as db,pytest.raises(ValueError,match='locator_stale'):
        x.enrollments.read_locator_identity_in(db,changed)


@pytest.mark.parametrize('column', ['profile', 'identity', 'profile_bytes_sha256', 'capsule_sha256'])
def test_locator_bootstrap_refuses_changed_capsule_integrity(enrolled,column):
    x=enrolled
    apply(x,command(x))
    selected=locator(x)
    with x.repo.database.transaction() as db:
        db.execute('DROP TRIGGER text_analysis_capsule_immutable')
        value=b'{}' if column in ('profile','identity') else '0'*64
        db.execute(f'UPDATE text_analysis_enrollment_capsules SET {column}=?',(value,))
        with pytest.raises(ValueError):
            x.enrollments.read_locator_identity_in(db,selected)


@pytest.mark.parametrize('field', ['role','grant_reference_ids','installation_key_jkt'])
def test_resume_cannot_replace_original_identity_lineage(enrolled,field):
    x=enrolled
    apply(x,command(x))
    apply(x,command(x,'pause'),fenced=False)
    if field == 'role':
        changed=replace(x.policy,identity=replace(x.policy.identity,role='operator'))
    else:
        authority=replace(x.policy.identity_authority,**{field:('different',) if field == 'grant_reference_ids' else 'different'})
        changed=replace(x.policy,identity_authority=authority)
    resume=command(x,'resume')
    with x.repo.database.transaction() as db,pytest.raises(ValueError,match='capsule_identity_mismatch'):
        x.enrollments.apply_command_in(db,resume,policy=changed)


def test_retired_owner_is_never_an_automatic_locator(enrolled):
    x=enrolled
    first=apply(x,command(x))
    selected=locator(x)
    apply(x,command(x,'retire'),fenced=False)
    with x.repo.database.read() as db:
        assert not x.enrollments.list_current_locators_in(db,installation_id=x.context.installation_id)
        with pytest.raises(ValueError,match='locator_stale'):
            x.enrollments.read_locator_identity_in(db,selected)
        assert x.enrollments.read_capsule_in(db,first.current.enrollment_id,policy=x.policy).identity == x.identity
