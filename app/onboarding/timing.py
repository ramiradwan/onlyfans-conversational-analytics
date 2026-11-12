"""In-memory sign-in close timing. One log line per close, with deltas only and no identifiers."""
from __future__ import annotations

import logging
import secrets
import time

logger = logging.getLogger(__name__)
POINTS = ("frame", "reconciled", "completed", "sent")
_LIMIT = 64
_marks: dict[str, dict[str, float]] = {}


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
