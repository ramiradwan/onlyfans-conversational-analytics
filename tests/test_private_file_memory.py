"""Bound memory retained by repeated Windows private-file checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat, pytest.mark.serial]


@pytest.mark.windows_production
@pytest.mark.skipif(os.name != "nt", reason="Windows owner and token semantics")
def test_private_objects_belong_to_user_even_when_created_elevated(tmp_path):
    import ctypes
    from app.persistence import private_files as files

    directory = tmp_path / "private-user-root"
    directory.mkdir()
    files.apply_private_directory_security(directory)
    target = directory / "authority.bin"
    target.write_bytes(b"original protected fixture")
    identity = target.stat().st_ino
    files.apply_private_file_security(target)
    assert files._windows_acl_is_owner_only(directory)
    assert files.private_file_identity(target, repair_permissions=False)[1] == identity

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    assert files._windows_user_sid(advapi, kernel).startswith("S-1-")
    # The ACL verifier compares both the owner and sole full-control ACE with
    # TokenUser. An Administrators-only object must not satisfy that contract.
    assert target.read_bytes() == b"original protected fixture"


@pytest.mark.windows_production
@pytest.mark.skipif(os.name != "nt", reason="Windows non-elevated shell token")
def test_elevated_creation_is_writable_without_elevation_and_admin_state_is_not_admitted(tmp_path):
    import ctypes
    from app.persistence import private_files as files

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.POINTER(ctypes.c_void_p)]
    advapi.OpenProcessToken.restype = ctypes.c_int
    advapi.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.POINTER(ctypes.c_uint32)]
    advapi.GetTokenInformation.restype = ctypes.c_int
    advapi.ImpersonateLoggedOnUser.argtypes = [ctypes.c_void_p]
    advapi.ImpersonateLoggedOnUser.restype = ctypes.c_int
    advapi.DuplicateTokenEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.POINTER(ctypes.c_void_p)]
    advapi.DuplicateTokenEx.restype = ctypes.c_int
    advapi.RevertToSelf.restype = ctypes.c_int
    token, shell_token, medium = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    shell_process = None
    assert advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    try:
        size, elevation = ctypes.c_uint32(), ctypes.c_uint32()
        assert advapi.GetTokenInformation(token, 18, ctypes.byref(elevation),
                                         ctypes.sizeof(elevation), ctypes.byref(size))
        if elevation.value != 2:  # TokenElevationTypeFull
            pytest.skip("requires an elevated split token to prove medium-token access")
        # Some Windows builds return an identification-only linked token.
        # Use the actual medium-token shell in this session and check its user
        # identity and elevation before touching only the owned test fixtures.
        shell_pid = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "$s=(Get-Process -Id $PID).SessionId; Get-Process explorer -ErrorAction SilentlyContinue | Where-Object SessionId -eq $s | Select-Object -First 1 -ExpandProperty Id"],
            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        if not shell_pid:
            pytest.skip("requires this qualification session's non-elevated Explorer")
        shell_process = kernel.OpenProcess(0x1000, False, int(shell_pid))
        assert shell_process
        assert advapi.OpenProcessToken(shell_process, 0x0008 | 0x0002,
                                      ctypes.byref(shell_token))
        assert advapi.DuplicateTokenEx(shell_token, 0x0008 | 0x0004, None, 2, 2,
                                       ctypes.byref(medium)), ctypes.WinError(ctypes.get_last_error())
        assert advapi.GetTokenInformation(medium, 20, ctypes.byref(elevation),
                                         ctypes.sizeof(elevation), ctypes.byref(size))
        assert elevation.value == 0
        user_sid = files._windows_user_sid(advapi, kernel)
        directory = tmp_path / "user-owned"
        directory.mkdir()
        files.apply_private_directory_security(directory)
        target = directory / "authority.bin"
        target.write_bytes(b"original protected fixture")
        files.apply_private_file_security(target)
        original_identity = files.private_file_identity(target, repair_permissions=False)

        # Reproduce the old helper's exact group-owner/group-only ACL on an
        # independent disposable file, never on retained application state.
        legacy = tmp_path / "administrator-only.bin"
        legacy.write_bytes(b"unsupported owner")
        subprocess.run(["icacls", str(legacy), "/setowner", "*S-1-5-32-544"],
                       check=True, capture_output=True, timeout=10)
        subprocess.run(["icacls", str(legacy), "/inheritance:r", "/grant:r",
                        "*S-1-5-32-544:F"], check=True, capture_output=True, timeout=10)
        before = subprocess.run(["icacls", str(legacy)], check=True,
                                capture_output=True, timeout=10).stdout
        with pytest.raises(files.PrivateFileSecurityError):
            files.private_file_identity(legacy, repair_permissions=False)
        assert subprocess.run(["icacls", str(legacy)], check=True,
                              capture_output=True, timeout=10).stdout == before

        assert advapi.ImpersonateLoggedOnUser(medium)
        try:
            assert files._windows_user_sid(advapi, kernel) == user_sid
            assert files.private_file_identity(target, repair_permissions=False) == original_identity
            assert target.read_bytes() == b"original protected fixture"
            with target.open("ab") as handle:
                handle.write(b"; medium-token write")
            # The root admits presentation creation without granting access to
            # any other user or to the Administrators group.
            (directory / "webview").mkdir()
            (directory / "webview" / "probe").write_bytes(b"writable")
            with pytest.raises(PermissionError):
                legacy.read_bytes()
        finally:
            assert advapi.RevertToSelf()
    finally:
        if medium.value:
            assert kernel.CloseHandle(medium)
        if shell_token.value:
            assert kernel.CloseHandle(shell_token)
        if shell_process:
            assert kernel.CloseHandle(shell_process)
        assert kernel.CloseHandle(token)


def _measure_acl_growth(path: Path, *, combined: bool = False) -> dict[str, int]:
    import ctypes
    import gc
    import tracemalloc

    from app.persistence.private_files import apply_private_file_security, private_file_identity
    check_file = private_file_identity if combined else apply_private_file_security

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
            check_file(path)
        before = sample()
        for _ in range(2000):
            check_file(path)
        after = sample()
        return {
            "private_bytes": after[0] - before[0],
            "traced_bytes": after[1] - before[1],
        }
    finally:
        tracemalloc.stop()


@pytest.mark.windows_production
@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
@pytest.mark.parametrize('combined', [False, True])
def test_private_file_checks_have_bounded_memory(tmp_path: Path, combined: bool) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app; print(app.__file__); "
            "from pathlib import Path; import json, sys; "
            "from tests.test_private_file_memory import _measure_acl_growth; "
            "print(json.dumps(_measure_acl_growth(Path(sys.argv[1]), combined=sys.argv[2]=='1')))",
            str(tmp_path / "private.bin"),
            "1" if combined else "0",
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



def test_private_file_identity_matches_live_file_and_detects_replacement(tmp_path):
    from app.persistence import private_files as files
    target=tmp_path/'current.bin';target.write_bytes(b'original')
    files.apply_private_file_security(target)
    initial=files.private_file_identity(target)
    metadata=target.stat()
    assert initial==(metadata.st_dev,metadata.st_ino)
    replacement=tmp_path/'replacement.bin';replacement.write_bytes(b'replacement')
    files.apply_private_file_security(replacement)
    os.replace(replacement,target)
    assert files.private_file_identity(target)!=initial
    assert target.read_bytes()==b'replacement'


def test_private_identity_missing_sidecar_is_distinct_from_unverified_file(tmp_path):
    from app.persistence import private_files as files
    missing=tmp_path/'missing-wal'
    assert files.private_file_identity(missing,missing_ok=True) is None
    with pytest.raises(files.PrivateFileSecurityError):files.private_file_identity(missing)
    assert not missing.exists()
    with pytest.raises(files.PrivateFileSecurityError):files.private_file_identity(tmp_path,missing_ok=True)


@pytest.mark.skipif(os.name!='nt',reason='Windows native security handles')
@pytest.mark.windows_production
def test_private_identity_observes_DACL_through_one_live_handle(tmp_path,monkeypatch):
    from app.persistence import private_files as files
    target=tmp_path/'current.bin';target.write_bytes(b'fixture');files.apply_private_file_security(target)
    original=files._windows_acl_is_owner_only;handles=[]
    def observe(path,*,handle=None):
        assert handle is not None;handles.append(handle)
        return original(path,handle=handle)
    monkeypatch.setattr(files,'_windows_acl_is_owner_only',observe)
    initial=files.private_file_identity(target)
    assert len(handles)==1 and initial==(target.stat().st_dev,target.stat().st_ino)
    # A later, real ACL change is observed and repaired; no cached authorization.
    subprocess.run(['icacls',str(target),'/inheritance:e'],check=True,capture_output=True,timeout=10)
    assert not original(target)
    assert files.private_file_identity(target)==initial
    assert len(handles)==3 and original(target)
    assert files.private_file_identity(target)==initial and len(handles)==4


@pytest.mark.skipif(os.name!='nt',reason='Windows native handles')
@pytest.mark.windows_production
@pytest.mark.parametrize('inject_error',[False,True])
def test_private_identity_releases_native_handle_on_success_and_failure(tmp_path,monkeypatch,inject_error):
    import ctypes,gc
    from app.persistence import private_files as files
    target=tmp_path/'handles.bin';target.write_bytes(b'fixture');files.apply_private_file_security(target)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=ctypes.c_void_p
    count=kernel.GetProcessHandleCount;count.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)];count.restype=ctypes.c_int
    def handles():
        value=ctypes.c_uint32();assert count(kernel.GetCurrentProcess(),ctypes.byref(value));return value.value
    files.private_file_identity(target)
    if inject_error:
        def fail(*args,**kwargs):raise OSError('native ACL read failure')
        monkeypatch.setattr(files,'_windows_acl_is_owner_only',fail)
    before=handles()
    for _ in range(200):
        if inject_error:
            with pytest.raises(files.PrivateFileSecurityError):files.private_file_identity(target)
        else:files.private_file_identity(target)
    gc.collect()
    assert handles()<=before+2


@pytest.mark.skipif(os.name!='nt',reason='Windows permission repair')
@pytest.mark.parametrize('defect',['read_error','write_error','recheck_false','replacement'])
def test_combined_identity_repair_never_trusts_a_failed_or_changed_file(tmp_path,monkeypatch,defect):
    from app.persistence import private_files as files
    identity=(12,34);reads=[]
    def observation(path,**kwargs):
        reads.append(True)
        if defect=='read_error':raise OSError('access denied')
        if len(reads)==1:return identity,False
        return ((56,78),True) if defect=='replacement' else (identity,False)
    def repair(path):
        if defect=='write_error':raise OSError('cannot establish private file')
    monkeypatch.setattr(files,'_windows_private_file_observation',observation)
    monkeypatch.setattr(files,'_set_windows_owner_only_acl',repair)
    with pytest.raises(files.PrivateFileSecurityError):files.private_file_identity(tmp_path/'untrusted.bin')
