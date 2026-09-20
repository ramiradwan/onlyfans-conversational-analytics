"""Resolve bounded graph references from one witnessed generation."""

from __future__ import annotations

from collections.abc import Callable
import hashlib

from app.analytics.conversation_pages import PAGE_RECORDS
from app.analytics.graph_row_encoding import EncodedGraphRecord, node_record_bytes, edge_record_bytes


def _read_graph_records(connection, generation_id: str, account: str, kind: str,
                        keys: list[str], check: Callable[[], None]) -> list[tuple[dict, bytes]]:
    """Check actual row bytes and selected membership, not stored digests alone."""

    check()
    if kind not in ('node', 'edge') or not 1 <= len(keys) <= PAGE_RECORDS:
        raise ValueError('conversation_page_graph_lookup_invalid')
    from app.analytics.graph_identity import require_graph_id
    for key in keys:
        require_graph_id(key, expected_kind='edge' if kind == 'edge' else None)
    # Select the generation's bucket before the record. This avoids examining
    # other retained versions of an identity or following reverse memberships.
    parameters = [value for key in dict.fromkeys(keys) for value in (key, key[3:5])]
    requested = ','.join('(?,?)' for _ in range(len(parameters) // 2))
    rows = connection.execute(f'''WITH requested(record_id,bucket) AS (VALUES {requested})
        SELECT c.* FROM requested q
        CROSS JOIN generation_graph_segments m
        CROSS JOIN graph_segment_{kind}s r USING(creator_account_id,segment_id)
        CROSS JOIN graph_{kind}_content c USING(creator_account_id,content_id,{kind}_id)
        WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind=?
          AND m.bucket=q.bucket AND r.{kind}_id=q.record_id''',
        (*parameters, generation_id, account, kind))
    result = {}
    encode = node_record_bytes if kind == 'node' else edge_record_bytes
    try:
        for row in rows:
            check()
            record, data = encode(row, account)
            key = row[kind + '_id']
            if key in result or hashlib.sha256(data).hexdigest() != row['content_id']:
                raise ValueError('conversation_page_graph_content_invalid')
            # Reuse the record whose bytes were just checked; do not decode it again.
            result[key] = (record, data)
    finally:
        rows.close()
    check()
    if len(result) != len(set(keys)):
        raise ValueError('conversation_page_graph_reference_absent')
    return [result[key] for key in keys]


def graph_records(connection, generation_id: str, account: str, kind: str,
                  keys: list[str], check: Callable[[], None]) -> list[dict]:
    """Return the checked mappings without decoding their canonical bytes."""

    return [record for record, _ in _read_graph_records(
        connection, generation_id, account, kind, keys, check)]


def encoded_graph_records(connection, generation_id: str, account: str, kind: str,
                          keys: list[str], check: Callable[[], None]) -> list[EncodedGraphRecord]:
    """Retain canonical graph bytes, not the disposable validated mappings."""

    result = []
    for record, data in _read_graph_records(connection, generation_id, account, kind, keys, check):
        check()
        result.append(EncodedGraphRecord(
            account, kind, record[kind + '_id'], record['kind' if kind == 'node' else 'relation'],
            data.decode('utf-8'), record.get('source_id'), record.get('target_id'),
            kind == 'edge' and record['properties'].get('scope') == 'conversation'))
    return result
