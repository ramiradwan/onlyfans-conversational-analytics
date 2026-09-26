"""Enrichment reuse survives only verified, committed cleanup transitions."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from app.analytics.enrichment_proof_transition import (
    GUARD_NAMES, capture_transition, finish_transition,
)
from app.analytics.opaque_refs import account_ref
from app.analytics.validation_receipt import content_stamp
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path, conversations=3, messages=4)
    yield value
    cleanup(value)


def active(fixture, connection):
    generation = connection.execute(
        "SELECT * FROM projection_generations WHERE status='active'"
    ).fetchone()
    proof = fixture.stores.projections._trusted_conversation_enrichment_proof(
        connection, generation
    )
    return generation, proof


def append_message(fixture, ordinal):
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', f'proof-message-{ordinal}', NOW, ordinal)
        advance(db)


def test_repeated_updates_validate_only_the_changed_unit(fixture, monkeypatch):
    from app.analytics import conversation_enrichment_unit_sql as storage
    from app.analytics import conversation_enrichment_units as units
    from unittest.mock import Mock

    fixture.pipeline.project_account(ACCOUNT)
    checked = Mock(wraps=storage._validate_unit)
    created = Mock(wraps=units.create_enrichment_unit)
    monkeypatch.setattr(storage, '_validate_unit', checked)
    monkeypatch.setattr(units, 'create_enrichment_unit', created)
    for ordinal in range(1, 5):
        checked.reset_mock()
        created.reset_mock()
        before = sum(analyzer.calls for analyzer in fixture.analyzers)
        fixture.source.loaded.clear()
        append_message(fixture, ordinal)
        candidate = fixture.pipeline.build_candidate(ACCOUNT)
        result = fixture.pipeline.publish_candidate(candidate)
        assert checked.call_count == created.call_count == 1
        assert sum(analyzer.calls for analyzer in fixture.analyzers) - before == 3
        assert fixture.source.loaded == ['chat-1']
        with fixture.stores.database.read() as db:
            generation, proof = active(fixture, db)
            assert proof is not None
            assert proof.stamp == content_stamp(db)
            assert len(proof.headers) == 3
            assert db.execute(
                "SELECT COUNT(*) FROM projection_generations WHERE status='retired'"
            ).fetchone()[0] == 1
    cold_equal(fixture, result.artifact)


def prepare_cleanup(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    append_message(fixture, 1)
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections.rollback_retention = 0


def test_a_stale_proof_is_not_renewed_by_cleanup(fixture):
    prepare_cleanup(fixture)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        assert proof is not None
        db.execute('UPDATE generation_content_epoch SET value=value+1')
    assert fixture.stores.projections.collect_garbage(account_ref(ACCOUNT)) == 1
    with fixture.stores.database.read() as db:
        assert active(fixture, db)[1] is None


def test_cleanup_rollback_does_not_install_a_new_proof(fixture, monkeypatch):
    prepare_cleanup(fixture)
    store = fixture.stores.projections
    with fixture.stores.database.read() as db:
        generation, proof = active(fixture, db)
    original = fixture.stores.database.transaction

    @contextmanager
    def failing_transaction(**kwargs):
        with original(**kwargs) as db:
            yield db
            raise RuntimeError('commit failed')

    monkeypatch.setattr(fixture.stores.database, 'transaction', failing_transaction)
    with pytest.raises(RuntimeError, match='commit failed'):
        store.collect_garbage(account_ref(ACCOUNT))
    assert store._conversation_enrichment_proofs[generation['generation_id']] is proof
    with fixture.stores.database.read() as db:
        assert active(fixture, db)[1] is proof
        assert db.execute(
            "SELECT COUNT(*) FROM projection_generations WHERE status='retired'"
        ).fetchone()[0] == 1


def test_write_after_commit_still_invalidates_the_renewed_proof(fixture, monkeypatch):
    prepare_cleanup(fixture)
    store = fixture.stores.projections
    install = store._install_enrichment_transition

    def concurrent_write(transition, renewed):
        assert renewed is not None
        with fixture.stores.database.transaction() as db:
            db.execute('UPDATE generation_content_epoch SET value=value+1')
        install(transition, renewed)

    monkeypatch.setattr(store, '_install_enrichment_transition', concurrent_write)
    assert store.collect_garbage(account_ref(ACCOUNT)) == 1
    with fixture.stores.database.read() as db:
        assert active(fixture, db)[1] is None


@pytest.mark.parametrize('field,value', [
    ('generation_id', 'other-generation'), ('binding', 'different'),
    ('stamp', ('other-store', 'other-schema', 0, 0)), ('headers', ()),
])
def test_capture_requires_the_exact_current_proof(fixture, field, value):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        assert capture_transition(db, generation, replace(proof, **{field: value})) is None


def test_schema_change_prevents_renewal(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        transition = capture_transition(db, generation, proof)
        assert transition is not None
        db.execute('SAVEPOINT original_schema')
        db.execute('DROP TRIGGER conversation_enrichment_units_immutable')
        assert finish_transition(db, transition) is None
        db.execute('ROLLBACK TO original_schema')
        assert finish_transition(db, transition) is not None


def test_removed_selection_prevents_renewal(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        transition = capture_transition(db, generation, proof)
        db.execute('SAVEPOINT original_generation')
        db.execute(
            "UPDATE projection_generations SET status='retired',retired_at=? "
            'WHERE generation_id=?', (NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), generation['generation_id'])
        )
        assert finish_transition(db, transition) is None
        db.execute('ROLLBACK TO original_generation')
        assert finish_transition(db, transition) is not None


def test_missing_or_evicted_proof_is_not_recreated(fixture):
    prepare_cleanup(fixture)
    store = fixture.stores.projections
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        transition = capture_transition(db, generation, proof)
        renewed = finish_transition(db, transition)
    assert renewed is not None
    store._conversation_enrichment_proofs.clear()
    store._install_enrichment_transition(transition, renewed)
    assert not store._conversation_enrichment_proofs
    assert store.collect_garbage(account_ref(ACCOUNT)) == 1
    assert not store._conversation_enrichment_proofs


def test_transition_requires_a_transaction(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        generation, proof = active(fixture, db)
        with pytest.raises(ValueError, match='requires_transaction'):
            capture_transition(db, generation, proof)
        with pytest.raises(ValueError, match='requires_transaction'):
            finish_transition(db, None)


@pytest.mark.parametrize('name', GUARD_NAMES)
def test_a_proof_cannot_cross_cleanup_under_missing_guards(fixture, name):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        db.execute('SAVEPOINT original_guards')
        db.execute('DROP TRIGGER ' + name)
        # Model a proof validated under this schema: a fresh stamp alone must
        # not establish the immutability required for transition renewal.
        under_changed_schema = replace(proof, stamp=content_stamp(db))
        assert capture_transition(db, generation, under_changed_schema) is None
        db.execute('ROLLBACK TO original_guards')
        assert capture_transition(db, generation, proof) is not None


@pytest.mark.parametrize('column', [0, 1, 2, 3, 4, 5, 6, 7, 8])
def test_renewal_requires_the_entire_reference_selection(fixture, column):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        original = capture_transition(db, generation, proof)
        row = list(original.references[0])
        row[column] = 'changed'
        changed = replace(original, references=(tuple(row), *original.references[1:]))
        assert finish_transition(db, changed) is None


def test_transition_does_not_read_enrichment_payloads(fixture):
    from sqlcipher3.dbapi2 import SQLITE_DENY, SQLITE_OK, SQLITE_READ

    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        def authorize(action, table, column, *unused):
            if (action == SQLITE_READ and table == 'conversation_enrichment_units'
                    and column in ('message_bytes', 'analyzer_bytes')):
                return SQLITE_DENY
            return SQLITE_OK
        db.set_authorizer(authorize)
        try:
            transition = capture_transition(db, generation, proof)
            assert transition is not None
            assert finish_transition(db, transition) is not None
        finally:
            db.set_authorizer(None)


def test_unreviewed_trigger_prevents_transition_renewal(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        db.execute('SAVEPOINT original_catalog')
        db.execute("CREATE TRIGGER extra_side_effect AFTER DELETE ON graph_owned_nodes "
                   'BEGIN SELECT 1; END')
        current = replace(proof, stamp=content_stamp(db))
        assert capture_transition(db, generation, current) is None
        db.execute('ROLLBACK TO original_catalog')
        assert capture_transition(db, generation, proof) is not None


def test_transition_cannot_be_transferred_to_another_connection(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        transition = capture_transition(db, generation, proof)
        assert transition is not None
        with fixture.stores.database.read() as other:
            other.execute('BEGIN')
            assert finish_transition(other, transition) is None


def test_activation_does_not_renew_an_intervening_write(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    append_message(fixture, 1)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    store = fixture.stores.projections

    def intervene(phase, generation_id):
        if phase == 'canonical_completed':
            with fixture.stores.database.transaction() as db:
                db.execute('UPDATE generation_content_epoch SET value=value+1')

    store.crash_hook = intervene
    try:
        result = fixture.pipeline.publish_candidate(candidate)
    finally:
        store.crash_hook = None
    with fixture.stores.database.read() as db:
        assert active(fixture, db)[1] is None
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('field,value', [
    ('account_ref', account_ref('different-account')),
    ('conversation_ref', 'c1:' + '1' * 64),
    ('unit_id', '0' * 64),
])
def test_capture_rejects_a_header_outside_the_selected_manifest(fixture, field, value):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        header = replace(proof.headers[0], **{field: value})
        altered = replace(proof, headers=(header, *proof.headers[1:]))
        assert capture_transition(db, generation, altered) is None


def test_foreign_key_enforcement_is_required(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        generation, proof = active(fixture, db)
        assert proof is not None
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        try:
            assert active(fixture, db)[1] is None
            assert capture_transition(db, generation, proof) is None
        finally:
            db.rollback()


@pytest.mark.parametrize('mutation', ['update', 'replace', 'delete'])
def test_selected_payload_is_immutable_during_transition(fixture, mutation):
    from app.persistence import sqlite_api as sqlite3

    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        transition = capture_transition(db, generation, proof)
        assert transition is not None
        if mutation == 'update':
            sql = 'UPDATE conversation_enrichment_units SET message_bytes=?'
            parameters = (b'changed payload',)
        elif mutation == 'replace':
            sql = ('INSERT OR REPLACE INTO conversation_enrichment_units '
                   'SELECT * FROM conversation_enrichment_units LIMIT 1')
            parameters = ()
        else:
            sql = 'DELETE FROM conversation_enrichment_units'
            parameters = ()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, parameters)
        renewed = finish_transition(db, transition)
        assert renewed is not None and renewed.headers == proof.headers
        assert renewed.stamp == proof.stamp


def test_unrecognized_catalog_keeps_the_full_validation_fallback(fixture):
    with fixture.stores.database.transaction() as db:
        db.execute('CREATE TRIGGER harmless_extension AFTER DELETE '
                   'ON graph_owned_nodes BEGIN SELECT 1; END')
    fixture.pipeline.project_account(ACCOUNT)
    for ordinal in range(1, 3):
        append_message(fixture, ordinal)
        result = fixture.pipeline.project_account(ACCOUNT)
        cold_equal(fixture, result.artifact)
        with fixture.stores.database.read() as db:
            assert active(fixture, db)[1] is None


def test_an_evicted_transition_does_not_replace_a_newer_proof(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    store = fixture.stores.projections
    with fixture.stores.database.transaction() as db:
        generation, proof = active(fixture, db)
        transition = capture_transition(db, generation, proof)
        renewed = finish_transition(db, transition)
    assert renewed is not None
    newer = replace(proof)
    store._conversation_enrichment_proofs[proof.generation_id] = newer
    store._install_enrichment_transition(transition, renewed)
    assert store._conversation_enrichment_proofs[proof.generation_id] is newer


def test_activation_failure_does_not_install_a_renewed_proof(fixture, monkeypatch):
    from unittest.mock import Mock
    from app.analytics import sqlite_projection_store as storage

    fixture.pipeline.project_account(ACCOUNT)
    store = fixture.stores.projections
    with fixture.stores.database.read() as db:
        predecessor, _ = active(fixture, db)
    append_message(fixture, 1)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    proof = store._conversation_enrichment_proofs[candidate.staged_generation_id]
    finish = storage.finish_transition
    install = Mock(wraps=store._install_enrichment_transition)
    monkeypatch.setattr(store, '_install_enrichment_transition', install)

    def fail_before_commit(connection, transition):
        assert transition.proof is proof
        assert finish(connection, transition) is not None
        raise RuntimeError('activation transaction failed')

    monkeypatch.setattr(storage, 'finish_transition', fail_before_commit)
    with pytest.raises(RuntimeError, match='activation transaction failed'):
        fixture.pipeline.publish_candidate(candidate)
    install.assert_not_called()
    assert store._conversation_enrichment_proofs[candidate.staged_generation_id] is proof
    with fixture.stores.database.read() as db:
        current, _ = active(fixture, db)
        assert current['generation_id'] == predecessor['generation_id']
        failed = db.execute(
            'SELECT * FROM projection_generations WHERE generation_id=?',
            (candidate.staged_generation_id,),
        ).fetchone()
        assert failed['status'] == 'retired'
        assert store._trusted_conversation_enrichment_proof(db, failed) is None
        assert db.execute(
            'SELECT COUNT(*) FROM conversation_enrichment_refs WHERE generation_id=?',
            (candidate.staged_generation_id,),
        ).fetchone()[0] == 0
