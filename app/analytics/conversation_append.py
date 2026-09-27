"""Reuse an independently matched prefix within the existing graph representation."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
import hashlib
import json
import re

from app.analytics.compact_graph import CompactGraph
from app.analytics.conversation_enrichment_units import (
    ConversationEnrichmentUnit, message_records,
)
from app.analytics.conversation_graph_units import graph_unit_ids
from app.analytics.graph_projection import stable_node_id
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.metrics import build_conversation_metrics
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.source_snapshot import conversation_digest
from app.models.analytics import GraphEdge, GraphNodeKind, MessageEnrichment

MIN_APPEND_MESSAGES = 1024


def _previous_graph(loader, unit, check):
    """Read exact canonical bytes from verified chunks, not stored digest claims."""
    graph = CompactGraph(unit.header.account_ref)
    node_ids, edge_ids = graph_unit_ids(unit)
    for kind, keys, target, counts in (
        ('node', node_ids, graph.nodes, graph.node_counts),
        ('edge', edge_ids, graph.edges, graph.edge_counts),
    ):
        grouped = defaultdict(set)
        for key in keys:
            grouped[key[3:5]].add(key)
        for bucket, selected in sorted(grouped.items()):
            check()
            chunk = loader.graph_segment_chunk(kind, bucket)
            if chunk is None:
                raise ValueError('conversation_append_chunk_missing')
            segment, encoded = chunk
            if segment.kind != kind or segment.bucket != bucket:
                raise ValueError('conversation_append_chunk_invalid')
            for key, category, data in _checked_record_spans(segment, encoded, graph.account_ref, check):
                if key in selected:
                    if key in target:
                        raise ValueError('conversation_append_graph_invalid')
                    target[key] = data
                    counts[category] += 1
                    graph.encoded_bytes += len(data.encode('utf-8'))
        if set(target) != set(keys):
            raise ValueError('conversation_append_membership_missing')
    if graph.digest(check=check) != unit.header.graph_digest:
        raise ValueError('conversation_append_graph_digest_invalid')
    return graph


def try_append(pipeline, account, source_revision, conversation, raw, loader, reuse, config, cutoff,
               check, cancellation_check):
    """Use only a current-process proof and an exact canonical prefix match."""
    from app.analytics.enrichment import EnrichmentStage
    from app.analytics.graph_projection import RelationshipGraphProjector
    if (reuse is None or not pipeline.reuse_enrichment or not pipeline.reuse_conversations
            or type(pipeline.enrichment) is not EnrichmentStage
            or type(pipeline.graph_projector) is not RelationshipGraphProjector):
        return None
    policies = pipeline.enrichment._input_policies
    if any(p is None or p.preceding_messages or p.following_messages for p in policies):
        return None
    graph_proof = getattr(loader, 'graph_segment_proof', None)
    enrichment_proof = getattr(loader, 'enrichment_unit_proof', None)
    if graph_proof is None or enrichment_proof is None:
        return None
    partition, ref = account_ref(account), conversation_ref(account, conversation.conversation_id)
    expected = next((h for h in enrichment_proof.headers if h.conversation_ref == ref), None)
    if (expected is None or expected.message_count < MIN_APPEND_MESSAGES
            or expected.account_ref != partition or expected.config_digest != config
            or expected.retention_cutoff > cutoff or expected.expires_at <= cutoff + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)):
        return None
    messages = raw['messages']
    ordered = sorted(conversation.messages, key=lambda m: (m.sent_at, m.source_ordinal))
    if (len(messages) != expected.message_count + 1 or len(ordered) != len(messages)
            or ordered[-1].message_id != messages[-1]['message_id']
            or ordered[-1].sent_at < expected.last_source_at):
        return None
    prefix = dict(raw, messages=messages[:-1], last_message_at=messages[-2]['sent_at'])
    if conversation_digest(prefix) != expected.input_digest:
        return None
    old_graph = loader.previous_graph_unit(ref)
    if old_graph is None:
        return None
    h = old_graph.header
    if (h.account_ref != partition or h.conversation_ref != ref
            or h.input_digest != expected.input_digest or h.config_digest != config
            or h.retention_cutoff > cutoff or h.expires_at <= pipeline._retention_clock()):
        return None
    total = sum(segment.count for segment in graph_proof.segments)
    if 4 * (h.node_count + h.edge_count) < total:
        return None  # Reading whole bucket chunks is reserved for dominant threads.
    reference = loader.enrichment_unit_reference(ref, expected.input_digest, config)
    if reference is None or reference.header != expected:
        return None
    content = pipeline.projections.load_enrichment_unit_contents(partition, [expected.unit_id])
    pair = content.get(expected.unit_id)
    if pair is None:
        raise ValueError('conversation_append_enrichment_missing')
    unit = ConversationEnrichmentUnit(expected, pair[0], pair[1])
    findings = []
    for record, message in zip(message_records(unit), ordered[:-1], strict=True):
        check()
        finding = MessageEnrichment.model_validate_json(record)
        if (finding.account_ref != partition or finding.conversation_ref != ref
                or finding.message_ref != message_ref(account, conversation.conversation_id, message.message_id)
                or finding.source_ordinal != message.source_ordinal
                or finding.sent_at != message.sent_at or finding.direction != message.direction):
            raise ValueError('conversation_append_source_mismatch')
        findings.append(finding)
    with reuse.known_new_message(message_ref(account, conversation.conversation_id, ordered[-1].message_id)):
        findings.extend(pipeline.enrichment.enrich_conversation(account,
            conversation.model_copy(update={'messages': ordered[-1:]}),
            cancellation_check=cancellation_check))
    metrics = build_conversation_metrics(account, conversation, findings)
    graph = _previous_graph(loader, old_graph, check)
    delta = CompactGraph(partition)
    boundary = conversation.model_copy(update={'messages': ordered[-2:]})
    shift = len(ordered) - 2
    for nodes, edges in pipeline.graph_projector.batches(account, source_revision,
            [boundary], findings[-2:], [metrics], cancellation_check=cancellation_check):
        corrected = []
        for edge in edges:
            check()
            if edge.sequence is not None:
                edge = GraphEdge.model_validate({**edge.model_dump(), 'sequence': edge.sequence + shift})
            corrected.append(edge)
        delta.add(nodes, corrected, check=check)
    conversation_node = stable_node_id(partition, GraphNodeKind.CONVERSATION, ref)
    for kind, records, target, counts in (
        ('node', delta.nodes, graph.nodes, graph.node_counts),
        ('edge', delta.edges, graph.edges, graph.edge_counts),
    ):
        for key, data in records.items():
            check()
            previous = target.get(key)
            if previous is not None and previous != data and key != conversation_node:
                raise ValueError('conversation_append_changed_prefix_record')
            if previous is None:
                counts[json.loads(data)['kind' if kind == 'node' else 'relation']] += 1
            target[key] = data
            graph.encoded_bytes += len(data.encode('utf-8')) - (len(previous.encode('utf-8')) if previous else 0)
    return findings, metrics, graph, delta, unit


# Canonical graph records have fixed leading fields. Their complete bytes have
# already been validated by the generation proof; this is framing, not validation
# of arbitrary JSON. Recheck the actual chunk digest and all record metadata.
_RECORD_START = re.compile(
    r'(?:^|,)(?P<record>\{"account_ref":"(?P<account>[^"\\]+)",'
    r'(?:"kind":"(?P<category>[^"\\]+)","node_id":"(?P<node>[^"\\]+)"'
    r'|"edge_id":"(?P<edge>[^"\\]+)"))'
)


def _checked_record_spans(segment, encoded, account, check):
    """Frame only the exact bytes bound to a complete-content segment proof."""
    if (segment.kind not in ('node', 'edge') or segment.chunk_digest is None
            or hashlib.sha256(encoded).hexdigest() != segment.chunk_digest):
        raise ValueError('conversation_append_chunk_invalid')
    text = encoded.decode('utf-8')
    if not text:
        raise ValueError('conversation_append_chunk_invalid')
    count, categories, position = 0, Counter(), 0
    while position < len(text):
        check()
        previous = _RECORD_START.match(text, position - 1 if position else 0)
        if previous is None or previous.start('record') != position:
            raise ValueError('conversation_append_chunk_invalid')
        end = text.find(',{"account_ref":', position + 1)
        if end < 0:
            end = len(text)
        data = text[position:end]
        key = previous.group(segment.kind)
        if previous.group('account') != account or key is None or not data.endswith('}'):
            raise ValueError('conversation_append_chunk_invalid')
        category = previous.group('category')
        if segment.kind == 'edge':
            marker = ',"relation":"'
            start = data.rfind(marker)
            if start < 0:
                raise ValueError('conversation_append_chunk_invalid')
            start += len(marker)
            finish = data.find('"', start)
            if not data[finish:].startswith('","sequence":'):
                raise ValueError('conversation_append_chunk_invalid')
            category = data[start:finish]
        count += 1
        categories[category] += 1
        yield key, category, data
        position = end + 1
    if count != segment.count or tuple(sorted(categories.items())) != segment.categories:
        raise ValueError('conversation_append_chunk_invalid')
    check()
