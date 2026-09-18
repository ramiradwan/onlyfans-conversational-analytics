"""Validate and execute approved questions against pinned read-only generations."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Callable, Iterable

from app.analytics.cancellation import CancellationCheck
from app.analytics.errors import (
    AnalyticsError,
    InvalidAnalyticsRequest,
    ProjectionUnavailable,
)
from app.analytics.historical_derivation import (
    PARTICIPANT_ANALYTICS_MAX_DAYS,
    historical_retention_cutoff,
)
from app.analytics.opaque_refs import account_ref
from app.analytics.query_contracts import (
    PagePosition,
    QuestionId,
    QuestionPage,
    QuestionPlan,
    QuestionResult,
    QuestionSnapshot,
    ResolvedQuestion,
    utc_instant,
)
from app.analytics.query_cursor import (
    InvalidQuestionCursor,
    QuestionCursor,
    QuestionCursorCodec,
    StaleQuestionCursor,
    digest,
)
from app.analytics.query_execution import (
    QuestionBudget,
    QuestionHandler,
    QuestionLimits,
    QuestionReader,
    QuestionResultInvalid,
)
from app.security.runtime_policy import RuntimePolicy, authorized_account

REASONS = MappingProxyType(
    {
        "no_later_creator_reply.v1": "no_later_creator_reply",
        "pricing_discussions.v1": "pricing_discussion",
    }
)


@dataclass(frozen=True, slots=True)
class RegisteredQuestion:
    question: QuestionId
    revision: str
    handler: QuestionHandler

    def __post_init__(self) -> None:
        if self.question not in REASONS or not callable(self.handler):
            raise ValueError("question_registration_invalid")
        if (
            not isinstance(self.revision, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.revision) is None
        ):
            raise ValueError("question_revision_invalid")

    @property
    def definition_digest(self) -> str:
        return digest(
            {
                "question": self.question,
                "revision": self.revision,
                "contract": "analytics-questions.v1",
                "counting_unit": "conversations",
                "selection": "half_open",
                "followup": "through_cutoff_inclusive",
                "sort": "evidence_time_desc_conversation_ref_asc",
            }
        )


class AnalyticsQuestionService:
    def __init__(
        self,
        reader: QuestionReader,
        handlers: Iterable[RegisteredQuestion] = (),
        *,
        limits: QuestionLimits = QuestionLimits(),
        cursor_secret: bytes | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        registered = {}
        for item in handlers:
            if item.question in registered:
                raise ValueError("question_registration_duplicate")
            registered[item.question] = item
        self._handlers = MappingProxyType(registered)
        self._reader = reader
        self._limits = limits
        self._cursors = QuestionCursorCodec(cursor_secret)
        self._clock = clock
        self._monotonic = monotonic

    def execute(
        self,
        policy: RuntimePolicy,
        request: object,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> QuestionResult:
        account = account_ref(authorized_account(policy, None))
        try:
            plan = QuestionPlan.model_validate(request)
        except (ValueError, TypeError):
            raise InvalidAnalyticsRequest(
                "analytics_question_invalid", "The question or its filters are invalid."
            ) from None
        budget = QuestionBudget(
            self._limits,
            cancellation_check=cancellation_check,
            monotonic=self._monotonic,
        )
        budget.check()
        now = utc_instant(self._clock())
        cursor = self._cursors.decode(plan.cursor) if plan.cursor else None
        cutoff = plan.cutoff or (cursor.cutoff if cursor else now)
        if plan.end > cutoff or cutoff > now:
            raise InvalidAnalyticsRequest(
                "analytics_question_cutoff_invalid",
                "The end date must not be after the analysis time.",
            )
        normalized = plan.model_dump(mode="json", exclude={"cursor", "cutoff"})
        request_digest = digest(
            {"account": account, "plan": normalized, "cutoff": cutoff.isoformat()}
        )
        if cursor and cursor.request_digest != request_digest:
            raise InvalidQuestionCursor()
        if cursor and cursor.expires_at <= now:
            raise StaleQuestionCursor()
        registration = self._handlers.get(plan.question)
        if registration is None:
            raise ProjectionUnavailable(reason_code="analytics_question_not_enabled")
        try:
            with self._reader.open(account, budget) as session:
                budget.check()
                snapshot = QuestionSnapshot.model_validate(session.snapshot)
                self._check_snapshot(snapshot, account, now)
                session.assert_current(snapshot, budget)
                snapshot_digest = digest(snapshot.model_dump(mode="json"))
                if cursor and (
                    cursor.snapshot_digest != snapshot_digest
                    or cursor.definition_digest != registration.definition_digest
                ):
                    raise StaleQuestionCursor()
                clean_plan = plan.model_copy(update={"cursor": None, "cutoff": cutoff})
                resolved = ResolvedQuestion(
                    plan=clean_plan,
                    account_ref=account,
                    cutoff=cutoff,
                    retention_cutoff_exclusive=historical_retention_cutoff(now),
                    selection_clipped_by_retention=plan.start
                    <= historical_retention_cutoff(now),
                    definition_digest=registration.definition_digest,
                    snapshot=snapshot,
                )
                after = cursor.after if cursor else None
                page = QuestionPage.model_validate(
                    registration.handler(session, resolved, after, budget)
                )
                budget.check()
                last = self._check_page(page, resolved, after, budget)
                session.assert_current(snapshot, budget)
                checked_at = utc_instant(self._clock())
                self._check_snapshot(snapshot, account, checked_at)
                if checked_at < now:
                    raise QuestionResultInvalid()
                budget.check()
                next_cursor = None
                if page.has_more:
                    expiry = (
                        cursor.expires_at if cursor else now + timedelta(minutes=15)
                    )
                    if snapshot.retention_due_at is not None:
                        expiry = min(expiry, snapshot.retention_due_at)
                    if expiry <= checked_at:
                        raise StaleQuestionCursor()
                    next_cursor = self._cursors.encode(
                        QuestionCursor(
                            request_digest=request_digest,
                            snapshot_digest=snapshot_digest,
                            definition_digest=registration.definition_digest,
                            cutoff=cutoff,
                            expires_at=expiry,
                            after=last,
                        )
                    )
                result = QuestionResult(
                    question=resolved,
                    page=page,
                    checked_at=checked_at,
                    next_cursor=next_cursor,
                )
            budget.check()
            self._check_snapshot(snapshot, account, utc_instant(self._clock()))
            return result
        except AnalyticsError:
            raise
        except Exception:
            raise QuestionResultInvalid() from None

    @staticmethod
    def _check_snapshot(
        snapshot: QuestionSnapshot, account: str, now: datetime
    ) -> None:
        if snapshot.account_ref != account or snapshot.derived_at > now:
            raise QuestionResultInvalid()
        if snapshot.retention_due_at is not None and snapshot.retention_due_at <= now:
            raise ProjectionUnavailable(reason_code="analytics_question_source_expired")

    @staticmethod
    def _check_page(
        page: QuestionPage,
        question: ResolvedQuestion,
        after: PagePosition | None,
        budget: QuestionBudget,
    ) -> PagePosition | None:
        plan, snapshot = question.plan, question.snapshot
        if (
            len(page.rows) > plan.page_size
            or page.evaluated_conversation_count > budget.records_examined
        ):
            raise QuestionResultInvalid()
        if snapshot.source_message_count == 0 and (
            page.rows or page.evaluated_conversation_count
        ):
            raise QuestionResultInvalid()
        if page.coverage.evaluated_start is not None:
            if (
                not question.retention_cutoff_exclusive
                < page.coverage.evaluated_start
                <= page.coverage.evaluated_end
                <= question.cutoff
            ):
                raise QuestionResultInvalid()
        total = page.total_matching_conversations
        if total is not None:
            if page.has_more and total <= len(page.rows):
                raise QuestionResultInvalid()
            if (
                after is None
                and not page.has_more
                and not page.truncated
                and total != len(page.rows)
            ):
                raise QuestionResultInvalid()
            if snapshot.source_message_count == 0 and total != 0:
                raise QuestionResultInvalid()
        previous, seen = after, set()
        evidence_count = 0
        for row in page.rows:
            budget.check()
            if (
                row.account_ref != question.account_ref
                or row.reason != REASONS[plan.question]
            ):
                raise QuestionResultInvalid()
            if row.conversation_ref in seen or (
                plan.filters.conversation_ref is not None
                and row.conversation_ref != plan.filters.conversation_ref
            ):
                raise QuestionResultInvalid()
            seen.add(row.conversation_ref)
            position = PagePosition(
                evidence_at=row.latest_evidence_at,
                conversation_ref=row.conversation_ref,
            )
            if previous is not None and not previous.precedes(position):
                raise QuestionResultInvalid()
            previous = position
            evidence_count += len(row.evidence)
            if evidence_count > 512:
                raise QuestionResultInvalid()
            for evidence in row.evidence:
                budget.check()
                if (
                    snapshot.retention_due_at is None
                    or snapshot.retention_due_at
                    > evidence.sent_at + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
                ):
                    raise QuestionResultInvalid()
                if evidence.source_revision != snapshot.source_revision:
                    raise QuestionResultInvalid()
                if (
                    not question.retention_cutoff_exclusive
                    < evidence.sent_at
                    <= question.cutoff
                ):
                    raise QuestionResultInvalid()
                if (
                    plan.question == "pricing_discussions.v1"
                    and not plan.start <= evidence.sent_at < plan.end
                ):
                    raise QuestionResultInvalid()
        return previous
