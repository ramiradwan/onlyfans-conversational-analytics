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
