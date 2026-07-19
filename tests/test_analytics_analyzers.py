from __future__ import annotations

from datetime import datetime, timezone

from app.analytics.analyzers import (
    RuleBasedEngagementAnalyzer,
    RuleBasedSentimentAnalyzer,
    RuleBasedTopicEntityAnalyzer,
)
from app.analytics.enrichment import EnrichmentStage
from app.analytics.opaque_refs import message_ref
from app.models.analytics import (
    CanonicalConversation,
    CanonicalMessage,
    EngagementState,
    EntityType,
    MessageAnalysisInput,
    MessageDirection,
    SentimentLabel,
)


NOW = datetime(2026, 7, 10, 9, tzinfo=timezone.utc)


def analysis_input(text: str) -> MessageAnalysisInput:
    return MessageAnalysisInput(
        creator_account_id="synthetic-creator",
        conversation_id="synthetic-conversation",
        participant_id="synthetic-participant",
        message_id="synthetic-message",
        text=text,
        sent_at=NOW,
        direction=MessageDirection.INBOUND,
    )


def test_rule_analyzers_are_repeatable_and_dependency_free() -> None:
    message = analysis_input(
        "Thanks, the upload issue is resolved. See @demo and "
        "https://example.invalid/file #sample for $20."
    )
    sentiment = RuleBasedSentimentAnalyzer()
    topics = RuleBasedTopicEntityAnalyzer()
    engagement = RuleBasedEngagementAnalyzer()

    assert sentiment.analyze(message) == sentiment.analyze(message)
    assert topics.analyze(message) == topics.analyze(message)
    assert engagement.analyze(message) == engagement.analyze(message)
    assert sentiment.analyze(message).label == SentimentLabel.POSITIVE
    assert {item.taxonomy_id for item in topics.analyze(message).topics} == {
        "feedback",
        "media",
        "support",
    }
    assert {item.entity_type for item in topics.analyze(message).entities} == {
        EntityType.AMOUNT,
        EntityType.HASHTAG,
        EntityType.MENTION,
        EntityType.URL,
    }
    assert engagement.analyze(message).state == EngagementState.TRANSACTIONAL


def test_sentiment_negation_and_neutral_fallback_are_explicit() -> None:
    analyzer = RuleBasedSentimentAnalyzer()

    negated = analyzer.analyze(analysis_input("This is not good."))
    neutral = analyzer.analyze(analysis_input("The file is available."))

    assert negated.label == SentimentLabel.NEGATIVE
    assert negated.evidence_count == 1
    assert neutral.label == SentimentLabel.NEUTRAL
    assert neutral.score == 0.0


def test_enrichment_stage_emits_one_result_per_canonical_message() -> None:
    conversation = CanonicalConversation(
        conversation_id="conversation-1",
        platform_user_id="participant-1",
        display_name="Participant One",
        messages=[
            CanonicalMessage(
                message_id="message-2",
                source_ordinal=1,
                text="I will send the helpful link.",
                sent_at=NOW.replace(minute=2),
                direction=MessageDirection.OUTBOUND,
            ),
            CanonicalMessage(
                message_id="message-1",
                source_ordinal=0,
                text="Could you help with the issue?",
                sent_at=NOW,
                direction=MessageDirection.INBOUND,
            ),
        ],
    )

    results = EnrichmentStage().enrich_conversation(
        "synthetic-creator", conversation
    )

    assert [item.message_ref for item in results] == [
        message_ref("synthetic-creator", "conversation-1", "message-1"),
        message_ref("synthetic-creator", "conversation-1", "message-2"),
    ]
    assert results[0].engagement.state == EngagementState.INQUIRY
    assert results[1].engagement.state == EngagementState.COMMITMENT
