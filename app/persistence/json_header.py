"""Validate JSON objects without retaining selected top-level array contents."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
from typing import Callable, Iterator

_CHECK: ContextVar[Callable[[], None] | None] = ContextVar("json_header_check", default=None)
_SPACE = json.decoder.WHITESPACE.match


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("json_duplicate_key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("json_nonfinite_literal")


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


@lru_cache(maxsize=8)
def _omitted(fields: str) -> frozenset[str]:
    values = _DECODER.decode(fields)
    if not isinstance(values, list) or not all(isinstance(key, str) for key in values):
        raise ValueError("json_header_fields_invalid")
    return frozenset(values)


def _array_end(text: str, position: int, check: Callable[[], None]) -> int:
    if text[position:position + 1] != "[":
        raise ValueError("json_header_array_required")
    position = _SPACE(text, position + 1).end()
    if text[position:position + 1] == "]":
        return position + 1
    while True:
        check()
        _, position = _DECODER.raw_decode(text, position)
        position = _SPACE(text, position).end()
        marker = text[position:position + 1]
        if marker == "]":
            return position + 1
        if marker != ",":
            raise ValueError("json_array_separator_invalid")
        position = _SPACE(text, position + 1).end()


def _header(text: str, fields: frozenset[str], check: Callable[[], None]) -> str:
    position = _SPACE(text, 0).end()
    if text[position:position + 1] != "{":
        raise ValueError("json_object_required")
    position = _SPACE(text, position + 1).end()
    result: dict[str, object] = {}
    if text[position:position + 1] != "}":
        while True:
            check()
            key, position = _DECODER.raw_decode(text, position)
            if not isinstance(key, str) or key in result:
                raise ValueError("json_object_key_invalid")
            position = _SPACE(text, position).end()
            if text[position:position + 1] != ":":
                raise ValueError("json_object_colon_required")
            position = _SPACE(text, position + 1).end()
            if key in fields:
                position = _array_end(text, position, check)
                result[key] = []
            else:
                result[key], position = _DECODER.raw_decode(text, position)
            position = _SPACE(text, position).end()
            marker = text[position:position + 1]
            if marker == "}":
                break
            if marker != ",":
                raise ValueError("json_object_separator_invalid")
            position = _SPACE(text, position + 1).end()
    if text[position:position + 1] != "}" or _SPACE(text, position + 1).end() != len(text):
        raise ValueError("json_document_end_invalid")
    check()
    return json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)


def json_object_header(document: object, fields: object) -> str | None:
    """Return a header only after every array item has valid JSON syntax."""

    if not isinstance(document, (str, bytes)) or not isinstance(fields, str):
        return None
    try:
        text = document.decode("utf-8") if isinstance(document, bytes) else document
        return _header(text, _omitted(fields), _CHECK.get() or (lambda: None))
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


@contextmanager
def json_validation_scope(check: Callable[[], None]) -> Iterator[None]:
    """Propagate cancellation through a SQLite user-function invocation."""

    interrupted: list[Exception] = []
    def poll() -> None:
        try:
            check()
        except Exception as error:
            interrupted.append(error)
            raise
    token = _CHECK.set(poll)
    try:
        yield
    except Exception:
        if interrupted:
            raise interrupted[0] from None
        raise
    finally:
        _CHECK.reset(token)


def configure_json_functions(connection: object) -> None:
    connection.create_function(  # type: ignore[attr-defined]
        "ofca_json_header_v1", 2, json_object_header, deterministic=True,
    )
