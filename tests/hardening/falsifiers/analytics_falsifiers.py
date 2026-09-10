"""Deliberately broken adapters used only to verify the convergence oracle."""

from __future__ import annotations

import hashlib
import json

from app.models.analytics import RebuildArtifact


def _valid_graph_id(label: str) -> str:
    return "g1:" + hashlib.sha256(
        b"analytics_convergence-falsifier-identity\0" + label.encode("utf-8")
    ).hexdigest()


def _graph_digest(artifact: RebuildArtifact) -> str:
    value = {
        "nodes": [
            node.model_dump(mode="json")
            for node in sorted(artifact.nodes, key=lambda item: item.node_id)
        ],
        "edges": [
            edge.model_dump(mode="json")
            for edge in sorted(artifact.edges, key=lambda item: item.edge_id)
        ],
    }
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class BrokenMetricAdapter:
    """Corrupt one aggregate after live publication."""

    @staticmethod
    def apply(artifact: RebuildArtifact) -> RebuildArtifact:
        metrics = artifact.projection.creator_metrics.model_copy(
            update={
                "message_count": artifact.projection.creator_metrics.message_count
                + 1
            }
        )
        return artifact.model_copy(
            update={"projection": artifact.projection.model_copy(update={"creator_metrics": metrics})}
        )


class BrokenProvenanceAdapter:
    """Publish an artifact with a false canonical witness."""

    @staticmethod
    def apply(artifact: RebuildArtifact) -> RebuildArtifact:
        return artifact.model_copy(
            update={
                "projection": artifact.projection.model_copy(
                    update={"canonical_content_digest": "sha256:" + "0" * 64}
                )
            }
        )


class BrokenIdentityAdapter:
    """Derive a valid-format message identity from processing order."""

    @staticmethod
    def apply(artifact: RebuildArtifact) -> RebuildArtifact:
        # This deliberately depends on a mutable traversal position, rather
        # than canonical account/conversation/message identity.
        position = len(artifact.projection.message_enrichments) - 1
        selected = artifact.projection.message_enrichments[position].model_copy(
            update={
                "message_ref": "m1:"
                + hashlib.sha256(
                    f"processing-order:{position}".encode("utf-8")
                ).hexdigest()
            }
        )
        enrichments = list(artifact.projection.message_enrichments)
        enrichments[position] = selected
        return artifact.model_copy(
            update={
                "projection": artifact.projection.model_copy(
                    update={"message_enrichments": enrichments}
                )
            }
        )


class BrokenGraphAdapter:
    """Omit one semantic edge only from the simulated incremental result."""

    @staticmethod
    def apply(artifact: RebuildArtifact) -> RebuildArtifact:
        return artifact.model_copy(update={"edges": artifact.edges[1:]})


class BrokenDeletionClosureAdapter:
    """Leave an edge pointing at a graph node removed by a deletion."""

    @staticmethod
    def apply(artifact: RebuildArtifact, *, stale_node_id: str) -> RebuildArtifact:
        first = artifact.edges[0].model_copy(update={"target_id": stale_node_id})
        return artifact.model_copy(update={"edges": [first] + artifact.edges[1:]})


class BrokenDerivedDeletionAdapter:
    """Retain one deleted-message contribution while remaining structurally valid."""

    @staticmethod
    def apply(artifact: RebuildArtifact) -> RebuildArtifact:
        metrics = artifact.projection.creator_metrics.model_copy(
            update={
                "message_count": artifact.projection.creator_metrics.message_count
                + 1
            }
        )
        return artifact.model_copy(
            update={
                "projection": artifact.projection.model_copy(
                    update={"creator_metrics": metrics}
                )
            }
        )


class ForgedTopicEntityIdentityAdapter:
    """Forge one valid-format topic/entity node and repair local graph shape."""

    @staticmethod
    def apply(artifact: RebuildArtifact) -> RebuildArtifact:
        original = next(
            node for node in artifact.nodes if node.kind.value in {"topic", "entity"}
        )
        forged_id = _valid_graph_id(f"forged-{original.kind.value}")
        nodes = [
            node.model_copy(update={"node_id": forged_id})
            if node.node_id == original.node_id
            else node
            for node in artifact.nodes
        ]
        edges = [
            edge.model_copy(
                update={
                    "edge_id": _valid_graph_id(f"forged-edge:{index}"),
                    "source_id": (
                        forged_id if edge.source_id == original.node_id else edge.source_id
                    ),
                    "target_id": (
                        forged_id if edge.target_id == original.node_id else edge.target_id
                    ),
                }
            )
            if original.node_id in {edge.source_id, edge.target_id}
            else edge
            for index, edge in enumerate(artifact.edges)
        ]
        mutated = artifact.model_copy(update={"nodes": nodes, "edges": edges})
        return mutated.model_copy(
            update={
                "projection": mutated.projection.model_copy(
                    update={"graph_digest": _graph_digest(mutated)}
                )
            }
        )
