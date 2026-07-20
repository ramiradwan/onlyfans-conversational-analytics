"""Account-bound reads from already activated analytics projections."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from statistics import mean
from threading import RLock

from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.errors import (
    CanonicalAccountNotFound,
    InvalidAnalyticsRequest,
    ProjectionBackpressure,
    ProjectionCoordinatorClosed,
    ProjectionStorageUnavailable,
    ProjectionUnavailable,
)
from app.analytics.metrics import (
    CONVERSATION_METRICS_PROVENANCE,
    PRIORITY_SCORE_PROVENANCE,
    priority_score,
)
from app.analytics.opaque_refs import conversation_ref, message_ref
from app.analytics.pipeline import AnalyticsPipeline, CanonicalReadModelSource
from app.analytics.provenance import stable_config_digest
from app.analytics.scheduling import InProcessProjectionScheduler
from app.models.analytics import (
    AnalysisMode,
    AnalyticsProjection,
    AnalyticsWindow,
    CalibrationStatus,
    MessageDirection,
    MessageEnrichment,
    MetricProvenance,
    WindowScope,
)
from app.models.core import Message, UserRef
from app.models.graph import EngagementAction, ExtendedConversationNode, Topic
from app.models.insights import (
    AnalyticsUpdate,
    FullSyncResponse,
    ResponseTimeMetricsResponse,
    SliceProvenance,
    SentimentTrendPoint,
    SentimentTrendResponse,
    TopicMetricsCollection,
    TopicMetricsResponse,
)
from app.transport.ingestion import AccountReadModel


RESPONSE_TIME_REVISION = "response_time.baseline.v2"
RESPONSE_TIME_PROVENANCE = MetricProvenance(
    metric_name="response_time",
    revision=RESPONSE_TIME_REVISION,
    config_digest=stable_config_digest(
        name="response_time",
        revision=RESPONSE_TIME_REVISION,
        config={
            "pairing": "latest_inbound_then_first_outbound",
            "turn_boundary": "direction_change",
            "silence_percentage": "unanswered_opportunity_share",
        },
    ),
    mode=AnalysisMode.BASELINE,
    calibration_status=CalibrationStatus.NOT_CALIBRATED,
)


@dataclass(frozen=True, slots=True)
class AnalyticsRuntime:
    source: CanonicalReadModelSource
    pipeline: AnalyticsPipeline
    scheduler: InProcessProjectionScheduler


_RUNTIMES: dict[int, AnalyticsRuntime] = {}
_RUNTIME_LOCK = RLock()
_STARTUP_TASK: asyncio.Task[None] | None = None
LOGGER = logging.getLogger(__name__)


def analytics_runtime(
    source: CanonicalReadModelSource | None = None,
) -> AnalyticsRuntime:
    """Return the process-local derived runtime for one canonical repository."""

    use_default_runtime = source is None
    if source is None:
        from app.transport import transport_manager

        source = transport_manager.ingestion
    key = id(source)
    with _RUNTIME_LOCK:
        existing = _RUNTIMES.get(key)
        if (
            existing is not None
            and existing.source is source
            and not existing.scheduler.closed
        ):
            return existing
        if use_default_runtime:
            from app.core.config import settings
            from app.transport import transport_manager

            if settings.canonical_persistence_backend == "sqlite":
                stores = create_analytics_stores(
                    "sqlite",
                    projections_path=settings.analytics_projection_database_path,
                    canonical_path=settings.canonical_database_path,
                    activation=transport_manager.projection_activation,
                    canonical_identity_reader=lambda account_id: (
                        canonical_identity(source.account_read_model(account_id))
                        if source.account_exists(account_id)
                        else None
                    ),
                    lazy=True,
                )
                pipeline = AnalyticsPipeline(
                    source,
                    projections=stores.projections,
                    graph=stores.graph,
                )
            else:
                pipeline = AnalyticsPipeline(source)
        else:
            pipeline = AnalyticsPipeline(source)
        runtime = AnalyticsRuntime(
            source=source,
            pipeline=pipeline,
            scheduler=InProcessProjectionScheduler(pipeline),
        )
        _RUNTIMES[key] = runtime
        return runtime


def analytics_pipeline(
    source: CanonicalReadModelSource | None = None,
) -> AnalyticsPipeline:
    return analytics_runtime(source).pipeline


def projection_scheduler(
    source: CanonicalReadModelSource | None = None,
) -> InProcessProjectionScheduler:
    return analytics_runtime(source).scheduler


def configure_default_projection_scheduler() -> InProcessProjectionScheduler:
    """Return the in-process, per-account scheduling seam for the default runtime."""

    return projection_scheduler()


async def start_default_projection_scheduler() -> InProcessProjectionScheduler:
    """Bind the coordinator and recover every canonical account at startup."""

    scheduler = configure_default_projection_scheduler()
    await scheduler.start(recover=True)
    return scheduler


def launch_default_projection_scheduler() -> asyncio.Task[None]:
    """Launch derived recovery after transport readiness without blocking it."""

    global _STARTUP_TASK
    scheduler = configure_default_projection_scheduler()
    if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
        return _STARTUP_TASK

    async def start() -> None:
        try:
            await scheduler.start(recover=True)
        except (ProjectionCoordinatorClosed, ProjectionStorageUnavailable):
            LOGGER.warning(
                "analytics_scheduler_event "
                "reason_code=analytics_projection_start_unavailable "
                "event_type=startup count=1"
            )
        except Exception:
            LOGGER.exception(
                "analytics_scheduler_event "
                "reason_code=analytics_projection_start_failed "
                "event_type=startup count=1"
            )

    _STARTUP_TASK = asyncio.create_task(
        start(), name="analytics-projection-startup"
    )
    return _STARTUP_TASK


async def shutdown_default_projection_scheduler(*, timeout: float = 5.0) -> bool:
    """Close publication/admission and await bounded owned-worker shutdown."""

    from app.transport import transport_manager

    source = transport_manager.ingestion
    key = id(source)
    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(key)
    if runtime is None or runtime.source is not source:
        return True
    drained = await runtime.scheduler.close(timeout=timeout)
    global _STARTUP_TASK
    startup_task = _STARTUP_TASK
    _STARTUP_TASK = None
    if startup_task is not None and not startup_task.done():
        startup_task.cancel()
    with _RUNTIME_LOCK:
        if _RUNTIMES.get(key) is runtime:
            _RUNTIMES.pop(key, None)
    return drained


def reset_analytics_runtimes() -> None:
    """Clear derived process state; canonical data remains untouched."""

    global _STARTUP_TASK
    with _RUNTIME_LOCK:
        for runtime in _RUNTIMES.values():
            runtime.scheduler.abort()
        _RUNTIMES.clear()
    if _STARTUP_TASK is not None and not _STARTUP_TASK.done():
        _STARTUP_TASK.cancel()
    _STARTUP_TASK = None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidAnalyticsRequest(
            "analytics_timestamp_timezone_required",
            "Analytics timestamps must include a timezone.",
        )
    return value.astimezone(timezone.utc)


def _window(
    projection: AnalyticsProjection,
    start_date: datetime | None,
    end_date: datetime | None,
) -> AnalyticsWindow:
    start = _utc(start_date) if start_date is not None else None
    end = _utc(end_date) if end_date is not None else None
    if start is not None and end is not None and start > end:
        raise InvalidAnalyticsRequest(
            "analytics_date_range_invalid",
            "The analytics start must not be after the end.",
        )
    if start is None and end is None:
        return projection.window
    return AnalyticsWindow(scope=WindowScope.REQUESTED, start=start, end=end)


def _messages_in_window(
    projection: AnalyticsProjection,
    window: AnalyticsWindow,
) -> list[MessageEnrichment]:
    return [
        item
        for item in projection.message_enrichments
        if (window.start is None or item.sent_at >= window.start)
        and (window.end is None or item.sent_at <= window.end)
    ]


def _range_analyzer_provenance(
    projection: AnalyticsProjection,
    index: int,
    messages: list[MessageEnrichment],
):
    base = projection.analyzers[index]
    if index == 0:
        confidences = [item.sentiment.confidence for item in messages]
    elif index == 1:
        confidences = [
            topic.confidence
            for item in messages
            for topic in item.topic_entities.topics
        ]
    else:
        confidences = [item.engagement.confidence for item in messages]
    count = len(messages)
    return base.model_copy(
        update={
            "analyzed_sample_count": count,
            "eligible_sample_count": count,
            "sample_coverage": 1.0 if count else None,
            "mean_confidence": round(mean(confidences), 6) if confidences else None,
            "unavailable_reason": None if count else "no_eligible_samples",
        }
    )


def _effective_window(messages: list[MessageEnrichment]) -> AnalyticsWindow:
    ordered = sorted(
        messages,
        key=lambda item: (item.sent_at, item.source_ordinal),
    )
    return AnalyticsWindow(
        scope=WindowScope.EFFECTIVE,
        start=ordered[0].sent_at if ordered else None,
        end=ordered[-1].sent_at if ordered else None,
    )


def _effective_projection_window(projection: AnalyticsProjection) -> AnalyticsWindow:
    return AnalyticsWindow(
        scope=WindowScope.EFFECTIVE,
        start=projection.window.start,
        end=projection.window.end,
    )


def _slice_provenance(
    projection: AnalyticsProjection,
    requested_window: AnalyticsWindow,
    *,
    sample_count: int,
    eligible_sample_count: int,
    effective_window: AnalyticsWindow,
) -> SliceProvenance:
    coverage = (
        round(sample_count / eligible_sample_count, 6)
        if eligible_sample_count
        else None
    )
    return SliceProvenance(
        account_ref=projection.account_ref,
        requested_window=requested_window,
        effective_window=effective_window,
        sample_count=sample_count,
        eligible_sample_count=eligible_sample_count,
        sample_coverage=coverage,
        unavailable_reason=(
            None if eligible_sample_count else "no_eligible_samples"
        ),
        source_revision=projection.source_revision,
        projection_generation=projection.projection_generation,
        projection_digest=projection.content_digest,
        canonical_content_digest=projection.canonical_content_digest,
        graph_digest=projection.graph_digest,
        pipeline_revision=projection.pipeline_revision,
        pipeline_config_digest=projection.pipeline_config_digest,
        pipeline_identity_digest=projection.pipeline_identity_digest,
    )


def _message_slice_provenance(
    projection: AnalyticsProjection,
    requested_window: AnalyticsWindow,
    messages: list[MessageEnrichment],
) -> SliceProvenance:
    return _slice_provenance(
        projection,
        requested_window,
        sample_count=len(messages),
        eligible_sample_count=len(messages),
        effective_window=_effective_window(messages),
    )


def _aggregate_conversation_provenance(
    projection: AnalyticsProjection,
) -> MetricProvenance:
    sample_count = sum(
        item.provenance.sample_count for item in projection.conversation_metrics
    )
    covered = sum(
        item.provenance.sample_count * (item.provenance.sample_coverage or 0.0)
        for item in projection.conversation_metrics
    )
    return CONVERSATION_METRICS_PROVENANCE.model_copy(
        update={
            "sample_count": sample_count,
            "sample_coverage": (
                round(covered / sample_count, 6) if sample_count else None
            ),
            "unavailable_reason": None if sample_count else "no_messages",
        }
    )


def _topic_metrics(
    messages: list[MessageEnrichment],
    window: AnalyticsWindow,
) -> list[TopicMetricsResponse]:
    counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for message in messages:
        for topic in message.topic_entities.topics:
            counts[topic.taxonomy_id] += 1
            labels[topic.taxonomy_id] = topic.label
    total = sum(counts.values())
    if not total:
        return []
    ordered_messages = sorted(
        messages,
        key=lambda item: (item.sent_at, item.source_ordinal),
    )
    lower = window.start or ordered_messages[0].sent_at
    upper = window.end or ordered_messages[-1].sent_at
    midpoint = lower + (upper - lower) / 2
    early: Counter[str] = Counter()
    late: Counter[str] = Counter()
    for message in ordered_messages:
        target = early if message.sent_at <= midpoint else late
        target.update(topic.taxonomy_id for topic in message.topic_entities.topics)
    results: list[TopicMetricsResponse] = []
    for topic_id, volume in sorted(
        counts.items(), key=lambda item: (-item[1], labels[item[0]], item[0])
    ):
        if lower == upper:
            trend = None
            trend_unavailable_reason = "insufficient_time_span"
        elif early[topic_id]:
            trend = round(
                (late[topic_id] - early[topic_id]) / early[topic_id] * 100.0,
                6,
            )
            trend_unavailable_reason = None
        else:
            trend = None
            trend_unavailable_reason = "zero_baseline_samples"
        results.append(
            TopicMetricsResponse(
                topic=labels[topic_id],
                volume=volume,
                percentage_of_total=round(volume / total * 100.0, 6),
                trend=trend,
                trend_unavailable_reason=trend_unavailable_reason,
            )
        )
    return results


def _sentiment_trend(
    projection: AnalyticsProjection,
    messages: list[MessageEnrichment],
    window: AnalyticsWindow,
) -> SentimentTrendResponse:
    by_day: dict[date, list[float]] = defaultdict(list)
    for message in messages:
        by_day[_utc(message.sent_at).date()].append(message.sentiment.score)
    return SentimentTrendResponse(
        account_ref=projection.account_ref,
        trend=[
            SentimentTrendPoint(
                date=datetime.combine(day, time.min, tzinfo=timezone.utc),
                sentiment_score=round(sum(scores) / len(scores), 6),
                message_count=len(scores),
            )
            for day, scores in sorted(by_day.items())
        ],
        window=window,
        provenance=_range_analyzer_provenance(projection, 0, messages),
        source_revision=projection.source_revision,
        projection_generation=projection.projection_generation,
        projection_digest=projection.content_digest,
        canonical_content_digest=projection.canonical_content_digest,
        graph_digest=projection.graph_digest,
        pipeline_revision=projection.pipeline_revision,
        pipeline_config_digest=projection.pipeline_config_digest,
        pipeline_identity_digest=projection.pipeline_identity_digest,
        range_provenance=_message_slice_provenance(
            projection,
            window,
            messages,
        ),
    )


def _response_metrics(
    projection: AnalyticsProjection,
    messages: list[MessageEnrichment],
    window: AnalyticsWindow,
) -> ResponseTimeMetricsResponse:
    grouped: dict[str, list[MessageEnrichment]] = defaultdict(list)
    for message in messages:
        grouped[message.conversation_ref].append(message)
    response_seconds: list[float] = []
    opportunities = 0
    turns: list[int] = []
    for conversation_id in sorted(grouped):
        ordered = sorted(
            grouped[conversation_id],
            key=lambda item: (item.sent_at, item.source_ordinal),
        )
        previous_direction = None
        pending_inbound_at = None
        conversation_turns = 0
        for message in ordered:
            if message.direction != previous_direction:
                conversation_turns += 1
                if message.direction == MessageDirection.INBOUND:
                    opportunities += 1
            if message.direction == MessageDirection.INBOUND:
                pending_inbound_at = message.sent_at
            elif pending_inbound_at is not None:
                response_seconds.append(
                    max(0.0, (message.sent_at - pending_inbound_at).total_seconds())
                )
                pending_inbound_at = None
            previous_direction = message.direction
        turns.append(conversation_turns)
    responded = len(response_seconds)
    coverage = responded / opportunities if opportunities else None
    unavailable_reasons: dict[str, str] = {}
    if not responded:
        unavailable_reasons["average_handling_time_minutes"] = "no_responses"
    if not opportunities:
        unavailable_reasons["response_coverage"] = "no_response_opportunities"
        unavailable_reasons["silence_percentage"] = "no_response_opportunities"
    if not turns:
        unavailable_reasons["turns"] = "no_messages"
    return ResponseTimeMetricsResponse(
        account_ref=projection.account_ref,
        average_handling_time_minutes=(
            round(sum(response_seconds) / responded / 60.0, 6)
            if responded
            else None
        ),
        silence_percentage=(
            round((1.0 - coverage) * 100.0, 6)
            if coverage is not None
            else None
        ),
        turns=round(sum(turns) / len(turns), 6) if turns else None,
        response_coverage=(round(coverage, 6) if coverage is not None else None),
        response_opportunity_count=opportunities,
        responded_count=responded,
        window=window,
        provenance=RESPONSE_TIME_PROVENANCE.model_copy(
            update={
                "sample_count": opportunities,
                "sample_coverage": (
                    round(coverage, 6) if coverage is not None else None
                ),
                "unavailable_reason": (
                    None if opportunities else "no_response_opportunities"
                ),
            }
        ),
        source_revision=projection.source_revision,
        projection_generation=projection.projection_generation,
        projection_digest=projection.content_digest,
        canonical_content_digest=projection.canonical_content_digest,
        graph_digest=projection.graph_digest,
        pipeline_revision=projection.pipeline_revision,
        pipeline_config_digest=projection.pipeline_config_digest,
        pipeline_identity_digest=projection.pipeline_identity_digest,
        range_provenance=_message_slice_provenance(
            projection,
            window,
            messages,
        ),
        unavailable_reasons=unavailable_reasons,
    )


def _projection_is_current(
    projection: AnalyticsProjection | None,
    runtime: AnalyticsRuntime,
    source_revision: int,
) -> bool:
    return bool(
        projection is not None
        and projection.source_revision == source_revision
        and projection.pipeline_revision == runtime.pipeline.pipeline_revision
        and projection.pipeline_config_digest
        == runtime.pipeline.pipeline_config_digest
    )


async def _canonical_account(
    runtime: AnalyticsRuntime,
    creator_account_id: str,
) -> AccountReadModel:
    try:
        return await runtime.scheduler.canonical_account(creator_account_id)
    except (ProjectionBackpressure, ProjectionCoordinatorClosed) as error:
        raise ProjectionUnavailable(availability="unavailable") from error


async def active_projection(
    creator_account_id: str,
    *,
    source: CanonicalReadModelSource | None = None,
) -> AnalyticsProjection:
    """Return current active state without mutating coordinator or projections."""

    runtime = analytics_runtime(source)
    account = await _canonical_account(runtime, creator_account_id)
    try:
        projection = await runtime.scheduler.active_projection(
            creator_account_id, account
        )
    except ProjectionStorageUnavailable as error:
        await runtime.scheduler.request_recovery(
            creator_account_id, account.view_revision
        )
        raise ProjectionUnavailable(
            availability="error",
            reason_code=error.code,
        ) from error
    if _projection_is_current(projection, runtime, account.view_revision):
        return projection  # type: ignore[return-value]
    state = runtime.scheduler.state(
        creator_account_id,
        canonical_revision=account.view_revision,
    )
    raise ProjectionUnavailable(
        availability=state.availability.value,
        reason_code=state.reason_code,
    )


async def fetch_topic_metrics(
    start_date: datetime | None,
    end_date: datetime | None,
    creator_account_id: str,
    *,
    source: CanonicalReadModelSource | None = None,
) -> TopicMetricsCollection:
    projection = await active_projection(creator_account_id, source=source)
    window = _window(projection, start_date, end_date)
    messages = _messages_in_window(projection, window)
    return TopicMetricsCollection(
        account_ref=projection.account_ref,
        topics=_topic_metrics(messages, window),
        window=window,
        provenance=_range_analyzer_provenance(projection, 1, messages),
        source_revision=projection.source_revision,
        projection_generation=projection.projection_generation,
        projection_digest=projection.content_digest,
        canonical_content_digest=projection.canonical_content_digest,
        graph_digest=projection.graph_digest,
        pipeline_revision=projection.pipeline_revision,
        pipeline_config_digest=projection.pipeline_config_digest,
        pipeline_identity_digest=projection.pipeline_identity_digest,
        range_provenance=_message_slice_provenance(projection, window, messages),
    )


async def fetch_sentiment_trend(
    start_date: datetime | None,
    end_date: datetime | None,
    creator_account_id: str,
    *,
    source: CanonicalReadModelSource | None = None,
) -> SentimentTrendResponse:
    projection = await active_projection(creator_account_id, source=source)
    window = _window(projection, start_date, end_date)
    return _sentiment_trend(
        projection,
        _messages_in_window(projection, window),
        window,
    )


async def fetch_response_time_metrics(
    start_date: datetime | None,
    end_date: datetime | None,
    creator_account_id: str,
    *,
    source: CanonicalReadModelSource | None = None,
) -> ResponseTimeMetricsResponse:
    projection = await active_projection(creator_account_id, source=source)
    window = _window(projection, start_date, end_date)
    return _response_metrics(
        projection,
        _messages_in_window(projection, window),
        window,
    )


def _analytics_update_from_projection(
    projection: AnalyticsProjection,
    start_date: datetime | None,
    end_date: datetime | None,
) -> AnalyticsUpdate:
    window = _window(projection, start_date, end_date)
    selected = _messages_in_window(projection, window)
    sentiment = _sentiment_trend(projection, selected, window)
    response = _response_metrics(projection, selected, window)
    all_time = projection.window
    effective_all_time = _effective_projection_window(projection)
    conversation_count = len(projection.conversation_metrics)
    message_count = len(projection.message_enrichments)
    selected_provenance = _message_slice_provenance(
        projection,
        window,
        selected,
    )
    slice_provenance = {
        "topics": selected_provenance,
        "sentiment_trend": selected_provenance,
        "response_time_metrics": selected_provenance,
        "priority_scores": _slice_provenance(
            projection,
            all_time,
            sample_count=conversation_count,
            eligible_sample_count=conversation_count,
            effective_window=effective_all_time,
        ),
        "unread_counts": _slice_provenance(
            projection,
            all_time,
            sample_count=conversation_count,
            eligible_sample_count=conversation_count,
            effective_window=effective_all_time,
        ),
        "conversation_metrics": _slice_provenance(
            projection,
            all_time,
            sample_count=sum(
                item.message_count for item in projection.conversation_metrics
            ),
            eligible_sample_count=message_count,
            effective_window=effective_all_time,
        ),
        "creator_metrics": _slice_provenance(
            projection,
            all_time,
            sample_count=projection.creator_metrics.message_count,
            eligible_sample_count=message_count,
            effective_window=effective_all_time,
        ),
        "message_enrichments": _slice_provenance(
            projection,
            all_time,
            sample_count=message_count,
            eligible_sample_count=message_count,
            effective_window=effective_all_time,
        ),
        "graph": _slice_provenance(
            projection,
            all_time,
            sample_count=projection.graph.node_count,
            eligible_sample_count=projection.graph.node_count,
            effective_window=effective_all_time,
        ),
    }
    return AnalyticsUpdate(
        topics=_topic_metrics(selected, window),
        sentiment_trend=sentiment,
        response_time_metrics=response,
        priorityScores={
            item.conversation_ref: priority_score(item)
            for item in projection.conversation_metrics
        },
        unreadCounts={
            item.conversation_ref: item.unread_count
            for item in projection.conversation_metrics
        },
        account_ref=projection.account_ref,
        source_revision=projection.source_revision,
        projection_generation=projection.projection_generation,
        projection_digest=projection.content_digest,
        canonical_content_digest=projection.canonical_content_digest,
        graph_digest=projection.graph_digest,
        pipeline_revision=projection.pipeline_revision,
        pipeline_config_digest=projection.pipeline_config_digest,
        pipeline_identity_digest=projection.pipeline_identity_digest,
        requested_window=window,
        slice_windows={
            "topics": window,
            "sentiment_trend": window,
            "response_time_metrics": window,
            "priority_scores": all_time,
            "unread_counts": all_time,
            "conversation_metrics": all_time,
            "creator_metrics": all_time,
            "message_enrichments": all_time,
            "graph": all_time,
        },
        slice_provenance=slice_provenance,
        analyzer_provenance=projection.analyzers,
        metric_provenance={
            "priority_scores": PRIORITY_SCORE_PROVENANCE.model_copy(
                update={
                    "sample_count": len(projection.conversation_metrics),
                    "sample_coverage": (
                        1.0 if projection.conversation_metrics else None
                    ),
                    "unavailable_reason": (
                        None if projection.conversation_metrics else "no_conversations"
                    ),
                }
            ),
            "response_time_metrics": response.provenance,
            "conversation_metrics": _aggregate_conversation_provenance(
                projection
            ),
            "creator_metrics": projection.creator_metrics.provenance,
        },
        conversation_metrics=projection.conversation_metrics,
        creator_metrics=projection.creator_metrics,
        message_enrichments=projection.message_enrichments,
        graph=projection.graph,
    )


async def build_analytics_update(
    creator_account_id: str,
    start_date: datetime | None,
    end_date: datetime | None,
    *,
    source: CanonicalReadModelSource | None = None,
) -> AnalyticsUpdate:
    projection = await active_projection(creator_account_id, source=source)
    return _analytics_update_from_projection(projection, start_date, end_date)


def _conversations_from_snapshot(
    creator_account_id: str,
    account: AccountReadModel,
    projection: AnalyticsProjection,
) -> list[ExtendedConversationNode]:
    """Build compatibility conversations from one account/projection generation."""

    if account.view_revision != projection.source_revision:
        raise ProjectionUnavailable(availability="unavailable")
    metrics_by_id = {
        item.conversation_ref: item for item in projection.conversation_metrics
    }
    enrichments_by_message = {
        item.message_ref: item for item in projection.message_enrichments
    }
    results: list[ExtendedConversationNode] = []
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for conversation_id, conversation in sorted(account.conversations.items()):
        analytics_conversation_ref = conversation_ref(
            creator_account_id, conversation_id
        )
        metrics = metrics_by_id[analytics_conversation_ref]
        legacy_messages: list[Message] = []
        for raw in conversation.get("messages", []):
            enrichment = enrichments_by_message[
                message_ref(
                    creator_account_id,
                    conversation_id,
                    raw["message_id"],
                )
            ]
            legacy_messages.append(
                Message(
                    id=raw["message_id"],
                    chat_id=conversation_id,
                    text=raw["text"],
                    created_at=raw["sent_at"],
                    is_inbound=raw["direction"] == "inbound",
                    sentimentScore=round(
                        (enrichment.sentiment.score + 1.0) / 2.0, 6
                    ),
                    topics=[
                        topic.label for topic in enrichment.topic_entities.topics
                    ],
                )
            )
        topic_labels: dict[str, str] = {}
        engagement_states: set[str] = set()
        for raw in conversation.get("messages", []):
            enrichment = enrichments_by_message[
                message_ref(
                    creator_account_id,
                    conversation_id,
                    raw["message_id"],
                )
            ]
            topic_labels.update(
                {
                    topic.taxonomy_id: topic.label
                    for topic in enrichment.topic_entities.topics
                }
            )
            engagement_states.add(enrichment.engagement.state.value)
        results.append(
            ExtendedConversationNode(
                conversationId=conversation_id,
                analyticsRef=analytics_conversation_ref,
                startDate=metrics.started_at or epoch,
                endDate=metrics.ended_at,
                messageCount=metrics.message_count,
                averageResponseTime=(
                    metrics.average_response_seconds / 60.0
                    if metrics.average_response_seconds is not None
                    else None
                ),
                turns=metrics.turn_count,
                silencePercentage=(
                    round((1.0 - metrics.response_coverage) * 100.0, 6)
                    if metrics.response_coverage is not None
                    else None
                ),
                messages=legacy_messages,
                topics=[
                    Topic(topicId=topic_id, description=label, embedding=[])
                    for topic_id, label in sorted(topic_labels.items())
                ],
                actions=[
                    EngagementAction(
                        actionId=state,
                        name=state.replace("_", " ").title(),
                        embedding=[],
                        type="message_function",
                    )
                    for state in sorted(engagement_states)
                ],
                sentiment=metrics.average_sentiment_score,
                outcomes=[],
                priorityScore=priority_score(metrics),
                withUser=UserRef(
                    id=conversation["platform_user_id"],
                    displayName=conversation.get("display_name"),
                ),
            )
        )
    return results


def _same_projection_generation(
    expected: AnalyticsProjection,
    observed: AnalyticsProjection | None,
) -> bool:
    return bool(
        observed is not None
        and observed.source_revision == expected.source_revision
        and observed.projection_generation == expected.projection_generation
        and observed.content_digest == expected.content_digest
        and observed.canonical_content_digest == expected.canonical_content_digest
        and observed.graph_digest == expected.graph_digest
        and observed.pipeline_revision == expected.pipeline_revision
        and observed.pipeline_config_digest == expected.pipeline_config_digest
        and observed.pipeline_identity_digest == expected.pipeline_identity_digest
    )


async def fetch_conversations_for_account(
    creator_account_id: str,
    *,
    source: CanonicalReadModelSource | None = None,
) -> list[ExtendedConversationNode]:
    projection = await active_projection(creator_account_id, source=source)
    runtime = analytics_runtime(source)
    account = await _canonical_account(runtime, creator_account_id)
    if account.view_revision != projection.source_revision:
        state = runtime.scheduler.state(
            creator_account_id,
            canonical_revision=account.view_revision,
        )
        raise ProjectionUnavailable(
            availability=state.availability.value,
            reason_code=state.reason_code,
        )
    results = _conversations_from_snapshot(
        creator_account_id, account, projection
    )
    observed_account = await _canonical_account(runtime, creator_account_id)
    observed_projection = await runtime.scheduler.active_projection(
        creator_account_id, observed_account
    )
    if (
        observed_account.view_revision != account.view_revision
        or not _same_projection_generation(projection, observed_projection)
    ):
        raise ProjectionUnavailable(availability="unavailable")
    return results


async def get_full_snapshot(
    creator_account_id: str,
    *,
    source: CanonicalReadModelSource | None = None,
) -> FullSyncResponse:
    runtime = analytics_runtime(source)
    for _ in range(2):
        projection = await active_projection(creator_account_id, source=source)
        account = await _canonical_account(runtime, creator_account_id)
        if account.view_revision != projection.source_revision:
            continue
        conversations = _conversations_from_snapshot(
            creator_account_id, account, projection
        )
        analytics = _analytics_update_from_projection(projection, None, None)
        observed_account = await _canonical_account(runtime, creator_account_id)
        observed_projection = await runtime.scheduler.active_projection(
            creator_account_id, observed_account
        )
        if (
            observed_account.view_revision == account.view_revision
            and _same_projection_generation(projection, observed_projection)
        ):
            conversation_provenance = _aggregate_conversation_provenance(
                projection
            )
            message_count = sum(
                item.message_count for item in projection.conversation_metrics
            )
            return FullSyncResponse(
                account_ref=projection.account_ref,
                conversations=conversations,
                analytics=analytics,
                conversation_window=projection.window,
                conversation_metric_provenance=conversation_provenance,
                conversation_range_provenance=_slice_provenance(
                    projection,
                    projection.window,
                    sample_count=message_count,
                    eligible_sample_count=len(projection.message_enrichments),
                    effective_window=_effective_projection_window(projection),
                ),
            )
    raise ProjectionUnavailable(availability="unavailable")
