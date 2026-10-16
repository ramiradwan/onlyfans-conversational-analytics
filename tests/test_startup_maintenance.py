"""Exercise early retained-state classification and actual writer lifetimes."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app import packaged_entry
from app.core.first_run import VerifiedGrantBindings, initialize_production_configuration
from app.core.startup_maintenance import StartupRefused, inspect_retained_state
from app.persistence.database import AuthSQLite
from app.security import local_data_key

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat, pytest.mark.serial]
ROOT = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()} if root.exists() else {}


def auth_store(root):
    database = AuthSQLite(root / "auth.sqlite3")
    with database.transaction() as connection:
        connection.execute("CREATE TABLE reviewed_marker (value TEXT)")
        connection.execute("INSERT INTO reviewed_marker VALUES ('synthetic-retained-state')")


def test_fresh_inspection_does_not_create_root_key_configuration_or_database(tmp_path):
    root = tmp_path / "absent"
    assert inspect_retained_state(root).mode == "fresh"
    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows installer mutex contract")
def test_brain_can_publish_installer_signal_already_held_by_its_bridge(monkeypatch):
    import ctypes
    from uuid import uuid4
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    name = "OFCA-qualification-installer-signal-" + uuid4().hex
    bridge = kernel.CreateMutexW(None, False, name)
    assert bridge
    monkeypatch.setattr(packaged_entry, "RUNNING_APPLICATION_MUTEX_NAME", name)
    monkeypatch.setattr(packaged_entry, "_running_application_mutex", None)
    try:
        assert packaged_entry.hold_running_application_mutex()
    finally:
        if packaged_entry._running_application_mutex is not None:
            kernel.CloseHandle(packaged_entry._running_application_mutex)
        kernel.CloseHandle(bridge)


def test_saved_provisioning_and_configured_roots_are_read_without_mutation(tmp_path):
    root = tmp_path / "retained"
    auth_store(root)
    before = snapshot(root)
    assert inspect_retained_state(root).mode == "provisioning"
    assert snapshot(root) == before
    initialize_production_configuration(bindings=VerifiedGrantBindings("synthetic-installation", "creator"),
                                        extension_id="a" * 32, data_directory=root)
    before = snapshot(root)
    state = inspect_retained_state(root)
    assert state.mode == "configured" and state.databases == ("auth.sqlite3",)
    assert snapshot(root) == before


@pytest.mark.parametrize("name", ["canonical.sqlite3", "projections.sqlite3", "analytics-projections.sqlite3"])
def test_retained_runtime_database_without_configuration_is_not_first_run(tmp_path, name):
    root = tmp_path / "retained"
    root.mkdir()
    (root / name).write_bytes(b"synthetic-encrypted-store-sentinel")
    before = snapshot(root)
    with pytest.raises(StartupRefused, match="retained_configuration_missing"):
        inspect_retained_state(root)
    assert snapshot(root) == before


@pytest.mark.parametrize("name", ["auth.sqlite3-wal", "unknown.sqlite3", "unexpected-state.json"])
def test_orphaned_and_unknown_retained_layout_refuses_without_rewriting_it(tmp_path, name):
    (tmp_path / name).write_bytes(b"synthetic-retained-layout-sentinel")
    before = snapshot(tmp_path)
    with pytest.raises(StartupRefused):
        inspect_retained_state(tmp_path)
    assert snapshot(tmp_path) == before


def test_missing_original_master_is_recovery_and_never_calls_creation(tmp_path, monkeypatch):
    (tmp_path / "auth.sqlite3").write_bytes(b"synthetic-retained-store-sentinel")
    before = snapshot(tmp_path)
    monkeypatch.setattr(local_data_key, "_test_master_key", lambda: None)
    monkeypatch.setattr(local_data_key.sys, "platform", "win32")
    def forbidden(*args, **kwargs):
        pytest.fail("retained-state inspection invoked key creation or ACL repair")
    monkeypatch.setattr(local_data_key, "_load_or_create_master_key", forbidden)
    monkeypatch.setattr(local_data_key, "apply_private_file_security", forbidden)
    with pytest.raises(StartupRefused, match="retained_state_unavailable"):
        inspect_retained_state(tmp_path)
    assert snapshot(tmp_path) == before


def test_configuration_path_conflict_refuses_before_any_runtime_graph(tmp_path):
    auth_store(tmp_path)
    result = initialize_production_configuration(bindings=VerifiedGrantBindings("synthetic-installation", "creator"),
                                                extension_id="a" * 32, data_directory=tmp_path)
    # Production first-run config escapes backslashes as JSON; replace a whole
    # configuration line so this falsifier is independent of path spelling.
    lines = result.configuration_file.read_text().splitlines()
    result.configuration_file.write_text("\n".join("CANONICAL_DATABASE_PATH=relative.sqlite3" if line.startswith("CANONICAL_DATABASE_PATH=") else line for line in lines) + "\n")
    before = snapshot(tmp_path)
    with pytest.raises(StartupRefused, match="retained_configuration_root_conflict"):
        inspect_retained_state(tmp_path)
    assert snapshot(tmp_path) == before


def test_read_only_private_observation_never_repairs_a_broad_acl(tmp_path, monkeypatch):
    from app.persistence import private_files
    path = tmp_path / "synthetic-secret.bin"
    path.write_bytes(b"reviewed-protected-material-sentinel")
    if os.name == "nt":
        monkeypatch.setattr(private_files, "_windows_private_file_observation", lambda *args, **kwargs: ((1, 2), False))
        def forbidden(*args, **kwargs):
            pytest.fail("read-only inspection repaired file permissions")
        monkeypatch.setattr(private_files, "_set_windows_owner_only_acl", forbidden)
    else:
        path.chmod(0o644)
    with pytest.raises(private_files.PrivateFileSecurityError):
        private_files.private_file_identity(path, repair_permissions=False)
    assert path.read_bytes() == b"reviewed-protected-material-sentinel"


def test_parent_observation_precedes_root_inspection_or_application_import(monkeypatch):
    @contextmanager
    def closed_parent(request):
        request()
        yield
    from app.core import process_lifetime, startup_maintenance
    monkeypatch.setattr(process_lifetime, "parent_lifetime", closed_parent)
    def forbidden(*args, **kwargs):
        pytest.fail("already-closed parent reached root ownership or a constructor")
    monkeypatch.setattr(startup_maintenance, "acquire_runtime_ownership", forbidden)
    monkeypatch.setattr(packaged_entry, "select_brain_application", forbidden)
    assert packaged_entry.run_brain() == 1


_LEASE_PROBE = '''
import sys
from app.core.startup_maintenance import acquire_runtime_ownership, StartupRefused
try:
    state = acquire_runtime_ownership(sys.argv[1])
except StartupRefused as error:
    print(str(error), flush=True)
    raise SystemExit(43)
print('owned:' + state.mode, flush=True)
if len(sys.argv) > 2:
    sys.stdin.readline()
'''


def test_two_processes_cannot_reach_constructors_and_killed_owner_releases_lease(tmp_path):
    root = tmp_path / "shared-root"
    owner = subprocess.Popen([sys.executable, "-u", "-c", _LEASE_PROBE, str(root), "hold"],
                             cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == "owned:fresh"
        assert owner.poll() is None
        contender = subprocess.run([sys.executable, "-c", _LEASE_PROBE, str(root)], cwd=ROOT,
                                   capture_output=True, text=True, timeout=20)
        assert contender.returncode == 43 and "runtime_writer_conflict" in contender.stdout
        assert not (root / "auth.sqlite3").exists()
        owner.kill()
        owner.wait(timeout=20)
        successor = subprocess.run([sys.executable, "-c", _LEASE_PROBE, str(root)], cwd=ROOT,
                                   capture_output=True, text=True, timeout=20)
        assert successor.returncode == 0 and "owned:fresh" in successor.stdout, successor.stderr
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=20)


def test_corrupt_root_refuses_packaged_boot_without_importing_configured_graph(tmp_path):
    (tmp_path / "canonical.sqlite3").write_bytes(b"synthetic-retained-state")
    probe = '''
import sys
from app.packaged_entry import run_brain
from app.core.startup_maintenance import StartupRefused
try:
    run_brain()
except StartupRefused as error:
    print(str(error))
    print('configured_graph=' + str('app.main' in sys.modules))
    raise SystemExit(43)
raise SystemExit(1)
'''
    environment = dict(os.environ, LOCAL_ANALYTICS_DATA_DIR=str(tmp_path))
    before = snapshot(tmp_path)
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 43 and "configured_graph=False" in result.stdout, result.stderr
    assert snapshot(tmp_path) == before
