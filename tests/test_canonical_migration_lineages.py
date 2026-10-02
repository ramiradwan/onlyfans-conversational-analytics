"""Preserve the two exact historical canonical v9 ledgers without normalization."""
from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.rebuild import ReadOnlyCanonicalDatabase, RebuildFailure
from app.persistence import migrations, sqlite_api as sqlite3
from app.persistence.backup import SQLiteBackupError, backup_canonical_database, restore_backup, verify_backup
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationError, MigrationRunner
from app.persistence.retention import CreatorVaultRetention
from app.persistence.retention_restore import restore_migration_backup_with_deletion_barriers


pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]

ACCOUNT = "migration-lineage-account"
NOW = datetime.now(timezone.utc)


def _ledger(connection):
    return [tuple(row) for row in connection.execute(
        "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
    )]


def _content(connection):
    return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
            for table in ("account_heads", "account_chats", "account_messages")}


def _historical_database(tmp_path, lineage):
    catalog = tmp_path / "historical"
    catalog.mkdir(parents=True)
    for source in migrations.CANONICAL_MIGRATIONS_DIR.glob("*.sql"):
        if int(source.name[:4]) <= 8:
            shutil.copy2(source, catalog / source.name)
    database = CanonicalSQLite(tmp_path / "canonical.sqlite3")
    MigrationRunner(database, migrations_dir=catalog).run()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,1,?)",
            (ACCOUNT, NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO account_chats(creator_account_id,chat_id,record_kind,platform_user_id,
               content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
               VALUES (?,'chat','full','fan','chat-hash',1,1,0,?)""", (ACCOUNT, NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO account_messages(creator_account_id,message_id,chat_id,sender_platform_user_id,
               text,sent_at,direction,content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
               VALUES (?,'message','chat','fan','preserved message',?,'inbound','message-hash',1,1,0,?)""",
            (ACCOUNT, NOW.isoformat(), NOW.isoformat()),
        )
    relative = ("0009_message_catchup.sql" if lineage == "main" else
                "legacy_analytics_v9/0009_analytics_source_tokens.sql")
    source = migrations.CANONICAL_MIGRATIONS_DIR / relative
    shutil.copy2(source, catalog / source.name)
    assert MigrationRunner(database, migrations_dir=catalog).run() == [9]
    return database, catalog


@pytest.mark.parametrize("lineage", ["main", "analytics"])
def test_upgrade_preserves_historical_ledger_content_identity_and_backup(tmp_path, lineage):
    database, _ = _historical_database(tmp_path, lineage)
    source = HistoryAnalyticsSource(SimpleNamespace(database=database))
    identity = source.read_identity(ACCOUNT)
    with database.read() as connection:
        old_ledger, old_content = _ledger(connection), _content(connection)
        token = (connection.execute("SELECT token FROM analytics_source_tokens").fetchone()[0]
                 if lineage == "analytics" else None)
        catchup = ([tuple(row) for row in connection.execute("SELECT * FROM message_catchup")]
                   if lineage == "main" else None)
    # Exact historical prefixes remain usable by the read-only rebuild boundary.
    with ReadOnlyCanonicalDatabase(database.path) as readonly:
        readonly.validate_schema()
    runner = MigrationRunner(database, migrations_dir=migrations.CANONICAL_MIGRATIONS_DIR)
    assert runner.run() == [10]
    assert runner.last_backup_path is not None
    with closing(database.open_detached(runner.last_backup_path, read_only=True)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert _ledger(connection) == old_ledger
        assert _content(connection) == old_content
    with database.read() as connection:
        assert _ledger(connection)[:9] == old_ledger
        assert _ledger(connection)[9][1] == (
            "analytics_source_tokens" if lineage == "main" else "message_catchup"
        )
        assert _content(connection) == old_content
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        current_token = connection.execute("SELECT token FROM analytics_source_tokens").fetchone()[0]
        assert len(current_token) == 32 and all(c in "0123456789abcdef" for c in current_token)
        if token is not None:
            assert current_token == token
        current_catchup = [tuple(row) for row in connection.execute("SELECT * FROM message_catchup")]
        if catchup is not None:
            assert current_catchup == catchup
        else:
            assert len(current_catchup) == 1 and current_catchup[0][0] == ACCOUNT
    assert source.read_identity(ACCOUNT) == identity
    reopened = MigrationRunner(CanonicalSQLite(database.path))
    assert reopened.run() == [] and reopened.last_backup_path is None
    with ReadOnlyCanonicalDatabase(database.path) as readonly:
        readonly.validate_schema()


@pytest.mark.parametrize("lineage", ["main", "analytics"])
@pytest.mark.parametrize("tamper", ["prefix", "name", "checksum", "gap", "duplicate", "user_version", "future"])
def test_recognized_lineage_never_excuses_invalid_history(tmp_path, lineage, tamper):
    database, _ = _historical_database(tmp_path, lineage)
    with database.transaction() as connection:
        if tamper == "prefix":
            connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=1", ("0" * 64,))
        elif tamper == "name":
            connection.execute("UPDATE schema_migrations SET name='unrecognized' WHERE version=9")
        elif tamper == "checksum":
            connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=9", ("0" * 64,))
        elif tamper == "gap":
            connection.execute("DELETE FROM schema_migrations WHERE version=4")
        elif tamper == "duplicate":
            # A malformed ledger must not hide duplicate rows behind a dict.
            rows = _ledger(connection)
            connection.execute("DROP TABLE schema_migrations")
            connection.execute("CREATE TABLE schema_migrations(version INTEGER,name TEXT,checksum TEXT,applied_at TEXT)")
            connection.executemany("INSERT INTO schema_migrations VALUES (?,?,?,?)", rows + [rows[4]])
        elif tamper == "user_version":
            connection.execute("PRAGMA user_version=8")
        else:
            connection.execute("INSERT INTO schema_migrations VALUES (11,'future',?,'synthetic')", ("0" * 64,))
            connection.execute("PRAGMA user_version=11")
    with database.read() as connection:
        before = (_ledger(connection), _content(connection), connection.execute("PRAGMA user_version").fetchone()[0])
    runner = MigrationRunner(database)
    with pytest.raises(MigrationError):
        runner.run()
    assert runner.last_backup_path is None
    with database.read() as connection:
        assert (_ledger(connection), _content(connection), connection.execute("PRAGMA user_version").fetchone()[0]) == before


@pytest.mark.parametrize("lineage", ["main", "analytics"])
@pytest.mark.parametrize("malformed_version", ["10", 10.1], ids=["text", "fractional"])
def test_malformed_version_types_are_refused_by_every_validation_boundary(tmp_path, lineage, malformed_version):
    database, _ = _historical_database(tmp_path, lineage)
    assert MigrationRunner(database).run() == [10]
    with database.transaction() as connection:
        rows = _ledger(connection)
        connection.execute("DROP TABLE schema_migrations")
        # No INTEGER affinity: preserve the malformed value instead of having
        # SQLite repair it before the validation under test can observe it.
        connection.execute("CREATE TABLE schema_migrations(version,name TEXT,checksum TEXT,applied_at TEXT)")
        # Corrupt the final version: text sorts after integers, so the old
        # int() normalization would otherwise accept the complete 1..10 list.
        rows[9] = (malformed_version, *rows[9][1:])
        connection.executemany("INSERT INTO schema_migrations VALUES (?,?,?,?)", rows)
        stored_type = connection.execute(
            "SELECT typeof(version) FROM schema_migrations WHERE name=?", (rows[9][1],)
        ).fetchone()[0]
        assert stored_type == ("text" if isinstance(malformed_version, str) else "real")
    with database.read() as connection:
        before = (_ledger(connection), _content(connection), connection.execute("PRAGMA user_version").fetchone()[0])
    runner = MigrationRunner(database)
    with pytest.raises(MigrationError):
        runner.run()
    assert runner.last_backup_path is None
    with pytest.raises(SQLiteBackupError):
        backup_canonical_database(database, tmp_path / "invalid.canonical.backup")
    with ReadOnlyCanonicalDatabase(database.path) as readonly:
        with pytest.raises(RebuildFailure, match="incompatible"):
            readonly.validate_schema()
    with database.read() as connection:
        assert (_ledger(connection), _content(connection), connection.execute("PRAGMA user_version").fetchone()[0]) == before


@pytest.mark.parametrize("lineage", ["main", "analytics"])
def test_pending_migration_failure_rolls_back_and_restart_preserves_history(tmp_path, monkeypatch, lineage):
    database, _ = _historical_database(tmp_path, lineage)
    with database.read() as connection:
        before = (_ledger(connection), _content(connection))
    apply = MigrationRunner._apply

    def broken(connection, migration):
        apply(connection, replace(migration, sql=migration.sql + "\nINSERT INTO absent_lineage_test_table VALUES (1);"))

    runner = MigrationRunner(database)
    with monkeypatch.context() as patch:
        patch.setattr(MigrationRunner, "_apply", staticmethod(broken))
        with pytest.raises(sqlite3.OperationalError):
            runner.run()
    with database.read() as connection:
        assert (_ledger(connection), _content(connection)) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        new_table = "analytics_source_tokens" if lineage == "main" else "message_catchup"
        assert connection.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (new_table,)).fetchone() is None
    assert runner.last_backup_path is not None
    assert MigrationRunner(database).run() == [10]


@pytest.mark.parametrize("lineage", ["main", "analytics"])
def test_both_histories_use_the_same_future_catalog_tail(tmp_path, monkeypatch, lineage):
    database, _ = _historical_database(tmp_path, lineage)
    future = tmp_path / "future-catalog"
    shutil.copytree(migrations.CANONICAL_MIGRATIONS_DIR, future)
    (future / "0011_future_lineage_probe.sql").write_text("CREATE TABLE future_lineage_probe(value TEXT);", encoding="utf-8")
    monkeypatch.setattr(migrations, "CANONICAL_MIGRATIONS_DIR", future)
    runner = MigrationRunner(database, migrations_dir=future)
    assert runner.run() == [10, 11]
    with database.read() as connection:
        assert _ledger(connection)[8][1] == ("message_catchup" if lineage == "main" else "analytics_source_tokens")
        assert _ledger(connection)[10][1] == "future_lineage_probe"
        assert connection.execute("SELECT COUNT(*) FROM future_lineage_probe").fetchone()[0] == 0
    assert runner.run() == []


@pytest.mark.parametrize("lineage", ["main", "analytics"])
@pytest.mark.parametrize("destination_lineage", ["same", "other"])
def test_completed_backups_and_migration_restore_preserve_each_lineage(tmp_path, lineage, destination_lineage):
    database, _ = _historical_database(tmp_path, lineage)
    runner = MigrationRunner(database)
    assert runner.run() == [10]
    migration_backup = runner.last_backup_path
    assert migration_backup is not None
    backup = tmp_path / "completed.canonical.backup"
    manifest = backup_canonical_database(database, backup)
    assert verify_backup(backup, expected_store="canonical") == manifest
    restored = tmp_path / "restored.sqlite3"
    restore_backup(backup, restored, expected_store="canonical")
    with ReadOnlyCanonicalDatabase(restored) as readonly:
        readonly.validate_schema()
        assert _ledger(readonly.connection)[8][1] == ("message_catchup" if lineage == "main" else "analytics_source_tokens")

    if destination_lineage == "other":
        other = "analytics" if lineage == "main" else "main"
        database, _ = _historical_database(tmp_path / "other-destination", other)
        assert MigrationRunner(database).run() == [10]
        with database.read() as connection:
            assert _ledger(connection)[8][1] == ("message_catchup" if other == "main" else "analytics_source_tokens")

    # A pre-upgrade backup cannot resurrect a message deleted after the upgrade.
    CreatorVaultRetention(database, clock=lambda: NOW).delete_message(ACCOUNT, "message")
    with database.read() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert database.open_connection_count(database.path) == 0
    for suffix in ("-wal", "-shm"):
        Path(str(database.path) + suffix).unlink(missing_ok=True)
    restore_migration_backup_with_deletion_barriers(database, migration_backup)
    with database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert _ledger(connection)[8][1] == ("message_catchup" if lineage == "main" else "analytics_source_tokens")
        assert connection.execute("SELECT 1 FROM account_messages WHERE message_id='message'").fetchone() is None
        assert connection.execute("SELECT 1 FROM deletion_barriers WHERE creator_account_id=? AND scope_kind='message' AND scope_key='message'", (ACCOUNT,)).fetchone() is not None


def test_custom_catalog_is_not_treated_as_the_shipped_canonical_catalog(tmp_path):
    database, catalog = _historical_database(tmp_path, "analytics")
    (catalog / "0010_custom.sql").write_text("CREATE TABLE custom_probe(value TEXT);", encoding="utf-8")
    assert MigrationRunner(database, migrations_dir=catalog).run() == [10]
    with database.read() as connection:
        assert _ledger(connection)[9][1] == "custom"


@pytest.mark.parametrize("fault", ["changed", "missing", "unexpected"])
def test_compatibility_resources_are_pinned_and_closed(tmp_path, monkeypatch, fault):
    database, _ = _historical_database(tmp_path, "analytics")
    copied = tmp_path / "copied-catalog"
    shutil.copytree(migrations.CANONICAL_MIGRATIONS_DIR, copied)
    historical = copied / "legacy_analytics_v9" / "0009_analytics_source_tokens.sql"
    if fault == "changed":
        historical.write_bytes(historical.read_bytes() + b"\n-- changed historical bytes\n")
    elif fault == "missing":
        historical.unlink()
    else:
        (historical.parent / "0011_unexpected.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(migrations, "CANONICAL_MIGRATIONS_DIR", copied)
    runner = MigrationRunner(database, migrations_dir=copied)
    with pytest.raises(MigrationError):
        runner.run()
    assert runner.last_backup_path is None
    with database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
