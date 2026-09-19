"""Reuse immutable graph records through generation-owned segment manifests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
import hashlib
from itertools import groupby, islice
import json
import time
from uuid import uuid4

from app.analytics.compact_graph import CompactGraph, _json


@dataclass(frozen=True, slots=True)
class SegmentPlan:
    kind: str
    bucket: str
    digest: str
    segment_id: str
    keys: list[str]
    reused: bool


def supported(connection) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='generation_graph_segments'").fetchone() is not None


def _predecessor_segments(store, connection, partition: str) -> dict:
    generation = connection.execute("SELECT * FROM projection_generations WHERE creator_account_id=? AND status='active'", (partition,)).fetchone()
    if generation is None:
        return {}
    witness = store.activation.get(generation['generation_id'])
    if not store._intent_matches(generation, witness, require_completed=True):
        return {}
    return {(row['kind'], row['bucket']): (row['content_digest'], row['segment_id'])
        for row in connection.execute("""SELECT s.* FROM generation_graph_segments m
          JOIN graph_segments s USING(creator_account_id,segment_id)
          WHERE m.generation_id=? AND m.creator_account_id=? AND s.sealed=1""",
          (generation['generation_id'], partition))}


def _plans(graph: CompactGraph, existing: dict,
           check: Callable[[], None]) -> Iterator[SegmentPlan]:
    for kind, records in (('node', graph.nodes), ('edge', graph.edges)):
        for bucket, grouped in groupby(sorted(records), key=lambda key: key[3:5]):
            keys = list(grouped)
            digest = hashlib.sha256(('graph-segment.v1:' + kind + ':' + bucket).encode())
            for key in keys:
                check()
                version = hashlib.sha256(records[key].encode('utf-8')).hexdigest()
                digest.update(key.encode() + b':' + version.encode() + b'\n')
            signature = digest.hexdigest()
            previous = existing.get((kind, bucket))
            reused = previous is not None and previous[0] == signature
            yield SegmentPlan(kind, bucket, signature, previous[1] if reused else str(uuid4()), keys, reused)


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


def write_shared_graph(writer, graph: CompactGraph, store, *, check=lambda: None) -> dict[str, int]:
    """Write changed content only; verify the complete selected graph at publication."""

    with writer.lease_session():
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
        statistics = {'segments_reused': sum(p.reused for p in plans),
                      'segments_written': sum(not p.reused for p in plans),
                      'node_memberships_written': 0, 'edge_memberships_written': 0,
                      'node_content_written': 0, 'edge_content_written': 0, 'node_identities_written': 0}
        for kind, fields in (
            ('node', 'node_id,kind,occurred_at,properties_json'),
            ('edge', 'edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json'),
        ):
            entries = (item for plan in plans if plan.kind == kind and not plan.reused
                       for item in _records(graph, plan, check))
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
                    before = db.total_changes
                    cursor = db.executemany(statement, [value for value, _ in batch])
                    statistics[kind + '_content_written'] += cursor.rowcount
                    if kind == 'node':
                        statistics['node_identities_written'] += db.total_changes - before - cursor.rowcount
                    db.executemany(f'INSERT INTO graph_segment_{kind}s(creator_account_id,segment_id,{kind}_id,content_id) VALUES (?,?,?,?)',
                                   [member for _, member in batch])
                writer._record_operation_duration(time.monotonic() - started, allow_growth=True)
                statistics[kind + '_memberships_written'] += len(batch)
        with writer._owned_transaction() as db:
            for plan in plans:
                check()
                if not plan.reused:
                    db.execute('UPDATE graph_segments SET sealed=1 WHERE creator_account_id=? AND segment_id=?',
                               (graph.account_ref, plan.segment_id))
        writer.refresh()
        return statistics


def uses_segments(connection, generation_id: str, account_id: str) -> bool:
    return connection.execute('SELECT 1 FROM generation_graph_segments WHERE generation_id=? AND creator_account_id=? LIMIT 1',
                              (generation_id, account_id)).fetchone() is not None


def verify_segment_links(connection, generation_id: str, account_id: str) -> None:
    """Check actual selected endpoints and layout independently of foreign-key settings."""

    from app.analytics.graph_store import GraphReferentialIntegrityError
    parameters = (generation_id, account_id)
    shared = uses_segments(connection, *parameters)
    if shared:
        for table in ('graph_owned_nodes', 'graph_owned_edges'):
            if connection.execute(f'SELECT 1 FROM {table} WHERE generation_id=? AND creator_account_id=? LIMIT 1', parameters).fetchone():
                raise GraphReferentialIntegrityError('graph_generation_layout_mixed')
        invalid = connection.execute('''SELECT 1 FROM generation_graph_segments m
            LEFT JOIN graph_segments s USING(creator_account_id,segment_id)
            WHERE m.generation_id=? AND m.creator_account_id=?
              AND (s.segment_id IS NULL OR s.sealed!=1 OR s.kind!=m.kind OR s.bucket!=m.bucket) LIMIT 1''', parameters).fetchone()
        if invalid:
            raise GraphReferentialIntegrityError('graph_segment_invalid')
    missing = connection.execute('''SELECT 1 FROM graph_owned_edges e
        LEFT JOIN graph_owned_nodes source ON source.generation_id=e.generation_id AND source.creator_account_id=e.creator_account_id AND source.node_id=e.source_id
        LEFT JOIN graph_owned_nodes target ON target.generation_id=e.generation_id AND target.creator_account_id=e.creator_account_id AND target.node_id=e.target_id
        WHERE e.generation_id=? AND e.creator_account_id=?
          AND (source.node_id IS NULL OR target.node_id IS NULL) LIMIT 1''', parameters).fetchone()
    if missing:
        raise GraphReferentialIntegrityError('graph_endpoint_absent')
    if not shared:
        return
    missing = connection.execute('''WITH sides(side) AS (VALUES(0),(1)), missing AS (
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
    if missing:
        raise GraphReferentialIntegrityError('graph_endpoint_absent')


def ordered_rows(connection, generation_id: str, account_id: str, kind: str):
    """Use bucket order, which is identical to the canonical fixed-format ID order."""

    if kind not in ('node', 'edge'):
        raise ValueError('graph_record_kind_invalid')
    fields = ('node_id,kind,occurred_at,properties_json' if kind == 'node' else
              'edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json')
    return connection.execute(f'''SELECT m.generation_id,m.creator_account_id,{','.join('c.'+field for field in fields.split(','))}
        FROM generation_graph_segments m CROSS JOIN graph_segment_{kind}s r USING(creator_account_id,segment_id)
        CROSS JOIN graph_{kind}_content c USING(creator_account_id,content_id,{kind}_id)
        WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind=?
        ORDER BY m.bucket,r.{kind}_id''', (generation_id, account_id, kind))
