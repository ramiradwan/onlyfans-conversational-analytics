"""Session-bound HTTP access to active canonical analytics projections."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.analytics.errors import (
    AnalyticsError,
    CanonicalAccountNotFound,
    InvalidAnalyticsRequest,
    ProjectionUnavailable,
)
from app.api.dependencies import (
    account_bound_to_session,
    get_authenticated_account_session,
)
from app.models.analytics import AnalyticsProjection
from app.models.auth import AuthenticatedAccountSession
from app.models.insights import (
    AnalyticsErrorResponse,
    AnalyticsUpdate,
    ResponseTimeMetricsResponse,
    SentimentTrendResponse,
    TopicMetricsCollection,
)
from app.services import insights_service


router = APIRouter(prefix="/api/v1/insights", tags=["Insights"])

PROTECTED_ERROR_RESPONSES = {
    status: {"model": AnalyticsErrorResponse}
    for status in (404, 422, 503)
}


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise InvalidAnalyticsRequest(
            "analytics_timestamp_invalid",
            "An analytics timestamp is invalid.",
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidAnalyticsRequest(
            "analytics_timestamp_timezone_required",
            "Analytics timestamps must include a timezone.",
        )
    return parsed


def _analytics_http_error(error: AnalyticsError) -> HTTPException:
    if isinstance(error, CanonicalAccountNotFound):
        status_code = 404
        availability = "unavailable"
    elif isinstance(error, ProjectionUnavailable):
        status_code = 503
        availability = error.availability
    elif isinstance(error, InvalidAnalyticsRequest):
        status_code = 422
        availability = "unavailable"
    else:
        status_code = 503
        availability = "unavailable"
    detail: dict[str, str | bool] = {
        "code": error.code,
        "message": error.public_message,
        "availability": availability,
    }
    if isinstance(error, ProjectionUnavailable):
        detail["retryable"] = error.retryable
    return HTTPException(status_code=status_code, detail=detail)


def _request_context(
    session: AuthenticatedAccountSession,
    creator_account_id: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, datetime | None, datetime | None]:
    account_id = account_bound_to_session(session, creator_account_id)
    return account_id, _timestamp(start_date), _timestamp(end_date)


@router.get(
    "/topics",
    response_model=TopicMetricsCollection,
    operation_id="getTopics",
    responses=PROTECTED_ERROR_RESPONSES,
)
async def get_topic_metrics(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    creator_account_id: str | None = Query(None),
    session: AuthenticatedAccountSession = Depends(
        get_authenticated_account_session
    ),
) -> TopicMetricsCollection:
    try:
        account_id, start, end = _request_context(
            session, creator_account_id, start_date, end_date
        )
        return await insights_service.fetch_topic_metrics(start, end, account_id)
    except AnalyticsError as error:
        raise _analytics_http_error(error) from error


@router.get(
    "/sentiment-trend",
    response_model=SentimentTrendResponse,
    operation_id="getSentimentTrend",
    responses=PROTECTED_ERROR_RESPONSES,
)
async def get_sentiment_trend(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    creator_account_id: str | None = Query(None),
    session: AuthenticatedAccountSession = Depends(
        get_authenticated_account_session
    ),
) -> SentimentTrendResponse:
    try:
        account_id, start, end = _request_context(
            session, creator_account_id, start_date, end_date
        )
        return await insights_service.fetch_sentiment_trend(start, end, account_id)
    except AnalyticsError as error:
        raise _analytics_http_error(error) from error


@router.get(
    "/response-time",
    response_model=ResponseTimeMetricsResponse,
    operation_id="getResponseTimeMetrics",
    responses=PROTECTED_ERROR_RESPONSES,
)
async def get_response_time_metrics(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    creator_account_id: str | None = Query(None),
    session: AuthenticatedAccountSession = Depends(
        get_authenticated_account_session
    ),
) -> ResponseTimeMetricsResponse:
    try:
        account_id, start, end = _request_context(
            session, creator_account_id, start_date, end_date
        )
        return await insights_service.fetch_response_time_metrics(
            start, end, account_id
        )
    except AnalyticsError as error:
        raise _analytics_http_error(error) from error


@router.get(
    "/full",
    response_model=AnalyticsUpdate,
    operation_id="getFullAnalytics",
    responses=PROTECTED_ERROR_RESPONSES,
)
async def get_full_analytics(
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    creator_account_id: str | None = Query(None),
    session: AuthenticatedAccountSession = Depends(
        get_authenticated_account_session
    ),
) -> AnalyticsUpdate:
    try:
        account_id, start, end = _request_context(
            session, creator_account_id, start_date, end_date
        )
        return await insights_service.build_analytics_update(
            account_id, start, end
        )
    except AnalyticsError as error:
        raise _analytics_http_error(error) from error


@router.get(
    "/projection",
    response_model=AnalyticsProjection,
    operation_id="getAnalyticsProjection",
    responses=PROTECTED_ERROR_RESPONSES,
)
async def get_projection(
    creator_account_id: str | None = Query(None),
    session: AuthenticatedAccountSession = Depends(
        get_authenticated_account_session
    ),
) -> AnalyticsProjection:
    account_id = account_bound_to_session(session, creator_account_id)
    try:
        return await insights_service.active_projection(account_id)
    except AnalyticsError as error:
        raise _analytics_http_error(error) from error
