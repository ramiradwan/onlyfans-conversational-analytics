from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.database import CanonicalSQLite
from app.persistence.history import HistoryRepository
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention

ACCOUNT = "creator-account"
NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def database(path: Path) -> CanonicalSQLite:
    db = CanonicalSQLite(path)
    MigrationRunner(db).run()
    return db


def seed(db: CanonicalSQLite) -> None:
    now = NOW.isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id,updated_at) VALUES (?,?)",
            (ACCOUNT, now),
        )
        connection.execute(
            """INSERT INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at,lifecycle_origin,lifecycle_started_at)
               VALUES (?,'chat-1','full','participant-1','Before',?,'old-hash',1,1,
                       'event-1',0,?,'ordinary',?)""",
            (ACCOUNT, now, now, now),
        )
        connection.execute(
            """INSERT INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES (?,'message-1','chat-1','participant-1','private text',?,'inbound',
                       NULL,'message-hash',1,1,'event-1',0,?,'ordinary',?)""",
            (ACCOUNT, now, now, now),
        )


def test_upstream_change_and_tombstone_are_not_creator_deletion_authority(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed(db)
    history = HistoryRepository(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    later = (NOW + timedelta(minutes=1)).isoformat()

    with db.transaction() as connection:
        assert history._merge_chat(
            connection,
            ACCOUNT,
            {
                "chat_id": "chat-1",
                "record_kind": "full",
                "platform_user_id": "participant-1",
                "display_name": "After",
                "updated_at": later,
            },
            2,
            2,
            "event-2",
            later,
        )
    assert retention.barriers(ACCOUNT) == []

    with db.transaction() as connection:
        history._delete_entity(
            connection,
            ACCOUNT,
            "message",
            "message-1",
            "chat-1",
            2,
            3,
            "event-3",
            later,
        )
        assert connection.execute(
            """SELECT 1 FROM entity_tombstones
               WHERE creator_account_id=? AND entity_kind='message' AND entity_id='message-1'""",
            (ACCOUNT,),
        ).fetchone() is not None
    assert retention.barriers(ACCOUNT) == []

    retention.delete_message(ACCOUNT, "message-1")
    assert retention.barriers(ACCOUNT)[0]["scope_key"] == "message-1"
