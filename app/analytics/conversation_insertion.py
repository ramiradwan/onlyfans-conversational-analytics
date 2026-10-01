"""Reuse a proved canonical selection for one bounded tied-time insertion."""
from dataclasses import dataclass
from datetime import datetime
import json

from app.analytics.compact_graph import CompactGraph
from app.analytics.conversation_enrichment_units import (
    ConversationEnrichmentUnit, InsertedMessageEnrichments, message_records,
    MAX_ENRICHMENT_UNIT_RECORDS, _canonical,
)
from app.analytics.conversation_pages import PAGE_RECORDS
from app.analytics.metrics import ConversationMetricInput, build_conversation_metrics_from_values
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref, participant_ref
from app.analytics.source_snapshot import conversation_digest
from app.models.analytics import CanonicalConversation, GraphEdge


@dataclass(frozen=True)
class InsertedConversation:
    findings: InsertedMessageEnrichments
    metrics: object
    delta: CompactGraph
    graph_unit: object
    enrichment_unit: ConversationEnrichmentUnit
    removed_edges: frozenset[str]

def metric_input(value):
    return ConversationMetricInput(datetime.fromisoformat(value['sent_at']), value['source_ordinal'],
        value['direction'], value['sentiment']['label'], float(value['sentiment']['score']),
        tuple(t['taxonomy_id'] for t in value['topic_entities']['topics']),
        tuple(e['entity_type'] for e in value['topic_entities']['entities']), value['engagement']['state'])

def match_inserted_source(account, raw, previous, rows, check):
    """Removing exactly one row must reproduce the independently verified input."""
    messages = raw['messages']
    if len(messages) != previous.message_count + 1 or len(rows) != previous.message_count:
        return None
    if not rows or len(messages) > MAX_ENRICHMENT_UNIT_RECORDS:
        return None
    prior, output, seen = [], [], set()
    insertion = None
    cursor = 0
    for ordinal, record in enumerate(rows):
        check()
        value = json.loads(record)
        reference = value['message_ref']
        if reference in seen or type(value['source_ordinal']) is not int or value['source_ordinal'] != ordinal:
            return None
        seen.add(reference)
        selected = messages[cursor]
        selected_ref = message_ref(account, raw['conversation_id'], selected['message_id'])
        if selected_ref != reference:
            if insertion is not None:
                return None
            insertion = cursor
            output.append(None)
            cursor += 1
            selected = messages[cursor]
            selected_ref = message_ref(account, raw['conversation_id'], selected['message_id'])
        if (selected_ref != reference or type(selected['source_ordinal']) is not int
                or selected['source_ordinal'] != cursor
                or value['account_ref'] != previous.account_ref
                or value['conversation_ref'] != previous.conversation_ref
                or value['participant_ref'] != previous.metrics.participant_ref
                or not isinstance(value['sent_at'], str)
                or datetime.fromisoformat(value['sent_at']) != datetime.fromisoformat(selected['sent_at'])
                or value['direction'] != selected['direction']):
            return None
        prior.append(selected if cursor == ordinal else dict(selected, source_ordinal=ordinal))
        actual = value if cursor == ordinal else dict(value, source_ordinal=cursor)
        output.append(record if cursor == ordinal else _canonical(actual))
        cursor += 1
    # Appends use their existing path. This path requires an actual tied insertion.
    if insertion is None or cursor != len(messages) or len(messages)-insertion-1 > PAGE_RECORDS:
        return None
    added = messages[insertion]
    if (type(added['source_ordinal']) is not int or added['source_ordinal'] != insertion
            or message_ref(account, raw['conversation_id'], added['message_id']) in seen
            or datetime.fromisoformat(added['sent_at']) != datetime.fromisoformat(messages[insertion+1]['sent_at'])):
        return None
    old_raw = dict(raw, messages=prior, last_message_at=prior[-1]['sent_at'])
    check()
    if conversation_digest(old_raw) != previous.input_digest:
        return None
    check()
    # Source/identity checks above remain complete. Metric objects are built
    # only if the bounded terminal-tie calculation cannot use prior aggregates.
    return insertion, old_raw, output, None


def build_inserted_metrics(previous, rows, index, inserted, check):
    """Compute from source-matched rows; never trust a supplied metric result."""
    from app.analytics import tied_insertion_metrics
    from app.analytics.metrics import build_conversation_metrics_from_bound_values
    from app.models.analytics import MessageEnrichment

    suffix = []
    for row in rows[index+1:]:
        check()
        suffix.append(MessageEnrichment.model_validate_json(row))
    result = tied_insertion_metrics.tied_suffix_metrics(previous.metrics, suffix, inserted, check)
    if result is not None:
        return result
    values = []
    for row in rows:
        check()
        values.append(metric_input(json.loads(row)))
    return build_conversation_metrics_from_bound_values(
        previous.account_ref, previous.conversation_ref, previous.metrics.participant_ref,
        previous.metrics.unread_count, values)

def suffix_graph(pipeline, account, revision, raw, rows, metrics, start, check, cancellation):
    """Use the ordinary projector for every affected ordinal and neighbor."""
    from app.models.analytics import MessageEnrichment
    conversation = CanonicalConversation.model_validate(dict(raw, messages=raw['messages'][start:]))
    findings = [MessageEnrichment.model_validate_json(row) for row in rows[start:]]
    graph = CompactGraph(account_ref(account))
    for nodes, edges in pipeline.graph_projector.batches(account, revision, [conversation], findings,
                                                       [metrics], cancellation_check=cancellation):
        adjusted = []
        for edge in edges:
            check()
            adjusted.append(edge if edge.sequence is None else GraphEdge.model_validate(
                {**edge.model_dump(), 'sequence': edge.sequence + start}))
        graph.add(nodes, adjusted, check=check)
    return graph

def try_insert(pipeline, account, revision, raw, input_digest, loader, reuse, config, cutoff, check, cancellation):
    """Only an exact one-message insertion with message-local analysis is eligible."""
    from app.analytics.enrichment import EnrichmentStage
    from app.analytics.graph_projection import RelationshipGraphProjector
    if (reuse is None or not pipeline.reuse_enrichment or not pipeline.reuse_conversations
            or type(pipeline.enrichment) is not EnrichmentStage
            or type(pipeline.graph_projector) is not RelationshipGraphProjector):
        return None
    pipeline.enrichment.validate_configuration()
    if any(p is None or p.preceding_messages or p.following_messages for p in pipeline.enrichment._input_policies):
        return None
    graph_proof = getattr(loader, 'graph_unit_proof', None)
    segment_proof = getattr(loader, 'graph_segment_proof', None)
    proof = getattr(loader, 'enrichment_unit_proof', None)
    generation = getattr(loader, 'active_generation_id', None)
    if (proof is None or graph_proof is None or segment_proof is None or generation is None
            or any(p.generation_id != generation for p in (proof, graph_proof, segment_proof))):
        return None
    partition, ref = account_ref(account), conversation_ref(account, raw['conversation_id'])
    previous = next((h for h in proof.headers if h.conversation_ref == ref), None)
    if (previous is None or not previous.message_count or previous.account_ref != partition
            or previous.config_digest != config or previous.retention_cutoff > cutoff
            or previous.first_source_at <= cutoff or previous.expires_at <= pipeline._retention_clock()
            or previous.metrics.participant_ref != participant_ref(account, raw['platform_user_id'])
            or previous.metrics.unread_count != raw['unread_count']
            or len(raw['messages']) != previous.message_count + 1
            or datetime.fromisoformat(raw['messages'][-1]['sent_at']) != previous.last_source_at):
        return None
    reference = loader.enrichment_unit_reference(ref, previous.input_digest, config)
    if reference is None or reference.header != previous:
        return None
    old_graph = loader.previous_graph_unit(ref)
    if (old_graph is None or old_graph.header.checksum_version != 2
            or old_graph.header.input_digest != previous.input_digest or old_graph.header.config_digest != config
            or old_graph.header.account_ref != partition or old_graph.header.conversation_ref != ref
            or old_graph.header.expires_at <= pipeline._retention_clock()):
        return None
    pair = pipeline.projections.load_enrichment_unit_contents(partition, [previous.unit_id]).get(previous.unit_id)
    if pair is None:
        return None
    unit = ConversationEnrichmentUnit(previous, pair[0], pair[1])
    rows = message_records(unit)
    matched = match_inserted_source(account, raw, previous, rows, check)
    if matched is None:
        return None
    index, old_raw, output, values = matched
    start = max(0, index-1)
    conversation = CanonicalConversation.model_validate(dict(raw, messages=raw['messages'][start:]))
    added = next(m for m in conversation.messages if m.source_ordinal == index)
    with reuse.known_new_message(message_ref(account, raw['conversation_id'], added.message_id)):
        findings = pipeline.enrichment.enrich_conversation(account,
            conversation.model_copy(update={'messages': [added]}), cancellation_check=cancellation)
    if len(findings) != 1 or findings[0].source_ordinal != index:
        raise ValueError('conversation_insertion_message_invalid')
    inserted = findings[0]
    output[index] = _canonical(inserted.model_dump(mode='json'))
    metrics = build_inserted_metrics(previous, output, index, inserted, check)
    findings = InsertedMessageEnrichments(output, inserted, index, previous.first_source_at)
    old_delta = suffix_graph(pipeline, account, revision, old_raw, rows, previous.metrics, start, check, cancellation)
    delta = suffix_graph(pipeline, account, revision, raw, output, metrics, start, check, cancellation)
    from app.analytics.conversation_graph_insertion import replace_suffix
    graph_unit, removed = replace_suffix(loader, old_graph, old_delta, delta,
        input_digest=input_digest, config=config, cutoff=cutoff,
        findings=findings, metrics=metrics, check=check)
    if graph_unit is None:
        return None
    from app.analytics.conversation_enrichment_insertion import pack_insertion
    enrichment_unit = pack_insertion(unit, findings, metrics, input_digest, config, cutoff,
                                    reuse.conversation_entries(ref), check)
    if enrichment_unit is None:
        return None
    return InsertedConversation(findings, metrics, delta, graph_unit, enrichment_unit, frozenset(removed))
