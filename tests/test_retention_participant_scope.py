from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention


ACCOUNT = "creator-account"
NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def database(path: Path) -> CanonicalSQLite:
    value = CanonicalSQLite(path)
    MigrationRunner(value).run()
    return value


def seed_message(
    db: CanonicalSQLite,
    *,
    message_id: str,
    chat_id: str,
    participant_id: str,
    direction: str,
    sender_id: str,
) -> int:
    now = NOW.isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id,updated_at) VALUES (?,?)",
            (ACCOUNT, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at,lifecycle_origin,lifecycle_started_at)
               VALUES (?,?,'full',?,'Participant',?,'chat-hash',1,1,'chat-event',0,?,'ordinary',?)""",
            (ACCOUNT, chat_id, participant_id, now, now, now),
        )
        return connection.execute(
            """INSERT OR IGNORE INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES (?,?,?,?,?,?,?,NULL,?,1,1,?,0,?,'ordinary',?)""",
            (
                ACCOUNT,
                message_id,
                chat_id,
                sender_id,
                f"text-{message_id}",
                now,
                direction,
                f"hash-{message_id}",
                f"event-{message_id}",
                now,
                now,
            ),
        ).rowcount


def test_participant_delete_removes_whole_participant_conversation_and_fences_replay(
    tmp_path: Path,
) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(
        db,
        message_id="participant-authored",
        chat_id="participant-chat",
        participant_id="participant-1",
        direction="inbound",
        sender_id="participant-1",
    )
    seed_message(
        db,
        message_id="creator-authored",
        chat_id="participant-chat",
        participant_id="participant-1",
        direction="outbound",
        sender_id="creator-1",
    )
    seed_message(
        db,
        message_id="other",
        chat_id="other-chat",
        participant_id="participant-2",
        direction="inbound",
        sender_id="participant-2",
    )

    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.delete_participant(ACCOUNT, "participant-1")

    with db.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM account_chats WHERE creator_account_id=? AND chat_id='participant-chat'",
            (ACCOUNT,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM account_messages WHERE creator_account_id=? AND chat_id='participant-chat'",
            (ACCOUNT,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id='other'",
            (ACCOUNT,),
        ).fetchone() is not None

    # Ordinary replay cannot recreate the barred participant chat.
    assert seed_message(
        db,
        message_id="stale-creator-authored",
        chat_id="participant-chat",
        participant_id="participant-1",
        direction="outbound",
        sender_id="creator-1",
    ) == 0
    with db.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM account_chats WHERE creator_account_id=? AND chat_id='participant-chat'",
            (ACCOUNT,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id='stale-creator-authored'",
            (ACCOUNT,),
        ).fetchone() is None


def test_participant_barrier_blocks_snapshot_chat_reconstruction(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(
        db,
        message_id="m1",
        chat_id="participant-chat",
        participant_id="participant-1",
        direction="inbound",
        sender_id="participant-1",
    )
    CreatorVaultRetention(db, clock=lambda: NOW).delete_participant(
        ACCOUNT, "participant-1"
    )

    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO ingest_streams(
                   creator_account_id,agent_installation_id,agent_stream_id,created_at)
               VALUES (?,?,?,?)""",
            (ACCOUNT, "agent", "stream", NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO snapshot_uploads(
                   creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                   starting_checkpoint,through_seq,chunk_count,expected_chats,
                   expected_messages,expected_coverage_evidence,next_chunk_index,
                   received_chats,received_messages,received_coverage_evidence,
                   last_entity_kind,begin_fingerprint,state,created_at,committed_at)
               VALUES (?,?,?,?,NULL,1,1,1,0,0,0,0,0,0,NULL,'begin','staging',?,NULL)""",
            (ACCOUNT, "agent", "stream", "snapshot", NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO snapshot_chunks(
                   creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                   chunk_index,entity_kind,record_count,fingerprint,committed_at)
               VALUES (?,?,?,?,0,'chat',1,'chunk',?)""",
            (ACCOUNT, "agent", "stream", "snapshot", NOW.isoformat()),
        )
        inserted = connection.execute(
            """INSERT OR IGNORE INTO snapshot_chat_records(
                   creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                   chat_id,chunk_index,record_json,is_tombstone,record_kind,
                   platform_user_id,display_name,upstream_updated_at,content_hash)
               VALUES (?,?,?,?,?,0,'{}',0,'full',?,?,?,?)""",
            (
                ACCOUNT,
                "agent",
                "stream",
                "snapshot",
                "participant-chat",
                "participant-1",
                "Participant",
                NOW.isoformat(),
                "chat-hash",
            ),
        ).rowcount

    assert inserted == 0


def test_participant_deletion_scope_account_chat_lookup_is_indexed(tmp_path: Path) -> None:
    db = database(tmp_path / "index.db")
    with db.connect() as connection:
        rows = connection.execute(
            "PRAGMA index_info(participant_deletion_chat_scopes_account_chat)"
        ).fetchall()
        assert [row["name"] for row in rows] == [
            "creator_account_id",
            "chat_id",
        ]

        query_plan = connection.execute(
            """EXPLAIN QUERY PLAN
               SELECT 1
               FROM participant_deletion_chat_scopes AS s
               WHERE s.creator_account_id = ?
                 AND s.chat_id = ?""",
            ("creator-1", "chat-1"),
        ).fetchall()
        detail = " ".join(str(row["detail"]) for row in query_plan)
        assert "USING INDEX participant_deletion_chat_scopes_account_chat" in detail or "USING COVERING INDEX participant_deletion_chat_scopes_account_chat" in detail

