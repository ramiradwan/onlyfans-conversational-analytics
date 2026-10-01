"""Native process-tree ownership falsifiers; no product benchmark runs here."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tools.analytics_qualification_process import supervise

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]

ROOT = Path(__file__).resolve().parents[1]


def running(pid):
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def wait_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("owned process lifecycle did not finish")


def test_successful_parent_cannot_leave_a_running_child(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    pid_file, release = tmp_path / "grandchild.json", tmp_path / "release-parent"
    code = ("import subprocess,sys,json,time\nfrom pathlib import Path\n"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            f"Path({str(pid_file.with_suffix('.tmp'))!r}).write_text(json.dumps(p.pid))\n"
            f"Path({str(pid_file.with_suffix('.tmp'))!r}).replace({str(pid_file)!r})\n"
            f"while not Path({str(release)!r}).exists():\n    time.sleep(0.01)\n")
    handle, kernel = None, None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(supervise, [sys.executable, "-c", code], ROOT, tmp_path, 15)
        try:
            wait_until(pid_file.exists)
            pid = json.loads(pid_file.read_text())
            if os.name == "nt":
                import ctypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                handle = kernel.OpenProcess(0x100000, False, pid)
                assert handle, "test must hold the original child identity"
            release.touch()
            result = future.result(timeout=25)
            assert result["status"] == "FAIL"
            assert result["reason"] == "worker_left_running_children"
            assert result["worker_joined"]
            if handle:
                assert kernel.WaitForSingleObject(handle, 0) == 0
            else:
                assert not running(pid)
        finally:
            release.touch()
            if handle:
                kernel.CloseHandle(handle)


def test_supervisor_death_kills_its_entire_tree(tmp_path):
    pid_file = tmp_path / "children.json"
    child = tmp_path / "child.py"
    child.write_text("import subprocess,sys,time,json,os; from pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"Path({str(pid_file)!r}).write_text(json.dumps([os.getpid(),p.pid]))\n"
        "time.sleep(60)\n")
    supervisor = ("from pathlib import Path; from tools.analytics_qualification_process import supervise; "
        f"supervise({[sys.executable, str(child)]!r},Path({str(ROOT)!r}),Path({str(tmp_path)!r}),60)")
    process = subprocess.Popen([sys.executable, "-c", supervisor], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = []
    try:
        wait_until(pid_file.exists)
        pids = json.loads(pid_file.read_text())
        assert all(running(pid) for pid in pids)
        process.kill()
        process.wait(timeout=10)
        wait_until(lambda: all(not running(pid) for pid in pids))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        # Only the recorded children of this test are eligible for emergency cleanup.
        for pid in pids:
            if running(pid):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    os.kill(pid, 9)


@pytest.mark.parametrize("state,expected", [(0, True), (258, False)])
def test_zero_job_accounting_also_requires_signaled_process_handles(state, expected):
    from types import SimpleNamespace
    from tools.analytics_qualification_process import WindowsJob, worker_tree_exited
    job = WindowsJob.__new__(WindowsJob)
    job.process_handles = {123: 456}
    job.kernel = SimpleNamespace(WaitForSingleObject=lambda handle, timeout: state)
    job.sample = lambda: (0, 0)
    assert worker_tree_exited(job, 0) is expected


def test_failed_process_wait_cannot_report_a_join():
    from types import SimpleNamespace
    from tools.analytics_qualification_process import WindowsJob, worker_tree_exited
    job = WindowsJob.__new__(WindowsJob)
    job.process_handles = {123: 456}
    job.kernel = SimpleNamespace(WaitForSingleObject=lambda handle, timeout: 0xFFFFFFFF)
    job.sample = lambda: (0, 0)
    with pytest.raises(OSError, match="qualification_process_wait_failed"):
        worker_tree_exited(job, 0)


def test_reused_pid_outside_owned_job_is_not_retained():
    from types import SimpleNamespace
    from tools.analytics_qualification_process import WindowsJob
    job = WindowsJob.__new__(WindowsJob)
    job.process_handles = {}
    job.handle = 99
    closed = []
    def outside(process, owner, member):
        member._obj.value = False
        return True
    job.kernel = SimpleNamespace(OpenProcess=lambda *args: 456,
        IsProcessInJob=outside, CloseHandle=lambda value: closed.append(value))
    job._retain_member(123)
    assert closed == [456] and job.process_handles == {}


@pytest.mark.skipif(os.name == "nt", reason="Linux process filesystem observation")
@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError, PermissionError, OSError])
def test_proc_observation_only_accepts_disappearance(monkeypatch, error):
    def failed_read(path, *args, **kwargs):
        raise error("injected process observation")
    monkeypatch.setattr(Path, "read_text", failed_read)
    if error in (FileNotFoundError, ProcessLookupError):
        assert running(123) is False
    else:
        with pytest.raises(error, match="injected process observation"):
            running(123)
