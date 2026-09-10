"""Independent semantic comparison vocabulary for Task 6A rebuilds.

This module deliberately does not call the pipeline's digest or comparison
helpers.  It classifies the public rebuild artifact according to the checked-in
rebuild-equivalence contract, then compares normalized semantic and provenance
surfaces directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from app.analytics.historical_derivation import (
    HISTORICAL_DERIVATION_SCHEMA,
    PARTICIPANT_ANALYTICS_MAX_DAYS,
    RETENTION_BASIS,
)
from app.models.analytics import RebuildArtifact
from app.transport.ingestion import AccountReadModel


class AnalyticsOracleMismatch(AssertionError):
    """A directly classified Task 6A semantic/provenance mismatch."""


@dataclass(frozen=True, slots=True)
class ReproducibilityContext:
    """Closed context actually consumed or witnessed by a Task 6A build."""

    canonical_account_ref: str
    canonical_view_revision: int
    canonical_content_digest: str
    pipeline_revision: str
    pipeline_config_digest: str
    analyzer_provenance: tuple[tuple[str, str, str], ...]
    graph_schema_version: str
    retention_policy: tuple[int, str, str]
    evaluation_clock: datetime
    deterministic_seed: int | None = None
    external_enrichment_provenance: tuple[()] = ()

    def json_safe(self) -> dict[str, Any]:
        document = asdict(self)
        document["evaluation_clock"] = self.evaluation_clock.astimezone(timezone.utc).isoformat()
        return document


def context_for(
    creator_account_id: str,
    account: AccountReadModel,
    *,
    pipeline_revision: str,
    pipeline_config_digest: str,
    analyzer_provenance: tuple[tuple[str, str, str], ...],
    evaluation_clock: datetime,
    deterministic_seed: int | None,
) -> ReproducibilityContext:
    """Record every Task 6 contract input for one frozen clean rebuild."""

    if evaluation_clock.tzinfo is None or evaluation_clock.utcoffset() is None:
        raise ValueError("evaluation_clock must be timezone-aware")
    canonical_content_digest = _canonical_content_digest(account)
    return ReproducibilityContext(
        canonical_account_ref=_opaque_ref("account", creator_account_id),
        canonical_view_revision=account.view_revision,
        canonical_content_digest=canonical_content_digest,
        pipeline_revision=pipeline_revision,
        pipeline_config_digest=pipeline_config_digest,
        analyzer_provenance=tuple(sorted(analyzer_provenance)),
        graph_schema_version="relationship_graph.v1",
        retention_policy=(
            PARTICIPANT_ANALYTICS_MAX_DAYS,
            RETENTION_BASIS,
            HISTORICAL_DERIVATION_SCHEMA,
        ),
        evaluation_clock=evaluation_clock.astimezone(timezone.utc),
        deterministic_seed=deterministic_seed,
    )


def _canonical_content_digest(account: AccountReadModel) -> str:
    """Independent serialization of the documented canonical witness format."""

    encoded = json.dumps(
        {"view_revision": account.view_revision, "conversations": account.conversations},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(b"ofca:canonical-account:v1\0" + encoded).hexdigest()


def _opaque_ref(domain: str, *identity_parts: str) -> str:
    """Independent domain-separated opaque-reference calculation for the oracle."""

    prefixes = {
        "account": "a1",
        "conversation": "c1",
        "participant": "p1",
        "message": "m1",
    }
    digest = hashlib.sha256(b"ofca:analytics-ref:v1\0")
    encoded_domain = domain.encode("ascii")
    digest.update(len(encoded_domain).to_bytes(2, "big"))
    digest.update(encoded_domain)
    for part in identity_parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{prefixes[domain]}:{digest.hexdigest()}"


def _model(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _sorted_models(values: list[Any], *keys: str) -> list[dict[str, Any]]:
    return [_model(value) for value in sorted(values, key=lambda value: tuple(getattr(value, key) for key in keys))]


def normalize_artifact(artifact: RebuildArtifact) -> dict[str, Any]:
    """Return all supported Task 6A fields, excluding only lifecycle fields.

    `projection_generation`, publication epochs, staged generation ids, lease
    ownership, and execution timestamps are intentionally absent: none are part
    of `RebuildArtifact` semantic output.  `projection_digest` is normalized
    with its lifecycle generation removed, matching the contract's composite
    digest rule.
    """

    projection = artifact.projection
    projection_document = _model(projection)
    projection_document.pop("projection_generation", None)
    projection_document.pop("projection_digest", None)
    return {
        "canonical_witness": {
            "account_ref": projection.account_ref,
            "source_revision": projection.source_revision,
            "canonical_content_digest": projection.canonical_content_digest,
        },
        "pipeline_provenance": {
            "schema_version": projection.schema_version,
            "pipeline_revision": projection.pipeline_revision,
            "pipeline_config_digest": projection.pipeline_config_digest,
            "pipeline_identity_digest": projection.pipeline_identity_digest,
            "analyzers": _sorted_models(projection.analyzers, "analyzer_name"),
        },
        "semantic_projection": {
            "availability": projection.availability.value,
            "window": _model(projection.window),
            "message_enrichments": _sorted_models(
                projection.message_enrichments, "conversation_ref", "source_ordinal"
            ),
            "conversation_metrics": _sorted_models(
                projection.conversation_metrics, "conversation_ref"
            ),
            "creator_metrics": _model(projection.creator_metrics),
            "graph": _model(projection.graph),
            "graph_digest": projection.graph_digest,
            # Fresh Task 6A stores both allocate generation 1, so the
            # composite digest is comparable here.  Task 6B must instead use
            # projection_without_lifecycle when generations may differ.
            "projection_digest": projection.projection_digest,
            "projection_without_lifecycle": projection_document,
        },
        "graph": {
            "nodes": _sorted_models(artifact.nodes, "node_id"),
            "edges": _sorted_models(artifact.edges, "edge_id"),
        },
    }


def _assert_context(artifact: RebuildArtifact, context: ReproducibilityContext) -> None:
    projection = artifact.projection
    actual_analyzers = tuple(
        sorted(
            (item.analyzer_name, item.revision, item.config_digest)
            for item in projection.analyzers
        )
    )
    expected = {
        "account_ref": context.canonical_account_ref,
        "source_revision": context.canonical_view_revision,
        "canonical_content_digest": context.canonical_content_digest,
        "pipeline_revision": context.pipeline_revision,
        "pipeline_config_digest": context.pipeline_config_digest,
        "analyzer_provenance": context.analyzer_provenance,
    }
    observed = {
        "account_ref": projection.account_ref,
        "source_revision": projection.source_revision,
        "canonical_content_digest": projection.canonical_content_digest,
        "pipeline_revision": projection.pipeline_revision,
        "pipeline_config_digest": projection.pipeline_config_digest,
        "analyzer_provenance": actual_analyzers,
    }
    if observed != expected:
        raise AnalyticsOracleMismatch(
            f"reproducibility context mismatch: expected={expected!r}, observed={observed!r}"
        )


def _assert_referential_closure(normalized: dict[str, Any]) -> None:
    node_ids = {node["node_id"] for node in normalized["graph"]["nodes"]}
    dangling = [
        edge["edge_id"]
        for edge in normalized["graph"]["edges"]
        if edge["source_id"] not in node_ids or edge["target_id"] not in node_ids
    ]
    if dangling:
        raise AnalyticsOracleMismatch(f"derived referential closure failed for edges {dangling!r}")


def _assert_graph_digest(artifact: RebuildArtifact) -> None:
    """Independently bind the graph digest to normalized public node/edge rows."""

    value = {
        "nodes": [_model(node) for node in sorted(artifact.nodes, key=lambda item: item.node_id)],
        "edges": [_model(edge) for edge in sorted(artifact.edges, key=lambda item: item.edge_id)],
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if artifact.projection.graph_digest != expected:
        raise AnalyticsOracleMismatch(
            f"graph digest mismatch: expected={expected!r}, observed={artifact.projection.graph_digest!r}"
        )


def expected_active_refs(
    creator_account_id: str, account: AccountReadModel, context: ReproducibilityContext
) -> dict[str, set[str]]:
    """Derive active canonical identity expectations without production helpers."""

    cutoff = context.evaluation_clock - timedelta(days=context.retention_policy[0])
    conversations: set[str] = set()
    participants: set[str] = set()
    messages: set[str] = set()
    for conversation_id, conversation in account.conversations.items():
        active_messages = [
            message
            for message in conversation["messages"]
            if datetime.fromisoformat(str(message["sent_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            > cutoff
        ]
        if not active_messages:
            continue
        participant_id = str(conversation["platform_user_id"])
        conversations.add(_opaque_ref("conversation", creator_account_id, conversation_id))
        participants.add(_opaque_ref("participant", creator_account_id, participant_id))
        messages.update(
            _opaque_ref("message", creator_account_id, conversation_id, str(message["message_id"]))
            for message in active_messages
        )
    return {"conversations": conversations, "participants": participants, "messages": messages}


def _assert_canonical_identity_mapping(
    artifact: RebuildArtifact, expected_refs: dict[str, set[str]]
) -> None:
    projection = artifact.projection
    actual_messages = {item.message_ref for item in projection.message_enrichments}
    actual_conversations = {item.conversation_ref for item in projection.conversation_metrics}
    actual_participants = {item.participant_ref for item in projection.conversation_metrics}
    actual_participants.update(item.participant_ref for item in projection.message_enrichments)
    actual = {
        "conversations": actual_conversations,
        "participants": actual_participants,
        "messages": actual_messages,
    }
    if actual != expected_refs:
        raise AnalyticsOracleMismatch(
            f"canonical opaque identity mapping mismatch: expected={expected_refs!r}, observed={actual!r}"
        )


def assert_deterministic_rebuilds(
    first: RebuildArtifact,
    second: RebuildArtifact,
    context: ReproducibilityContext,
    *,
    expected_refs: dict[str, set[str]] | None = None,
) -> None:
    """Assert the full supported 6A matrix across two fresh clean builds."""

    _assert_context(first, context)
    _assert_context(second, context)
    if expected_refs is not None:
        _assert_canonical_identity_mapping(first, expected_refs)
        _assert_canonical_identity_mapping(second, expected_refs)
    left = normalize_artifact(first)
    right = normalize_artifact(second)
    _assert_referential_closure(left)
    _assert_referential_closure(right)
    _assert_graph_digest(first)
    _assert_graph_digest(second)
    for field in ("canonical_witness", "pipeline_provenance", "semantic_projection", "graph"):
        if left[field] != right[field]:
            raise AnalyticsOracleMismatch(
                f"Task 6A mismatch in {field}: left={left[field]!r}, right={right[field]!r}"
            )


def reproduction_payload(
    *,
    creator_account_id: str,
    account: AccountReadModel,
    context: ReproducibilityContext,
    first: RebuildArtifact,
    second: RebuildArtifact,
) -> dict[str, Any]:
    """JSON-safe permanent-reproducer material, independent of Hypothesis cache."""

    return {
        "creator_account_id": creator_account_id,
        "canonical_input": {
            "view_revision": account.view_revision,
            "conversations": account.conversations,
        },
        "reproducibility_context": context.json_safe(),
        "expected_semantic_provenance_shape": normalize_artifact(first),
        "actual_semantic_provenance_shape": normalize_artifact(second),
    }
