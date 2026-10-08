"""Process-local envelope for one independently verified immutable generation.

The envelope introduces no new publication authority. It is installed only after
persisted validation succeeds and is reusable only while generation binding and
storage content stamp still match exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GenerationVerificationEnvelope:
    generation_id: str
    binding: str
    stamp: tuple
    graph: object
    conversations: object
    enrichment: object
    source_due_at: datetime | None


def build_envelope(receipt, graph, conversations, enrichment):
    if receipt is None or graph is None or conversations is None or enrichment is None:
        return None
    generation_id = receipt.generation_id
    binding = receipt.binding
    stamp = tuple(receipt.stamp)
    if (
        graph.generation_id != generation_id
        or conversations.generation_id != generation_id
        or enrichment.generation_id != generation_id
        or graph.binding != binding
        or conversations.binding != binding
        or enrichment.binding != binding
        or tuple(graph.stamp_prefix) != stamp[:3]
        or tuple(conversations.stamp_prefix) != stamp[:3]
        or tuple(enrichment.stamp) != stamp
        or not enrichment.headers
    ):
        return None
    return GenerationVerificationEnvelope(
        generation_id=generation_id,
        binding=binding,
        stamp=stamp,
        graph=graph,
        conversations=conversations,
        enrichment=enrichment,
        source_due_at=min(header.expires_at for header in enrichment.headers),
    )


def transitioned(envelope, old_enrichment, renewed_enrichment):
    if (
        envelope is None
        or renewed_enrichment is None
        or envelope.enrichment is not old_enrichment
        or renewed_enrichment.generation_id != envelope.generation_id
        or renewed_enrichment.binding != envelope.binding
        or tuple(renewed_enrichment.stamp[:3]) != tuple(envelope.stamp[:3])
    ):
        return None
    return replace(
        envelope,
        stamp=tuple(renewed_enrichment.stamp),
        enrichment=renewed_enrichment,
    )


@dataclass(frozen=True, slots=True)
class StartupVerification:
    """Checked bytes, not a published trust envelope; never serialized."""
    receipt: object
    row: tuple
    witness: object
    database_identity: tuple
    envelope: GenerationVerificationEnvelope
    account_ref: str
    source_revision: int
    canonical_content_digest: str
    pipeline_revision: str
    pipeline_config_digest: str


def envelope_from_values(receipt, values, *, legacy_units=None):
    """Construct all bounded metadata before touching any store cache."""
    from app.analytics.shared_graph import GraphSegmentProof
    from app.analytics.conversation_graph_units import ConversationGraphProof, MAX_PROOF_BUCKET_KEYS
    from app.analytics.conversation_enrichment_units import ConversationEnrichmentProof
    from app.analytics.conversation_reuse import MAX_GRAPH_UNITS
    from app.analytics.membership_prefixes import MAX_ENVELOPE_PREFIX_BYTES
    enrichment_headers = tuple(sorted(values.get('enrichment_units', ()), key=lambda h: h.conversation_ref))
    expected = {h.conversation_ref for h in enrichment_headers}
    segments = tuple(values.get('graph_segments', ()))
    if not 0 < len(expected) == len(enrichment_headers) <= MAX_GRAPH_UNITS:
        return None
    integrity = values.get('conversation_integrity')
    if legacy_units is None:
        if integrity is None:
            return None
        headers = tuple(sorted(integrity.headers, key=lambda h: h.conversation_ref))
        groups = tuple(sorted(integrity.integrity_groups))
        prefixes = tuple(getattr(integrity, 'membership_prefixes', ()))
        if (any(h.checksum_version != 2 for h in headers)
                or {x[0] for x in groups} != expected):
            return None
    else:
        from app.analytics.conversation_integrity import decode_manifest
        headers = tuple(sorted((u.header for u in legacy_units), key=lambda h:h.conversation_ref))
        groups, prefixes = [], []
        for unit in legacy_units:
            ref = unit.header.conversation_ref
            groups.append((ref, tuple(sorted(decode_manifest(unit))) if unit.header.checksum_version == 2 else ()))
            if unit.membership_prefixes is not None:
                prefixes.append((ref, *unit.membership_prefixes))
        groups, prefixes = tuple(sorted(groups)), tuple(prefixes)
    if len(headers) != len(expected) or {h.conversation_ref for h in headers} != expected:
        return None
    if sum(len(x[1]) for x in groups) > MAX_PROOF_BUCKET_KEYS:
        if legacy_units is None:
            return None
        groups = ()  # Keep the established legacy compatibility behavior.
    kept, used = [], 0
    for value in prefixes:
        if value[0] not in expected:
            continue
        cost = len(value[1]) + len(value[2])
        if used + cost <= MAX_ENVELOPE_PREFIX_BYTES:
            kept.append(value); used += cost
    graph = GraphSegmentProof(receipt.generation_id, receipt.binding, tuple(receipt.stamp[:3]), segments)
    conversations = ConversationGraphProof(receipt.generation_id, receipt.binding,
        tuple(receipt.stamp[:3]), headers, groups, tuple(sorted(kept)))
    enrichment = ConversationEnrichmentProof(receipt.generation_id, receipt.binding,
        tuple(receipt.stamp), enrichment_headers)
    return build_envelope(receipt, graph, conversations, enrichment)
