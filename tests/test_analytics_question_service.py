"""Exercise query delivery with prepared rows, without running feature classifiers."""

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.analytics.errors import InvalidAnalyticsRequest, ProjectionUnavailable
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.query_contracts import (
    PagePosition,
    QuestionCoverage,
    QuestionEvidence,
    QuestionPage,
    QuestionPlan,
    QuestionRow,
    QuestionSnapshot,
)
from app.analytics.query_cursor import InvalidQuestionCursor, StaleQuestionCursor
from app.analytics.query_execution import (
    QuestionCancelled,
    QuestionLimitExceeded,
    QuestionLimits,
    QuestionResultInvalid,
)
from app.analytics.query_service import AnalyticsQuestionService, RegisteredQuestion
from app.security.runtime_policy import (
    AuthContext,
    AuthorizationEpoch,
    RuntimeAuthorizationDenied,
    RuntimePolicy,
)
from tests.state_models.analytics_question_cases import instant, load_cases

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
ACCOUNT = "synthetic-question-creator"
SECRET = b"synthetic-question-cursor-key-0001"


def sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def policy(account: str = ACCOUNT) -> RuntimePolicy:
    return RuntimePolicy(
        AuthContext("synthetic-principal", account, "creator"), AuthorizationEpoch(1)
    )


def plan(**updates) -> dict:
    return {
        "question": "no_later_creator_reply.v1",
        "start": NOW - timedelta(days=1),
        "end": NOW,
        "timezone": "UTC",
        **updates,
    }


def row(
    name: str,
    *,
    account: str = ACCOUNT,
    at: datetime = NOW - timedelta(hours=1),
    reason: str = "no_later_creator_reply",
) -> QuestionRow:
    a, c = account_ref(account), conversation_ref(account, name)
    evidence = QuestionEvidence(
        account_ref=a,
        conversation_ref=c,
        message_ref=message_ref(account, name, "synthetic-message"),
        source_revision=7,
        source_version_digest=sha("synthetic-source-version"),
        sent_at=at,
    )
    return QuestionRow(
        account_ref=a,
        conversation_ref=c,
        latest_evidence_at=at,
        reason=reason,
        coverage="unknown",
        evidence=(evidence,),
    )


class PreparedReader:
    def __init__(self, rows=(), *, account=ACCOUNT, now=NOW):
        self.rows = sorted(
            rows, key=lambda r: (-r.latest_evidence_at.timestamp(), r.conversation_ref)
        )
        self.snapshot = QuestionSnapshot(
            account_ref=account_ref(account),
            source_revision=7,
            projection_generation=2,
            generation_id="synthetic-generation",
            canonical_content_digest=sha("canonical"),
            projection_digest=sha("projection"),
            derived_at=now - timedelta(minutes=1),
            source_message_count=100,
            retention_due_at=now + timedelta(days=1),
        )
        self.opens = self.checks = self.calls = 0
        self.changed = False

    @contextmanager
    def open(self, account, budget):
        self.opens += 1
        assert account == self.snapshot.account_ref
        budget.check()
        yield self

    def assert_current(self, snapshot, budget):
        self.checks += 1
        budget.check()
        if self.changed or snapshot != self.snapshot:
            raise ProjectionUnavailable(availability="building")

    def page(self, session, question, after, budget):
        self.calls += 1
        assert session is self
        rows = [
            r
            for r in self.rows
            if after is None
            or after.precedes(
                PagePosition(
                    evidence_at=r.latest_evidence_at,
                    conversation_ref=r.conversation_ref,
                )
            )
        ]
        selected = tuple(rows[: question.plan.page_size])
        budget.consume(len(selected))
        return QuestionPage(
            rows=selected,
            coverage=QuestionCoverage(history="unknown", ordering="unknown"),
            evaluated_conversation_count=len(selected),
            undetermined_conversation_count=0,
            total_matching_conversations=len(self.rows),
            has_more=len(rows) > len(selected),
        )


def service(
    reader,
    handler=None,
    *,
    question="no_later_creator_reply.v1",
    revision="v1",
    **kwargs,
):
    return AnalyticsQuestionService(
        reader,
        [RegisteredQuestion(question, revision, handler or reader.page)],
        cursor_secret=SECRET,
        clock=kwargs.pop("clock", lambda: NOW),
        **kwargs,
    )


def test_pages_have_stable_order_and_fixed_cutoff():
    reader = PreparedReader([row(str(i)) for i in range(5)])
    clock = [NOW]
    executor = service(reader, clock=lambda: clock[0])
    request = plan(page_size=2)
    found = []
    while True:
        result = executor.execute(policy(), request)
        found.extend(r.conversation_ref for r in result.page.rows)
        assert result.question.cutoff == NOW
        assert result.question.plan.cursor is None
        assert result.page.total_matching_conversations == 5
        if result.next_cursor is None:
            break
        request = plan(page_size=2, cursor=result.next_cursor)
        clock[0] += timedelta(seconds=1)
    assert found == [r.conversation_ref for r in reader.rows]
    assert len(set(found)) == 5
    assert reader.checks == 6


def test_empty_result_remains_available_with_unknown_history():
    result = service(PreparedReader()).execute(policy(), plan())
    assert result.availability == "available" and result.page.rows == ()
    assert result.page.coverage.history == "unknown"
    assert result.next_cursor is None


@pytest.mark.parametrize(
    "update",
    [
        {"account": "synthetic-other"},
        {"gremlin": "g.V()"},
        {"max_records": 999999},
        {"question": "unknown.v1"},
        {"page_size": True},
        {"page_size": 201},
        {"page_size": "50"},
        {"page_size": 0},
        {"timezone": "Mars/Unknown"},
        {"start": "2026-09-17T00:00:00"},
        {"end": NOW + timedelta(seconds=1)},
        {"start": NOW},
        {"cutoff": NOW - timedelta(seconds=1)},
        {"filters": {"language": "en"}},
        {"cursor": "x" * 4097},
    ],
)
def test_invalid_plans_do_not_reach_storage(update):
    reader = PreparedReader()
    with pytest.raises(InvalidAnalyticsRequest):
        service(reader).execute(policy(), plan(**update))
    assert reader.opens == 0


def test_unvalidated_model_copy_is_revalidated():
    request = QuestionPlan.model_validate(plan()).model_copy(update={"page_size": 999})
    with pytest.raises(InvalidAnalyticsRequest):
        service(PreparedReader()).execute(policy(), request)


def test_account_authority_is_required_before_reading():
    reader = PreparedReader()
    with pytest.raises(RuntimeAuthorizationDenied):
        service(reader).execute(RuntimePolicy(None, AuthorizationEpoch(1)), plan())
    assert reader.opens == 0


def first_cursor(reader):
    executor = service(reader)
    result = executor.execute(policy(), plan(page_size=1))
    assert result.next_cursor
    return executor, result.next_cursor


@pytest.mark.parametrize(
    "update",
    [
        {"page_size": 2},
        {"timezone": "Europe/Helsinki"},
        {"start": NOW - timedelta(days=2)},
        {"question": "pricing_discussions.v1"},
        {"filters": {"conversation_ref": conversation_ref(ACCOUNT, "other")}},
        {"cutoff": NOW - timedelta(seconds=1), "end": NOW - timedelta(seconds=1)},
    ],
)
def test_cursor_rejects_changed_question_or_filters(update):
    reader = PreparedReader([row("1"), row("2")])
    executor, cursor = first_cursor(reader)
    with pytest.raises(InvalidQuestionCursor):
        executor.execute(policy(), plan(**{"page_size": 1, "cursor": cursor, **update}))
    assert reader.opens == 1


def test_cursor_cannot_select_another_account():
    reader = PreparedReader([row("1"), row("2")])
    executor, cursor = first_cursor(reader)
    with pytest.raises(InvalidQuestionCursor):
        executor.execute(policy("synthetic-other"), plan(page_size=1, cursor=cursor))
    assert reader.opens == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("projection_generation", 3),
        ("source_revision", 8),
        ("generation_id", "synthetic-restored-generation"),
        ("canonical_content_digest", sha("changed-canonical")),
        ("projection_digest", sha("changed-projection")),
    ],
)
def test_cursor_rejects_changed_generation_or_source(field, value):
    reader = PreparedReader([row("1"), row("2")])
    executor, cursor = first_cursor(reader)
    reader.snapshot = reader.snapshot.model_copy(update={field: value})
    with pytest.raises(StaleQuestionCursor):
        executor.execute(policy(), plan(page_size=1, cursor=cursor))
    assert reader.calls == 1


def test_changed_handler_revision_invalidates_cursor():
    reader = PreparedReader([row("1"), row("2")])
    _, cursor = first_cursor(reader)
    with pytest.raises(StaleQuestionCursor):
        service(reader, revision="v2").execute(
            policy(), plan(page_size=1, cursor=cursor)
        )
    assert reader.calls == 1


def test_cursor_expires_without_renewing_on_each_page():
    reader = PreparedReader([row(str(i)) for i in range(4)])
    executor, cursor = first_cursor(reader)
    late = service(reader, clock=lambda: NOW + timedelta(minutes=15))
    with pytest.raises(StaleQuestionCursor):
        late.execute(policy(), plan(page_size=1, cursor=cursor))


@pytest.mark.parametrize(
    "mutation", ["payload", "signature", "padding", "extra", "unicode"]
)
def test_cursor_tampering_is_rejected(mutation):
    reader = PreparedReader([row("1"), row("2")])
    executor, token = first_cursor(reader)
    payload, signature = token.split(".")
    altered = {
        "payload": "A" + payload[1:] + "." + signature,
        "signature": payload
        + "."
        + ("A" if signature[0] != "A" else "B")
        + signature[1:],
        "padding": payload + "=." + signature,
        "extra": token + ".extra",
        "unicode": "é." + signature,
    }[mutation]
    with pytest.raises(InvalidQuestionCursor):
        executor.execute(policy(), plan(page_size=1, cursor=altered))
    assert reader.opens == 1


def test_new_process_key_invalidates_old_cursor():
    reader = PreparedReader([row("1"), row("2")])
    _, cursor = first_cursor(reader)
    executor = AnalyticsQuestionService(
        reader,
        [RegisteredQuestion("no_later_creator_reply.v1", "v1", reader.page)],
        clock=lambda: NOW,
    )
    with pytest.raises(InvalidQuestionCursor):
        executor.execute(policy(), plan(page_size=1, cursor=cursor))


def test_unregistered_question_is_unavailable_without_reading():
    reader = PreparedReader()
    with pytest.raises(ProjectionUnavailable):
        AnalyticsQuestionService(reader).execute(policy(), plan())
    assert reader.opens == 0


@pytest.mark.parametrize("status", ["unavailable", "building", "error"])
def test_projection_failures_keep_their_availability(status):
    reader = PreparedReader()

    def fail(session, question, after, budget):
        raise ProjectionUnavailable(availability=status)

    with pytest.raises(ProjectionUnavailable) as error:
        service(reader, fail).execute(policy(), plan())
    assert error.value.availability == status


def test_generation_change_during_read_returns_no_result():
    reader = PreparedReader([row("1")])

    def change(session, question, after, budget):
        page = reader.page(session, question, after, budget)
        reader.changed = True
        return page

    with pytest.raises(ProjectionUnavailable):
        service(reader, change).execute(policy(), plan())
    assert reader.checks == 2


def test_expired_projection_is_not_read_or_rebuilt():
    reader = PreparedReader([row("1")])
    reader.snapshot = reader.snapshot.model_copy(update={"retention_due_at": NOW})
    with pytest.raises(ProjectionUnavailable):
        service(reader).execute(policy(), plan())
    assert reader.calls == 0


@pytest.mark.parametrize(
    "fault", ["too_many", "unmetered", "deadline", "cancelled", "swallowed_limit"]
)
def test_work_limits_refuse_invalid_results(fault):
    reader = PreparedReader([row("1")])
    tick, cancelled = [0.0], [False]

    def handle(session, question, after, budget):
        page = reader.page(session, question, after, budget)
        if fault == "too_many":
            budget.consume(11)
        elif fault == "unmetered":
            return page.model_copy(update={"evaluated_conversation_count": 11})
        elif fault == "deadline":
            tick[0] = 1.0
        elif fault == "cancelled":
            cancelled[0] = True
        else:
            try:
                budget.consume(11)
            except QuestionLimitExceeded:
                pass
        return page

    executor = service(
        reader, handle, limits=QuestionLimits(max_records=10), monotonic=lambda: tick[0]
    )
    with pytest.raises(
        (QuestionLimitExceeded, QuestionResultInvalid, QuestionCancelled)
    ):
        executor.execute(policy(), plan(), cancellation_check=lambda: cancelled[0])


def test_pre_cancelled_question_does_not_open_storage():
    reader = PreparedReader()
    with pytest.raises(QuestionCancelled):
        service(reader).execute(policy(), plan(), cancellation_check=lambda: True)
    assert reader.opens == 0


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate",
        "reverse",
        "account",
        "revision",
        "future",
        "expired",
        "reason",
        "page_size",
        "source_digest",
    ],
)
def test_malformed_adapter_results_are_refused(fault):
    reader = PreparedReader([row("1"), row("2")])

    def corrupt(session, question, after, budget):
        page = reader.page(session, question, after, budget)
        rows = list(page.rows)
        if fault == "duplicate":
            rows = [rows[0], rows[0]]
        elif fault == "reverse":
            rows.reverse()
        elif fault == "account":
            rows[0] = row("1", account="synthetic-other")
        elif fault in {"future", "expired"}:
            at = (
                NOW + timedelta(seconds=1)
                if fault == "future"
                else NOW - timedelta(days=90)
            )
            rows[0] = row("1", at=at)
        elif fault == "reason":
            rows[0] = row("1", reason="pricing_discussion")
        elif fault in {"revision", "source_digest"}:
            update = (
                {"source_revision": 9}
                if fault == "revision"
                else {"source_version_digest": "unverified"}
            )
            ev = rows[0].evidence[0].model_copy(update=update)
            rows[0] = rows[0].model_copy(update={"evidence": (ev,)})
        else:
            rows = rows * 101
        return page.model_copy(update={"rows": tuple(rows)})

    with pytest.raises(QuestionResultInvalid):
        service(reader, corrupt).execute(policy(), plan())


def test_truncation_never_returns_an_exact_total_or_cursor():
    reader = PreparedReader([row("1")])

    def truncate(session, question, after, budget):
        return reader.page(session, question, after, budget).model_copy(
            update={"truncated": True, "total_matching_conversations": None}
        )

    result = service(reader, truncate).execute(policy(), plan())
    assert result.page.truncated and result.next_cursor is None
    assert result.page.total_matching_conversations is None


def test_truncated_exact_total_is_rejected():
    reader = PreparedReader([row("1")])

    def truncate(session, question, after, budget):
        return reader.page(session, question, after, budget).model_copy(
            update={"truncated": True}
        )

    with pytest.raises(QuestionResultInvalid):
        service(reader, truncate).execute(policy(), plan())


def test_adapter_error_does_not_disclose_content():
    def fail(*args):
        raise RuntimeError("synthetic-private-message")

    with pytest.raises(QuestionResultInvalid) as error:
        service(PreparedReader(), fail).execute(policy(), plan())
    assert "synthetic-private-message" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_prepared_fixture_answers_keep_scope_evidence_and_unknown_counts(case):
    now = instant(case["now"])
    rows = []
    for conversation in case["expected"]["matches"]:
        evidence = []
        for mid in case["expected"]["evidence"][conversation]:
            source = next(
                m
                for m in case["messages"]
                if (m["account"], m["conversation"], m["id"])
                == (case["account"], conversation, mid)
            )
            evidence.append(
                QuestionEvidence(
                    account_ref=account_ref(case["account"]),
                    conversation_ref=conversation_ref(case["account"], conversation),
                    message_ref=message_ref(case["account"], conversation, mid),
                    source_revision=case["source_revision"],
                    sent_at=instant(source["at"]),
                    source_version_digest=sha(str(source["version"])),
                )
            )
        rows.append(
            QuestionRow(
                account_ref=account_ref(case["account"]),
                conversation_ref=conversation_ref(case["account"], conversation),
                latest_evidence_at=max(e.sent_at for e in evidence),
                reason="pricing_discussion"
                if case["question"] == "pricing_discussions.v1"
                else "no_later_creator_reply",
                coverage=case["coverage"],
                evidence=tuple(evidence),
            )
        )
    reader = PreparedReader(rows, account=case["account"], now=now)
    source_times = [
        instant(m["at"])
        for m in case["messages"]
        if m["account"] == case["account"]
        and not m["deleted"]
        and now - timedelta(days=90) < instant(m["at"]) <= instant(case["cutoff"])
    ]
    reader.snapshot = reader.snapshot.model_copy(
        update={
            "source_revision": case["source_revision"],
            "projection_generation": case["projection_generation"],
            "source_message_count": len(source_times),
            "retention_due_at": min(source_times) + timedelta(days=90)
            if source_times
            else None,
        }
    )
    unknown_count = len(case["expected"]["undetermined"])

    def prepared(session, question, after, budget):
        page = reader.page(session, question, after, budget)
        budget.consume(unknown_count)
        return page.model_copy(
            update={
                "coverage": QuestionCoverage(
                    history=case["coverage"], ordering="unknown"
                ),
                "evaluated_conversation_count": len(rows) + unknown_count,
                "undetermined_conversation_count": unknown_count,
            }
        )

    result = service(
        reader, prepared, question=case["question"], clock=lambda: now
    ).execute(
        policy(case["account"]),
        {
            "question": case["question"],
            "start": case["start"],
            "end": case["end"],
            "cutoff": case["cutoff"],
            "timezone": "UTC",
        },
    )
    assert {r.conversation_ref for r in result.page.rows} == {
        conversation_ref(case["account"], c) for c in case["expected"]["matches"]
    }
    assert result.page.undetermined_conversation_count == unknown_count
    assert result.page.coverage.history == case["coverage"]
    assert result.availability == case["expected"]["availability"]


@pytest.mark.parametrize("total,more", [(2, False), (1, True), (99, False)])
def test_exact_totals_must_agree_with_a_complete_first_page(total, more):
    reader = PreparedReader([row("1")])

    def invalid_total(session, question, after, budget):
        page = reader.page(session, question, after, budget)
        return page.model_copy(
            update={"total_matching_conversations": total, "has_more": more}
        )

    with pytest.raises(QuestionResultInvalid):
        service(reader, invalid_total).execute(policy(), plan())


def test_retention_clipping_is_separate_from_requested_dates():
    result = service(PreparedReader()).execute(
        policy(), plan(start=NOW - timedelta(days=100))
    )
    assert result.question.selection_clipped_by_retention
    assert result.question.plan.start == NOW - timedelta(days=100)
    assert result.question.retention_cutoff_exclusive == NOW - timedelta(days=90)


@pytest.mark.parametrize("with_rows", [False, True])
def test_source_expiry_during_execution_suppresses_the_result(with_rows):
    reader = PreparedReader([row("1")] if with_rows else [])
    reader.snapshot = reader.snapshot.model_copy(
        update={"retention_due_at": NOW + timedelta(seconds=1)}
    )
    clock = [NOW]

    def expires(session, question, after, budget):
        page = reader.page(session, question, after, budget)
        clock[0] += timedelta(seconds=1)
        return page

    with pytest.raises(ProjectionUnavailable) as error:
        service(reader, expires, clock=lambda: clock[0]).execute(policy(), plan())
    assert error.value.reason_code == "analytics_question_source_expired"
    assert reader.checks == 2


def test_empty_result_requires_a_current_canonical_witness():
    reader = PreparedReader()
    reader.changed = True
    with pytest.raises(ProjectionUnavailable):
        service(reader).execute(policy(), plan())
    assert reader.calls == 0


def test_continuing_pages_does_not_extend_cursor_lifetime():
    reader = PreparedReader([row(str(i)) for i in range(4)])
    clock = [NOW]
    executor = service(reader, clock=lambda: clock[0])
    first = executor.execute(policy(), plan(page_size=1))
    clock[0] += timedelta(minutes=10)
    second = executor.execute(policy(), plan(page_size=1, cursor=first.next_cursor))
    assert second.next_cursor is not None
    clock[0] = NOW + timedelta(minutes=15)
    with pytest.raises(StaleQuestionCursor):
        executor.execute(policy(), plan(page_size=1, cursor=second.next_cursor))
    assert reader.calls == 2
