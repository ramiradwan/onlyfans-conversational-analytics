"""Scheduler currentness must remain bound to live source and checked storage."""

from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.analytics.currentness import GenerationCurrentness
from app.analytics.sqlite_projection_store import ProjectionValidationError
from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    value = make_fixture(tmp_path)
    value.pipeline.project_account(ACCOUNT)
    value.reads = Mock(wraps=value.stores.projections.get)
    monkeypatch.setattr(value.stores.projections, 'get', value.reads)
    yield value
    cleanup(value)


def current(fixture):
    return fixture.pipeline.projection_is_current(ACCOUNT, 1)


def test_unchanged_reconciliation_does_not_read_the_graph_twice(fixture):
    assert current(fixture) and current(fixture)
    assert fixture.reads.call_count == 1


@pytest.mark.parametrize('mutation', ['edit', 'delete', 'revision'])
def test_source_mutations_are_not_hidden_by_a_positive_proof(fixture, mutation):
    assert current(fixture)
    with fixture.repositories.database.transaction() as db:
        if mutation == 'edit':
            db.execute("UPDATE account_messages SET text='Changed synthetic source'")
        elif mutation == 'delete':
            db.execute("DELETE FROM account_messages WHERE message_id='m-0-0'")
        else:
            db.execute('UPDATE account_heads SET canonical_revision=canonical_revision+1')
    assert not current(fixture)


@pytest.mark.parametrize('mutation', ['node', 'document'])
def test_actual_content_tamper_invalidates_a_currentness_proof(fixture, mutation):
    assert current(fixture)
    with fixture.stores.database.transaction() as db:
        if mutation == 'node':
            db.execute('DROP TRIGGER graph_node_content_immutable')
            db.execute("UPDATE graph_node_content SET properties_json=json_set(properties_json,'$.character_count',999) WHERE kind='message'")
        else:
            db.execute('DROP TRIGGER projection_document_update_blocked')
            db.execute("UPDATE analytics_projections SET document_json=json_set(document_json,'$.message_enrichments[#-1].source_ordinal',999)")
    with pytest.raises(ProjectionValidationError):
        current(fixture)


def test_currentness_expiry_is_fixed_not_extended_by_reads(fixture):
    now = [0.0]
    fixture.stores.projections._currentness.clock = lambda: now[0]
    assert current(fixture)
    now[0] = 59
    assert current(fixture)
    assert fixture.reads.call_count == 1
    now[0] = 60
    assert current(fixture)
    assert fixture.reads.call_count == 2


def test_source_expiry_is_checked_on_a_warm_proof(fixture):
    with fixture.stores.database.read() as db:
        first = db.execute('SELECT first_source FROM projection_query_metadata').fetchone()[0]
    from datetime import datetime
    assert current(fixture)
    fixture.clock.now = datetime.fromisoformat(first.replace('Z', '+00:00')) + timedelta(days=90)
    assert not current(fixture)
    assert fixture.reads.call_count == 1


def test_witness_is_checked_even_on_a_warm_proof(fixture, monkeypatch):
    assert current(fixture)
    monkeypatch.setattr(fixture.stores.projections.activation, 'get', lambda generation: None)
    assert not current(fixture)


@pytest.mark.parametrize('change', ['changed_stamp', 'missing_stamp', 'restart'])
def test_missing_or_changed_proof_requires_another_full_read(fixture, monkeypatch, change):
    import app.analytics.currentness as module
    assert current(fixture)
    if change == 'restart':
        fixture.stores.projections._currentness = GenerationCurrentness()
    else:
        original = module.content_stamp
        if change == 'changed_stamp':
            monkeypatch.setattr(module, 'content_stamp', lambda db: (*original(db), 'changed'))
        else:
            monkeypatch.setattr(module, 'content_stamp', lambda db: None)
    assert current(fixture)
    assert fixture.reads.call_count == 2
    if change == 'missing_stamp':
        assert current(fixture)
        assert fixture.reads.call_count == 3


def test_pipeline_configuration_and_requested_revision_are_checked(fixture):
    assert current(fixture)
    assert not fixture.pipeline.projection_is_current(ACCOUNT, 99)
    fixture.pipeline.pipeline_config_digest = 'sha256:' + '0'*64
    assert not current(fixture)


def test_currentness_cache_is_bounded_and_does_not_authorize_other_accounts(fixture):
    from app.analytics.currentness import MAX_CURRENTNESS
    assert current(fixture)
    cache = fixture.stores.projections._currentness
    proof = next(iter(cache.entries.values()))
    cache.entries.clear()
    for index in range(MAX_CURRENTNESS + 1):
        cache.entries['synthetic-key-' + str(index)] = proof
    assert current(fixture)
    assert len(cache.entries) == MAX_CURRENTNESS
    assert not fixture.pipeline.projection_is_current('absent-synthetic-account', 1)


def test_storage_change_during_validation_does_not_create_a_reusable_proof(fixture, monkeypatch):
    import app.analytics.currentness as module
    original = module.content_stamp
    serial = [0]
    def changing(db):
        serial[0] += 1
        return (*original(db), serial[0])
    monkeypatch.setattr(module, 'content_stamp', changing)
    assert current(fixture)
    assert not fixture.stores.projections._currentness.entries
    assert current(fixture)
    assert fixture.reads.call_count == 2
