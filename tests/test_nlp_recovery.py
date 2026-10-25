"""Real disposable-store recovery with invented canonical findings."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

import pytest

from app.analytics.factory import create_analytics_stores
from app.analytics.nlp_findings_store import checked_snapshot
from app.analytics.nlp_literals import _POLICY_BYTES, checked_publication, LiteralIntegrityError
from app.analytics.errors import ProjectionUnavailable
from app.analytics.nlp_recovery import RecoveryCoordinator
from app.models.knowledge_contributions import record_digest
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.nlp_dependencies import DependencyPersistence
from app.persistence.retention import CreatorVaultRetention
from tests.continuous_analytics_fixture import ACCOUNT, insert_message, advance
from tests.test_knowledge_contributions import data, seal, _positive
from tests.test_nlp_canonical_execution import execution as licensed_execution, authority_basis

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.mark.parametrize('when',['begin','visibility','same_owner_other_principal'])
def test_current_enrollment_guard_fences_replacement_without_narrowing_saved_scope(licensed_execution,when):
    from app.models.text_analysis_jobs import EnrollmentIntent
    from app.security.text_analysis_policy import encode_run_context
    x=licensed_execution()
    assert x.runner.process_one(ACCOUNT)
    with x.repository.database.read() as db:
        manifest=x.repository.get_committed(db.execute('SELECT contribution_id FROM knowledge_contributions').fetchone()[0],policy=x.policy)
    refs=((manifest.contribution_id,record_digest(manifest)),)
    x.runner.findings.mode(ACCOUNT,'paused')
    coordinator=RecoveryCoordinator(x.repository,DependencyPersistence(x.auth),x.f.source,x.f.stores.projections,
        policy=x.policy,clock=lambda:x.f.clock.now)
    ticket=None
    if when == 'visibility':
        ticket=coordinator.begin(ACCOUNT,x.profile,refs,intent=str(uuid4()))
    target=x.enrollment.enrollment_id
    with x.repository.database.transaction() as db:
        if when != 'same_owner_other_principal':
            x.jobs.set_enrollment_mode_in(db,target,'retired',policy=x.policy)
            other=x.jobs.enroll_in(db,EnrollmentIntent(external_key=str(uuid4()),
                run_context=encode_run_context(x.context),parameters=x.owner.parameters),policy=x.policy)
            target=other.enrollment_id
            x.jobs.set_enrollment_mode_in(db,target,'paused',policy=x.policy)
        db.execute('INSERT INTO text_analysis_account_current VALUES(?,?,?,?,?)',
            (x.context.installation_id,ACCOUNT,x.context.principal_id,1,target))
    if when == 'same_owner_other_principal':
        coordinator.policy=replace(x.policy,identity=replace(x.policy.identity,principal_id='another-current-principal'))
        ticket=coordinator.begin(ACCOUNT,x.profile,refs,intent=str(uuid4()))
        with coordinator.visibility(ticket):
            pass
    else:
        with pytest.raises(ValueError,match='current_owner_conflict'):
            if ticket is None:
                coordinator.begin(ACCOUNT,x.profile,refs,intent=str(uuid4()))
            else:
                with coordinator.visibility(ticket):
                    pass


def prepare(data, *, loss=False):
    f, repo, spec, members, profile, frames, policy = data
    frames[:] = [_positive(profile, raw) for raw in frames]
    _, manifest = seal(data)
    with f.repositories.database.transaction() as db:
        insert_message(db, 'chat-0', 'pending-source', f.clock.now)
        advance(db)
    authentication = SQLiteAuthenticationStore(f.stores.database.path.parent / 'auth.sqlite3')
    authority = DependencyPersistence(authentication)
    authority.initialize('f' * 64)
    policy = replace(policy, authorization_epoch=authentication.build_runtime_policy(policy.identity).authorization_epoch)
    coordinator = RecoveryCoordinator(repo, authority, f.source, f.stores.projections,
        policy=policy, clock=lambda: f.clock.now)
    refs = ((spec.contribution_id, record_digest(manifest)),)
    if loss:
        original = coordinator.finish(complete(coordinator, coordinator.begin(ACCOUNT, profile, refs,
            intent=str(uuid4()), normalization=_POLICY_BYTES)))
        with f.stores.database.read() as db:
            f.previous_recovery = checked_publication(db, original['snapshot'])
            old_store = db.execute('SELECT store_id FROM projection_store_identity').fetchone()[0]
            assert db.execute("SELECT count(*) FROM projection_generations WHERE status='active'").fetchone()[0] == 1
        path = f.stores.database.path
        f.stores.projections.close()
        for suffix in ('', '-wal', '-shm'):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        f.stores = create_analytics_stores('sqlite', projections_path=path,
            canonical_database=f.repositories.database, activation=f.repositories.projection_activation,
            canonical_identity_reader=f.source.read_identity, retention_clock=lambda: f.clock.now)
        with f.stores.database.read() as db:
            assert db.execute('SELECT store_id FROM projection_store_identity').fetchone()[0] != old_store
            assert not db.execute('SELECT 1 FROM nlp_records').fetchone()
        coordinator = RecoveryCoordinator(repo, authority, f.source, f.stores.projections,
            policy=policy, clock=lambda: f.clock.now)
    return coordinator, refs


def complete(coordinator, ticket):
    for _ in range(32):
        if ticket.phase == 'complete':
            return ticket
        ticket = coordinator.step(ticket, limit=1)
    pytest.fail('bounded recovery did not finish')


def test_full_projection_loss_recovers_paused_current_population_and_literals(data):
    c, refs = prepare(data, loss=True)
    f, repo, spec, _, profile, frames, policy = data
    ticket = c.begin(ACCOUNT, profile, refs, intent=str(uuid4()), normalization=_POLICY_BYTES)
    # A lost page response and a new facade do not dispatch processing.
    c.step(ticket, limit=1)
    reopened = DependencyPersistence(SQLiteAuthenticationStore(c.authority.database.path))
    assert reopened.gate is c.authority.gate
    c = RecoveryCoordinator(repo, reopened, f.source, f.stores.projections, policy=c.policy, clock=lambda: f.clock.now)
    ticket = complete(c, ticket)
    output = c.finish(ticket)
    assert c.findings.state(ACCOUNT)['mode'] == 'paused'
    assert output['literal_availability'] == 'current'
    with f.stores.database.read() as db:
        value = checked_snapshot(db, output['snapshot'])
        assert set(value['records']) == {hashlib.sha256(raw).hexdigest() for raw in frames}
        assert value['read_inventory']['source_count'] == 3
        assert all(task['eligible'] == 3 and task['completed'] == 2 and task['missing'] == 1 for task in value['coverage'].values())
        assert all(item['label_status'] == 'id_only' for item in value['read_inventory']['labels'])
        assert bytes(db.execute('SELECT document FROM nlp_records LIMIT 1').fetchone()[0]) in frames
        literals = checked_publication(db, output['snapshot'])
        assert literals.envelope.records == 2
        assert literals.envelope.occurrences == f.previous_recovery.envelope.occurrences == 2
        assert [(node.type_id, node.literal_key) for node in literals.nodes] == [(node.type_id, node.literal_key) for node in f.previous_recovery.nodes]
        assert literals.envelope.account_namespace != f.previous_recovery.envelope.account_namespace
        assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state='running'").fetchone()[0] == 0
    assert record_digest(repo.get_committed(spec.contribution_id, policy=policy)) == refs[0][1]
    assert [m['record_utf8'].encode() for m in repo.export_document(spec.contribution_id, policy=policy)['members']] == frames
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.query_runtime import QuestionResources
    from app.models.insights import FindingsRequest, FindingsCategoryFilter
    from app.security.runtime_policy import AuthContext
    from app.security.local_sessions import build_runtime_policy
    resources = QuestionResources(f.source, AnalyticsPipeline(f.source, projections=c.projections), clock=lambda: f.clock.now)
    try:
        projection = output['generation'].artifact.projection
        request = FindingsRequest(expected_generation=projection.projection_generation,
            expected_projection_digest=projection.projection_digest,
            category_filter=FindingsCategoryFilter(task='message_topics', category_id='topic.synthetic'))
        read_policy = build_runtime_policy(AuthContext('reader', ACCOUNT, 'creator',
            platform_creator_id='platform.synthetic', session_id='session.synthetic'))
        response = resources.findings(read_policy, request)
        assert response.schema_version == 'analytics-findings.v3'
        assert response.tasks[0].complete_messages == 2
        assert response.tasks[0].rows[0].supporting_messages == 2
        assert response.evidence_page.total == 2
    finally:
        resources.close()
        assert resources.wait_closed()


def test_remove_invalidates_old_ticket_but_fresh_explicit_intent_recovers(data):
    c, refs = prepare(data)
    profile = data[4]
    old = c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    with c.authority.gate:
        c.findings.mode(ACCOUNT, 'removed')
    with pytest.raises(ValueError, match='selection_changed'):
        c.step(old)
    with pytest.raises(ValueError, match='intent_stale'):
        c.begin(ACCOUNT, profile, refs, intent=old.intent)
    new = c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    output = c.finish(complete(c, new))
    assert output['literal_availability'] == 'unavailable'
    assert c.findings.state(ACCOUNT)['mode'] == 'paused'


@pytest.mark.parametrize('phase', ['generation', 'literal'])
@pytest.mark.parametrize('mutation', ['remove', 'withdraw'])
def test_final_visibility_rechecks_actual_authority_and_selection(data, phase, mutation, monkeypatch):
    c, refs = prepare(data)
    profile = data[4]
    ticket = complete(c, c.begin(ACCOUNT, profile, refs, intent=str(uuid4()), normalization=_POLICY_BYTES))
    original = c.visibility
    calls = []
    @contextmanager
    def raced(value):
        calls.append(1)
        # finish entry, generation commit, literal first write, literal seal.
        target = 2 if phase == 'generation' else 4
        if len(calls) == target:
            with c.authority.gate:
                if mutation == 'remove':
                    c.findings.mode(ACCOUNT, 'removed')
                else:
                    c.authority.update(lambda state: {**state, 'epoch': state['epoch'] + 1,
                        'withdrawals': [profile.identity_digest()]})
        with original(value):
            yield
    monkeypatch.setattr(c, 'visibility', raced)
    with pytest.raises((ValueError, LiteralIntegrityError, ProjectionUnavailable)):
        c.finish(ticket)
    with c.projections.database.read() as db:
        if phase == 'generation':
            assert db.execute("SELECT count(*) FROM projection_generations WHERE status='active'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM nlp_literal_publications WHERE state='sealed'").fetchone()[0] == 0


def test_canonical_retirement_cancel_and_busy_selection_refuse(data):
    c, refs = prepare(data)
    f, _, _, _, profile, _, _ = data
    ticket = c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    c.findings.mode(ACCOUNT, 'enabled')
    with pytest.raises(ValueError, match='busy'):
        c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    c.findings.mode(ACCOUNT, 'paused')
    ticket = c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    CreatorVaultRetention(f.repositories.database, clock=lambda: f.clock.now).delete_message(ACCOUNT, 'm-0-0')
    with pytest.raises(ValueError):
        c.step(ticket)
    with c.authority.database.transaction() as db:
        db.execute('UPDATE authorization_epoch SET value=value+1 WHERE singleton=1')
    with pytest.raises(ValueError, match='read_authority_changed'):
        c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    c.cancelled = lambda: True
    with pytest.raises(ValueError, match='cancelled'):
        c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))


@pytest.mark.parametrize('identical', [True, False])
def test_whole_pinned_selection_conflicts_or_keeps_every_exact_origin(data, identical):
    from app.models.knowledge_contributions import inventory_digest
    c, refs = prepare(data)
    f, repo, spec, members, profile, frames, policy = data
    intent = str(uuid4())
    identity = f.source.read_identity(ACCOUNT)
    members = [m.model_copy(update={'source_revision': identity.revision}) for m in members]
    other = spec.model_copy(update={'request_id': intent, 'job_id': intent, 'contribution_id': str(uuid4()),
        'input': spec.input.model_copy(update={'canonical_revision': identity.revision,
            'canonical_content_sha256': identity.content_digest.removeprefix('sha256:'),
            'inventory_sha256': inventory_digest(members)})})
    repo.create_job(other, members, profile_bytes=profile.model_dump_json().encode(), policy=policy)
    token = repo.claim_attempt(intent, expected_fence=0, lease_seconds=60, policy=policy)
    for ordinal, raw in enumerate(frames):
        if not identical:
            value = json.loads(raw)
            for task in value['tasks']:
                task['result']['payload'].update(outcome='assessed_no_match', findings=[])
            raw = json.dumps(value).encode()
        repo.stage_member(token, ordinal, raw, policy=policy)
    manifest = repo.commit(token, policy=policy)
    references = refs + ((other.contribution_id, record_digest(manifest)),)
    if not identical:
        with pytest.raises(ValueError, match='result_conflict'):
            c.begin(ACCOUNT, profile, references, intent=str(uuid4()))
        assert f.stores.database.active_generation(ACCOUNT) is None
    else:
        ticket = complete(c, c.begin(ACCOUNT, profile, references, intent=str(uuid4())))
        output = c.finish(ticket)
        with c.projections.database.read() as db:
            assert db.execute('SELECT count(*) FROM nlp_record_origins').fetchone()[0] == 4
            assert len(checked_snapshot(db, output['snapshot'])['records']) == 2


def test_original_presentation_and_unavailable_normalization_do_not_relabel_saved_view(data):
    from app.analytics.nlp_read_projection import frozen_labels
    c, refs = prepare(data)
    profile = data[4]
    labels = json.dumps(frozen_labels(profile, lambda identity: None if identity['task_contract'] == 'concept_mentions.v1'
        else {**identity, 'display_label': 'Frozen label'})).encode()
    first = c.finish(complete(c, c.begin(ACCOUNT, profile, refs, intent=str(uuid4()), presentation=labels)))
    second = c.finish(complete(c, c.begin(ACCOUNT, profile, refs, intent=str(uuid4()), normalization=b'unsupported-original-policy')))
    assert second['literal_availability'] == 'unavailable'
    with c.projections.database.read() as db:
        old_labels = checked_snapshot(db, first['snapshot'])['read_inventory']['labels']
        assert any(x['display_label'] == 'Frozen label' for x in old_labels)
        assert any(x['label_status'] == 'id_only' for x in old_labels)
        assert all(x['label_status'] == 'id_only' for x in checked_snapshot(db, second['snapshot'])['read_inventory']['labels'])


@pytest.mark.parametrize('literal', [False, True])
@pytest.mark.parametrize('mutation', ['remove', 'withdraw'])
def test_real_dependency_mutations_share_visibility_gate_across_facades(data, literal, mutation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace
    from app.nlp.profiles import ResourcePolicy
    from app.nlp.release_catalog import canonical, CATALOG_TYPE
    from app.services.nlp_dependencies import DependencyService
    from tests.test_nlp_profile_authentication import keys, sign
    c, refs = prepare(data)
    profile = data[4]
    ticket = c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    facade = DependencyPersistence(SQLiteAuthenticationStore(c.authority.database.path))
    service = DependencyService(facade, SimpleNamespace(limits=SimpleNamespace(maximum_store_bytes=0)),
        c.findings, c.source, None, keys=keys(), approvals=None, engine_provider=None,
        resource_policy=ResourcePolicy(download_bytes=1024, installed_bytes=1024, temporary_bytes=1024,
            transition_peak_memory_bytes=1024, inference_threads=2),
        spool_root=c.projections.database.path.parent / 'unused-spool', clock=lambda: 1000)
    assert service.gate is facade.gate is c.authority.gate
    contended = Event()
    def change():
        acquired = service.gate.acquire(blocking=False)
        if acquired:
            service.gate.release()
        assert not acquired
        contended.set()
        if mutation == 'remove':
            service.remove(ACCOUNT)
        else:
            service.learn(sign(canonical({'schema_version': 'nlp-release-catalog.v1', 'sequence': 1,
                'issued_at': 900, 'expires_at': 2000, 'profiles': [],
                'withdrawals': [profile.identity_digest()]}), typ=CATALOG_TYPE))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with (c.literal_visibility(ticket) if literal else c.visibility(ticket)):
            future = pool.submit(change)
            assert contended.wait(3)
            assert not future.done()
        future.result(timeout=5)
    with pytest.raises(ValueError):
        c.step(ticket)


@pytest.mark.parametrize('damage', ['malformed', 'extra', 'digest', 'source'])
def test_literal_exact_raw_intake_remains_source_and_digest_checked(data, damage):
    from app.analytics.nlp_literals import capture_record
    from app.analytics.canonical_source import read_transaction_evidence
    from app.nlp import v2
    c, refs = prepare(data)
    f, repo, _, members, profile, frames, _ = data
    c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    raw = frames[0]
    record = v2.read_record(raw)
    with f.repositories.database.read() as db:
        source = read_transaction_evidence(db, ACCOUNT, members[0].location.model_dump(), repo.source_guard.budget())
    digest = hashlib.sha256(raw).hexdigest()
    if damage == 'malformed':
        raw = b'{'
    elif damage == 'extra':
        value = json.loads(raw); value['extra'] = True; raw = json.dumps(value).encode()
        digest = hashlib.sha256(raw).hexdigest()
    elif damage == 'digest':
        raw += b' '
    else:
        with f.repositories.database.read() as db:
            source = read_transaction_evidence(db, ACCOUNT, members[1].location.model_dump(), repo.source_guard.budget())
    with f.stores.database.transaction() as db, pytest.raises((ValueError, LiteralIntegrityError)):
        capture_record(db, profile, source, record, digest, record_bytes=raw)


@pytest.mark.parametrize('reuse', [False, True])
def test_expiry_after_actual_literal_writer_acquisition_cannot_seal_or_reuse(data, reuse, monkeypatch):
    from app.analytics import nlp_literals as literals
    c, refs = prepare(data)
    f, _, _, _, profile, _, _ = data
    ticket = complete(c, c.begin(ACCOUNT, profile, refs, intent=str(uuid4()), normalization=_POLICY_BYTES))
    if reuse:
        c.finish(ticket)
    with c.projections.database.read() as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM nlp_literal_publications WHERE state='sealed'")]
    expires = f.clock.now + timedelta(seconds=1)
    c.policy = replace(c.policy, expires_at=expires)
    armed, entered = [], []
    if reuse:
        checked = literals.checked_publication
        def arm_reuse(*args, **kwargs):
            result = checked(*args, **kwargs)
            armed.append(True)
            return result
        monkeypatch.setattr(literals, 'checked_publication', arm_reuse)
    else:
        rows = literals._rows
        def arm_seal(db, table, *args, **kwargs):
            result = rows(db, table, *args, **kwargs)
            if table == 'nlp_literal_publication_batches':
                armed.append(True)
            return result
        monkeypatch.setattr(literals, '_rows', arm_seal)
    transaction = c.projections.database.transaction
    @contextmanager
    def writer(*args, **kwargs):
        with transaction(*args, **kwargs) as db:
            if armed and not entered:
                # The real SQLite writer is held; no authorization has run in it yet.
                entered.append(True)
                f.clock.now = expires + timedelta(seconds=1)
            yield db
    monkeypatch.setattr(c.projections.database, 'transaction', writer)
    with pytest.raises(LiteralIntegrityError) as refused:
        c.finish(ticket)
    assert entered == [True]
    assert str(refused.value.__cause__) == 'nlp_recovery_read_expired'
    with c.projections.database.read() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM nlp_literal_publications WHERE state='sealed'")] == before
        assert not db.execute("SELECT 1 FROM nlp_literal_publications WHERE state='building'").fetchone()


@pytest.mark.parametrize('boundary', ['begin_expiry', 'begin_epoch', 'visibility_expiry', 'literal_source_expiry'])
def test_fresh_read_authorization_after_owner_or_source_guard_wait(data, boundary, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    c, refs = prepare(data)
    f, _, _, _, profile, _, _ = data
    begins = boundary.startswith('begin_')
    ticket = None if begins else c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
    expires = f.clock.now + timedelta(seconds=1)
    c.policy = replace(c.policy, expires_at=expires)
    attempted, yielded = Event(), Event()
    if boundary == 'literal_source_expiry':
        original = c.repository.read_committed
        @contextmanager
        def waiting(*args, **kwargs):
            attempted.set()
            with original(*args, **kwargs) as value:
                yield value
        monkeypatch.setattr(c.repository, 'read_committed', waiting)
        held = c.repository.database.transaction()
    else:
        original = c.authority.recovery_visibility
        @contextmanager
        def waiting(*args, **kwargs):
            attempted.set()
            with original(*args, **kwargs):
                yield
        monkeypatch.setattr(c.authority, 'recovery_visibility', waiting)
        held = c.authority.gate
    def operation():
        if begins:
            return c.begin(ACCOUNT, profile, refs, intent=str(uuid4()))
        with (c.literal_visibility(ticket) if boundary == 'literal_source_expiry' else c.visibility(ticket)):
            yielded.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with held:
            future = pool.submit(operation)
            assert attempted.wait(3)
            if boundary == 'begin_epoch':
                with c.authority.database.transaction() as db:
                    db.execute('UPDATE authorization_epoch SET value=value+1 WHERE singleton=1')
            else:
                f.clock.now = expires + timedelta(seconds=1)
        with pytest.raises(ValueError, match='read_authority_changed|read_expired'):
            future.result(timeout=5)
    assert not yielded.is_set()
    if begins:
        with c.projections.database.read() as db:
            assert not db.execute('SELECT 1 FROM nlp_accounts').fetchone()
