"""Canonical projection encoding without a second full message dictionary tree."""

from __future__ import annotations

import hashlib
from io import StringIO
from typing import Callable, Iterator

import json
from app.models.analytics import AnalyticsProjection

_ARRAY_FIELDS = frozenset({"message_enrichments", "conversation_metrics"})


def projection_chunks(projection: AnalyticsProjection, *, include_digest: bool = True,
                      check: Callable[[], None] = lambda: None) -> Iterator[str]:
    """Preserve the public canonical JSON format while encoding one row at a time."""

    excluded = set(_ARRAY_FIELDS)
    if not include_digest:
        excluded.add("projection_digest")
    header = projection.model_dump(mode="json", exclude=excluded)
    encode = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False)
    yield "{"
    for index, name in enumerate(sorted(set(header) | _ARRAY_FIELDS)):
        check()
        if index:
            yield ","
        yield encode(name) + ":"
        if name in _ARRAY_FIELDS:
            yield "["
            for ordinal, item in enumerate(getattr(projection, name)):
                check()
                if ordinal:
                    yield ","
                yield encode(item.model_dump(mode="json"))
            yield "]"
        else:
            yield encode(header[name])
    check()
    yield "}"


def projection_document(projection: AnalyticsProjection, *, check=lambda: None) -> str:
    with StringIO() as output:
        for chunk in projection_chunks(projection, check=check):
            output.write(chunk)
        return output.getvalue()


def projection_digest(projection: AnalyticsProjection, *, check=lambda: None) -> str:
    digest = hashlib.sha256()
    for chunk in projection_chunks(projection, include_digest=False, check=check):
        digest.update(chunk.encode("utf-8"))
    return "sha256:" + digest.hexdigest()
