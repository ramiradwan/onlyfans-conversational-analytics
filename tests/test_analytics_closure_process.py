"""Native process-tree ownership falsifiers; no product benchmark runs here."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tools.analytics_qualification_process import supervise

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
    except FileNotFoundError:
        return False


def wait_until(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("owned process lifecycle did not finish")


def test_successful_parent_cannot_leave_a_running_child(tmp_path):
    pid_file = tmp_path / "grandchild.json"
    code = ("import subprocess,sys,json; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(pid_file)!r}).write_text(json.dumps(p.pid))")
    result = supervise([sys.executable, "-c", code], ROOT, tmp_path, 15)
    assert result["status"] == "FAIL"
    assert result["reason"] == "worker_left_running_children"
    assert result["worker_joined"]
    pid = json.loads(pid_file.read_text())
    assert not running(pid)


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
