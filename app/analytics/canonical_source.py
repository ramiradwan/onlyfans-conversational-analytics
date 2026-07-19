"""Immutable analytics snapshots read from signer-v2 canonical history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from app.persistence.history import HistoryRepository
from app.transport.ingestion import AccountReadModel


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

    def _read(self):
        if self.connection is not None:
            from contextlib import nullcontext

            return nullcontext(self.connection)
        return self.history.database.read()

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
