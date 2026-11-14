"""In-memory sign-in close timing. One log line per close, with deltas only and no identifiers."""
from __future__ import annotations

import logging
import math
import secrets
import time
from contextlib import contextmanager
from functools import wraps

logger = logging.getLogger(__name__)
POINTS = ("frame", "reconciled", "completed", "sent")
_LIMIT = 64
_marks: dict[str, dict[str, float]] = {}
_completed: dict[str, float] = {}
OPERATIONS = frozenset({"frontend", "command", "worker", "source", "binding", "effect", "key",
    "ceremony-prepare", "ceremony-read", "ceremony-recover", "ceremony-cancel",
    "authentication-read", "authentication-select", "authentication-cancel", "authentication-switch",
    "decision-read", "decision-submit", "wait", "helper", "paired", "extension"})
PHASES = frozenset({"click", "command-send", "accept", "entry", "prepare", "binding", "queue",
    "run", "reopen", "usability", "sign", "challenge", "protected", "frame", "read-end",
    "helper-finish", "task-send", "popup-remove", "challenge-send", "challenge-receive",
    "protected-send", "protected-receive"})


@contextmanager
def exchange(operation, kind):
    observe(operation, kind + "-send", "ok", 0)
    with phase(operation, kind + "-receive"):
        yield


def observe(operation, phase, status, elapsed_ms):
    if (operation not in OPERATIONS or phase not in PHASES or status not in {"ok", "error", "cancelled"}
            or type(elapsed_ms) not in {int, float} or not math.isfinite(elapsed_ms)
            or not 0 <= elapsed_ms <= 3_600_000):
        raise ValueError("Invalid sign-in timing")
    logger.info("event=sign_in_phase operation=%s phase=%s status=%s elapsed_ms=%s",
        operation, phase, status, round(elapsed_ms))


@contextmanager
def phase(operation, point, *, clock=time.monotonic):
    if operation not in OPERATIONS or point not in PHASES:
        raise ValueError("Unknown sign-in phase")
    started, status = clock(), "ok"
    try:
        yield
    except BaseException as error:
        status = "cancelled" if type(error).__name__ == "CancelledError" else "error"
        raise
    finally:
        observe(operation, point, status, max(0, (clock() - started) * 1000))


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


def completed(key, *, clock=time.monotonic):
    if len(_completed) >= _LIMIT:
        _completed.pop(next(iter(_completed)))
    _completed.setdefault(key, clock())
    mark(key, "completed", clock=clock)


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
    return record


def reset() -> None:
    _marks.clear()
    _completed.clear()
