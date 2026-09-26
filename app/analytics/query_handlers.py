"""Descriptive reply and pricing queries over versioned source facts."""

from app.analytics.query_contracts import PagePosition, QuestionCoverage, QuestionPage, QuestionRow
from app.analytics.query_facts import QuestionFactsSession


def _human(message):
    return message.kind in {"message", "attachment"} and message.role in {"participant", "creator"}


def _system(message):
    return message.kind == "system" or message.role == "system"


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
    return _page(session, question, after, budget, pricing=False)


def pricing_discussions(session, question, after, budget):
    return _page(session, question, after, budget, pricing=True)


def _page(session: QuestionFactsSession, question, after, budget, *, pricing):
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
        selected = [m for m in messages if question.plan.start <= m.sent_at < question.plan.end]
        if not selected:
            continue
        evaluated += 1
        coverage_values.add(conversation.coverage)
        ordering_values.update(m.ordering for m in messages)
        matching = []
        if pricing:
            eligible += len(selected)
            usable = [m for m in selected if _human(m) and m.classification_current
                      and m.language == "en" and m.pricing in {"positive", "negative"}]
            analyzed += len(usable)
            matching = [m for m in usable if m.pricing == "positive"]
            unknown = len(usable) != len(selected)
        else:
            latest = _last(messages)
            unknown = any(not _human(m) for m in latest) or len({m.role for m in latest}) != 1
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
        candidates.append((position, conversation.coverage, matching[:32], len(matching) > 32))
        candidates.sort(key=lambda item: (-item[0].evidence_at.timestamp(), item[0].conversation_ref))
        del candidates[question.plan.page_size + 1:]
    rows, reference_count = [], 0
    for position, coverage, messages, truncated in candidates[:question.plan.page_size]:
        remaining_rows = min(len(candidates), question.plan.page_size) - len(rows) - 1
        limit = min(len(messages), 512 - reference_count - remaining_rows)
        references = tuple(session.evidence(m, budget)[0] for m in messages[:limit])
        reference_count += len(references)
        rows.append(QuestionRow(account_ref=question.account_ref,
            conversation_ref=position.conversation_ref, latest_evidence_at=position.evidence_at,
            reason="pricing_discussion" if pricing else "no_later_creator_reply",
            coverage=coverage, evidence=references,
            evidence_truncated=truncated or limit < len(messages)))
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
