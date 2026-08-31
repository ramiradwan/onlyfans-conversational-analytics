from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.backup import backup_canonical_database
from app.persistence.database import CanonicalSQLite
from app.persistence.factory import create_canonical_repositories
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention
from app.persistence.retention_restore import restore_migration_backup_with_deletion_barriers

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "managed-recovery-account"
CHAT = "managed-recovery-chat"
MESSAGE = "managed-recovery-message"
PARTICIPANT = "managed-recovery-participant"


def _migration_name(path: Path, created_at: datetime) -> Path:
    stamp = created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path / f"canonical.sqlite3.schema-v1-to-v2.{stamp}-deadbeef.bak"


def _seed_message(path: Path) -> tuple[CanonicalSQLite, object]:
    repositories = create_canonical_repositories("sqlite", canonical_path=path)
    assert repositories.database is not None
    now = NOW.isoformat()
    with repositories.database.transaction() as connection:
        connection.execute(
            "INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)",
            (ACCOUNT, 1, now),
        )
        connection.execute(
            """
            INSERT INTO account_chats(
                creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                winning_event_id,is_deleted,updated_at
            ) VALUES (?,?,'full',?,'Participant',?,'chat-hash',1,1,'chat-event',0,?)
            """,
            (ACCOUNT, CHAT, PARTICIPANT, now, now),
        )
        connection.execute(
            """
            INSERT INTO account_messages(
                creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                direction,upstream_updated_at,content_hash,winning_stream_epoch,
                winning_source_seq,winning_event_id,is_deleted,updated_at
            ) VALUES (?,?,?,?,?,?,'inbound',NULL,'message-hash',1,1,'message-event',0,?)
            """,
            (ACCOUNT, MESSAGE, CHAT, PARTICIPANT, "stale text", now, now),
        )
    return repositories.database, repositories


def _raw_migration_snapshot(database: CanonicalSQLite, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with database.read() as source:
        target = database.open_detached(destination)
        try:
            source.backup(target)
        finally:
            target.close()


def _clear_sqlite_sidecars(path: Path) -> None:
    with CanonicalSQLite(path).read() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def test_canonical_backup_publication_prunes_expired_complete_cohort(tmp_path: Path) -> None:
    database, _ = _seed_message(tmp_path / "canonical.sqlite3")
    recovery_dir = tmp_path / "managed"
    recovery_dir.mkdir()
    old = _migration_name(recovery_dir, NOW - timedelta(days=31))
    old.write_bytes(b"old")
    old.with_name(old.name + ".manifest.json").write_text(
        json.dumps({"created_at": (NOW - timedelta(days=31)).isoformat()}),
        encoding="utf-8",
    )
    old.with_name(old.name + ".key.dpapi").write_bytes(b"old-key")

    backup_canonical_database(database, recovery_dir / "current.canonical.backup")

    assert not old.exists()
    assert not old.with_name(old.name + ".manifest.json").exists()
    assert not old.with_name(old.name + ".key.dpapi").exists()


def test_migration_backup_publication_prunes_cohorts_older_than_thirty_days(
    tmp_path: Path,
) -> None:
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    runner = MigrationRunner(database)
    runner.run()
    recovery_dir = tmp_path / "backups"
    old = _migration_name(recovery_dir, NOW - timedelta(days=31))
    recovery_dir.mkdir(exist_ok=True)
    old.write_bytes(b"old")
    old.with_name(old.name + ".manifest.json").write_text(
        json.dumps({"created_at": (NOW - timedelta(days=31)).isoformat()}),
        encoding="utf-8",
    )
    old.with_name(old.name + ".key.dpapi").write_bytes(b"old-key")
    catalog = runner._load_catalog()
    with database.read() as connection:
        applied = runner._validate_applied(connection, catalog)
        published = runner._backup(connection, applied, catalog[-1].version)

    assert published.exists()
    assert not old.exists()
    assert not old.with_name(old.name + ".manifest.json").exists()
    assert not old.with_name(old.name + ".key.dpapi").exists()


def test_stale_migration_recovery_cannot_resurrect_deleted_content(tmp_path: Path) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    database, repositories = _seed_message(canonical_path)
    recovery_dir = tmp_path / "backups"
    backup_path = _migration_name(recovery_dir, NOW - timedelta(days=1))
    _raw_migration_snapshot(database, backup_path)

    retention = CreatorVaultRetention(database, clock=lambda: NOW)
    retention.delete_message(ACCOUNT, MESSAGE)
    with database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id=?",
            (ACCOUNT, MESSAGE),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM deletion_barriers WHERE creator_account_id=? AND scope_kind='message' AND scope_key=?",
            (ACCOUNT, MESSAGE),
        ).fetchone() is not None

    del repositories
    _clear_sqlite_sidecars(canonical_path)
    restore_migration_backup_with_deletion_barriers(database, backup_path, now=NOW)

    reopened = create_canonical_repositories("sqlite", canonical_path=canonical_path)
    assert reopened.database is not None
    with reopened.database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id=?",
            (ACCOUNT, MESSAGE),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM deletion_barriers WHERE creator_account_id=? AND scope_kind='message' AND scope_key=?",
            (ACCOUNT, MESSAGE),
        ).fetchone() is not None
