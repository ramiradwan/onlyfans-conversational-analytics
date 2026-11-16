"""Sign-in phase timing with closed labels and deltas only. No identifiers, URLs or credentials."""
from __future__ import annotations

import logging
import math
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)
POINTS = ("frame", "reconciled", "completed", "sent")
_LIMIT = 64
_marks: dict[str, dict[str, float]] = {}
_completed: dict[str, float] = {}
_ready: dict[str, float] = {}
OPERATIONS = frozenset({"frontend", "command", "worker", "source", "binding", "effect", "key",
    "ceremony-prepare", "ceremony-read", "ceremony-recover", "ceremony-cancel",
    "authentication-read", "authentication-select", "authentication-cancel", "authentication-switch",
    "decision-read", "decision-submit", "wait", "helper", "paired", "extension",
    "handoff-prepare", "handoff-complete", "handoff-receipt", "handoff-wait", "transport", "snapshot"})
PHASES = frozenset({"click", "command-send", "accept", "entry", "prepare", "binding", "queue",
    "run", "reopen", "usability", "sign", "challenge", "protected", "frame", "read-end",
    "helper-finish", "task-send", "popup-remove", "challenge-send", "challenge-receive",
    "protected-send", "protected-receive", "observe", "recover", "ensure", "connect", "tls",
    "read", "watch", "renew", "waiting", "reserve", "lookup", "create", "activate", "persist",
    "entry-open", "entry-pending", "pickup", "popup-create", "resume", "guard",
    "task", "task-found", "launch", "payload", "session-get", "payload-get", "payload-queue", "payload-handler",
    "stream-wake", "payload-ready", "attach-sent", "attach-received", "ahead-reused", "ahead-none", "ahead-age",
    "ahead-source", "ahead-changed-tab", "ahead-changed-storage", "ahead-changed-consent", "ahead-changed-account",
    "ahead-changed-authentication", "ahead-changed-other", "attach-done", "read-done", "authenticate-received",
    "open-guarded", "navigate-sent"})
# Event-loop work below this duration is not recorded.
SLOW_MS = 25
SINK_DIRECTORY = "diagnostics"
SINK_FILENAME = "sign-in-timing.log"
SINK_LIMIT_BYTES = 65_536
_sink_lock = threading.Lock()
_sink: Path | None = None


def record_to(root) -> None:
    """Also append records to a size-bounded file below an existing local data root."""
    global _sink
    with _sink_lock:
        _sink = None if root is None else Path(root) / SINK_DIRECTORY / SINK_FILENAME


def _append(line: str) -> None:
    with _sink_lock:
        path = _sink
        if path is None:
            return
        try:
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            data = f"{stamp} {line}\n".encode("ascii")
            if not path.parent.is_dir():
                path.parent.mkdir()
                from app.persistence.private_files import apply_private_directory_security
                apply_private_directory_security(path.parent)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size + len(data) > SINK_LIMIT_BYTES:
                os.replace(path, path.with_suffix(".1.log"))
            with open(path, "ab") as handle:
                handle.write(data)
        except Exception:
            # Timing is diagnostic only and never affects sign-in.
            pass


@contextmanager
def exchange(operation, kind):
    observe(operation, kind + "-send", "ok", 0)
    with phase(operation, kind + "-receive"):
        yield


def observe(operation, phase, status, elapsed_ms, *, http=None):
    """`http` is an optional HTTP status code, recorded as a number only."""
    if (operation not in OPERATIONS or phase not in PHASES or status not in {"ok", "error", "cancelled"}
            or type(elapsed_ms) not in {int, float} or not math.isfinite(elapsed_ms)
            or not 0 <= elapsed_ms <= 3_600_000
            or http is not None and (type(http) is not int or not 100 <= http <= 599)):
        raise ValueError("Invalid sign-in timing")
    suffix = "" if http is None else f" http={http}"
    logger.info("event=sign_in_phase operation=%s phase=%s status=%s elapsed_ms=%s%s",
        operation, phase, status, round(elapsed_ms), suffix)
    _append(f"sign_in_phase operation={operation} phase={phase} status={status} elapsed_ms={round(elapsed_ms)}{suffix}")


@contextmanager
def phase(operation, point, *, clock=time.monotonic, minimum_ms=0):
    """Record one phase; with minimum_ms, record only phases at least that long."""
    if operation not in OPERATIONS or point not in PHASES:
        raise ValueError("Unknown sign-in phase")
    started, status = clock(), "ok"
    try:
        yield
    except BaseException as error:
        status = "cancelled" if type(error).__name__ == "CancelledError" else "error"
        raise
    finally:
        elapsed = max(0, (clock() - started) * 1000)
        if elapsed >= minimum_ms or status != "ok":
            observe(operation, point, status, elapsed)


def measured(operation, point):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with phase(operation, point):
                return function(*args, **kwargs)
        return run
    return decorate


def observation(value, *, operation, phases, record=True):
    if (not isinstance(value, dict) or set(value) != {"operation", "phase", "status", "elapsed_ms"}
            or value["operation"] != operation or value["phase"] not in phases):
        raise ValueError("Invalid sign-in observation")
    if record:
        observe(value["operation"], value["phase"], value["status"], value["elapsed_ms"])
    elif (value["status"] not in {"ok", "error", "cancelled"} or type(value["elapsed_ms"]) not in {int, float}
            or not math.isfinite(value["elapsed_ms"]) or not 0 <= value["elapsed_ms"] <= 3_600_000):
        raise ValueError("Invalid sign-in observation")


def observations(values, *, operation, phases, limit):
    """Validate every mark of a batch before recording any of them."""
    if not isinstance(values, list) or not 0 < len(values) <= limit:
        raise ValueError("Invalid sign-in observation")
    for value in values:
        observation(value, operation=operation, phases=phases, record=False)
    for value in values:
        observe(value["operation"], value["phase"], value["status"], value["elapsed_ms"])


def completed(key, *, clock=time.monotonic):
    if len(_completed) >= _LIMIT:
        _completed.pop(next(iter(_completed)))
    _completed.setdefault(key, clock())
    mark(key, "completed", clock=clock)


def entry_ready(key, *, clock=time.monotonic):
    """Remember when a sign-in's browser step became open, until its helper first reads it."""
    if len(_ready) >= _LIMIT:
        _ready.pop(next(iter(_ready)))
    _ready.setdefault(key, clock())


def entry_served(key, *, clock=time.monotonic):
    started = _ready.pop(key, None)
    if started is not None:
        observe("helper", "pickup", "ok", max(0, (clock() - started) * 1000))


def helper_finish(key, *, clock=time.monotonic):
    started = _completed.pop(key, None)
    if started is not None:
        observe("helper", "helper-finish", "ok", max(0, (clock() - started) * 1000))


def mark(key: str, point: str, *, clock=time.monotonic) -> None:
    if point not in POINTS:
        raise ValueError("Unknown timing point")
    if point != "frame" and key not in _marks:
        return
    if point == "frame":
        _marks.pop(key, None)
        if len(_marks) >= _LIMIT:
            _marks.pop(next(iter(_marks)))
        _marks[key] = {}
    _marks[key].setdefault(point, clock())


def close(key: str, route: str, *, clock=time.monotonic) -> dict | None:
    """Record the push for a sign-in that began with a wait frame, then forget it."""
    if route not in {"helper", "paired"}:
        raise ValueError("Unknown close route")
    marks = _marks.pop(key, None)
    if marks is None or "frame" not in marks:
        return None
    marks.setdefault("sent", clock())
    if "completed" in marks:
        observe(route, "task-send", "ok", max(0, (marks["sent"] - marks["completed"]) * 1000))
    record = {"event": "sign_in_close_timing", "run": secrets.randbelow(1 << 30), "route": route}
    previous = "frame"
    for point in POINTS[1:]:
        if point in marks:
            record[f"{previous}_to_{point}_ms"] = round((marks[point] - marks[previous]) * 1000)
            previous = point
    logger.info("event=sign_in_close_timing %s", " ".join(f"{name}={value}" for name, value in record.items()
                                                          if name != "event"))
    _append("sign_in_close_timing " + " ".join(f"{name}={value}" for name, value in record.items()
                                             if name not in {"event", "run"}))
    return record


def reset() -> None:
    _marks.clear()
    _completed.clear()
    _ready.clear()
