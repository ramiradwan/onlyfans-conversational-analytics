"""Private canonical graph records for memory-conscious SQLite construction."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import hashlib
from itertools import islice
import json
import time
from typing import Callable

from app.models.analytics import AnalyticsProjection, GraphNode, GraphEdge, GraphProjectionSummary


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class CompactGraph:
    def __init__(self, account_ref: str):
        self.account_ref = account_ref
        self.nodes: dict[str, str] = {}
        self.edges: dict[str, str] = {}
        self.node_counts: Counter[str] = Counter()
        self.edge_counts: Counter[str] = Counter()
        self.encoded_bytes = 0
    def add(self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge], *, check: Callable[[], None]) -> None:
        for source, target, counts, key_name, kind_name in (
            (nodes, self.nodes, self.node_counts, "node_id", "kind"),
            (edges, self.edges, self.edge_counts, "edge_id", "relation"),
        ):
            for record in source:
                check()
                if record.account_ref != self.account_ref:
                    raise ValueError("compact_graph_account_invalid")
                key = getattr(record, key_name)
                data = _json(record.model_dump(mode="json"))
                previous = target.get(key)
                if previous is not None:
                    if previous != data:
                        raise ValueError("graph_record_identity_collision")
                    continue
                target[key] = data
                counts[getattr(record, kind_name).value] += 1
                self.encoded_bytes += len(data.encode("utf-8"))

    def merge(self, other: CompactGraph, *, check: Callable[[], None]) -> None:
        if other.account_ref != self.account_ref:
            raise ValueError("compact_graph_account_invalid")
        for source, target, counts, field in ((other.nodes, self.nodes, self.node_counts, "kind"),
                                            (other.edges, self.edges, self.edge_counts, "relation")):
            for key, data in source.items():
                check()
                if key in target and target[key] != data:
                    raise ValueError("graph_record_identity_collision")
                if key not in target:
                    target[key] = data
                    counts[json.loads(data)[field]] += 1
                    self.encoded_bytes += len(data.encode("utf-8"))

    def digest(self, *, check: Callable[[], None],
               node_ids: Iterable[str] | None = None,
               edge_ids: Iterable[str] | None = None) -> str:
        digest = hashlib.sha256(b'{"edges":[')
        for name, records, selected in (("edges", self.edges, edge_ids), ("nodes", self.nodes, node_ids)):
            check()
            if name == "nodes":
                digest.update(b'],"nodes":[')
            for ordinal, key in enumerate(sorted(records if selected is None else selected)):
                check()
                if ordinal:
                    digest.update(b",")
                digest.update(records[key].encode("utf-8"))
        digest.update(b"]}")
        return "sha256:" + digest.hexdigest()

    def summary(self, revision: int) -> GraphProjectionSummary:
        return GraphProjectionSummary(account_ref=self.account_ref, source_revision=revision,
            node_count=len(self.nodes), edge_count=len(self.edges),
            node_counts_by_kind=dict(sorted(self.node_counts.items())),
            edge_counts_by_relation=dict(sorted(self.edge_counts.items())))

    def materialize(self) -> tuple[list[GraphNode], list[GraphEdge]]:
        return ([GraphNode.model_validate_json(self.nodes[key]) for key in sorted(self.nodes)],
                [GraphEdge.model_validate_json(self.edges[key]) for key in sorted(self.edges)])


@dataclass(frozen=True, slots=True)
class CompactArtifact:
    projection: AnalyticsProjection
    graph: CompactGraph


def write_compact_graph(writer, graph: CompactGraph, *, check=lambda: None, store=None) -> None:
    """Write private canonical rows under the ordinary lease and SQL guards."""

    from app.analytics.sqlite_graph_store import _timestamp

    if store is not None and getattr(store, "reuse_graph_content", True):
        from app.analytics.shared_graph import supported, write_shared_graph
        with writer.database.read() as connection:
            enabled = supported(connection)
        if enabled:
            write_shared_graph(writer, graph, store, check=check)
            return

    def parameters(data: str, node: bool):
        record = json.loads(data)
        timestamp = record["occurred_at"]
        at = _timestamp(datetime.fromisoformat(timestamp)) if timestamp else None
        prefix = (writer._generation_id, record["account_ref"])
        properties = _json(record["properties"])
        if node:
            return (*prefix, record["node_id"], record["kind"], at, properties)
        return (*prefix, record["edge_id"], record["source_id"], record["target_id"],
                record["relation"], at, record["sequence"], properties)

    with writer.lease_session():
        for node, records, statement in (
            (True, graph.nodes, "INSERT INTO graph_nodes(generation_id,creator_account_id,node_id,kind,occurred_at,properties_json) VALUES (?,?,?,?,?,?)"),
            (False, graph.edges, "INSERT INTO graph_edges(generation_id,creator_account_id,edge_id,source_id,target_id,relation,occurred_at,sequence,properties_json) VALUES (?,?,?,?,?,?,?,?,?)"),
        ):
            keys = iter(sorted(records))
            while True:
                check()
                writer._check_heartbeat()
                batch_keys = list(islice(keys, writer._chunk_size))
                if not batch_keys:
                    break
                started = time.monotonic()
                batch = [parameters(records[key], node) for key in batch_keys]
                writer._record_operation_duration(time.monotonic() - started)
                started = time.monotonic()
                with writer._owned_transaction() as connection:
                    connection.executemany(statement, batch)
                writer._record_operation_duration(time.monotonic() - started, allow_growth=True)
                writer._check_heartbeat()
        writer.refresh()
