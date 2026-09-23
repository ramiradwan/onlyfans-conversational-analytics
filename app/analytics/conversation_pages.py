"""Bounded pages of exact conversation outputs owned by one generation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
import hashlib
import hmac
import json
import secrets
import zlib
from typing import Callable, Literal

from pydantic import AwareDatetime, Field, model_validator

from app.analytics.compact_graph import CompactGraph, CompactArtifact, _json
from app.analytics.enrichment_cache import CacheRecord, CachedEnrichment
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.models.analytics import (AccountRef, ConversationRef, Sha256Digest,
    ConversationMetrics, MessageEnrichment, GraphNode, GraphEdge)

from app.analytics.graph_row_encoding import EncodedGraphRecord


PAGE_BYTES = 256 * 1024
PAGE_RECORDS = 256
MAX_PAGES = 4096
GRAPH_REFERENCE_ENCODING = "zlib-json-graph-ids.v2"
_PAGE_RECEIPT_KEY = secrets.token_bytes(32)


class ConversationPageHeader(CacheRecord):
    format: Literal['conversation-pages.v1'] = 'conversation-pages.v1'
    encoding: Literal['zlib-json.v1', 'zlib-json-graph-ids.v2'] = 'zlib-json.v1'
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
    graph_digest: Sha256Digest | None = None

    @model_validator(mode='after')
    def validate_graph_binding(self) -> ConversationPageHeader:
        if (self.encoding == GRAPH_REFERENCE_ENCODING) != (self.graph_digest is not None):
            raise ValueError('conversation_page_graph_binding_invalid')
        return self


@dataclass(frozen=True, slots=True)
class ConversationPage:
    kind: str
    data: bytes


@dataclass(frozen=True, slots=True)
class GraphPageReceipt:
    """One build's checked page set, bound to the exact tracked storage state."""

    generation_id: str
    header: ConversationPageHeader
    stamp: tuple
    proof: str = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PagedConversation:
    header: ConversationPageHeader
    pages: tuple[ConversationPage, ...]
    generation_id: str | None = None
    # This callback is valid only while its source connection remains open.
    graph_records: Callable[[str, list[str], Callable[[], None]], list[dict]] | None = field(
        default=None, repr=False, compare=False)
    graph_read_stamp: tuple | None = field(default=None, repr=False, compare=False)
    graph_stamp_reader: Callable[[], tuple | None] | None = field(default=None, repr=False, compare=False)
    graph_receipt: GraphPageReceipt | None = field(default=None, repr=False, compare=False)
    graph_encoded_records: Callable[[str, list[str], Callable[[], None]], list[EncodedGraphRecord]] | None = field(
        default=None, repr=False, compare=False)

    @property
    def retained_bytes(self) -> int:
        return len(self.header.model_dump_json().encode()) + sum(len(p.data) for p in self.pages)


@dataclass(frozen=True, slots=True)
class ConversationPageReference:
    """Carry a witnessed cache identity without retaining its compressed pages."""

    generation_id: str
    header: ConversationPageHeader
    graph_receipt: GraphPageReceipt | None = field(default=None, repr=False, compare=False)

    @property
    def retained_bytes(self) -> int:
        # Count the complete payload against the unchanged cache budget.
        return len(self.header.model_dump_json().encode()) + self.header.byte_count


@dataclass(frozen=True, slots=True)
class VerifiedPageReference:
    """A same-build page set checked under one unchanged tracked storage stamp."""

    generation_id: str
    header: ConversationPageHeader


def _page_receipt_proof(generation_id: str, header: ConversationPageHeader,
                        stamp: tuple) -> str:
    payload = json.dumps(
        {
            "generation_id": generation_id,
            "header": header.model_dump(mode="json"),
            "stamp": list(stamp),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hmac.new(_PAGE_RECEIPT_KEY, payload, hashlib.sha256).hexdigest()


def verify_page_receipt(receipt, generation_id: str, header: ConversationPageHeader,
                        stamp: tuple) -> bool:
    """Accept only the exact same-process proof created after a complete restore."""

    if (not isinstance(receipt, GraphPageReceipt)
            or not isinstance(receipt.proof, str) or len(receipt.proof) != 64
            or receipt.generation_id != generation_id
            or receipt.header != header
            or receipt.stamp != stamp):
        return False
    expected = _page_receipt_proof(generation_id, header, stamp)
    return hmac.compare_digest(receipt.proof, expected)


def record_verified_graph_read(packed: PagedConversation) -> PagedConversation:
    """Call only after restore_pages has checked every page and graph digest."""

    if (packed.generation_id is None or packed.header.graph_digest is None
            or packed.graph_read_stamp is None or packed.graph_stamp_reader is None
            or packed.graph_stamp_reader() != packed.graph_read_stamp):
        return packed
    proof = _page_receipt_proof(
        packed.generation_id, packed.header, packed.graph_read_stamp
    )
    return replace(packed, graph_receipt=GraphPageReceipt(
        packed.generation_id, packed.header, packed.graph_read_stamp, proof))


def trusted_page_reference(
    generation_id: str, header: ConversationPageHeader, stamp: tuple
) -> ConversationPageReference:
    """Carry a page set proven by the exact same validated generation stamp."""

    proof = _page_receipt_proof(generation_id, header, stamp)
    return ConversationPageReference(
        generation_id, header,
        GraphPageReceipt(generation_id, header, stamp, proof),
    )


def page_digest(pages) -> str:
    digest = hashlib.sha256(b'conversation-pages.v1\0')
    for page in pages:
        digest.update(page.kind.encode() + b'\0' + len(page.data).to_bytes(8, 'big') + page.data)
    return 'sha256:' + digest.hexdigest()


def create_pages(*, account, conversation, input_digest, config_digest, cutoff,
                 findings, metrics, graph, analyzer_entries, max_bytes, check, graph_references=False):
    """Stop retaining pages at the shared byte limit; analysis itself still succeeds."""

    pages, used = [], 0
    nodes = (json.dumps(key) for key in graph.nodes) if graph_references else graph.nodes.values()
    edges = (json.dumps(key) for key in graph.edges) if graph_references else graph.edges.values()
    sources = (('message', (m.model_dump_json() for m in findings)),
        ('node', nodes), ('edge', edges),
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
    header = ConversationPageHeader(encoding=GRAPH_REFERENCE_ENCODING if graph_references else "zlib-json.v1",
        account_ref=account, conversation_ref=conversation,
        input_digest=input_digest, config_digest=config_digest, retention_cutoff=cutoff,
        expires_at=min(m.sent_at for m in findings) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS),
        metrics=metrics, page_count=len(pages), byte_count=used, pages_digest=page_digest(pages),
        node_count=len(graph.nodes), edge_count=len(graph.edges),
        graph_digest=graph.digest(check=check) if graph_references else None)
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


def records(packed: PagedConversation, check, *, graph=None, encoded_graph=False):
    header = packed.header
    header.validate_graph_binding()
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
        if header.encoding == GRAPH_REFERENCE_ENCODING and page.kind in ('node', 'edge'):
            from app.analytics.graph_identity import require_graph_id

            for key in values:
                check()
                require_graph_id(key, expected_kind='edge' if page.kind == 'edge' else None)
            if packed.generation_id is not None:
                reader = packed.graph_encoded_records if encoded_graph else None
                reader = reader or packed.graph_records
                if reader is None:
                    raise ValueError('conversation_page_graph_reader_unavailable')
                values = reader(page.kind, values, check)
            elif graph is not None:
                try:
                    resolver = getattr(graph, 'resolve_records', None)
                    if callable(resolver):
                        values = resolver(page.kind, values, check)
                    else:
                        target = graph.nodes if page.kind == 'node' else graph.edges
                        values = [json.loads(target[key]) for key in values]
                except KeyError as error:
                    raise ValueError('conversation_page_graph_reference_absent') from error
            else:
                raise ValueError('conversation_page_graph_reader_unavailable')
        for value in values:
            check()
            if isinstance(value, EncodedGraphRecord):
                if not encoded_graph or value.kind != page.kind:
                    raise ValueError('conversation_page_record_invalid')
            elif not isinstance(value, dict):
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


def restore_pages_with_graph_unit(packed: PagedConversation, unit, check):
    """Restore scalar outputs while checking graph IDs against a trusted unit."""

    from app.analytics.conversation_graph_units import graph_unit_ids

    header = packed.header
    unit_header = unit.header
    if (header.account_ref != unit_header.account_ref
            or header.conversation_ref != unit_header.conversation_ref
            or header.input_digest != unit_header.input_digest
            or header.config_digest != unit_header.config_digest
            or header.graph_digest != unit_header.graph_digest
            or header.node_count != unit_header.node_count
            or header.edge_count != unit_header.edge_count):
        raise ValueError('conversation_graph_unit_header_invalid')
    expected_nodes, expected_edges = graph_unit_ids(unit)
    findings, entries, node_ids, edge_ids = [], [], [], []
    sources, cache_keys = {}, set()
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
        if page.kind == 'message':
            for value in values:
                check()
                item = MessageEnrichment.model_validate(value)
                validate_message(item, header)
                findings.append(item); sources[item.message_ref] = item
        elif page.kind in ('node', 'edge'):
            if header.encoding != GRAPH_REFERENCE_ENCODING:
                raise ValueError('conversation_graph_unit_encoding_invalid')
            from app.analytics.graph_identity import require_graph_id
            target = node_ids if page.kind == 'node' else edge_ids
            for key in values:
                check()
                require_graph_id(key, expected_kind='edge' if page.kind == 'edge' else None)
                target.append(key)
        else:
            for value in values:
                check()
                item = CachedEnrichment.model_validate(value)
                validate_analyzer(item, sources.get(item.key.message_ref), header)
                signature = item.key.digest
                if signature in cache_keys:
                    raise ValueError('conversation_page_duplicate_analyzer')
                cache_keys.add(signature); entries.append(item)
    if tuple(sorted(node_ids)) != expected_nodes or tuple(sorted(edge_ids)) != expected_edges:
        raise ValueError('conversation_graph_unit_membership_invalid')
    validate_summary(header, findings, len(node_ids), len(edge_ids))
    return findings, header.metrics, entries


def restore_pages(packed: PagedConversation, check):
    """Validate a bounded page at a time before contributing any cached output."""

    header = packed.header
    graph, findings, entries = CompactGraph(header.account_ref), [], []
    sources, cache_keys = {}, set()
    for kind, value in records(packed, check, encoded_graph=True):
        if kind == 'message':
            item = MessageEnrichment.model_validate(value)
            validate_message(item, header)
            findings.append(item); sources[item.message_ref] = item
        elif isinstance(value, EncodedGraphRecord):
            # The storage reader already validated columns and actual content bytes.
            # Keep those exact bytes rather than decode, model and encode them again.
            target = graph.nodes if kind == 'node' else graph.edges
            counts = graph.node_counts if kind == 'node' else graph.edge_counts
            if value.account_ref != header.account_ref or value.key in target or value.conversation_edge:
                raise ValueError('conversation_page_graph_invalid')
            if kind == 'edge' and (value.source_id not in graph.nodes or value.target_id not in graph.nodes):
                raise ValueError('conversation_page_endpoint_invalid')
            target[value.key] = value.data
            counts[value.category] += 1
            graph.encoded_bytes += len(value.data.encode('utf-8'))
        elif kind in ('node', 'edge'):
            model = GraphNode if kind == 'node' else GraphEdge
            item = model.model_validate(value)
            key = item.node_id if kind == 'node' else item.edge_id
            target = graph.nodes if kind == 'node' else graph.edges
            if key in target or (kind == 'edge' and item.properties.get('scope') == 'conversation'):
                raise ValueError('conversation_page_graph_invalid')
            if kind == 'edge' and (item.source_id not in graph.nodes or item.target_id not in graph.nodes):
                raise ValueError('conversation_page_endpoint_invalid')
            graph.add([item] if kind == 'node' else [], [item] if kind == 'edge' else [], check=check)
        else:
            item = CachedEnrichment.model_validate(value)
            validate_analyzer(item, sources.get(item.key.message_ref), header)
            signature = item.key.digest
            if signature in cache_keys:
                raise ValueError('conversation_page_duplicate_analyzer')
            cache_keys.add(signature); entries.append(item)
    validate_summary(header, findings, len(graph.nodes), len(graph.edges))
    if header.graph_digest is not None and graph.digest(check=check) != header.graph_digest:
        raise ValueError('conversation_page_graph_digest_invalid')
    return findings, header.metrics, graph, entries


def checked_page_sets(artifact, page_sets, *, check=lambda: None):
    """Bind cache pages to the artifact being staged, not just to their checksums."""

    sources, metrics, seen = None, None, set()
    for packed in page_sets:
        if metrics is None:
            if not isinstance(artifact, CompactArtifact):
                raise ValueError('conversation_pages_require_compact_artifact')
            metrics = {m.conversation_ref: m for m in artifact.projection.conversation_metrics}
        header = packed.header
        if (header.account_ref != artifact.projection.account_ref or header.conversation_ref in seen
                or metrics.get(header.conversation_ref) != header.metrics):
            raise ValueError('conversation_pages_artifact_invalid')
        seen.add(header.conversation_ref)
        if isinstance(packed, VerifiedPageReference):
            yield packed
            continue
        if sources is None:
            values = artifact.projection.message_enrichments
            local_messages = getattr(values, "validation_messages", None)
            sources = (
                local_messages()
                if callable(local_messages)
                else {m.message_ref: m for m in values}
            )
        findings, nodes, edges, cache_keys = [], set(), set(), set()
        for kind, value in records(packed, check, graph=artifact.graph):
            if kind == 'message':
                item = MessageEnrichment.model_validate(value)
                validate_message(item, header)
                if sources.get(item.message_ref) != item:
                    raise ValueError('conversation_pages_result_invalid')
                findings.append(item)
            elif kind in ('node', 'edge'):
                key = value.get(kind + '_id')
                target, keys = (artifact.graph.nodes, nodes) if kind == 'node' else (artifact.graph.edges, edges)
                matcher = getattr(artifact.graph, 'page_record_matches', None)
                matches = (
                    matcher(kind, key, value)
                    if callable(matcher) else target.get(key) == _json(value)
                )
                if key in keys or not matches:
                    raise ValueError('conversation_pages_graph_invalid')
                if kind == 'edge' and (value['properties'].get('scope') == 'conversation'
                        or value['source_id'] not in nodes or value['target_id'] not in nodes):
                    raise ValueError('conversation_page_endpoint_invalid')
                keys.add(key)
            else:
                item = CachedEnrichment.model_validate(value)
                validate_analyzer(item, sources.get(item.key.message_ref), header)
                signature = item.key.digest
                if signature in cache_keys:
                    raise ValueError('conversation_page_duplicate_analyzer')
                cache_keys.add(signature)
        validate_summary(header, findings, len(nodes), len(edges))
        if (header.graph_digest is not None
                and artifact.graph.digest(check=check, node_ids=nodes, edge_ids=edges) != header.graph_digest):
            raise ValueError('conversation_page_graph_digest_invalid')
        yield packed


def validate_page_sets(artifact, page_sets, *, check=lambda: None):
    """Validate callers that do not consume one checked set at a time."""

    for _ in checked_page_sets(artifact, page_sets, check=check):
        pass
