"""Compare real question handlers with hand-authored expected answers."""

from datetime import datetime, timedelta, timezone
import hashlib
from collections import defaultdict

import pytest

from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.query_contracts import QuestionEvidence, QuestionPlan, QuestionSnapshot, ResolvedQuestion
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.analytics.query_facts import QuestionConversation, QuestionMessage
from app.analytics.query_handlers import no_later_creator_reply, participant_activity_decline, pricing_discussions
from tests.state_models.analytics_question_cases import instant, load_cases

pytestmark = [pytest.mark.ci_tier('fast')]


def sha(value):
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class FixtureFacts:
    def __init__(self, case):
        self.case = case
        self.references = {}

    def conversations(self, question, budget):
        grouped = defaultdict(list)
        for m in self.case["messages"]:
            if m["account"] != self.case["account"] or m["deleted"]:
                continue
            budget.consume()
            ref = message_ref(m["account"], m["conversation"], m["id"])
            self.references[ref] = QuestionEvidence(
                account_ref=account_ref(m["account"]),
                conversation_ref=conversation_ref(m["account"], m["conversation"]),
                message_ref=ref, source_revision=self.case["source_revision"],
                source_version_digest=sha(str(m["version"])), sent_at=m["at"])
            grouped[m["conversation"]].append(QuestionMessage(
                ref, instant(m["at"]), m["role"], m["kind"], m["order"], m["ordering"],
                m["pricing"], m["finding_version"] == m["version"], m["language"]))
        for chat, messages in grouped.items():
            yield QuestionConversation(conversation_ref(self.case["account"], chat),
                                       tuple(messages), self.case["coverage"])

    def evidence(self, message, budget):
        budget.consume()
        return self.references[message.message_ref], None


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_handlers_match_literal_cases(case):
    now = instant(case["now"])
    plan = QuestionPlan(question=case["question"], start=case["start"], end=case["end"],
                        cutoff=case["cutoff"], timezone="UTC")
    snapshot = QuestionSnapshot(account_ref=account_ref(case["account"]),
        source_revision=case["source_revision"], projection_generation=case["projection_generation"],
        generation_id="synthetic-generation", canonical_content_digest=sha("canonical"),
        projection_digest=sha("projection"), derived_at=now, source_message_count=1,
        retention_due_at=now + timedelta(days=1))
    resolved = ResolvedQuestion(plan=plan, account_ref=snapshot.account_ref,
        cutoff=plan.cutoff, retention_cutoff_exclusive=now-timedelta(days=90),
        selection_clipped_by_retention=False, definition_digest=sha("definition"), snapshot=snapshot)
    handler = pricing_discussions if plan.question == "pricing_discussions.v1" else no_later_creator_reply
    page = handler(FixtureFacts(case), resolved, None, QuestionBudget(QuestionLimits()))
    assert {r.conversation_ref for r in page.rows} == {
        conversation_ref(case["account"], c) for c in case["expected"]["matches"]}
    assert page.undetermined_conversation_count == len(case["expected"]["undetermined"])
    assert {r.conversation_ref: {e.message_ref for e in r.evidence} for r in page.rows} == {
        conversation_ref(case["account"], c): {message_ref(case["account"], c, m) for m in refs}
        for c, refs in case["expected"]["evidence"].items()}


def test_authoritative_order_takes_precedence_over_message_clock():
    from copy import deepcopy

    case = deepcopy(next(c for c in load_cases() if c["id"] == "reply-after-selected-window"))
    case["messages"][1]["at"] = "2026-09-03T12:00:00Z"
    # Source sequence places the creator response after the participant message.
    assert case["messages"][1]["order"] > case["messages"][0]["order"]
    test_handlers_match_literal_cases(case)


@pytest.mark.parametrize(("earlier", "recent", "expected"), [
    ([14, 13, 12, 11, 8], [7, 0], True),        # Both outside edges and the shared boundary.
    ([13, 12, 11, 10], [], False),               # Fewer than five in the first week.
    ([13, 12, 11, 10, 8], [7, 4, 0], False),    # Only two fewer.
    ([14, 13, 12, 11, 10, 9, 8], [7, 5, 3, 0], False),  # Three fewer, less than half.
    ([14, 13, 12, 11, 10, 8], [7, 4, 0], True),         # Exactly half.
    ([9, 9.01, 9.02, 9.03, 9.04], [], False),  # One UTC date.
    ([13, 12, 11, 10, 8], [], True),             # Complete recent week with no participant messages.
])
def test_activity_compares_participant_counts_only(earlier, recent, expected):
    from types import SimpleNamespace

    through = datetime(2026, 10, 28, 10, tzinfo=timezone.utc)
    start = through - timedelta(days=14)
    account, chat = "fictional", "book-club"
    aref, cref = account_ref(account), conversation_ref(account, chat)
    messages = []
    for age, role, kind in [
        *((age, "participant", "message") for age in earlier + recent),
        (14.01, "participant", "message"), (-0.01, "participant", "message"),
        (4.5, "creator", "message"), (5.5, "system", "system"),
    ]:
        messages.append(QuestionMessage(message_ref(account, chat, str(len(messages))),
            through - timedelta(days=age), role, kind))
    plan = QuestionPlan(question="participant_activity_decline.v1", start=start, end=through, cutoff=through,
                        timezone="Europe/Helsinki")
    resolved = SimpleNamespace(plan=plan, account_ref=aref, cutoff=through,
                               retention_cutoff_exclusive=start-timedelta(days=1), selection_clipped_by_retention=False)

    def answer(values, coverage="complete"):
        session = SimpleNamespace(conversations=lambda q,b: [QuestionConversation(cref, tuple(values), coverage)],
            evidence=lambda m,b: (QuestionEvidence(account_ref=aref, conversation_ref=cref, message_ref=m.message_ref,
                source_revision=1, source_version_digest=sha("source"), sent_at=m.sent_at), None))
        return participant_activity_decline(session, resolved, None, QuestionBudget(QuestionLimits()))

    page = answer(messages)
    assert page.total_matching_conversations == int(expected) and page.undetermined_conversation_count == 0
    if expected:
        row = page.rows[0]
        assert (row.earlier_messages, row.recent_messages) == (len(earlier), len(recent))
        assert len(row.evidence) == (2 if recent else 1)
    for coverage in ("partial", "unknown"):
        page = answer(messages, coverage)
        assert not page.rows and page.undetermined_conversation_count == 1
    for role, kind, age in (("participant", "unknown", 6), ("unknown", "message", 6),
                            ("participant", "message", earlier[0])):
        page = answer([*messages, QuestionMessage(message_ref(account, chat, "ambiguous"),
                      through-timedelta(days=age), role, kind)])
        assert not page.rows and page.undetermined_conversation_count == 1
    resolved.selection_clipped_by_retention = True
    assert answer(messages).undetermined_conversation_count == 1
