"""Reuse immutable graph records through generation-owned segment manifests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
from heapq import merge
from itertools import groupby, islice
import json
import time
from uuid import uuid4

from app.analytics.compact_graph import CompactGraph, _json


MAX_SEGMENT_CHUNK_BYTES = 64 * 1024 * 1024
GRAPH_SEGMENT_ROOT_PIPELINE_REVISION = "graph.segment-root.v1"
_GRAPH_SEGMENT_ROOT_DOMAIN = b"analytics-graph.segment-root.v1\0"


def _segment_fields(item):
    if isinstance(item, tuple):
        if len(item) != 4:
            raise ValueError("graph_segment_root_item_invalid")
        return item
    count = getattr(item, "count", None)
    if count is None:
        keys = getattr(item, "keys", None)
        if keys is None:
            raise ValueError("graph_segment_root_count_missing")
        count = len(keys)
    return item.kind, item.bucket, item.digest, int(count)


def segment_root_digest(segments, *, check=lambda: None) -> str:
    """Compose a graph identity from exact deterministic segment identities."""

    values = sorted(
        (_segment_fields(item) for item in segments),
        key=lambda item: (item[0] != "edge", item[1]),
    )
    seen = set()
    digest = hashlib.sha256(_GRAPH_SEGMENT_ROOT_DOMAIN)
    for kind, bucket, content_digest, count in values:
        check()
        if kind not in ("node", "edge") or len(bucket) != 2 or count < 0:
            raise ValueError("graph_segment_root_item_invalid")
        key = (kind, bucket)
        if key in seen:
            raise ValueError("graph_segment_root_duplicate")
        seen.add(key)
        digest.update(b"\0")
        digest.update(kind.encode("ascii"))
        digest.update(b":")
        digest.update(bucket.encode("ascii"))
        digest.update(b":")
        digest.update(str(count).encode("ascii"))
        digest.update(b":")
        digest.update(content_digest.encode("ascii"))
    check()
    return "sha256:" + digest.hexdigest()


def _segment_digest(kind: str, bucket: str, records, keys, check) -> str:
    digest = hashlib.sha256(("graph-segment.v1:" + kind + ":" + bucket).encode())
    for key in keys:
        check()
        version = hashlib.sha256(records[key].encode("utf-8")).hexdigest()
        digest.update(key.encode() + b":" + version.encode() + b"\n")
    return digest.hexdigest()


def compact_segment_root(graph: CompactGraph, *, check=lambda: None) -> str:
    parts = []
    for kind, records in (("node", graph.nodes), ("edge", graph.edges)):
        for bucket, grouped in groupby(sorted(records), key=lambda key: key[3:5]):
            keys = list(grouped)
            parts.append((kind, bucket, _segment_digest(kind, bucket, records, keys, check), len(keys)))
    return segment_root_digest(parts, check=check)


def materialized_segment_root(nodes, edges, *, check=lambda: None) -> str:
    from app.analytics.graph_privacy import safe_graph_edge, safe_graph_node

    values = {"node": {}, "edge": {}}
    for kind, records, validator, key_name in (
        ("node", nodes, safe_graph_node, "node_id"),
        ("edge", edges, safe_graph_edge, "edge_id"),
    ):
        for item in records:
            check()
            item = validator(item)
            key = getattr(item, key_name)
            data = _json(item.model_dump(mode="json"))
            previous = values[kind].get(key)
            if previous is not None and previous != data:
                raise ValueError("graph_record_identity_collision")
            values[kind][key] = data
    parts = []
    for kind in ("node", "edge"):
        records = values[kind]
        for bucket, grouped in groupby(sorted(records), key=lambda key: key[3:5]):
            keys = list(grouped)
            parts.append((kind, bucket, _segment_digest(kind, bucket, records, keys, check), len(keys)))
    return segment_root_digest(parts, check=check)


def projection_graph_digest(pipeline_revision: str, graph, edges=None, *, check=lambda: None) -> str:
    """Use legacy canonical bytes unless the pipeline explicitly opts into segment roots."""

    if GRAPH_SEGMENT_ROOT_PIPELINE_REVISION in pipeline_revision:
        segments = getattr(graph, "segments", None)
        if segments is not None:
            return segment_root_digest(segments, check=check)
        if isinstance(graph, CompactGraph):
            return compact_segment_root(graph, check=check)
        return materialized_segment_root(graph, edges or (), check=check)
    if isinstance(graph, CompactGraph) or getattr(graph, "compact_graph", False):
        return graph.digest(check=check)
    from app.analytics.graph_privacy import graph_content_digest
    return graph_content_digest(graph, edges or (), check=check)


@dataclass(frozen=True, slots=True)
class SegmentPlan:
    kind: str
    bucket: str
    digest: str
    segment_id: str
    keys: list[str]
    reused: bool


@dataclass(frozen=True, slots=True)
class VerifiedSegment:
    kind: str
    bucket: str
    segment_id: str
    digest: str
    count: int
    categories: tuple[tuple[str, int], ...]
    chunk_digest: str | None = None


@dataclass(frozen=True, slots=True)
class GraphSegmentProof:
    generation_id: str
    binding: str
    stamp_prefix: tuple
    segments: tuple[VerifiedSegment, ...]


@dataclass(frozen=True, slots=True)
class SegmentValidation:
    kind: str
    bucket: str
    segment_id: str
    digest: str
    count: int
    reused: bool


@dataclass(frozen=True, slots=True)
class SharedGraphValidation:
    graph_digest: str
    plans: tuple[SegmentValidation, ...]
    proof: GraphSegmentProof | None
    removed_nodes: tuple[str, ...] | None = None
    segment_root: str | None = None


class PredecessorSegments(dict):
    def __init__(self, generation_id: str, values: dict, proof=None):
        super().__init__(values)
        self.generation_id = generation_id
        self.proof = proof


def supported(connection) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_graph_segments'").fetchone() is not None


def chunks_supported(connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_segment_chunks'"
    ).fetchone() is not None


def _predecessor_segments(store, connection, partition: str) -> dict:
    generation = connection.execute("SELECT * FROM projection_generations WHERE creator_account_id=? AND status='active'", (partition,)).fetchone()
    if generation is None:
        return {}
    witness = store.activation.get(generation['generation_id'])
    if not store._intent_matches(generation, witness, require_completed=True):
        return {}
    values = {(row['kind'], row['bucket']): (row['content_digest'], row['segment_id'])
        for row in connection.execute("""SELECT s.* FROM generation_graph_segments m
          JOIN graph_segments s USING(creator_account_id,segment_id)
          WHERE m.generation_id=? AND m.creator_account_id=? AND s.sealed=1""",
          (generation['generation_id'], partition))}
    proof_reader = getattr(store, '_trusted_graph_segment_proof', None)
    proof = proof_reader(connection, generation) if callable(proof_reader) else None
    return PredecessorSegments(generation['generation_id'], values, proof)


def _plans(graph: CompactGraph, existing: dict,
           check: Callable[[], None]) -> Iterator[SegmentPlan]:
    for kind, records in (('node', graph.nodes), ('edge', graph.edges)):
        for bucket, grouped in groupby(sorted(records), key=lambda key: key[3:5]):
            keys = list(grouped)
            signature = _segment_digest(kind, bucket, records, keys, check)
            previous = existing.get((kind, bucket))
            reused = previous is not None and previous[0] == signature
            yield SegmentPlan(kind, bucket, signature, previous[1] if reused else str(uuid4()), keys, reused)


def _segment_chunk(graph, plan, check):
    records = graph.nodes if plan.kind == 'node' else graph.edges
    parts, used = [], 0
    for key in plan.keys:
        check()
        data = records[key].encode('utf-8')
        used += len(data) + (1 if parts else 0)
        if used > MAX_SEGMENT_CHUNK_BYTES:
            return None
        parts.append(data)
    if not parts:
        return None
    chunk = b','.join(parts)
    return chunk, hashlib.sha256(chunk).hexdigest()


def _ensure_segment_chunks(connection, graph, plans, check):
    if not chunks_supported(connection):
        return 0
    written = 0
    for plan in plans:
        check()
        value = _segment_chunk(graph, plan, check)
        if value is None:
            continue
        chunk, digest = value
        row = connection.execute(
            """SELECT kind,record_count,canonical_digest
               FROM graph_segment_chunks
               WHERE creator_account_id=? AND segment_id=?""",
            (graph.account_ref, plan.segment_id),
        ).fetchone()
        if row is not None:
            continue
        connection.execute(
            """INSERT INTO graph_segment_chunks
               (creator_account_id,segment_id,kind,record_count,canonical_bytes,canonical_digest)
               VALUES (?,?,?,?,?,?)""",
            (
                graph.account_ref, plan.segment_id, plan.kind, len(plan.keys),
                chunk, digest,
            ),
        )
        written += 1
    return written


def _records(graph, plan, check):
    from app.analytics.sqlite_graph_store import _timestamp

    records = graph.nodes if plan.kind == 'node' else graph.edges
    for key in plan.keys:
        check()
        data = records[key]
        row = json.loads(data)
        stamp = _timestamp(datetime.fromisoformat(row['occurred_at'])) if row['occurred_at'] else None
        properties = _json(row['properties'])
        content = hashlib.sha256(data.encode('utf-8')).hexdigest()
        if plan.kind == 'node':
            values = (graph.account_ref, content, key, row['kind'], stamp, properties)
        else:
            values = (graph.account_ref, content, key, row['source_id'], row['target_id'],
                      row['relation'], stamp, row['sequence'], properties)
        membership = (graph.account_ref, plan.segment_id, key, content)
        yield values, membership


def _content_order(graph, plans, kind, check, page_plans=None):
    """Merge bounded bucket iterators in physical content-key order."""

    records = graph.nodes if kind == 'node' else graph.edges
    def signature(key):
        check()
        return hashlib.sha256(records[key].encode('utf-8')).digest()
    streams = []
    for plan in plans:
        check()
        if plan.kind == kind and not plan.reused:
            keys = plan.keys
            if page_plans is not None:
                pages = page_plans[(kind, plan.segment_id)]
                keys = [key for key in keys if not pages[key[3:6]].reused]
            ordered = replace(plan, keys=sorted(keys, key=signature))
            streams.append(_records(graph, ordered, check))
    yield from merge(*streams, key=lambda item: item[0][1])


def _existing_ids(connection, table, field, account, identities, check):
    """Look up bounded parameter groups without running insert triggers on hits."""

    result = set()
    for offset in range(0, len(identities), 256):
        check()
        batch = identities[offset:offset + 256]
        placeholders = ','.join('?' for _ in batch)
        result.update(row[0] for row in connection.execute(
            f'SELECT {field} FROM {table} WHERE creator_account_id=? AND {field} IN ({placeholders})',
            (account, *batch)))
    return result


def write_shared_graph(writer, graph: CompactGraph, store, *, check=lambda: None,
                       graph_digest=None) -> dict[str, int]:
    """Write changed content only; verify the complete selected graph at publication."""

    from app.analytics.database import content_write_cache
    from app.analytics.graph_membership_pages import (
        prepare_pages, supported as pages_supported, write_members,
    )

    with writer.lease_session(), content_write_cache(
        writer._write_connection, len(graph.nodes) + len(graph.edges)
    ):
        connection = writer._write_connection
        existing = _predecessor_segments(store, connection, graph.account_ref)
        plans = list(_plans(graph, existing, check))
        with writer._owned_transaction() as db:
            for plan in plans:
                check()
                if not plan.reused:
                    db.execute('INSERT INTO graph_segments(creator_account_id,segment_id,kind,bucket,content_digest) VALUES (?,?,?,?,?)',
                        (graph.account_ref, plan.segment_id, plan.kind, plan.bucket, plan.digest))
                db.execute('INSERT INTO generation_graph_segments(generation_id,creator_account_id,kind,bucket,segment_id) VALUES (?,?,?,?,?)',
                    (writer._generation_id, graph.account_ref, plan.kind, plan.bucket, plan.segment_id))
        with writer._owned_transaction() as db:
            segment_chunks_written = _ensure_segment_chunks(db, graph, plans, check)
            page_plans = {} if pages_supported(db) else None
            if page_plans is not None:
                for plan in plans:
                    check()
                    if plan.reused:
                        continue
                    records = graph.nodes if plan.kind == 'node' else graph.edges
                    previous = existing.get((plan.kind, plan.bucket))
                    page_plans[(plan.kind, plan.segment_id)] = prepare_pages(
                        db, graph.account_ref, plan.segment_id, plan.kind,
                        records, plan.keys, None if previous is None else previous[1],
                        check=check)
        statistics = {'segments_reused': sum(p.reused for p in plans),
                      'segments_written': sum(not p.reused for p in plans),
                      'segment_chunks_written': segment_chunks_written,
                      'node_memberships_written': 0, 'edge_memberships_written': 0,
                      'node_content_written': 0, 'edge_content_written': 0, 'node_identities_written': 0}
        page_count = sum(len(pages) for pages in (page_plans or {}).values())
        with content_write_cache(connection, len(graph.nodes) + len(graph.edges),
                                 membership_page_count=page_count):
            for kind, fields in (
                ('node', 'node_id,kind,occurred_at,properties_json'),
                ('edge', 'edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json'),
            ):
                entries = _content_order(graph, plans, kind, check, page_plans)
                placeholders = ','.join('?' for _ in range(len(fields.split(',')) + 2))
                statement = f'INSERT INTO graph_{kind}_content(creator_account_id,content_id,{fields}) VALUES ({placeholders}) ON CONFLICT(creator_account_id,content_id) DO NOTHING'
                while True:
                    check()
                    writer._check_heartbeat()
                    batch = list(islice(entries, writer._chunk_size))
                    if not batch:
                        break
                    started = time.monotonic()
                    with writer._owned_transaction() as db:
                        existing_content = _existing_ids(db, f'graph_{kind}_content',
                            'content_id', graph.account_ref, [value[1] for value, _ in batch], check)
                        changed = [value for value, _ in batch if value[1] not in existing_content]
                        if kind == 'node':
                            identities = {value[2] for value in changed}
                            present = _existing_ids(db, 'graph_node_identities', 'node_id',
                                graph.account_ref, sorted(identities), check)
                            statistics['node_identities_written'] += len(identities - present)
                        cursor = db.executemany(statement, changed)
                        statistics[kind + '_content_written'] += cursor.rowcount
                        write_members(db, kind, [member for _, member in batch], page_plans)
                    writer._record_operation_duration(time.monotonic() - started, allow_growth=True)
                    statistics[kind + '_memberships_written'] += len(batch)
        with writer._owned_transaction() as db:
            for plan in plans:
                check()
                if not plan.reused:
                    db.execute('UPDATE graph_segments SET sealed=1 WHERE creator_account_id=? AND segment_id=?',
                               (graph.account_ref, plan.segment_id))
        writer.refresh()
        writer.shared_graph_validation = SharedGraphValidation(
            graph_digest=graph_digest or graph.digest(check=check),
            plans=tuple(SegmentValidation(
                p.kind, p.bucket, p.segment_id, p.digest, len(p.keys), p.reused
            ) for p in plans),
            proof=(existing.proof if isinstance(existing, PredecessorSegments) else None),
            segment_root=segment_root_digest(plans, check=check),
        )
        return statistics


def _verify_segment_rows(connection, account_id, plan, check):
    from app.analytics.graph_row_encoding import node_bytes, edge_bytes
    from app.analytics.graph_store import GraphReferentialIntegrityError

    relation = 'node_id' if plan.kind == 'node' else 'edge_id'
    table = 'graph_segment_nodes' if plan.kind == 'node' else 'graph_segment_edges'
    content = 'graph_node_content' if plan.kind == 'node' else 'graph_edge_content'
    encode = node_bytes if plan.kind == 'node' else edge_bytes
    digest = hashlib.sha256(
        ('graph-segment.v1:' + plan.kind + ':' + plan.bucket).encode()
    )
    rows = connection.execute(f'''SELECT c.* FROM {table} r
        JOIN {content} c USING(creator_account_id,content_id,{relation})
        WHERE r.creator_account_id=? AND r.segment_id=?
        ORDER BY r.{relation}''', (account_id, plan.segment_id))
    count, categories = 0, Counter()
    chunk_digest = hashlib.sha256()
    try:
        for row in rows:
            check()
            category, encoded = encode(row, account_id)
            version = hashlib.sha256(encoded).hexdigest()
            if row['content_id'] != version:
                raise GraphReferentialIntegrityError('graph_segment_content_invalid')
            digest.update(
                row[relation].encode() + b':' + version.encode() + b'\n'
            )
            if count:
                chunk_digest.update(b',')
            chunk_digest.update(encoded)
            categories[category] += 1
            count += 1
    finally:
        rows.close()
    if ((plan.count >= 0 and count != plan.count)
            or digest.hexdigest() != plan.digest):
        raise GraphReferentialIntegrityError('graph_segment_digest_invalid')
    return VerifiedSegment(
        plan.kind, plan.bucket, plan.segment_id, plan.digest, count,
        tuple(sorted(categories.items())), chunk_digest.hexdigest(),
    )


def verify_generation_segments(connection, generation_id, account_id, check):
    """Anchor a process-local proof to actual persisted segment bytes."""

    plans = [
        SegmentValidation(
            row['kind'], row['bucket'], row['segment_id'],
            row['content_digest'], -1, False,
        )
        for row in connection.execute(
            '''SELECT m.kind,m.bucket,m.segment_id,s.content_digest
               FROM generation_graph_segments m
               JOIN graph_segments s USING(creator_account_id,segment_id)
               WHERE m.generation_id=? AND m.creator_account_id=? AND s.sealed=1
               ORDER BY m.kind,m.bucket''',
            (generation_id, account_id),
        )
    ]
    return tuple(
        _verify_segment_rows(connection, account_id, plan, check)
        for plan in plans
    )


def verify_shared_graph(connection, generation_id, account_id, validation, check):
    """Reuse only exact, schema-bound proofs of immutable predecessor segments."""

    if validation is None or validation.proof is None:
        return None
    from app.analytics.graph_store import GraphReferentialIntegrityError
    from app.analytics.validation_receipt import content_stamp

    stamp = content_stamp(connection)
    proof = validation.proof
    if stamp is None or tuple(stamp[:3]) != proof.stamp_prefix:
        return None
    expected = sorted(
        (plan.kind, plan.bucket, plan.segment_id, plan.digest)
        for plan in validation.plans
    )
    actual = [tuple(row) for row in connection.execute(
        '''SELECT m.kind,m.bucket,m.segment_id,s.content_digest
           FROM generation_graph_segments m
           JOIN graph_segments s USING(creator_account_id,segment_id)
           WHERE m.generation_id=? AND m.creator_account_id=? AND s.sealed=1
           ORDER BY m.kind,m.bucket''',
        (generation_id, account_id),
    )]
    if actual != expected:
        raise GraphReferentialIntegrityError('graph_segment_plan_invalid')
    proven = {(item.kind, item.bucket): item for item in proof.segments}
    verified, node_counts, edge_counts = [], Counter(), Counter()
    for plan in validation.plans:
        check()
        if plan.reused:
            item = proven.get((plan.kind, plan.bucket))
            if (item is None or item.segment_id != plan.segment_id
                    or item.digest != plan.digest or item.count != plan.count
                    or item.chunk_digest is None):
                return None
        else:
            item = _verify_segment_rows(connection, account_id, plan, check)
        verified.append(item)
        counts = node_counts if plan.kind == 'node' else edge_counts
        counts.update(dict(item.categories))
    root = segment_root_digest(verified, check=check)
    if validation.segment_root is not None and root != validation.segment_root:
        raise GraphReferentialIntegrityError('graph_segment_root_invalid')
    return (
        validation.graph_digest, node_counts, edge_counts, tuple(verified), root
    )


def selected_content_ids(connection, generation_id: str, account_id: str,
                         kind: str, keys, check=lambda: None, *,
                         page_layout=False) -> dict[str, str]:
    """Return content hashes only for selected identities in one witnessed generation."""

    if kind not in ('node', 'edge'):
        raise ValueError('graph_record_kind_invalid')
    result = {}
    values = list(dict.fromkeys(keys))
    for offset in range(0, len(values), 256):
        check()
        batch = values[offset:offset + 256]
        if not batch:
            continue
        marks = ','.join('?' for _ in batch)
        relation = kind + '_id'
        membership = f"JOIN graph_segment_{kind}s r USING(creator_account_id,segment_id)"
        page_filter = ""
        if page_layout:
            membership = (
                "JOIN graph_segment_membership_pages p USING(creator_account_id,segment_id) "
                f"JOIN graph_membership_{kind}s r USING(creator_account_id,page_id)"
            )
            page_filter = f"AND p.kind=m.kind AND p.bucket=substr(c.{relation},4,3)"
        rows = connection.execute(f'''SELECT c.{relation},c.content_id
            FROM graph_{kind}_content c
            WHERE c.creator_account_id=? AND c.{relation} IN ({marks})
              AND EXISTS (
                SELECT 1
                FROM generation_graph_segments m
                {membership}
                WHERE m.generation_id=?
                  AND m.creator_account_id=c.creator_account_id
                  AND m.kind=? AND m.bucket=substr(c.{relation},4,2)
                  {page_filter}
                  AND r.{relation}=c.{relation}
                  AND r.content_id=c.content_id
              )''',
            (account_id, *batch, generation_id, kind))
        for row in rows:
            check()
            result[row[0]] = row[1]
    return result


def verified_segment_chunks_complete(connection, account_id: str,
                                     proof: GraphSegmentProof) -> bool:
    """Check the selected predecessor chunk headers against its proof."""

    if not chunks_supported(connection) or not proof.segments:
        return False
    rows = {
        row['segment_id']: row
        for row in connection.execute(
            """SELECT c.segment_id,c.kind,c.record_count,c.canonical_digest
               FROM generation_graph_segments m
               JOIN graph_segment_chunks c
                 USING(creator_account_id,segment_id,kind)
               WHERE m.generation_id=? AND m.creator_account_id=?""",
            (proof.generation_id, account_id),
        )
    }
    return all(
        item.chunk_digest is not None
        and item.segment_id in rows
        and rows[item.segment_id]['kind'] == item.kind
        and int(rows[item.segment_id]['record_count']) == item.count
        and rows[item.segment_id]['canonical_digest'] == item.chunk_digest
        for item in proof.segments
    )


def verified_segment_chunk(connection, account_id: str, proof: GraphSegmentProof,
                           kind: str, bucket: str):
    """Open one immutable canonical chunk only when it matches the live proof."""

    if kind not in ('node', 'edge'):
        raise ValueError('graph_record_kind_invalid')
    item = next((value for value in proof.segments
                 if value.kind == kind and value.bucket == bucket), None)
    if item is None or item.chunk_digest is None or not chunks_supported(connection):
        return None
    row = connection.execute(
        """SELECT kind,record_count,canonical_bytes,canonical_digest
           FROM graph_segment_chunks
           WHERE creator_account_id=? AND segment_id=?""",
        (account_id, item.segment_id),
    ).fetchone()
    if (row is None or row['kind'] != kind or int(row['record_count']) != item.count
            or row['canonical_digest'] != item.chunk_digest
            or hashlib.sha256(row['canonical_bytes']).hexdigest() != item.chunk_digest):
        return None
    return item, row['canonical_bytes']


def uses_segments(connection, generation_id: str, account_id: str) -> bool:
    return connection.execute('SELECT 1 FROM generation_graph_segments WHERE generation_id=? AND creator_account_id=? LIMIT 1',
                              (generation_id, account_id)).fetchone() is not None


def _incremental_endpoint_links_valid(
    connection, generation_id, account_id, validation, check
):
    """Verify only endpoint closure that can change from a proven predecessor."""

    if (validation is None or validation.proof is None
            or validation.removed_nodes is None):
        return False
    from app.analytics.graph_store import GraphReferentialIntegrityError
    from app.analytics.validation_receipt import content_stamp

    stamp = content_stamp(connection)
    proof = validation.proof
    if stamp is None or tuple(stamp[:3]) != proof.stamp_prefix:
        return False

    expected = sorted(
        (plan.kind, plan.bucket, plan.segment_id, plan.digest)
        for plan in validation.plans
    )
    actual = [tuple(row) for row in connection.execute(
        '''SELECT m.kind,m.bucket,m.segment_id,s.content_digest
           FROM generation_graph_segments m
           JOIN graph_segments s USING(creator_account_id,segment_id)
           WHERE m.generation_id=? AND m.creator_account_id=? AND s.sealed=1
           ORDER BY m.kind,m.bucket''',
        (generation_id, account_id),
    )]
    if actual != expected:
        raise GraphReferentialIntegrityError('graph_segment_plan_invalid')

    proven = {(item.kind, item.bucket): item for item in proof.segments}
    for plan in validation.plans:
        check()
        if not plan.reused:
            continue
        item = proven.get((plan.kind, plan.bucket))
        if (item is None or item.segment_id != plan.segment_id
                or item.digest != plan.digest or item.count != plan.count
                or item.chunk_digest is None):
            return False

    # A reused edge was already endpoint-closed in the predecessor. It can
    # become invalid only if one of its selected node IDs disappeared.
    removed = list(validation.removed_nodes)
    for offset in range(0, len(removed), 128):
        check()
        batch = removed[offset:offset + 128]
        if not batch:
            continue
        marks = ','.join('?' for _ in batch)
        for field in ('source_id', 'target_id'):
            referenced = connection.execute(f'''SELECT 1
                FROM graph_edge_content e
                JOIN graph_segment_edges r
                  USING(creator_account_id,content_id,edge_id)
                JOIN generation_graph_segments m
                  USING(creator_account_id,segment_id)
                WHERE m.generation_id=? AND m.creator_account_id=?
                  AND m.kind='edge' AND e.{field} IN ({marks})
                LIMIT 1''', (generation_id, account_id, *batch)).fetchone()
            if referenced is not None:
                raise GraphReferentialIntegrityError('graph_endpoint_absent')

    from app.analytics.graph_membership_pages import supported as pages_supported
    node_membership = 'JOIN graph_segment_nodes n USING(creator_account_id,segment_id)'
    source_page = target_page = ''
    if pages_supported(connection):
        node_membership = (
            'JOIN graph_segment_membership_pages np USING(creator_account_id,segment_id) '
            'JOIN graph_membership_nodes n USING(creator_account_id,page_id)'
        )
        source_page = "AND np.kind='node' AND np.bucket=substr(e.source_id,4,3)"
        target_page = "AND np.kind='node' AND np.bucket=substr(e.target_id,4,3)"

    # New or changed edge buckets need their current endpoints checked against
    # the selected node manifest. Bucket routing keeps each node lookup bounded.
    for plan in validation.plans:
        check()
        if plan.kind != 'edge' or plan.reused:
            continue
        missing = connection.execute(f'''SELECT 1
            FROM graph_segment_edges r
            JOIN graph_edge_content e
              USING(creator_account_id,content_id,edge_id)
            WHERE r.creator_account_id=? AND r.segment_id=?
              AND (
                NOT EXISTS (
                    SELECT 1 FROM generation_graph_segments nm
                    {node_membership}
                    WHERE nm.generation_id=? AND nm.creator_account_id=?
                      AND nm.kind='node'
                      AND nm.bucket=substr(e.source_id,4,2)
                      {source_page}
                      AND n.node_id=e.source_id
                )
                OR NOT EXISTS (
                    SELECT 1 FROM generation_graph_segments nm
                    {node_membership}
                    WHERE nm.generation_id=? AND nm.creator_account_id=?
                      AND nm.kind='node'
                      AND nm.bucket=substr(e.target_id,4,2)
                      {target_page}
                      AND n.node_id=e.target_id
                )
              )
            LIMIT 1''', (
                account_id, plan.segment_id,
                generation_id, account_id,
                generation_id, account_id,
            )).fetchone()
        if missing is not None:
            raise GraphReferentialIntegrityError('graph_endpoint_absent')
    return True


def _verify_all_shared_endpoints(connection, generation_id, account_id):
    """Cold/restart fallback that independently scans complete endpoint closure."""

    parameters = (generation_id, account_id)
    return connection.execute('''WITH sides(side) AS (VALUES(0),(1)), missing AS (
        SELECT CASE side WHEN 0 THEN e.source_id ELSE e.target_id END AS node_id
        FROM generation_graph_segments m
        CROSS JOIN graph_segment_edges r USING(creator_account_id,segment_id)
        LEFT JOIN graph_edge_content e USING(creator_account_id,content_id,edge_id)
        CROSS JOIN sides
        WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind='edge'
        EXCEPT
        SELECT nc.node_id FROM generation_graph_segments nm
        CROSS JOIN graph_segment_nodes n USING(creator_account_id,segment_id)
        CROSS JOIN graph_node_content nc USING(creator_account_id,content_id,node_id)
        WHERE nm.generation_id=? AND nm.creator_account_id=? AND nm.kind='node')
        SELECT 1 FROM missing LIMIT 1''', parameters * 2).fetchone()


def verify_segment_links(
    connection, generation_id: str, account_id: str, *,
    validation=None, check=lambda: None,
) -> None:
    """Check selected endpoints and layout, reusing only proven predecessor closure."""

    from app.analytics.graph_store import GraphReferentialIntegrityError
    parameters = (generation_id, account_id)
    shared = uses_segments(connection, *parameters)
    if shared:
        for table in ('graph_owned_nodes', 'graph_owned_edges'):
            if connection.execute(
                f'SELECT 1 FROM {table} WHERE generation_id=? '
                'AND creator_account_id=? LIMIT 1', parameters
            ).fetchone():
                raise GraphReferentialIntegrityError('graph_generation_layout_mixed')
        invalid = connection.execute('''SELECT 1 FROM generation_graph_segments m
            LEFT JOIN graph_segments s USING(creator_account_id,segment_id)
            WHERE m.generation_id=? AND m.creator_account_id=?
              AND (s.segment_id IS NULL OR s.sealed!=1
                   OR s.kind!=m.kind OR s.bucket!=m.bucket)
            LIMIT 1''', parameters).fetchone()
        if invalid:
            raise GraphReferentialIntegrityError('graph_segment_invalid')
    missing = connection.execute('''SELECT 1 FROM graph_owned_edges e
        LEFT JOIN graph_owned_nodes source
          ON source.generation_id=e.generation_id
         AND source.creator_account_id=e.creator_account_id
         AND source.node_id=e.source_id
        LEFT JOIN graph_owned_nodes target
          ON target.generation_id=e.generation_id
         AND target.creator_account_id=e.creator_account_id
         AND target.node_id=e.target_id
        WHERE e.generation_id=? AND e.creator_account_id=?
          AND (source.node_id IS NULL OR target.node_id IS NULL)
        LIMIT 1''', parameters).fetchone()
    if missing:
        raise GraphReferentialIntegrityError('graph_endpoint_absent')
    if not shared:
        return
    if _incremental_endpoint_links_valid(
        connection, generation_id, account_id, validation, check
    ):
        return
    if _verify_all_shared_endpoints(connection, generation_id, account_id):
        raise GraphReferentialIntegrityError('graph_endpoint_absent')


def ordered_rows(connection, generation_id: str, account_id: str, kind: str):
    """Use bucket order, which is identical to the canonical fixed-format ID order."""

    if kind not in ('node', 'edge'):
        raise ValueError('graph_record_kind_invalid')
    fields = ('node_id,kind,occurred_at,properties_json' if kind == 'node' else
              'edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json')
    return connection.execute(f'''SELECT m.generation_id,m.creator_account_id,
            m.bucket AS segment_bucket,m.segment_id,
            s.content_digest AS segment_digest,c.content_id,
            {','.join('c.'+field for field in fields.split(','))}
        FROM generation_graph_segments m
        CROSS JOIN graph_segments s USING(creator_account_id,segment_id)
        CROSS JOIN graph_segment_{kind}s r USING(creator_account_id,segment_id)
        CROSS JOIN graph_{kind}_content c USING(creator_account_id,content_id,{kind}_id)
        WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind=?
          AND s.sealed=1 AND s.kind=m.kind AND s.bucket=m.bucket
        ORDER BY m.bucket,r.{kind}_id''', (generation_id, account_id, kind))
