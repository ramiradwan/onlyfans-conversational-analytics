"""Require an unchanged stored snapshot before consuming a validation receipt."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from app.analytics.validation_receipt import (
    MAX_RECEIPTS, ValidationReceipts, content_stamp,
)
from app.analytics.sqlite_projection_store import ProjectionValidationError
from app.persistence import sqlite_api as sqlite3
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup, advance


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path)
    yield value
    cleanup(value)


def observed_validation(fixture, monkeypatch):
    store = fixture.stores.projections
    observed = Mock(wraps=store._validate_persisted_generation)
    monkeypatch.setattr(store, '_validate_persisted_generation', observed)
    return observed


@pytest.mark.parametrize('shared', [False, True])
def test_unchanged_candidate_uses_full_staging_check_once(fixture, monkeypatch, shared):
    fixture.stores.projections.reuse_graph_content = shared
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    assert candidate.staged_generation_id in fixture.stores.projections._validation_receipts.entries
    observed = observed_validation(fixture, monkeypatch)
    result = fixture.pipeline.publish_candidate(candidate)
    assert result.changed
    observed.assert_not_called()
    assert not fixture.stores.projections._validation_receipts.entries
    assert len(result.artifact.projection.message_enrichments) == 9


@pytest.mark.parametrize('condition', ['disabled', 'expired', 'missing', 'schema_changed', 'tracking_removed', 'other_content'])
def test_unusable_receipt_runs_independent_final_check(fixture, monkeypatch, condition):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    store = fixture.stores.projections
    cache = store._validation_receipts
    if condition == 'disabled':
        store.reuse_validation_receipts = False
    elif condition == 'expired':
        key = candidate.staged_generation_id
        cache.entries[key] = replace(cache.entries[key], expires_at=0)
    elif condition == 'missing':
        cache.entries.clear()
    else:
        with store.database.transaction() as db:
            if condition == 'schema_changed':
                db.execute('CREATE TABLE synthetic_receipt_change(value INTEGER)')
            elif condition == 'tracking_removed':
                db.execute('DROP TRIGGER generation_content_graph_owned_nodes_insert')
            else:
                db.execute('UPDATE generation_content_epoch SET value=value+1')
    observed = observed_validation(fixture, monkeypatch)
    assert fixture.pipeline.publish_candidate(candidate).changed
    observed.assert_called_once_with(candidate.staged_generation_id, materialize_projection=False)


@pytest.mark.parametrize('stage', ['validated', 'canonical_completed'])
@pytest.mark.parametrize('target', ['node', 'projection'])
def test_changed_stored_content_never_uses_an_old_receipt(fixture, stage, target):
    def tamper(phase, generation):
        if phase != stage:
            return
        with fixture.stores.database.transaction() as db:
            if target == 'node':
                db.execute('DROP TRIGGER graph_node_content_immutable')
                db.execute("UPDATE graph_node_content SET properties_json=json_set(properties_json,'$.character_count',999) WHERE kind='message'")
            else:
                db.execute('DROP TRIGGER conversation_enrichment_units_immutable')
                db.execute("""UPDATE conversation_enrichment_units
                    SET canonical_digest=? WHERE creator_account_id=(
                        SELECT creator_account_id FROM projection_generations
                        WHERE generation_id=?
                    )""", ('0' * 64, generation))
    fixture.stores.projections.crash_hook = tamper
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with pytest.raises(ProjectionValidationError):
        fixture.pipeline.publish_candidate(candidate)
    assert fixture.stores.projections.get(ACCOUNT) is None


@pytest.mark.parametrize('statement', [
    'DELETE FROM generation_content_epoch',
    'UPDATE generation_content_epoch SET value=0',
    'UPDATE generation_content_epoch SET value=value-1',
    'INSERT OR REPLACE INTO generation_content_epoch VALUES(1,0)',
])
def test_content_counter_cannot_be_reset(fixture, statement):
    fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        before = content_stamp(db)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)
        assert content_stamp(db) == before


def test_receipt_requires_an_activation_transaction_and_is_single_use(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    store = fixture.stores.projections
    with store.database.read() as db:
        row = db.execute('SELECT * FROM projection_generations WHERE generation_id=?',
                         (candidate.staged_generation_id,)).fetchone()
        with pytest.raises(ValueError):
            store._validation_receipts.take(db, row)
        db.execute('BEGIN IMMEDIATE')
        assert store._validation_receipts.take(db, row)
        assert not store._validation_receipts.take(db, row)
        db.rollback()


def test_receipts_are_process_local_bounded_and_expire_without_sliding(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    receipt = fixture.stores.projections._validation_receipts.entries[candidate.staged_generation_id]
    cache = ValidationReceipts()
    for index in range(MAX_RECEIPTS + 1):
        cache.put(replace(receipt, generation_id=str(index)))
    assert len(cache.entries) == MAX_RECEIPTS and '0' not in cache.entries
    assert not ValidationReceipts().entries


@pytest.mark.parametrize('field,value', [('creator_account_id', 'a1:'+'0'*64),
    ('canonical_revision', 999), ('pipeline_config_digest', 'sha256:'+'0'*64),
    ('graph_digest', 'sha256:'+'0'*64), ('expected_active_revision', 999),
    ('owner_instance_nonce', 'different-owner')])
def test_receipt_binds_generation_metadata(fixture, field, value):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    store = fixture.stores.projections
    with store.database.transaction() as db:
        row = dict(db.execute('SELECT * FROM projection_generations WHERE generation_id=?',
                              (candidate.staged_generation_id,)).fetchone())
        row[field] = value
        assert not store._validation_receipts.take(db, row)


def test_future_catalogs_do_not_reuse_an_unreviewed_receipt(fixture):
    with fixture.stores.database.read() as db:
        assert content_stamp(db) is not None
        db.execute('BEGIN IMMEDIATE')
        db.execute('PRAGMA user_version=20')
        assert content_stamp(db) is None
        db.rollback()
        assert content_stamp(db) is not None


def test_every_tracking_trigger_is_required(fixture):
    with fixture.stores.database.read() as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'generation_content_*'")]
        assert len(names) == 75
        for name in names:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DROP TRIGGER ' + name)
            assert content_stamp(db) is None
            db.rollback()
        assert content_stamp(db) is not None


def test_a_change_during_verification_prevents_a_receipt(fixture, monkeypatch):
    import app.analytics.sqlite_projection_store as storage
    original = storage.recompute_generation
    calls = []
    def changed(connection, *args, **kwargs):
        result = original(connection, *args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            connection.execute('UPDATE generation_content_epoch SET value=value+1')
        return result
    monkeypatch.setattr(storage, 'recompute_generation', changed)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    assert candidate.staged_generation_id not in fixture.stores.projections._validation_receipts.entries
    assert fixture.pipeline.publish_candidate(candidate).changed
    assert len(calls) == 2


def test_rolled_back_mutations_do_not_invalidate_unchanged_data(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    store = fixture.stores.projections
    with store.database.read() as db:
        original = content_stamp(db)
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE generation_content_epoch SET value=value+1')
        assert content_stamp(db) != original
        db.rollback()
        assert content_stamp(db) == original
    assert fixture.pipeline.publish_candidate(candidate).changed
