"""Stream canonical identities and load individual conversations on demand."""

from __future__ import annotations

import hashlib
import json
from json.encoder import encode_basestring
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime
from typing import Callable

from app.analytics.errors import CanonicalRevisionChanged
from app.analytics.identity import CanonicalIdentity
from app.analytics.source_coverage import AcquisitionCoverage


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def instant(value: str) -> str:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("canonical_time_invalid")
    return result.isoformat()


def conversation_digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(encoded(value).encode()).hexdigest()


def _encoded_message(message, source_ordinal: int) -> str:
    """Emit the established sorted canonical JSON without a per-message mapping."""

    return (
        '{"direction":' + encode_basestring(str(message["direction"]))
        + ',"message_id":' + encode_basestring(str(message["message_id"]))
        + ',"sent_at":' + encode_basestring(instant(str(message["sent_at"])))
        + ',"sentiment":null,"source_ordinal":' + str(source_ordinal)
        + ',"text":' + encode_basestring(str(message["text"])) + '}'
    )


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
    """Match canonical JSON byte for byte without holding message bodies."""

    account_hash = hashlib.sha256(b"ofca:canonical-account:v1\0")
    digests, message_count = {}, 0
    coverage = AcquisitionCoverage(db, account_id, consume=consume)

    def emit(text: str, conversation_hash=None) -> None:
        check()
        data = text.encode("utf-8")
        account_hash.update(data)
        if conversation_hash is not None:
            conversation_hash.update(data)

    emit('{"conversations":{')
    chats = db.execute("""SELECT chat_id,platform_user_id,display_name FROM account_chats
        WHERE creator_account_id=? AND is_deleted=0 ORDER BY chat_id""", (account_id,))
    for ordinal, chat in enumerate(chats):
        consume()
        if ordinal:
            emit(',')
        chat_id = str(chat["chat_id"])
        emit(encoded(chat_id) + ':')
        part = hashlib.sha256()
        latest = db.execute("""SELECT sent_at FROM account_messages
            WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
            ORDER BY sent_at DESC,winning_stream_epoch DESC,winning_source_seq DESC,message_id DESC
            LIMIT 1""", (account_id, chat_id)).fetchone()
        emit('{"acquisition_coverage":' + encoded(coverage.conversation(chat_id)), part)
        emit(',"conversation_id":' + encoded(chat_id), part)
        emit(',"display_name":' + encoded(chat["display_name"]), part)
        emit(',"last_message_at":' + encoded(instant(latest[0]) if latest else None), part)
        emit(',"messages":[', part)
        text_column = 'text' if max_text is None else 'substr(text,1,?) AS text'
        parameters = (account_id, chat_id) if max_text is None else (max_text + 1, account_id, chat_id)
        messages = db.execute(f"""SELECT message_id,{text_column},sent_at,direction
            FROM account_messages WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
            ORDER BY sent_at,winning_stream_epoch,winning_source_seq,message_id""", parameters)
        for index, message in enumerate(messages):
            consume()
            if max_text is not None and len(message["text"]) > max_text:
                from app.analytics.query_execution import QuestionLimitExceeded
                raise QuestionLimitExceeded()
            if index:
                emit(',', part)
            emit(_encoded_message(message, index), part)
            message_count += 1
        participant = chat["platform_user_id"] or 'placeholder:' + chat_id
        emit('],"platform_user_id":' + encoded(participant) + ',"unread_count":0}', part)
        digests[chat_id] = 'sha256:' + part.hexdigest()
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
    coverage = AcquisitionCoverage(db, account_id)
    check()
    return {"acquisition_coverage": coverage.conversation(conversation_id),
        "conversation_id": conversation_id, "platform_user_id": row[0] or f"placeholder:{conversation_id}",
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
