"""Launch an immutable Brain executable and retain its passive lifecycle stream."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from tools import analytics_qualification as q


def receipt_account(run_id, account):
    return hashlib.sha256((run_id + "\0account\0" + account).encode()).hexdigest()


def settings_environment_names():
    source = Path(__file__).resolve().parents[1] / "app/core/config.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    definitions = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings"]
    if len(definitions) != 1:
        raise ValueError("settings_environment_fields_unavailable")
    return {node.target.id.upper() for node in definitions[0].body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}


def decode_receipt(line):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate_receipt_field")
        return result
    if len(line) > 4096:
        raise ValueError("oversized_lifecycle_receipt")
    return json.loads(line, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_receipt_number")))


class ReceiptStream:
    def __init__(self, path, run_id, pid, started_ns):
        self.path, self.run_id, self.pid, self.started_ns = Path(path), run_id, pid, started_ns
        self.records, self.error = [], None
        self.bytes = 0
        self.lock = threading.Lock()

    def append(self, line, received_ns):
        value = decode_receipt(line)
        with self.lock:
            if (not isinstance(value, dict) or value.get("schema") != "analytics-lifecycle.v1"
                    or value.get("run_id") != self.run_id or value.get("pid") != self.pid
                    or type(value.get("sequence")) is not int
                    or value["sequence"] != len(self.records) + 1
                    or type(value.get("monotonic_ns")) is not int
                    or not self.started_ns <= value["monotonic_ns"] <= received_ns
                    or (self.records and (self.records[-1].get("event_type") == "footer"
                        or value["monotonic_ns"] < self.records[-1]["monotonic_ns"]))
                    or value.get("event_type") not in {"startup", "canonical_commit", "activation_commit",
                        "cleanup_complete", "scheduler_drained", "persisted_validation", "shutdown", "footer",
                        "build_started", "build_completed", "build_cancelled"}):
                raise ValueError("lifecycle_identity_sequence_or_clock_invalid")
            if not self.records and (value.get("event_type") != "startup"
                    or not value.get("clock_implementation")
                    or type(value.get("clock_resolution_ns")) not in (int, float)
                    or not math.isfinite(value["clock_resolution_ns"])
                    or value["clock_resolution_ns"] <= 0):
                raise ValueError("lifecycle_startup_missing")
            if len(self.records) >= 262144 or self.bytes + len(line) > 268435456:
                raise ValueError("lifecycle_record_budget_exceeded")
            self.records.append(value)
            self.bytes += len(line)
        return value

    def read(self, stream):
        try:
            with self.path.open("xb") as output:
                while line := stream.readline(4097):
                    self.append(line, time.monotonic_ns())
                    output.write(line)
                output.flush()
                os.fsync(output.fileno())
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.error = type(error).__name__ + ":" + str(error)
        finally:
            stream.close()

    def snapshot(self):
        if self.error:
            raise ValueError(self.error)
        with self.lock:
            return list(self.records)

    def check_complete(self):
        records = self.snapshot()
        if (len(records) < 3 or records[-1].get("event_type") != "footer"
                or records[-1].get("complete") is not True
                or records[-2].get("event_type") != "shutdown"
                or records[-2].get("workers_joined") is not True
                or sum(r.get("event_type") == "startup" for r in records) != 1
                or sum(r.get("event_type") == "shutdown" for r in records) != 1):
            raise ValueError("lifecycle_stream_incomplete")


class PackagedProcess:
    def __init__(self, executable, data_directory, output, run_id):
        self.executable, self.data_directory = Path(executable), Path(data_directory)
        self.output, self.run_id = Path(output), run_id
        self.process = self.reader = self.receipts = None
        self.lifetime_write_fd = None

    def start(self):
        if self.process is not None:
            raise ValueError("packaged_process_already_started")
        self.output.mkdir(parents=True, exist_ok=False)
        environment = os.environ.copy()
        settings_names = settings_environment_names()
        for name in tuple(environment):
            if (name.upper() in settings_names
                    or name.startswith(("OFCA_TEST_", "PYTHONPATH", "PYTHONHOME", "ANALYTICS_RECEIPT_"))
                    or name == "BRAIN_PARENT_LIFETIME_HANDLE"):
                environment.pop(name)
        environment["LOCAL_ANALYTICS_DATA_DIR"] = str(self.data_directory)
        environment["ANALYTICS_RECEIPT_RUN_ID"] = self.run_id
        read_fd, write_fd = os.pipe()
        try:
            lifetime_read_fd, lifetime_write_fd = os.pipe()
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        try:
            os.set_inheritable(write_fd, True)
            os.set_inheritable(lifetime_read_fd, True)
            options = {}
            if os.name == "nt":
                import msvcrt
                handle = msvcrt.get_osfhandle(write_fd)
                lifetime_handle = msvcrt.get_osfhandle(lifetime_read_fd)
                startup = subprocess.STARTUPINFO()
                startup.lpAttributeList = {"handle_list": [handle, lifetime_handle]}
                options["startupinfo"] = startup
            else:
                handle, lifetime_handle = write_fd, lifetime_read_fd
                options["pass_fds"] = (write_fd, lifetime_read_fd)
            environment["ANALYTICS_RECEIPT_HANDLE"] = str(handle)
            environment["BRAIN_PARENT_LIFETIME_HANDLE"] = str(lifetime_handle)
            self.started_ns = time.monotonic_ns()
            self.process = subprocess.Popen([str(self.executable), "--brain"],
                cwd=self.executable.parent, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        except BaseException:
            os.close(read_fd)
            os.close(lifetime_write_fd)
            raise
        finally:
            os.close(write_fd)
            os.close(lifetime_read_fd)
        self.lifetime_write_fd = lifetime_write_fd
        stream = None
        try:
            stream = os.fdopen(read_fd, "rb")
            self.receipts = ReceiptStream(self.output / "lifecycle.ndjson", self.run_id,
                                          self.process.pid, self.started_ns)
            self.reader = threading.Thread(target=self.receipts.read, args=(stream,),
                name="packaged-lifecycle-reader", daemon=True)
            self.reader.start()
            q.write_once(self.output / "process.json", {"pid": self.process.pid,
                "run_id": self.run_id, "started_ns": self.started_ns,
                "instance": f"{self.process.pid}:{self.run_id}",
                "executable_sha256": q.file_digest(self.executable),
                "supervisor_instance": os.environ.get("OFCA_QUALIFICATION_PROCESS")})
        except BaseException:
            os.close(self.lifetime_write_fd)
            self.lifetime_write_fd = None
            self.process.kill()
            self.process.wait(timeout=1)
            if self.reader is not None and self.reader.ident is not None:
                self.reader.join(timeout=1)
                if self.reader.is_alive():
                    raise ValueError("failed_launch_reader_not_joined")
            elif stream is None:
                os.close(read_fd)
            else:
                stream.close()
            raise
        return self

    def stop(self, timeout=10):
        if self.process is None:
            return
        deadline = time.monotonic() + timeout
        if self.lifetime_write_fd is not None:
            os.close(self.lifetime_write_fd)
            self.lifetime_write_fd = None
        failure = None
        try:
            self.process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            failure = "packaged_shutdown_deadline_exceeded"
            self.process.kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                failure = "packaged_process_not_joined"
        finally:
            self.reader.join(timeout=1 if failure else max(0, deadline - time.monotonic()))
        if self.reader.is_alive():
            raise ValueError("lifecycle_reader_not_joined")
        if failure:
            raise ValueError(failure)
        if self.process.returncode != 0:
            raise ValueError("packaged_process_exit_failed")
        self.receipts.check_complete()
