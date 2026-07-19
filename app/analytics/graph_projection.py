"""Project enriched conversations into a privacy-preserving relationship graph."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.graph_identity import graph_id
from app.analytics.opaque_refs import (
    account_ref,
    validated_account_ref,
    conversation_ref,
    message_ref,
    participant_ref,
)
from app.models.analytics import (
    CanonicalConversation,
    ConversationMetrics,
    GraphEdge,
    GraphNode,
    GraphNodeKind,
    GraphProperty,
    GraphProjectionSummary,
    GraphRelation,
    MessageDirection,
    MessageEnrichment,
)


def stable_node_id(
    partition_key: str, kind: GraphNodeKind, external_key: str
) -> str:
    """Return a fixed-format graph-node reference."""

    return graph_id(validated_account_ref(partition_key), kind.value, external_key)


def stable_edge_id(
    partition_key: str,
    relation: GraphRelation,
    source_id: str,
    target_id: str,
    qualifier: str = "",
) -> str:
    return graph_id(
        validated_account_ref(partition_key),
        "edge",
        relation.value,
        source_id,
        target_id,
        qualifier,
    )


class RelationshipGraphProjector:
    """Build message-level temporal and relationship-dynamics graph records."""

    def project(
        self,
        creator_account_id: str,
        source_revision: int,
        conversations: list[CanonicalConversation],
        enrichments: list[MessageEnrichment],
        metrics: list[ConversationMetrics],
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> tuple[list[GraphNode], list[GraphEdge], GraphProjectionSummary]:
        check_cancelled(cancellation_check)
        partition_ref = account_ref(creator_account_id)
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        metrics_by_conversation = {
            item.conversation_ref: item for item in metrics
        }
        enrichments_by_conversation: dict[
            str, list[MessageEnrichment]
        ] = defaultdict(list)
        for enrichment in enrichments:
            check_cancelled(cancellation_check)
            enrichments_by_conversation[enrichment.conversation_ref].append(
                enrichment
            )

        creator_node_id = stable_node_id(
            partition_ref, GraphNodeKind.PARTICIPANT, partition_ref
        )
        nodes[creator_node_id] = GraphNode(
            node_id=creator_node_id,
            account_ref=partition_ref,
            kind=GraphNodeKind.PARTICIPANT,
            properties={"role": "creator"},
        )

        conversations_by_participant: dict[
            str, list[tuple[CanonicalConversation, ConversationMetrics]]
        ] = defaultdict(list)
        for conversation in sorted(
            conversations, key=lambda item: item.conversation_id
        ):
            check_cancelled(cancellation_check)
            conversation_opaque_ref = conversation_ref(
                creator_account_id, conversation.conversation_id
            )
            participant_opaque_ref = participant_ref(
                creator_account_id, conversation.platform_user_id
            )
            conversation_metrics = metrics_by_conversation[
                conversation_opaque_ref
            ]
            conversations_by_participant[participant_opaque_ref].append(
                (conversation, conversation_metrics)
            )
            participant_node_id = stable_node_id(
                partition_ref,
                GraphNodeKind.PARTICIPANT,
                participant_opaque_ref,
            )
            nodes.setdefault(
                participant_node_id,
                GraphNode(
                    node_id=participant_node_id,
                    account_ref=partition_ref,
                    kind=GraphNodeKind.PARTICIPANT,
                    properties={"role": "counterpart"},
                ),
            )

            conversation_node_id = stable_node_id(
                partition_ref,
                GraphNodeKind.CONVERSATION,
                conversation_opaque_ref,
            )
            nodes[conversation_node_id] = GraphNode(
                node_id=conversation_node_id,
                account_ref=partition_ref,
                kind=GraphNodeKind.CONVERSATION,
                occurred_at=conversation_metrics.started_at,
                properties={
                    "message_count": conversation_metrics.message_count,
                    "turn_count": conversation_metrics.turn_count,
                    "average_sentiment_score": (
                        conversation_metrics.average_sentiment_score
                    ),
                    "response_coverage": conversation_metrics.response_coverage,
                },
            )
            self._edge(
                edges,
                partition_ref,
                GraphRelation.PARTICIPATES_IN,
                creator_node_id,
                conversation_node_id,
                qualifier="creator",
                properties={"role": "creator"},
            )
            self._edge(
                edges,
                partition_ref,
                GraphRelation.PARTICIPATES_IN,
                participant_node_id,
                conversation_node_id,
                qualifier="counterpart",
                properties={"role": "counterpart"},
            )

            message_inputs = {
                message_ref(
                    creator_account_id,
                    conversation.conversation_id,
                    item.message_id,
                ): item
                for item in conversation.messages
            }
            ordered_enrichments = sorted(
                enrichments_by_conversation[conversation_opaque_ref],
                key=lambda item: (item.sent_at, item.source_ordinal),
            )
            previous_message_node_id: str | None = None
            previous_sent_at: datetime | None = None
            for sequence, enrichment in enumerate(ordered_enrichments):
                check_cancelled(cancellation_check)
                raw_message = message_inputs[enrichment.message_ref]
                message_node_id = stable_node_id(
                    partition_ref,
                    GraphNodeKind.MESSAGE,
                    enrichment.message_ref,
                )
                nodes[message_node_id] = GraphNode(
                    node_id=message_node_id,
                    account_ref=partition_ref,
                    kind=GraphNodeKind.MESSAGE,
                    occurred_at=enrichment.sent_at,
                    properties={
                        "direction": enrichment.direction.value,
                        "source_ordinal": enrichment.source_ordinal,
                        "character_count": len(raw_message.text),
                    },
                )
                self._edge(
                    edges,
                    partition_ref,
                    GraphRelation.CONTAINS,
                    conversation_node_id,
                    message_node_id,
                    qualifier=enrichment.message_ref,
                    occurred_at=enrichment.sent_at,
                    sequence=sequence,
                )
                actor_id = (
                    participant_node_id
                    if enrichment.direction == MessageDirection.INBOUND
                    else creator_node_id
                )
                recipient_id = (
                    creator_node_id
                    if enrichment.direction == MessageDirection.INBOUND
                    else participant_node_id
                )
                self._edge(
                    edges,
                    partition_ref,
                    GraphRelation.SENT,
                    actor_id,
                    message_node_id,
                    qualifier=enrichment.message_ref,
                    occurred_at=enrichment.sent_at,
                    sequence=sequence,
                )
                self._edge(
                    edges,
                    partition_ref,
                    GraphRelation.RECEIVED_BY,
                    message_node_id,
                    recipient_id,
                    qualifier=enrichment.message_ref,
                    occurred_at=enrichment.sent_at,
                    sequence=sequence,
                )

                affect_node_id = stable_node_id(
                    partition_ref,
                    GraphNodeKind.AFFECT_STATE,
                    enrichment.message_ref,
                )
                nodes[affect_node_id] = GraphNode(
                    node_id=affect_node_id,
                    account_ref=partition_ref,
                    kind=GraphNodeKind.AFFECT_STATE,
                    occurred_at=enrichment.sent_at,
                    properties={
                        "label": enrichment.sentiment.label.value,
                        "score": enrichment.sentiment.score,
                        "confidence": enrichment.sentiment.confidence,
                    },
                )
                self._edge(
                    edges,
                    partition_ref,
                    GraphRelation.EXPRESSES_AFFECT,
                    message_node_id,
                    affect_node_id,
                    qualifier=enrichment.message_ref,
                    occurred_at=enrichment.sent_at,
                    sequence=sequence,
                )

                engagement_node_id = stable_node_id(
                    partition_ref,
                    GraphNodeKind.ENGAGEMENT_STATE,
                    enrichment.message_ref,
                )
                nodes[engagement_node_id] = GraphNode(
                    node_id=engagement_node_id,
                    account_ref=partition_ref,
                    kind=GraphNodeKind.ENGAGEMENT_STATE,
                    occurred_at=enrichment.sent_at,
                    properties={
                        "state": enrichment.engagement.state.value,
                        "confidence": enrichment.engagement.confidence,
                    },
                )
                self._edge(
                    edges,
                    partition_ref,
                    GraphRelation.HAS_ENGAGEMENT_STATE,
                    message_node_id,
                    engagement_node_id,
                    qualifier=enrichment.message_ref,
                    occurred_at=enrichment.sent_at,
                    sequence=sequence,
                )

                for topic in enrichment.topic_entities.topics:
                    topic_node_id = stable_node_id(
                        partition_ref, GraphNodeKind.TOPIC, topic.topic_ref
                    )
                    nodes.setdefault(
                        topic_node_id,
                        GraphNode(
                            node_id=topic_node_id,
                            account_ref=partition_ref,
                            kind=GraphNodeKind.TOPIC,
                            properties={
                                "taxonomy_id": topic.taxonomy_id,
                                "label": topic.label,
                            },
                        ),
                    )
                    self._edge(
                        edges,
                        partition_ref,
                        GraphRelation.MENTIONS_TOPIC,
                        message_node_id,
                        topic_node_id,
                        qualifier=topic.topic_ref,
                        occurred_at=enrichment.sent_at,
                        sequence=sequence,
                        properties={"confidence": topic.confidence},
                    )

                for entity in enrichment.topic_entities.entities:
                    entity_node_id = stable_node_id(
                        partition_ref,
                        GraphNodeKind.ENTITY,
                        entity.entity_ref,
                    )
                    nodes.setdefault(
                        entity_node_id,
                        GraphNode(
                            node_id=entity_node_id,
                            account_ref=partition_ref,
                            kind=GraphNodeKind.ENTITY,
                            properties={
                                "entity_type": entity.entity_type.value,
                                "entity_ref": entity.entity_ref,
                            },
                        ),
                    )
                    self._edge(
                        edges,
                        partition_ref,
                        GraphRelation.MENTIONS_ENTITY,
                        message_node_id,
                        entity_node_id,
                        qualifier=entity.entity_ref,
                        occurred_at=enrichment.sent_at,
                        sequence=sequence,
                        properties={"confidence": entity.confidence},
                    )

                if (
                    previous_message_node_id is not None
                    and previous_sent_at is not None
                ):
                    interval = max(
                        0.0,
                        (enrichment.sent_at - previous_sent_at).total_seconds(),
                    )
                    self._edge(
                        edges,
                        partition_ref,
                        GraphRelation.PRECEDES,
                        previous_message_node_id,
                        message_node_id,
                        qualifier="message",
                        occurred_at=enrichment.sent_at,
                        sequence=sequence - 1,
                        properties={
                            "scope": "message",
                            "interval_seconds": round(interval, 6),
                        },
                    )
                previous_message_node_id = message_node_id
                previous_sent_at = enrichment.sent_at

        for participant_opaque_ref, items in sorted(
            conversations_by_participant.items()
        ):
            check_cancelled(cancellation_check)
            ordered_items = sorted(
                items,
                key=lambda item: (
                    item[1].started_at is None,
                    item[1].started_at,
                    item[1].conversation_ref,
                ),
            )
            for left, right in zip(ordered_items, ordered_items[1:]):
                left_conversation, left_metrics = left
                right_conversation, right_metrics = right
                left_id = stable_node_id(
                    partition_ref,
                    GraphNodeKind.CONVERSATION,
                    conversation_ref(
                        creator_account_id, left_conversation.conversation_id
                    ),
                )
                right_id = stable_node_id(
                    partition_ref,
                    GraphNodeKind.CONVERSATION,
                    conversation_ref(
                        creator_account_id, right_conversation.conversation_id
                    ),
                )
                interval = None
                if left_metrics.ended_at and right_metrics.started_at:
                    interval = max(
                        0.0,
                        (
                            right_metrics.started_at - left_metrics.ended_at
                        ).total_seconds(),
                    )
                properties: dict[str, GraphProperty] = {
                    "scope": "conversation"
                }
                if interval is not None:
                    properties["interval_seconds"] = round(interval, 6)
                self._edge(
                    edges,
                    partition_ref,
                    GraphRelation.PRECEDES,
                    left_id,
                    right_id,
                    qualifier=participant_opaque_ref,
                    occurred_at=right_metrics.started_at,
                    properties=properties,
                )

        ordered_nodes = [nodes[key] for key in sorted(nodes)]
        ordered_edges = [edges[key] for key in sorted(edges)]
        check_cancelled(cancellation_check)
        node_counts = Counter(node.kind.value for node in ordered_nodes)
        edge_counts = Counter(edge.relation.value for edge in ordered_edges)
        summary = GraphProjectionSummary(
            account_ref=partition_ref,
            source_revision=source_revision,
            node_count=len(ordered_nodes),
            edge_count=len(ordered_edges),
            node_counts_by_kind={
                key: node_counts[key] for key in sorted(node_counts)
            },
            edge_counts_by_relation={
                key: edge_counts[key] for key in sorted(edge_counts)
            },
        )
        return ordered_nodes, ordered_edges, summary

    @staticmethod
    def _edge(
        edges: dict[str, GraphEdge],
        partition_key: str,
        relation: GraphRelation,
        source_id: str,
        target_id: str,
        *,
        qualifier: str = "",
        occurred_at: datetime | None = None,
        sequence: int | None = None,
        properties: dict[str, GraphProperty] | None = None,
    ) -> None:
        edge_id = stable_edge_id(
            partition_key, relation, source_id, target_id, qualifier
        )
        edge = GraphEdge(
            edge_id=edge_id,
            account_ref=partition_key,
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            occurred_at=occurred_at,
            sequence=sequence,
            properties=properties or {},
        )
        existing = edges.get(edge_id)
        if existing is not None and existing != edge:
            raise ValueError("graph_edge_identity_collision")
        edges[edge_id] = edge
