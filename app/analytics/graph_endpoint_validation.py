"""Operation-local endpoint closure while all selected graph rows are verified."""

from sys import exc_info, getsizeof

from app.analytics.graph_identity import require_graph_id
from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.validation_receipt import content_stamp

# Bound the actual retained Python collection, not a process-memory promise.
MAX_COLD_ENDPOINT_ROWS = 524_288
MAX_COLD_ENDPOINT_BYTES = 64 * 1024 * 1024


def _pragma_version(connection, name):
    cursor = connection.execute('PRAGMA ' + name)
    try:
        value = int(cursor.fetchone()[0])
    except BaseException:
        try:
            cursor.close()
        except BaseException:
            pass
        raise
    cursor.close()
    return value


def _snapshot(connection):
    stamp = content_stamp(connection)
    if stamp is None:
        return None
    return (stamp, _pragma_version(connection, 'data_version'),
            _pragma_version(connection, 'user_version'), connection.total_changes)


def verify_cold_graph_rows(connection, generation_id, account_id, check):
    """Return fully verified rows, or refuse and leave the original SQL path.

    This owns the collection, its admission and its edge check. No caller-supplied
    set, completed flag or endpoint proof can authorize deferral. Nothing about
    the collection enters the returned graph or any retained startup receipt.
    """
    if (getattr(connection, 'in_transaction', False) is not True
            or type(getattr(connection, 'total_changes', None)) is not int):
        return None
    from app.analytics.shared_graph import _verify_segment_layout
    from app.analytics.graph_verification import verify_graph_rows

    check()
    if not _verify_segment_layout(connection, generation_id, account_id):
        return None
    changes = connection.total_changes
    snapshot = _snapshot(connection)
    if snapshot is None:
        return None
    selected = set()
    string_bytes = 0
    examined = 0

    def same_transaction():
        if (connection.in_transaction is not True
                or connection.total_changes != changes):
            raise GraphReferentialIntegrityError('graph_endpoint_snapshot_changed')

    try:
        cursor = connection.execute("""WITH selected_nodes AS (
            SELECT c.node_id FROM generation_graph_segments m
            CROSS JOIN graph_segment_nodes r USING(creator_account_id,segment_id)
            CROSS JOIN graph_node_content c USING(creator_account_id,content_id,node_id)
            WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind='node')
            SELECT node_id FROM selected_nodes""", (generation_id, account_id))
        try:
            for row in cursor:
                check()
                same_transaction()
                examined += 1
                if examined > MAX_COLD_ENDPOINT_ROWS:
                    return None
                key = require_graph_id(row[0])
                if key not in selected:
                    size = getsizeof(key)
                    if string_bytes + size + getsizeof(selected) > MAX_COLD_ENDPOINT_BYTES:
                        return None
                    selected.add(key)
                    string_bytes += size
                    # A set-table resize is charged before the collection is used.
                    # Refusal releases it before invoking the original SQL verifier.
                    if string_bytes + getsizeof(selected) > MAX_COLD_ENDPOINT_BYTES:
                        return None
        except BaseException:
            try:
                cursor.close()
            except BaseException:
                pass
            raise
        finally:
            # On a normal return (including capacity refusal) close errors fail
            # the operation; an active primary BaseException is preserved above.
            if exc_info()[0] is None:
                cursor.close()
        check()
        same_transaction()

        def edge_check(row):
            same_transaction()
            # The LEFT content join must retain unresolved selected membership;
            # an INNER join would hide this case before endpoint validation.
            if (row['edge_id'] is None or row['content_id'] is None
                    or row['source_id'] not in selected
                    or row['target_id'] not in selected):
                raise GraphReferentialIntegrityError('graph_endpoint_absent')

        verified = verify_graph_rows(
            connection, generation_id, account_id, check=check,
            _endpoint_check=edge_check,
        )
        check()
        same_transaction()
        if _snapshot(connection) != snapshot:
            raise GraphReferentialIntegrityError('graph_endpoint_snapshot_changed')
        return verified
    finally:
        selected.clear()
