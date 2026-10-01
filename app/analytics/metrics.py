"""Pure metric projection functions for enriched canonical conversations."""

from __future__ import annotations

from collections import Counter
from statistics import median
from datetime import datetime
from typing import NamedTuple

from app.analytics.provenance import stable_config_digest
from app.analytics.opaque_refs import account_ref, conversation_ref, participant_ref
from app.models.analytics import (
    AnalysisMode,
    AnalyticsWindow,
    CalibrationStatus,
    CanonicalConversation,
    ConversationMetrics,
    CreatorMetrics,
    MessageDirection,
    MessageEnrichment,
    MetricProvenance,
    WindowScope,
)


CONVERSATION_METRICS_REVISION = "conversation_metrics.baseline.v2"
CONVERSATION_METRICS_PROVENANCE = MetricProvenance(
    metric_name="conversation_metrics",
    revision=CONVERSATION_METRICS_REVISION,
    config_digest=stable_config_digest(
        name="conversation_metrics",
        revision=CONVERSATION_METRICS_REVISION,
        config={
            "response_pairing": "latest_inbound_then_first_outbound",
            "turn_boundary": "direction_change",
            "silence": "adjacent_message_gap",
        },
    ),
    mode=AnalysisMode.BASELINE,
    calibration_status=CalibrationStatus.NOT_CALIBRATED,
)
CREATOR_METRICS_REVISION = "creator_metrics.baseline.v2"
CREATOR_METRICS_PROVENANCE = MetricProvenance(
    metric_name="creator_metrics",
    revision=CREATOR_METRICS_REVISION,
    config_digest=stable_config_digest(
        name="creator_metrics",
        revision=CREATOR_METRICS_REVISION,
        config={
            "aggregation": "message_weighted_conversation_metrics",
            "participant_identity": "distinct_platform_user_id",
            "response_coverage": "responded_over_opportunities",
        },
    ),
    mode=AnalysisMode.BASELINE,
    calibration_status=CalibrationStatus.NOT_CALIBRATED,
)
PRIORITY_SCORE_REVISION = "priority_score.baseline.v1"
PRIORITY_SCORE_PROVENANCE = MetricProvenance(
    metric_name="priority_score",
    revision=PRIORITY_SCORE_REVISION,
    config_digest=stable_config_digest(
        name="priority_score",
        revision=PRIORITY_SCORE_REVISION,
        config={
            "unread_weight": 15.0,
            "unanswered_weight": 20.0,
            "negative_sentiment_weight": 30.0,
            "maximum": 100.0,
        },
    ),
    mode=AnalysisMode.BASELINE,
    calibration_status=CalibrationStatus.NOT_CALIBRATED,
)


def _ordered_counts(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


class ConversationMetricInput(NamedTuple):
    """Only the values used by the unchanged conversation metric calculation."""
    sent_at: datetime
    source_ordinal: int
    direction: MessageDirection
    sentiment_label: str
    sentiment_score: float
    topic_ids: tuple[str, ...]
    entity_types: tuple[str, ...]
    engagement_state: str

    @classmethod
    def from_enrichment(cls, item):
        return cls(item.sent_at, item.source_ordinal, item.direction,
            item.sentiment.label.value, item.sentiment.score,
            tuple(topic.taxonomy_id for topic in item.topic_entities.topics),
            tuple(entity.entity_type.value for entity in item.topic_entities.entities),
            item.engagement.state.value)



def _safe_appended_average(previous: float | None, count: int, added: float) -> float | None:
    """Return the exact six-decimal result only when the stored average proves it."""
    if previous is None or count <= 0:
        return None
    from decimal import Decimal, localcontext
    # The stored value is Python's six-decimal rounding of the previous float
    # average. Widen half a unit by 1e-9 to cover binary arithmetic at the tie.
    center = Decimal(str(previous))
    radius = Decimal('0.000000501')
    with localcontext() as context:
        context.prec = 50
        score = Decimal.from_float(float(added))
        low = ((center - radius) * count + score) / (count + 1)
        high = ((center + radius) * count + score) / (count + 1)
    rounded_low = round(float(low), 6)
    rounded_high = round(float(high), 6)
    return rounded_low if rounded_low == rounded_high else None


def append_conversation_metrics(previous, last, tail, unread_count, *,
                                previous_sentiment_total=None,
                                sentiment_total_factory=None):
    """Update an exact append only when no historical response sample changes.

    An outbound message after an inbound message creates a new response-time
    sample whose median cannot be recovered from the public aggregate. That
    case deliberately returns None and uses the complete existing calculation.
    """
    from app.models.analytics import MessageDirection
    if (previous.message_count <= 0 or previous.ended_at != last.sent_at
            or previous.unread_count != unread_count
            or tail.sent_at < last.sent_at
            or tail.account_ref != previous.account_ref
            or tail.conversation_ref != previous.conversation_ref
            or tail.participant_ref != previous.participant_ref
            or (last.direction == MessageDirection.INBOUND
                and tail.direction == MessageDirection.OUTBOUND)):
        return None
    # The exact previous average need not be reconstructed when both endpoints
    # are in the same six-decimal rounding bin: their weighted average stays in
    # that bin. Otherwise recover the exact historical float sum only after all
    # cheaper append-admission checks have passed.
    average_sentiment = None
    if (previous.average_sentiment_score is not None
            and float(tail.sentiment.score) == previous.average_sentiment_score):
        average_sentiment = previous.average_sentiment_score
    elif previous_sentiment_total is not None:
        average_sentiment = round(
            (previous_sentiment_total + tail.sentiment.score)
            / (previous.message_count + 1), 6
        )
    else:
        average_sentiment = _safe_appended_average(
            previous.average_sentiment_score, previous.message_count,
            tail.sentiment.score,
        )
    if average_sentiment is None and sentiment_total_factory is not None:
        previous_sentiment_total = sentiment_total_factory()
        average_sentiment = round(
            (previous_sentiment_total + tail.sentiment.score)
            / (previous.message_count + 1), 6
        )
    if average_sentiment is None:
        return None
    def increment(values, key):
        result = dict(values)
        result[key] = result.get(key, 0) + 1
        return dict(sorted(result.items()))
    topics = dict(previous.topic_counts)
    for item in tail.topic_entities.topics:
        topics[item.taxonomy_id] = topics.get(item.taxonomy_id, 0) + 1
    entities = dict(previous.entity_counts)
    for item in tail.topic_entities.entities:
        key = item.entity_type.value
        entities[key] = entities.get(key, 0) + 1
    changed_direction = tail.direction != last.direction
    inbound_added = int(tail.direction == MessageDirection.INBOUND)
    opportunities = previous.response_opportunity_count + int(inbound_added and changed_direction)
    responded = previous.responded_count
    gap = max(0.0, (tail.sent_at - last.sent_at).total_seconds())
    maximum_silence = max(previous.maximum_silence_seconds or 0.0, gap)
    unavailable = dict(previous.unavailable_reasons)
    if opportunities:
        unavailable.pop('response_coverage', None)
    if responded:
        unavailable.pop('response_time', None)
    unavailable.pop('average_sentiment_score', None)
    unavailable.pop('maximum_silence_seconds', None)
    count = previous.message_count + 1
    provenance = previous.provenance.model_copy(update={
        'sample_count': count, 'sample_coverage': 1.0, 'unavailable_reason': None
    })
    return previous.model_copy(update={
        'ended_at': tail.sent_at,
        'duration_seconds': round(max(0.0, (tail.sent_at - previous.started_at).total_seconds()), 6),
        'message_count': count,
        'inbound_message_count': previous.inbound_message_count + inbound_added,
        'outbound_message_count': previous.outbound_message_count + (1 - inbound_added),
        'turn_count': previous.turn_count + int(changed_direction),
        'response_opportunity_count': opportunities,
        'responded_count': responded,
        'response_coverage': round(responded / opportunities, 6) if opportunities else None,
        'maximum_silence_seconds': round(maximum_silence, 6),
        'average_sentiment_score': average_sentiment,
        'sentiment_counts': increment(previous.sentiment_counts, tail.sentiment.label.value),
        'topic_counts': dict(sorted(topics.items())),
        'entity_counts': dict(sorted(entities.items())),
        'engagement_counts': increment(previous.engagement_counts, tail.engagement.state.value),
        'provenance': provenance,
        'window': previous.window.model_copy(update={'end': tail.sent_at}),
        'unavailable_reasons': unavailable,
    })

def build_conversation_metrics(
    creator_account_id: str,
    conversation: CanonicalConversation,
    enrichments: list[MessageEnrichment],
) -> ConversationMetrics:
    """Aggregate one conversation without using wall-clock state."""
    return build_conversation_metrics_from_values(creator_account_id, conversation,
        (ConversationMetricInput.from_enrichment(item) for item in enrichments))


def build_conversation_metrics_from_values(
    creator_account_id: str,
    conversation: CanonicalConversation,
    enrichments,
) -> ConversationMetrics:
    """Apply the same metric rules to values from models or a verified prefix."""

    return build_conversation_metrics_from_bound_values(
        account_ref(creator_account_id),
        conversation_ref(creator_account_id, conversation.conversation_id),
        participant_ref(creator_account_id, conversation.platform_user_id),
        conversation.unread_count, enrichments,
    )


def build_conversation_metrics_from_bound_values(account, conversation, participant, unread_count, enrichments):
    """Apply the same metric rules to an already account-bound stored selection."""
    ordered = sorted(
        enrichments,
        key=lambda item: (item.sent_at, item.source_ordinal),
    )
    inbound = sum(item.direction == MessageDirection.INBOUND for item in ordered)
    outbound = len(ordered) - inbound
    turn_count = 0
    previous_direction: MessageDirection | None = None
    response_opportunities = 0
    response_seconds: list[float] = []
    pending_inbound_at = None
    for item in ordered:
        if item.direction != previous_direction:
            turn_count += 1
            if item.direction == MessageDirection.INBOUND:
                response_opportunities += 1
        if item.direction == MessageDirection.INBOUND:
            pending_inbound_at = item.sent_at
        elif pending_inbound_at is not None:
            response_seconds.append(
                max(0.0, (item.sent_at - pending_inbound_at).total_seconds())
            )
            pending_inbound_at = None
        previous_direction = item.direction

    silence_seconds = [
        max(0.0, (right.sent_at - left.sent_at).total_seconds())
        for left, right in zip(ordered, ordered[1:])
    ]
    sentiment_counts = Counter(item.sentiment_label for item in ordered)
    topic_counts: Counter[str] = Counter()
    entity_counts: Counter[str] = Counter()
    engagement_counts: Counter[str] = Counter()
    for item in ordered:
        topic_counts.update(item.topic_ids)
        entity_counts.update(
            item.entity_types
        )
        engagement_counts[item.engagement_state] += 1

    sentiment_total = sum(item.sentiment_score for item in ordered)
    average_sentiment = sentiment_total / len(ordered) if ordered else None
    started_at = ordered[0].sent_at if ordered else None
    ended_at = ordered[-1].sent_at if ordered else None
    duration = (
        max(0.0, (ended_at - started_at).total_seconds())
        if started_at is not None and ended_at is not None
        else 0.0
    )
    unavailable_reasons: dict[str, str] = {}
    if not response_opportunities:
        unavailable_reasons["response_coverage"] = "no_response_opportunities"
    if not response_seconds:
        unavailable_reasons["response_time"] = "no_responses"
    if not ordered:
        unavailable_reasons["average_sentiment_score"] = "no_messages"
    if not silence_seconds:
        unavailable_reasons["maximum_silence_seconds"] = "insufficient_messages"
    return ConversationMetrics(
        account_ref=account,
        conversation_ref=conversation,
        participant_ref=participant,
        unread_count=unread_count,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=round(duration, 6),
        message_count=len(ordered),
        inbound_message_count=inbound,
        outbound_message_count=outbound,
        turn_count=turn_count,
        response_opportunity_count=response_opportunities,
        responded_count=len(response_seconds),
        response_coverage=(
            round(len(response_seconds) / response_opportunities, 6)
            if response_opportunities
            else None
        ),
        average_response_seconds=(
            round(sum(response_seconds) / len(response_seconds), 6)
            if response_seconds
            else None
        ),
        median_response_seconds=(
            round(float(median(response_seconds)), 6) if response_seconds else None
        ),
        maximum_silence_seconds=(
            round(max(silence_seconds), 6) if silence_seconds else None
        ),
        average_sentiment_score=(
            round(average_sentiment, 6) if average_sentiment is not None else None
        ),
        sentiment_counts=_ordered_counts(sentiment_counts),
        topic_counts=_ordered_counts(topic_counts),
        entity_counts=_ordered_counts(entity_counts),
        engagement_counts=_ordered_counts(engagement_counts),
        provenance=CONVERSATION_METRICS_PROVENANCE.model_copy(
            update={
                "sample_count": len(ordered),
                "sample_coverage": 1.0 if ordered else None,
                "unavailable_reason": None if ordered else "no_messages",
            }
        ),
        window=AnalyticsWindow(
            scope=WindowScope.ALL_TIME,
            start=started_at,
            end=ended_at,
        ),
        unavailable_reasons=unavailable_reasons,
    )


def build_creator_metrics(
    creator_account_id: str,
    conversations: list[ConversationMetrics],
) -> CreatorMetrics:
    """Aggregate all conversation metrics for one creator account."""

    ordered = sorted(conversations, key=lambda item: item.conversation_ref)
    message_count = sum(item.message_count for item in ordered)
    inbound = sum(item.inbound_message_count for item in ordered)
    outbound = sum(item.outbound_message_count for item in ordered)
    opportunities = sum(item.response_opportunity_count for item in ordered)
    responded = sum(item.responded_count for item in ordered)
    weighted_response_total = sum(
        (item.average_response_seconds or 0.0) * item.responded_count
        for item in ordered
    )
    weighted_sentiment_total = sum(
        (item.average_sentiment_score or 0.0) * item.message_count
        for item in ordered
    )
    sentiment_counts: Counter[str] = Counter()
    topic_counts: Counter[str] = Counter()
    entity_counts: Counter[str] = Counter()
    engagement_counts: Counter[str] = Counter()
    for item in ordered:
        sentiment_counts.update(item.sentiment_counts)
        topic_counts.update(item.topic_counts)
        entity_counts.update(item.entity_counts)
        engagement_counts.update(item.engagement_counts)
    starts = [item.started_at for item in ordered if item.started_at is not None]
    ends = [item.ended_at for item in ordered if item.ended_at is not None]
    unavailable_reasons: dict[str, str] = {}
    if not ordered:
        unavailable_reasons["average_messages_per_conversation"] = "no_conversations"
    if not opportunities:
        unavailable_reasons["response_coverage"] = "no_response_opportunities"
    if not responded:
        unavailable_reasons["average_response_seconds"] = "no_responses"
    if not message_count:
        unavailable_reasons["average_sentiment_score"] = "no_messages"
    return CreatorMetrics(
        account_ref=account_ref(creator_account_id),
        conversation_count=len(ordered),
        participant_count=len({item.participant_ref for item in ordered}),
        message_count=message_count,
        inbound_message_count=inbound,
        outbound_message_count=outbound,
        active_from=min(starts) if starts else None,
        active_until=max(ends) if ends else None,
        average_messages_per_conversation=(
            round(message_count / len(ordered), 6) if ordered else None
        ),
        response_opportunity_count=opportunities,
        responded_count=responded,
        response_coverage=(
            round(responded / opportunities, 6) if opportunities else None
        ),
        average_response_seconds=(
            round(weighted_response_total / responded, 6) if responded else None
        ),
        average_sentiment_score=(
            round(weighted_sentiment_total / message_count, 6)
            if message_count
            else None
        ),
        sentiment_counts=_ordered_counts(sentiment_counts),
        topic_counts=_ordered_counts(topic_counts),
        entity_counts=_ordered_counts(entity_counts),
        engagement_counts=_ordered_counts(engagement_counts),
        provenance=CREATOR_METRICS_PROVENANCE.model_copy(
            update={
                "sample_count": message_count,
                "sample_coverage": 1.0 if message_count else None,
                "unavailable_reason": None if message_count else "no_messages",
            }
        ),
        window=AnalyticsWindow(
            scope=WindowScope.ALL_TIME,
            start=min(starts) if starts else None,
            end=max(ends) if ends else None,
        ),
        unavailable_reasons=unavailable_reasons,
    )


def priority_score(metrics: ConversationMetrics) -> float:
    """Uncalibrated baseline ranking based only on derived conversation state."""

    unanswered = max(0, metrics.response_opportunity_count - metrics.responded_count)
    negative_weight = max(0.0, -(metrics.average_sentiment_score or 0.0)) * 30.0
    score = metrics.unread_count * 15.0 + unanswered * 20.0 + negative_weight
    return round(min(100.0, score), 6)
