from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.services.retention_maintenance import RetentionMaintenance


ACCOUNT = "creator-account"
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def database(path: Path) -> CanonicalSQLite:
    value = CanonicalSQLite(path)
    MigrationRunner(value).run()
    return value


def seed_message(db: CanonicalSQLite, *, sent_at: datetime) -> None:
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
               VALUES (?,?,'full',?,'Participant',?,'chat-hash',1,1,'chat-event',0,?,'ordinary',?)""",
            (ACCOUNT, "chat-1", "participant-1", now, now, now),
        )
        connection.execute(
            """INSERT INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES (?,?,?,?,?,?,'inbound',NULL,'message-hash',1,1,'message-event',0,?,'ordinary',?)""",
            (
                ACCOUNT,
                "message-1",
                "chat-1",
                "participant-1",
                "private text",
                sent_at.isoformat(),
                now,
                now,
            ),
        )


def test_runtime_maintenance_expires_idle_working_content(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db, sent_at=NOW - timedelta(days=31))

    result = RetentionMaintenance(db, clock=lambda: NOW).run_once()

    assert result.accounts_checked == 1
    assert result.expired_message_count == 1
    with db.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id=?",
            (ACCOUNT, "message-1"),
        ).fetchone() is None
        barrier = connection.execute(
            """SELECT provenance FROM deletion_barriers
               WHERE creator_account_id=? AND scope_kind='message' AND scope_key=?""",
            (ACCOUNT, "message-1"),
        ).fetchone()
    assert barrier is not None
    assert barrier[0] == "retention_expiry"


def test_runtime_maintenance_prunes_idle_managed_recovery_cohort(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    recovery = tmp_path / "backups"
    recovery.mkdir(exist_ok=True)
    backup = recovery / "old.backup"
    backup.write_text("encrypted", encoding="utf-8")
    backup.with_name(backup.name + ".manifest.json").write_text(
        json.dumps({"created_at": (NOW - timedelta(days=31)).isoformat()}),
        encoding="utf-8",
    )
    backup.with_name(backup.name + ".key.dpapi").write_text(
        "wrapped",
        encoding="utf-8",
    )

    result = RetentionMaintenance(
        db,
        managed_recovery_roots=(recovery,),
        clock=lambda: NOW,
    ).run_once()

    assert result.removed_recovery_file_count == 3
    assert not backup.exists()
    assert not backup.with_name(backup.name + ".manifest.json").exists()
    assert not backup.with_name(backup.name + ".key.dpapi").exists()


def test_runtime_maintenance_preserves_current_managed_recovery_cohort(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    recovery = tmp_path / "backups"
    recovery.mkdir(exist_ok=True)
    backup = recovery / "recent.backup"
    backup.write_text("encrypted", encoding="utf-8")
    backup.with_name(backup.name + ".manifest.json").write_text(
        json.dumps({"created_at": (NOW - timedelta(days=1)).isoformat()}),
        encoding="utf-8",
    )

    result = RetentionMaintenance(
        db,
        managed_recovery_roots=(recovery,),
        clock=lambda: NOW,
    ).run_once()

    assert result.removed_recovery_file_count == 0
    assert backup.exists()
