"""Lossless chunk storage preserves canonical bytes and migration admission."""

import hashlib
from pathlib import Path
import shutil
from types import SimpleNamespace
import zlib

import pytest

from app.analytics import graph_chunk_encoding as codec, shared_graph
from app.analytics.database import ProjectionsDatabase
from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.opaque_refs import account_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.analytics.validation_receipt import content_stamp
from app.persistence import sqlite_api
from app.persistence.migrations import SchemaCompatibilityError, load_migration_catalog
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def db():
    connection = sqlite_api.connect(':memory:')
    connection.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name,checksum)')
    connection.execute('INSERT INTO schema_migrations VALUES (32,?,?)',
                       ('graph_chunk_encoding', codec.ADMISSION_CHECKSUM))
    connection.execute('PRAGMA user_version=32')
    try:
        yield connection
    finally:
        connection.close()


def envelope(raw, *, declared=None):
    return (codec.MAGIC + bytes((codec.VERSION, codec.CODEC_ZLIB))
            + (len(raw) if declared is None else declared).to_bytes(8, 'big') + zlib.compress(raw))


def test_round_trip_keeps_digest_and_raw_when_encoding_would_enlarge_it(db):
    raw = b'{"record":"' + b'repeated canonical fields ' * 10000 + b'"}'
    encoded = codec.encode(db, raw)
    assert encoded.startswith(codec.MAGIC) and len(encoded) < len(raw)
    assert codec.decode(db, encoded) == raw
    assert hashlib.sha256(codec.decode(db, encoded)).digest() == hashlib.sha256(raw).digest()
    assert codec.encode(db, b'{}') == b'{}'
    assert codec.decode(db, raw) is raw


@pytest.mark.parametrize('change', [
    'DELETE FROM schema_migrations',
    "UPDATE schema_migrations SET name='other'",
    "UPDATE schema_migrations SET checksum='false'",
    'DROP TABLE schema_migrations',
    'PRAGMA user_version=31',
])
def test_unadmitted_catalog_cannot_emit_or_accept_envelopes(db, change):
    raw = b'{"record":"' + b'x' * 2000 + b'"}'
    encoded = codec.encode(db, raw)
    db.execute(change)
    assert not codec.admitted(db)
    assert codec.encode(db, raw) is raw
    assert codec.decode(db, raw) is raw
    with pytest.raises(codec.GraphChunkEncodingError):
        codec.decode(db, encoded)


@pytest.mark.parametrize('damage', [
    'magic', 'short', 'version', 'codec', 'zero_length', 'oversized_length',
    'short_length', 'long_length', 'truncated', 'trailing', 'concatenated', 'invalid_stream',
])
def test_malformed_envelopes_are_refused(db, damage):
    raw = b'{"record":"' + b'x' * 2000 + b'"}'
    encoded = envelope(raw)
    prefix = codec.MAGIC + bytes((codec.VERSION, codec.CODEC_ZLIB))
    values = {
        'magic': b'\x00invalid' + encoded,
        'short': encoded[:codec.HEADER_BYTES - 1],
        'version': codec.MAGIC + b'\x02' + encoded[len(codec.MAGIC) + 1:],
        'codec': codec.MAGIC + b'\x01\x02' + encoded[len(codec.MAGIC) + 2:],
        'zero_length': prefix + bytes(8) + encoded[codec.HEADER_BYTES:],
        'oversized_length': prefix + (codec.MAX_CHUNK_BYTES + 1).to_bytes(8, 'big') + encoded[codec.HEADER_BYTES:],
        'short_length': envelope(raw, declared=len(raw) - 1),
        'long_length': envelope(raw, declared=len(raw) + 1),
        'truncated': encoded[:-1],
        'trailing': encoded + b'trailing',
        'concatenated': encoded + zlib.compress(raw),
        'invalid_stream': encoded[:codec.HEADER_BYTES] + b'invalid',
    }
    with pytest.raises(codec.GraphChunkEncodingError):
        codec.decode(db, values[damage])


def test_bomb_is_bounded_by_declared_output_before_expansion(db, monkeypatch):
    monkeypatch.setattr(codec, 'MAX_CHUNK_BYTES', 4096)
    encoded = envelope(b'x' * 1024 * 1024, declared=16)
    original = zlib.decompressobj
    requested = []

    class BoundedDecoder:
        def __init__(self):
            self.native = original()
        def __getattr__(self, name):
            return getattr(self.native, name)
        def decompress(self, data, maximum):
            requested.append(maximum)
            return self.native.decompress(data, maximum)

    monkeypatch.setattr(codec.zlib, 'decompressobj', BoundedDecoder)
    with pytest.raises(codec.GraphChunkEncodingError):
        codec.decode(db, encoded)
    assert requested == [17]


def test_raw_and_encoded_schema_ceiling_remains_distinct_from_full_writer(db, monkeypatch):
    raw = b'{"record":"' + b'x' * 80 + b'"}'
    monkeypatch.setattr(codec, 'MAX_CHUNK_BYTES', 128)
    monkeypatch.setattr(shared_graph, 'MAX_SEGMENT_CHUNK_BYTES', 64)
    graph = SimpleNamespace(nodes={'node': raw.decode()}, edges={})
    plan = SimpleNamespace(kind='node', keys=['node'])
    assert shared_graph._segment_chunk(graph, plan, lambda: None) is None
    assert codec.decode(db, raw) is raw
    assert codec.decode(db, codec.encode(db, raw)) == raw
    with pytest.raises(codec.GraphChunkEncodingError):
        codec.decode(db, b'x' * 129)
    with pytest.raises(codec.GraphChunkEncodingError):
        codec.encode(db, b'x' * 129)


@pytest.mark.parametrize('operation', ['encode', 'decode'])
@pytest.mark.parametrize('error', [ProjectionBuildCancelled, ValueError])
def test_block_checks_propagate_cancellation_unchanged(db, operation, error):
    raw = b'{"record":"' + b'x' * (codec.BLOCK_BYTES * 5) + b'"}'
    payload = raw if operation == 'encode' else codec.encode(db, raw)
    calls = 0
    cancellation = error()

    def check():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise cancellation

    with pytest.raises(error) as caught:
        getattr(codec, operation)(db, payload, check=check)
    assert caught.value is cancellation and calls == 4


def stored_chunks(database):
    with database.read() as connection:
        return [tuple(row) for row in connection.execute(
            'SELECT segment_id,canonical_bytes,canonical_digest FROM graph_segment_chunks ORDER BY segment_id')]


def test_forward_admission_preserves_raw_rows_backup_and_mixed_public_artifacts(tmp_path):
    fixture = make_fixture(tmp_path / 'canonical', backend='memory')
    catalog = tmp_path / 'catalog31'
    catalog.mkdir()
    source = Path(__file__).parents[1] / 'app/analytics/sql'
    for migration in sorted(source.glob('*.sql'))[:31]:
        shutil.copy2(migration, catalog / migration.name)
    legacy = ProjectionsDatabase(tmp_path / 'legacy.sqlite3', migrations_dir=catalog)
    options = dict(activation=fixture.repositories.projection_activation,
                   canonical_identity_reader=fixture.source.read_identity, rollback_retention=2)
    store = SQLiteAnalyticsProjectionStore(legacy, **options)
    pipeline = AnalyticsPipeline(fixture.source, projections=store,
                                enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
    try:
        first = pipeline.project_account(ACCOUNT).artifact
        before = stored_chunks(legacy)
        assert before and all(not data.startswith(codec.MAGIC) for _, data, _ in before)
        with legacy.read() as connection:
            stamp = content_stamp(connection)
            schema_cookie = connection.execute('PRAGMA schema_version').fetchone()[0]
        upgraded = ProjectionsDatabase(legacy.path)
        assert stored_chunks(upgraded) == before
        with upgraded.read() as connection:
            assert codec.admitted(connection)
            assert connection.execute('PRAGMA schema_version').fetchone()[0] == schema_cookie
            assert content_stamp(connection) == stamp
        admission = load_migration_catalog(source)[-1]
        assert (admission.version, admission.name, admission.checksum) == (32, 'graph_chunk_encoding', codec.ADMISSION_CHECKSUM)
        with upgraded.open_detached(upgraded.migration_runner.last_backup_path, read_only=True) as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 31
            assert [tuple(row) for row in connection.execute(
                'SELECT segment_id,canonical_bytes,canonical_digest FROM graph_segment_chunks ORDER BY segment_id')] == before
        with pytest.raises(SchemaCompatibilityError):
            ProjectionsDatabase(legacy.path, migrations_dir=catalog)
        current = SQLiteAnalyticsProjectionStore(upgraded, **options)
        assert current.get_artifact(ACCOUNT) == first
        pipeline.projections, pipeline.graph = current, current.graph
        with fixture.repositories.database.transaction() as connection:
            insert_message(connection, 'chat-1', 'mixed-encoding-message', NOW)
            advance(connection)
        result = pipeline.project_account(ACCOUNT).artifact
        fixture.pipeline = pipeline
        cold_equal(fixture, result)
        assert current.get_artifact(ACCOUNT) == result
        chunks = stored_chunks(upgraded)
        assert any(data.startswith(codec.MAGIC) for _, data, _ in chunks)
        assert any(not data.startswith(codec.MAGIC) for _, data, _ in chunks)
        with upgraded.read() as connection:
            assert all(hashlib.sha256(codec.decode(connection, data)).hexdigest() == digest for _, data, digest in chunks)
            assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        current.rollback_retention = 0
        current.collect_garbage(result.projection.account_ref)
        current.clear(ACCOUNT)
        assert stored_chunks(upgraded) == []
    finally:
        cleanup(fixture)


def test_full_and_incremental_chunks_reduce_storage_and_preserve_canonical_artifacts(tmp_path, monkeypatch):
    from app.analytics import incremental_graph

    fixture = make_fixture(tmp_path, conversations=3, messages=20)
    fixture.stores.projections.rollback_retention = 0
    original = incremental_graph.write_incremental_graph
    incremental_calls = []

    def observed(*args, **kwargs):
        incremental_calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(incremental_graph, 'write_incremental_graph', observed)
    try:
        first = fixture.pipeline.project_account(ACCOUNT).artifact
        cold_equal(fixture, first)
        chunks = stored_chunks(fixture.stores.database)
        with fixture.stores.database.read() as connection:
            raw_bytes = sum(len(codec.decode(connection, data)) for _, data, _ in chunks)
        assert sum(len(data) for _, data, _ in chunks) < raw_bytes
        with fixture.repositories.database.transaction() as connection:
            insert_message(connection, 'chat-1', 'encoded-incremental-message', NOW)
            advance(connection)
        second = fixture.pipeline.project_account(ACCOUNT).artifact
        assert incremental_calls
        cold_equal(fixture, second)
        assert fixture.stores.projections.get_artifact(ACCOUNT) == second
        fixture.stores.projections.clear(ACCOUNT)
        assert stored_chunks(fixture.stores.database) == []
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('damage', ['truncated', 'unadmitted'])
def test_verified_read_boundaries_refuse_invalid_encoding_without_weakening_metadata(tmp_path, damage):
    from tests.test_graph_chunk_metadata import active_proof

    fixture = make_fixture(tmp_path, conversations=1, messages=3)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        proof = active_proof(fixture)
        item = proof.segments[0]
        plan = shared_graph.SegmentValidation(
            item.kind, item.bucket, item.segment_id, item.digest, item.count,
            False, item.categories, item.chunk_digest)
        validation = shared_graph.SharedGraphValidation('', (plan,), proof, (), ())
        with fixture.stores.database.read() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if damage == 'truncated':
                    connection.execute('DROP TRIGGER graph_segment_chunks_immutable')
                    connection.execute('UPDATE graph_segment_chunks SET canonical_bytes=? WHERE segment_id=?',
                                       (codec.MAGIC, item.segment_id))
                else:
                    connection.execute('UPDATE schema_migrations SET checksum=? WHERE version=32', ('false',))
                assert shared_graph.verified_segment_chunks_complete(connection, account_ref(ACCOUNT), proof)
                assert shared_graph.verified_segment_chunk(
                    connection, account_ref(ACCOUNT), proof, item.kind, item.bucket) is None
                with pytest.raises(GraphReferentialIntegrityError, match='graph_segment_chunk_invalid'):
                    shared_graph._verified_changed_segment_chunks(
                        connection, account_ref(ACCOUNT), validation, lambda: None)
            finally:
                connection.rollback()
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('incremental', [False, True])
def test_cancelled_encoding_preserves_active_generation_and_retry(tmp_path, monkeypatch, incremental):
    fixture = make_fixture(tmp_path, conversations=2, messages=5)
    fixture.stores.projections.rollback_retention = 0
    original = codec.encode
    interrupted = False

    def interrupt(connection, raw, *, check=lambda: None):
        nonlocal interrupted
        interrupted = True
        return original(connection, raw, check=check)

    try:
        previous = None
        if incremental:
            fixture.pipeline.project_account(ACCOUNT)
            previous = fixture.stores.database.active_generation(ACCOUNT).generation_id
            with fixture.repositories.database.transaction() as connection:
                insert_message(connection, 'chat-1', 'cancel-encoded-incremental', NOW)
                advance(connection)
        monkeypatch.setattr(codec, 'encode', interrupt)
        with pytest.raises(ProjectionBuildCancelled):
            fixture.pipeline.build_candidate(ACCOUNT, cancellation_check=lambda: interrupted)
        assert interrupted
        active = fixture.stores.database.active_generation(ACCOUNT)
        assert (None if active is None else active.generation_id) == previous
        assert fixture.stores.database.open_connection_count(fixture.stores.database.path) == 0
        with fixture.stores.database.read() as connection:
            cancelled = connection.execute(
                "SELECT * FROM projection_generations WHERE status='building'"
            ).fetchall()
            references = connection.execute(
                'SELECT COUNT(*) FROM generation_graph_segments WHERE generation_id=?',
                (cancelled[0]['generation_id'],)).fetchone()[0] if cancelled else 0
        assert len(cancelled) == 1
        cancelled = cancelled[0]
        assert cancelled['status'] == 'building' and references > 0
        assert tuple(cancelled[field] for field in (
            'owner_id', 'owner_pid', 'owner_process_started_at',
            'owner_instance_nonce', 'owner_capability_digest'
        )) == fixture.stores.projections.build_owner.persisted_identity
        fixture.stores.projections.discard_generation(cancelled['generation_id'])
        with fixture.stores.database.read() as connection:
            assert connection.execute(
                'SELECT 1 FROM generation_graph_segments WHERE generation_id=?',
                (cancelled['generation_id'],)).fetchone() is None
            assert connection.execute(
                'SELECT 1 FROM projection_generations WHERE generation_id=?',
                (cancelled['generation_id'],)).fetchone() is None
        active = fixture.stores.database.active_generation(ACCOUNT)
        assert (None if active is None else active.generation_id) == previous
        monkeypatch.setattr(codec, 'encode', original)
        result = fixture.pipeline.project_account(ACCOUNT).artifact
        cold_equal(fixture, result)
        fixture.stores.projections.clear(ACCOUNT)
        assert stored_chunks(fixture.stores.database) == []
    finally:
        cleanup(fixture)
