"""Bounded selected-content reuse inside one complete cold verification.

Only the recomputation owner creates and seals this collector. It receives
already checked graph rows, never a caller-provided mapping or stored proof.
The private savepoint must survive until the last consumer has finished.
"""

import re
from secrets import token_hex
from sys import exc_info, getsizeof

from app.analytics.graph_endpoint_validation import _snapshot
from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.validation_receipt import generation_binding
from app.persistence import sqlite_api as sqlite3


MAX_COLD_SELECTION_ROWS = 2 * 1024 * 1024
MAX_COLD_SELECTION_BYTES = 256 * 1024 * 1024
_HEX64 = re.compile(r'[0-9a-f]{64}')


def _close_cursor(cursor):
    if exc_info()[0] is None:
        cursor.close()
    else:
        try:
            cursor.close()
        except BaseException:
            pass


def _tuple_bytes(value):
    """Charge the small owned snapshot, including its nested stamp tuple."""
    return getsizeof(value) + (sum(_tuple_bytes(item) for item in value)
                              if isinstance(value, tuple) else 0)


class ColdGraphSelection:
    """Private operation-owned collector; refusal preserves the original SQL.

    ``discard`` releases data but keeps the transaction sentinel. ``finish``
    is the successful-return gate; ``close`` preserves a primary failure.
    No data or capability from this object enters a verification receipt.
    """

    __slots__ = ('_connection', '_generation', '_binding', '_account', '_check',
                 '_changes', '_snapshot', '_savepoint', '_maps', '_pair_bytes',
                 '_base_bytes', '_walk_started', '_walk_complete', '_admitted', '_finished',
                 '_disabled', '_max_rows', '_max_bytes', 'rows',
                 'retained_bytes', 'peak_rows', 'peak_bytes', 'discard_reason', '_ready')

    def __init__(self, connection, generation, account, check):
        self._connection, self._check = connection, check
        self._generation = generation['generation_id']
        self._binding = generation_binding(generation)
        self._account = account
        self._changes = None
        self._snapshot = self._savepoint = None
        self._maps = ({}, {})
        self._pair_bytes = self._base_bytes = 0
        self._walk_started = self._walk_complete = self._admitted = self._finished = False
        self._disabled = True
        self._max_rows, self._max_bytes = MAX_COLD_SELECTION_ROWS, MAX_COLD_SELECTION_BYTES
        self.rows = self.retained_bytes = self.peak_rows = self.peak_bytes = 0
        self.discard_reason = None
        self._ready = False
        try:
            check()
            if (getattr(connection, 'in_transaction', False) is not True
                    or type(getattr(connection, 'total_changes', None)) is not int
                    or generation['creator_account_id'] != account):
                self.discard('scope_unavailable')
                return
            from app.analytics.graph_membership_pages import supported
            from app.analytics.shared_graph import uses_segments
            if (not supported(connection)
                    or not uses_segments(connection, self._generation, account)):
                self.discard('layout_unavailable')
                return
            self._changes = connection.total_changes
            self._snapshot = _snapshot(connection)
            if self._snapshot is None:
                self.discard('snapshot_unavailable')
                return
            name = 'cold_selection_' + token_hex(16)
            self._savepoint = name
            try:
                cursor = connection.execute('SAVEPOINT ' + name)
            except sqlite3.DatabaseError as error:
                self._savepoint = None
                if str(error).lower() == 'not authorized':
                    self.discard('savepoint_unavailable')
                    return
                raise
            cursor.close()
            self._admitted = True
            self._assert_snapshot()
            self._base_bytes = (getsizeof(self) + getsizeof(self._maps)
                + _tuple_bytes(self._snapshot) + getsizeof(self._generation)
                + getsizeof(self._binding) + getsizeof(account) + getsizeof(name))
            self._disabled = False
            self._measure()
            if self.retained_bytes > self._max_bytes or self._max_rows < 1:
                self.discard('capacity')
        except BaseException:
            self.close()
            raise

    def _control(self, sql):
        cursor = self._connection.execute(sql)
        cursor.close()

    def _assert_transaction(self):
        if (self._connection.in_transaction is not True
                or self._connection.total_changes != self._changes):
            raise GraphReferentialIntegrityError('cold_selection_snapshot_changed')

    def _current_binding(self):
        cursor = self._connection.execute(
            'SELECT * FROM projection_generations WHERE generation_id=? '
            'AND creator_account_id=?', (self._generation, self._account))
        try:
            row = cursor.fetchone()
            return None if row is None else generation_binding(row)
        finally:
            _close_cursor(cursor)

    def _assert_snapshot(self):
        self._assert_transaction()
        if (_snapshot(self._connection) != self._snapshot
                or self._current_binding() != self._binding):
            raise GraphReferentialIntegrityError('cold_selection_snapshot_changed')
        self._assert_transaction()

    def _measure(self):
        self.retained_bytes = (self._base_bytes + self._pair_bytes
                              + sum(getsizeof(mapping) for mapping in self._maps))
        self.peak_bytes = max(self.peak_bytes, self.retained_bytes)
        self.peak_rows = max(self.peak_rows, self.rows)

    @property
    def ready(self):
        return (self._ready and self._walk_complete and not self._disabled
                and not self._finished)

    def discard(self, reason='unavailable'):
        for mapping in self._maps:
            mapping.clear()
        self._pair_bytes = self.rows = self.retained_bytes = 0
        self._ready = False
        self._disabled = True
        if self.discard_reason is None:
            self.discard_reason = reason

    def collect(self, kind, row, identity, content_id):
        """Observe only after the ordinary encoder and canonical hash checks."""
        if self._disabled or self._finished:
            return
        if not self._walk_started or self._walk_complete:
            self.discard('graph_scope_unavailable')
            return
        self._assert_transaction()
        prefix = 'g1:' if kind == 'node' else 'e1:' if kind == 'edge' else None
        try:
            eligible = (prefix is not None and type(identity) is str
                and len(identity) == 67 and identity.startswith(prefix)
                and _HEX64.fullmatch(identity[3:]) is not None
                and type(content_id) is str and _HEX64.fullmatch(content_id) is not None
                and row[kind + '_id'] == identity and row['content_id'] == content_id
                and row['generation_id'] == self._generation
                and row['creator_account_id'] == self._account
                and row['segment_bucket'] == identity[3:5]
                and row['page_bucket'] == identity[3:6])
        except (KeyError, IndexError, TypeError):
            eligible = False
        if not eligible:
            self.discard('row_ineligible')
            return
        mapping = self._maps[kind == 'edge']
        key = bytes.fromhex(identity[3:])
        if key in mapping:
            self.discard('duplicate_identity')
            return
        value = bytes.fromhex(content_id)
        cost = getsizeof(key) + getsizeof(value)
        if self.rows + 1 > self._max_rows or self.retained_bytes + cost > self._max_bytes:
            self.discard('capacity')
            return
        mapping[key] = value
        self.rows += 1
        self._pair_bytes += cost
        # Charge each actual dictionary resize before permitting any lookup.
        self._measure()
        if self.retained_bytes > self._max_bytes:
            self.discard('capacity')

    def _begin_graph(self, connection, generation_id, account_id):
        """The actual row verifier binds its connection and one complete walk."""
        if self._disabled or self._finished:
            return
        if (self._walk_started or connection is not self._connection
                or generation_id != self._generation or account_id != self._account):
            self.discard('graph_scope_unavailable')
            return
        self._assert_transaction()
        self._walk_started = True

    def _finish_graph(self):
        """Called only by the complete row verifier, never by its consumer."""
        if not self._disabled and self._walk_started:
            self._assert_transaction()
            self._walk_complete = True

    def seal(self):
        """The owner calls this after all link, digest and coverage checks."""
        if not self._admitted or self._finished:
            return False
        self._check()
        self._assert_snapshot()
        if self._disabled or not self._walk_complete:
            self.discard('graph_incomplete')
            return False
        self._ready = True
        return True

    def matches(self, connection, generation, account):
        if (not self.ready or self._finished or connection is not self._connection
                or account != self._account or generation['generation_id'] != self._generation
                or generation_binding(generation) != self._binding):
            return False
        self._assert_snapshot()
        return True

    def selected(self, kind, bucket, identities):
        """Resolve a complete replayable request or refuse it without partial data."""
        if not self.ready or self._finished:
            return None
        # A refused request must remain replayable by the original SQL path.
        from app.analytics.conversation_id_frames import EncodedIds
        from app.analytics.conversation_integrity import MAX_GROUP_RECORDS
        if (kind not in ('node', 'edge') or type(identities) not in (list, tuple, EncodedIds)
                or len(identities) > MAX_GROUP_RECORDS):
            self.discard('request_ineligible')
            return None
        self._check()
        self._assert_transaction()
        mapping = self._maps[kind == 'edge']
        prefix = 'g1:' if kind == 'node' else 'e1:'
        result, result_bytes = {}, 0
        for identity in identities:
            self._check()
            self._assert_transaction()
            if (type(identity) is not str or len(identity) != 67
                    or not identity.startswith(prefix) or _HEX64.fullmatch(identity[3:]) is None
                    or (bucket is not None and identity[3:5] != bucket)):
                self.discard('request_ineligible')
                return None
            value = mapping.get(bytes.fromhex(identity[3:]))
            if value is None:
                self.discard('coverage_missing')
                return None
            if identity in result:
                continue
            content = value.hex()
            cost = getsizeof(identity) + getsizeof(content)
            if self.retained_bytes + result_bytes + cost + getsizeof(result) > self._max_bytes:
                self.discard('lookup_capacity')
                return None
            result[identity] = content
            result_bytes += cost
            used = self.retained_bytes + result_bytes + getsizeof(result)
            self.peak_bytes = max(self.peak_bytes, used)
            if used > self._max_bytes:
                self.discard('lookup_capacity')
                return None
        self._assert_transaction()
        return result

    def finish(self):
        """Gate successful return on the original snapshot and surviving sentinel."""
        if self._finished:
            return
        try:
            if self._admitted:
                self._check()
                self._assert_snapshot()
                # A rollback/commit followed by BEGIN can preserve all counters.
                # It cannot preserve this private savepoint; RELEASE must succeed.
                try:
                    self._control('RELEASE SAVEPOINT ' + self._savepoint)
                except sqlite3.OperationalError as error:
                    if str(error).lower().startswith('no such savepoint'):
                        raise GraphReferentialIntegrityError(
                            'cold_selection_snapshot_changed') from None
                    raise
                self._savepoint = None
                self._assert_transaction()
            self._finished = True
        finally:
            self.discard('finished')

    def close(self):
        if exc_info()[0] is None:
            self.finish()
            return
        self.discard('failed')
        if self._savepoint is not None:
            try:
                self._control('RELEASE SAVEPOINT ' + self._savepoint)
            except BaseException:
                pass
            self._savepoint = None
        self._finished = True
