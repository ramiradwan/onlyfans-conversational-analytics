from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.persistence import sqlite_api as sqlite3
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
from app.services import agent_configuration
from app.services.agent_configuration import (
    BOOTSTRAP_CONFIG_REVISION,
    AgentConfigurationAuthority,
    ConfigInstallationRecord,
    build_config_document,
)
from app.services.command_execution import CommandDeliveryTarget, CommandService
from app.transport.ingestion import IngestionService, StreamKey

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


FIXTURES = Path(__file__).parents[1] / "shared" / "fixtures" / "protocol" / "v2"
_BOOTSTRAP_SEQUENCE = int(BOOTSTRAP_CONFIG_REVISION.removeprefix("config-"))
# The revision immediately before the bootstrap document, which a fresh
# database never holds, and the one a first publication on top of it issues.
PRE_BOOTSTRAP_CONFIG_REVISION = f"config-{_BOOTSTRAP_SEQUENCE - 1}"
PUBLISHED_CONFIG_REVISION = f"config-{_BOOTSTRAP_SEQUENCE + 1}"
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
            "resource": "chats",
            "url_pattern": "/api2/v2/chats",
            "enabled": True,
        },
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


def test_fresh_sqlite_bootstrap_is_hard_cut_to_the_bootstrap_revision(
    tmp_path: Path,
) -> None:
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "fresh.sqlite3"
    )
    authority = AgentConfigurationAuthority(repositories.configuration)

    assert repositories.configuration.document(
        ACCOUNT_ID, PRE_BOOTSTRAP_CONFIG_REVISION
    ) is None
    assert repositories.configuration.document(ACCOUNT_ID, BOOTSTRAP_CONFIG_REVISION) is not None
    assert authority.required_document(ACCOUNT_ID).config_revision == BOOTSTRAP_CONFIG_REVISION


def _open_configuration(path: Path) -> AgentConfigurationAuthority:
    return AgentConfigurationAuthority(
        create_canonical_repositories("sqlite", canonical_path=path).configuration
    )


def test_persisted_bootstrap_content_change_demands_a_new_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Revisions are immutable, so bootstrap content cannot change in place.

    An installation that already persisted the current revision refuses to
    start on altered content; publishing it under the next revision succeeds.
    """

    path = tmp_path / "installed.sqlite3"
    _open_configuration(path)

    monkeypatch.setattr(
        agent_configuration,
        "BOOTSTRAP_COMMAND_POLICY",
        {**agent_configuration.BOOTSTRAP_COMMAND_POLICY, "max_text_length": 1},
    )
    with pytest.raises(RuntimeError, match="content has changed"):
        _open_configuration(path)

    monkeypatch.setattr(
        agent_configuration, "BOOTSTRAP_CONFIG_REVISION", PUBLISHED_CONFIG_REVISION
    )
    reopened = _open_configuration(path)
    assert (
        reopened.required_document(ACCOUNT_ID).config_revision
        == PUBLISHED_CONFIG_REVISION
    )


@pytest.mark.asyncio
async def test_configuration_and_commands_survive_fresh_repository_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical.sqlite3"
    first = create_canonical_repositories("sqlite", canonical_path=path)
    configuration = AgentConfigurationAuthority(first.configuration)
    installation_id = uuid4()
    configuration.bind_installation(ACCOUNT_ID, installation_id, BOOTSTRAP_CONFIG_REVISION)
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
    assert (
        restarted_configuration.required_document(ACCOUNT_ID).config_revision
        == PUBLISHED_CONFIG_REVISION
    )
    assert restarted_configuration.installation(
        ACCOUNT_ID, installation_id
    ).required_config_revision == published.config_revision
    assert restarted_command is not None
    assert restarted_command.state == "succeeded"
    assert restarted_command.result_apply_count == 1


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
    raw = database.open_detached(path)
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


def test_a_second_runner_in_one_process_waits_for_the_first(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    write_migration(
        migration_dir,
        "0001_initial.sql",
        "CREATE TABLE stable (id INTEGER PRIMARY KEY);",
    )
    holding = MigrationRunner(
        CanonicalSQLite(tmp_path / "canonical.sqlite3"), migrations_dir=migration_dir
    )
    inside = threading.Event()
    validate = holding._validate_database

    def hold(connection) -> None:
        inside.set()
        time.sleep(0.5)
        validate(connection)

    holding._validate_database = hold
    escaped: list[BaseException] = []

    def run_holding() -> None:
        try:
            holding.run()
        except BaseException as error:
            escaped.append(error)

    thread = threading.Thread(target=run_holding, name="holding-migration-runner")
    thread.start()
    try:
        assert inside.wait(timeout=10.0)
        # An uncollapsed parent component names the lock file the holding
        # thread owns under a second spelling, which must select one mutex.
        waiting = MigrationRunner(
            CanonicalSQLite(tmp_path / "canonical.sqlite3"),
            migrations_dir=migration_dir,
            lock_path=migration_dir / ".." / ".bridge-installation-migration.lock",
        )
        assert waiting.lock_path != holding.lock_path
        assert waiting.run() == []
    finally:
        thread.join(timeout=30.0)
    assert escaped == []
    assert not thread.is_alive()


def test_configuration_publication_rolls_back_document_if_required_update_fails(
    tmp_path: Path,
) -> None:
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.sqlite3"
    )
    authority = AgentConfigurationAuthority(repositories.configuration)
    document = build_config_document(
        creator_account_id=ACCOUNT_ID,
        config_revision=PUBLISHED_CONFIG_REVISION,
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
    assert repositories.configuration.document(
        ACCOUNT_ID, PUBLISHED_CONFIG_REVISION
    ) is None
    assert authority.required_document(ACCOUNT_ID).config_revision == BOOTSTRAP_CONFIG_REVISION


def _startup_runner(tmp_path: Path):
    directory = tmp_path / "startup-migrations"
    write_migration(
        directory,
        "0001_initial.sql",
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));",
    )
    database = CanonicalSQLite(tmp_path / "startup.sqlite3", encryption_key=b"r" * 32)
    return database, MigrationRunner(database, migrations_dir=directory)


def _trace_startup_statements(database, monkeypatch):
    statements = []
    original_connect = database.connect

    def connect():
        connection = original_connect()
        connection.set_trace_callback(lambda sql: statements.append((connection, sql)))
        return connection

    monkeypatch.setattr(database, "connect", connect)
    return statements


def _startup_full_checks(statements):
    return [
        (connection, sql.strip().lower().rstrip(";"))
        for connection, sql in statements
        if sql.strip().lower().rstrip(";")
        in {"pragma integrity_check", "pragma foreign_key_check"}
    ]


def test_validated_startup_read_uses_one_connection_snapshot_and_lock_scope(
    tmp_path: Path, monkeypatch,
) -> None:
    from app.persistence.database import LocalSQLite, SQLiteConfigurationError

    database, runner = _startup_runner(tmp_path)
    runner.run()
    statements = _trace_startup_statements(database, monkeypatch)
    readers = []

    def read(connection):
        readers.append(connection)
        assert connection.in_transaction
        assert LocalSQLite.open_connection_count(database.path) == 1
        with pytest.raises(MigrationLockError):
            with InstallationMigrationLock(runner.lock_path):
                pass
        with pytest.raises(SQLiteConfigurationError, match="requires closed connections"):
            with LocalSQLite.exclusive_lifecycle(database.path):
                pass
        return connection.execute("SELECT COUNT(*) FROM child").fetchone()[0]

    assert runner.run_with_validated_read(read) == ([], 0)
    assert len(readers) == 1
    assert _startup_full_checks(statements) == [
        (readers[0], "pragma integrity_check"),
        (readers[0], "pragma foreign_key_check"),
    ]
    assert LocalSQLite.open_connection_count(database.path) == 0
    with pytest.raises(sqlite3.ProgrammingError):
        readers[0].execute("SELECT 1")


def test_validated_startup_read_finishes_pending_migrations_before_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()
    write_migration(
        runner.migrations_dir, "0002_new_table.sql",
        "CREATE TABLE migrated_value (value INTEGER); INSERT INTO migrated_value VALUES (7);",
    )
    statements = _trace_startup_statements(database, monkeypatch)
    readers = []

    def read(connection):
        readers.append(connection)
        assert connection.in_transaction
        return connection.execute("SELECT value FROM migrated_value").fetchone()[0]

    assert runner.run_with_validated_read(read) == ([2], 7)
    checks = _startup_full_checks(statements)
    assert checks == [(readers[0], "pragma integrity_check"),
                      (readers[0], "pragma foreign_key_check")]
    assert runner.last_backup_path is not None and runner.last_backup_path.exists()
    database.validate_integrity()


def test_standalone_migration_runs_each_keep_full_database_validation(
    tmp_path: Path, monkeypatch,
) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()
    statements = _trace_startup_statements(database, monkeypatch)
    assert runner.run() == []
    assert runner.run() == []
    assert [sql for _, sql in _startup_full_checks(statements)] == [
        "pragma integrity_check", "pragma foreign_key_check",
        "pragma integrity_check", "pragma foreign_key_check",
    ]


@pytest.mark.parametrize("statement", [
    "COMMIT", "ROLLBACK", "SAVEPOINT scope_escape", "BEGIN",
    "PRAGMA query_only=OFF", "PRAGMA user_version=123",
    "INSERT INTO parent VALUES (1)", "DROP TABLE child",
])
def test_validated_startup_reader_cannot_mutate_or_end_its_snapshot(
    tmp_path: Path, statement: str,
) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()

    def read(connection):
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(statement)
        assert connection.in_transaction
        return connection.execute("SELECT COUNT(*) FROM parent").fetchone()[0]

    with pytest.raises(MigrationError):
        runner.run_with_validated_read(read)
    assert database.open_connection_count(database.path) == 0
    with database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 0


def test_validated_startup_read_refuses_callback_that_escapes_snapshot(tmp_path: Path) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()

    def read(connection):
        connection.set_authorizer(None)
        connection.rollback()
        return 1

    with pytest.raises(MigrationError):
        runner.run_with_validated_read(read)
    assert database.open_connection_count(database.path) == 0


def test_validated_startup_read_rejects_changed_physical_file_observation(
    tmp_path: Path, monkeypatch,
) -> None:
    from app.persistence.database import SQLiteConfigurationError

    database, runner = _startup_runner(tmp_path)
    runner.run()
    reader_finished = False
    original_security_check = database._restrict_permissions

    def observe_identity():
        identity = original_security_check()
        # Windows prevents os.replace while this native handle is open. Model
        # the same changed physical identity returned by the security observer.
        return (identity[0], identity[1] + 1) if reader_finished else identity

    monkeypatch.setattr(database, "_restrict_permissions", observe_identity)

    def read(connection):
        nonlocal reader_finished
        connection.execute("SELECT COUNT(*) FROM child").fetchone()
        reader_finished = True
        return 1

    with pytest.raises((MigrationError, SQLiteConfigurationError)):
        runner.run_with_validated_read(read)
    assert reader_finished
    assert database.open_connection_count(database.path) == 0


def test_validated_startup_read_excludes_untracked_concurrent_writer(tmp_path: Path) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()
    errors = []
    attempted = threading.Event()

    def write():
        connection = database.open_detached(database.path)
        try:
            connection.execute("PRAGMA busy_timeout=100")
            attempted.set()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO parent VALUES (1)")
            connection.commit()
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    def read(connection):
        writer = threading.Thread(target=write)
        writer.start()
        try:
            assert attempted.wait(10)
            writer.join(10)
            assert not writer.is_alive()
            assert len(errors) == 1
            assert isinstance(errors[0], sqlite3.OperationalError)
            return connection.execute("SELECT COUNT(*) FROM parent").fetchone()[0]
        finally:
            writer.join(10)

    assert runner.run_with_validated_read(read) == ([], 0)


def test_validated_startup_read_unavailable_with_existing_owned_connection(tmp_path: Path) -> None:
    from app.persistence.database import StartupValidationUnavailable

    database, runner = _startup_runner(tmp_path)
    runner.run()
    callbacks = []
    with database.read():
        with pytest.raises(StartupValidationUnavailable):
            runner.run_with_validated_read(lambda connection: callbacks.append(connection))
        assert callbacks == []
        # Standalone migrations remain supported while other handles exist.
        assert runner.run() == []
    assert database.open_connection_count(database.path) == 0


def test_validated_startup_read_does_not_invoke_reader_after_foreign_key_failure(
    tmp_path: Path,
) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()
    connection = database.open_detached(database.path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO child VALUES (1, 999)")
        connection.commit()
    finally:
        connection.close()
    callbacks = []
    with pytest.raises(MigrationError, match="foreign-key check failed"):
        runner.run_with_validated_read(lambda connection: callbacks.append(connection))
    assert callbacks == []
    assert database.open_connection_count(database.path) == 0


def test_validated_startup_read_rejects_corrupt_database_before_reader(tmp_path: Path) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()
    database.path.write_bytes(b"synthetic-not-a-sqlite-database")
    callbacks = []
    with pytest.raises((MigrationError, sqlite3.DatabaseError)):
        runner.run_with_validated_read(lambda connection: callbacks.append(connection))
    assert callbacks == []
    assert database.open_connection_count(database.path) == 0


@pytest.mark.parametrize("failure", [RuntimeError("reader failed"), asyncio.CancelledError()])
def test_validated_startup_read_closes_connection_on_failure_or_cancellation(
    tmp_path: Path, failure,
) -> None:
    database, runner = _startup_runner(tmp_path)
    runner.run()
    readers = []

    def read(connection):
        readers.append(connection)
        raise failure

    with pytest.raises(type(failure)):
        runner.run_with_validated_read(read)
    assert database.open_connection_count(database.path) == 0
    with pytest.raises(sqlite3.ProgrammingError):
        readers[0].execute("SELECT 1")
    assert runner.run() == []
