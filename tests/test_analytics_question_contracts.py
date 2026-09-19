"""Validate question windows, bounded records, and cursor configuration."""

from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from app.analytics.query_contracts import QuestionCoverage, QuestionPlan, SourceSpan
from app.analytics.query_cursor import QuestionCursorCodec
from app.analytics.query_execution import (
    QuestionBudget,
    QuestionLimits,
    QuestionResultInvalid,
)
from app.analytics.query_service import AnalyticsQuestionService, RegisteredQuestion


def request(**updates):
    return {
        "question": "pricing_discussions.v1",
        "start": "2026-09-17T00:00:00Z",
        "end": "2026-09-18T00:00:00Z",
        "timezone": "UTC",
        **updates,
    }


@pytest.mark.parametrize(
    "start,end,hours",
    [
        ("2026-03-29T00:00:00+02:00", "2026-03-30T00:00:00+03:00", 23),
        ("2026-10-25T00:00:00+03:00", "2026-10-26T00:00:00+02:00", 25),
    ],
)
def test_resolved_calendar_windows_preserve_dst_duration(start, end, hours):
    plan = QuestionPlan.model_validate(
        request(start=start, end=end, timezone="Europe/Helsinki")
    )
    assert plan.end - plan.start == timedelta(hours=hours)
    assert plan.start.tzinfo == timezone.utc
    assert plan.end.tzinfo == timezone.utc


@pytest.mark.parametrize(
    "field,value",
    [
        ("start", 1234),
        ("start", True),
        ("start", "0001-01-01T00:00:00+14:00"),
        ("start", "2026-09-17"),
        ("timezone", ""),
        ("timezone", "../UTC"),
        ("timezone", "/UTC"),
        ("timezone", "x" * 65),
        ("filters", {"topic": "anything"}),
        ("filters", {"language": "unsupported"}),
    ],
)
def test_contract_rejects_invalid_values(field, value):
    with pytest.raises(ValidationError):
        QuestionPlan.model_validate(request(**{field: value}))


@pytest.mark.parametrize(
    "updates",
    [
        {"evaluated_start": "2026-09-18T00:00:00Z"},
        {"eligible_classification_count": 1},
        {"eligible_classification_count": 1, "analyzed_classification_count": 2},
        {"eligible_classification_count": True, "analyzed_classification_count": 0},
        {"supported_languages": ["en", "en"]},
    ],
)
def test_coverage_cannot_invent_counts_or_intervals(updates):
    with pytest.raises(ValidationError):
        QuestionCoverage(history="unknown", ordering="unknown", **updates)


def test_source_spans_use_nonempty_codepoint_intervals():
    assert SourceSpan(start=1, end=3).end == 3
    with pytest.raises(ValidationError):
        SourceSpan(start=3, end=3)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_records": 0},
        {"max_records": True},
        {"max_records": 100001},
        {"wall_clock_ms": 0},
        {"wall_clock_ms": float("inf")},
        {"wall_clock_ms": 30001},
    ],
)
def test_only_bounded_server_limits_are_accepted(limits):
    with pytest.raises(ValueError):
        QuestionLimits(**limits)


def test_record_accounting_cannot_subtract_work():
    budget = QuestionBudget(QuestionLimits())
    with pytest.raises(QuestionResultInvalid):
        budget.consume(-1)


@pytest.mark.parametrize("secret", [b"", b"short", "not-bytes"])
def test_cursor_keys_require_sufficient_bytes(secret):
    with pytest.raises(ValueError):
        QuestionCursorCodec(secret)


def test_registry_rejects_unknown_and_duplicate_handlers():
    with pytest.raises(ValueError):
        RegisteredQuestion("unknown.v1", "v1", lambda *args: None)
    item = RegisteredQuestion("pricing_discussions.v1", "v1", lambda *args: None)
    with pytest.raises(ValueError):
        AnalyticsQuestionService(None, [item, item])


def test_adapter_timeout_shares_the_request_deadline():
    tick = [10.0]
    budget = QuestionBudget(QuestionLimits(), monotonic=lambda: tick[0])
    assert budget.remaining_seconds() == 1.0
    tick[0] = 10.75
    assert budget.remaining_seconds() == 0.25
    budget.consume(1)
    assert budget.remaining_seconds() == 0.25


@pytest.mark.parametrize("fault", ["deadline", "between_checks", "cancelled", "records"])
def test_adapter_timeout_cannot_extend_or_bypass_a_limit(fault):
    from app.analytics.query_execution import QuestionCancelled, QuestionLimitExceeded

    tick, cancelled = [0.0], [False]
    budget = QuestionBudget(
        QuestionLimits(max_records=1), monotonic=lambda: tick[0],
        cancellation_check=lambda: cancelled[0],
    )
    if fault == "deadline":
        tick[0] = 1.0
    elif fault == "between_checks":
        readings = iter((0.99, 1.0))
        budget._clock = lambda: next(readings)
    elif fault == "cancelled":
        cancelled[0] = True
    else:
        with pytest.raises(QuestionLimitExceeded):
            budget.consume(2)
    with pytest.raises((QuestionLimitExceeded, QuestionCancelled)):
        budget.remaining_seconds()
