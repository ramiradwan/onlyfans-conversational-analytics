"""Versioned, bounded conversation integrity summaries; graph semantics are unchanged."""
from __future__ import annotations
from itertools import groupby
import hashlib
import json
import re
from app.analytics.graph_identity import require_graph_id
from app.analytics.opaque_refs import require_opaque_ref

VERSION = 2
MAX_GROUP_RECORDS = 16384
MAX_METADATA_BYTES = 256 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_BUCKET = re.compile(r"[0-9a-f]{2}\Z")

class IntegrityCapacity(ValueError):
    """This optional summary exceeds its bounded representation."""

def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode('ascii')

def membership_digest(keys):
    digest = hashlib.sha256(b'conversation-members.v2\0')
    for key in keys:
        digest.update(key.encode('ascii') + b'\n')
    return digest.hexdigest()

def summarize_group(account, conversation, kind, bucket, versions, check=lambda: None):
    """Bind exact ordered identities and content versions, never a cached payload claim."""
    require_opaque_ref(account, 'account')
    require_opaque_ref(conversation, 'conversation')
    if kind not in ('node', 'edge') or not _BUCKET.fullmatch(bucket):
        raise ValueError('conversation_integrity_group_invalid')
    if not 0 < len(versions) <= MAX_GROUP_RECORDS:
        raise IntegrityCapacity('conversation_integrity_group_capacity')
    keys = sorted(versions)
    digest = hashlib.sha256(_encode(['conversation-group.v2', account, conversation, kind, bucket]))
    for key in keys:
        check()
        require_graph_id(key, expected_kind='edge' if kind == 'edge' else None)
        content = versions[key]
        if key[3:5] != bucket or not isinstance(content, str) or not _HEX.fullmatch(content):
            raise ValueError('conversation_integrity_content_invalid')
        digest.update(b'\0' + key.encode('ascii') + b':' + content.encode('ascii'))
    return (kind, bucket, len(keys), membership_digest(keys), digest.hexdigest())

def _checked_groups(groups):
    if not isinstance(groups, (list, tuple)) or not 1 <= len(groups) <= 512:
        raise ValueError('conversation_integrity_manifest_invalid')
    result, previous = [], None
    for value in groups:
        if not isinstance(value, (list, tuple)) or len(value) != 5:
            raise ValueError('conversation_integrity_manifest_invalid')
        kind, bucket, count, members, content = value
        if (kind not in ('node', 'edge') or not isinstance(bucket, str)
                or not _BUCKET.fullmatch(bucket) or type(count) is not int
                or not 0 < count <= MAX_GROUP_RECORDS
                or any(not isinstance(v, str) or not _HEX.fullmatch(v) for v in (members, content))):
            raise ValueError('conversation_integrity_manifest_invalid')
        key = (kind, bucket)
        if previous is not None and key <= previous:
            raise ValueError('conversation_integrity_group_order_invalid')
        previous = key
        result.append(tuple(value))
    return tuple(result)

def encode_manifest(account, conversation, groups):
    groups = _checked_groups(groups)
    require_opaque_ref(account, 'account')
    require_opaque_ref(conversation, 'conversation')
    data = _encode({'version': VERSION, 'account': account, 'conversation': conversation, 'groups': groups})
    if len(data) > MAX_METADATA_BYTES:
        raise IntegrityCapacity('conversation_integrity_manifest_capacity')
    root = hashlib.sha256(b'conversation-integrity-root.v2\0' + data).hexdigest()
    return 'sha256:' + root, data

def decode_manifest(unit):
    data, header = unit.integrity_metadata, unit.header
    if header.checksum_version != VERSION or not isinstance(data, bytes) or len(data) > MAX_METADATA_BYTES:
        raise ValueError('conversation_integrity_version_invalid')
    try:
        value = json.loads(data)
        if (set(value) != {'version', 'account', 'conversation', 'groups'}
                or type(value['version']) is not int or value['version'] != VERSION
                or value['account'] != header.account_ref
                or value['conversation'] != header.conversation_ref):
            raise ValueError('conversation_integrity_scope_invalid')
        root, canonical = encode_manifest(value['account'], value['conversation'], value['groups'])
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('conversation_integrity_manifest_invalid') from error
    if canonical != data or root != header.graph_digest:
        raise ValueError('conversation_integrity_digest_invalid')
    groups = _checked_groups(value['groups'])
    if (sum(g[2] for g in groups if g[0] == 'node') != header.node_count
            or sum(g[2] for g in groups if g[0] == 'edge') != header.edge_count):
        raise ValueError('conversation_integrity_count_invalid')
    return {(g[0], g[1]): g for g in groups}

def groups_for_members(unit, check=lambda: None):
    from app.analytics.conversation_graph_units import graph_unit_ids
    summaries = decode_manifest(unit)
    from app.analytics.conversation_id_frames import canonical_groups
    framed = canonical_groups(unit, summaries, check)
    if framed is not None:
        return summaries, framed
    result = {}
    for kind, keys in zip(('node', 'edge'), graph_unit_ids(unit), strict=True):
        for bucket, values in groupby(keys, key=lambda key: key[3:5]):
            check()
            selected = tuple(values)
            summary = summaries.get((kind, bucket))
            if summary is None or summary[2:4] != (len(selected), membership_digest(selected)):
                raise ValueError('conversation_integrity_membership_invalid')
            result[(kind, bucket)] = selected
    if result.keys() != summaries.keys():
        raise ValueError('conversation_integrity_membership_invalid')
    return summaries, result

def from_graph(account, conversation, graph, check=lambda: None):
    groups = []
    if graph.account_ref != account:
        raise ValueError('conversation_integrity_scope_invalid')
    for kind, records in (('edge', graph.edges), ('node', graph.nodes)):
        for bucket, keys in groupby(sorted(records), key=lambda key: key[3:5]):
            versions = {}
            for key in keys:
                check()
                if len(versions) >= MAX_GROUP_RECORDS:
                    raise IntegrityCapacity('conversation_integrity_group_capacity')
                versions[key] = hashlib.sha256(records[key].encode('utf-8')).hexdigest()
            groups.append(summarize_group(account, conversation, kind, bucket, versions, check))
    return encode_manifest(account, conversation, groups)

def append_unit(loader, previous, delta, *, conversation_node, input_digest,
                config_digest, cutoff, findings, metrics, check,
                trusted_predecessor=False):
    """Only touched groups read exact predecessor content-version metadata."""
    from app.analytics.conversation_graph_units import create_membership_unit
    header = previous.header
    if delta.account_ref != header.account_ref:
        raise ValueError('conversation_integrity_scope_invalid')
    trusted = None
    if trusted_predecessor:
        from app.analytics.conversation_id_frames import trusted_groups
        trusted = trusted_groups(previous, check)
    summaries, members = (
        trusted if trusted is not None else groups_for_members(previous, check)
    )
    final = {kind: [] for kind in ('node', 'edge')}
    for kind, changes in (('edge', delta.edges), ('node', delta.nodes)):
        for bucket in sorted({key[3:5] for key in changes}):
            check()
            selected = members.get((kind, bucket), ())
            content_ids = (
                getattr(loader, 'append_graph_content_ids', None)
                if trusted_predecessor else None
            )
            if content_ids is None:
                content_ids = loader.graph_content_ids
            versions = content_ids(kind, list(selected), check=check) if selected else {}
            if len(versions) != len(selected):
                raise ValueError('conversation_integrity_membership_missing')
            if selected and summarize_group(header.account_ref, header.conversation_ref,
                    kind, bucket, versions, check) != summaries[(kind, bucket)]:
                raise ValueError('conversation_integrity_predecessor_changed')
            for key, data in changes.items():
                if key[3:5] != bucket:
                    continue
                check()
                content = hashlib.sha256(data.encode('utf-8')).hexdigest()
                old = versions.get(key)
                if old is not None and old != content and key != conversation_node:
                    raise ValueError('conversation_append_changed_prefix_record')
                versions[key] = content
            summaries[(kind, bucket)] = summarize_group(header.account_ref,
                header.conversation_ref, kind, bucket, versions, check)
            members[(kind, bucket)] = tuple(sorted(versions))
        for (group_kind, bucket), keys in sorted(members.items()):
            if group_kind == kind:
                final[kind].append(keys)
    root, metadata = encode_manifest(header.account_ref, header.conversation_ref,
                                    [summaries[k] for k in sorted(summaries)])
    from app.analytics.conversation_id_frames import IdGroups
    return create_membership_unit(account_ref=header.account_ref,
        conversation_ref=header.conversation_ref, input_digest=input_digest,
        config_digest=config_digest, cutoff=cutoff, findings=findings, metrics=metrics,
        nodes=IdGroups(final['node']), edges=IdGroups(final['edge']), digest=root,
        checksum_version=VERSION, integrity_metadata=metadata)
