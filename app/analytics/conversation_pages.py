"""Bounded pages of exact conversation outputs owned by one generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import zlib
from typing import Literal

from pydantic import AwareDatetime, Field

from app.analytics.compact_graph import CompactGraph, CompactArtifact, _json
from app.analytics.enrichment_cache import CacheRecord, CachedEnrichment
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.models.analytics import (AccountRef, ConversationRef, Sha256Digest,
    ConversationMetrics, MessageEnrichment, GraphNode, GraphEdge)

PAGE_BYTES = 256 * 1024
PAGE_RECORDS = 256
MAX_PAGES = 4096


class ConversationPageHeader(CacheRecord):
    format: Literal['conversation-pages.v1'] = 'conversation-pages.v1'
    encoding: Literal['zlib-json.v1'] = 'zlib-json.v1'
    account_ref: AccountRef
    conversation_ref: ConversationRef
    input_digest: Sha256Digest
    config_digest: Sha256Digest
    retention_cutoff: AwareDatetime
    expires_at: AwareDatetime
    metrics: ConversationMetrics
    page_count: int = Field(ge=1, le=MAX_PAGES)
    byte_count: int = Field(ge=1, le=64 * 1024 * 1024)
    pages_digest: Sha256Digest
    node_count: int = Field(ge=1)
    edge_count: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class ConversationPage:
    kind: str
    data: bytes


@dataclass(frozen=True, slots=True)
class PagedConversation:
    header: ConversationPageHeader
    pages: tuple[ConversationPage, ...]

    @property
    def retained_bytes(self) -> int:
        return len(self.header.model_dump_json().encode()) + sum(len(p.data) for p in self.pages)


def page_digest(pages) -> str:
    digest = hashlib.sha256(b'conversation-pages.v1\0')
    for page in pages:
        digest.update(page.kind.encode() + b'\0' + len(page.data).to_bytes(8, 'big') + page.data)
    return 'sha256:' + digest.hexdigest()


def create_pages(*, account, conversation, input_digest, config_digest, cutoff,
                 findings, metrics, graph, analyzer_entries, max_bytes, check):
    """Stop retaining pages at the shared byte limit; analysis itself still succeeds."""

    pages, used = [], 0
    sources = (('message', (m.model_dump_json() for m in findings)),
        ('node', graph.nodes.values()), ('edge', graph.edges.values()),
        ('analyzer', (data.decode('utf-8') for data in analyzer_entries)))
    for kind, records in sources:
        batch, size = [], 2
        for record in records:
            check()
            raw = record.encode('utf-8')
            if len(raw) + 2 > PAGE_BYTES:
                return None
            if batch and (len(batch) == PAGE_RECORDS or size + len(raw) + 1 > PAGE_BYTES):
                page = ConversationPage(kind, zlib.compress(b'[' + b','.join(batch) + b']', 1))
                pages.append(page); used += len(page.data)
                batch, size = [], 2
            if used > max_bytes or len(pages) >= MAX_PAGES:
                return None
            batch.append(raw); size += len(raw) + 1
        if batch:
            page = ConversationPage(kind, zlib.compress(b'[' + b','.join(batch) + b']', 1))
            pages.append(page); used += len(page.data)
    if (not pages or len(pages) > MAX_PAGES or used > max_bytes
            or any(len(page.data) > PAGE_BYTES for page in pages)):
        return None
    header = ConversationPageHeader(account_ref=account, conversation_ref=conversation,
        input_digest=input_digest, config_digest=config_digest, retention_cutoff=cutoff,
        expires_at=min(m.sent_at for m in findings) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
        metrics=metrics, page_count=len(pages), byte_count=used, pages_digest=page_digest(pages),
        node_count=len(graph.nodes), edge_count=len(graph.edges))
    result = PagedConversation(header, tuple(pages))
    return result if result.retained_bytes <= max_bytes else None


def unpack_page(data: bytes) -> bytes:
    """Reject truncated streams, trailing data and oversized decompression."""

    if not isinstance(data, bytes) or not data or len(data) > PAGE_BYTES:
        raise ValueError('conversation_page_size_invalid')
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(data, PAGE_BYTES + 1)
    except zlib.error as error:
        raise ValueError('conversation_page_encoding_invalid') from error
    if len(raw) > PAGE_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('conversation_page_expansion_invalid')
    return raw


def records(packed: PagedConversation, check):
    header = packed.header
    if (len(packed.pages) != header.page_count
            or sum(len(p.data) for p in packed.pages) != header.byte_count
            or page_digest(packed.pages) != header.pages_digest):
        raise ValueError('conversation_pages_digest_invalid')
    rank, previous = {'message': 0, 'node': 1, 'edge': 2, 'analyzer': 3}, -1
    for page in packed.pages:
        check()
        if page.kind not in rank or rank[page.kind] < previous or len(page.data) > PAGE_BYTES:
            raise ValueError('conversation_page_shape_invalid')
        previous = rank[page.kind]
        values = json.loads(unpack_page(page.data))
        if not isinstance(values, list) or not 1 <= len(values) <= PAGE_RECORDS:
            raise ValueError('conversation_page_count_invalid')
        for value in values:
            check()
            if not isinstance(value, dict):
                raise ValueError('conversation_page_record_invalid')
            yield page.kind, value


def validate_message(message, header):
    if (message.account_ref != header.account_ref
            or message.conversation_ref != header.conversation_ref
            or message.participant_ref != header.metrics.participant_ref
            or message.sent_at <= header.retention_cutoff):
        raise ValueError('conversation_page_source_invalid')


def validate_analyzer(entry, source, header):
    due = source.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS) if source else None
    if (source is None or entry.key.account_ref != header.account_ref
            or entry.key.conversation_ref != header.conversation_ref
            or not header.expires_at <= entry.key.expires_at <= due
            or entry.result() != getattr(source, entry.key.slot)):
        raise ValueError('conversation_page_analyzer_invalid')


def validate_summary(header, findings, node_count, edge_count):
    if (header.metrics.account_ref != header.account_ref
            or header.metrics.conversation_ref != header.conversation_ref
            or not findings or len(findings) != header.metrics.message_count
            or len({m.message_ref for m in findings}) != len(findings)
            or node_count != header.node_count or edge_count != header.edge_count
            or header.expires_at != min(m.sent_at for m in findings)
                + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)):
        raise ValueError('conversation_pages_summary_invalid')
    if [(m.sent_at, m.source_ordinal) for m in findings] != sorted((m.sent_at, m.source_ordinal) for m in findings):
        raise ValueError('conversation_pages_order_invalid')


def restore_pages(packed: PagedConversation, check):
    """Validate a bounded page at a time before contributing any cached output."""

    header = packed.header
    graph, findings, entries = CompactGraph(header.account_ref), [], []
    sources, cache_keys = {}, set()
    for kind, value in records(packed, check):
        if kind == 'message':
            item = MessageEnrichment.model_validate(value)
            validate_message(item, header)
            findings.append(item); sources[item.message_ref] = item
        elif kind in ('node', 'edge'):
            model = GraphNode if kind == 'node' else GraphEdge
            item = model.model_validate(value)
            key = item.node_id if kind == 'node' else item.edge_id
            target = graph.nodes if kind == 'node' else graph.edges
            if key in target or (kind == 'edge' and item.properties.get('scope') == 'conversation'):
                raise ValueError('conversation_page_graph_invalid')
            graph.add([item] if kind == 'node' else [], [item] if kind == 'edge' else [], check=check)
        else:
            item = CachedEnrichment.model_validate(value)
            validate_analyzer(item, sources.get(item.key.message_ref), header)
            signature = item.key.digest
            if signature in cache_keys:
                raise ValueError('conversation_page_duplicate_analyzer')
            cache_keys.add(signature); entries.append(item)
    validate_summary(header, findings, len(graph.nodes), len(graph.edges))
    for data in graph.edges.values():
        check(); edge = json.loads(data)
        if edge['source_id'] not in graph.nodes or edge['target_id'] not in graph.nodes:
            raise ValueError('conversation_page_endpoint_invalid')
    return findings, header.metrics, graph, entries


def validate_page_sets(artifact, page_sets, *, check=lambda: None):
    """Bind cache pages to the artifact being staged, not just to their checksums."""

    if not page_sets:
        return
    if not isinstance(artifact, CompactArtifact):
        raise ValueError('conversation_pages_require_compact_artifact')
    sources = {m.message_ref: m for m in artifact.projection.message_enrichments}
    metrics = {m.conversation_ref: m for m in artifact.projection.conversation_metrics}
    seen = set()
    for packed in page_sets:
        header = packed.header
        if (header.account_ref != artifact.projection.account_ref or header.conversation_ref in seen
                or metrics.get(header.conversation_ref) != header.metrics):
            raise ValueError('conversation_pages_artifact_invalid')
        seen.add(header.conversation_ref)
        findings, nodes, edges, cache_keys = [], set(), set(), set()
        for kind, value in records(packed, check):
            if kind == 'message':
                item = MessageEnrichment.model_validate(value)
                validate_message(item, header)
                if sources.get(item.message_ref) != item:
                    raise ValueError('conversation_pages_result_invalid')
                findings.append(item)
            elif kind in ('node', 'edge'):
                key = value.get(kind + '_id')
                target, keys = (artifact.graph.nodes, nodes) if kind == 'node' else (artifact.graph.edges, edges)
                if key in keys or target.get(key) != _json(value):
                    raise ValueError('conversation_pages_graph_invalid')
                keys.add(key)
            else:
                item = CachedEnrichment.model_validate(value)
                validate_analyzer(item, sources.get(item.key.message_ref), header)
                signature = item.key.digest
                if signature in cache_keys:
                    raise ValueError('conversation_page_duplicate_analyzer')
                cache_keys.add(signature)
        validate_summary(header, findings, len(nodes), len(edges))
        for page in packed.pages:
            if page.kind != 'edge':
                continue
            for value in json.loads(unpack_page(page.data)):
                check()
                if (value['properties'].get('scope') == 'conversation'
                        or value['source_id'] not in nodes or value['target_id'] not in nodes):
                    raise ValueError('conversation_page_endpoint_invalid')
