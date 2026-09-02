"""Boot dispatch coverage at the import boundary."""

from __future__ import annotations

import builtins
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app import packaged_entry
from app.core.config import Settings


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
# Every Settings field name, uppercased: the environment surface cleared from
# the child process below.
_SETTINGS_ENVIRONMENT_NAMES = {name.upper() for name in Settings.model_fields}

_FRESH_PROCESS_PROBE = """\
import sys

from app import packaged_entry

application = packaged_entry.select_brain_application()
boot_mode = "provisioning" if application.openapi_url is None else "configured"
print(f"boot_mode={boot_mode}")
print(f"config_imported={str('app.core.config' in sys.modules).lower()}")

from fastapi.testclient import TestClient

with TestClient(application) as client:
    response = client.get("/health")
    print(f"health_status={response.status_code}")
"""


def test_fresh_process_unconfigured_provisioning_does_not_import_config(
    tmp_path,
) -> None:
    """A fresh interpreter with no runtime.env boots provisioning without
    reaching app.core.config: the provisioning import graph must not cross
    into configured-runtime settings before a bootstrap token can exist.
    """
    data_directory = tmp_path / "runtime-data"
    data_directory.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _SETTINGS_ENVIRONMENT_NAMES
    }
    environment["LOCAL_ANALYTICS_DATA_DIR"] = str(data_directory)

    result = subprocess.run(
        [sys.executable, "-c", _FRESH_PROCESS_PROBE],
        cwd=PRODUCT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "boot_mode=provisioning" in result.stdout
    assert "config_imported=false" in result.stdout
    assert "health_status=200" in result.stdout


def test_missing_runtime_configuration_selects_provisioning_without_main_import(
    tmp_path, monkeypatch
) -> None:
    original_import = builtins.__import__

    def fail_if_runtime_is_imported(name, *args, **kwargs):
        if name == "app.main":
            raise AssertionError("app.main import boundary was crossed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_if_runtime_is_imported)

    application = packaged_entry.select_brain_application(tmp_path)

    assert application.openapi_url is None


def test_runtime_configuration_presence_selects_runtime_application(tmp_path, monkeypatch) -> None:
    configuration = tmp_path / "runtime.env"
    configuration.touch()
    sentinel = object()
    main_module = types.ModuleType("app.main")
    main_module.app = sentinel

    monkeypatch.setitem(sys.modules, "app.main", main_module)

    assert packaged_entry.select_brain_application(tmp_path) is sentinel


_RUNNING_MUTEX_PROBE = """\
import ctypes
import sys
from ctypes import wintypes

from app import packaged_entry

if "--hold" in sys.argv:
    packaged_entry.hold_running_application_mutex()

open_mutex = ctypes.windll.kernel32.OpenMutexW
open_mutex.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
open_mutex.restype = wintypes.HANDLE
SYNCHRONIZE = 0x00100000
handle = open_mutex(
    SYNCHRONIZE, False, packaged_entry.RUNNING_APPLICATION_MUTEX_NAME
)
print(f"published={str(bool(handle)).lower()}")
"""


def _run_mutex_probe(*arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _RUNNING_MUTEX_PROBE, *arguments],
        cwd=PRODUCT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(os.name != "nt", reason="the mutex is a Windows installer signal")
def test_running_application_mutex_is_published_only_while_held() -> None:
    """The installer's running-application signal exists only when the app runs."""

    assert "published=false" in _run_mutex_probe()
    assert "published=true" in _run_mutex_probe("--hold")


def test_main_publishes_the_running_application_mutex(monkeypatch) -> None:
    """Both process modes reach the mutex before dispatching."""

    published: list[bool] = []

    def record() -> bool:
        published.append(True)
        return True

    monkeypatch.setattr(packaged_entry, "hold_running_application_mutex", record)
    monkeypatch.setattr(packaged_entry, "run_brain", lambda: 0)

    assert packaged_entry.main(["--brain"]) == 0
    assert published == [True]


def test_installer_mutex_directive_matches_the_packaged_name() -> None:
    """The uninstaller guard is inert if the two names drift apart."""

    script = (PRODUCT_ROOT / "packaging" / "inno" / "brain.iss").read_text(
        encoding="utf-8"
    )
    defined = re.findall(r'^#define AppMutexName "([^"]+)"$', script, re.MULTILINE)
    assert defined == [packaged_entry.RUNNING_APPLICATION_MUTEX_NAME]
    # The directive and the uninstall-side check both resolve to that one define,
    # so neither can drift from the name the application publishes.
    assert "AppMutex={#AppMutexName}" in script
    assert "OpenRunningApplicationMutex(Synchronize, False, '{#AppMutexName}')" in script
