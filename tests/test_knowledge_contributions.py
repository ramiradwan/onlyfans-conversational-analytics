"""Invented typed results against real canonical and disposable SQLite stores."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.analytics.canonical_source import read_transaction_evidence
from app.analytics.nlp_contribution_sources import ContributionSourceGuard
from app.analytics.database import ProjectionsDatabase
from app.analytics.nlp_contribution_replay import replay_contribution
from app.analytics.nlp_findings_store import FindingsStore, source_binding
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.core.config import settings
from app.models.knowledge_contributions import (
    CapabilityBinding, InputIdentity, JobSpec, SourceMemberSpec, inventory_digest, members_digest,
)
from app.nlp import v2
from app.nlp.profiles import read_profile
from app.persistence.backup import backup_canonical_database, verify_backup
from app.persistence.contributions import ContributionRepository
from app.persistence.retention import CreatorVaultRetention
from app.persistence.retention_restore import restore_canonical_with_deletion_barriers
from app.security.runtime_policy import AuthContext, AuthorizationEpoch, IdentityAccountAuthority, RuntimePolicy
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
PROFILE = Path(__file__).resolve().parents[1] / 'shared/nlp/v2/fixtures/joint-profile.json'


@pytest.fixture
def data(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=2)
    profile = read_profile(PROFILE.read_bytes())
    policy = RuntimePolicy(AuthContext('principal', ACCOUNT, 'creator'), AuthorizationEpoch(1),
        identity_authority=IdentityAccountAuthority('organization', 'installation', 'key', 'jkt', (), ()))
    repository = ContributionRepository(f.repositories.database, source_guard=ContributionSourceGuard(), clock=lambda: f.clock.now.timestamp())
    budget = QuestionBudget(QuestionLimits(max_records=100_000, wall_clock_ms=30_000))
    with f.repositories.database.read() as db:
        sources = [read_transaction_evidence(db, ACCOUNT, {'conversation_id': 'chat-0', 'message_id': f'm-0-{i}'}, budget)
                   for i in range(2)]
    identity = f.source.read_identity(ACCOUNT)
    members = [SourceMemberSpec(ordinal=i, location=s.location.model_dump(), source=source_binding(s), source_revision=s.source_revision,
                               text_sha256=hashlib.sha256(s.text.encode()).hexdigest()) for i, s in enumerate(sources)]
    job_id = str(uuid4())
    spec = JobSpec(request_id=job_id, job_id=job_id, contribution_id=str(uuid4()), installation_id='installation',
        organization_id='organization', creator_account_id=ACCOUNT,
        capability=CapabilityBinding(capability_id='invented', artifact_family='invented-results', artifact_major=1,
            object_sha256='1' * 64, object_reference_id='invented-reference', profile_id='invented', profile_sha256='2' * 64),
        parameter_sha256='3' * 64, policy_identity_sha256='4' * 64,
        algorithm_artifacts_sha256='5' * 64, model_or_rules_identity_sha256='6' * 64,
        input=InputIdentity(canonical_revision=identity.revision,
            canonical_content_sha256=identity.content_digest.removeprefix('sha256:'),
            inventory_sha256=inventory_digest(members), source_count=len(members)),
        algorithm_profile_sha256=profile.identity_digest())
    frames = []
    for source in sources:
        tasks = []
        for binding in profile.bindings:
            component = profile.component(binding.component_id)
            task = dict(task=binding.task, outcome='assessed_no_match', reason='none', findings=[],
                        coverage=dict(source_codepoints=len(source.text), examined=[dict(start=0, end=len(source.text))],
                                      excluded=[], limits_reached=[]))
            tasks.append(dict(source=source_binding(source).model_dump(), profile_sha256=profile.identity_digest(),
                result=dict(component_id=component.component_id, component_sha256=component.identity_digest(),
                            text_sha256=hashlib.sha256(source.text.encode()).hexdigest(), payload=task)))
        frames.append(v2.EnrichmentRecord(source=source_binding(source), profile=profile,
                     analyzed_at='2026-09-18T12:00:00Z', tasks=tuple(v2.StoredTask.model_validate_json(json.dumps(t)) for t in tasks)).model_dump_json().encode())
    yield f, repository, spec, members, profile, frames, policy
    cleanup(f)


def create(data):
    _, repo, spec, members, profile, _, policy = data
    return repo.create_job(spec, members, profile_bytes=profile.model_dump_json().encode(), policy=policy)


def seal(data):
    _, repo, spec, _, _, frames, policy = data
    create(data)
    token = repo.claim_attempt(spec.job_id, expected_fence=0, lease_seconds=60, policy=policy)
    for ordinal, frame in enumerate(frames):
        repo.stage_member(token, ordinal, frame, policy=policy)
    return token, repo.commit(token, policy=policy)


def test_lost_create_response_reuses_durable_intent_and_conflicts(data):
    f, repo, spec, members, profile, _, policy = data
    create(data)  # The response is deliberately discarded.
    reopened = ContributionRepository(f.repositories.database, source_guard=ContributionSourceGuard(), clock=repo.clock)
    assert reopened.create_job(spec, members, profile_bytes=profile.model_dump_json().encode(), policy=policy)['job_id'] == spec.job_id
    changed = spec.model_copy(update={'parameter_sha256': '7' * 64})
    with pytest.raises(ValueError, match='intent_conflict'):
        reopened.create_job(changed, members, profile_bytes=profile.model_dump_json().encode(), policy=policy)
    new_id = str(uuid4())
    rerun = spec.model_copy(update={'request_id': new_id, 'job_id': new_id, 'contribution_id': str(uuid4())})
    assert reopened.create_job(rerun, members, profile_bytes=profile.model_dump_json().encode(), policy=policy)['job_id'] != spec.job_id


def test_stale_attempt_and_reused_member_preserve_original_producer(data):
    f, repo, spec, _, _, frames, policy = data
    create(data)
    first = repo.claim_attempt(spec.job_id, expected_fence=0, lease_seconds=1, policy=policy)
    original = repo.stage_member(first, 0, frames[0], policy=policy)
    f.clock.now += timedelta(seconds=2)
    second = repo.claim_attempt(spec.job_id, expected_fence=1, lease_seconds=60, policy=policy)
    with pytest.raises(ValueError, match='attempt_stale'):
        repo.stage_member(first, 1, frames[1], policy=policy)
    with pytest.raises(ValueError, match='attempt_stale'):
        repo.commit(first, policy=policy)
    assert repo.stage_member(second, 0, frames[0], policy=policy) == original
    newer = repo.stage_member(second, 1, frames[1], policy=policy)
    manifest = repo.commit(second, policy=policy)
    assert manifest.attempt_fence == 2
    assert manifest.ordered_members_sha256 == members_digest([original, newer])
    assert repo.commit(second, policy=policy) == manifest
    exported = repo.export_document(spec.contribution_id, policy=policy)
    assert [m['identity']['producing_attempt_fence'] for m in exported['members']] == [1, 2]
    assert exported['members'][0]['identity']['producing_job_attempt_id'] == first.job_attempt_id
    assert exported['manifest']['job_attempt_id'] == second.job_attempt_id


def test_staging_incomplete_or_conflicting_results_are_not_committed(data):
    _, repo, spec, _, _, frames, policy = data
    create(data)
    token = repo.claim_attempt(spec.job_id, expected_fence=0, lease_seconds=60, policy=policy)
    repo.stage_member(token, 0, frames[0], policy=policy)
    with pytest.raises(ValueError, match='member_missing'):
        repo.commit(token, policy=policy)
    with pytest.raises(ValueError, match='contribution_unavailable'):
        repo.get_committed(spec.contribution_id, policy=policy)
    changed = frames[0].replace(b'12:00:00Z', b'12:00:01Z')
    with pytest.raises(ValueError, match='member_conflict'):
        repo.stage_member(token, 0, changed, policy=policy)


def test_crash_after_seal_replay_is_exact_idempotent_and_rebuilds_deleted_projection(data, tmp_path, monkeypatch):
    f, repo, spec, _, profile, frames, policy = data
    _, manifest = seal(data)  # No derived import before reopening the repository.
    repo = ContributionRepository(f.repositories.database, source_guard=ContributionSourceGuard(), clock=repo.clock)
    monkeypatch.setattr(settings, 'environment', 'production')
    assert repo.get_committed(spec.contribution_id, policy=policy) == manifest
    path = tmp_path / 'replay.sqlite3'
    for attempt in range(2):
        projections = ProjectionsDatabase(path)
        findings = FindingsStore(projections)
        findings.select(ACCOUNT, profile, mode='paused')
        page = replay_contribution(repo, findings, spec.contribution_id, policy=policy, limit=1)
        assert page['imported'] == 1 and not page['complete']
        # Lost page response redelivery neither changes completion nor repeats work.
        assert replay_contribution(repo, findings, spec.contribution_id, policy=policy, limit=1)['imported'] == 0
        assert replay_contribution(repo, findings, spec.contribution_id, policy=policy,
                                   after_ordinal=page['after_ordinal'])['complete']
        with projections.read() as db:
            saved = [bytes(r[0]) for r in db.execute('SELECT document FROM nlp_records ORDER BY record_digest')]
            assert saved == sorted(frames, key=lambda b: hashlib.sha256(b).hexdigest())
            assert db.execute('SELECT count(*) FROM nlp_jobs WHERE state="running"').fetchone()[0] == 0
        if attempt == 0:
            # All connections are context-owned and closed; delete only this fixture DB.
            path.unlink()
    with pytest.raises(ValueError, match='findings_execution_unavailable'):
        repo.create_job(spec, [], profile_bytes=profile.model_dump_json().encode(), policy=policy)


@pytest.mark.parametrize('kind', ['message', 'conversation', 'participant', 'account'])
def test_source_deletion_erases_whole_contribution_and_blocks_old_intent(data, kind):
    f, repo, spec, _, _, _, policy = data
    seal(data)
    retention = CreatorVaultRetention(f.repositories.database, clock=lambda: f.clock.now)
    getattr(retention, {'message': 'delete_message', 'conversation': 'delete_conversation',
                        'participant': 'delete_participant', 'account': 'delete_all'}[kind])(
        ACCOUNT, *({'message': ['m-0-0'], 'conversation': ['chat-0'], 'participant': ['synthetic-fan'], 'account': []}[kind]))
    with pytest.raises(ValueError, match='contribution_unavailable'):
        repo.get_committed(spec.contribution_id, policy=policy)
    with f.repositories.database.read() as db:
        assert db.execute('SELECT count(*) FROM knowledge_members').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM knowledge_sources').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM knowledge_attempts').fetchone()[0] == 0
    with pytest.raises(ValueError, match='intent_retired'):
        create(data)


def test_source_change_and_retention_expiry_do_not_reuse_stale_output(data):
    f, repo, spec, _, _, _, policy = data
    seal(data)
    f.clock.now += timedelta(days=1000)
    with pytest.raises(ValueError, match='source_expired'):
        repo.get_committed(spec.contribution_id, policy=policy)
    CreatorVaultRetention(f.repositories.database, clock=lambda: f.clock.now).enforce(ACCOUNT)
    with pytest.raises(ValueError, match='contribution_unavailable'):
        repo.get_committed(spec.contribution_id, policy=policy)


def test_wrong_account_and_producer_tampering_refuse(data):
    f, repo, spec, _, _, _, policy = data
    seal(data)
    with pytest.raises(ValueError):
        repo.get_committed(spec.contribution_id, policy=replace(policy, identity=AuthContext('other', 'other', 'creator')))
    with pytest.raises(Exception, match='knowledge_member_immutable'):
        with f.repositories.database.transaction() as db:
            db.execute('UPDATE knowledge_members SET producing_attempt_fence=2')


def test_backup_export_and_restore_respect_newer_deletion_barriers(data, tmp_path):
    f, repo, spec, _, _, frames, policy = data
    seal(data)
    backup = tmp_path / 'knowledge.backup.sqlite3'
    manifest = backup_canonical_database(f.repositories.database, backup)
    assert verify_backup(backup) == manifest
    assert manifest.high_water['knowledge_contributions']['rows'] > 0
    exported = repo.export_document(spec.contribution_id, policy=policy)
    assert [m['record_utf8'].encode() for m in exported['members']] == frames
    CreatorVaultRetention(f.repositories.database, clock=lambda: f.clock.now).delete_message(ACCOUNT, 'm-0-0')
    restore_canonical_with_deletion_barriers(backup, f.repositories.database.path, now=f.clock.now, overwrite=True)
    with pytest.raises(ValueError, match='contribution_unavailable'):
        repo.get_committed(spec.contribution_id, policy=policy)


def test_request_identity_and_unknown_fields_are_closed(data):
    _, _, spec, _, _, _, _ = data
    with pytest.raises(ValueError):
        JobSpec.model_validate({**spec.model_dump(), 'request_id': str(uuid4())})
    with pytest.raises(ValueError):
        JobSpec.model_validate({**spec.model_dump(), 'caller_uri': 'unused'})


def test_competing_claim_and_source_change_invalidate_staged_lineage(data):
    f, repo, spec, _, _, frames, policy = data
    create(data)
    token = repo.claim_attempt(spec.job_id, expected_fence=0, lease_seconds=60, policy=policy)
    with pytest.raises(ValueError, match='attempt_conflict'):
        ContributionRepository(f.repositories.database, source_guard=ContributionSourceGuard(), clock=repo.clock).claim_attempt(
            spec.job_id, expected_fence=0, lease_seconds=60, policy=policy)
    repo.stage_member(token, 0, frames[0], policy=policy)
    with f.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='changed source' WHERE creator_account_id=? AND message_id='m-0-0'", (ACCOUNT,))
    with pytest.raises(ValueError, match='job_unavailable'):
        repo.stage_member(token, 1, frames[1], policy=policy)
    with pytest.raises(ValueError, match='intent_retired'):
        create(data)


def test_producer_identity_changes_digest_and_sealed_state_cannot_reopen(data):
    f, repo, spec, _, _, _, policy = data
    seal(data)
    from app.models.knowledge_contributions import MemberIdentity
    exported = repo.export_document(spec.contribution_id, policy=policy)
    identities = [MemberIdentity.model_validate_json(json.dumps(m['identity'])) for m in exported['members']]
    changed = identities[0].model_copy(update={'producing_job_attempt_id': str(uuid4())})
    assert members_digest([changed, identities[1]]) != exported['manifest']['ordered_members_sha256']
    with pytest.raises(Exception, match='knowledge_job_terminal'):
        with f.repositories.database.transaction() as db:
            db.execute("UPDATE knowledge_jobs SET state='pending' WHERE job_id=?", (spec.job_id,))


def test_unrelated_account_revision_change_preserves_fixed_source_population(data):
    f, repo, spec, _, _, frames, policy = data
    create(data)
    token = repo.claim_attempt(spec.job_id, expected_fence=0, lease_seconds=60, policy=policy)
    from tests.continuous_analytics_fixture import insert_message, advance
    with f.repositories.database.transaction() as db:
        insert_message(db, 'chat-0', 'new-unselected-message', f.clock.now)
        advance(db)
    for ordinal, frame in enumerate(frames):
        repo.stage_member(token, ordinal, frame, policy=policy)
    manifest = repo.commit(token, policy=policy)
    assert manifest.member_count == 2
    assert repo.get_committed(spec.contribution_id, policy=policy) == manifest


def test_positive_typed_findings_and_stable_occurrence_ids_survive_export_and_replay(data, tmp_path):
    _, repo, spec, _, profile, frames, policy = data
    value = json.loads(frames[0])
    for task in value['tasks']:
        payload = task['result']['payload']
        descriptor = next(t for c in profile.components for t in c.tasks if t.task == payload['task'])
        payload['outcome'] = 'assessed_findings'
        payload['findings'] = ([{'topic_id': descriptor.enabled_ids[0]}] if payload['task'] == 'message_topics' else
                               [{'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}}])
    frames[0] = json.dumps(value).encode()
    seal(data)
    first = repo.export_document(spec.contribution_id, policy=policy)
    assert len(first['members'][0]['finding_ids']) == len(set(first['members'][0]['finding_ids'])) == 2
    findings = FindingsStore(ProjectionsDatabase(tmp_path / 'positive-replay.sqlite3'))
    findings.select(ACCOUNT, profile, mode='paused')
    replay_contribution(repo, findings, spec.contribution_id, policy=policy)
    assert repo.export_document(spec.contribution_id, policy=policy) == first
    with findings.database.read() as db:
        assert bytes(db.execute('SELECT document FROM nlp_records WHERE record_digest=?',
                                (hashlib.sha256(frames[0]).hexdigest(),)).fetchone()[0]) == frames[0]


def _positive(profile, frame):
    value = json.loads(frame)
    for task in value['tasks']:
        payload = task['result']['payload']
        descriptor = next(t for c in profile.components for t in c.tasks if t.task == payload['task'])
        payload['outcome'] = 'assessed_findings'
        payload['findings'] = ([{'topic_id': descriptor.enabled_ids[0]}] if payload['task'] == 'message_topics' else
                               [{'concept_id': descriptor.enabled_ids[0], 'span': {'start': 0, 'end': 6}}])
    return json.dumps(value).encode()


def _additional(data, message_ids):
    f, repo, spec, _, profile, frames, policy = data
    from tests.continuous_analytics_fixture import insert_message, advance
    with f.repositories.database.transaction() as db:
        for mid in message_ids:
            insert_message(db, 'chat-0', mid, f.clock.now)
        advance(db)
    budget = repo.source_guard.budget()
    with f.repositories.database.read() as db:
        sources = [read_transaction_evidence(db, ACCOUNT, {'conversation_id': 'chat-0', 'message_id': mid}, budget)
                   for mid in message_ids]
    identity = f.source.read_identity(ACCOUNT)
    members, results = [], []
    for ordinal, source in enumerate(sources):
        binding = source_binding(source)
        members.append(SourceMemberSpec(ordinal=ordinal, location=source.location.model_dump(), source=binding,
            source_revision=source.source_revision, text_sha256=hashlib.sha256(source.text.encode()).hexdigest()))
        frame = json.loads(_positive(profile, frames[0]))
        frame['source'] = binding.model_dump(mode='json')
        for task in frame['tasks']:
            task['source'] = binding.model_dump(mode='json')
        results.append(json.dumps(frame).encode())
    new_id = str(uuid4())
    other = spec.model_copy(update={'request_id': new_id, 'job_id': new_id, 'contribution_id': str(uuid4()),
        'input': InputIdentity(canonical_revision=identity.revision,
            canonical_content_sha256=identity.content_digest.removeprefix('sha256:'),
            inventory_sha256=inventory_digest(members), source_count=len(members))})
    repo.create_job(other, members, profile_bytes=profile.model_dump_json().encode(), policy=policy)
    token = repo.claim_attempt(other.job_id, expected_fence=0, lease_seconds=60, policy=policy)
    for ordinal, frame in enumerate(results):
        repo.stage_member(token, ordinal, frame, policy=policy)
    return other, repo.commit(token, policy=policy), results


@pytest.mark.parametrize('retirement', ['delete', 'edit'])
@pytest.mark.parametrize('partial', [False, True])
def test_origin_retirement_fences_saved_publication_freeze_and_bounded_erasure(data, retirement, partial):
    f, repo, spec, _, profile, frames, policy = data
    frames[:] = [_positive(profile, frame) for frame in frames]
    seal(data)
    other, _, other_frames = _additional(data, ['independent-d'])
    findings = FindingsStore(f.stores.database)
    findings.select(ACCOUNT, profile, mode='paused')
    replay_contribution(repo, findings, spec.contribution_id, policy=policy, limit=1 if partial else 64)
    replay_contribution(repo, findings, other.contribution_id, policy=policy)
    from app.analytics.nlp_findings_store import checked_snapshot, require_current_snapshot, bind_generation
    identity = f.source.read_identity(ACCOUNT)
    frozen = findings.freeze(ACCOUNT, identity)
    with f.stores.database.read() as db:
        assert len(checked_snapshot(db, frozen)['records']) == (2 if partial else 3)
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.nlp_findings_store import check_generation
    pipeline = AnalyticsPipeline(f.source, projections=f.stores.projections, enrichment=f.pipeline.enrichment,
        clock=lambda: f.clock.now, nlp_findings=frozen, nlp_authorize=lambda: None, nlp_admit=lambda: None)
    pipeline.rebuild_account(ACCOUNT)
    published = f.stores.database.active_generation(ACCOUNT).generation_id
    if retirement == 'delete':
        target = CreatorVaultRetention(f.repositories.database, clock=lambda: f.clock.now).delete_message(ACCOUNT, 'm-0-1' if partial else 'm-0-0')
    else:
        with f.repositories.database.transaction() as db:
            db.execute('UPDATE account_messages SET text=? WHERE creator_account_id=? AND message_id=?',
                       ('edited', ACCOUNT, 'm-0-1' if partial else 'm-0-0'))
        target = 0
    # No cleanup has run. The remaining old source is still live but its whole origin is retired.
    with f.stores.database.transaction() as db:
        generation = db.execute('SELECT * FROM projection_generations WHERE generation_id=?', (published,)).fetchone()
        with pytest.raises(ValueError, match='origin_retired'):
            check_generation(db, generation, current=True, verify_records=False)
        with pytest.raises(ValueError, match='origin_retired'):
            check_generation(db, generation, current=False)
        with pytest.raises(ValueError, match='origin_retired'):
            checked_snapshot(db, frozen)
        with pytest.raises(ValueError, match='origin_retired'):
            require_current_snapshot(db, frozen)
        with pytest.raises(ValueError, match='origin_retired'):
            bind_generation(db, 'not-published', frozen, 'base', identity)
    current = findings.freeze(ACCOUNT, f.source.read_identity(ACCOUNT))
    with f.stores.database.read() as db:
        value = checked_snapshot(db, current)
        assert value['records'] == [hashlib.sha256(other_frames[0]).hexdigest()]
        assert all(v['assessed'] == 1 and v['missing'] >= 1 for v in value['coverage'].values())
    # A reopened projection has no implicit access to a stored canonical path.
    reopened = ProjectionsDatabase(f.stores.database.path)
    with pytest.raises(ValueError, match='checker_unavailable'):
        FindingsStore(reopened).freeze(ACCOUNT, f.source.read_identity(ACCOUNT))
    reopened.bind_contribution_origins(repo.origin_is_live, repo.origin_epoch)
    assert FindingsStore(reopened).freeze(ACCOUNT, f.source.read_identity(ACCOUNT)) == current
    from app.analytics.nlp_erasure import FindingsErasure
    erasure = FindingsErasure(f.source, f.stores.projections, clock=lambda: f.clock.now)
    for _ in range(80):
        if erasure.step(ACCOUNT, target, account_wide=0):
            break
    else:
        pytest.fail('bounded erasure did not finish')
    with f.stores.database.read() as db:
        assert [bytes(r[0]) for r in db.execute('SELECT document FROM nlp_records')] == other_frames
    assert replay_contribution(repo, findings, other.contribution_id, policy=policy)['imported'] == 0


def test_origin_lookup_uses_digest_index_constant_query_count_and_fresh_ttl(data, monkeypatch):
    f, repo, _, _, _, _, policy = data
    spec, manifest, frames = _additional(data, [f'bounded-{i}' for i in range(16)])
    from app.models.knowledge_contributions import record_digest
    from app.analytics.opaque_refs import account_ref
    with f.repositories.database.read() as db:
        store = db.execute('SELECT store_id FROM knowledge_store_identity').fetchone()[0]
        plan = ' '.join(str(tuple(r)) for r in db.execute("EXPLAIN QUERY PLAN SELECT document FROM knowledge_members INDEXED BY knowledge_member_digest "
            "WHERE job_id=? AND json_extract(identity,'$.record_sha256')=?", (spec.job_id, hashlib.sha256(frames[0]).hexdigest())))
        assert 'knowledge_member_digest' in plan
    queries = []
    connect = f.repositories.database.connect
    def traced():
        db = connect()
        db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(f.repositories.database, 'connect', traced)
    for frame in frames:
        before = len(queries)
        assert repo.origin_is_live(store, spec.contribution_id, record_digest(manifest), account_ref(ACCOUNT), hashlib.sha256(frame).hexdigest()) == 1
        statements = queries[before:]
        assert len([q for q in statements if q.lstrip().upper().startswith('SELECT')]) <= 6
        assert not any('FROM knowledge_sources s LEFT JOIN account_messages' in q for q in statements)
    f.clock.now += timedelta(days=91)
    assert repo.origin_is_live(store, spec.contribution_id, record_digest(manifest), account_ref(ACCOUNT), hashlib.sha256(frames[0]).hexdigest()) == 0


@pytest.mark.parametrize('lazy', [False, True])
def test_factory_reopen_binds_canonical_owner_before_reads_and_retirement(data, lazy):
    from app.analytics.factory import create_analytics_stores
    from app.analytics.nlp_findings_store import checked_snapshot
    f, repo, spec, _, profile, _, policy = data
    seal(data)
    findings = FindingsStore(f.stores.database)
    findings.select(ACCOUNT, profile, mode='paused')
    replay_contribution(repo, findings, spec.contribution_id, policy=policy)
    frozen = findings.freeze(ACCOUNT, f.source.read_identity(ACCOUNT))
    stores = create_analytics_stores('sqlite', projections_path=f.stores.database.path,
        canonical_database=f.repositories.database, activation=f.repositories.projection_activation,
        canonical_identity_reader=f.source.read_identity, retention_clock=lambda: f.clock.now, lazy=lazy)
    try:
        if lazy:
            stores.projections.ensure_ready()
            database = stores.projections._store.database
        else:
            database = stores.database
        with database.read() as db:
            assert len(checked_snapshot(db, frozen)['records']) == 2
        CreatorVaultRetention(f.repositories.database, clock=lambda: f.clock.now).delete_message(ACCOUNT, 'm-0-0')
        with database.read() as db, pytest.raises(ValueError, match='origin_retired'):
            checked_snapshot(db, frozen)
    finally:
        stores.projections.close()


def test_default_runtime_reopen_has_read_fence_without_execution_configuration(data):
    from app.analytics.runtime import configure_default_analytics_runtime, reset_analytics_runtimes
    from app.analytics.nlp_findings_store import checked_snapshot
    f, repo, spec, _, profile, _, policy = data
    seal(data)
    findings = FindingsStore(f.stores.database)
    findings.select(ACCOUNT, profile, mode='paused')
    replay_contribution(repo, findings, spec.contribution_id, policy=policy)
    frozen = findings.freeze(ACCOUNT, f.source.read_identity(ACCOUNT))
    try:
        runtime = configure_default_analytics_runtime(f.source, backend='sqlite',
            projections_path=f.stores.database.path, canonical_database=f.repositories.database,
            activation=f.repositories.projection_activation, post_commit_rebuild_enabled=False)
        assert runtime.findings is None
        projections = runtime.pipeline.projections
        projections.ensure_ready()
        with projections._store.database.read() as db:
            assert len(checked_snapshot(db, frozen)['records']) == 2
        projections.close()
    finally:
        reset_analytics_runtimes()


def test_unbound_factory_owner_refuses_saved_origins_without_quarantine(data):
    from app.analytics.factory import create_analytics_stores
    from app.analytics.nlp_contribution_origins import OriginCheckerUnavailable
    from app.analytics.nlp_findings_store import checked_snapshot
    f, repo, spec, _, profile, _, policy = data
    seal(data)
    findings = FindingsStore(f.stores.database)
    findings.select(ACCOUNT, profile, mode='paused')
    replay_contribution(repo, findings, spec.contribution_id, policy=policy)
    frozen = findings.freeze(ACCOUNT, f.source.read_identity(ACCOUNT))
    from app.analytics.pipeline import AnalyticsPipeline
    pipeline = AnalyticsPipeline(f.source, projections=f.stores.projections, enrichment=f.pipeline.enrichment,
        clock=lambda: f.clock.now, nlp_findings=frozen, nlp_authorize=lambda: None, nlp_admit=lambda: None)
    pipeline.rebuild_account(ACCOUNT)
    active = f.stores.database.active_generation(ACCOUNT)
    stores = create_analytics_stores('sqlite', projections_path=f.stores.database.path,
        activation=f.repositories.projection_activation, canonical_identity_reader=f.source.read_identity,
        retention_clock=lambda: f.clock.now, lazy=True)
    try:
        with pytest.raises(OriginCheckerUnavailable):
            stores.projections.ensure_ready()
        assert not stores.projections._needs_recovery
        assert not list(f.stores.database.path.parent.glob('*.corrupt*'))
        with f.stores.database.read() as db:
            assert len(checked_snapshot(db, frozen)['records']) == 2
        assert f.stores.database.active_generation(ACCOUNT) == active
    finally:
        stores.projections.close()


def test_identical_record_keeps_live_exact_origin_and_refuses_unknown_owner(data):
    from app.analytics.nlp_findings_store import checked_snapshot
    f, repo, spec, members, profile, frames, policy = data
    seal(data)
    selected = [members[1].model_copy(update={'ordinal': 0})]
    intent = str(uuid4())
    other = spec.model_copy(update={'request_id': intent, 'job_id': intent, 'contribution_id': str(uuid4()),
        'input': spec.input.model_copy(update={'source_count': 1, 'inventory_sha256': inventory_digest(selected)})})
    repo.create_job(other, selected, profile_bytes=profile.model_dump_json().encode(), policy=policy)
    token = repo.claim_attempt(intent, expected_fence=0, lease_seconds=60, policy=policy)
    repo.stage_member(token, 0, frames[1], policy=policy)
    repo.commit(token, policy=policy)
    findings = FindingsStore(f.stores.database)
    findings.select(ACCOUNT, profile, mode='paused')
    replay_contribution(repo, findings, spec.contribution_id, policy=policy)
    assert replay_contribution(repo, findings, other.contribution_id, policy=policy)['imported'] == 0
    CreatorVaultRetention(f.repositories.database, clock=lambda: f.clock.now).delete_message(ACCOUNT, 'm-0-0')
    frozen = findings.freeze(ACCOUNT, f.source.read_identity(ACCOUNT))
    with f.stores.database.read() as db:
        assert checked_snapshot(db, frozen)['records'] == [hashlib.sha256(frames[1]).hexdigest()]
    original = repo.origin_is_live
    f.stores.database.bind_contribution_origins(lambda *args: None, repo.origin_epoch)
    with f.stores.database.read() as db, pytest.raises(ValueError, match='checker_unavailable'):
        checked_snapshot(db, frozen)
    f.stores.database.bind_contribution_origins(original, repo.origin_epoch)
