"""Verify conversation summaries against independently checked generation content."""
from collections import OrderedDict
from dataclasses import dataclass
from sys import getsizeof

from app.core.lifecycle_receipts import startup_timed

from app.analytics import conversation_graph_unit_sql as units
from app.analytics.conversation_integrity import (
    decode_manifest, groups_for_members, summarize_group,
)
from app.analytics.validation_receipt import content_stamp, generation_binding
from app.analytics.shared_graph import MAX_CHANGED_VERIFICATION_ROWS, MAX_CHANGED_VERIFICATION_BYTES


@dataclass(frozen=True, slots=True)
class VerifiedConversationIntegrity:
    headers: tuple
    integrity_groups: tuple
    membership_prefixes: tuple = ()


# Additional cold-selection buffers only; existing whole-unit bounds still apply.
MAX_FULL_SELECTION_IDS = 256
MAX_FULL_SELECTION_BYTES = 256 * 1024
_FULL_SELECTION_BASE_BYTES = 4096
_FULL_SELECTION_MEMBER_BYTES = (
    3 * getsizeof('g1:' + '0' * 64) + getsizeof('0' * 64) + 256
)
_FULL_SELECTION_GROUP_BYTES = 256


def _verify_cold_groups(connection, generation_id, account, conversation,
                        summaries, members, current, versions, check, _selection=None):
    """Combine small full-validation selections, retaining each original summary."""
    from app.analytics.shared_graph import selected_content_ids

    pending, identities = [], []
    used, kind = _FULL_SELECTION_BASE_BYTES, None

    def verify(key, summary, selected, actual):
        check()
        if len(actual) != len(selected) or summarize_group(
                account, conversation, *key, actual, check) != summary:
            raise ValueError('conversation_integrity_selected_content_changed')

    def flush():
        nonlocal used, kind
        if not pending:
            return
        check()
        actual = (None if _selection is None
                  else _selection.selected(kind, None, identities))
        if actual is None:
            actual = selected_content_ids(connection, generation_id, account, kind,
                                          identities, check, page_layout=True)
        if actual.keys() != set(identities):
            raise ValueError('conversation_integrity_selected_content_changed')
        for key, summary, selected in pending:
            check()
            # Validate the original group, rather than a combined batch digest.
            group = {identity: actual[identity] for identity in selected}
            verify(key, summary, selected, group)
        pending.clear()
        identities.clear()
        used, kind = _FULL_SELECTION_BASE_BYTES, None

    for key, summary in summaries.items():
        check()
        selected = members[key]
        segment = current.get(key)
        segment_id = segment if isinstance(segment, str) else getattr(segment, 'segment_id', None)
        # Ordering and duplicate checks already ran on the actual unit bytes.
        # Charge fixed-width IDs, content hashes and conservative container space.
        cost = len(selected) * _FULL_SELECTION_MEMBER_BYTES + _FULL_SELECTION_GROUP_BYTES
        if (segment_id is None or len(selected) > MAX_FULL_SELECTION_IDS
                or _FULL_SELECTION_BASE_BYTES + cost > MAX_FULL_SELECTION_BYTES):
            flush()
            verify(key, summary, selected, versions(*key, selected))
            continue
        if pending and (kind != key[0]
                or len(identities) + len(selected) > MAX_FULL_SELECTION_IDS
                or used + cost > MAX_FULL_SELECTION_BYTES):
            flush()
        kind = key[0]
        pending.append((key, summary, selected))
        for identity in selected:
            check()
            identities.append(identity)
        used += cost
    flush()


@startup_timed('startup.conversation_integrity', counter='startup.conversation_integrity.calls')
def verify_generation_integrity(connection, generation, account, *, proof=None,
                                graph_validation=None, segments=(), prepared=None,
                                verified_changes=None, check=lambda: None,
                                _selection=None):
    """A process-local proof can skip only groups in unchanged verified segments."""
    if not units.integrity_supported(connection):
        return VerifiedConversationIntegrity((), (), ())
    references = units.list_references(connection, generation['generation_id'], account)
    if not any(ref.header.checksum_version == 2 for ref in references):
        return VerifiedConversationIntegrity((), (), ())
    stamp = content_stamp(connection)
    from app.analytics.cold_graph_selection import ColdGraphSelection
    if (type(_selection) is not ColdGraphSelection
            or proof is not None or graph_validation is not None
            or prepared is not None or verified_changes is not None
            or not _selection.matches(connection, generation, account)):
        _selection = None
    trusted, trusted_groups, trusted_prefixes, old_segments, old_generation = {}, {}, {}, {}, None
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
            trusted_groups = dict(getattr(proof, 'integrity_groups', ()))
            trusted_prefixes = {item[0]: (item[1], item[2])
                                for item in getattr(proof, 'membership_prefixes', ())}
            old_segments = {(s.kind, s.bucket): s for s in prior_graph.segments}
            old_generation = proof.generation_id
    current = {(s.kind, s.bucket): s for s in segments}
    if not current:
        rows = connection.execute('SELECT m.kind,m.bucket,m.segment_id FROM generation_graph_segments m '
            'WHERE m.generation_id=? AND m.creator_account_id=?', (generation['generation_id'], account))
        current = {(r['kind'], r['bucket']): r['segment_id'] for r in rows}
    selection = getattr(verified_changes, 'membership_selection', None)
    from app.analytics.graph_membership_selection import MembershipSelection
    if (not isinstance(selection, MembershipSelection) or not trusted
            or not selection.matches(connection, generation, account, graph_validation)):
        selection = None
    cache, cache_rows, cache_bytes = OrderedDict(), 0, 0

    def versions(kind, bucket, selected):
        nonlocal cache_rows, cache_bytes, selection
        segment = current.get((kind, bucket))
        segment_id = segment if isinstance(segment, str) else getattr(segment, 'segment_id', None)
        if segment_id is None:
            raise ValueError('conversation_integrity_segment_missing')
        if _selection is not None:
            covered = _selection.selected(kind, bucket, selected)
            if covered is not None:
                return covered
        if selection is not None:
            covered = selection.selected(kind, segment_id, selected)
            if covered is not None:
                return covered
            # Do not retain a selective buffer alongside an unbounded sequence
            # of fallback selections. Once coverage is incomplete, release it.
            selection.discard()
            selection = None
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

    from app.analytics.conversation_membership_validation import (
        changed_predecessor_members, predecessor_manifest_matches, proven_unit_is_unchanged,
    )
    complete_predecessor = bool(trusted) and predecessor_manifest_matches(
        connection, old_generation, account, prior_graph.segments, check)
    if complete_predecessor and verified_changes is not None:
        changes = verified_changes.invalidated
    else:
        changes = (changed_predecessor_members(
            connection, account, old_segments, current, prepared, check
        ) if complete_predecessor else None)
    headers, verified_groups, verified_prefixes = [], [], []
    capture_prefixes = proof is None and graph_validation is None
    for reference in references:
        check()
        h = reference.header
        if h.checksum_version == 1:
            continue
        predecessor = trusted.get(h.conversation_ref)
        if (changes is not None and predecessor == h
                and proven_unit_is_unchanged(
                    connection, generation['generation_id'], account, h, changes, check,
                    integrity_groups=trusted_groups.get(h.conversation_ref),
                    membership_prefixes=trusted_prefixes.get(h.conversation_ref),
                )):
            headers.append(h)
            verified_groups.append((h.conversation_ref, tuple(trusted_groups.get(h.conversation_ref, ()))))
            prefixes = trusted_prefixes.get(h.conversation_ref)
            if prefixes is not None:
                verified_prefixes.append((h.conversation_ref, prefixes[0], prefixes[1]))
            continue
        unit = units.load_unit(connection, generation['generation_id'], account, h.conversation_ref)
        if unit is None or unit.header != h:
            raise ValueError('conversation_integrity_unit_missing')
        predecessor = trusted.get(h.conversation_ref)
        if predecessor == h:
            old_summaries = decode_manifest(unit)
        else:
            previous = None if predecessor is None else units.load_integrity_metadata(
                connection, old_generation, account, h.conversation_ref)
            old_summaries = (decode_manifest(previous) if previous is not None
                and previous.header == predecessor and predecessor.checksum_version == 2 else {})
        summaries, members = groups_for_members(unit, check,
            proven_summaries=old_summaries if complete_predecessor else None)
        if (proof is None and graph_validation is None
                and prepared is None and verified_changes is None):
            _verify_cold_groups(connection, generation['generation_id'], account,
                h.conversation_ref, summaries, members, current, versions, check,
                _selection=_selection)
        else:
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
        verified_groups.append((h.conversation_ref, tuple(sorted(summaries))))
        if capture_prefixes:
            from itertools import chain
            from app.analytics.membership_prefixes import bitmap
            nodes = chain.from_iterable(
                values for (kind, _), values in members.items() if kind == 'node'
            )
            edges = chain.from_iterable(
                values for (kind, _), values in members.items() if kind == 'edge'
            )
            verified_prefixes.append((h.conversation_ref, bitmap(nodes), bitmap(edges)))
    if content_stamp(connection) != stamp:
        raise ValueError('conversation_integrity_store_changed')
    check()
    return VerifiedConversationIntegrity(
        tuple(headers), tuple(sorted(verified_groups)), tuple(sorted(verified_prefixes))
    )
