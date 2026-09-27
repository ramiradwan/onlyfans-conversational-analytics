"""Stream the existing canonical conversation digest without retaining its graph."""
from itertools import groupby
from heapq import merge
import hashlib

from app.analytics.conversation_graph_units import graph_unit_ids, create_membership_unit


def append_unit(loader, previous, delta, *, conversation_node, input_digest,
                config_digest, cutoff, findings, metrics, check):
    if previous.header.checksum_version == 2:
        from app.analytics.conversation_integrity import append_unit as append_v2, IntegrityCapacity
        try:
            return append_v2(loader, previous, delta, conversation_node=conversation_node,
                input_digest=input_digest, config_digest=config_digest, cutoff=cutoff,
                findings=findings, metrics=metrics, check=check)
        except IntegrityCapacity:
            return None
    from app.analytics.conversation_append import _checked_record_spans
    account = previous.header.account_ref
    if delta.account_ref != account:
        raise ValueError('conversation_append_graph_invalid')
    old_nodes, old_edges = graph_unit_ids(previous)
    before = hashlib.sha256(b'{"edges":[')
    after = hashlib.sha256(b'{"edges":[')
    final_members = {}
    for kind, keys, changed in (('edge', old_edges, delta.edges), ('node', old_nodes, delta.nodes)):
        if kind == 'node':
            before.update(b'],"nodes":[')
            after.update(b'],"nodes":[')
        old_count = new_count = 0
        members = set(keys)
        new_keys = tuple(sorted(set(changed) - members))
        inserted = iter(new_keys)
        next_new = next(inserted, None)
        for bucket, grouped in groupby(keys, key=lambda key: key[3:5]):
            selected = tuple(grouped)
            wanted = set(selected)
            seen = 0
            check()
            opened = loader.graph_segment_chunk(kind, bucket)
            if opened is None:
                raise ValueError('conversation_append_chunk_missing')
            segment, encoded = opened
            if segment.kind != kind or segment.bucket != bucket:
                raise ValueError('conversation_append_chunk_invalid')
            for key, category, data in _checked_record_spans(segment, encoded, account, check):
                if key not in wanted:
                    continue
                if seen >= len(selected) or selected[seen] != key:
                    raise ValueError('conversation_append_membership_invalid')
                seen += 1
                raw = data.encode('utf-8')
                if old_count:
                    before.update(b',')
                before.update(raw)
                old_count += 1
                while next_new is not None and next_new < key:
                    check()
                    if new_count:
                        after.update(b',')
                    after.update(changed[next_new].encode('utf-8'))
                    new_count += 1
                    next_new = next(inserted, None)
                replacement = changed.get(key)
                if replacement is not None and replacement != data:
                    if key != conversation_node:
                        raise ValueError('conversation_append_changed_prefix_record')
                    raw = replacement.encode('utf-8')
                if new_count:
                    after.update(b',')
                after.update(raw)
                new_count += 1
            if seen != len(selected):
                raise ValueError('conversation_append_membership_missing')
            check()
        while next_new is not None:
            check()
            if new_count:
                after.update(b',')
            after.update(changed[next_new].encode('utf-8'))
            new_count += 1
            next_new = next(inserted, None)
        final_members[kind] = tuple(merge(keys, new_keys))
        if old_count != len(keys) or new_count != len(final_members[kind]):
            raise ValueError('conversation_append_membership_invalid')
    before.update(b']}')
    after.update(b']}')
    if 'sha256:' + before.hexdigest() != previous.header.graph_digest:
        raise ValueError('conversation_append_graph_digest_invalid')
    check()
    return create_membership_unit(account_ref=account, conversation_ref=previous.header.conversation_ref,
        input_digest=input_digest, config_digest=config_digest, cutoff=cutoff,
        findings=findings, metrics=metrics, nodes=final_members['node'], edges=final_members['edge'],
        digest='sha256:' + after.hexdigest())
