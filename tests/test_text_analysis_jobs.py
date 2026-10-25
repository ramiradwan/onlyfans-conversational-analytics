"""Canonical execution identity and atomic transitions with signed fixture authority."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.analytics.nlp_preview import SELECTION_POLICY
from app.models.knowledge_contributions import AttemptToken, inventory_digest
from app.models.text_analysis_jobs import EnrollmentIntent, ExecutionIntent, ExecutionParameters, JobPlan
from app.persistence.text_analysis_authority import FixtureTextAnalysisLifecycle, TextAnalysisCurrentEvidence, trust_digest
from app.persistence.text_analysis_jobs import TextAnalysisJobStore
from app.security.runtime_policy import AuthContext
from app.security.text_analysis_contract import canonical
from app.security.text_analysis_policy import encode_run_context
from tests.continuous_analytics_fixture import ACCOUNT, advance, cleanup, insert_message, make_fixture
from tests.test_knowledge_contributions import data as contribution_data
from tests.test_text_analysis_execution_authority import basis as authority_basis, CASES

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def execution(contribution_data, authority_basis):
    f, repo, original, members, profile, frames, _ = contribution_data
    auth, authority, _, _, original_context, _, _, schemas = authority_basis

    def make(kind='license', *, one=False):
        case = next(c for c in CASES if c['case_id'] == kind + '-accepted')
        trust = authority_basis[6].trust
        ref = authority.import_verified_reference(case['token'], kind=kind, context=case['context'], trust=trust, schemas=schemas)
        context = replace(original_context, reference_id=ref.reference_id, compact_jws_sha256=ref.compact_jws_sha256,
                          creator_account_id=ACCOUNT, algorithm_profile_sha256=profile.identity_digest())
        policy = replace(auth.build_runtime_policy(AuthContext('person', ACCOUNT, 'creator')),
                         identity_authority=authority_basis[5].identity_authority, text_analysis_authority=ref)
        with auth.database.transaction() as db:
            db.execute("INSERT OR REPLACE INTO nlp_dependency_profiles VALUES(?,?,?,'available')",
                       (profile.identity_digest(), b'{}', context.algorithm_artifacts_sha256))
            reference_version = db.execute('SELECT version FROM text_analysis_authority_references WHERE reference_id=?',
                                           (ref.reference_id,)).fetchone()[0]
        evidence = TextAnalysisCurrentEvidence(trust, trust_digest(trust), policy.authorization_epoch.value,
            reference_version, 2, 'd'*64, 'e'*64, hashlib.sha256(encode_run_context(context)).hexdigest(), None,
            context.seat_id, context.installation_key_id, context.installation_key_jkt, True)
        selected = members[:1] if one else members
        input_identity = original.input.model_copy(update={'inventory_sha256': inventory_digest(selected), 'source_count': len(selected)})
        plan = JobPlan(parameter_sha256=original.parameter_sha256, input=input_identity,
            algorithm_profile_sha256=profile.identity_digest(), policy_identity_sha256=context.policy_identity_sha256,
            algorithm_artifacts_sha256=context.algorithm_artifacts_sha256,
            model_or_rules_identity_sha256=original.model_or_rules_identity_sha256)
        intent = ExecutionIntent(external_key=str(uuid4()), authority_kind=kind,
                                 run_context=encode_run_context(context), scope_sha256='7'*64,
                                 preview_selection_policy=SELECTION_POLICY if kind == 'preview' else None)
        store = TextAnalysisJobStore(repo)
        lifecycle = FixtureTextAnalysisLifecycle(auth.database, repo.database)
        if kind == 'preview':
            with authority.transition_fence(policy, context, kind=kind, phase='import', evidence=evidence,
                    lifecycle=lifecycle, fixture_only=True) as current, repo.database.transaction() as db:
                store.import_preview_in(db, intent.run_context, policy=current)
        activated = authority.activate_reference(policy, context, kind=kind, evidence=evidence,
                                                  lifecycle=lifecycle, fixture_only=True)
        policy, evidence = activated.policy, activated.evidence

        @contextmanager
        def held():
            with authority.transition_fence(policy, context, kind=kind, phase='start', evidence=evidence,
                    lifecycle=lifecycle, fixture_only=True) as current:
                yield current

        return SimpleNamespace(f=f, repo=repo, store=store, context=context, intent=intent, plan=plan,
                               members=selected, profile=profile, frames=frames, policy=policy, held=held, ref=ref)
    return make


def prepare(x):
    with x.repo.database.transaction() as db:
        return x.store.prepare_in(db, x.intent, x.plan, x.members, profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy)


def activate(x, receipt):
    with x.held() as current, x.repo.database.transaction() as db:
        if x.intent.authority_kind == 'preview':
            x.store.import_preview_in(db, x.intent.run_context, policy=current)
        return x.store.activate_in(db, receipt.job_id, confirmation=receipt.confirmation, policy=current)


@pytest.mark.parametrize('kind', ['license', 'preview'])
def test_prepare_is_durable_idempotent_and_never_claimable(execution, kind):
    x = execution(kind)
    first = prepare(x)
    assert prepare(x) == first and first.state == 'prepared'
    with x.repo.database.read() as db:
        spec = json.loads(db.execute('SELECT spec FROM knowledge_jobs WHERE job_id=?', (first.job_id,)).fetchone()[0])
        assert spec['request_id'] == first.job_id and spec['contribution_id'] == first.contribution_id
        assert db.execute('SELECT count(*) FROM knowledge_sources WHERE job_id=?', (first.job_id,)).fetchone()[0] == len(x.members)
    with x.held() as current:
        with pytest.raises(ValueError):
            x.repo.claim_attempt(first.job_id, expected_fence=0, lease_seconds=60, policy=current)
    changed = x.intent.model_copy(update={'scope_sha256': '8'*64})
    with pytest.raises(ValueError, match='intent_conflict'), x.repo.database.transaction() as db:
        x.store.prepare_in(db, changed, x.plan, x.members, profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy)


def test_prepare_and_activation_rollback_share_owning_transaction(execution):
    x = execution('preview')
    with pytest.raises(RuntimeError), x.repo.database.transaction() as db:
        x.store.prepare_in(db, x.intent, x.plan, x.members, profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy)
        raise RuntimeError('rollback')
    with x.repo.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_intents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] == 0
    first = prepare(x)
    with x.held() as current:
        with x.repo.database.transaction() as db:
            x.store.import_preview_in(db, x.intent.run_context, policy=current)
        with pytest.raises(RuntimeError), x.repo.database.transaction() as db:
            x.store.activate_in(db, first.job_id, confirmation=first.confirmation, policy=current)
            raise RuntimeError('rollback')
    with x.repo.database.read() as db:
        assert x.store.read_in(db, first.job_id, policy=x.policy).state == 'prepared'
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'available'
        assert db.execute('SELECT confirmed_sha256 FROM text_analysis_intents').fetchone()[0] is None


def test_unrelated_additions_do_not_recreate_or_enlarge_prepared_job(execution):
    x = execution()
    first = prepare(x)
    with x.repo.database.transaction() as db:
        insert_message(db, 'chat-0', 'new-source', x.f.clock.now)
        advance(db)
    assert activate(x, first).state == 'pending'
    with x.held() as current:
        token = x.repo.claim_attempt(first.job_id, expected_fence=0, lease_seconds=60, policy=current)
        for i, frame in enumerate(x.frames):
            x.repo.stage_member(token, i, frame, policy=current)
        with pytest.raises(RuntimeError), x.repo.database.transaction() as db:
            x.repo.commit_in(db, token, policy=current)
            raise RuntimeError('rollback')
        with x.repo.database.read() as db:
            assert db.execute('SELECT count(*) FROM knowledge_contributions').fetchone()[0] == 0
        manifest = x.repo.commit(token, policy=current)
    assert manifest.member_count == 2
    assert x.repo.get_committed(first.contribution_id, policy=x.policy) == manifest


def test_module_write_cannot_use_starter_or_unfenced_policy(execution):
    x = execution()
    first = prepare(x)
    with pytest.raises(ValueError, match='fence_missing'), x.repo.database.transaction() as db:
        x.store.activate_in(db, first.job_id, policy=x.policy)
    with pytest.raises(ValueError), x.repo.database.transaction() as db:
        missing = replace(x.policy, text_analysis_authority=None)
        x.store.activate_in(db, first.job_id, policy=missing)
    with x.repo.database.read() as db:
        assert x.store.read_in(db, first.job_id, policy=missing).state == 'prepared'


@pytest.mark.parametrize('phase', ['stage', 'commit'])
def test_lease_expiry_during_record_validation_cannot_write_output(execution, monkeypatch, phase):
    x = execution()
    first = prepare(x)
    activate(x, first)
    original = x.repo.source_guard.record
    def elapsed(profile, source, frame):
        result = original(profile, source, frame)
        x.f.clock.now += timedelta(seconds=61)
        return result
    with x.held() as current:
        token = x.repo.claim_attempt(first.job_id, expected_fence=0, lease_seconds=60, policy=current)
        if phase == 'commit':
            for i, frame in enumerate(x.frames):
                x.repo.stage_member(token, i, frame, policy=current)
        monkeypatch.setattr(x.repo.source_guard, 'record', elapsed)
        with pytest.raises(ValueError, match='attempt_stale'):
            if phase == 'stage':
                x.repo.stage_member(token, 0, x.frames[0], policy=current)
            else:
                x.repo.commit(token, policy=current)
        with x.repo.database.read() as db:
            assert db.execute('SELECT count(*) FROM knowledge_contributions').fetchone()[0] == 0
            if phase == 'stage':
                assert db.execute('SELECT count(*) FROM knowledge_members').fetchone()[0] == 0


def test_prepare_still_requires_exact_live_input_head_and_rolls_back_receipt(execution):
    x = execution()
    with x.repo.database.transaction() as db:
        advance(db)
    with pytest.raises(ValueError, match='input_changed'):
        prepare(x)
    with x.repo.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_intents').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] == 0


@pytest.mark.parametrize('changed', ['text', 'expiry'])
def test_selected_source_change_or_expiry_refuses_activation(execution, changed):
    x = execution('preview')
    first = prepare(x)
    if changed == 'text':
        with x.repo.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='changed' WHERE message_id='m-0-0'")
        with x.repo.database.read() as db:
            assert x.store.read_in(db, first.job_id, policy=x.policy).state == 'result_deleted'
    else:
        x.f.clock.now += timedelta(days=1000)
    with pytest.raises(ValueError):
        activate(x, first)


def test_preview_confirmation_and_reservation_cannot_be_substituted(execution):
    x = execution('preview')
    first = prepare(x)
    with x.held() as current:
        with x.repo.database.transaction() as db:
            assert x.store.import_preview_in(db, x.intent.run_context, policy=current) == 'available'
        with pytest.raises(ValueError, match='confirmation_mismatch'), x.repo.database.transaction() as db:
            x.store.activate_in(db, first.job_id, confirmation=first.confirmation+b' ', policy=current)
    activate(x, first)
    with x.held() as current, x.repo.database.transaction() as db:
        assert x.store.import_preview_in(db, x.intent.run_context, policy=current) == 'reserved'
    with x.held() as current:
        token = x.repo.claim_attempt(first.job_id, expected_fence=0, lease_seconds=60, policy=current)
        with pytest.raises(ValueError, match='preview_output_required'):
            x.repo.commit(token, policy=current)
        with pytest.raises(ValueError, match='preview_output_required'):
            x.repo.stage_member(token, 0, x.frames[0], policy=current)


def preview_result(x, receipt, *, findings):
    from app.analytics.nlp_preview import PreviewAccumulator
    with x.repo.database.read() as db:
        _, spec, profile = x.repo._job(db, receipt.job_id, x.policy)
        sources = tuple(x.repo._sources(db, receipt.job_id))
        accumulator = PreviewAccumulator(spec, profile, sources)
        for member, raw in zip(sources, x.frames):
            source = x.repo._source(db, spec, member, x.repo._budget())
            if findings:
                value = json.loads(raw)
                for task in value['tasks']:
                    payload = task['result']['payload']
                    if payload['task'] == 'message_topics':
                        component = profile.component(task['result']['component_id'])
                        category = next(t.enabled_ids[0] for t in component.tasks if t.task == 'message_topics')
                        payload.update(outcome='assessed_findings', findings=[{'topic_id':category}])
                raw = json.dumps(value).encode()
            accumulator.add(member.ordinal, source, raw)
        return accumulator.finish()


def test_preview_output_spend_and_job_completion_are_one_transaction(execution):
    from app.analytics.nlp_preview import encode_preview
    x = execution('preview')
    first = prepare(x)
    activate(x, first)
    output = preview_result(x, first, findings=True)
    frame = encode_preview(output)
    with x.held() as current:
        token = x.repo.claim_attempt(first.job_id, expected_fence=0, lease_seconds=60, policy=current)
        with pytest.raises(RuntimeError), x.repo.database.transaction() as db:
            x.repo.commit_preview_in(db, token, frame, policy=current)
            raise RuntimeError('rollback')
        with x.repo.database.read() as db:
            assert db.execute('SELECT count(*) FROM text_analysis_preview_outputs').fetchone()[0] == 0
            assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'reserved'
            assert db.execute('SELECT state FROM knowledge_jobs').fetchone()[0] == 'running'
        with x.repo.database.transaction() as db:
            assert x.repo.commit_preview_in(db, token, frame, policy=current) == output
        with x.repo.database.transaction() as db:
            assert x.repo.commit_preview_in(db, token, frame, policy=current) == output
            assert x.store.import_preview_in(db, x.intent.run_context, policy=current) == 'spent'
        with x.repo.database.read() as db:
            assert db.execute('SELECT count(*) FROM knowledge_members').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM knowledge_contributions').fetchone()[0] == 0
            saved = bytes(db.execute('SELECT document FROM text_analysis_preview_outputs').fetchone()[0])
            assert saved == frame and b'nlp-result.v2' not in saved and b'analyzed_at' not in saved
            assert x.store.read_in(db, first.job_id, policy=x.policy).state == 'committed'
        with x.repo.database.transaction() as db:
            assert x.store.activate_in(db, first.job_id, confirmation=first.confirmation, policy=current).state == 'committed'


def test_preview_retirement_removes_output_but_never_replenishes_spend(execution):
    from app.analytics.nlp_preview import encode_preview
    from app.persistence.retention import CreatorVaultRetention
    x = execution('preview')
    first = prepare(x)
    activate(x, first)
    frame = encode_preview(preview_result(x, first, findings=True))
    with x.held() as current:
        token = x.repo.claim_attempt(first.job_id, expected_fence=0, lease_seconds=60, policy=current)
        with x.repo.database.transaction() as db:
            x.repo.commit_preview_in(db, token, frame, policy=current)
    CreatorVaultRetention(x.repo.database, clock=lambda:x.f.clock.now).delete_message(ACCOUNT, 'm-0-0')
    with x.repo.database.read() as db:
        receipt = x.store.read_in(db, first.job_id, policy=x.policy)
        assert receipt.state == 'result_deleted' and receipt.contribution_id == first.contribution_id
        assert receipt.confirmation is None and receipt.terminal_reason == 'source_retired'
        assert db.execute('SELECT count(*) FROM text_analysis_preview_outputs').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM knowledge_sources').fetchone()[0] == 0
        assert db.execute('SELECT state,contribution_id FROM text_analysis_preview_admissions').fetchone()[:] == ('spent', first.contribution_id)
    assert prepare(x) == receipt
    with x.held() as current, x.repo.database.transaction() as db:
        assert x.store.import_preview_in(db, x.intent.run_context, policy=current) == 'spent'


def test_empty_preview_releases_without_output_or_spend_and_cannot_restart_old_job(execution):
    x = execution('preview')
    first = prepare(x)
    activate(x, first)
    outcome = preview_result(x, first, findings=False)
    assert outcome.disposition == 'empty'
    with x.held() as current:
        token = x.repo.claim_attempt(first.job_id, expected_fence=0, lease_seconds=60, policy=current)
        with x.repo.database.transaction() as db:
            receipt = x.repo.finish_without_output_in(db, token, outcome, policy=current)
            assert receipt.state == 'failed'
        with x.repo.database.read() as db:
            assert db.execute('SELECT count(*) FROM text_analysis_preview_outputs').fetchone()[0] == 0
            assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'available'
        with pytest.raises(ValueError), x.repo.database.transaction() as db:
            x.store.activate_in(db, first.job_id, confirmation=first.confirmation, policy=current)
        with pytest.raises(ValueError):
            x.repo.claim_attempt(first.job_id, expected_fence=1, lease_seconds=60, policy=current)


def test_child_slots_survive_restart_additions_pause_and_account_deletion(execution):
    from app.persistence.retention import CreatorVaultRetention
    x = execution(one=True)
    params = ExecutionParameters.model_validate({k:v for k,v in x.plan.model_dump().items() if k!='input'})
    enrollment_intent = EnrollmentIntent(external_key=str(uuid4()), run_context=x.intent.run_context, parameters=params)
    with x.repo.database.transaction() as db:
        parent = x.store.enroll_in(db, enrollment_intent, policy=x.policy)
        first = x.store.prepare_child_in(db, parent.enrollment_id, x.plan, x.members,
                                        profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy)
    with x.repo.database.transaction() as db:
        insert_message(db, 'chat-0', 'new-source', x.f.clock.now)
        advance(db)
    changed_input = x.plan.input.model_copy(update={'canonical_revision': 2, 'canonical_content_sha256': '9'*64})
    new_plan = x.plan.model_copy(update={'input': changed_input})
    new_members = [m.model_copy(update={'source_revision': 2}) for m in x.members]
    reopened = TextAnalysisJobStore(x.repo)
    with x.repo.database.transaction() as db:
        assert reopened.prepare_child_in(db, parent.enrollment_id, new_plan, new_members,
                                        profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy) == first
        reopened.set_enrollment_mode_in(db, parent.enrollment_id, 'paused', policy=x.policy)
    with pytest.raises(ValueError, match='enrollment_unavailable'), x.repo.database.transaction() as db:
        reopened.prepare_child_in(db, parent.enrollment_id, x.plan, x.members,
                                  profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy)
    with x.held() as current, x.repo.database.transaction() as db:
        reopened.set_enrollment_mode_in(db, parent.enrollment_id, 'enabled', policy=current)
        assert reopened.activate_in(db, first.job_id, policy=current).state == 'pending'
    CreatorVaultRetention(x.repo.database, clock=lambda:x.f.clock.now).delete_all(ACCOUNT)
    with x.repo.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_enrollments').fetchone()[0] == 'retired'
        assert reopened.read_in(db, first.job_id, policy=x.policy).state == 'result_deleted'
        assert db.execute('SELECT count(*) FROM knowledge_sources').fetchone()[0] == 0
    with pytest.raises(ValueError, match='enrollment_retired'), x.repo.database.transaction() as db:
        reopened.set_enrollment_mode_in(db, parent.enrollment_id, 'enabled', policy=x.policy)
    with x.repo.database.transaction() as db:
        replacement = reopened.enroll_in(db, enrollment_intent.model_copy(update={'external_key':str(uuid4())}), policy=x.policy)
        assert replacement.enrollment_id != parent.enrollment_id


def test_ports_require_a_held_transaction_and_closed_plans(execution):
    x = execution()
    with x.repo.database.read() as db, pytest.raises(ValueError, match='transaction_required'):
        x.store.prepare_in(db, x.intent, x.plan, x.members, profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy)
    with pytest.raises(ValueError):
        JobPlan.model_validate(dict(x.plan.model_dump(), job_id=str(uuid4())))


def canonical_rows(database):
    with database.read() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: sorted((tuple(row) for row in db.execute(f'SELECT * FROM "{table}"')), key=repr) for table in tables}


def test_foreign_held_connection_cannot_read_or_mutate_owned_repository(execution, tmp_path):
    x = execution()
    first = prepare(x)
    foreign = make_fixture(tmp_path/'foreign', conversations=1, messages=2)
    try:
        original = canonical_rows(x.repo.database), canonical_rows(foreign.repositories.database)
        with x.held() as current, foreign.repositories.database.transaction() as db:
            operations = [
                lambda: x.store.prepare_in(db, x.intent, x.plan, x.members,
                    profile_bytes=x.profile.model_dump_json().encode(), policy=current),
                lambda: x.store.activate_in(db, first.job_id, policy=current),
                lambda: x.repo.claim_attempt_in(db, first.job_id, expected_fence=0, lease_seconds=60, policy=current),
                lambda: x.store.read_in(db, first.job_id, policy=current),
                lambda: x.store.read_key_in(db, x.intent.external_key, policy=current),
                lambda: x.store.read_plan_in(db, first.job_id, policy=current),
                lambda: x.store.read_enrollment_in(db, str(uuid4()), policy=current),
                lambda: x.store.list_enrollments_in(db, policy=current),
                lambda: x.store.list_children_in(db, str(uuid4()), policy=current),
            ]
            for operation in operations:
                with pytest.raises(ValueError, match='connection_unowned'):
                    operation()
        closed = x.repo.database.connect()
        closed.close()
        with pytest.raises(ValueError, match='connection_unowned'):
            x.repo._transaction(closed)
        detached = x.repo.database.open_detached(x.repo.database.path, read_only=True)
        try:
            with pytest.raises(ValueError, match='connection_unowned'):
                x.store.read_in(detached, first.job_id, policy=x.policy)
        finally:
            detached.close()
        assert (canonical_rows(x.repo.database), canonical_rows(foreign.repositories.database)) == original
    finally:
        cleanup(foreign)


@pytest.mark.parametrize('kind', ['license', 'preview'])
def test_owned_connection_rejects_fence_for_another_canonical_owner(execution, tmp_path, kind):
    from app.analytics.nlp_contribution_sources import ContributionSourceGuard
    from app.persistence.contributions import ContributionRepository
    x = execution(kind)
    foreign = make_fixture(tmp_path/'foreign', conversations=1, messages=2)
    try:
        repo = ContributionRepository(foreign.repositories.database, source_guard=ContributionSourceGuard(), clock=x.repo.clock)
        store = TextAnalysisJobStore(repo)
        with repo.database.transaction() as db:
            first = store.prepare_in(db, x.intent, x.plan, x.members,
                profile_bytes=x.profile.model_dump_json().encode(), policy=x.policy)
        token = AttemptToken(job_id=first.job_id, attempt_fence=1, job_attempt_id=str(uuid4()),
                             lease_expires_at=float(repo.clock()+60))
        original = canonical_rows(x.repo.database), canonical_rows(repo.database)
        with x.held() as current, repo.database.transaction() as db:
            operations = [
                lambda: store.activate_in(db, first.job_id, confirmation=first.confirmation, policy=current),
                lambda: repo.claim_attempt_in(db, first.job_id, expected_fence=0, lease_seconds=60, policy=current),
                lambda: repo.stage_member_in(db, token, 0, x.frames[0], policy=current),
                lambda: repo.commit_in(db, token, policy=current),
            ]
            if kind == 'preview':
                operations += [
                    lambda: store.import_preview_in(db, x.intent.run_context, policy=current),
                    lambda: repo.commit_preview_in(db, token, b'{}', policy=current),
                    lambda: repo.finish_without_output_in(db, token, None, policy=current),
                ]
            for operation in operations:
                with pytest.raises(ValueError, match='canonical_owner_mismatch'):
                    operation()
        assert (canonical_rows(x.repo.database), canonical_rows(repo.database)) == original
    finally:
        cleanup(foreign)


@pytest.mark.parametrize('interrupt', [False, True])
def test_populated_v14_upgrade_preserves_exact_rows_and_rolls_back_failure(contribution_data, tmp_path, interrupt):
    from app.persistence.database import CanonicalSQLite
    from app.persistence.contributions import storage_high_water
    from app.persistence.migrations import CANONICAL_MIGRATIONS_DIR, MigrationRunner, load_migration_catalog
    from tests.test_knowledge_contributions import seal
    seal(contribution_data)
    f = contribution_data[0]
    catalog = tmp_path/'v14'
    catalog.mkdir()
    for path in CANONICAL_MIGRATIONS_DIR.glob('*.sql'):
        if int(path.name[:4]) <= 14:
            (catalog/path.name).write_bytes(path.read_bytes())
    database = CanonicalSQLite(tmp_path/'upgrade.sqlite3')
    MigrationRunner(database, migrations_dir=catalog).run()
    tables = ('knowledge_store_identity', 'knowledge_retirement_epochs', 'knowledge_jobs',
              'knowledge_attempts', 'knowledge_sources', 'knowledge_members',
              'knowledge_contributions', 'knowledge_retired_intents')
    with f.repositories.database.read() as source, database.transaction() as target:
        for table in ('account_heads', 'account_chats', 'account_messages') + tables:
            rows = source.execute(f'SELECT * FROM {table}').fetchall()
            target.execute(f'DELETE FROM {table}')
            if rows:
                target.executemany(f'INSERT INTO {table} VALUES({",".join("?" for _ in rows[0])})',
                                   [tuple(r) for r in rows])
    def snapshot(db):
        return {table:[tuple(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1,2')] for table in tables}
    with database.read() as db:
        before = snapshot(db)
        before_high_water = storage_high_water(db)
        ledger = [tuple(r) for r in db.execute('SELECT * FROM schema_migrations ORDER BY version')]
    if interrupt:
        migration = load_migration_catalog()[-1]
        with database.read() as db, pytest.raises(Exception):
            MigrationRunner._apply(db, replace(migration, sql=migration.sql+'\nSELECT missing_migration_column;'))
        with database.read() as db:
            assert snapshot(db) == before
            assert db.execute('PRAGMA user_version').fetchone()[0] == 14
            assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'migration15_%'").fetchone()[0] == 0
    assert MigrationRunner(database).run() == [15]
    with database.read() as db:
        assert snapshot(db) == before
        assert storage_high_water(db) == before_high_water
        assert [tuple(r) for r in db.execute('SELECT * FROM schema_migrations WHERE version<=14 ORDER BY version')] == ledger
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'migration15_%'").fetchone()[0] == 0
    assert MigrationRunner(database).run() == []
