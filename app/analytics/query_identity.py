"""Stream the canonical account digest without retaining an account read model."""

import hashlib
import json
from datetime import datetime

from app.analytics.identity import CanonicalIdentity
from app.analytics.query_execution import QuestionLimitExceeded


def canonical_question_identity(db, account, revision, budget):
    digest = hashlib.sha256(b"ofca:canonical-account:v1\0")

    def emit(value):
        budget.check()
        digest.update(value.encode("utf-8"))

    def encoded(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def iso(value):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("canonical_time_invalid")
        return result.isoformat()

    emit('{"conversations":{')
    first_chat = True
    chats = db.execute("""SELECT chat_id,platform_user_id,display_name FROM account_chats
        WHERE creator_account_id=? AND is_deleted=0 ORDER BY chat_id""", (account,))
    for chat in chats:
        budget.consume()
        if not first_chat:
            emit(',')
        first_chat = False
        latest = db.execute("""SELECT sent_at FROM account_messages
            WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
            ORDER BY sent_at DESC,winning_stream_epoch DESC,winning_source_seq DESC,message_id DESC
            LIMIT 1""", (account, chat["chat_id"])).fetchone()
        emit(encoded(chat["chat_id"]) + ':{"conversation_id":' + encoded(chat["chat_id"]))
        emit(',"display_name":' + encoded(chat["display_name"]))
        emit(',"last_message_at":' + encoded(iso(latest[0]) if latest else None) + ',"messages":[')
        messages = db.execute("""SELECT message_id,substr(text,1,65537) AS text,sent_at,direction
            FROM account_messages WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
            ORDER BY sent_at,winning_stream_epoch,winning_source_seq,message_id""", (account, chat["chat_id"]))
        for ordinal, message in enumerate(messages):
            budget.consume()
            if len(message["text"]) > 65536:
                raise QuestionLimitExceeded()
            if ordinal:
                emit(',')
            emit(encoded({"message_id": str(message["message_id"]), "source_ordinal": ordinal,
                "text": str(message["text"]), "sent_at": iso(str(message["sent_at"])),
                "direction": str(message["direction"]), "sentiment": None}))
        participant = chat["platform_user_id"] or 'placeholder:' + chat["chat_id"]
        emit('],"platform_user_id":' + encoded(participant) + ',"unread_count":0}')
    emit('},"view_revision":' + str(revision) + '}')
    return CanonicalIdentity(revision=revision, content_digest="sha256:" + digest.hexdigest())
