"""Bounded runtime-owned native handles with revoked, request-local read leases."""

from contextlib import contextmanager
from threading import Condition, get_ident
import time
from weakref import WeakSet, ref

from app.analytics.errors import ProjectionBackpressure, ProjectionUnavailable
from app.persistence import sqlite_api
from app.persistence.database import SQLiteConfigurationError


def _read_authorizer(action, first, second, _database, _trigger):
    # SQLITE_PRAGMA and SQLITE_RECURSIVE have stable SQLite action values.
    if action in (sqlite_api.SQLITE_SELECT, sqlite_api.SQLITE_READ,
                  sqlite_api.SQLITE_FUNCTION, 33):
        return sqlite_api.SQLITE_OK
    if action == 19:
        if second is None and first.lower() in (
                'data_version', 'schema_version', 'user_version', 'query_only',
                'foreign_keys', 'synchronous', 'busy_timeout'):
            return sqlite_api.SQLITE_OK
        if first.lower() == 'busy_timeout' and str(second).isdigit():
            return sqlite_api.SQLITE_OK
        if first.lower() == 'query_only' and str(second).upper() in ('ON', '1'):
            return sqlite_api.SQLITE_OK
    return sqlite_api.SQLITE_DENY


class _Cursor:
    def __init__(self, cursor, lease):
        self._cursor = cursor
        self._lease = ref(lease)
        lease._cursors.add(self)
        lease._native_cursors[id(self)] = cursor

    def _check(self):
        lease = self._lease()
        if self._cursor is None or lease is None:
            raise sqlite_api.ProgrammingError('question_cursor_closed')
        lease._check()
        return self._cursor

    def __getattr__(self, name):
        if name not in ('description', 'rowcount', 'lastrowid', 'arraysize'):
            raise AttributeError(name)
        return getattr(self._check(), name)

    @property
    def connection(self):
        self._check()
        return self._lease()

    def execute(self, *args, **kwargs):
        self._check().execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        self._check().executemany(*args, **kwargs)
        return self

    def executescript(self, *args, **kwargs):
        raise sqlite_api.ProgrammingError('question_scripts_forbidden')

    def __iter__(self):
        self._check()
        return self

    def __next__(self):
        try:
            return next(self._check())
        except StopIteration:
            self.close()
            raise

    def fetchone(self):
        value = self._check().fetchone()
        if value is None:
            self.close()
        return value

    def fetchmany(self, *args, **kwargs):
        value = self._check().fetchmany(*args, **kwargs)
        if not value:
            self.close()
        return value

    def fetchall(self):
        try:
            return self._check().fetchall()
        finally:
            self.close()

    def close(self):
        lease = self._lease()
        if lease is not None and lease._owner != get_ident():
            raise sqlite_api.ProgrammingError('question_connection_thread_changed')
        cursor, self._cursor = self._cursor, None
        if cursor is not None:
            cursor.close()
            if lease is not None:
                lease._native_cursors.pop(id(self), None)

    def __del__(self):
        try:
            lease = self._lease()
            if lease is None or lease._owner == get_ident():
                self.close()
            else:
                # GC can run on another thread. The owner retains the native
                # cursor and closes it before the pair is returned.
                self._cursor = None
        except BaseException:
            pass


class QuestionReadLease:
    """A thread-bound facade; neither cursors nor authority survive its request."""

    def __init__(self, pair, name, budget):
        self._pair, self._name, self._budget = pair, name, budget
        self._owner = get_ident()
        self._active = True
        self._reading = False
        self._cursors = WeakSet()
        self._native_cursors = {}
        self._observation = None
        self._changes = None

    @property
    def database(self):
        return getattr(self._pair, self._name + '_database')

    @property
    def _native(self):
        return getattr(self._pair, self._name)

    def _check(self, *, budget=True, poison=True, reading=True):
        if not self._active or self._owner != get_ident() or reading and not self._reading:
            raise sqlite_api.ProgrammingError('question_connection_lease_revoked')
        if budget:
            self._budget.check()
        if poison and self._pair.poisoned:
            raise ProjectionUnavailable()

    @property
    def _secured_file_identity(self):
        self._check()
        return self._observation

    def __getattr__(self, name):
        if name not in ('_tracked_path', 'in_transaction', 'isolation_level',
                        'row_factory', 'total_changes'):
            raise AttributeError(name)
        self._check()
        return getattr(self._native, name)

    def execute(self, *args, **kwargs):
        self._check()
        return _Cursor(self._native.execute(*args, **kwargs), self)

    def executemany(self, *args, **kwargs):
        self._check()
        return _Cursor(self._native.executemany(*args, **kwargs), self)

    def executescript(self, *args, **kwargs):
        raise sqlite_api.ProgrammingError('question_scripts_forbidden')

    def cursor(self, *args, **kwargs):
        self._check()
        return _Cursor(self._native.cursor(*args, **kwargs), self)

    def set_progress_handler(self, callback, steps):
        self._check(budget=callback is not None, poison=callback is not None)
        return self._native.set_progress_handler(callback, steps)

    def set_trace_callback(self, callback):
        self._check(budget=callback is not None, poison=callback is not None)
        return self._native.set_trace_callback(callback)

    def close_cursors(self):
        if self._owner != get_ident():
            raise sqlite_api.ProgrammingError('question_connection_thread_changed')
        error = None
        for cursor in list(self._cursors):
            try:
                cursor.close()
            except BaseException as failure:
                error = error or failure
        self._cursors.clear()
        for key, native in list(self._native_cursors.items()):
            try:
                native.close()
            except BaseException as failure:
                error = error or failure
            else:
                self._native_cursors.pop(key, None)
        if error is not None:
            self._pair.poisoned = True
            raise error

    def observe(self, *, initial=False):
        """Perform a fresh security/identity observation, including all sidecars."""
        self._check()
        files = self.database._observe_private_files()
        self._budget.check()
        expected = self._native._question_file_identities
        if (files[0] != self._native._secured_file_identity
                or any(actual != original and not (initial and original is None)
                       for actual, original in zip(files, expected))):
            self._pair.poisoned = True
            raise SQLiteConfigurationError('question_connection_file_replaced')
        if initial:
            self._native._question_file_identities = files
        self._observation = files[0]
        return files[0]

    @contextmanager
    def read(self):
        self._check(reading=False)
        if self._reading:
            raise sqlite_api.ProgrammingError('nested_question_connection_lease')
        self._reading = True
        failed = False
        try:
            if self._native is None:
                # The caller's store preflight rejects missing files first. The
                # generic connect path retains its configuration and tracking.
                connection = self.database.connect()
                setattr(self._pair, self._name, connection)
                cursor = connection.execute('PRAGMA query_only=ON')
                cursor.close()
                self._observation = connection._secured_file_identity
                files = connection._secured_file_identities
                if files is None or files[0] != self._observation:
                    raise SQLiteConfigurationError('question_connection_opening_binding_missing')
                connection._question_file_identities = files
                if any(identity is None for identity in files[1:]):
                    # The first actual database read initializes WAL handles.
                    # Only this first initialization can bind absent sidecars;
                    # every later observation compares the immutable lineage.
                    cursor = connection.execute('SELECT name FROM sqlite_schema LIMIT 1')
                    try:
                        cursor.fetchall()
                    finally:
                        cursor.close()
                    self.observe(initial=True)
                self._budget.check()
            else:
                self.observe()
            connection = self._native
            if connection.in_transaction or connection.isolation_level is not None:
                raise SQLiteConfigurationError('question_connection_requires_autocommit')
            self._changes = connection.total_changes
            self._validate_configuration()
            connection.set_authorizer(_read_authorizer)
            yield self
        except BaseException:
            failed = True
            self._pair.poisoned = True
            raise
        finally:
            try:
                self.close_cursors()
            except BaseException:
                self._pair.poisoned = True
                if not failed:
                    raise
            finally:
                self._reading = False

    def _validate_configuration(self):
        connection = self._native
        configuration = []
        for name in ('query_only', 'foreign_keys', 'synchronous'):
            cursor = connection.execute('PRAGMA ' + name)
            try:
                configuration.append(cursor.fetchone()[0])
            finally:
                cursor.close()
        if (connection.in_transaction or connection.isolation_level is not None
                or connection.row_factory is not sqlite_api.Row
                or configuration != [1, 1, 2]
                or self._changes is not None and connection.total_changes != self._changes):
            self._pair.poisoned = True
            raise SQLiteConfigurationError('question_connection_configuration_changed')

    def _finish(self):
        error = None
        try:
            try:
                self.close_cursors()
            except BaseException as failure:
                error = failure
            if self._native is not None:
                # Remove per-request callbacks before another lease can begin.
                for clear in (lambda: self._native.set_progress_handler(None, 0),
                              lambda: self._native.set_trace_callback(None),
                              lambda: self._native.set_authorizer(None)):
                    try:
                        clear()
                    except BaseException as failure:
                        error = error or failure
                try:
                    self._validate_configuration()
                    cursor = self._native.execute('PRAGMA busy_timeout=' + str(self.database.busy_timeout_ms))
                    cursor.close()
                except BaseException as failure:
                    error = error or failure
        finally:
            self._active = False
        if error is not None:
            raise error


class _Pair:
    def __init__(self, source, store, canonical, analytics):
        self.source, self.store = source, store
        self.canonical_database, self.analytics_database = canonical, analytics
        self.canonical = self.analytics = None
        self.busy = True
        self.poisoned = False

    def matches(self, source, store, canonical, analytics):
        return (self.source is source and self.store is store
                and self.canonical_database is canonical and self.analytics_database is analytics)

    def close(self):
        error = None
        for name in ('analytics', 'canonical'):
            connection = getattr(self, name)
            if connection is not None:
                try:
                    connection.close()
                except BaseException as failure:
                    error = error or failure
                else:
                    setattr(self, name, None)
        if error is not None:
            raise error


class QuestionConnectionPool:
    """Two exclusively borrowed pairs. Generic repository reads remain fresh."""

    def __init__(self):
        self._condition = Condition()
        self._pairs = []
        self._closed = False

    @contextmanager
    def borrow(self, source, store, account, budget):
        budget.check()
        canonical = source.history.database
        analytics = store.question_connection_database(account, budget)
        if canonical.path == analytics.path:
            raise SQLiteConfigurationError('question_database_scopes_must_be_separate')
        store.register_question_connections(self)
        retired = None
        with self._condition:
            if self._closed:
                raise ProjectionUnavailable()
            pair = next((p for p in self._pairs if not p.busy and not p.poisoned
                         and p.matches(source, store, canonical, analytics)), None)
            if pair is None:
                retired = next((p for p in self._pairs if not p.busy), None)
                if retired is not None:
                    retired.busy = retired.poisoned = True
                elif len(self._pairs) >= 2:
                    raise ProjectionBackpressure()
                pair = _Pair(source, store, canonical, analytics)
                if retired is None:
                    self._pairs.append(pair)
            else:
                pair.busy = True
        if retired is not None:
            try:
                retired.close()
            except BaseException:
                with self._condition:
                    if retired.canonical is None and retired.analytics is None:
                        self._pairs.remove(retired)
                    else:
                        retired.busy = False
                    self._condition.notify_all()
                raise
            with self._condition:
                self._pairs.remove(retired)
                if self._closed:
                    self._condition.notify_all()
                    raise ProjectionUnavailable()
                self._pairs.append(pair)
                self._condition.notify_all()
        canonical_lease = QuestionReadLease(pair, 'canonical', budget)
        analytics_lease = QuestionReadLease(pair, 'analytics', budget)
        failed = False
        try:
            budget.check()
            yield canonical_lease, analytics_lease
        except BaseException:
            failed = pair.poisoned = True
            raise
        finally:
            error = None
            for lease in (analytics_lease, canonical_lease):
                try:
                    lease._finish()
                except BaseException as failure:
                    pair.poisoned = True
                    error = error or failure
            if not failed:
                try:
                    budget.check()
                except BaseException as failure:
                    pair.poisoned = True
                    error = error or failure
            with self._condition:
                discard = pair.poisoned or self._closed
            if discard:
                try:
                    pair.close()
                except BaseException as failure:
                    error = error or failure
            with self._condition:
                if discard and pair.canonical is None and pair.analytics is None:
                    if pair in self._pairs:
                        self._pairs.remove(pair)
                else:
                    pair.busy = False
                self._condition.notify_all()
            if error is not None and not failed:
                raise error

    def invalidate_database(self, database):
        self._discard(lambda pair: pair.canonical_database is database
                      or pair.analytics_database is database)

    def invalidate(self):
        """Discard native handles when runtime authorization/navigation expires."""
        self._discard(lambda pair: True)

    def _discard(self, selected):
        with self._condition:
            idle = []
            for pair in self._pairs:
                if selected(pair):
                    pair.poisoned = True
                    if not pair.busy:
                        pair.busy = True
                        idle.append(pair)
        error = None
        for pair in idle:
            try:
                pair.close()
            except BaseException as failure:
                error = error or failure
            with self._condition:
                if pair.canonical is None and pair.analytics is None:
                    self._pairs.remove(pair)
                else:
                    pair.busy = False
                self._condition.notify_all()
        if error is not None:
            raise error

    def close(self):
        with self._condition:
            self._closed = True
        self._discard(lambda pair: True)

    def wait_closed(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._pairs:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass
