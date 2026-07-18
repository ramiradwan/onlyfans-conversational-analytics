from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.persistence.database import CanonicalSQLite
from app.persistence.factory import create_canonical_repositories
from app.persistence.migrations import (
    InstallationMigrationLock,
    MigrationChecksumError,
    MigrationError,
    MigrationLockError,
    MigrationRunner,
    SchemaCompatibilityError,
)
from app.protocol import AGENT_TO_BRAIN_ADAPTER
from app.services.agent_configuration import (
    AgentConfigurationAuthority,
    build_config_document,
)
from app.services.command_execution import CommandDeliveryTarget, CommandService
from app.transport.ingestion import IngestionService, StreamKey


FIXTURES = Path(__file__).parents[1] / "shared" / "fixtures" / "protocol" / "v1"
ACCOUNT_ID = "dev-creator-account"
NOW = datetime(2026, 7, 18, 10, 5, tzinfo=timezone.utc)
ACTION = {
    "type": "message.send",
    "conversation_id": "chat-1",
    "text": "Durable hello",
    "media_url": None,
}
CAPTURE_POLICY = {
    "observation_interval_seconds": 60,
    "rules": [
        {
            "resource": "messages",
            "url_pattern": "/api2/v2/chats/*/messages",
            "enabled": True,
        }
    ],
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def payload(name: str):
    return AGENT_TO_BRAIN_ADAPTER.validate_json(
        json.dumps(fixture(name))
    ).payload


def key() -> StreamKey:
    hello = payload("agent.hello")
    return StreamKey(ACCOUNT_ID, hello.agent_installation_id, hello.agent_stream_id)


def write_migration(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


def test_authoritative_connection_uses_wal_full_sync_foreign_keys_and_timeout(
    tmp_path: Path,
) -> None:
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3", busy_timeout_ms=7_500)
    MigrationRunner(database).run()
    with database.read() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 7_500


@pytest.mark.asyncio
async def test_committed_canonical_state_survives_fresh_repository_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical.sqlite3"
    first = create_canonical_repositories("sqlite", canonical_path=path)
    ingestion = IngestionService(first.ingestion)
    stream = key()
    assert (await ingestion.ingest_snapshot(stream, payload("ingest.snapshot"))).status == "accepted"
    assert (await ingestion.ingest_delta(stream, payload("ingest.delta"))).status == "accepted"

    configuration = AgentConfigurationAuthority(first.configuration)
    installation_id = uuid4()
    configuration.bind_installation(ACCOUNT_ID, installation_id, "config-7")
    published = await configuration.publish(
        ACCOUNT_ID,
        capture_policy=CAPTURE_POLICY,
        command_policy={
            "allowed_actions": ["message.send"],
            "max_text_length": 500,
            "require_idempotency": True,
        },
        issued_at=NOW,
    )

    commands = CommandService(first.commands)

    async def sender(_: dict) -> None:
        return None

    command = await commands.issue(
        creator_account_id=ACCOUNT_ID,
        action=ACTION,
        deadline=NOW + timedelta(minutes=5),
        target=CommandDeliveryTarget(uuid4(), "fence-1", ACCOUNT_ID),
        sender=sender,
        now=NOW,
    )
    commands.record_result(
        {
            "creator_account_id": ACCOUNT_ID,
            "command_id": command.command_id,
            "result_id": uuid4(),
            "status": "succeeded",
            "completed_at": NOW + timedelta(seconds=1),
            "output": {"external_message_id": "message-9"},
            "error": None,
        },
        received_at=NOW + timedelta(seconds=2),
    )

    second = create_canonical_repositories("sqlite", canonical_path=path)
    restarted_configuration = AgentConfigurationAuthority(second.configuration)
    restarted_command = second.commands.get(command.command_id)
    assert second.ingestion.checkpoint(stream) == 11
    assert second.ingestion.account_read_model(ACCOUNT_ID).view_revision == 2
    assert restarted_configuration.required_document(ACCOUNT_ID).config_revision == "config-8"
    assert restarted_configuration.installation(
        ACCOUNT_ID, installation_id
    ).required_config_revision == published.config_revision
    assert restarted_command is not None
    assert restarted_command.state == "succeeded"
    assert restarted_command.result_apply_count == 1


@pytest.mark.asyncio
async def test_snapshot_replacement_is_atomic_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "canonical.sqlite3"
    repositories = create_canonical_repositories("sqlite", canonical_path=path)
    service = IngestionService(repositories.ingestion)
    stream = key()
    await service.ingest_snapshot(stream, payload("ingest.snapshot"))

    assert repositories.database is not None
    reader = repositories.database.connect()
    try:
        reader.execute("BEGIN")
        old_checkpoint = reader.execute(
            "SELECT committed_source_seq FROM ingest_checkpoints"
        ).fetchone()[0]
        old_chat = reader.execute(
            "SELECT document_json FROM canonical_chats"
        ).fetchone()[0]

        replacement_document = fixture("ingest.snapshot")
        replacement_document["payload"].update(
            snapshot_id=str(uuid4()), through_seq=12
        )
        replacement_document["payload"]["chats"][0]["display_name"] = "Jordan"
        replacement_document["payload"]["messages"][0]["text"] = "Replaced"
        replacement = await service.ingest_snapshot(
            stream,
            AGENT_TO_BRAIN_ADAPTER.validate_json(
                json.dumps(replacement_document)
            ).payload,
        )
        assert replacement.status == "accepted"

        assert reader.execute(
            "SELECT committed_source_seq FROM ingest_checkpoints"
        ).fetchone()[0] == old_checkpoint
        assert reader.execute(
            "SELECT document_json FROM canonical_chats"
        ).fetchone()[0] == old_chat
        reader.commit()

        assert reader.execute(
            "SELECT committed_source_seq FROM ingest_checkpoints"
        ).fetchone()[0] == 12
        new_chat = json.loads(
            reader.execute("SELECT document_json FROM canonical_chats").fetchone()[0]
        )
        new_message = json.loads(
            reader.execute("SELECT document_json FROM canonical_messages").fetchone()[0]
        )
        assert (new_chat["display_name"], new_message["text"]) == (
            "Jordan",
            "Replaced",
        )
    finally:
        reader.close()


def test_migrations_are_idempotent_checksummed_and_backed_up(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    write_migration(
        migration_dir,
        "0001_initial.sql",
        "CREATE TABLE durable_value (id INTEGER PRIMARY KEY, value TEXT NOT NULL);",
    )
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    runner = MigrationRunner(database, migrations_dir=migration_dir)
    assert runner.run() == [1]
    assert runner.last_backup_path is not None and runner.last_backup_path.exists()
    assert runner.run() == []
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1

    write_migration(
        migration_dir,
        "0001_initial.sql",
        "CREATE TABLE durable_value (id INTEGER PRIMARY KEY, value TEXT NOT NULL);\n-- edited",
    )
    with pytest.raises(MigrationChecksumError):
        runner.run()


def test_failed_migration_rolls_back_and_can_be_recovered_on_restart(
    tmp_path: Path,
) -> None:
    migration_dir = tmp_path / "migrations"
    write_migration(
        migration_dir,
        "0001_initial.sql",
        "CREATE TABLE stable (id INTEGER PRIMARY KEY);",
    )
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    runner = MigrationRunner(database, migrations_dir=migration_dir)
    runner.run()
    write_migration(
        migration_dir,
        "0002_partial.sql",
        "CREATE TABLE must_rollback (id INTEGER); INSERT INTO missing_table VALUES (1);",
    )
    with pytest.raises(sqlite3.OperationalError):
        runner.run()
    with database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='must_rollback'"
        ).fetchone() is None
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1

    write_migration(
        migration_dir,
        "0002_partial.sql",
        "CREATE TABLE recovered (id INTEGER PRIMARY KEY);",
    )
    restarted = MigrationRunner(database, migrations_dir=migration_dir)
    assert restarted.run() == [2]
    with database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovered'"
        ).fetchone() is not None


def test_migration_runner_refuses_a_schema_newer_than_its_catalog(
    tmp_path: Path,
) -> None:
    migration_dir = tmp_path / "migrations"
    write_migration(
        migration_dir,
        "0001_initial.sql",
        "CREATE TABLE stable (id INTEGER PRIMARY KEY);",
    )
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    runner = MigrationRunner(database, migrations_dir=migration_dir)
    runner.run()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO schema_migrations (version, name, checksum, applied_at)
            VALUES (2, 'future', ?, ?)
            """,
            ("0" * 64, NOW.isoformat()),
        )
        connection.execute("PRAGMA user_version = 2")
    with pytest.raises(SchemaCompatibilityError, match="newer or missing"):
        runner.run()


def test_migration_runner_refuses_foreign_key_integrity_failure(tmp_path: Path) -> None:
    path = tmp_path / "canonical.sqlite3"
    database = CanonicalSQLite(path)
    MigrationRunner(database).run()
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            """
            INSERT INTO ingest_checkpoints (
                creator_account_id, agent_installation_id, agent_stream_id,
                committed_source_seq, committed_at
            ) VALUES ('missing', 'missing', 'missing', 1, ?)
            """,
            (NOW.isoformat(),),
        )
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(MigrationError, match="foreign-key check failed"):
        MigrationRunner(database).run()


def test_installation_migration_lock_excludes_a_second_runner(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    write_migration(
        migration_dir,
        "0001_initial.sql",
        "CREATE TABLE stable (id INTEGER PRIMARY KEY);",
    )
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    runner = MigrationRunner(database, migrations_dir=migration_dir)
    with InstallationMigrationLock(runner.lock_path):
        with pytest.raises(MigrationLockError):
            runner.run()


def test_configuration_publication_rolls_back_document_if_required_update_fails(
    tmp_path: Path,
) -> None:
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.sqlite3"
    )
    authority = AgentConfigurationAuthority(repositories.configuration)
    document = build_config_document(
        creator_account_id=ACCOUNT_ID,
        config_revision="config-8",
        issued_at=NOW,
        capture_policy=CAPTURE_POLICY,
        command_policy={
            "allowed_actions": [],
            "max_text_length": 500,
            "require_idempotency": True,
        },
    )
    assert repositories.database is not None
    with repositories.database.transaction() as connection:
        connection.executescript(
            """
            CREATE TRIGGER reject_required_insert BEFORE INSERT ON config_required
            BEGIN SELECT RAISE(ABORT, 'required update rejected'); END;
            CREATE TRIGGER reject_required_update BEFORE UPDATE ON config_required
            BEGIN SELECT RAISE(ABORT, 'required update rejected'); END;
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="required update rejected"):
        repositories.configuration.publish_document(document)  # type: ignore[attr-defined]
    assert repositories.configuration.document(ACCOUNT_ID, "config-8") is None
    assert authority.required_document(ACCOUNT_ID).config_revision == "config-7"
