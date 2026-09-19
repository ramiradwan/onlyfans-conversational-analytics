"""Check bounded cache staging against canonical records and transaction rollback."""

from dataclasses import FrozenInstanceError
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.analytics import enrichment_cache as cache
from app.analytics.enrichment_cache import CachedEnrichment, EnrichmentReuse
from app.analytics.enrichment_sql import INSERT_BATCH_SIZE, insert_entries
from app.analytics.errors import ProjectionBuildCancelled
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup


@pytest.fixture(scope='module')
def sample(tmp_path_factory):
    value = make_fixture(tmp_path_factory.mktemp('cache-staging'), conversations=3, messages=30)
    try:
        artifact = value.pipeline.project_account(ACCOUNT).artifact
        with value.stores.database.read() as db:
            entries = tuple(row[0].encode() for row in db.execute(
                'SELECT document_json FROM enrichment_reuse ORDER BY cache_key'))
        assert len(entries) == 270
        yield SimpleNamespace(artifact=artifact, entries=entries)
    finally:
        cleanup(value)


def test_storage_records_match_independent_canonical_encoding(sample):
    records = list(cache.storage_entries(sample.artifact, sample.entries))
    for raw, record in zip(sample.entries, records, strict=True):
        expected = CachedEnrichment.model_validate_json(raw)
        text = expected.model_dump_json()
        assert record.account_ref == expected.key.account_ref
        assert record.cache_key == expected.key.digest
        assert record.expires_at == expected.key.expires_at.isoformat()
        assert record.document_json == text
        assert record.document_digest == hashlib.sha256(text.encode()).hexdigest()
        with pytest.raises(FrozenInstanceError):
            record.document_json = '{}'
    assert cache.validate_entries(sample.artifact, sample.entries) == [
        CachedEnrichment.model_validate_json(raw) for raw in sample.entries]


@pytest.mark.parametrize('formatted', [False, True])
def test_normalized_storage_does_not_depend_on_input_whitespace(sample, formatted):
    raw = sample.entries[0]
    if formatted:
        raw = json.dumps(json.loads(raw), indent=2).encode()
    assert list(cache.storage_entries(sample.artifact, (raw,))) == list(
        cache.storage_entries(sample.artifact, (sample.entries[0],)))


def test_empty_entries_require_no_database_work(sample):
    db = Mock()
    insert_entries(db, 'synthetic-generation', cache.storage_entries(sample.artifact, ()))
    db.executemany.assert_not_called()


@pytest.mark.parametrize('count', [1, 63, 64, 65, 129, 270])
def test_sql_batches_are_bounded_without_owning_the_transaction(sample, count):
    db = Mock()
    insert_entries(db, 'synthetic-generation',
                   cache.storage_entries(sample.artifact, sample.entries[:count]))
    sizes = [len(call.args[1]) for call in db.executemany.call_args_list]
    assert sum(sizes) == count and max(sizes) <= INSERT_BATCH_SIZE == 64
    assert len(sizes) == (count + INSERT_BATCH_SIZE - 1) // INSERT_BATCH_SIZE
    assert {row[0] for call in db.executemany.call_args_list for row in call.args[1]} == {
        'synthetic-generation'}
    db.execute.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_validation_is_lazy_and_checks_each_record(sample):
    check = Mock()
    iterator = cache.storage_entries(sample.artifact, sample.entries, check=check)
    check.assert_not_called()
    first = next(iterator)
    assert first.document_json.encode() == sample.entries[0]
    assert check.call_count >= 2
    check.side_effect = ProjectionBuildCancelled
    with pytest.raises(ProjectionBuildCancelled):
        next(iterator)


@pytest.mark.parametrize('mutation', [
    'missing', 'account', 'conversation', 'result', 'earlier_expiry', 'later_expiry',
    'unknown_field', 'unknown_slot', 'duplicate', 'malformed',
])
def test_invalid_records_fail_in_model_and_storage_paths(sample, mutation):
    value = json.loads(sample.entries[0])
    if mutation == 'missing':
        value['key']['message_ref'] = 'm1:' + '0' * 64
    elif mutation == 'account':
        value['key']['account_ref'] = 'a1:' + '0' * 64
    elif mutation == 'conversation':
        value['key']['conversation_ref'] = 'c1:' + '0' * 64
    elif mutation == 'result':
        value['result_json'] = '{}'
    elif mutation == 'earlier_expiry':
        value['key']['expires_at'] = '2000-01-01T00:00:00Z'
    elif mutation == 'later_expiry':
        value['key']['expires_at'] = '2999-01-01T00:00:00Z'
    elif mutation == 'unknown_field':
        value['extra'] = 'not allowed'
    elif mutation == 'unknown_slot':
        value['key']['slot'] = 'other'
    raw = b'{' if mutation == 'malformed' else json.dumps(value).encode()
    entries = (raw, raw) if mutation == 'duplicate' else (raw,)
    for operation in (cache.validate_entries, cache.storage_entries):
        with pytest.raises(ValueError):
            list(operation(sample.artifact, entries))


@pytest.mark.parametrize('name,value', [
    ('MAX_CACHE_ENTRIES', 0), ('MAX_CACHE_BYTES', 1), ('MAX_ENTRY_BYTES', 1),
])
def test_original_size_limits_apply_to_both_validation_paths(sample, monkeypatch, name, value):
    monkeypatch.setattr(cache, name, value)
    for operation in (cache.validate_entries, cache.storage_entries):
        with pytest.raises(ValueError):
            list(operation(sample.artifact, sample.entries[:1]))


def test_retaining_a_checked_record_uses_one_key_hash(sample, monkeypatch):
    entry = CachedEnrichment.model_validate_json(sample.entries[0])
    result = entry.result()
    expected = EnrichmentReuse(None, ACCOUNT, lambda: NOW)
    expected.retain(entry.key, result)
    digest = Mock(wraps=cache.fingerprint)
    monkeypatch.setattr(cache, 'fingerprint', digest)
    actual = EnrichmentReuse(None, ACCOUNT, lambda: NOW)
    actual.retain_record(entry)
    assert actual.entries == expected.entries
    assert actual.bytes_used == expected.bytes_used
    assert actual.conversation_entries(entry.key.conversation_ref) == tuple(expected.entries.values())
    assert digest.call_count == 1


def test_checked_retention_does_not_parse_the_result_again(sample, monkeypatch):
    entry = CachedEnrichment.model_validate_json(sample.entries[0])
    monkeypatch.setattr(CachedEnrichment, 'result', Mock(side_effect=AssertionError('result parsed')))
    reuse = EnrichmentReuse(None, ACCOUNT, lambda: NOW)
    reuse.retain_record(entry)
    assert tuple(reuse.entries.values()) == sample.entries[:1]


@pytest.mark.parametrize('limit,value', [
    ('MAX_CACHE_ENTRIES', 0), ('MAX_CACHE_BYTES', 1), ('MAX_ENTRY_BYTES', 1),
])
def test_checked_retention_keeps_existing_caps(sample, monkeypatch, limit, value):
    entry = CachedEnrichment.model_validate_json(sample.entries[0])
    monkeypatch.setattr(cache, limit, value)
    reuse = EnrichmentReuse(None, ACCOUNT, lambda: NOW)
    reuse.retain_record(entry)
    assert reuse.entries == {} and reuse.bytes_used == 0


def test_checked_retention_expires_without_sliding_deadline(sample):
    entry = CachedEnrichment.model_validate_json(sample.entries[0])
    reuse = EnrichmentReuse(None, ACCOUNT, lambda: entry.key.expires_at)
    reuse.retain_record(entry)
    assert reuse.entries == {}


def test_staging_rejects_unchecked_result_retained_from_a_caller(sample):
    entry = CachedEnrichment.model_validate_json(sample.entries[0])
    forged = entry.model_copy(update={'result_json': '{}'})
    reuse = EnrichmentReuse(None, ACCOUNT, lambda: NOW)
    reuse.retain_record(forged)
    with pytest.raises(ValueError):
        list(cache.storage_entries(sample.artifact, tuple(reuse.entries.values())))


@pytest.mark.parametrize('failure', ['invalid', 'cancelled'])
def test_partial_cache_batch_is_rolled_back_with_candidate(tmp_path, monkeypatch, failure):
    from app.analytics import enrichment_sql
    fixture = make_fixture(tmp_path, conversations=1, messages=30)
    try:
        artifact = fixture.pipeline.project_account(ACCOUNT).artifact
        with fixture.stores.database.read() as db:
            entries = tuple(row[0].encode() for row in db.execute(
                'SELECT document_json FROM enrichment_reuse ORDER BY cache_key'))
            before = [tuple(row) for row in db.execute(
                'SELECT generation_id,cache_key,document_digest FROM enrichment_reuse ORDER BY cache_key')]
            generations = [tuple(row) for row in db.execute('SELECT generation_id,status FROM projection_generations')]
        assert len(entries) > INSERT_BATCH_SIZE
        batches = []
        original = enrichment_sql.insert_entries
        def observe(connection, generation_id, records, *, check):
            class Connection:
                def executemany(self, statement, parameters):
                    result = connection.executemany(statement, parameters)
                    batches.append(len(parameters))
                    return result
            return original(Connection(), generation_id, records, check=check)
        monkeypatch.setattr(enrichment_sql, 'insert_entries', observe)
        if failure == 'invalid':
            entries = entries[:INSERT_BATCH_SIZE] + (b'{}',)
        exception = ValueError if failure == 'invalid' else ProjectionBuildCancelled
        with pytest.raises(exception):
            fixture.stores.projections.stage_artifact(
                artifact, creator_account_id=ACCOUNT,
                canonical_identity=fixture.source.read_identity(ACCOUNT),
                enrichment_entries=entries,
                cancellation_check=lambda: failure == 'cancelled' and bool(batches))
        assert batches == [INSERT_BATCH_SIZE]
        with fixture.stores.database.read() as db:
            assert [tuple(row) for row in db.execute(
                'SELECT generation_id,cache_key,document_digest FROM enrichment_reuse ORDER BY cache_key')] == before
            assert [tuple(row) for row in db.execute('SELECT generation_id,status FROM projection_generations')] == generations
        assert fixture.stores.projections.get(ACCOUNT) == artifact.projection
    finally:
        cleanup(fixture)


def test_canonicalized_document_still_obeys_per_record_byte_limit(sample, monkeypatch):
    original = CachedEnrichment.model_validate_json(sample.entries[0])
    document = json.loads(sample.entries[0])
    document['key']['expires_at'] = int(original.key.expires_at.timestamp())
    raw = json.dumps(document, separators=(',', ':')).encode()
    canonical = CachedEnrichment.model_validate_json(raw).model_dump_json().encode()
    assert len(raw) < len(canonical)
    monkeypatch.setattr(cache, 'MAX_ENTRY_BYTES', len(canonical) - 1)
    with pytest.raises(ValueError, match='enrichment_cache_entry_invalid'):
        list(cache.storage_entries(sample.artifact, (raw,)))


def test_validation_checks_cancellation_before_traversing_projection(sample):
    def cancelled():
        raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        list(cache.storage_entries(None, sample.entries, check=cancelled))
