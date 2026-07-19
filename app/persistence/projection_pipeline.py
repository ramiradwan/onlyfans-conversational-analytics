"""Deterministic in-process NLP and property-graph projection seam.

The canonical store deliberately carries no acquisition-origin semantics into
the read model.  Every canonical message reaches this pipeline in the same
shape, whether it was observed passively or acquired by the signer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, Protocol, Sequence


Sentiment = Literal["positive", "neutral", "negative", "unknown"]
AnalysisStatus = Literal["available", "unavailable"]


def _document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _identifier(kind: str, *parts: str) -> str:
    material = _document([kind, *parts]).encode("utf-8")
    return f"{kind}:sha256:{hashlib.sha256(material).hexdigest()}"


def _source_hash(message: "CanonicalProjectionMessage") -> str:
    material = {
        "conversation_id": message.conversation_id,
        "direction": message.direction,
        "message_id": message.message_id,
        "sender_platform_user_id": message.sender_platform_user_id,
        "sent_at": message.sent_at,
        "text": message.text,
    }
    return "sha256:" + hashlib.sha256(_document(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalProjectionConversation:
    conversation_id: str
    platform_user_id: str | None
    display_name: str | None


@dataclass(frozen=True, slots=True)
class CanonicalProjectionMessage:
    conversation_id: str
    message_id: str
    sender_platform_user_id: str
    text: str
    sent_at: str
    direction: Literal["inbound", "outbound"]


@dataclass(frozen=True, slots=True)
class MessageAnalysisProjection:
    conversation_id: str
    message_id: str
    source_hash: str
    status: AnalysisStatus
    sentiment: Sentiment
    analyzer_id: str | None
    document_json: str


@dataclass(frozen=True, slots=True)
class LpgNodeProjection:
    conversation_id: str
    node_id: str
    node_kind: Literal["conversation", "message"]
    entity_id: str
    document_json: str


@dataclass(frozen=True, slots=True)
class LpgEdgeProjection:
    conversation_id: str
    edge_id: str
    source_node_id: str
    target_node_id: str
    relationship: Literal["CONTAINS"]
    document_json: str


@dataclass(frozen=True, slots=True)
class ProjectionBatch:
    analyses: tuple[MessageAnalysisProjection, ...]
    nodes: tuple[LpgNodeProjection, ...]
    edges: tuple[LpgEdgeProjection, ...]


class ProjectionPipeline(Protocol):
    """Replaceable deterministic projection boundary for local model builds."""

    pipeline_version: str

    def project(
        self,
        conversations: Sequence[CanonicalProjectionConversation],
        messages: Sequence[CanonicalProjectionMessage],
    ) -> ProjectionBatch: ...


class DeterministicProjectionPipeline:
    """Build durable LPG material and explicit no-model NLP results.

    A configured local NLP implementation can replace this object through the
    ``ProjectionPipeline`` seam.  The default never invents sentiment: it
    records a deterministic ``unavailable`` result with ``unknown`` sentiment.
    """

    pipeline_version = "deterministic-local-v1"

    def project(
        self,
        conversations: Sequence[CanonicalProjectionConversation],
        messages: Sequence[CanonicalProjectionMessage],
    ) -> ProjectionBatch:
        analyses: list[MessageAnalysisProjection] = []
        nodes: list[LpgNodeProjection] = []
        edges: list[LpgEdgeProjection] = []
        conversation_nodes: dict[str, str] = {}

        for conversation in sorted(conversations, key=lambda item: item.conversation_id):
            node_id = _identifier("conversation", conversation.conversation_id)
            conversation_nodes[conversation.conversation_id] = node_id
            nodes.append(
                LpgNodeProjection(
                    conversation_id=conversation.conversation_id,
                    node_id=node_id,
                    node_kind="conversation",
                    entity_id=conversation.conversation_id,
                    document_json=_document(
                        {
                            "display_name": conversation.display_name,
                            "entity_id": conversation.conversation_id,
                            "node_kind": "conversation",
                            "pipeline_version": self.pipeline_version,
                            "platform_user_id": conversation.platform_user_id,
                        }
                    ),
                )
            )

        for message in sorted(messages, key=lambda item: item.message_id):
            analysis_document = {
                "analyzer_id": None,
                "pipeline_version": self.pipeline_version,
                "reason": "model_not_configured",
                "sentiment": "unknown",
                "status": "unavailable",
                "topics": [],
            }
            analyses.append(
                MessageAnalysisProjection(
                    conversation_id=message.conversation_id,
                    message_id=message.message_id,
                    source_hash=_source_hash(message),
                    status="unavailable",
                    sentiment="unknown",
                    analyzer_id=None,
                    document_json=_document(analysis_document),
                )
            )
            message_node_id = _identifier("message", message.message_id)
            nodes.append(
                LpgNodeProjection(
                    conversation_id=message.conversation_id,
                    node_id=message_node_id,
                    node_kind="message",
                    entity_id=message.message_id,
                    document_json=_document(
                        {
                            "analysis_status": "unavailable",
                            "conversation_id": message.conversation_id,
                            "direction": message.direction,
                            "entity_id": message.message_id,
                            "node_kind": "message",
                            "pipeline_version": self.pipeline_version,
                            "sent_at": message.sent_at,
                            "sentiment": "unknown",
                        }
                    ),
                )
            )
            conversation_node_id = conversation_nodes[message.conversation_id]
            relationship = "CONTAINS"
            edges.append(
                LpgEdgeProjection(
                    conversation_id=message.conversation_id,
                    edge_id=_identifier(
                        "edge", conversation_node_id, relationship, message_node_id
                    ),
                    source_node_id=conversation_node_id,
                    target_node_id=message_node_id,
                    relationship=relationship,
                    document_json=_document(
                        {
                            "pipeline_version": self.pipeline_version,
                            "relationship": relationship,
                        }
                    ),
                )
            )

        return ProjectionBatch(tuple(analyses), tuple(nodes), tuple(edges))
