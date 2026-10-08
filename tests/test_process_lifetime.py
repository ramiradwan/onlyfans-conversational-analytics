"""Parent EOF shuts down a windowed process without console signals."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from app.core.process_lifetime import HANDLE_ENV, parent_lifetime

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


def test_absent_parent_does_not_request_shutdown(monkeypatch):
    monkeypatch.delenv(HANDLE_ENV, raising=False)
    requests = []
    with parent_lifetime(lambda: requests.append(True)):
        pass
    assert requests == []


def test_invalid_parent_handle_is_rejected(monkeypatch):
    monkeypatch.setenv(HANDLE_ENV, "invalid")
    with pytest.raises(ValueError, match="inherited pipe"):
        with parent_lifetime(lambda: None):
            pytest.fail("invalid supervision cannot start")
    assert HANDLE_ENV not in os.environ


@pytest.mark.parametrize("parent_closes", [False, True])
def test_windowed_child_observes_parent_eof_and_joins(tmp_path, parent_closes):
    reader, writer = os.pipe()
    os.set_inheritable(reader, True)
    environment = os.environ.copy()
    options = {}
    executable = Path(sys.executable)
    if os.name == "nt":
        import msvcrt
        handle = msvcrt.get_osfhandle(reader)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": [handle]}
        options["startupinfo"] = startup
        executable = executable.with_name("pythonw.exe")
        assert executable.is_file()
    else:
        handle = reader
        options["pass_fds"] = (reader,)
    environment[HANDLE_ENV] = str(handle)
    marker = tmp_path / "ready"
    result = tmp_path / "result"
    script = """
import sys, threading
from pathlib import Path
from app.core.process_lifetime import parent_lifetime
stopped = threading.Event()
with parent_lifetime(stopped.set):
    Path(sys.argv[1]).write_text('ready')
    observed = stopped.wait(5 if sys.argv[3] == 'True' else .2)
Path(sys.argv[2]).write_text(str(observed))
"""
    process = None
    try:
        process = subprocess.Popen([str(executable), "-c", script, str(marker), str(result), str(parent_closes)],
            env=environment, cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        os.close(reader)
        reader = None
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.01)
        assert marker.exists()
        if parent_closes:
            os.close(writer)
            writer = None
        assert process.wait(timeout=6) == 0
        assert result.read_text() == str(parent_closes)
    finally:
        for descriptor in (reader, writer):
            if descriptor is not None:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
