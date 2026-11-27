"""Fictional inboxes with literal expected weekly counts for the activity question."""

import json
from datetime import timedelta
from uuid import uuid4

from tests.analytics_coverage_fixture import seed_coverage
from tests.test_history_kind_adoption import synthetic_kind


def seed_activity_inbox(db, account, through, *, steady=False):
    """Create a fresh account. The second account can reuse all the native IDs."""
    generation = str(uuid4())
    db.execute("INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,1,?)",
               (account, through.isoformat()))
    weeks = {
        "orchard": ([14, 13, 12, 11, 10, 8], [7, 3, 0]),
        "harbor": ([13, 12, 11, 10, 8], []),
        "meadow": ([13, 12, 11, 10, 8], [7, 5, 3, 1]),
        "uncovered": ([13, 12, 11, 10, 8], []),
    }
    for chat, (earlier, recent) in weeks.items():
        db.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,platform_user_id,
            display_name,content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
            VALUES (?,?,'full',?,?,'fixture-chat',1,1,0,?)""",
            (account, chat, chat, chat.title(), through.isoformat()))
        times = [through - timedelta(days=d) for d in earlier + (list(range(6)) if steady else recent)]
        kind = synthetic_kind(conversation=chat)
        kind["context"].update(account_id=account, generation_id=generation)
        for n, instant in enumerate(times):
            db.execute("""INSERT INTO account_messages(creator_account_id,message_id,chat_id,
                sender_platform_user_id,text,sent_at,direction,content_hash,winning_stream_epoch,
                winning_source_seq,is_deleted,updated_at,event_kind_json)
                VALUES (?,?,?,?,?,?,'inbound','fixture-message',1,?,0,?,?)""",
                (account, f"{chat}-{n}", chat, chat, f"Fictional {chat} note {n}", instant.isoformat(),
                 n + 1, through.isoformat(), json.dumps(kind)))
    seed_coverage(db, account, ("orchard", "harbor", "meadow"), through, generation=generation)
    db.execute("""INSERT INTO projection_work(creator_account_id,canonical_revision,work_kind,conversation_id,created_at)
        VALUES (?,1,'reseed',NULL,?)""", (account, through.isoformat()))
