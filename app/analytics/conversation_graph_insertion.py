"""Replace only a canonically reconstructed suffix of a verified graph unit."""
import hashlib
import json
from app.analytics.conversation_integrity import (
    IntegrityCapacity, groups_for_members, summarize_group, encode_manifest,
)
from app.analytics.conversation_graph_units import create_membership_unit
from app.analytics.conversation_id_frames import IdGroups
from app.models.analytics import GraphRelation


def replace_suffix(loader, previous, old_delta, delta, *, input_digest, config, cutoff,
                   findings, metrics, check):
    h = previous.header
    if (h.checksum_version != 2 or h.account_ref != old_delta.account_ref
            or h.account_ref != delta.account_ref or h.config_digest != config
            or h.retention_cutoff > cutoff or h.participant_ref != metrics.participant_ref):
        raise ValueError('conversation_insertion_graph_binding_invalid')
    if set(old_delta.nodes) - set(delta.nodes):
        raise ValueError('conversation_insertion_node_removal_invalid')
    removed = set(old_delta.edges) - set(delta.edges)
    if len(removed) > 1 or any(
            json.loads(old_delta.edges[key])['relation'] != GraphRelation.PRECEDES.value
            or json.loads(old_delta.edges[key])['properties'].get('scope') != 'message'
            for key in removed):
        raise ValueError('conversation_insertion_edge_removal_invalid')
    # Only the loader can bind an actual stored unit to its predecessor proof.
    trusted = None
    reader = getattr(loader, 'insertion_graph_groups', None)
    content_reader = getattr(loader, 'insertion_graph_content_ids',
                             getattr(loader, 'append_graph_content_ids', None))
    if reader is not None and content_reader is not None:
        trusted = reader(previous, check)
    summaries, members = trusted if trusted is not None else groups_for_members(previous, check)
    if trusted is None:
        content_reader = loader.graph_content_ids
    for kind, before, after in (('node', old_delta.nodes, delta.nodes), ('edge', old_delta.edges, delta.edges)):
        deleted = removed if kind == 'edge' else set()
        for bucket in sorted({key[3:5] for key in (*before, *after)}):
            check()
            keys = members.get((kind, bucket), ())
            old = {key: data for key, data in before.items() if key[3:5] == bucket}
            new = {key: data for key, data in after.items() if key[3:5] == bucket}
            if trusted is not None and old == new:
                # This group does not change. Still bind the complete old suffix
                # to actual predecessor membership and content before reusing it.
                from bisect import bisect_left
                for key in old:
                    at = bisect_left(keys, key)
                    if at == len(keys) or keys[at] != key:
                        raise ValueError('conversation_insertion_old_suffix_mismatch')
                actual = content_reader(kind, list(old), check=check)
                if any(actual.get(key) != hashlib.sha256(data.encode()).hexdigest()
                       for key, data in old.items()):
                    raise ValueError('conversation_insertion_old_suffix_mismatch')
                continue
            versions = content_reader(kind, list(keys), check=check) if keys else {}
            if (len(versions) != len(keys) or keys and summarize_group(
                    h.account_ref, h.conversation_ref, kind, bucket, versions, check) != summaries[(kind, bucket)]):
                raise ValueError('conversation_insertion_predecessor_changed')
            for key, data in before.items():
                if key[3:5] == bucket and versions.get(key) != hashlib.sha256(data.encode()).hexdigest():
                    raise ValueError('conversation_insertion_old_suffix_mismatch')
            for key in deleted:
                if key[3:5] == bucket:
                    del versions[key]
            for key, data in after.items():
                if key[3:5] != bucket:
                    continue
                check()
                content = hashlib.sha256(data.encode()).hexdigest()
                if key in versions and versions[key] != content and key not in before:
                    raise ValueError('conversation_insertion_changed_unrelated_record')
                versions[key] = content
            if versions:
                try:
                    summaries[(kind, bucket)] = summarize_group(h.account_ref, h.conversation_ref,
                                                               kind, bucket, versions, check)
                except IntegrityCapacity:
                    return None, set()
                members[(kind, bucket)] = tuple(sorted(versions))
            else:
                summaries.pop((kind, bucket), None)
                members.pop((kind, bucket), None)
    root, metadata = encode_manifest(h.account_ref, h.conversation_ref, [summaries[k] for k in sorted(summaries)])
    nodes = IdGroups([values for (kind, _), values in sorted(members.items()) if kind == 'node'])
    edges = IdGroups([values for (kind, _), values in sorted(members.items()) if kind == 'edge'])
    unit = create_membership_unit(account_ref=h.account_ref, conversation_ref=h.conversation_ref,
        input_digest=input_digest, config_digest=config, cutoff=cutoff, findings=findings,
        metrics=metrics, nodes=nodes, edges=edges, digest=root, checksum_version=2, integrity_metadata=metadata)
    check()
    return unit, removed
