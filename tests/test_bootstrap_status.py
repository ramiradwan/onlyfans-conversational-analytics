"""Child-owned observations are bounded and parent loss cancels work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
import jsonschema

from app.core import bootstrap_status as status

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]
REPOSITORY = Path(__file__).resolve().parents[1]


def test_closed_status_contract_and_final_listener_phase():
    reader, writer = os.pipe()
    stopped = threading.Event()
    emitter = status.BootstrapStatus(writer, stopped)
    try:
        for phase in ("starting", "checking", "checkpointing", "migrating", "validating", "ready-to-check-listener"):
            emitter.emit(phase)
        emitter.close()
        frames = [json.loads(line) for line in os.read(reader, status.MAX_TOTAL_BYTES).splitlines()]
        assert [frame["seq"] for frame in frames] == list(range(1, 7))
        assert all(set(frame) == {"schema", "seq", "phase", "reason"} and frame["reason"] is None for frame in frames)
        assert frames[-1]["phase"] == "ready-to-check-listener"
        schema = json.loads((REPOSITORY / "shared/bootstrap-status/schema.json").read_text())
        for frame in frames:
            jsonschema.validate(frame, schema)
        assert b"".join((json.dumps(frame, separators=(",", ":")) + "\n").encode() for frame in frames) == (
            REPOSITORY / "shared/bootstrap-status/happy.ndjson").read_bytes()
        with pytest.raises(ValueError):
            emitter.emit("migrating")
        assert not stopped.is_set()
    finally:
        emitter.close()
        os.close(reader)


def test_heartbeat_is_throttled_and_frame_limit_cancels_instead_of_growing(monkeypatch):
    written = []
    monkeypatch.setattr(status.os, "write", lambda fd, data: written.append(data) or len(data))
    now = [1.0]
    monkeypatch.setattr(status.time, "monotonic", lambda: now[0])
    stopped = threading.Event()
    emitter = status.BootstrapStatus(123, stopped)
    emitter.emit("starting")
    emitter.emit("checking")
    for _ in range(10):
        assert not emitter.cancelled()
    assert len(written) == 2
    for _ in range(status.MAX_FRAMES):
        now[0] += status.HEARTBEAT_SECONDS
        emitter.cancelled()
    assert stopped.is_set()
    assert len(written) == status.MAX_FRAMES
    assert all(len(frame) <= status.MAX_FRAME_BYTES for frame in written)


def test_broken_status_reader_requests_cancellation():
    reader, writer = os.pipe()
    os.close(reader)
    stopped = threading.Event()
    emitter = status.BootstrapStatus(writer, stopped)
    try:
        emitter.emit("starting")
        assert stopped.is_set()
    finally:
        emitter.close()


def test_shutdown_observation_is_one_bounded_tail_after_ready():
    reader, writer = os.pipe()
    stopped = threading.Event()
    emitter = status.BootstrapStatus(writer, stopped)
    try:
        with pytest.raises(ValueError):
            emitter.shutdown_complete(True)
        for phase in ("starting", "checking", "checkpointing", "migrating", "validating", "ready-to-check-listener"):
            emitter.emit(phase)
        emitter.shutdown_complete(True)
        with pytest.raises(ValueError):
            emitter.shutdown_complete(True)
        emitter.close()
        frames = os.read(reader, status.MAX_TOTAL_BYTES).splitlines(keepends=True)
        assert frames[-1] == (REPOSITORY / "shared/bootstrap-status/shutdown.ndjson").read_bytes()
        jsonschema.validate(json.loads(frames[-1]), json.loads(
            (REPOSITORY / "shared/bootstrap-status/shutdown.schema.json").read_text()))
        assert len(frames[-1]) <= status.MAX_FRAME_BYTES
        assert not stopped.is_set()
    finally:
        emitter.close()
        os.close(reader)


def test_invalid_status_handle_never_enters_application(monkeypatch):
    monkeypatch.setenv(status.HANDLE_ENV, "invalid")
    with pytest.raises(ValueError):
        with status.bootstrap_status(threading.Event()):
            pytest.fail("invalid bootstrap handle admitted startup")
    assert status.HANDLE_ENV not in os.environ


def test_parent_eof_interrupts_pre_listener_sql_and_exits_without_forced_cleanup(tmp_path):
    lifetime_read, lifetime_write = os.pipe()
    status_read, status_write = os.pipe()
    inherited = (lifetime_read, status_write)
    environment = os.environ.copy()
    options = {}
    for descriptor in inherited:
        os.set_inheritable(descriptor, True)
    if os.name == "nt":
        import msvcrt
        handles = [msvcrt.get_osfhandle(descriptor) for descriptor in inherited]
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": handles}
        options["startupinfo"] = startup
    else:
        handles = inherited
        options["pass_fds"] = inherited
    environment["BRAIN_PARENT_LIFETIME_HANDLE"] = str(handles[0])
    environment[status.HANDLE_ENV] = str(handles[1])
    script = """
import sys,threading
from pathlib import Path
from app.core.bootstrap_status import bootstrap_status
from app.core.process_lifetime import parent_lifetime
from app.persistence.database import AuthSQLite
from app.persistence.migrations import MigrationRunner,MigrationCancelled
from app.persistence.upgrade_lifecycle import UpgradeStore,inspect_existing_stores
root=Path(sys.argv[1]); catalog=root/'catalog'; catalog.mkdir(parents=True)
(catalog/'0001_marker.sql').write_text('CREATE TABLE synthetic(value TEXT);')
MigrationRunner(AuthSQLite(root/'auth.sqlite3'),migrations_dir=catalog).run()
stopped=threading.Event()
with parent_lifetime(stopped.set),bootstrap_status(stopped) as status:
 status.emit('checking')
 # Exercise real SQLite VM progress and cancellation during pre-listener
 # validation, rather than a sleep that cannot be interrupted by SQL.
 original=MigrationRunner._validate_database
 def validation(connection):
  connection.execute('WITH RECURSIVE work(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM work WHERE n<1000000000) SELECT sum(n) FROM work').fetchone()
  original(connection)
 MigrationRunner._validate_database=staticmethod(validation)
 try: inspect_existing_stores(root,(UpgradeStore('auth.sqlite3','auth',catalog),),should_cancel=status.cancelled)
 except MigrationCancelled:
  (root/'cancelled').write_text('safe pre-listener cancellation')
 else: raise RuntimeError('work completed before cancellation qualification')
"""
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "child")],
            cwd=REPOSITORY, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **options)
        os.close(lifetime_read); lifetime_read = None
        os.close(status_write); status_write = None
        # Read a closed frame, not a timing guess, before closing the parent.
        with os.fdopen(status_read, "rb", buffering=0) as reader:
            status_read = None
            assert json.loads(reader.readline())["phase"] == "starting"
            assert json.loads(reader.readline())["phase"] == "checking"
            started = time.monotonic()
            os.close(lifetime_write); lifetime_write = None
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, (stdout, stderr)
            assert time.monotonic() - started < 10
        assert (tmp_path / "child" / "cancelled").read_text() == "safe pre-listener cancellation"
    finally:
        for descriptor in (lifetime_read, lifetime_write, status_read, status_write):
            if descriptor is not None:
                os.close(descriptor)
        if process is not None and process.poll() is None:
            process.kill(); process.wait(timeout=5)
