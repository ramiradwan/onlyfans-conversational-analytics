"""Keep publication checks complete while avoiding unrelated database work."""

from unittest.mock import Mock

import pytest

from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture
from tests.test_sqlite_graph_store import SQLiteGraphHarness, graph_node
from app.analytics.sqlite_graph_store import SQLiteGraphGenerationWriter


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path)
    yield value
    cleanup(value)


def test_open_checks_database_but_publication_and_reads_check_only_candidate(fixture, monkeypatch):
    db = fixture.stores.database
    connect = db.connect
    checks = []
    def traced():
        connection = connect()
        connection.set_trace_callback(lambda sql: checks.append(sql) if sql.startswith(
            ('PRAGMA integrity_check', 'PRAGMA foreign_key_check')) else None)
        return connection
    monkeypatch.setattr(db, 'connect', traced)
    fixture.stores.projections.reconcile_startup()
    assert checks == ['PRAGMA integrity_check', 'PRAGMA foreign_key_check']
    checks.clear()
    result = fixture.pipeline.project_account(ACCOUNT)
    assert fixture.stores.projections.get_artifact(ACCOUNT) == result.artifact
    assert fixture.stores.projections.get(ACCOUNT) == result.artifact.projection
    assert checks == []


def test_artifact_returns_the_records_validated_in_one_snapshot(fixture, monkeypatch):
    import app.analytics.sqlite_projection_store as module
    expected = fixture.pipeline.project_account(ACCOUNT).artifact
    scan = Mock(wraps=module._generation_graph)
    monkeypatch.setattr(module, '_generation_graph', scan)
    observed = fixture.stores.projections.get_artifact(ACCOUNT)
    assert scan.call_count == 1
    assert observed == expected
    observed.nodes[0].properties.clear()
    assert fixture.stores.projections.get_artifact(ACCOUNT) == expected


@pytest.mark.parametrize('published', [False, True])
def test_candidate_endpoint_validation_does_not_depend_on_full_file_scan(fixture, published):
    fixture.pipeline.compact_graph = False
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    if published:
        fixture.pipeline.publish_candidate(candidate)
    generation = candidate.staged_generation_id
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('DROP TRIGGER graph_node_delete_guard')
        db.execute("DELETE FROM graph_nodes WHERE generation_id=? AND kind='topic'", (generation,))
    with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
        fixture.stores.projections._validate_persisted_generation(generation)
    with pytest.raises(GraphReferentialIntegrityError):
        fixture.stores.projections.reconcile_startup()


def test_corrupt_pending_graph_is_refused_at_activation(fixture):
    fixture.pipeline.compact_graph = False
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        db.execute('DROP TRIGGER graph_node_building_update')
        db.execute("UPDATE graph_nodes SET properties_json=? WHERE generation_id=? AND kind='topic'",
                   ('{"taxonomy_id":"pricing","label":"Wrong label"}', candidate.staged_generation_id))
    with pytest.raises(ProjectionValidationError):
        fixture.pipeline.publish_candidate(candidate)
    assert fixture.stores.projections.get(ACCOUNT) is None


def test_generation_sequence_does_not_parse_projection_bodies(fixture, monkeypatch):
    from app.models.analytics import AnalyticsProjection
    result = fixture.pipeline.project_account(ACCOUNT)
    parse = Mock(side_effect=AssertionError('unexpected projection parsing'))
    monkeypatch.setattr(AnalyticsProjection, 'model_validate_json', parse)
    assert fixture.stores.projections.next_projection_generation(ACCOUNT) == result.artifact.projection.projection_generation + 1
    parse.assert_not_called()


def test_write_batch_growth_is_bounded_and_slow_work_shrinks_it(tmp_path):
    from app.analytics.sqlite_graph_store import _MAX_GRAPH_WRITE_CHUNK, _MIN_GRAPH_WRITE_CHUNK
    harness = SQLiteGraphHarness(tmp_path/'batches.sqlite3')
    writer = SQLiteGraphGenerationWriter(harness.database, generation_id=harness.begin_build(),
        partition_key=harness.account_ref, owner=harness.owner, lease_seconds=30)
    for _ in range(20):
        writer._record_operation_duration(0.001, allow_growth=True)
    assert writer._chunk_size == _MAX_GRAPH_WRITE_CHUNK
    writer._record_operation_duration(30)
    assert _MIN_GRAPH_WRITE_CHUNK <= writer._chunk_size < _MAX_GRAPH_WRITE_CHUNK
    for _ in range(20):
        writer._record_operation_duration(30)
    assert writer._chunk_size == _MIN_GRAPH_WRITE_CHUNK


def test_chunked_writes_release_every_connection(tmp_path):
    harness = SQLiteGraphHarness(tmp_path/'connections.sqlite3')
    generation = harness.begin_build()
    writer = SQLiteGraphGenerationWriter(harness.database, generation_id=generation,
        partition_key=harness.account_ref, owner=harness.owner, lease_seconds=30)
    nodes = [graph_node(f'synthetic-batch-{i}') for i in range(5000)]
    writer.replace(nodes=nodes, edges=[])
    assert writer._heartbeat_thread is None
    assert harness.database.open_connection_count(harness.database.path) == 0
    with harness.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM graph_nodes WHERE generation_id=?', (generation,)).fetchone()[0] == 5000


@pytest.mark.parametrize('table,trigger', [('graph_nodes', 'graph_node_building_insert'),
                                         ('graph_edges', 'graph_edge_building_insert')])
def test_candidate_checks_cross_account_children(fixture, table, trigger):
    from app.analytics.opaque_refs import account_ref
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute(f'DROP TRIGGER {trigger}')
        record = dict(db.execute(f'SELECT * FROM {table} LIMIT 1').fetchone())
        record['creator_account_id'] = account_ref('synthetic-other-owner')
        columns = ','.join(record)
        marks = ','.join('?' for _ in record)
        db.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})', tuple(record.values()))
    with pytest.raises(GraphReferentialIntegrityError, match='projection_account_mismatch'):
        fixture.stores.projections._validate_persisted_generation(candidate.staged_generation_id)


def test_retired_cleanup_rolls_back_edges_when_endpoint_removal_fails(fixture, monkeypatch):
    from contextlib import contextmanager
    fixture.stores.projections.rollback_retention = 2
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        db.execute('UPDATE account_heads SET canonical_revision=canonical_revision+1')
    latest = fixture.pipeline.project_account(ACCOUNT).artifact
    fixture.stores.projections.rollback_retention = 0
    database = fixture.stores.database
    transaction = database.transaction
    with database.read() as db:
        before = db.execute('SELECT COUNT(*) FROM graph_edges').fetchone()[0]
    class FailedConnection:
        def __init__(self, connection):
            self.connection = connection
        @property
        def in_transaction(self):
            return self.connection.in_transaction
        def execute(self, sql, parameters=()):
            if sql.startswith(('DELETE FROM graph_nodes', 'DELETE FROM graph_owned_nodes')):
                raise RuntimeError('synthetic cleanup interruption')
            return self.connection.execute(sql, parameters)
    @contextmanager
    def failing_transaction():
        with transaction() as connection:
            yield FailedConnection(connection)
    with monkeypatch.context() as patch:
        patch.setattr(database, 'transaction', failing_transaction)
        with pytest.raises(RuntimeError, match='synthetic cleanup'):
            fixture.stores.projections.collect_garbage(latest.projection.account_ref)
    with database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM graph_edges').fetchone()[0] == before
    assert fixture.stores.projections.get_artifact(ACCOUNT) == latest
    assert fixture.stores.projections.collect_garbage(latest.projection.account_ref) == 1
    assert fixture.stores.projections.get_artifact(ACCOUNT) == latest


def test_writer_cache_is_local_to_the_owned_session(tmp_path):
    from app.analytics.database import GENERATION_WRITE_CACHE_KIB
    harness = SQLiteGraphHarness(tmp_path/'cache.sqlite3')
    writer = SQLiteGraphGenerationWriter(harness.database, generation_id=harness.begin_build(),
        partition_key=harness.account_ref, owner=harness.owner, lease_seconds=30)
    with writer.lease_session():
        assert writer._write_connection.execute('PRAGMA cache_size').fetchone()[0] == -GENERATION_WRITE_CACHE_KIB
    assert writer._write_connection is None
    with harness.database.read() as db:
        assert db.execute('PRAGMA cache_size').fetchone()[0] != -GENERATION_WRITE_CACHE_KIB
    assert harness.database.open_connection_count(harness.database.path) == 0


@pytest.mark.parametrize('published', [False, True])
def test_candidate_rejects_a_missing_local_publication_epoch(fixture, published):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    if published:
        fixture.pipeline.publish_candidate(candidate)
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('DROP TRIGGER projection_publication_epoch_delete_blocked')
        db.execute('DELETE FROM projection_publication_epochs WHERE publication_epoch=?',
                   (candidate.publication_epoch,))
    with pytest.raises(GraphReferentialIntegrityError, match='projection_epoch_absent'):
        fixture.stores.projections._validate_persisted_generation(candidate.staged_generation_id)
