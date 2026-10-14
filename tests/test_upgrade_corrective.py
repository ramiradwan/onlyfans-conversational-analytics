"""Forward-only corrective release admission using protected catalog proofs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.persistence import upgrade_lifecycle as upgrade
from app.persistence.database import LocalSQLite
from test_upgrade_checkpoint import catalogs, seed, value, REPOSITORY

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


def coordinate(version="1.0.6.0"):
    return {"kind": "package", "version": version, "identity": {
        "purpose": "qualification", "name": "OFCA.Qualification.Direct",
        "publisher": "CN=OFCA MSIX Qualification Direct", "architecture": "x64",
        "application_id": "Bridge", "update_owner": "direct-appinstaller",
        "app_installer_uri": "https://qualification.example.test/desktop/direct/Dipsy.appinstaller",
    }}


def crash(root, stores, *, release="B", coord=None, phase="migrating", occurrence=2, after_retarget=False):
    script = """
import json,os,sys
from pathlib import Path
from app.persistence.migrations import InstallationMigrationLock
from app.persistence import upgrade_lifecycle as u
root=Path(sys.argv[1])
stores=tuple(u.UpgradeStore(n+'.sqlite3',n,Path(p)) for n,p in zip(('auth','canonical'),sys.argv[2:4]))
coordinate=json.loads(sys.argv[5]); count=0
def phase(name):
 global count
 if name==sys.argv[6]:
  count+=1
  if count==int(sys.argv[7]): os._exit(91)
if sys.argv[8]=='yes':
 atomic=u._atomic_protected
 def after(path,data):
  atomic(path,data)
  if path.name==u.JOURNAL_FILENAME and data['phase']=='intent' and data['resume']['release']['id']==sys.argv[4]: os._exit(91)
 u._atomic_protected=after
with InstallationMigrationLock(root/'.ofca-runtime-writer.lock'):
 u.maintain_existing_stores(root,stores,release_id=sys.argv[4],release_coordinate=coordinate,on_phase=phase)
"""
    result = subprocess.run([sys.executable, "-c", script, str(root), *(str(s.catalog) for s in stores),
                             release, json.dumps(coord or coordinate()), phase, str(occurrence),
                             "yes" if after_retarget else "no"], cwd=REPOSITORY, env=os.environ.copy(),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 91, result.stderr


def setup(tmp_path, *, phase="migrating", occurrence=2):
    stores = catalogs(tmp_path)
    root = tmp_path / "retained"
    seed(root, stores)
    crash(root, stores, phase=phase, occurrence=occurrence)
    return root, stores


def bytes_at(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not p.name.endswith("-shm")}


def append_corrective(stores, number=3):
    for store in stores:
        (store.catalog / f"{number:04}_corrective.sql").write_text(
            f"ALTER TABLE retained ADD COLUMN corrective{number} INTEGER NOT NULL DEFAULT 1;", encoding="utf-8")


def test_newer_same_family_release_resumes_without_snapshot_restore_or_recheckpoint(tmp_path, monkeypatch):
    root, stores = setup(tmp_path)
    before = upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)
    with LocalSQLite(root / "auth.sqlite3", key_scope="auth").transaction() as connection:
        connection.execute("UPDATE retained SET value='newer security state'")
    append_corrective(stores)
    def forbidden(*_):
        pytest.fail("active verified checkpoint was replaced")
    monkeypatch.setattr(upgrade, "_snapshot_database", forbidden)
    def progress(phase):
        if phase == "migrating":
            current = upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)
            assert current["target"] == before["target"]
            assert current["files"] == before["files"]
            assert current["resume"]["release"]["version"] == "1.0.7.0"
    assert upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=coordinate("1.0.7.0"), on_phase=progress)
    assert value(root, stores[0]) == "newer security state"
    assert value(root, stores[1]) == "committed migration result"
    journal = upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)
    assert journal["phase"] == "complete" and journal["resume"]["schemas"]["auth.sqlite3"]["target"] == 3
    assert not (root / upgrade.CHECKPOINT_DIRECTORY).exists()
    assert not upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=coordinate("1.0.7.0"))


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(version="1.0.5.0"),
    lambda c: c.update(version="1.0.6.0"),
    lambda c: c["identity"].update(name="OFCA.Qualification.Another"),
    lambda c: c["identity"].update(publisher="CN=Another publisher"),
    lambda c: c["identity"].update(purpose="production"),
    lambda c: c["identity"].update(application_id="Another"),
    lambda c: c["identity"].update(app_installer_uri="https://another.example.test/feed.appinstaller"),
    lambda c: c.update(kind="legacy", identity={"product": "brain-bridge/v1"}, version="0.7.5"),
])
def test_changed_delivery_identity_or_nonforward_package_cannot_adopt_intent(tmp_path, mutation):
    root, stores = setup(tmp_path)
    candidate = coordinate("1.0.7.0")
    mutation(candidate)
    before = bytes_at(root)
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="target_conflict"):
        upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=candidate)
    assert bytes_at(root) == before


def test_unapplied_migration_history_is_part_of_the_corrective_proof(tmp_path):
    root, stores = setup(tmp_path)
    # Auth2 is already committed; canonical2 is pending. The latter cannot
    # silently change merely because its checksum is not in the live ledger.
    (stores[1].catalog / "0002_forward.sql").write_text("UPDATE retained SET value='rewritten pending migration';", encoding="utf-8")
    before = bytes_at(root)
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="target_conflict"):
        upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=coordinate("1.0.7.0"))
    assert bytes_at(root) == before


def test_interruption_after_durable_retarget_resumes_latest_intent_and_pins_its_unapplied_history(tmp_path):
    root, stores = setup(tmp_path)
    append_corrective(stores)
    crash(root, stores, release="C", coord=coordinate("1.0.7.0"), after_retarget=True)
    journal = upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)
    assert journal["resume"]["release"]["id"] == "C"
    assert value(root, stores[0]) == "committed migration result"
    (stores[1].catalog / "0003_corrective.sql").write_text("ALTER TABLE retained ADD COLUMN different INTEGER;", encoding="utf-8")
    before = bytes_at(root)
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="target_conflict"):
        upgrade.maintain_existing_stores(root, stores, release_id="D", release_coordinate=coordinate("1.0.8.0"))
    assert bytes_at(root) == before
    (stores[1].catalog / "0003_corrective.sql").write_text("ALTER TABLE retained ADD COLUMN corrective3 INTEGER NOT NULL DEFAULT 1;", encoding="utf-8")
    assert upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=coordinate("1.0.7.0"))


def test_newer_corrective_rebuilds_partial_checkpoint_only_before_sql_started(tmp_path):
    root, stores = setup(tmp_path, phase="checkpointing", occurrence=2)
    assert not (root / upgrade.JOURNAL_FILENAME).exists()
    append_corrective(stores)
    assert upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=coordinate("1.0.7.0"))
    assert all(value(root, store) == "committed migration result" for store in stores)


def test_completed_successor_detects_schema_regression_against_successor_not_original_target(tmp_path):
    root, stores = setup(tmp_path)
    append_corrective(stores)
    upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=coordinate("1.0.7.0"))
    with LocalSQLite(root / "auth.sqlite3", key_scope="auth").transaction() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=3")
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="schema_regressed"):
        upgrade.maintain_existing_stores(root, stores, release_id="C", release_coordinate=coordinate("1.0.7.0"))


def test_legacy_correction_requires_a_newer_application_version_and_catalog_prefix(tmp_path):
    source = tmp_path / "legacy-catalogs"
    source.mkdir()
    stores = catalogs(source)
    legacy_root = tmp_path / "legacy"
    seed(legacy_root, stores)
    legacy = {"kind": "legacy", "identity": {"product": "brain-bridge/v1"}, "version": "0.7.5"}
    crash(legacy_root, stores, coord=legacy)
    append_corrective(stores)
    before = bytes_at(legacy_root)
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="target_conflict"):
        upgrade.maintain_existing_stores(legacy_root, stores, release_id="different-binary-C", release_coordinate=legacy)
    assert bytes_at(legacy_root) == before
    legacy["version"] = "0.7.6"
    assert upgrade.maintain_existing_stores(legacy_root, stores, release_id="different-binary-C", release_coordinate=legacy)


@pytest.mark.parametrize("damage", ["unknown", "bad_digest", "bool_version"])
def test_protected_v2_metadata_remains_closed_and_consistent(tmp_path, damage):
    root, stores = setup(tmp_path)
    journal = upgrade._read_protected(root / upgrade.JOURNAL_FILENAME)
    if damage == "unknown":
        journal["resume"]["authority"] = True
    elif damage == "bad_digest":
        journal["resume"]["target"] = "0" * 64
    else:
        journal["resume"]["schemas"]["auth.sqlite3"]["source"] = True
    upgrade._atomic_protected(root / upgrade.JOURNAL_FILENAME, journal)
    before = bytes_at(root)
    with pytest.raises(upgrade.UpgradeRecoveryRequired, match="metadata_invalid"):
        upgrade.maintain_existing_stores(root, stores, release_id="B", release_coordinate=coordinate())
    assert bytes_at(root) == before
