"""Select range evidence and latest-message ties without loading a whole thread."""

from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.opaque_refs import conversation_ref, message_ref
from app.analytics.query_contracts import utc_instant
from app.analytics.query_facts import QuestionConversation, QuestionMessage

# SQLite's date index is a coarse filter; Python verifies exact source instants.
DATE_TOLERANCE_DAYS = 0.00002


def reply_conversations(scope, question, budget):
    from app.analytics.query_canonical import LIVE_MESSAGE

    db, account = scope.connection, scope.account
    chats = db.execute('''SELECT chat_id FROM account_chats
        WHERE creator_account_id=? AND is_deleted=0 ORDER BY chat_id LIMIT ?''',
        (account, budget.limits.max_records - budget.records_examined + 1))

    def records(chat, start, end):
        return db.execute('''SELECT m.message_id,m.sent_at,m.direction,
            m.sender_platform_user_id,c.platform_user_id,julianday(m.sent_at) AS indexed_time
            FROM account_messages m JOIN account_chats c
              ON c.creator_account_id=m.creator_account_id AND c.chat_id=m.chat_id
            WHERE m.creator_account_id=? AND m.chat_id=? AND m.is_deleted=0 AND c.is_deleted=0
              AND julianday(m.sent_at)>=julianday(?)-?
              AND julianday(m.sent_at)<=julianday(?)+?''' + LIVE_MESSAGE + '''
            ORDER BY julianday(m.sent_at) DESC,m.message_id LIMIT ?''',
            (account, chat, start.isoformat(), DATE_TOLERANCE_DAYS,
             end.isoformat(), DATE_TOLERANCE_DAYS,
             budget.limits.max_records - budget.records_examined + 1))

    def message(row, chat):
        ref = message_ref(account, chat, str(row['message_id']))
        scope.locations[ref] = EvidenceLocation(conversation_id=chat, message_id=str(row['message_id']))
        role = 'creator' if row['direction'] == 'outbound' else (
            'participant' if row['sender_platform_user_id'] == row['platform_user_id'] else 'unknown')
        return QuestionMessage(ref, utc_instant(row['sent_at']), role, 'unknown')

    for candidate in chats:
        budget.consume()
        chat = str(candidate[0])
        ref = conversation_ref(account, chat)
        if question.plan.filters.conversation_ref not in {None, ref}:
            continue
        selected = None
        for row in records(chat, max(question.plan.start, question.retention_cutoff_exclusive),
                           min(question.plan.end, question.cutoff)):
            budget.consume()
            at = utc_instant(row['sent_at'])
            if question.plan.start <= at < question.plan.end and question.retention_cutoff_exclusive < at <= question.cutoff:
                selected = row
                break
        if selected is None:
            continue
        latest, newest, indexed_end = [], None, None
        for row in records(chat, question.retention_cutoff_exclusive, question.cutoff):
            budget.consume()
            if indexed_end is not None and row['indexed_time'] < indexed_end - DATE_TOLERANCE_DAYS:
                break
            at = utc_instant(row['sent_at'])
            if not question.retention_cutoff_exclusive < at <= question.cutoff:
                continue
            if indexed_end is None:
                indexed_end = row['indexed_time']
            if newest is None or at > newest:
                latest, newest = [row], at
            elif at == newest:
                latest.append(row)
        rows = {str(row['message_id']): row for row in [selected, *latest]}
        yield QuestionConversation(ref, tuple(message(row, chat) for row in rows.values()), 'unknown')
    scope.check(budget)
