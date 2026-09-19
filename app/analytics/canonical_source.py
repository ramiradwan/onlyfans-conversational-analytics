"""Immutable analytics snapshots read from signer-v2 canonical history."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
from app.persistence import sqlite_api as sqlite3
from datetime import datetime

from app.analytics.evidence_contracts import (
    EvidenceLocation, EvidenceMessage, MAX_EVIDENCE_TEXT_CHARS,
)
from app.analytics.query_execution import QuestionBudget
from app.canonical.read_models import AccountReadModel
from app.persistence.history import HistoryRepository


class HistoryAnalyticsSource:
    """Expose signer canonical history through the analytics read-source contract."""

    def __init__(
        self,
        history: HistoryRepository,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.history = history
        self.connection = connection
        from app.analytics.source_tokens import SourceIdentityCache
        self._identity_cache = SourceIdentityCache()

    def _read(self):
        if self.connection is not None:
            from contextlib import nullcontext

            return nullcontext(self.connection)
        return self.history.database.read()

    def analytics_snapshot(self, account_id: str, *, cancellation_check=None):
        from app.analytics.cancellation import check_cancelled
        from app.analytics.source_snapshot import SourceCatalog, scan_identity, cancellable_source_read
        from app.analytics.errors import CanonicalAccountNotFound

        check = lambda: check_cancelled(cancellation_check)
        with self._read() as connection, cancellable_source_read(connection, cancellation_check):
            own_transaction = self.connection is None and not connection.in_transaction
            if own_transaction:
                connection.execute("BEGIN")
            try:
                row = connection.execute("SELECT canonical_revision FROM account_heads WHERE creator_account_id=?", (account_id,)).fetchone()
                if row is None:
                    raise CanonicalAccountNotFound()
                token = self._identity_cache.token(connection, account_id) if self.connection is None else None
                identity, digests, count = scan_identity(connection, account_id, int(row[0]), check=check)
                check()
                self._identity_cache.put(account_id, token, identity)
            finally:
                if own_transaction:
                    connection.rollback()
        return SourceCatalog(identity, digests,
            lambda chat: self.conversation_read_model(account_id, chat, cancellation_check=cancellation_check), count)

    def read_identity(self, account_id: str):
        from app.analytics.errors import CanonicalAccountNotFound

        try:
            if self.connection is None:
                with self._read() as connection:
                    cached = self._identity_cache.get(account_id, self._identity_cache.token(connection, account_id))
                if cached is not None:
                    return cached
            return self.analytics_snapshot(account_id).identity
        except CanonicalAccountNotFound:
            return None

    def conversation_read_model(self, account_id: str, conversation_id: str, *, cancellation_check=None):
        from app.analytics.cancellation import check_cancelled
        from app.analytics.source_snapshot import read_conversation, cancellable_source_read

        with self._read() as connection, cancellable_source_read(connection, cancellation_check):
            return read_conversation(connection, account_id, conversation_id,
                check=lambda: check_cancelled(cancellation_check))

    @contextmanager
    def open_question_scope(self, account_id, budget):
        """Pin live canonical identity and bound every source read."""

        from app.analytics.query_canonical import CanonicalQuestionScope
        from app.analytics.query_identity import canonical_question_identity
        from app.analytics.query_sql import bounded_sql

        if self.connection is not None:
            raise ValueError("question_live_read_required")
        with self.history.database.read() as connection, bounded_sql(connection, budget):
            budget.consume(2)
            scope = CanonicalQuestionScope(connection, account_id, budget)
            token = self._identity_cache.token(connection, account_id)
            scope.identity = self._identity_cache.get(account_id, token)
            if scope.identity is None:
                scope.identity = canonical_question_identity(connection, account_id, scope.revision, budget)
                scope.check(budget)
                if token == self._identity_cache.token(connection, account_id):
                    self._identity_cache.put(account_id, token, scope.identity)
            scope.check(budget)
            yield scope
            scope.check(budget)

    def read_evidence_message(
        self, account_id: str, location: EvidenceLocation, budget: QuestionBudget,
    ) -> EvidenceMessage | None:
        """Read one live, undeleted source by its indexed canonical identity."""

        if self.connection is not None:
            raise ValueError("evidence_live_read_required")
        location = EvidenceLocation.model_validate(location)
        budget.check()
        with self.history.database.read() as connection:
            budget.check()
            timeout_ms = max(1, int(budget.remaining_seconds() * 1000))
            connection.execute(f"PRAGMA busy_timeout={timeout_ms}")
            connection.execute("PRAGMA query_only=ON")
            interrupted = []

            def progress() -> int:
                try:
                    budget.check()
                except Exception as error:
                    interrupted.append(error)
                    return 1
                return 0

            connection.set_progress_handler(progress, 100)
            try:
                budget.consume()
                row = connection.execute(
                    """SELECT h.canonical_revision,m.text,m.sent_at,m.direction,
                              m.sender_platform_user_id,m.upstream_updated_at,
                              m.content_hash,m.winning_stream_epoch,m.winning_source_seq
                         FROM account_messages AS m
                         JOIN account_heads AS h
                           ON h.creator_account_id=m.creator_account_id
                         JOIN account_chats AS c
                           ON c.creator_account_id=m.creator_account_id AND c.chat_id=m.chat_id
                        WHERE m.creator_account_id=? AND m.message_id=? AND m.chat_id=?
                          AND m.is_deleted=0 AND c.is_deleted=0 AND length(m.text)<=?
                          AND length(CAST(m.text AS BLOB))<=?
                          AND NOT EXISTS (
                              SELECT 1 FROM entity_tombstones AS t
                               WHERE t.creator_account_id=m.creator_account_id
                                 AND ((t.entity_kind='message' AND t.entity_id=m.message_id)
                                   OR (t.entity_kind='chat' AND t.entity_id=m.chat_id)))
                          AND NOT EXISTS (
                              SELECT 1 FROM deletion_barriers AS b
                               WHERE b.creator_account_id=m.creator_account_id
                                 AND ((b.scope_kind='account' AND b.scope_key='*')
                                   OR (b.scope_kind='conversation' AND b.scope_key=m.chat_id)
                                   OR (b.scope_kind='message' AND b.scope_key=m.message_id)
                                   OR (b.scope_kind='participant' AND b.scope_key=c.platform_user_id)))
                          AND NOT EXISTS (
                              SELECT 1 FROM participant_deletion_chat_scopes AS p
                               WHERE p.creator_account_id=m.creator_account_id AND p.chat_id=m.chat_id)
                        LIMIT 1""",
                    (account_id, location.message_id, location.conversation_id,
                     MAX_EVIDENCE_TEXT_CHARS, MAX_EVIDENCE_TEXT_CHARS * 4),
                ).fetchone()
            except sqlite3.OperationalError:
                if interrupted:
                    raise interrupted[0] from None
                budget.check()
                raise
            finally:
                connection.set_progress_handler(None, 0)
        budget.check()
        if row is None:
            return None
        return EvidenceMessage(
            account_id=account_id, location=location, source_revision=row[0],
            text=row[1], sent_at=row[2], direction=row[3], sender_id=row[4],
            upstream_updated_at=row[5], content_hash=row[6],
            stream_epoch=row[7], source_sequence=row[8],
        )

    def account_revision(self, account_id: str) -> int | None:
        with self._read() as connection:
            row = connection.execute("SELECT canonical_revision FROM account_heads WHERE creator_account_id=?", (account_id,)).fetchone()
        return None if row is None else int(row[0])

    def account_exists(self, creator_account_id: str) -> bool:
        with self._read() as connection:
            return connection.execute(
                "SELECT 1 FROM account_heads WHERE creator_account_id=?",
                (creator_account_id,),
            ).fetchone() is not None

    def account_revisions(self) -> list[tuple[str, int]]:
        with self._read() as connection:
            return [
                (str(row[0]), int(row[1]))
                for row in connection.execute(
                    """SELECT creator_account_id,canonical_revision FROM account_heads
                       ORDER BY creator_account_id"""
                )
            ]

    def account_read_model(self, creator_account_id: str) -> AccountReadModel:
        with self._read() as connection:
            head = connection.execute(
                """SELECT canonical_revision FROM account_heads
                   WHERE creator_account_id=?""",
                (creator_account_id,),
            ).fetchone()
            if head is None:
                return AccountReadModel()

            account = AccountReadModel(view_revision=int(head[0]))
            chat_rows = connection.execute(
                """SELECT chat_id,platform_user_id,display_name,upstream_updated_at
                     FROM account_chats
                    WHERE creator_account_id=? AND is_deleted=0 ORDER BY chat_id""",
                (creator_account_id,),
            ).fetchall()
            for row in chat_rows:
                account.conversations[str(row[0])] = {
                    "conversation_id": str(row[0]),
                    "platform_user_id": row[1] or f"placeholder:{row[0]}",
                    "display_name": row[2],
                    "unread_count": 0,
                    "last_message_at": None,
                    "messages": [],
                }

            messages = connection.execute(
                """SELECT chat_id,message_id,text,sent_at,direction,
                          winning_stream_epoch,winning_source_seq
                     FROM account_messages
                    WHERE creator_account_id=? AND is_deleted=0
                    ORDER BY chat_id,sent_at,winning_stream_epoch,
                             winning_source_seq,message_id""",
                (creator_account_id,),
            ).fetchall()
            ordinals: dict[str, int] = {}
            for row in messages:
                conversation_id = str(row[0])
                conversation = account.conversations.get(conversation_id)
                if conversation is None:
                    continue
                ordinal = ordinals.get(conversation_id, 0)
                ordinals[conversation_id] = ordinal + 1
                message = {
                    "message_id": str(row[1]),
                    "source_ordinal": ordinal,
                    "text": str(row[2]),
                    "sent_at": self._iso(str(row[3])),
                    "direction": str(row[4]),
                    "sentiment": None,
                }
                conversation["messages"].append(message)
                conversation["last_message_at"] = message["sent_at"]
            return account

    def canonical_content_digest(self, creator_account_id: str) -> str | None:
        if not self.account_exists(creator_account_id):
            return None
        account = self.account_read_model(creator_account_id)
        encoded = json.dumps(
            {
                "canonical_revision": account.view_revision,
                "conversations": account.conversations,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(
            b"ofca:canonical-account:v2\0" + encoded
        ).hexdigest()

    @staticmethod
    def _iso(value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("canonical message timestamp must include a timezone")
        return parsed.isoformat()
