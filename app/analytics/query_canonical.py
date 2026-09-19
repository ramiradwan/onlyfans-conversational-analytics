"""Bounded source selection within a gateway-owned canonical connection."""

from app.analytics.errors import CanonicalAccountNotFound, ProjectionUnavailable
from app.analytics.evidence_contracts import EvidenceLocation
from app.analytics.opaque_refs import conversation_ref, message_ref
from app.analytics.query_contracts import utc_instant
from app.analytics.query_facts import QuestionConversation, QuestionMessage


class CanonicalQuestionScope:
    def __init__(self, connection, account, budget):
        self.connection, self.account = connection, account
        self.token = connection.execute("PRAGMA data_version").fetchone()[0]
        row = connection.execute("SELECT canonical_revision FROM account_heads WHERE creator_account_id=?",
                                 (account,)).fetchone()
        if row is None:
            raise CanonicalAccountNotFound()
        self.revision = int(row[0])
        self.locations = {}
        self.check(budget)

    def check(self, budget):
        budget.check()
        current = self.connection.execute("PRAGMA data_version").fetchone()[0]
        if current != self.token:
            raise ProjectionUnavailable(availability="building")

    def conversations(self, question, budget):
        if question.plan.question == "no_later_creator_reply.v1":
            from app.analytics.query_reply_source import reply_conversations
            yield from reply_conversations(self, question, budget)
            return
        db, account = self.connection, self.account
        candidates = db.execute("""SELECT DISTINCT m.chat_id
            FROM account_messages AS m JOIN account_chats AS c
              ON c.creator_account_id=m.creator_account_id AND c.chat_id=m.chat_id
            WHERE m.creator_account_id=? AND m.is_deleted=0 AND c.is_deleted=0
              AND julianday(m.sent_at)>=julianday(?)-0.00002
              AND julianday(m.sent_at)<=julianday(?)+0.00002
            ORDER BY m.chat_id LIMIT ?""",
            (account, question.plan.start.isoformat(), question.plan.end.isoformat(),
             budget.limits.max_records - budget.records_examined + 1)).fetchall()
        budget.consume(len(candidates))
        for candidate in candidates:
            chat = str(candidate[0])
            cref = conversation_ref(account, chat)
            if question.plan.filters.conversation_ref not in {None, cref}:
                continue
            records = db.execute("""SELECT m.message_id,m.sent_at,m.direction,
                    m.sender_platform_user_id,c.platform_user_id
                FROM account_messages AS m JOIN account_chats AS c
                  ON c.creator_account_id=m.creator_account_id AND c.chat_id=m.chat_id
                WHERE m.creator_account_id=? AND m.chat_id=?
                  AND m.is_deleted=0 AND c.is_deleted=0
                  AND julianday(m.sent_at)>=julianday(?)-0.00002
                  AND julianday(m.sent_at)<=julianday(?)+0.00002
                """ + LIVE_MESSAGE + " ORDER BY m.sent_at,m.message_id LIMIT ?",
                (account, chat, question.retention_cutoff_exclusive.isoformat(),
                 question.cutoff.isoformat(),
                 budget.limits.max_records - budget.records_examined + 1)).fetchall()
            budget.consume(len(records))
            messages = []
            for record in records:
                budget.check()
                at = utc_instant(record["sent_at"])
                if not question.retention_cutoff_exclusive < at <= question.cutoff:
                    continue
                ref = message_ref(account, chat, str(record["message_id"]))
                self.locations[ref] = EvidenceLocation(conversation_id=chat,
                                                       message_id=str(record["message_id"]))
                role = "creator" if record["direction"] == "outbound" else (
                    "participant" if record["sender_platform_user_id"] == record["platform_user_id"] else "unknown")
                messages.append(QuestionMessage(ref, at, role, "unknown"))
            yield QuestionConversation(cref, tuple(messages), "unknown")
        self.check(budget)


LIVE_MESSAGE = """
    AND NOT EXISTS (SELECT 1 FROM entity_tombstones AS t
        WHERE t.creator_account_id=m.creator_account_id
          AND ((t.entity_kind='message' AND t.entity_id=m.message_id)
            OR (t.entity_kind='chat' AND t.entity_id=m.chat_id)))
    AND NOT EXISTS (SELECT 1 FROM deletion_barriers AS b
        WHERE b.creator_account_id=m.creator_account_id
          AND ((b.scope_kind='account' AND b.scope_key='*')
            OR (b.scope_kind='conversation' AND b.scope_key=m.chat_id)
            OR (b.scope_kind='message' AND b.scope_key=m.message_id)
            OR (b.scope_kind='participant' AND b.scope_key=c.platform_user_id)))
    AND NOT EXISTS (SELECT 1 FROM participant_deletion_chat_scopes AS p
        WHERE p.creator_account_id=m.creator_account_id AND p.chat_id=m.chat_id)
"""
