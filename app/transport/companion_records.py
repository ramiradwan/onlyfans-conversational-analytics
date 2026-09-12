"""Bounded application framing inside authenticated Noise records.

Protocol-v2 documents keep their existing 512 KiB limit. Fragmentation is only
an application encoding; Snow owns authentication, encryption and ordering.
"""

from __future__ import annotations

import base64
import json
import math
import time
from uuid import UUID, uuid4

MAX_DOCUMENT_BYTES = 524_288
FRAGMENT_BYTES = 2_800
ASSEMBLY_SECONDS = 10.0


class CompanionRecordError(ValueError):
    def __init__(self) -> None:
        super().__init__("session_record_refused")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CompanionRecordError()
        result[key] = value
    return result


def _constant(value):
    raise CompanionRecordError()


def _validate(value, depth=0):
    if depth > 64 or (isinstance(value, float) and not math.isfinite(value)):
        raise CompanionRecordError()
    if type(value) is int:
        try:
            if not math.isfinite(float(value)):
                raise CompanionRecordError()
        except OverflowError:
            raise CompanionRecordError() from None
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, entry in value.items():
            if not isinstance(key, str):
                raise CompanionRecordError()
            key.encode("utf-8")
            _validate(entry, depth + 1)
    elif isinstance(value, list):
        for entry in value:
            _validate(entry, depth + 1)


def document(raw: bytes | str) -> dict:
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if (
            not isinstance(encoded, bytes)
            or not 1 <= len(encoded) <= MAX_DOCUMENT_BYTES
        ):
            raise CompanionRecordError()
        result = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant
        )
        if not isinstance(result, dict):
            raise CompanionRecordError()
        _validate(result)
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise CompanionRecordError() from None


def encode_document(value: dict) -> bytes:
    try:
        _validate(value)
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if not isinstance(value, dict) or not 1 <= len(raw) <= MAX_DOCUMENT_BYTES:
            raise CompanionRecordError()
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise CompanionRecordError() from None


def fragments(value: dict):
    raw = encode_document(value)
    identifier = str(uuid4())
    for index, offset in enumerate(range(0, len(raw), FRAGMENT_BYTES)):
        data = raw[offset : offset + FRAGMENT_BYTES]
        yield encode_document(
            {
                "type": "fragment",
                "id": identifier,
                "index": index,
                "final": offset + len(data) == len(raw),
                "data": base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii"),
            }
        )


class Assembly:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.identifier = None
        self.index = 0
        self.started = None
        self.buffer = bytearray()

    @property
    def remaining(self) -> float | None:
        return (
            None
            if self.started is None
            else ASSEMBLY_SECONDS - (self.clock() - self.started)
        )

    def clear(self):
        self.buffer.clear()
        self.identifier = self.started = None
        self.index = 0

    def feed(self, raw: bytes) -> dict | None:
        try:
            if not 1 <= len(raw) <= 4_079 or (
                self.remaining is not None and self.remaining <= 0
            ):
                raise CompanionRecordError()
            part = document(raw)
            if (
                set(part) != {"type", "id", "index", "final", "data"}
                or part["type"] != "fragment"
            ):
                raise CompanionRecordError()
            if (
                str(UUID(part["id"])) != part["id"]
                or type(part["index"]) is not int
                or type(part["final"]) is not bool
            ):
                raise CompanionRecordError()
            text = part["data"]
            if not isinstance(text, str) or not 1 <= len(text) <= 3_734:
                raise CompanionRecordError()
            data = base64.b64decode(
                text.encode("ascii") + b"=" * (-len(text) % 4),
                altchars=b"-_",
                validate=True,
            )
            if (
                base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii") != text
                or not 1 <= len(data) <= FRAGMENT_BYTES
            ):
                raise CompanionRecordError()
            if not part["final"] and len(data) != FRAGMENT_BYTES:
                raise CompanionRecordError()
            if self.identifier is None:
                self.identifier, self.started = part["id"], self.clock()
            if (
                part["id"] != self.identifier
                or part["index"] != self.index
                or len(self.buffer) + len(data) > MAX_DOCUMENT_BYTES
            ):
                raise CompanionRecordError()
            self.buffer.extend(data)
            self.index += 1
            if part["final"]:
                result = document(bytes(self.buffer))
                self.clear()
                return result
            return None
        except (ValueError, TypeError, AttributeError, UnicodeError):
            self.clear()
            raise CompanionRecordError() from None
