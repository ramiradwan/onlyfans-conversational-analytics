"""Descriptive questions over versioned source facts."""

from datetime import timezone

from app.analytics.query_contracts import (
    ACTIVITY_WEEK, ActivityQuestionRow, PagePosition, QuestionCoverage, QuestionPage,
    QuestionRow, activity_declined,
)
from app.analytics.query_facts import QuestionFactsSession


def _human(message):
    return message.kind in {"message", "attachment"} and message.role in {"participant", "creator"}


def _system(message):
    return message.kind == "system"


def _last(messages):
    if all(m.ordering == "source" and m.order is not None for m in messages):
        end = max(m.order for m in messages)
        return [m for m in messages if m.order == end]
    newest = max(m.sent_at for m in messages)
    tied = [m for m in messages if m.sent_at == newest]
    if len(tied) > 1 and all(m.ordering == "source" and m.order is not None for m in tied):
        end = max(m.order for m in tied)
        return [m for m in tied if m.order == end]
    return tied


def no_later_creator_reply(session, question, after, budget):
    return _page(session, question, after, budget, reason="no_later_creator_reply")


def pricing_discussions(session, question, after, budget):
    return _page(session, question, after, budget, reason="pricing_discussion")


def participant_activity_decline(session, question, after, budget):
    return _page(session, question, after, budget, reason="participant_activity_decline")


def _activity_match(messages, split):
    earlier = [m for m in messages if m.role == "participant" and m.sent_at < split]
    recent = [m for m in messages if m.role == "participant" and m.sent_at >= split]
    if (not activity_declined(len(earlier), len(recent))
            or len({m.sent_at.astimezone(timezone.utc).date() for m in earlier}) < 2):
        return [], None
    anchors = [max(earlier, key=lambda m: m.sent_at)]
    if recent:
        anchors.append(max(recent, key=lambda m: m.sent_at))
    return anchors, (len(earlier), len(recent))


def _page(session: QuestionFactsSession, question, after, budget, *, reason):
    pricing = reason == "pricing_discussion"
    activity = reason == "participant_activity_decline"
    candidates = []
    evaluated = undetermined = total = eligible = analyzed = 0
    coverage_values, ordering_values = set(), set()
    for conversation in session.conversations(question, budget):
        budget.check()
        if (question.plan.filters.conversation_ref is not None
                and conversation.conversation_ref != question.plan.filters.conversation_ref):
            continue
        messages = [m for m in conversation.messages
                    if question.retention_cutoff_exclusive < m.sent_at <= question.cutoff
                    and not _system(m)]
        selected = [m for m in messages if question.plan.start <= m.sent_at
                    and (m.sent_at < question.plan.end or (activity and m.sent_at == question.plan.end))]
        if not selected and not (activity and conversation.has_removed_activity):
            continue
        evaluated += 1
        coverage_values.add(conversation.coverage)
        ordering_values.update(m.ordering for m in messages)
        matching = []
        counts = None
        if activity:
            unknown = (conversation.coverage != "complete" or question.selection_clipped_by_retention
                       or any(not _human(m) for m in selected)
                       or len({m.sent_at for m in selected}) != len(selected))
            if not unknown:
                matching, counts = _activity_match(selected, question.plan.end - ACTIVITY_WEEK)
        elif pricing:
            eligible += len(selected)
            usable = [m for m in selected if _human(m) and m.classification_current
                      and m.language == "en" and m.pricing in {"positive", "negative"}]
            analyzed += len(usable)
            matching = [m for m in usable if m.pricing == "positive"]
            unknown = len(usable) != len(selected)
        else:
            latest = _last(messages)
            unknown = len(latest) != 1 or any(not _human(m) for m in latest)
            if not unknown and latest[0].role == "participant":
                matching = [min(latest, key=lambda m: m.message_ref)]
        if not matching:
            undetermined += int(unknown)
            continue
        total += 1
        matching.sort(key=lambda m: (-m.sent_at.timestamp(), m.message_ref))
        position = PagePosition(evidence_at=matching[0].sent_at,
                                conversation_ref=conversation.conversation_ref)
        if after is not None and not after.precedes(position):
            continue
        candidates.append((position, conversation.coverage, matching[:32], len(matching) > 32, counts))
        candidates.sort(key=lambda item: (-item[0].evidence_at.timestamp(), item[0].conversation_ref))
        del candidates[question.plan.page_size + 1:]
    rows, reference_count = [], 0
    for position, coverage, messages, truncated, counts in candidates[:question.plan.page_size]:
        remaining_rows = min(len(candidates), question.plan.page_size) - len(rows) - 1
        limit = min(len(messages), 512 - reference_count - remaining_rows)
        references = tuple(session.evidence(m, budget)[0] for m in messages[:limit])
        reference_count += len(references)
        fields = dict(account_ref=question.account_ref,
            conversation_ref=position.conversation_ref, latest_evidence_at=position.evidence_at,
            reason=reason, coverage=coverage, evidence=references,
            evidence_truncated=truncated or limit < len(messages))
        rows.append(QuestionRow(**fields) if counts is None else
                    ActivityQuestionRow(**fields, earlier_messages=counts[0], recent_messages=counts[1]))
    history = "unknown" if not coverage_values or "unknown" in coverage_values else (
        "partial" if "partial" in coverage_values else "complete")
    ordering = "unknown" if not ordering_values else (
        "source" if ordering_values == {"source"} else "inferred")
    return QuestionPage(rows=tuple(rows),
        coverage=QuestionCoverage(history=history, ordering=ordering,
            eligible_classification_count=eligible if pricing else None,
            analyzed_classification_count=analyzed if pricing else None,
            supported_languages=("en",) if pricing else ()),
        evaluated_conversation_count=evaluated,
        undetermined_conversation_count=undetermined,
        total_matching_conversations=total, has_more=len(candidates) > question.plan.page_size)
