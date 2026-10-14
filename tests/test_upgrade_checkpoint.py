"""Real encrypted checkpoints, process interruption and forward-only resume."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import os
import subprocess
import sys

import pytest

from app.persistence.database import LocalSQLite, open_encrypted_sqlite
from app.persistence.migrations import MigrationRunner, MigrationCancelled
from app.persistence.private_files import apply_private_file_security
from app.persistence import upgrade_lifecycle as upgrade
from app.security.local_data_key import database_key

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]
REPOSITORY = Path(__file__).resolve().parents[1]


def catalogs(tmp_path):
    directories = []
    for name in ("auth", "canonical"):
        directory = tmp_path / (name + "-catalog")
        directory.mkdir()
        (directory / "0001_retained.sql").write_text(
            "CREATE TABLE retained(value TEXT NOT NULL); INSERT INTO retained VALUES ('before migration');", encoding="utf-8")
        directories.append(directory)
    return tuple(upgrade.UpgradeStore(name + ".sqlite3", name, directory)
                 for name, directory in zip(("auth", "canonical"), directories))


def seed(root, stores):
    for store in stores:
        database = LocalSQLite(root / store.filename, key_scope=store.key_scope)
        MigrationRunner(database, migrations_dir=store.catalog).run()
    (root / "runtime.env").write_bytes(b"synthetic exact configuration and secret marker")
    apply_private_file_security(root / "runtime.env")
    for store in stores:
        (store.catalog / "0002_forward.sql").write_text(
            "UPDATE retained SET value='committed migration result';", encoding="utf-8")


def value(root, store, *, snapshot=False):
    path = root / (store.filename + ".bak") if snapshot else root / store.filename
    key_root = root.parent if snapshot else root
    key = database_key(key_root / store.filename, store.key_scope, create_if_missing=False)
    with closing(open_encrypted_sqlite(path, key, read_only=True,
                                      immutable=not path.with_name(path.name + "-wal").exists())) as connection:
        return connection.execute("SELECT value FROM retained").fetchone()[0]


def test_complete_verified_checkpoint_precedes_first_authoritative_migration(tmp_path):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    phases = []
    def phase(name):
        phases.append(name)
        if name == "migrating":
            journal = upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)
            assert journal["phase"] == "intent"
            for store in stores:
                assert value(root / upgrade.CHECKPOINT_DIRECTORY, store, snapshot=True) == "before migration"
                assert not (root / upgrade.CHECKPOINT_DIRECTORY / (store.filename + ".bak")).read_bytes().startswith(b"SQLite format 3")
            for file in (root / upgrade.CHECKPOINT_DIRECTORY).iterdir():
                assert b"synthetic exact configuration and secret marker" not in file.read_bytes()
    assert upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B", on_phase=phase)
    assert phases == ["checking", "checkpointing", "checkpointing", "checkpointing", "migrating", "migrating", "validating", "complete"]
    assert [value(root, store) for store in stores] == ["committed migration result"] * 2
    assert upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)["phase"] == "complete"
    assert not (root / upgrade.CHECKPOINT_DIRECTORY).exists()
    before = (root / upgrade.JOURNAL_FILENAME).read_bytes()
    assert not upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    assert (root / upgrade.JOURNAL_FILENAME).read_bytes() == before


def _crash_at(root, stores, phase, occurrence=1):
    script = """
import os,sys
from pathlib import Path
from app.core.startup_maintenance import acquire_runtime_ownership
from app.persistence.upgrade_lifecycle import UpgradeStore,maintain_existing_stores
root=Path(sys.argv[1])
stores=tuple(UpgradeStore(n+'.sqlite3',n,Path(p)) for n,p in zip(('auth','canonical'),sys.argv[2:4]))
# The public lower owner uses the same lease as real boot; root layout here is
# a bounded mechanism fixture rather than a production runtime.env.
from app.persistence.migrations import InstallationMigrationLock
with InstallationMigrationLock(root/'.ofca-runtime-writer.lock'):
 count=0
 def progress(phase):
  global count
  if phase==sys.argv[4]:
   count+=1
   if count==int(sys.argv[5]): os._exit(91)
 maintain_existing_stores(root,stores,release_id='synthetic-B',on_phase=progress)
"""
    result = subprocess.run([sys.executable, "-c", script, str(root), *(str(s.catalog) for s in stores),
                             phase, str(occurrence)], cwd=REPOSITORY, env=os.environ.copy(), timeout=30)
    assert result.returncode == 91


@pytest.mark.parametrize("phase,occurrence", [("checkpointing", 2), ("migrating", 2), ("validating", 1)])
def test_killed_process_resumes_forward_without_restoring_committed_state(tmp_path, phase, occurrence):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    _crash_at(root, stores, phase, occurrence)
    if phase == "checkpointing":
        assert (root / upgrade.CHECKPOINT_DIRECTORY / "auth.sqlite3.bak").is_file()
        assert not (root / upgrade.JOURNAL_FILENAME).exists()
    if phase == "migrating":
        assert [value(root, store) for store in stores] == ["committed migration result", "before migration"]
    assert upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    assert [value(root, store) for store in stores] == ["committed migration result"] * 2
    assert not (root / upgrade.CHECKPOINT_DIRECTORY).exists()


def test_resume_refuses_different_target_and_preserves_every_authoritative_byte(tmp_path):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    _crash_at(root, stores, "migrating", 2)
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and not p.name.endswith("-shm")}
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="target_conflict"):
        upgrade.maintain_existing_stores(root, stores, release_id="different-release")
    assert {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and not p.name.endswith("-shm")} == before


def test_checkpoint_tampering_cannot_resume_or_become_a_new_installation(tmp_path):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    _crash_at(root, stores, "migrating", 2)
    snapshot = root / upgrade.CHECKPOINT_DIRECTORY / "auth.sqlite3.bak"
    snapshot.write_bytes(b"synthetic damaged checkpoint")
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="checkpoint_changed"):
        upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    assert value(root, stores[0]) == "committed migration result"
    assert value(root, stores[1]) == "before migration"


def test_missing_store_after_interruption_never_creates_replacement(tmp_path):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    _crash_at(root, stores, "migrating", 2)
    (root / "canonical.sqlite3").unlink()
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="retained_state_changed"):
        upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    assert not (root / "canonical.sqlite3").exists()


def test_insufficient_space_and_cancellation_precede_authoritative_writes(tmp_path, monkeypatch):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    monkeypatch.setattr(upgrade.shutil, "disk_usage", lambda _: type("Space", (), {"free": 0})())
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="space_insufficient"):
        upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    assert [value(root, store) for store in stores] == ["before migration"] * 2
    assert not (root / upgrade.JOURNAL_FILENAME).exists()
    with pytest.raises(MigrationCancelled):
        upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B", should_cancel=lambda: True)


def test_authenticated_deletion_after_completion_cannot_restore_from_upgrade_cohort(tmp_path):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    with LocalSQLite(root / stores[1].filename, key_scope=stores[1].key_scope).transaction() as connection:
        connection.execute("DELETE FROM retained")
    assert not (root / upgrade.CHECKPOINT_DIRECTORY).exists()
    assert not upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    with LocalSQLite(root / stores[1].filename, key_scope=stores[1].key_scope).read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM retained").fetchone()[0] == 0


def test_cancellation_rolls_back_uncommitted_schema_and_ledger_before_exit(tmp_path, monkeypatch):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    (stores[0].catalog / "0002_forward.sql").write_text(
        "CREATE TABLE half_written AS WITH RECURSIVE work(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM work WHERE n<10000) SELECT n FROM work; UPDATE retained SET value='committed migration result';",
        encoding="utf-8")
    entered = [False]
    steps = [0]
    original = MigrationRunner._apply
    def applying(connection, migration):
        entered[0] = True
        original(connection, migration)
    def cancelled():
        if entered[0]:
            steps[0] += 1
        return steps[0] >= 4
    monkeypatch.setattr(MigrationRunner, "_apply", staticmethod(applying))
    with pytest.raises(MigrationCancelled):
        upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B", should_cancel=cancelled)
    assert entered[0]
    database = LocalSQLite(root / stores[0].filename, key_scope=stores[0].key_scope)
    with database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='half_written'").fetchone() is None
    assert value(root, stores[0]) == "before migration"
    assert upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)["phase"] == "intent"
    assert upgrade.maintain_existing_stores(root, stores, release_id="synthetic-B")
    assert value(root, stores[0]) == "committed migration result"
