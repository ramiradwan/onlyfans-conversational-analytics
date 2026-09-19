"""Session-bound HTTP access to active canonical analytics projections."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.analytics.evidence import EvidenceUnavailable
from app.analytics.evidence_contracts import ResolvedEvidence
from app.analytics.query_contracts import QuestionEvidence, QuestionPlan, QuestionResult
from app.api.security import verify_csrf_token, verify_same_origin


from app.analytics.errors import (
    AnalyticsError,
    CanonicalAccountNotFound,
    InvalidAnalyticsRequest,
    ProjectionUnavailable,
)
from app.api.activation import require_activated_runtime
from app.api.dependencies import (
    account_bound_to_session,
    get_authenticated_account_session,
)
from app.models.analytics import AnalyticsProjection
from app.security.runtime_policy import RuntimePolicy
from app.models.insights import (
    AnalyticsErrorResponse,
    AnalyticsUpdate,
    ResponseTimeMetricsResponse,
    SentimentTrendResponse,
    TopicMetricsCollection,
)
from app.services import insights_service


router = APIRouter(
    prefix="/api/v1/insights",
    tags=["Insights"],
    dependencies=[Depends(require_activated_runtime)],
)

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
    policy: RuntimePolicy,
    creator_account_id: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, datetime | None, datetime | None]:
    account_id = account_bound_to_session(policy, creator_account_id)
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
    policy: RuntimePolicy = Depends(
        get_authenticated_account_session
    ),
) -> TopicMetricsCollection:
    try:
        account_id, start, end = _request_context(
            policy, creator_account_id, start_date, end_date
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
    policy: RuntimePolicy = Depends(
        get_authenticated_account_session
    ),
) -> SentimentTrendResponse:
    try:
        account_id, start, end = _request_context(
            policy, creator_account_id, start_date, end_date
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
    policy: RuntimePolicy = Depends(
        get_authenticated_account_session
    ),
) -> ResponseTimeMetricsResponse:
    try:
        account_id, start, end = _request_context(
            policy, creator_account_id, start_date, end_date
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
    policy: RuntimePolicy = Depends(
        get_authenticated_account_session
    ),
) -> AnalyticsUpdate:
    try:
        account_id, start, end = _request_context(
            policy, creator_account_id, start_date, end_date
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
    policy: RuntimePolicy = Depends(
        get_authenticated_account_session
    ),
) -> AnalyticsProjection:
    account_id = account_bound_to_session(policy, creator_account_id)
    try:
        return await insights_service.active_projection(account_id)
    except AnalyticsError as error:
        raise _analytics_http_error(error) from error



def _question_schema(model):
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].split("/")[-1]])
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    return {"requestBody": {"required": True, "content": {
        "application/json": {"schema": expand(schema)}}}}


def _question_parameters(request):
    if request.query_params:
        raise HTTPException(422, detail="Question filters must be in the request body.")


async def _question_body(request):
    import asyncio
    import json

    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(415, detail="Use an application/json request.")
    if request.headers.get("content-encoding", "identity") != "identity":
        raise HTTPException(415, detail="Compressed requests are not supported.")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_field")
            result[key] = value
        return result

    async def read():
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 16384:
                raise HTTPException(413, detail="The question request is too large.")
            body.extend(chunk)
        return json.loads(body, object_pairs_hook=unique_object)

    try:
        return await asyncio.wait_for(read(), timeout=2)
    except (ValueError, UnicodeError, RecursionError, TimeoutError):
        raise HTTPException(422, detail="The question request is invalid.") from None


def _question_failure(error):
    if isinstance(error, HTTPException):
        if error.status_code == 422:
            return _question_failure(InvalidAnalyticsRequest(
                "analytics_question_invalid", "The question request is invalid."))
        error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
        return error
    if isinstance(error, EvidenceUnavailable):
        return HTTPException(404, detail={"code": error.code, "message": error.public_message,
            "availability": "unavailable"}, headers={"Cache-Control": "no-store"})
    result = _analytics_http_error(error)
    result.headers = {"Cache-Control": "no-store"}
    return result


@router.get("/questions", operation_id="getAnalyticsQuestions", responses=PROTECTED_ERROR_RESPONSES)
def get_questions(response: Response, policy: RuntimePolicy = Depends(get_authenticated_account_session)):
    account_bound_to_session(policy, None)
    response.headers["Cache-Control"] = "no-store"
    return {"questions": [
        {"question": "no_later_creator_reply.v1", "enabled": True,
         "limitations": ["Message types and history coverage may be unknown."]},
        {"question": "pricing_discussions.v1", "enabled": False,
         "reason": "analytics_pricing_not_qualified"}]}


@router.post("/questions", response_model=QuestionResult, operation_id="answerAnalyticsQuestion",
             responses=PROTECTED_ERROR_RESPONSES, openapi_extra=_question_schema(QuestionPlan))
async def answer_question(request: Request, response: Response,
                          policy: RuntimePolicy = Depends(get_authenticated_account_session)):
    response.headers["Cache-Control"] = "no-store"
    try:
        verify_same_origin(request)
        verify_csrf_token(policy, request.headers.get("x-csrf-token"))
        _question_parameters(request)
        return await insights_service.answer_question(policy, await _question_body(request))
    except (AnalyticsError, HTTPException) as error:
        raise _question_failure(error) from None


@router.post("/questions/evidence", response_model=ResolvedEvidence,
             operation_id="resolveAnalyticsEvidence", responses=PROTECTED_ERROR_RESPONSES,
             openapi_extra=_question_schema(QuestionEvidence))
async def resolve_evidence(request: Request, response: Response,
                           policy: RuntimePolicy = Depends(get_authenticated_account_session)):
    response.headers["Cache-Control"] = "no-store"
    try:
        verify_same_origin(request)
        verify_csrf_token(policy, request.headers.get("x-csrf-token"))
        _question_parameters(request)
        return await insights_service.resolve_question_evidence(policy, await _question_body(request))
    except (AnalyticsError, HTTPException) as error:
        raise _question_failure(error) from None


@router.delete("/questions/evidence", status_code=204, operation_id="clearAnalyticsEvidence",
               responses=PROTECTED_ERROR_RESPONSES)
def clear_evidence(request: Request, policy: RuntimePolicy = Depends(get_authenticated_account_session)):
    try:
        verify_same_origin(request)
        verify_csrf_token(policy, request.headers.get("x-csrf-token"))
        _question_parameters(request)
        insights_service.clear_question_evidence(policy)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    except (AnalyticsError, HTTPException) as error:
        raise _question_failure(error) from None
