"""Committed graph batches can reclaim WAL progress without changing readers."""

from pathlib import Path
import threading

import pytest

from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.graph_store import GraphStoreError
from app.analytics.sqlite_graph_store import SQLiteGraphGenerationWriter
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture
from tests.test_sqlite_graph_store import SQLiteGraphHarness

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def prepared_writer(path):
    harness = SQLiteGraphHarness(path)
    writer = SQLiteGraphGenerationWriter(harness.database, generation_id=harness.begin_build(),
        partition_key=harness.account_ref, owner=harness.owner, lease_seconds=30)
    with harness.database.transaction() as db:
        db.execute('CREATE TABLE batch_checkpoint_probe (id INTEGER PRIMARY KEY, value BLOB NOT NULL)')
        db.executemany('INSERT INTO batch_checkpoint_probe VALUES (?,?)',
                       [(index, bytes(2048)) for index in range(100)])
    return harness, writer


def connection_policy(connection):
    return tuple(connection.execute('PRAGMA ' + name).fetchone()[0] for name in
                 ('journal_mode', 'synchronous', 'foreign_keys', 'wal_autocheckpoint', 'cache_size'))


def test_no_anchor_checkpoint_reuses_wal_for_repeated_dirty_pages(tmp_path):
    def run(name, checkpoint):
        harness, writer = prepared_writer(tmp_path / name)
        peak = 0
        with writer.lease_session():
            assert harness.database._wal_anchor is None
            policy = connection_policy(writer._write_connection)
            assert policy[:4] == ('wal', 2, 1, 0)
            for index in range(16):
                value = bytes([index + 1]) * 2048
                with writer._owned_transaction() as db:
                    db.execute('UPDATE batch_checkpoint_probe SET value=?', (value,))
                if checkpoint:
                    result = writer._checkpoint_committed_batch()
                    assert result[0] == 0 and result[1] == result[2] > 0
                peak = max(peak, Path(str(harness.database.path) + '-wal').stat().st_size)
            assert connection_policy(writer._write_connection) == policy
            assert writer._write_connection.execute(
                'SELECT count(*) FROM batch_checkpoint_probe WHERE value=?', (value,)).fetchone()[0] == 100
        assert harness.database.open_connection_count(harness.database.path) == 0
        return peak

    unchecked = run('unchecked.sqlite3', False)
    checkpointed = run('checkpointed.sqlite3', True)
    assert checkpointed < unchecked / 2


def test_pinned_reader_keeps_its_snapshot_and_checkpoint_can_be_incomplete(tmp_path):
    harness, writer = prepared_writer(tmp_path / 'reader.sqlite3')
    reader = harness.database.connect()
    try:
        with writer.lease_session():
            reader.execute('BEGIN')
            assert bytes(reader.execute('SELECT value FROM batch_checkpoint_probe WHERE id=0').fetchone()[0]) == bytes(2048)
            value = b'x' * 2048
            with writer._owned_transaction() as db:
                db.execute('UPDATE batch_checkpoint_probe SET value=?', (value,))
            result = writer._checkpoint_committed_batch()
            assert result[1] > result[2] >= 0
            assert reader.in_transaction
            assert bytes(reader.execute('SELECT value FROM batch_checkpoint_probe WHERE id=0').fetchone()[0]) == bytes(2048)
            assert bytes(writer._write_connection.execute(
                'SELECT value FROM batch_checkpoint_probe WHERE id=0').fetchone()[0]) == value
            reader.rollback()
            result = writer._checkpoint_committed_batch()
            assert result[0] == 0 and result[1] == result[2] > 0
    finally:
        reader.close()
    assert harness.database.open_connection_count(harness.database.path) == 0


def test_checkpoint_requires_the_session_owner_and_autocommit(tmp_path):
    harness, writer = prepared_writer(tmp_path / 'ownership.sqlite3')
    with pytest.raises(GraphStoreError, match='graph_checkpoint_session_missing'):
        writer._checkpoint_committed_batch()
    assert harness.database.open_connection_count(harness.database.path) == 0
    with writer.lease_session():
        statements = []
        writer._write_connection.set_trace_callback(statements.append)
        with writer._owned_transaction():
            with pytest.raises(GraphStoreError, match='graph_checkpoint_transaction_active'):
                writer._checkpoint_committed_batch()
        failures = []

        def other_thread():
            try:
                writer._checkpoint_committed_batch()
            except GraphStoreError as error:
                failures.append(str(error))

        thread = threading.Thread(target=other_thread)
        thread.start(); thread.join(timeout=5)
        assert not thread.is_alive()
        assert failures == ['graph_lease_session_busy']
        assert not any('wal_checkpoint' in sql.lower() for sql in statements)
        writer._write_connection.set_trace_callback(None)
    assert harness.database.open_connection_count(harness.database.path) == 0


@pytest.mark.parametrize('failure', ['cancel', 'revoke'])
def test_checkpoint_boundary_failure_keeps_partial_generation_invisible(tmp_path, monkeypatch, failure):
    fixture = make_fixture(tmp_path, conversations=3, messages=30)
    checkpoint = SQLiteGraphGenerationWriter._checkpoint_committed_batch
    interrupted = False

    def interrupt(writer):
        nonlocal interrupted
        result = checkpoint(writer)
        if not interrupted:
            interrupted = True
            if failure == 'revoke':
                with fixture.stores.database.transaction() as db:
                    db.execute("UPDATE projection_generations SET owner_capability_digest=? WHERE status='building'",
                               ('sha256:' + 'f' * 64,))
        return result

    monkeypatch.setattr(SQLiteGraphGenerationWriter, '_checkpoint_committed_batch', interrupt)
    try:
        error = ProjectionBuildCancelled if failure == 'cancel' else GraphStoreError
        with pytest.raises(error):
            fixture.pipeline.build_candidate(ACCOUNT,
                cancellation_check=lambda: interrupted and failure == 'cancel')
        assert interrupted
        assert fixture.stores.database.active_generation(ACCOUNT) is None
        assert fixture.stores.database.open_connection_count(fixture.stores.database.path) == 0
        with fixture.stores.database.read() as db:
            assert not db.execute("SELECT 1 FROM projection_generations WHERE status!='building'").fetchall()
            assert not db.execute('SELECT 1 FROM graph_segments WHERE sealed=1').fetchall()
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        cleanup(fixture)
