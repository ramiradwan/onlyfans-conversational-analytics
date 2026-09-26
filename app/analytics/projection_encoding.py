"""Canonical projection encoding without a second full message dictionary tree."""

from __future__ import annotations

import hashlib
import json
from io import StringIO
from typing import Callable, Iterator

from app.models.analytics import AnalyticsProjection


_ARRAY_FIELDS = frozenset({"message_enrichments", "conversation_metrics"})
ENRICHMENT_UNIT_PIPELINE_REVISION = "enrichment.units.v1"
_UNIT_DIGEST_DOMAIN = b"analytics-projection.enrichment-units.v1\0"


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def projection_chunks(
    projection: AnalyticsProjection,
    *,
    include_digest: bool = True,
    check: Callable[[], None] = lambda: None,
) -> Iterator[str]:
    """Preserve the public canonical JSON format while encoding one row at a time."""

    excluded = set(_ARRAY_FIELDS)
    if not include_digest:
        excluded.add("projection_digest")
    header = projection.model_dump(mode="json", exclude=excluded)
    encode = lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    yield "{"
    for index, name in enumerate(sorted(set(header) | _ARRAY_FIELDS)):
        check()
        if index:
            yield ","
        yield encode(name) + ":"
        if name in _ARRAY_FIELDS:
            yield "["
            values = getattr(projection, name)
            raw_records = getattr(values, "iter_canonical_records", None)
            if name == "message_enrichments" and callable(raw_records):
                for ordinal, raw in enumerate(raw_records(check=check)):
                    check()
                    if ordinal:
                        yield ","
                    yield raw
            else:
                for ordinal, item in enumerate(values):
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


def enrichment_digest_components(values, *, check=lambda: None):
    """Return ordered conversation message digests without retaining full arrays."""

    components = getattr(values, "digest_components", None)
    if callable(components):
        return tuple(components())

    order = []
    groups = {}
    for item in values:
        check()
        reference = item.conversation_ref
        state = groups.get(reference)
        if state is None:
            state = [0, hashlib.sha256()]
            groups[reference] = state
            order.append(reference)
        if state[0]:
            state[1].update(b"\n")
        state[1].update(_canonical(item.model_dump(mode="json")))
        state[0] += 1
    return tuple(
        (reference, groups[reference][0], groups[reference][1].hexdigest())
        for reference in order
    )


def conversation_metrics_component(values, *, check=lambda: None):
    digest = hashlib.sha256()
    count = 0
    for item in values:
        check()
        if count:
            digest.update(b"\n")
        digest.update(_canonical(item.model_dump(mode="json")))
        count += 1
    return count, digest.hexdigest()


def projection_digest_from_components(
    projection: AnalyticsProjection, components, *, check=lambda: None
) -> str:
    """Versioned digest composable from conversation-local derived units."""

    header = projection.model_dump(
        mode="json",
        exclude={
            "projection_digest", "message_enrichments", "conversation_metrics"
        },
    )
    digest = hashlib.sha256(_UNIT_DIGEST_DOMAIN)
    digest.update(_canonical(header))
    metric_count, metric_digest = conversation_metrics_component(
        projection.conversation_metrics, check=check
    )
    digest.update(b"\0conversation_metrics:")
    digest.update(str(metric_count).encode("ascii"))
    digest.update(b":")
    digest.update(metric_digest.encode("ascii"))
    for conversation_ref, count, content_digest in components:
        check()
        digest.update(b"\0message_enrichment:")
        digest.update(conversation_ref.encode("ascii"))
        digest.update(b":")
        digest.update(str(int(count)).encode("ascii"))
        digest.update(b":")
        digest.update(content_digest.encode("ascii"))
    return "sha256:" + digest.hexdigest()


def projection_digest(projection: AnalyticsProjection, *, check=lambda: None) -> str:
    if ENRICHMENT_UNIT_PIPELINE_REVISION in projection.pipeline_revision:
        return projection_digest_from_components(
            projection,
            enrichment_digest_components(
                projection.message_enrichments, check=check
            ),
            check=check,
        )
    digest = hashlib.sha256()
    for chunk in projection_chunks(
        projection, include_digest=False, check=check
    ):
        digest.update(chunk.encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def projection_storage_document(
    projection: AnalyticsProjection,
    *,
    compact_enrichments: bool = False,
    check=lambda: None,
) -> str:
    """Store a compact v3 document when schema-17 units cover every message."""

    if (
        ENRICHMENT_UNIT_PIPELINE_REVISION in projection.pipeline_revision
        and compact_enrichments
    ):
        compact = projection.model_copy(update={"message_enrichments": []})
        return projection_document(compact, check=check)
    return projection_document(projection, check=check)
