"""Verify conversation summaries against independently checked generation content."""
from collections import OrderedDict
from sys import getsizeof

from app.analytics import conversation_graph_unit_sql as units
from app.analytics.conversation_integrity import (
    decode_manifest, groups_for_members, summarize_group,
)
from app.analytics.validation_receipt import content_stamp, generation_binding
from app.analytics.shared_graph import MAX_CHANGED_VERIFICATION_ROWS, MAX_CHANGED_VERIFICATION_BYTES


def verify_generation_integrity(connection, generation, account, *, proof=None,
                                graph_validation=None, segments=(), prepared=None,
                                check=lambda: None):
    """A process-local proof can skip only groups in unchanged verified segments."""
    if not units.integrity_supported(connection):
        return ()
    references = units.list_references(connection, generation['generation_id'], account)
    if not any(ref.header.checksum_version == 2 for ref in references):
        return ()
    stamp = content_stamp(connection)
    trusted, old_segments, old_generation = {}, {}, None
    prior_graph = getattr(graph_validation, 'proof', None)
    if proof is not None and prior_graph is not None and stamp is not None:
        prior = connection.execute('SELECT * FROM projection_generations WHERE generation_id=? '
            'AND creator_account_id=?', (proof.generation_id, account)).fetchone()
        if (prior is not None and prior['status'] == 'active'
                and generation['expected_active_generation_id'] == proof.generation_id
                and prior_graph.generation_id == proof.generation_id
                and prior_graph.binding == proof.binding == generation_binding(prior)
                and tuple(stamp[:3]) == proof.stamp_prefix == prior_graph.stamp_prefix):
            trusted = {h.conversation_ref: h for h in proof.headers}
            old_segments = {(s.kind, s.bucket): s for s in prior_graph.segments}
            old_generation = proof.generation_id
    current = {(s.kind, s.bucket): s for s in segments}
    if not current:
        rows = connection.execute('SELECT m.kind,m.bucket,m.segment_id FROM generation_graph_segments m '
            'WHERE m.generation_id=? AND m.creator_account_id=?', (generation['generation_id'], account))
        current = {(r['kind'], r['bucket']): r['segment_id'] for r in rows}
    cache, cache_rows, cache_bytes = OrderedDict(), 0, 0

    def versions(kind, bucket, selected):
        nonlocal cache_rows, cache_bytes
        segment = current.get((kind, bucket))
        segment_id = segment if isinstance(segment, str) else getattr(segment, 'segment_id', None)
        if segment_id is None:
            raise ValueError('conversation_integrity_segment_missing')
        key = kind, segment_id
        if key in cache:
            cache.move_to_end(key)
            mapping, used = cache[key]
        else:
            checked = None if prepared is None else prepared.get(key)
            if checked is None:
                # Full validation reads only this unit's requested membership.
                # Scanning a whole account bucket for every small unit amplifies reads.
                from app.analytics.shared_graph import selected_content_ids
                return selected_content_ids(connection, generation['generation_id'], account,
                    kind, list(selected), check, page_layout=True)
            mapping, used = {}, 0
            for row in checked:
                check()
                identity, content = row[kind + '_id'], row['content_id']
                if identity in mapping:
                    raise ValueError('conversation_integrity_duplicate_member')
                cost = getsizeof(identity) + getsizeof(content) + 64
                while cache and (cache_rows + len(mapping) + 1 > MAX_CHANGED_VERIFICATION_ROWS
                                 or cache_bytes + used + cost > MAX_CHANGED_VERIFICATION_BYTES):
                    _, (old, old_cost) = cache.popitem(last=False)
                    cache_rows -= len(old)
                    cache_bytes -= old_cost
                if len(mapping) + 1 > MAX_CHANGED_VERIFICATION_ROWS or used + cost > MAX_CHANGED_VERIFICATION_BYTES:
                    from app.analytics.shared_graph import selected_content_ids
                    return selected_content_ids(connection, generation['generation_id'], account,
                        kind, list(selected), check, page_layout=True)
                mapping[identity] = content
                used += cost
            cache[key] = mapping, used
            cache_rows += len(mapping)
            cache_bytes += used
        return {key: mapping[key] for key in selected if key in mapping}

    headers = []
    for reference in references:
        check()
        h = reference.header
        if h.checksum_version == 1:
            continue
        unit = units.load_unit(connection, generation['generation_id'], account, h.conversation_ref)
        if unit is None or unit.header != h:
            raise ValueError('conversation_integrity_unit_missing')
        summaries, members = groups_for_members(unit, check)
        predecessor = trusted.get(h.conversation_ref)
        if predecessor == h:
            old_summaries = summaries
        else:
            previous = None if predecessor is None else units.load_unit(
                connection, old_generation, account, h.conversation_ref)
            old_summaries = (decode_manifest(previous) if previous is not None
                and previous.header == predecessor and predecessor.checksum_version == 2 else {})
        for key, summary in summaries.items():
            check()
            prior = old_segments.get(key)
            now = current.get(key)
            unchanged = (prior is not None and now is not None
                         and getattr(now, 'segment_id', None) == prior.segment_id)
            if unchanged and old_summaries.get(key) == summary:
                continue
            selected = members[key]
            actual = versions(*key, selected)
            if len(actual) != len(selected) or summarize_group(account, h.conversation_ref,
                    *key, actual, check) != summary:
                raise ValueError('conversation_integrity_selected_content_changed')
        headers.append(h)
    if content_stamp(connection) != stamp:
        raise ValueError('conversation_integrity_store_changed')
    check()
    return tuple(headers)
