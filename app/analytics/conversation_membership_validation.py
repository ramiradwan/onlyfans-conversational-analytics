"""Reuse proven unit membership only after checking changed persisted selections."""
from hashlib import sha256
import re
from sys import getsizeof
import zlib

from app.analytics.conversation_graph_units import MAX_GRAPH_UNIT_BYTES, MAX_GRAPH_UNIT_RECORDS
from app.analytics.conversation_pages import PAGE_BYTES, PAGE_RECORDS
from app.analytics.shared_graph import MAX_CHANGED_VERIFICATION_BYTES, MAX_CHANGED_VERIFICATION_ROWS


def changed_predecessor_members(connection, account, before, current, prepared, check):
    """Return exact replaced/removed identities; None requires ordinary validation.

    Both manifests must already have passed persisted graph-byte verification.
    New identities cannot invalidate a previously verified immutable unit.
    """
    if not connection.in_transaction:
        return None
    changed = []
    for key, previous in before.items():
        now = current.get(key)
        if now is not None and (previous.kind, previous.bucket, previous.digest, previous.count) == (
                getattr(now, 'kind', None), getattr(now, 'bucket', None),
                getattr(now, 'digest', None), getattr(now, 'count', None)):
            continue
        if now is not None and (prepared is None or (previous.kind, getattr(now, 'segment_id', None)) not in prepared):
            return None
        changed.append((key, previous, now))
    if (sum(old.count for _, old, _ in changed) > MAX_CHANGED_VERIFICATION_ROWS
            or sum(getattr(now, 'count', 0) for _, _, now in changed) > MAX_CHANGED_VERIFICATION_ROWS):
        return None
    removed = {'node': set(), 'edge': set()}
    retained_bytes = 0
    for (kind, bucket), previous, now in changed:
        check()
        if kind not in removed or previous.kind != kind or previous.bucket != bucket:
            raise ValueError('conversation_integrity_segment_invalid')
        selected = {}
        selected_bytes = 0
        actual = sha256(('graph-segment.v1:' + kind + ':' + bucket).encode())
        last = None
        rows = () if now is None else prepared[(kind, now.segment_id)]
        for row in rows:
            check()
            key, content = row[kind + '_id'], row['content_id']
            if key[3:5] != bucket or (last is not None and key <= last):
                raise ValueError('conversation_integrity_selected_order_invalid')
            selected[key] = content
            selected_bytes += getsizeof(key) + getsizeof(content)
            if (len(selected) > MAX_CHANGED_VERIFICATION_ROWS
                    or retained_bytes + selected_bytes + getsizeof(selected) > MAX_CHANGED_VERIFICATION_BYTES):
                return None
            actual.update(key.encode('ascii') + b':' + content.encode('ascii') + b'\n')
            last = key
        if now is not None and (len(selected) != now.count or actual.hexdigest() != now.digest):
            raise ValueError('conversation_integrity_selected_digest_invalid')
        cursor = connection.execute(
            f'SELECT {kind}_id,content_id FROM graph_segment_{kind}s '
            f'WHERE creator_account_id=? AND segment_id=? ORDER BY {kind}_id LIMIT ?',
            (account, previous.segment_id, previous.count + 1))
        actual = sha256(('graph-segment.v1:' + kind + ':' + bucket).encode())
        count, last = 0, None
        try:
            for row in cursor:
                check()
                key, content = row[0], row[1]
                if key[3:5] != bucket or (last is not None and key <= last):
                    raise ValueError('conversation_integrity_predecessor_order_invalid')
                actual.update(key.encode('ascii') + b':' + content.encode('ascii') + b'\n')
                count += 1
                last = key
                if selected.get(key) != content:
                    removed[kind].add(key)
                    retained_bytes += getsizeof(key) + 64
                    if (sum(map(len, removed.values())) > PAGE_RECORDS
                            or retained_bytes + selected_bytes + getsizeof(selected) > MAX_CHANGED_VERIFICATION_BYTES):
                        return None
        finally:
            cursor.close()
        if count != previous.count or actual.hexdigest() != previous.digest:
            raise ValueError('conversation_integrity_predecessor_digest_invalid')
    check()
    return removed


def contains_changed_member(data, expected_count, kind, identities, check):
    """Inspect canonical fixed-width ID frames without constructing their models.

    Called only for the exact immutable unit named by an existing content proof.
    Noncanonical but legal legacy JSON requires the ordinary complete reader.
    """
    check()
    if (kind not in ('node', 'edge') or not isinstance(data, bytes) or not data
            or len(data) > MAX_GRAPH_UNIT_BYTES or not 0 <= expected_count <= MAX_GRAPH_UNIT_RECORDS
            or len(identities) > PAGE_RECORDS):
        return None
    if not identities:
        return False
    if expected_count == 0:
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(data, 3)
        except zlib.error:
            return None
        return False if raw == b'[]' and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail else None
    prefix = b'g1:' if kind == 'node' else b'e1:'
    frame = re.compile(rb'(?:"' + prefix + rb'[0-9a-f]{64}",)+')
    needles = re.compile(b'|'.join(re.escape(b'"' + key.encode('ascii') + b'"') for key in sorted(identities)))
    decoder = zlib.decompressobj()
    buffer, count, total = b'', 0, 0
    limit = min(MAX_GRAPH_UNIT_BYTES * 4, expected_count * 70 + 1)
    try:
        for offset in range(0, len(data), PAGE_BYTES):
            pending = memoryview(data)[offset:offset + PAGE_BYTES]
            while pending:
                check()
                raw = decoder.decompress(pending, PAGE_BYTES)
                pending = decoder.unconsumed_tail
                if decoder.unused_data or total + len(raw) > limit:
                    return None
                if not raw:
                    continue
                if total == 0:
                    if not raw.startswith(b'['):
                        return None
                    buffer = raw[1:]
                else:
                    buffer += raw
                total += len(raw)
                available = len(buffer) // 70
                if available:
                    block, buffer = buffer[:available * 70], buffer[available * 70:]
                    count += available
                    if count > expected_count:
                        return None
                    if count == expected_count:
                        if not block.endswith(b']'):
                            return None
                        block = block[:-1] + b','
                    if frame.fullmatch(block) is None:
                        return None
                    if needles.search(block):
                        return True
    except zlib.error:
        return None
    check()
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or buffer
            or count != expected_count or total != expected_count * 70 + 1):
        return None
    return False


def proven_unit_is_unchanged(connection, generation_id, account, header, changed, check, *,
                             integrity_groups=None, membership_prefixes=None):
    """Keep prior verification only when no replaced/removed record is a member.

    A verified v2 integrity manifest can first prove that whole hash buckets are
    absent from this immutable unit. Exact membership bytes are opened only for
    changed identities whose buckets are actually represented by the unit.
    """
    group_keys = (None if integrity_groups is None else
                  {(group[0], group[1]) for group in integrity_groups})
    for kind, identities in changed.items():
        check()
        if not identities:
            continue
        if group_keys is not None:
            identities = {identity for identity in identities
                          if (kind, identity[3:5]) in group_keys}
            if not identities:
                continue
        if membership_prefixes is not None:
            from app.analytics.membership_prefixes import possibly_contains
            summary = membership_prefixes[0 if kind == 'node' else 1]
            identities = {identity for identity in identities
                          if possibly_contains(summary, identity)}
            if not identities:
                continue
        row = connection.execute(
            f'SELECT u.{kind}_ids FROM conversation_graph_refs r '
            'JOIN conversation_graph_units u USING(creator_account_id,unit_id) '
            'WHERE r.generation_id=? AND r.creator_account_id=? AND r.conversation_ref=? AND r.unit_id=?',
            (generation_id, account, header.conversation_ref, header.unit_id)).fetchone()
        if row is None:
            raise ValueError('conversation_integrity_unit_missing')
        present = contains_changed_member(row[0], getattr(header, kind + '_count'), kind, identities, check)
        if present is not False:
            return False
    check()
    return True


def predecessor_manifest_matches(connection, generation_id, account, segments, check):
    """Reject partial or inconsistent segment proofs before membership reuse."""
    check()
    if not connection.in_transaction or not segments or len(segments) > 512:
        return False
    expected = sorted((s.kind, s.bucket, s.segment_id, s.digest, 1) for s in segments)
    rows = connection.execute(
        "SELECT m.kind,m.bucket,m.segment_id,s.content_digest,s.sealed "
        "FROM generation_graph_segments m LEFT JOIN graph_segments s "
        "USING(creator_account_id,segment_id) WHERE m.generation_id=? "
        "AND m.creator_account_id=? ORDER BY m.kind,m.bucket LIMIT ?",
        (generation_id, account, len(expected) + 1))
    try:
        actual = []
        for row in rows:
            check()
            actual.append(tuple(row))
    finally:
        rows.close()
    check()
    return actual == expected
