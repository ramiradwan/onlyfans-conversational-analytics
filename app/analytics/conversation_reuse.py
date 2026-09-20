"""Reuse exact conversation outputs while assembling a new full generation."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import timedelta

from pydantic import AwareDatetime, Field

from app.analytics.compact_graph import CompactGraph, CompactArtifact, _json
from app.analytics.cancellation import check_cancelled
from app.analytics.enrichment_cache import ACTIVE_REUSE, CacheRecord, CachedEnrichment, fingerprint
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.metrics import build_conversation_metrics, CONVERSATION_METRICS_PROVENANCE
from app.analytics.opaque_refs import account_ref, conversation_ref
from app.canonical.read_models import AccountReadModel
from app.models.analytics import (AccountRef, ConversationRef, Sha256Digest,
    ConversationMetrics, MessageEnrichment, GraphNode, GraphEdge)

MAX_FRAGMENT_BYTES = 8 * 1024 * 1024
MAX_FRAGMENT_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FRAGMENTS = 4096


class ConversationFragment(CacheRecord):
    account_ref: AccountRef
    conversation_ref: ConversationRef
    input_digest: Sha256Digest
    config_digest: Sha256Digest
    retention_cutoff: AwareDatetime
    expires_at: AwareDatetime
    enrichments: list[MessageEnrichment]
    metrics: ConversationMetrics
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    analyzer_entries: tuple[str, ...] = Field(default=(), repr=False)

    def validate_scope(self) -> None:
        if self.metrics.account_ref != self.account_ref or self.metrics.conversation_ref != self.conversation_ref:
            raise ValueError("conversation_fragment_scope_invalid")
        if not self.enrichments or any(m.account_ref != self.account_ref or
            m.conversation_ref != self.conversation_ref for m in self.enrichments):
            raise ValueError("conversation_fragment_sources_invalid")
        if (len({m.message_ref for m in self.enrichments}) != len(self.enrichments)
                or self.metrics.message_count != len(self.enrichments)
                or any(m.sent_at <= self.retention_cutoff or m.participant_ref != self.metrics.participant_ref
                       for m in self.enrichments)
                or len({n.node_id for n in self.nodes}) != len(self.nodes)
                or len({e.edge_id for e in self.edges}) != len(self.edges)):
            raise ValueError("conversation_fragment_membership_invalid")
        due = min(m.sent_at for m in self.enrichments) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
        if self.expires_at != due or any(n.account_ref != self.account_ref for n in self.nodes):
            raise ValueError("conversation_fragment_expiry_invalid")
        ids = {n.node_id for n in self.nodes}
        if any(e.account_ref != self.account_ref or e.source_id not in ids or e.target_id not in ids
               or e.properties.get("scope") == "conversation" for e in self.edges):
            raise ValueError("conversation_fragment_graph_invalid")
        sources = {m.message_ref: m for m in self.enrichments}
        for data in self.analyzer_entries:
            entry = CachedEnrichment.model_validate_json(data)
            source = sources.get(entry.key.message_ref)
            if source is None or entry.key.account_ref != self.account_ref or entry.key.conversation_ref != self.conversation_ref:
                raise ValueError("conversation_fragment_cache_scope_invalid")
            if entry.result() != getattr(source, entry.key.slot) or entry.key.expires_at < due:
                raise ValueError("conversation_fragment_cache_result_invalid")


class ConversationBuild:
    def __init__(self):
        self.entries: list[bytes] = []
        self.bytes_used = 0
        self.reused = 0
        self.recomputed = 0
        self.reader = None
        self.compact = False
        self.page_sets = []

    def retain_pages(self, packed) -> None:
        if (packed is None or len(self.entries) + len(self.page_sets) >= MAX_FRAGMENTS
                or self.bytes_used + packed.retained_bytes > MAX_FRAGMENT_TOTAL_BYTES):
            return
        if packed.generation_id is not None:
            from app.analytics.conversation_pages import ConversationPageReference
            self.page_sets.append(ConversationPageReference(packed.generation_id, packed.header))
        else:
            self.page_sets.append(packed)
        self.bytes_used += packed.retained_bytes

    def retain(self, fragment: ConversationFragment) -> None:
        if len(self.entries) + len(self.page_sets) >= MAX_FRAGMENTS:
            return
        data = fragment.model_dump_json().encode()
        if len(data) > MAX_FRAGMENT_BYTES or self.bytes_used + len(data) > MAX_FRAGMENT_TOTAL_BYTES:
            return
        self.entries.append(data)
        self.bytes_used += len(data)


ACTIVE_CONVERSATIONS: ContextVar[ConversationBuild | None] = ContextVar("conversation_build", default=None)


@contextmanager
def conversation_build(store, account_id, *, compact=False):
    state = ConversationBuild()
    state.compact = compact and getattr(store, "generation_references_supported", lambda: False)()
    opener = getattr(store, "open_conversation_fragments", None)
    with opener(account_id) if callable(opener) else nullcontext(None) as reader:
        state.reader = reader
        token = ACTIVE_CONVERSATIONS.set(state)
        try:
            yield state
        finally:
            ACTIVE_CONVERSATIONS.reset(token)


def assemble(pipeline, account_id, catalog, cutoff, cancellation_check):
    """Recompute changed conversations; rebuild shared topology from all live parts."""

    state, reuse = ACTIVE_CONVERSATIONS.get() or ConversationBuild(), ACTIVE_REUSE.get()
    config = fingerprint({"pipeline": pipeline.pipeline_config_digest,
        "metrics": CONVERSATION_METRICS_PROVENANCE.model_dump(mode="json"), "fragment": "v1"})
    compact = CompactGraph(account_ref(account_id)) if state.compact else None
    check = lambda: check_cancelled(cancellation_check)
    fragments, enrichments, metrics = [], [], []
    loader = state.reader
    for chat_id, input_digest in catalog.digests.items():
        check_cancelled(cancellation_check)
        ref = conversation_ref(account_id, chat_id)
        fragment = None
        local_graph = None
        packed, restored = None, None
        page_loader = getattr(loader, 'pages', None)
        if compact is not None and pipeline.reuse_conversations and reuse is not None and callable(page_loader):
            from app.analytics.conversation_pages import restore_pages
            candidate = page_loader(ref, input_digest, config, cancellation_check=cancellation_check)
            if candidate is not None and candidate.retained_bytes <= MAX_FRAGMENT_TOTAL_BYTES - state.bytes_used:
                h = candidate.header
                if h.retention_cutoff <= cutoff and h.expires_at > pipeline._retention_clock():
                    try:
                        restored = restore_pages(candidate, check)
                        packed = candidate
                    except (ValueError, TypeError, KeyError, RecursionError):
                        restored = None
        if restored is None and pipeline.reuse_conversations and reuse is not None and callable(loader):
            data = loader(ref, input_digest, config, cancellation_check=cancellation_check)
            if data is not None and len(data) <= MAX_FRAGMENT_BYTES:
                try:
                    value = ConversationFragment.model_validate_json(data)
                    value.validate_scope()
                    if (value.account_ref == account_ref(account_id) and value.conversation_ref == ref
                            and value.input_digest == input_digest and value.config_digest == config
                            and value.retention_cutoff <= cutoff
                            and value.expires_at > pipeline._retention_clock()):
                        fragment = value
                except (ValueError, TypeError):
                    pass
        if restored is not None:
            findings, counts, local_graph, cached = restored
            state.reused += 1
            for entry in cached:
                reuse.retain_record(entry)
        elif fragment is not None:
            state.reused += 1
            for data in fragment.analyzer_entries:
                item = CachedEnrichment.model_validate_json(data)
                reuse.retain_record(item)
        else:
            raw = catalog.conversation(chat_id)
            parts = pipeline._canonical_conversations(
                AccountReadModel(view_revision=catalog.view_revision, conversations={chat_id: raw}),
                cancellation_check=cancellation_check)
            if not parts:
                continue
            conversation = parts[0]
            state.recomputed += 1
            findings = pipeline.enrichment.enrich_conversation(account_id, conversation,
                cancellation_check=cancellation_check)
            counts = build_conversation_metrics(account_id, conversation, findings)
            local_graph = None
            if compact is not None:
                local_graph = CompactGraph(account_ref(account_id))
                for batch_nodes, batch_edges in pipeline.graph_projector.batches(account_id,
                        catalog.view_revision, [conversation], findings, [counts],
                        cancellation_check=cancellation_check):
                    local_graph.add(batch_nodes, batch_edges, check=check)
                nodes, edges = (local_graph.materialize() if len(findings) <= 256
                    and local_graph.encoded_bytes <= MAX_FRAGMENT_BYTES // 2 else (None, None))
            else:
                nodes, edges, _ = pipeline.graph_projector.project(account_id, catalog.view_revision,
                    [conversation], findings, [counts], cancellation_check=cancellation_check)
            entries = () if reuse is None else tuple(
                data.decode() for data in reuse.conversation_entries(ref))
            if nodes is not None:
                fragment = ConversationFragment(account_ref=account_ref(account_id), conversation_ref=ref,
                    input_digest=input_digest, config_digest=config, retention_cutoff=cutoff,
                    expires_at=min(m.sent_at for m in findings) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
                    enrichments=findings, metrics=counts, nodes=nodes, edges=edges, analyzer_entries=entries)
        if compact is not None:
            if local_graph is not None:
                compact.merge(local_graph, check=check)
            else:
                compact.add(fragment.nodes, fragment.edges, check=check)
                findings, counts = fragment.enrichments, fragment.metrics
            enrichments.extend(findings)
            metrics.append(counts)
        else:
            fragments.append(fragment)
            enrichments.extend(fragment.enrichments)
            metrics.append(fragment.metrics)
        if compact is not None and pipeline.reuse_conversations and reuse is not None and callable(page_loader):
            if packed is None and fragment is None and local_graph is not None:
                from app.analytics.conversation_pages import create_pages
                packed = create_pages(account=account_ref(account_id), conversation=ref,
                    input_digest=input_digest, config_digest=config, cutoff=cutoff,
                    findings=findings, metrics=counts, graph=local_graph,
                    analyzer_entries=reuse.conversation_entries(ref),
                    max_bytes=MAX_FRAGMENT_TOTAL_BYTES - state.bytes_used, check=check)
            state.retain_pages(packed)
        if fragment is not None and pipeline.reuse_conversations and reuse is not None:
            state.retain(fragment)
        check()
    if compact is not None:
        timeline = {}
        pipeline.graph_projector._conversation_edges(timeline, account_ref(account_id), metrics, cancellation_check)
        compact.add([], timeline.values(), check=check)
        if not compact.nodes:
            nodes, edges, _ = pipeline.graph_projector.project(account_id, catalog.view_revision, [], [], [])
            compact.add(nodes, edges, check=check)
        return enrichments, metrics, compact, None, compact.summary(catalog.view_revision)
    nodes, edges, summary = pipeline.graph_projector.compose(account_id, catalog.view_revision,
        fragments, metrics, cancellation_check=cancellation_check)
    return enrichments, metrics, nodes, edges, summary


def validate_fragments(artifact, entries: tuple[bytes, ...]):
    return list(iter_validated_fragments(artifact, entries))


def iter_validated_fragments(artifact, entries: tuple[bytes, ...]):
    if len(entries) > MAX_FRAGMENTS or sum(map(len, entries)) > MAX_FRAGMENT_TOTAL_BYTES:
        raise ValueError("conversation_fragment_budget_invalid")
    messages = {m.message_ref: m for m in artifact.projection.message_enrichments}
    metrics = {m.conversation_ref: m for m in artifact.projection.conversation_metrics}
    compact = isinstance(artifact, CompactArtifact)
    nodes = artifact.graph.nodes if compact else {n.node_id: n for n in artifact.nodes}
    edges = artifact.graph.edges if compact else {e.edge_id: e for e in artifact.edges}
    comparable = (lambda record: _json(record.model_dump(mode="json"))) if compact else (lambda record: record)
    seen = set()
    for data in entries:
        if len(data) > MAX_FRAGMENT_BYTES:
            raise ValueError("conversation_fragment_size_invalid")
        item = ConversationFragment.model_validate_json(data)
        item.validate_scope()
        if item.account_ref != artifact.projection.account_ref or item.conversation_ref in seen:
            raise ValueError("conversation_fragment_account_invalid")
        if (metrics.get(item.conversation_ref) != item.metrics
                or any(messages.get(m.message_ref) != m for m in item.enrichments)
                or len(item.enrichments) != item.metrics.message_count
                or any(nodes.get(n.node_id) != comparable(n) for n in item.nodes)
                or any(edges.get(e.edge_id) != comparable(e) for e in item.edges)):
            raise ValueError("conversation_fragment_output_invalid")
        seen.add(item.conversation_ref)
        yield item
