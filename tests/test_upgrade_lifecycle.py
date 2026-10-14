"""Admit real encrypted ledgers before application constructors can migrate."""
from __future__ import annotations

from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import subprocess
import sys
import traceback

import pytest

from app.core.startup_maintenance import RetainedState, StartupRefused, admit_retained_schemas
from app.persistence.database import AuthSQLite, open_encrypted_sqlite
from app.persistence.migrations import MigrationRunner, SchemaCompatibilityError
from app.persistence.private_files import apply_private_file_security
from app.persistence.upgrade_lifecycle import UpgradeStore, inspect_existing_stores
from app.security.local_data_key import database_key

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]
APP = Path(__file__).resolve().parents[1] / "app"
AUTH_CATALOG = APP / "persistence" / "auth_sql"
AUTH = UpgradeStore("auth.sqlite3", "auth", AUTH_CATALOG)


def seed(root, *, catalog=AUTH_CATALOG):
    database = AuthSQLite(root / AUTH.filename)
    MigrationRunner(database, migrations_dir=catalog).run()
    return database


def authority_bytes(root):
    return {path.name: path.read_bytes() for path in root.iterdir()
            if path.is_file() and not path.name.endswith("-shm")}


def test_current_schema_admission_creates_no_files_or_authority(tmp_path):
    root = tmp_path / "retained"
    seed(root)
    before = authority_bytes(root)
    names = sorted(path.name for path in root.iterdir())
    inspected = inspect_existing_stores(root, (AUTH,))[AUTH.filename]
    assert inspected.current_version == inspected.target_version and not inspected.pending_versions
    assert authority_bytes(root) == before
    assert sorted(path.name for path in root.iterdir()) == names
    admit_retained_schemas(RetainedState(root, "provisioning", (AUTH.filename,)))


def test_pending_auth_migration_completes_before_application_construction(tmp_path):
    catalog = tmp_path / "old-catalog"
    catalog.mkdir()
    first = sorted(AUTH_CATALOG.glob("*.sql"))[0]
    shutil.copy2(first, catalog / first.name)
    root = tmp_path / "retained"
    seed(root, catalog=catalog)
    inspected = inspect_existing_stores(root, (AUTH,))[AUTH.filename]
    assert inspected.current_version == 1 and inspected.pending_versions[0] == 2
    admit_retained_schemas(RetainedState(root, "provisioning", (AUTH.filename,)))
    assert not inspect_existing_stores(root, (AUTH,))[AUTH.filename].pending_versions
    assert not (root / "upgrade-checkpoint").exists()


def test_packaged_boot_constructs_application_only_after_pending_auth_maintenance(tmp_path, monkeypatch):
    from app import packaged_entry
    from app.core import startup_maintenance, process_lifetime
    catalog = tmp_path / "old-catalog"
    catalog.mkdir()
    first = sorted(AUTH_CATALOG.glob("*.sql"))[0]
    shutil.copy2(first, catalog / first.name)
    root = tmp_path / "retained"
    seed(root, catalog=catalog)
    @contextmanager
    def parent(_callback):
        yield
    monkeypatch.setattr(process_lifetime, "parent_lifetime", parent)
    monkeypatch.setattr(startup_maintenance, "acquire_runtime_ownership",
                        lambda: RetainedState(root, "provisioning", (AUTH.filename,)))
    class Selected(RuntimeError):
        pass
    def select(**kwargs):
        assert not inspect_existing_stores(root, (AUTH,))[AUTH.filename].pending_versions
        assert not (root / "upgrade-checkpoint").exists()
        raise Selected()
    monkeypatch.setattr(packaged_entry, "select_brain_application", select)
    with pytest.raises(Selected):
        packaged_entry.run_brain()


@pytest.mark.parametrize("mutation", ["future", "checksum", "missing-ledger", "version"])
def test_incompatible_ledger_is_never_fresh_or_rewritten(tmp_path, mutation):
    database = seed(tmp_path)
    with database.transaction() as connection:
        if mutation == "future":
            version = connection.execute("PRAGMA user_version").fetchone()[0] + 1
            connection.execute("INSERT INTO schema_migrations VALUES (?,?,?,?)", (version, "future", "0" * 64, "synthetic"))
            connection.execute(f"PRAGMA user_version={version}")
        elif mutation == "checksum":
            connection.execute("UPDATE schema_migrations SET checksum=? WHERE version=1", ("0" * 64,))
        elif mutation == "missing-ledger":
            connection.execute("DROP TABLE schema_migrations")
        else:
            connection.execute("PRAGMA user_version=0")
    before = authority_bytes(tmp_path)
    with pytest.raises(StartupRefused, match="retained_schema_incompatible"):
        admit_retained_schemas(RetainedState(tmp_path, "provisioning", (AUTH.filename,)))
    assert authority_bytes(tmp_path) == before


def test_read_only_opening_cannot_create_a_missing_parent_or_database(tmp_path):
    missing = tmp_path / "absent" / "auth.sqlite3"
    with pytest.raises(RuntimeError, match="source is missing"):
        open_encrypted_sqlite(missing, bytes(32), read_only=True)
    assert not missing.parent.exists()


def test_crashed_committed_wal_is_visible_to_admission_without_authority_writes(tmp_path):
    root = tmp_path / "retained"
    seed(root)
    script = """
import os,sys
from pathlib import Path
from app.persistence.database import AuthSQLite
database=AuthSQLite(Path(sys.argv[1])/'auth.sqlite3')
connection=database.connect()
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE synthetic_wal_marker(value TEXT)')
connection.execute("INSERT INTO synthetic_wal_marker VALUES ('synthetic committed WAL')")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(root)], cwd=APP.parent,
                   env=os.environ.copy(), check=True, timeout=20)
    assert (root / "auth.sqlite3-wal").stat().st_size > 0
    before = authority_bytes(root)
    inspected = inspect_existing_stores(root, (AUTH,))[AUTH.filename]
    assert not inspected.pending_versions
    # Use the same ordinary read-only mode: immutable would miss this table.
    with closing(open_encrypted_sqlite(root / AUTH.filename,
            database_key(root / AUTH.filename, "auth", create_if_missing=False), read_only=True)) as connection:
        assert connection.execute("SELECT value FROM synthetic_wal_marker").fetchone()[0] == "synthetic committed WAL"
    assert authority_bytes(root) == before


def test_missing_wal_index_requires_explicit_recovery(tmp_path):
    seed(tmp_path)
    (tmp_path / "auth.sqlite3-wal").write_bytes(b"synthetic WAL sentinel")
    apply_private_file_security(tmp_path / "auth.sqlite3-wal")
    before = authority_bytes(tmp_path)
    with pytest.raises(SchemaCompatibilityError, match="explicit index recovery"):
        inspect_existing_stores(tmp_path, (AUTH,))
    assert authority_bytes(tmp_path) == before


def test_immutable_opening_is_forbidden_when_wal_exists(tmp_path):
    seed(tmp_path)
    (tmp_path / "auth.sqlite3-wal").write_bytes(b"synthetic WAL sentinel")
    with pytest.raises(RuntimeError, match="cannot inspect retained WAL"):
        open_encrypted_sqlite(tmp_path / AUTH.filename,
                             database_key(tmp_path / AUTH.filename, "auth", create_if_missing=False),
                             read_only=True, immutable=True)


def test_refusal_diagnostic_does_not_echo_untrusted_ledger_text(tmp_path):
    database = seed(tmp_path)
    sentinel = "synthetic-untrusted-ledger-diagnostic-sentinel"
    with database.transaction() as connection:
        connection.execute("UPDATE schema_migrations SET name=? WHERE version=1", (sentinel,))
    with pytest.raises(StartupRefused) as captured:
        admit_retained_schemas(RetainedState(tmp_path, "provisioning", (AUTH.filename,)))
    rendered = "".join(traceback.format_exception(captured.value))
    assert "retained_schema_incompatible" in rendered
    assert sentinel not in rendered
