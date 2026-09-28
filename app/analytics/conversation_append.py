"""Reuse an independently matched prefix within the existing graph representation."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
import hashlib
import json
import re

from app.analytics.compact_graph import CompactGraph
from app.analytics.conversation_enrichment_units import (
    AppendedMessageEnrichments, ConversationEnrichmentUnit, message_records,
)
from app.analytics.conversation_graph_units import graph_unit_ids
from app.analytics.graph_projection import stable_node_id
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.metrics import (
    ConversationMetricInput, build_conversation_metrics_from_values as build_conversation_metrics,
)
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.source_snapshot import conversation_digest
from app.models.analytics import GraphEdge, GraphNodeKind

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
    if unit.header.checksum_version == 2:
        from app.analytics.conversation_integrity import from_graph
        digest, _ = from_graph(graph.account_ref, unit.header.conversation_ref, graph, check)
    else:
        digest = graph.digest(check=check)
    if digest != unit.header.graph_digest:
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
    raw_mode = conversation is None
    chat_id = raw['conversation_id'] if raw_mode else conversation.conversation_id
    partition, ref = account_ref(account), conversation_ref(account, chat_id)
    expected = next((h for h in enrichment_proof.headers if h.conversation_ref == ref), None)
    if (expected is None or expected.message_count < MIN_APPEND_MESSAGES
            or expected.account_ref != partition or expected.config_digest != config
            or expected.retention_cutoff > cutoff or expected.expires_at <= cutoff + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)):
        return None
    messages = raw['messages']
    if len(messages) != expected.message_count + 1:
        return None
    prefix = dict(raw, messages=messages[:-1], last_message_at=messages[-2]['sent_at'])
    if conversation_digest(prefix) != expected.input_digest:
        return None
    if raw_mode:
        # The exact prefix was already independently prepared and validated.
        # Validate new/boundary models, not another full copy of that prefix.
        from app.models.analytics import CanonicalConversation
        conversation = CanonicalConversation.model_validate(dict(raw, messages=messages[-2:]))
        ordered = [m.model_copy(update={'sent_at':pipeline._utc(m.sent_at)}) for m in conversation.messages]
        conversation = conversation.model_copy(update={'messages':ordered})
        if ordered[-1].source_ordinal != expected.message_count:
            return None
        def prefix_messages():
            for message in messages[:-1]:
                check()
                yield SimpleNamespace(message_id=message['message_id'],
                    sent_at=pipeline._utc(datetime.fromisoformat(message['sent_at'])),
                    source_ordinal=message['source_ordinal'], direction=message['direction'])
        prefix_values = prefix_messages()
    else:
        ordered = sorted(conversation.messages, key=lambda m: (m.sent_at, m.source_ordinal))
        if len(ordered) != len(messages):
            return None
        prefix_values = ordered[:-1]
    if (ordered[-1].message_id != messages[-1]['message_id']
            or ordered[-1].sent_at < expected.last_source_at):
        return None
    old_graph = loader.previous_graph_unit(ref)
    if old_graph is None:
        return None
    h = old_graph.header
    if getattr(loader, 'integrity_checksum_version', 1) == 2 and h.checksum_version == 1:
        return None  # Upgrade through complete ordinary construction, never relabel a digest.
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
    rows = message_records(unit)
    inputs, references = [], set()
    for record, message in zip(rows, prefix_values, strict=True):
        check()
        value = json.loads(record)
        if not isinstance(value['sent_at'], str):
            return None  # Noncanonical timestamp encodings use full model validation.
        at = datetime.fromisoformat(value['sent_at'])
        ref_value = message_ref(account, conversation.conversation_id, message.message_id)
        if (value['account_ref'] != partition or value['conversation_ref'] != ref
                or value['participant_ref'] != expected.metrics.participant_ref
                or value['message_ref'] != ref_value or ref_value in references
                or value['source_ordinal'] != message.source_ordinal
                or at != message.sent_at or value['direction'] != message.direction):
            raise ValueError('conversation_append_source_mismatch')
        references.add(ref_value)
        inputs.append(ConversationMetricInput(at, value['source_ordinal'], message.direction,
            value['sentiment']['label'], float(value['sentiment']['score']),
            tuple(topic['taxonomy_id'] for topic in value['topic_entities']['topics']),
            tuple(entity['entity_type'] for entity in value['topic_entities']['entities']),
            value['engagement']['state']))
    with reuse.known_new_message(message_ref(account, conversation.conversation_id, ordered[-1].message_id)):
        added = pipeline.enrichment.enrich_conversation(account,
            conversation.model_copy(update={'messages': ordered[-1:]}),
            cancellation_check=cancellation_check)
    if len(added) != 1 or added[0].message_ref in references:
        raise ValueError('conversation_append_tail_invalid')
    tail = added[0]
    inputs.append(ConversationMetricInput.from_enrichment(tail))
    metrics = build_conversation_metrics(account, conversation, inputs)
    findings = AppendedMessageEnrichments(rows, tail, references, inputs[0].sent_at, previous=unit)
    del inputs
    delta = CompactGraph(partition)
    boundary = conversation.model_copy(update={'messages': ordered[-2:]})
    shift = len(messages) - 2
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
    from app.analytics.conversation_graph_stream import append_unit
    graph_unit = append_unit(loader, old_graph, delta, conversation_node=conversation_node,
        input_digest=conversation_digest(raw), config_digest=config, cutoff=cutoff,
        findings=findings, metrics=metrics, check=check)
    graph = None
    if graph_unit is None:
        graph = _merge_append_delta(_previous_graph(loader, old_graph, check), delta, conversation_node, check)
    return findings, metrics, graph, delta if graph_unit is not None else None, unit, graph_unit


def _merge_append_delta(graph, delta, conversation_node, check):
    """Materialized fallback when the existing optional-unit bounds refuse an append."""
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
    return graph


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
