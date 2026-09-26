"""Owned qualification workers; parent failures cannot leave benchmarks running."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4


class WindowsJob:
    """Assign a kill-on-close job before releasing the waiting child launcher."""
    def __init__(self):
        from ctypes import wintypes as w
        size = ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", w.DWORD), ("Min", size), ("Max", size),
                        ("Active", w.DWORD), ("Affinity", size),
                        ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOps", "WriteOps", "OtherOps", "Read", "Write", "Other")]
        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessLimit", size),
                        ("JobLimit", size), ("PeakProcess", size), ("PeakJob", size)]
        self.info_type = Extended
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def sample(self):
        info = self.info_type()
        if not self.kernel.QueryInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        # Four LARGE_INTEGER values followed by four DWORD accounting counters.
        accounting = (ctypes.c_byte * 48)()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, accounting, 48, None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.PeakJob), ctypes.c_uint32.from_buffer(accounting, 40).value

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 99):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def available_memory() -> int:
    if os.name == "nt":
        class Memory(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
                (n, ctypes.c_uint64) for n in ("total", "available", "page", "free_page", "virtual", "free_virtual", "extended")]
        value = Memory()
        value.length = ctypes.sizeof(value)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            raise OSError("available_memory_measurement_failed")
        return int(value.available)
    values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return int(values["MemAvailable"].split()[0]) * 1024


def group_sample(group: int) -> tuple[int, int]:
    total, active = 0, 0
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == group and fields[0] != "Z":
                active += 1
                total += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (FileNotFoundError, ProcessLookupError):
            continue
    return total, active


def supervise(command: list[str], root: Path, attempt: Path, limit: float,
              *, limits: dict | None = None, environment: dict | None = None,
              cancel_event=None) -> dict:
    """The watchdog includes preparation, oracle work and synchronous cleanup."""
    token = uuid4().hex
    env = dict(os.environ, OFCA_QUALIFICATION_PROCESS=token, PYTHONUNBUFFERED="1")
    env.update(environment or {})
    started = time.monotonic()
    owner, process, reason, joined = None, None, None, False
    peak, minimum_free, minimum_memory = 0, shutil.disk_usage(attempt).free, available_memory()
    if limits and (minimum_free < limits["minimum_free_disk_bytes"] or
                   minimum_memory < limits["minimum_available_memory_bytes"]):
        return {"status": "BLOCKED", "worker_started": False, "reason": "insufficient_host_headroom",
                "free_disk_bytes": minimum_free, "available_memory_bytes": minimum_memory,
                "exit_code": None, "worker_joined": True, "complete": False}
    launcher = [sys.executable, str(Path(__file__).resolve()), "--owned-launcher", json.dumps(command)]
    try:
        owner = WindowsJob() if os.name == "nt" else None
        with (attempt / "worker.log").open("x", encoding="utf-8") as log:
            process = subprocess.Popen(launcher, cwd=root, env=env, stdin=subprocess.PIPE,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
            if owner:
                owner.assign(process)
            process.stdin.write(b"RUN\n")
            process.stdin.flush()
            while process.poll() is None:
                memory, _ = owner.sample() if owner else group_sample(process.pid)
                peak = max(peak, memory)
                minimum_free = min(minimum_free, shutil.disk_usage(attempt).free)
                minimum_memory = min(minimum_memory, available_memory())
                if cancel_event is not None and cancel_event.is_set():
                    reason = "operator_cancelled"
                elif time.monotonic() - started >= limit:
                    reason = "whole_worker_watchdog_expired"
                elif limits and peak > limits["maximum_process_tree_bytes"]:
                    reason = "process_tree_memory_limit"
                elif limits and minimum_free < limits["minimum_free_disk_bytes"]:
                    reason = "free_disk_limit"
                elif limits and minimum_memory < limits["minimum_available_memory_bytes"]:
                    reason = "available_memory_limit"
                if reason:
                    break
                time.sleep(min(0.1, max(0.001, limit / 10)))
            if reason is None:
                _, active = owner.sample() if owner else group_sample(process.pid)
                if active:
                    reason = "worker_left_running_children"
    except KeyboardInterrupt:
        reason = "operator_cancelled"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason = "worker_ownership_or_measurement_error:" + type(error).__name__
    finally:
        if process is not None:
            if owner:
                owner.terminate()
                # Assignment can fail while the launcher is still waiting for RUN.
                # It is our child, but not yet owned by the job in that case.
                if process.poll() is None:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=10)
            process.stdin.close()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                _, active = owner.sample() if owner else group_sample(process.pid)
                if active == 0:
                    joined = True
                    break
                time.sleep(0.01)
        if owner:
            owner.close()
    return {"status": "PASS" if process and process.returncode == 0 and reason is None and joined else "FAIL",
            "reason": reason, "exit_code": process.returncode if process else None,
            "timed_out": reason == "whole_worker_watchdog_expired", "worker_joined": joined,
            "worker_started": process is not None, "process_instance": token,
            "pid": process.pid if process else None, "seconds": time.monotonic() - started,
            "command": command, "maximum_seconds": limit,
            "resource_measurements": {"process_tree_peak_bytes": peak,
                "memory_measure": "job_peak_private_commit" if os.name == "nt" else "sampled_group_rss",
                "sample_seconds": 0.1, "minimum_free_disk_bytes": minimum_free,
                "minimum_available_memory_bytes": minimum_memory}}


def owned_launcher(command: list[str]) -> int:
    if sys.stdin.buffer.readline() != b"RUN\n":
        return 98
    if os.name != "nt":
        def parent_death():
            os.read(sys.stdin.fileno(), 1)
            os.killpg(os.getpgrp(), signal.SIGKILL)
        threading.Thread(target=parent_death, daemon=True).start()
    return subprocess.call(command, stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--owned-launcher":
        raise SystemExit("Internal worker owner; use qualify_analytics_baseline.py.")
    raise SystemExit(owned_launcher(json.loads(sys.argv[2])))
