"""Read-only adapter ports and cooperative work limits for analytics questions."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, ContextManager, Protocol

from app.analytics.cancellation import CancellationCheck
from app.analytics.errors import AnalyticsError
from app.analytics.query_contracts import (
    PagePosition,
    QuestionPage,
    QuestionSnapshot,
    ResolvedQuestion,
)


class QuestionLimitExceeded(AnalyticsError):
    code = "analytics_question_limit_exceeded"
    public_message = (
        "The question exceeded its processing limit. Narrow the date range."
    )


class QuestionCancelled(AnalyticsError):
    code = "analytics_question_cancelled"
    public_message = "The question was cancelled."


class QuestionResultInvalid(AnalyticsError):
    code = "analytics_question_result_invalid"
    public_message = "The question could not return a valid result."


@dataclass(frozen=True, slots=True)
class QuestionLimits:
    max_records: int = 10_000
    wall_clock_ms: int = 1_000

    def __post_init__(self) -> None:
        if type(self.max_records) is not int or not 1 <= self.max_records <= 100_000:
            raise ValueError("question_record_limit_invalid")
        if type(self.wall_clock_ms) is not int or not 1 <= self.wall_clock_ms <= 30_000:
            raise ValueError("question_time_limit_invalid")


class QuestionBudget:
    def __init__(
        self,
        limits: QuestionLimits,
        *,
        cancellation_check: CancellationCheck | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self._clock = monotonic
        self._deadline = monotonic() + limits.wall_clock_ms / 1000
        self._cancelled = cancellation_check
        self.records_examined = 0

    def check(self) -> None:
        if self.records_examined > self.limits.max_records:
            raise QuestionLimitExceeded()
        if self._cancelled is not None and self._cancelled():
            raise QuestionCancelled()
        if self._clock() >= self._deadline:
            raise QuestionLimitExceeded()

    def remaining_seconds(self) -> float:
        """Return the shared timeout for a downstream blocking operation."""

        self.check()
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise QuestionLimitExceeded()
        return remaining

    def consume(self, count: int = 1) -> None:
        self.check()
        if type(count) is not int or count < 0:
            raise QuestionResultInvalid()
        self.records_examined += count
        if self.records_examined > self.limits.max_records:
            raise QuestionLimitExceeded()


class QuestionReadSession(Protocol):
    """A pinned generation whose visibility is rechecked before returning results."""

    @property
    def snapshot(self) -> QuestionSnapshot: ...

    def assert_current(
        self, snapshot: QuestionSnapshot, budget: QuestionBudget
    ) -> None: ...


class QuestionReader(Protocol):
    def open(
        self, account_ref: str, budget: QuestionBudget
    ) -> ContextManager[QuestionReadSession]: ...


class QuestionHandler(Protocol):
    def __call__(
        self,
        session: QuestionReadSession,
        question: ResolvedQuestion,
        after: PagePosition | None,
        budget: QuestionBudget,
    ) -> QuestionPage: ...
