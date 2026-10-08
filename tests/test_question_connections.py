"""Encrypted read leases retain native handles, never request authority or cursors."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
from weakref import WeakSet

import pytest

from app.analytics.errors import ProjectionBackpressure, ProjectionUnavailable
from app.analytics.question_connections import QuestionConnectionPool, QuestionReadLease
from app.analytics.query_execution import QuestionBudget, QuestionLimits, QuestionLimitExceeded
from app.persistence import sqlite_api
from app.persistence.database import CanonicalSQLite, ProjectionsSQLite, LocalSQLite, SQLiteConfigurationError

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def budget(**kwargs):
    return QuestionBudget(QuestionLimits(), **kwargs)


class Store:
    def __init__(self, database):
        self.database = database
        self.pools = WeakSet()

    def question_connection_database(self, _account, work):
        work.check()
        assert self.database.path.exists()
        return self.database

    def register_question_connections(self, pool):
        self.pools.add(pool)

    def close(self):
        for pool in self.pools:
            pool.invalidate_database(self.database)


@pytest.fixture
def connections(tmp_path):
    canonical = CanonicalSQLite(tmp_path / 'canonical.sqlite3', encryption_key=b'c' * 32)
    analytics = ProjectionsSQLite(tmp_path / 'analytics.sqlite3', encryption_key=b'a' * 32)
    for database in (canonical, analytics):
        with database.transaction() as connection:
            connection.execute('CREATE TABLE records(value INTEGER)')
            connection.executemany('INSERT INTO records VALUES(?)', [(1,), (2,), (3,)])
    source = SimpleNamespace(history=SimpleNamespace(database=canonical))
    pool = QuestionConnectionPool()
    yield SimpleNamespace(pool=pool, source=source, store=Store(analytics),
                          canonical=canonical, analytics=analytics)
    pool.close()
    assert pool.wait_closed(1)
    assert LocalSQLite.open_connection_count(canonical.path) == 0
    assert LocalSQLite.open_connection_count(analytics.path) == 0


def borrow(case, work=None):
    return case.pool.borrow(case.source, case.store, 'account', work or budget())


def test_native_pair_reused_but_every_lease_and_cursor_revoked(connections, monkeypatch):
    case = connections
    opens = []
    for database in (case.canonical, case.analytics):
        original = database.connect
        def opened(original=original):
            native = original(); opens.append(native); return native
        monkeypatch.setattr(database, 'connect', opened)
    leases, cursors = [], []
    for _ in range(2):
        with borrow(case) as pair, ExitStack() as owned:
            for lease in pair:
                owned.enter_context(lease.read())
                cursor = lease.execute('SELECT value FROM records')
                assert cursor.connection is lease
                assert cursor.fetchone()[0] == 1
                leases.append(lease); cursors.append(cursor)
    assert len(opens) == 2
    for lease in leases:
        with pytest.raises(sqlite_api.ProgrammingError): lease.execute('SELECT 1')
    for cursor in cursors:
        with pytest.raises(sqlite_api.ProgrammingError): cursor.fetchone()
    case.pool.close()
    for native in opens:
        with pytest.raises(sqlite_api.ProgrammingError): native.execute('SELECT 1')


@pytest.mark.parametrize('sql', [
    'PRAGMA query_only=OFF', 'PRAGMA foreign_keys=OFF',
    'UPDATE records SET value=9', 'BEGIN', "ATTACH ':memory:' AS escaped",
    'PRAGMA wal_checkpoint',
])
def test_read_facade_cannot_change_configuration_or_write(connections, sql):
    case = connections
    with pytest.raises(sqlite_api.DatabaseError):
        with borrow(case) as (canonical, _), canonical.read():
            canonical.execute(sql)
    assert case.pool.wait_closed(0)
    with case.canonical.read() as connection:
        assert [row[0] for row in connection.execute('SELECT value FROM records')] == [1, 2, 3]


def test_cursor_methods_do_not_escape_to_native_handle(connections):
    with borrow(connections) as (canonical, _), canonical.read():
        cursor = canonical.cursor()
        assert cursor.execute('SELECT 1') is cursor
        assert cursor.connection is canonical
        assert cursor.fetchone()[0] == 1
        for owner in (cursor, canonical):
            with pytest.raises(sqlite_api.ProgrammingError): owner.executescript('SELECT 1')
        for name in ('backup', 'set_authorizer', 'create_function', 'commit', 'close'):
            with pytest.raises(AttributeError): getattr(canonical, name)


def test_partial_cursor_is_closed_before_live_data_version_check(connections):
    case = connections
    with borrow(case) as (canonical, _), canonical.read():
        initial = canonical.execute('PRAGMA data_version').fetchone()[0]
        cursor = canonical.execute('SELECT value FROM records')
        assert cursor.fetchone()[0] == 1
        with case.canonical.transaction() as writer:
            writer.execute('UPDATE records SET value=5 WHERE value=3')
        canonical.close_cursors()
        assert canonical.execute('PRAGMA data_version').fetchone()[0] != initial
        with pytest.raises(sqlite_api.ProgrammingError): cursor.fetchone()


def test_callbacks_and_busy_timeout_do_not_survive_a_lease(connections):
    case = connections
    calls = []
    with borrow(case) as (canonical, _), canonical.read():
        canonical.set_trace_callback(calls.append)
        canonical.set_progress_handler(lambda: calls.append('progress') or 0, 1)
        canonical.execute('PRAGMA busy_timeout=1').fetchall()
        canonical.execute('SELECT value FROM records').fetchall()
    observed = len(calls)
    with borrow(case) as (canonical, _), canonical.read():
        assert canonical.execute('PRAGMA busy_timeout').fetchone()[0] == case.canonical.busy_timeout_ms
        canonical.execute('SELECT value FROM records').fetchall()
    assert len(calls) == observed


def test_warm_entry_and_final_observation_recheck_security(connections, monkeypatch):
    case = connections
    original = case.canonical._observe_private_files
    observations = []
    def secured():
        result = original(); observations.append(result); return result
    monkeypatch.setattr(case.canonical, '_observe_private_files', secured)
    for _ in range(2):
        with borrow(case) as (canonical, _), canonical.read():
            canonical.execute('SELECT 1').fetchall()
            canonical.observe()
    assert 4 <= len(observations) <= 5  # A cold absent sidecar is bound once after initialization.


def test_warm_entry_rejects_changed_file_identity_and_evicts(connections, monkeypatch):
    case = connections
    with borrow(case) as (canonical, _), canonical.read():
        canonical.execute('SELECT 1').fetchall()
    original = case.canonical._observe_private_files
    monkeypatch.setattr(case.canonical, '_observe_private_files', lambda: ((-1, -1), *original()[1:]))
    with pytest.raises(SQLiteConfigurationError, match='file_replaced'):
        with borrow(case) as (canonical, _), canonical.read():
            pytest.fail('replaced native connection became readable')
    assert case.pool.wait_closed(0)


@pytest.mark.parametrize('exception', [QuestionLimitExceeded, asyncio.CancelledError, KeyboardInterrupt])
def test_all_request_exceptions_evict_both_handles(connections, exception):
    case = connections
    with pytest.raises(exception):
        with borrow(case) as pair, ExitStack() as owned:
            for lease in pair: owned.enter_context(lease.read())
            raise exception()
    assert case.pool.wait_closed(0)


def test_two_pair_bound_and_thread_ownership(connections):
    case = connections
    with borrow(case) as first, borrow(case) as second, ExitStack() as owned:
        for lease in (*first, *second): owned.enter_context(lease.read())
        assert LocalSQLite.open_connection_count(case.canonical.path) == 2
        assert LocalSQLite.open_connection_count(case.analytics.path) == 2
        with pytest.raises(ProjectionBackpressure):
            with borrow(case): pytest.fail('third pair admitted')
        with ThreadPoolExecutor(max_workers=1) as worker:
            operation = worker.submit(first[0].execute, 'SELECT 1')
            with pytest.raises(sqlite_api.ProgrammingError): operation.result()


def test_store_close_poison_active_and_closes_idle_without_cross_thread_close(connections):
    case = connections
    with borrow(case) as (canonical, analytics), canonical.read(), analytics.read():
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(case.store.close).result()
        assert LocalSQLite.open_connection_count(case.canonical.path) == 1
        assert LocalSQLite.open_connection_count(case.analytics.path) == 1
        with pytest.raises(ProjectionUnavailable): canonical.execute('SELECT 1')
    assert case.pool.wait_closed(0)


def test_pool_close_waits_for_owner_release_and_rejects_new_borrows(connections):
    case = connections
    with borrow(case) as (canonical, _), canonical.read():
        case.pool.close()
        assert not case.pool.wait_closed(0)
        with pytest.raises(ProjectionUnavailable):
            with borrow(case): pytest.fail('closed owner admitted new work')
    assert case.pool.wait_closed(0)


def test_dirty_native_configuration_is_evicted(connections, monkeypatch):
    case = connections
    original = case.canonical.connect
    native = []
    def opened():
        connection = original(); native.append(connection); return connection
    monkeypatch.setattr(case.canonical, 'connect', opened)
    with pytest.raises(SQLiteConfigurationError, match='configuration_changed'):
        with borrow(case) as (canonical, _), canonical.read():
            native[0].row_factory = None
    assert case.pool.wait_closed(0)


def test_native_close_failure_stays_tracked_until_retry(connections, monkeypatch):
    from app.persistence import database as database_module
    case = connections
    with borrow(case) as (canonical, _), canonical.read():
        canonical.execute('SELECT 1').fetchall()
    original = database_module._TrackedConnection._close_native
    def failed(native):
        if native._tracked_path == case.canonical.path: raise RuntimeError('close unavailable')
        original(native)
    monkeypatch.setattr(database_module._TrackedConnection, '_close_native', failed)
    with pytest.raises(RuntimeError, match='close unavailable'): case.pool.close()
    assert not case.pool.wait_closed(0)
    assert LocalSQLite.open_connection_count(case.canonical.path) == 1
    monkeypatch.setattr(database_module._TrackedConnection, '_close_native', original)
    case.pool.close()
    assert case.pool.wait_closed(0)


def test_security_observation_remains_inside_original_deadline(connections, monkeypatch):
    case = connections
    now = [0.0]
    work = budget(monotonic=lambda: now[0])
    original = case.canonical._observe_private_files
    def slow():
        result = original(); now[0] = 1.0; return result
    monkeypatch.setattr(case.canonical, '_observe_private_files', slow)
    with pytest.raises(QuestionLimitExceeded):
        with borrow(case, work) as (canonical, _), canonical.read():
            pytest.fail('expired original deadline was renewed')
    assert case.pool.wait_closed(0)


@pytest.mark.parametrize('sidecar', [1, 2])
@pytest.mark.parametrize('changed', [None, (-1, -1)])
def test_opened_sidecar_cannot_disappear_or_be_replaced(connections, monkeypatch, sidecar, changed):
    case = connections
    with borrow(case) as (canonical, _), canonical.read():
        canonical.execute('SELECT value FROM records').fetchall()
    original = case.canonical._observe_private_files
    assert original()[sidecar] is not None
    def replaced():
        files = list(original())  # Fresh ACL checks still pass; only lineage changes.
        files[sidecar] = changed
        return tuple(files)
    monkeypatch.setattr(case.canonical, '_observe_private_files', replaced)
    with pytest.raises(SQLiteConfigurationError, match='file_replaced'):
        with borrow(case) as (canonical, _), canonical.read():
            pytest.fail('replaced sidecar became readable')
    assert case.pool.wait_closed(0)


def test_absent_sidecars_bind_only_during_cold_native_initialization(connections, monkeypatch):
    case = connections
    original = case.canonical._observe_private_files
    first = [True]
    def initially_absent():
        files = original()
        if first[0]:
            first[0] = False
            return (files[0], None, None)
        return files
    monkeypatch.setattr(case.canonical, '_observe_private_files', initially_absent)
    with borrow(case) as (canonical, _), canonical.read():
        canonical.execute('SELECT value FROM records').fetchall()
        canonical.observe()
    with borrow(case) as (canonical, _), canonical.read():
        canonical.execute('SELECT value FROM records').fetchall()
    assert LocalSQLite.open_connection_count(case.canonical.path) == 1


def test_cleanup_deadline_failure_evicts_before_pair_can_be_reused(connections, monkeypatch):
    case = connections
    now = [0.0]
    work = budget(monotonic=lambda: now[0])
    original = QuestionReadLease._finish
    def slow_finish(lease):
        original(lease)
        now[0] = 1.0
    monkeypatch.setattr(QuestionReadLease, '_finish', slow_finish)
    with pytest.raises(QuestionLimitExceeded):
        with borrow(case, work) as (canonical, _), canonical.read():
            canonical.execute('SELECT 1').fetchall()
    assert case.pool.wait_closed(0)


def test_cursor_cleanup_preserves_primary_cancellation(connections, monkeypatch):
    case = connections
    original = QuestionReadLease.close_cursors
    def broken_cleanup(lease):
        original(lease)
        raise RuntimeError('cursor cleanup failed')
    monkeypatch.setattr(QuestionReadLease, 'close_cursors', broken_cleanup)
    with pytest.raises(asyncio.CancelledError):
        with borrow(case) as (canonical, _), canonical.read():
            canonical.execute('SELECT value FROM records').fetchone()
            raise asyncio.CancelledError()
    assert case.pool.wait_closed(0)


def test_cross_thread_cursor_gc_defers_native_close_to_lease_owner(connections):
    case = connections
    with borrow(case) as (canonical, _), canonical.read():
        held = [canonical.execute('SELECT value FROM records')]
        held[0].fetchone()
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(held.clear).result()
        assert canonical._native_cursors  # Worker GC did not operate the owner's native handle.
        canonical.close_cursors()
        assert not canonical._native_cursors
