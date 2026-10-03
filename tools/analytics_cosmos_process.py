"""Bounded tool subprocess transport with environment-only connection secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from app.analytics.exchange_codec import ExchangeInvalid, canonical_bytes

CONNECTION_ENV = ("ANALYTICS_GREMLIN_ENDPOINT", "ANALYTICS_GREMLIN_KEY",
                  "ANALYTICS_GREMLIN_DATABASE", "ANALYTICS_GREMLIN_GRAPH")
MAX_PIPE_BYTES = 4 * 1024 * 1024


def configured():
    return all(os.environ.get(name) for name in CONNECTION_ENV)


class NodeGremlinProcess:
    def __init__(self, *, node="node", timeout=30.0):
        if not configured():
            raise ExchangeInvalid("analytics_exchange_not_configured")
        self.timeout = timeout
        self.sequence = 0
        self.responses = queue.Queue(maxsize=2)
        self.stderr_seen = False
        self.writer = None
        entry = Path(__file__).with_name("analytics_cosmos_node") / "runner.cjs"
        self.process = subprocess.Popen([node, str(entry)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=os.environ.copy())
        self.stdout_thread = threading.Thread(target=self._stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _stdout(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_PIPE_BYTES + 1)
                if not line or len(line) > MAX_PIPE_BYTES or not line.endswith(b"\n"):
                    self.responses.put(None, timeout=1)
                    return
                self.responses.put(line, timeout=1)
        except (OSError, ValueError, queue.Full):
            return

    def _stderr(self):
        try:
            while self.process.stderr.read(4096):
                self.stderr_seen = True
        except (OSError, ValueError):
            pass

    def request(self, op, scope=None, *, timeout=None, **fields):
        self.sequence += 1
        request = {"id": self.sequence, "op": op, **fields}
        if scope is not None:
            request["scope"] = scope
        raw = canonical_bytes(request) + b"\n"
        if len(raw) > MAX_PIPE_BYTES:
            raise ExchangeInvalid("analytics_exchange_pipe_limit")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        written = threading.Event()
        failed = []
        def write():
            try:
                self.process.stdin.write(raw)
                self.process.stdin.flush()
            except (OSError, ValueError):
                failed.append(True)
            finally:
                written.set()
        try:
            self.writer = threading.Thread(target=write, daemon=True)
            self.writer.start()
            if not written.wait(max(0, deadline - time.monotonic())):
                self.close()
                raise ExchangeInvalid("analytics_exchange_timeout")
            self.writer.join()
            if failed:
                raise OSError()
            line = self.responses.get(timeout=max(0, deadline - time.monotonic()))
            response = json.loads(line) if line is not None else None
            if not isinstance(response, dict) or response.get("id") != self.sequence:
                raise ExchangeInvalid("analytics_exchange_transport")
            if response.get("status") != "ok":
                code = response.get("code")
                allowed = {"not_configured", "invalid_request", "identity_conflict", "not_published",
                           "timeout", "throttled", "unauthorized", "transport", "server_error"}
                raise ExchangeInvalid("analytics_exchange_" + (code if code in allowed else "transport"))
            return response
        except ExchangeInvalid:
            raise
        except queue.Empty:
            self.close()
            raise ExchangeInvalid("analytics_exchange_timeout") from None
        except (OSError, ValueError, TypeError):
            self.close()
            raise ExchangeInvalid("analytics_exchange_transport") from None

    def close(self):
        if self.writer is not None and self.writer.is_alive() and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
            self.writer.join(timeout=2)
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=5)
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
        if self.writer is not None and self.writer.is_alive():
            raise ExchangeInvalid("analytics_exchange_process_unjoined")
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        if self.stdout_thread.is_alive() or self.stderr_thread.is_alive():
            raise ExchangeInvalid("analytics_exchange_process_unjoined")
