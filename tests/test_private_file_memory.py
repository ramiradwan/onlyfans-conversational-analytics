"""Bound memory retained by repeated Windows private-file checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat, pytest.mark.serial]


def _measure_acl_growth(path: Path) -> dict[str, int]:
    import ctypes
    import gc
    import tracemalloc

    from app.persistence.private_files import apply_private_file_security

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            *[(name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                "PrivateUsage",
            )],
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int

    def sample() -> tuple[int, int]:
        gc.collect()
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PrivateUsage, tracemalloc.get_traced_memory()[0]

    path.write_bytes(b"fixture")
    tracemalloc.start()
    try:
        for _ in range(1000):
            apply_private_file_security(path)
        before = sample()
        for _ in range(2000):
            apply_private_file_security(path)
        after = sample()
        return {
            "private_bytes": after[0] - before[0],
            "traced_bytes": after[1] - before[1],
        }
    finally:
        tracemalloc.stop()


@pytest.mark.windows_production
@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
def test_private_file_checks_have_bounded_memory(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app; print(app.__file__); "
            "from pathlib import Path; import json, sys; "
            "from tests.test_private_file_memory import _measure_acl_growth; "
            "print(json.dumps(_measure_acl_growth(Path(sys.argv[1]))))",
            str(tmp_path / "private.bin"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    growth = json.loads(result.stdout.splitlines()[-1])
    print(json.dumps(growth))
    assert growth["private_bytes"] < 4 * 1024 * 1024, growth
    assert growth["traced_bytes"] < 4 * 1024 * 1024, growth



def test_already_private_acl_is_checked_each_time_without_rewriting(tmp_path, monkeypatch):
    from app.persistence import private_files as files
    checks, writes = [], []
    target = tmp_path / "private.bin"
    monkeypatch.setattr(files, "_windows_acl_is_owner_only", lambda path: checks.append(path) or True)
    monkeypatch.setattr(files, "_set_windows_owner_only_acl", lambda path: writes.append(path))
    for _ in range(3):
        files.apply_private_file_security(target, platform_name="nt")
    assert checks == [target] * 3 and writes == []


def test_changed_acl_is_repaired_and_independently_rechecked(tmp_path, monkeypatch):
    from app.persistence import private_files as files
    values = iter([True, False, True, True])
    calls = []
    def verify(path):
        calls.append("verify")
        return next(values)
    monkeypatch.setattr(files, "_windows_acl_is_owner_only", verify)
    monkeypatch.setattr(files, "_set_windows_owner_only_acl", lambda path: calls.append("repair"))
    for _ in range(3):
        files.apply_private_file_security(tmp_path / "changing.bin", platform_name="nt")
    assert calls == ["verify", "verify", "repair", "verify", "verify"]


@pytest.mark.parametrize("failure", ["initial_read", "write", "recheck_error", "recheck_rejected"])
def test_acl_fast_path_remains_fail_closed(tmp_path, monkeypatch, failure):
    from app.persistence import private_files as files
    calls = []
    def verify(path):
        calls.append("verify")
        if failure == "initial_read" or failure == "recheck_error" and calls.count("verify") == 2:
            raise OSError("injected security read error")
        return False
    def repair(path):
        calls.append("repair")
        if failure == "write":
            raise OSError("injected security write error")
    monkeypatch.setattr(files, "_windows_acl_is_owner_only", verify)
    monkeypatch.setattr(files, "_set_windows_owner_only_acl", repair)
    with pytest.raises(files.PrivateFileSecurityError):
        files.apply_private_file_security(tmp_path / "protected.bin", platform_name="nt")
    assert calls[0] == "verify"
    assert calls.count("repair") == (0 if failure == "initial_read" else 1)


@pytest.mark.windows_production
@pytest.mark.skipif(os.name != "nt", reason="Real Windows file permission checks")
def test_live_windows_permission_change_is_repaired_without_rewriting_good_acl(tmp_path, monkeypatch):
    from app.persistence import private_files as files
    target = tmp_path / "owner-only.bin"
    target.write_bytes(b"owned test data")
    files._set_windows_owner_only_acl(target)
    assert files._windows_acl_is_owner_only(target)
    original = files._set_windows_owner_only_acl
    repairs = []
    def repair(path):
        repairs.append(path)
        original(path)
    monkeypatch.setattr(files, "_set_windows_owner_only_acl", repair)
    identity = target.stat().st_ino
    files.apply_private_file_security(target)
    assert repairs == []
    # Change only this disposable test file; the protected-DACL check must now
    # fail even when the parent happens to have restrictive inherited grants.
    subprocess.run(["icacls", str(target), "/inheritance:e"], check=True,
                   capture_output=True, text=True, timeout=10)
    assert not files._windows_acl_is_owner_only(target)
    files.apply_private_file_security(target)
    assert repairs == [target] and files._windows_acl_is_owner_only(target)
    files.apply_private_file_security(target)
    assert repairs == [target]
    assert target.stat().st_ino == identity and target.read_bytes() == b"owned test data"
