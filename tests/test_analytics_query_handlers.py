"""Compare real question handlers with hand-authored expected answers."""

from datetime import timedelta
import hashlib
from collections import defaultdict

import pytest

from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.query_contracts import QuestionEvidence, QuestionPlan, QuestionSnapshot, ResolvedQuestion
from app.analytics.query_execution import QuestionBudget, QuestionLimits
from app.analytics.query_facts import QuestionConversation, QuestionMessage
from app.analytics.query_handlers import no_later_creator_reply, pricing_discussions
from tests.state_models.analytics_question_cases import instant, load_cases


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
