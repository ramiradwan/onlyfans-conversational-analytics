"""Deterministic enrichment stage composed from the three analyzer ports."""

from __future__ import annotations

from statistics import mean

from app.analytics.analyzers import (
    EngagementAnalyzer,
    RuleBasedEngagementAnalyzer,
    RuleBasedSentimentAnalyzer,
    RuleBasedTopicEntityAnalyzer,
    SentimentAnalyzer,
    TopicEntityAnalyzer,
)
from app.analytics.cancellation import CancellationCheck, check_cancelled
from app.analytics.errors import AnalyzerConfigurationInvalid
from app.analytics.enrichment_inputs import analyze_messages, analyzer_policy
from app.analytics.provenance import stable_config_digest
from app.analytics.opaque_refs import (
    account_ref,
    conversation_ref,
    message_ref,
    participant_ref,
)
from app.models.analytics import (
    AnalyzerProvenance,
    CanonicalConversation,
    MessageEnrichment,
)


class EnrichmentStage:
    """Run independent analyzers over canonical messages in stable order."""

    def __init__(
        self,
        *,
        sentiment: SentimentAnalyzer | None = None,
        topics_entities: TopicEntityAnalyzer | None = None,
        engagement: EngagementAnalyzer | None = None,
    ) -> None:
        self.sentiment = sentiment or RuleBasedSentimentAnalyzer()
        self.topics_entities = topics_entities or RuleBasedTopicEntityAnalyzer()
        self.engagement = engagement or RuleBasedEngagementAnalyzer()
        self._input_policies = tuple(analyzer_policy(a) for a in (self.sentiment, self.topics_entities, self.engagement))
        self._descriptors = tuple(
            self._descriptor(analyzer)
            for analyzer in (
                self.sentiment,
                self.topics_entities,
                self.engagement,
            )
        )

    @property
    def revision(self) -> str:
        return "+".join(
            f"{item.analyzer_name}@{item.revision}" for item in self._descriptors
        )

    @property
    def config_digest(self) -> str:
        return stable_config_digest(
            name="analytics_enrichment_stage",
            revision=self.revision,
            config=[
                {
                    "name": item.analyzer_name,
                    "revision": item.revision,
                    "config_digest": item.config_digest,
                    "mode": item.mode.value,
                    "calibration_status": item.calibration_status.value,
                    "input_policy": (policy.model_dump(mode="json") if policy else None),
                }
                for item, policy in zip(self._descriptors,
                    self._input_policies, strict=True)
            ],
        )

    @staticmethod
    def _descriptor(analyzer: object) -> AnalyzerProvenance:
        try:
            return AnalyzerProvenance(
                analyzer_name=getattr(analyzer, "name"),
                revision=getattr(analyzer, "revision"),
                config_digest=getattr(analyzer, "config_digest"),
                mode=getattr(analyzer, "mode"),
                calibration_status=getattr(analyzer, "calibration_status"),
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise AnalyzerConfigurationInvalid() from error

    def provenance(
        self, enrichments: list[MessageEnrichment]
    ) -> list[AnalyzerProvenance]:
        """Attach deterministic coverage and meaningful confidence summaries."""

        eligible = len(enrichments)
        confidence_sets = (
            [item.sentiment.confidence for item in enrichments],
            [
                topic.confidence
                for item in enrichments
                for topic in item.topic_entities.topics
            ],
            [item.engagement.confidence for item in enrichments],
        )
        return [
            descriptor.model_copy(
                update={
                    "analyzed_sample_count": eligible,
                    "eligible_sample_count": eligible,
                    "sample_coverage": 1.0 if eligible else None,
                    "mean_confidence": (
                        round(mean(confidences), 6) if confidences else None
                    ),
                    "unavailable_reason": (
                        None if eligible else "no_eligible_samples"
                    ),
                }
            )
            for descriptor, confidences in zip(
                self._descriptors, confidence_sets, strict=True
            )
        ]

    def enrich_conversation(
        self,
        creator_account_id: str,
        conversation: CanonicalConversation,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> list[MessageEnrichment]:
        check_cancelled(cancellation_check)
        ordered = sorted(
            conversation.messages,
            key=lambda message: (message.sent_at, message.source_ordinal),
        )
        results: list[MessageEnrichment] = []
        analyzers = (self.sentiment, self.topics_entities, self.engagement)
        if (tuple(self._descriptor(item) for item in analyzers) != self._descriptors
                or tuple(analyzer_policy(item) for item in analyzers) != self._input_policies):
            raise AnalyzerConfigurationInvalid()
        for message, outputs in analyze_messages(
            creator_account_id, conversation, ordered, analyzers,
            self._descriptors, cancellation_check,
        ):
            sentiment_result, topic_entity_result, engagement_result = outputs
            results.append(
                MessageEnrichment(
                    account_ref=account_ref(creator_account_id),
                    conversation_ref=conversation_ref(
                        creator_account_id, conversation.conversation_id
                    ),
                    participant_ref=participant_ref(
                        creator_account_id, conversation.platform_user_id
                    ),
                    message_ref=message_ref(
                        creator_account_id,
                        conversation.conversation_id,
                        message.message_id,
                    ),
                    source_ordinal=message.source_ordinal,
                    sent_at=message.sent_at,
                    direction=message.direction,
                    sentiment=sentiment_result,
                    topic_entities=topic_entity_result,
                    engagement=engagement_result,
                )
            )
        check_cancelled(cancellation_check)
        return results
