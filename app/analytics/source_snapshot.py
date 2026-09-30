"""Stream canonical identities and load individual conversations on demand."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from typing import Callable

from app.analytics.errors import CanonicalRevisionChanged
from app.analytics.identity import CanonicalIdentity


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def instant(value: str) -> str:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("canonical_time_invalid")
    return result.isoformat()


def conversation_digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(encoded(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceCatalog:
    identity: CanonicalIdentity
    digests: dict[str, str] = field(repr=False)
    load: Callable[[str], dict | None] = field(repr=False, compare=False)
    scanned_messages: int = 0
    identity_proof: object | None = field(default=None, repr=False, compare=False)

    @property
    def view_revision(self) -> int:
        return self.identity.revision

    def conversation(self, conversation_id: str) -> dict:
        value = self.load(conversation_id)
        if value is None or conversation_digest(value) != self.digests[conversation_id]:
            raise CanonicalRevisionChanged()
        return value


def scan_identity(db, account_id: str, revision: int, *, check=lambda: None,
                  consume=lambda: None, max_text: int | None = None):
    """Match canonical JSON byte for byte while streaming the account once.

    The digest contract is unchanged.  Chat metadata and latest timestamps are
    read in one grouped query and all live messages are consumed from one
    account-ordered cursor, avoiding two message queries per conversation.
    """

    account_hash = hashlib.sha256(b"ofca:canonical-account:v1\0")
    digests, message_count = {}, 0

    def emit(text: str, conversation_hash=None) -> None:
        check()
        data = text.encode("utf-8")
        account_hash.update(data)
        if conversation_hash is not None:
            conversation_hash.update(data)

    text_column = "m.text" if max_text is None else "substr(m.text,1,?) AS text"
    message_parameters = (account_id,) if max_text is None else (max_text + 1, account_id)
    chats = db.execute(
        """SELECT c.chat_id,c.platform_user_id,c.display_name,MAX(m.sent_at) AS last_message_at
             FROM account_chats AS c
             LEFT JOIN account_messages AS m
               ON m.creator_account_id=c.creator_account_id
              AND m.chat_id=c.chat_id AND m.is_deleted=0
            WHERE c.creator_account_id=? AND c.is_deleted=0
            GROUP BY c.chat_id,c.platform_user_id,c.display_name
            ORDER BY c.chat_id""",
        (account_id,),
    )
    messages = db.execute(
        f"""SELECT m.chat_id,m.message_id,{text_column},m.sent_at,m.direction
              FROM account_messages AS m
              JOIN account_chats AS c
                ON c.creator_account_id=m.creator_account_id
               AND c.chat_id=m.chat_id AND c.is_deleted=0
             WHERE m.creator_account_id=? AND m.is_deleted=0
             ORDER BY m.chat_id,m.sent_at,m.winning_stream_epoch,
                      m.winning_source_seq,m.message_id""",
        message_parameters,
    )
    pending = messages.fetchone()

    emit('{"conversations":{')
    for ordinal, chat in enumerate(chats):
        consume()
        if ordinal:
            emit(',')
        chat_id = str(chat["chat_id"])
        emit(encoded(chat_id) + ':')
        part = hashlib.sha256()
        emit('{"conversation_id":' + encoded(chat_id), part)
        emit(',"display_name":' + encoded(chat["display_name"]), part)
        latest = chat["last_message_at"]
        emit(',"last_message_at":' + encoded(instant(str(latest)) if latest else None), part)
        emit(',"messages":[', part)
        index = 0
        while pending is not None and str(pending["chat_id"]) == chat_id:
            consume()
            if max_text is not None and len(pending["text"]) > max_text:
                from app.analytics.query_execution import QuestionLimitExceeded
                raise QuestionLimitExceeded()
            if index:
                emit(',', part)
            emit(encoded({"message_id": str(pending["message_id"]), "source_ordinal": index,
                "text": str(pending["text"]), "sent_at": instant(str(pending["sent_at"])),
                "direction": str(pending["direction"]), "sentiment": None}), part)
            message_count += 1
            index += 1
            pending = messages.fetchone()
        participant = chat["platform_user_id"] or 'placeholder:' + chat_id
        emit('],"platform_user_id":' + encoded(participant) + ',"unread_count":0}', part)
        digests[chat_id] = 'sha256:' + part.hexdigest()
    if pending is not None:
        raise ValueError("canonical_message_without_live_chat")
    emit('},"view_revision":' + str(revision) + '}')
    return CanonicalIdentity(revision, 'sha256:' + account_hash.hexdigest()), digests, message_count


def read_conversation(db, account_id: str, conversation_id: str, *, check=lambda: None):
    row = db.execute("""SELECT platform_user_id,display_name FROM account_chats
        WHERE creator_account_id=? AND chat_id=? AND is_deleted=0""",
        (account_id, conversation_id)).fetchone()
    if row is None:
        return None
    messages = []
    for index, item in enumerate(db.execute("""SELECT message_id,text,sent_at,direction
        FROM account_messages WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
        ORDER BY sent_at,winning_stream_epoch,winning_source_seq,message_id""",
        (account_id, conversation_id))):
        check()
        messages.append({"message_id": str(item[0]), "source_ordinal": index,
            "text": str(item[1]), "sent_at": instant(str(item[2])),
            "direction": str(item[3]), "sentiment": None})
    return {"conversation_id": conversation_id, "platform_user_id": row[0] or f"placeholder:{conversation_id}",
        "display_name": row[1], "unread_count": 0,
        "last_message_at": messages[-1]["sent_at"] if messages else None, "messages": messages}


@contextmanager
def cancellable_source_read(connection, cancellation_check):
    """Interrupt long SQL work as well as Python-side source iteration."""

    from app.analytics.cancellation import check_cancelled
    from app.persistence import sqlite_api

    check_cancelled(cancellation_check)
    if cancellation_check is None:
        yield
        return
    interrupted = []
    def progress():
        try:
            check_cancelled(cancellation_check)
        except Exception as error:
            interrupted.append(error)
            return 1
        return 0
    connection.set_progress_handler(progress, 100)
    try:
        yield
    except sqlite_api.OperationalError:
        if interrupted:
            raise interrupted[0] from None
        raise
    finally:
        connection.set_progress_handler(None, 0)
    check_cancelled(cancellation_check)
