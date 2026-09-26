"""Incremental compact graph assembled from immutable predecessor segments."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from uuid import uuid4

from app.analytics.compact_graph import _json
from app.analytics.shared_graph import _segment_digest, segment_root_digest
from app.models.analytics import GraphProjectionSummary


@dataclass(frozen=True, slots=True)
class IncrementalSegment:
    kind: str
    bucket: str
    digest: str
    segment_id: str
    count: int
    categories: tuple[tuple[str, int], ...]
    chunk_digest: str
    chunk: bytes | None
    records: dict[str, str] | None
    reused: bool


class IncrementalRecordMap:
    def __init__(self, count: int, overlay: dict[str, str]):
        self._count = count
        self._overlay = overlay

    def __len__(self):
        return self._count

    def get(self, key, default=None):
        return self._overlay.get(key, default)

    def __contains__(self, key):
        return key in self._overlay


class IncrementalCompactGraph:
    compact_graph = True

    def __init__(
        self,
        account_ref: str,
        *,
        graph_digest: str,
        segments: tuple[IncrementalSegment, ...],
        node_counts: Counter,
        edge_counts: Counter,
        overlay_nodes: dict[str, str],
        overlay_edges: dict[str, str],
        predecessor_generation_id: str,
        predecessor_proof,
        store,
        removed_nodes: set[str],
        removed_edges: set[str],
    ):
        self.account_ref = account_ref
        self._digest = graph_digest
        self.segments = segments
        self.node_counts = node_counts
        self.edge_counts = edge_counts
        self.nodes = IncrementalRecordMap(sum(node_counts.values()), overlay_nodes)
        self.edges = IncrementalRecordMap(sum(edge_counts.values()), overlay_edges)
        self.predecessor_generation_id = predecessor_generation_id
        self.predecessor_proof = predecessor_proof
        self._store = store
        self._removed = {"node": frozenset(removed_nodes), "edge": frozenset(removed_edges)}
        self._resolved = {"node": {}, "edge": {}}
        self.encoded_bytes = sum(
            len(value.encode("utf-8"))
            for value in (*overlay_nodes.values(), *overlay_edges.values())
        )

    def digest(self, *, check=lambda: None, node_ids=None, edge_ids=None):
        if node_ids is None and edge_ids is None:
            check()
            return self._digest
        digest = hashlib.sha256(b'{"edges":[')
        for kind, selected, records in (
            ("edge", edge_ids or (), self.edges._overlay),
            ("node", node_ids or (), self.nodes._overlay),
        ):
            check()
            if kind == "node":
                digest.update(b'],"nodes":[')
            selected = sorted(selected)
            missing = [
                key for key in selected
                if key not in records and key not in self._resolved[kind]
            ]
            if missing:
                self.resolve_records(kind, missing, check)
            for ordinal, key in enumerate(selected):
                check()
                data = records.get(key, self._resolved[kind].get(key))
                if data is None:
                    raise ValueError("incremental_graph_subset_record_unavailable")
                if ordinal:
                    digest.update(b",")
                digest.update(data.encode("utf-8"))
        digest.update(b"]}")
        return "sha256:" + digest.hexdigest()

    def resolve_records(self, kind: str, keys, check=lambda: None):
        if kind not in ("node", "edge"):
            raise ValueError("graph_record_kind_invalid")
        overlay = self.nodes._overlay if kind == "node" else self.edges._overlay
        cache = self._resolved[kind]
        missing = [
            key for key in keys
            if key not in overlay and key not in cache and key not in self._removed[kind]
        ]
        if missing:
            from app.analytics.conversation_graph_sql import graph_records
            with self._store.database.read() as db:
                values = graph_records(
                    db, self.predecessor_generation_id, self.account_ref,
                    kind, missing, check,
                )
            for key, value in zip(missing, values, strict=True):
                cache[key] = _json(value)
        result = []
        for key in keys:
            check()
            if key in self._removed[kind]:
                raise KeyError(key)
            data = overlay.get(key, cache.get(key))
            if data is None:
                raise KeyError(key)
            result.append(json.loads(data))
        return result

    def page_record_matches(self, kind: str, key: str, value) -> bool:
        if key in self._removed[kind]:
            return False
        data = (self.nodes._overlay if kind == "node" else self.edges._overlay).get(key)
        if data is not None:
            return data == _json(value)
        data = self._resolved[kind].get(key)
        if data is not None:
            return data == _json(value)
        # The page reader has already checked the selected predecessor row and
        # its content hash. Retain those exact canonical bytes for the page
        # subgraph digest that follows.
        self._resolved[kind][key] = _json(value)
        return True

    def summary(self, revision: int) -> GraphProjectionSummary:
        return GraphProjectionSummary(
            account_ref=self.account_ref,
            source_revision=revision,
            node_count=len(self.nodes),
            edge_count=len(self.edges),
            node_counts_by_kind=dict(sorted(self.node_counts.items())),
            edge_counts_by_relation=dict(sorted(self.edge_counts.items())),
        )


def _records_from_chunk(kind: str, chunk: bytes) -> dict[str, str]:
    if not chunk:
        return {}
    try:
        rows = json.loads(b"[" + chunk + b"]")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("graph_segment_chunk_invalid") from error
    key = "node_id" if kind == "node" else "edge_id"
    result = {}
    for row in rows:
        if not isinstance(row, dict) or key not in row:
            raise ValueError("graph_segment_chunk_invalid")
        data = _json(row)
        if row[key] in result:
            raise ValueError("graph_segment_chunk_duplicate")
        result[row[key]] = data
    return result


def _segment_value(kind, bucket, records):
    if not records:
        return None
    key_name = "kind" if kind == "node" else "relation"
    keys = sorted(records)
    digest = _segment_digest(kind, bucket, records, keys, lambda: None)
    categories = Counter()
    parts = []
    for key in keys:
        data = records[key]
        row = json.loads(data)
        categories[row[key_name]] += 1
        parts.append(data.encode("utf-8"))
    chunk = b",".join(parts)
    return (
        digest,
        tuple(sorted(categories.items())),
        chunk,
        hashlib.sha256(chunk).hexdigest(),
    )


def build_incremental_graph(
    *,
    account_ref: str,
    loader,
    current_units,
    changed_graph,
    store,
    removed_nodes: set[str],
    removed_edges: set[str],
    timeline_nodes,
    timeline_edges,
    check=lambda: None,
):
    """Build final segment metadata from one witnessed predecessor plus an overlay."""

    proof = getattr(loader, "graph_segment_proof", None)
    generation_id = getattr(loader, "active_generation_id", None)
    chunk_reader = getattr(loader, "graph_segment_chunk", None)
    content_reader = getattr(loader, "graph_content_ids", None)
    if proof is None or generation_id is None or not callable(chunk_reader) or not callable(content_reader):
        return None

    overlay_nodes = dict(changed_graph.nodes)
    overlay_edges = dict(changed_graph.edges)
    for key, value in timeline_nodes.items():
        previous = overlay_nodes.get(key)
        data = _json(value.model_dump(mode="json"))
        if previous is not None and previous != data:
            raise ValueError("graph_record_identity_collision")
        overlay_nodes[key] = data
    for key, value in timeline_edges.items():
        previous = overlay_edges.get(key)
        data = _json(value.model_dump(mode="json"))
        if previous is not None and previous != data:
            raise ValueError("graph_record_identity_collision")
        overlay_edges[key] = data

    changes = {"node": {}, "edge": {}}
    for kind, records in (("node", overlay_nodes), ("edge", overlay_edges)):
        keys = list(records)
        previous = content_reader(kind, keys, check=check)
        for key, data in records.items():
            check()
            if previous.get(key) != hashlib.sha256(data.encode("utf-8")).hexdigest():
                changes[kind][key] = data

    affected = {
        "node": {key[3:5] for key in (*changes["node"], *removed_nodes)},
        "edge": {key[3:5] for key in (*changes["edge"], *removed_edges)},
    }
    base = {(item.kind, item.bucket): item for item in proof.segments}
    all_keys = set(base)
    all_keys.update(("node", bucket) for bucket in affected["node"])
    all_keys.update(("edge", bucket) for bucket in affected["edge"])

    segments = []
    for kind, bucket in sorted(all_keys, key=lambda item: (item[0] != "edge", item[1])):
        check()
        previous = base.get((kind, bucket))
        if bucket not in affected[kind] and previous is not None:
            if previous.chunk_digest is None:
                return None
            segments.append(IncrementalSegment(
                kind, bucket, previous.digest, previous.segment_id, previous.count,
                previous.categories, previous.chunk_digest, None, None, True,
            ))
            continue

        records = {}
        if previous is not None:
            opened = chunk_reader(kind, bucket)
            if opened is None:
                return None
            _, chunk = opened
            records = _records_from_chunk(kind, chunk)
        removals = removed_nodes if kind == "node" else removed_edges
        for key in tuple(records):
            if key in removals:
                records.pop(key)
        for key, data in changes[kind].items():
            if key[3:5] == bucket:
                records[key] = data
        value = _segment_value(kind, bucket, records)
        if value is None:
            continue
        digest, categories, chunk, chunk_digest = value
        segments.append(IncrementalSegment(
            kind, bucket, digest, str(uuid4()), len(records), categories,
            chunk_digest, chunk, records, False,
        ))

    node_counts, edge_counts = Counter(), Counter()
    for segment in segments:
        (node_counts if segment.kind == "node" else edge_counts).update(
            dict(segment.categories)
        )

    digest = segment_root_digest(segments, check=check)

    return IncrementalCompactGraph(
        account_ref,
        graph_digest=digest,
        segments=tuple(segments),
        node_counts=node_counts,
        edge_counts=edge_counts,
        overlay_nodes=overlay_nodes,
        overlay_edges=overlay_edges,
        predecessor_generation_id=generation_id,
        predecessor_proof=proof,
        store=store,
        removed_nodes=removed_nodes,
        removed_edges=removed_edges,
    )


def write_incremental_graph(writer, graph: IncrementalCompactGraph, store, *, check=lambda: None):
    """Publish reused segments plus only the affected rebuilt buckets."""

    from datetime import datetime
    from app.analytics.database import content_write_cache
    from app.analytics.graph_membership_pages import (
        prepare_pages, supported as pages_supported, write_members,
    )
    from app.analytics.shared_graph import (
        SegmentValidation, SharedGraphValidation, _existing_ids,
    )
    from app.analytics.sqlite_graph_store import _timestamp

    with writer.lease_session(), content_write_cache(
        writer._write_connection, len(graph.nodes) + len(graph.edges)
    ):
        with writer._owned_transaction() as db:
            for segment in graph.segments:
                check()
                if not segment.reused:
                    db.execute(
                        """INSERT INTO graph_segments
                           (creator_account_id,segment_id,kind,bucket,content_digest)
                           VALUES (?,?,?,?,?)""",
                        (
                            graph.account_ref, segment.segment_id, segment.kind,
                            segment.bucket, segment.digest,
                        ),
                    )
                db.execute(
                    """INSERT INTO generation_graph_segments
                       (generation_id,creator_account_id,kind,bucket,segment_id)
                       VALUES (?,?,?,?,?)""",
                    (
                        writer._generation_id, graph.account_ref, segment.kind,
                        segment.bucket, segment.segment_id,
                    ),
                )

        changed_segments = sorted(
            (segment for segment in graph.segments if not segment.reused),
            key=lambda segment: (segment.kind != "node", segment.bucket),
        )
        statistics = {
            "segments_reused": sum(segment.reused for segment in graph.segments),
            "segments_written": len(changed_segments),
            "segment_chunks_written": len(changed_segments),
            "node_memberships_written": 0,
            "edge_memberships_written": 0,
            "node_content_written": 0,
            "edge_content_written": 0,
            "node_identities_written": 0,
        }
        page_plans = {} if pages_supported(writer._write_connection) else None
        if page_plans is not None:
            previous = {(p.kind, p.bucket): p for p in graph.predecessor_proof.segments}
            with writer._owned_transaction() as db:
                for segment in changed_segments:
                    check()
                    prior = previous.get((segment.kind, segment.bucket))
                    records = segment.records or {}
                    page_plans[(segment.kind, segment.segment_id)] = prepare_pages(
                        db, graph.account_ref, segment.segment_id, segment.kind,
                        records, records, None if prior is None else prior.segment_id,
                        check=check)
            pages = [p for plans in page_plans.values() for p in plans.values()]
            statistics['membership_pages_reused'] = sum(p.reused for p in pages)
            statistics['membership_pages_written'] = sum(not p.reused for p in pages)
            statistics['membership_page_refs_written'] = len(pages)
        prepared = {"node": [], "edge": []}
        segment_members = []
        for segment in changed_segments:
            records = segment.records or {}
            kind = segment.kind
            members = []
            for key in sorted(records):
                check()
                if page_plans is not None and page_plans[(kind, segment.segment_id)][key[3:6]].reused:
                    continue
                data = records[key]
                row = json.loads(data)
                content_id = hashlib.sha256(data.encode("utf-8")).hexdigest()
                occurred = row["occurred_at"]
                stamp = _timestamp(datetime.fromisoformat(occurred)) if occurred else None
                properties = _json(row["properties"])
                if kind == "node":
                    value = (
                        graph.account_ref, content_id, key, row["kind"],
                        stamp, properties,
                    )
                else:
                    value = (
                        graph.account_ref, content_id, key, row["source_id"],
                        row["target_id"], row["relation"], stamp,
                        row["sequence"], properties,
                    )
                prepared[kind].append(value)
                members.append(
                    (graph.account_ref, segment.segment_id, key, content_id)
                )
            segment_members.append((segment, members))

        # Preserve the same physical write-order contract as the full shared
        # graph path: new immutable content is inserted globally by content ID,
        # independent of which changed bucket selected the record.
        for kind in ("node", "edge"):
            values = sorted(prepared[kind], key=lambda value: value[1])
            if not values:
                continue
            fields = (
                "node_id,kind,occurred_at,properties_json"
                if kind == "node"
                else "edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json"
            )
            placeholders = ",".join("?" for _ in range(len(fields.split(",")) + 2))
            statement = (
                f"INSERT INTO graph_{kind}_content"
                f"(creator_account_id,content_id,{fields}) VALUES ({placeholders}) "
                "ON CONFLICT(creator_account_id,content_id) DO NOTHING"
            )
            with writer._owned_transaction() as db:
                existing = _existing_ids(
                    db, f"graph_{kind}_content", "content_id", graph.account_ref,
                    [value[1] for value in values], check,
                )
                changed = [value for value in values if value[1] not in existing]
                if kind == "node" and changed:
                    identities = {value[2] for value in changed}
                    present = _existing_ids(
                        db, "graph_node_identities", "node_id",
                        graph.account_ref, sorted(identities), check,
                    )
                    statistics["node_identities_written"] += len(identities - present)
                if changed:
                    cursor = db.executemany(statement, changed)
                    statistics[kind + "_content_written"] += cursor.rowcount

        for segment, members in segment_members:
            kind = segment.kind
            relation = "node_id" if kind == "node" else "edge_id"
            with writer._owned_transaction() as db:
                if members:
                    write_members(db, kind, members, page_plans)
                    statistics[kind + "_memberships_written"] += len(members)
                db.execute(
                    """INSERT INTO graph_segment_chunks
                       (creator_account_id,segment_id,kind,record_count,
                        canonical_bytes,canonical_digest)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        graph.account_ref, segment.segment_id, kind, segment.count,
                        segment.chunk, segment.chunk_digest,
                    ),
                )
                db.execute(
                    """UPDATE graph_segments SET sealed=1
                       WHERE creator_account_id=? AND segment_id=?""",
                    (graph.account_ref, segment.segment_id),
                )
        writer.shared_graph_validation = SharedGraphValidation(
            graph_digest=graph.digest(check=check),
            plans=tuple(
                SegmentValidation(
                    item.kind, item.bucket, item.segment_id, item.digest,
                    item.count, item.reused,
                )
                for item in graph.segments
            ),
            proof=graph.predecessor_proof,
            removed_nodes=tuple(sorted(graph._removed["node"])),
            segment_root=segment_root_digest(graph.segments, check=check),
        )
        writer.refresh()
        return statistics
