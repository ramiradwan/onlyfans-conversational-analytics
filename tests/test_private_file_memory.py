"""Bound memory retained by repeated Windows private-file checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


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
