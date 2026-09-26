"""Check stored projection records without retaining their complete model arrays."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Annotated, Callable

from pydantic import BaseModel, TypeAdapter, create_model

from app.analytics.projection_encoding import projection_digest
from app.models.analytics import (AnalyticsModel, AnalyticsProjection,
                                  ConversationMetrics, MessageEnrichment)

_ARRAYS = {'conversation_metrics': ConversationMetrics, 'message_enrichments': MessageEnrichment}
_FIELDS = tuple(sorted(AnalyticsProjection.model_fields))
_HEADER_FIELDS = {name: field for name, field in AnalyticsProjection.model_fields.items()
                  if name not in _ARRAYS}
ProjectionHeader = create_model('ProjectionVerificationHeader', __base__=AnalyticsModel,
    **{name: (field.annotation, deepcopy(field)) for name, field in _HEADER_FIELDS.items()})
_ADAPTERS = {name: TypeAdapter(Annotated[field.annotation, *field.metadata]
                             if field.metadata else field.annotation,
                             config=None if isinstance(field.annotation, type)
                             and issubclass(field.annotation, BaseModel)
                             else {'hide_input_in_errors': True})
             for name, field in _HEADER_FIELDS.items()}
_SPACE = json.decoder.WHITESPACE.match


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('projection_duplicate_key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('projection_nonfinite_literal')


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


class _GeneralDocument(Exception):
    """The document needs the public model's ordering or default-field handling."""


@dataclass(frozen=True, slots=True)
class VerifiedProjection:
    header: BaseModel
    digest: str | None
    message_count: int
    conversation_count: int
    first_source_at: datetime | None
    streamed: bool
    conversation_metrics_digest: str | None = None


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _array(text: str, position: int, model, digest, check, *, source_times=False,
           observe=None):
    if text[position:position + 1] != '[':
        raise ValueError('projection_array_required')
    position = _SPACE(text, position + 1).end()
    if digest is not None:
        digest.update(b'[')
    count, first_source = 0, None
    if text[position:position + 1] != ']':
        while True:
            check()
            value, position = _DECODER.raw_decode(text, position)
            item = model.model_validate(value)
            encoded = _json(item.model_dump(mode='json'))
            if digest is not None:
                if count:
                    digest.update(b',')
                digest.update(encoded)
            if observe is not None:
                observe(item, encoded, count)
            count += 1
            if source_times and (first_source is None or item.sent_at < first_source):
                first_source = item.sent_at
            del item, value, encoded
            position = _SPACE(text, position).end()
            if text[position:position + 1] == ']':
                break
            if text[position:position + 1] != ',':
                raise ValueError('projection_array_separator_invalid')
            position = _SPACE(text, position + 1).end()
    check()
    if digest is not None:
        digest.update(b']')
    return position + 1, count, first_source


def _component_tuples(order, components):
    return tuple(
        (reference, components[reference][0], components[reference][1].hexdigest())
        for reference in order
    )


def _stream(text: str, check, *, with_digest: bool,
            enrichment_components=None) -> VerifiedProjection:
    from app.analytics.projection_encoding import ENRICHMENT_UNIT_PIPELINE_REVISION

    position = _SPACE(text, 0).end()
    if text[position:position + 1] != '{':
        raise ValueError('projection_object_required')
    position = _SPACE(text, position + 1).end()
    legacy = hashlib.sha256(b'{') if with_digest else None
    included = 0
    header, counts, first_source = {}, {}, None
    component_order, components = [], {}
    metrics_digest = hashlib.sha256()
    metrics_count = 0

    def observe_message(item, encoded, _ordinal):
        reference = item.conversation_ref
        state = components.get(reference)
        if state is None:
            state = [0, hashlib.sha256()]
            components[reference] = state
            component_order.append(reference)
        if state[0]:
            state[1].update(b'\n')
        state[1].update(encoded)
        state[0] += 1

    def observe_metric(_item, encoded, ordinal):
        nonlocal metrics_count
        if ordinal:
            metrics_digest.update(b'\n')
        metrics_digest.update(encoded)
        metrics_count += 1

    for ordinal, expected in enumerate(_FIELDS):
        check()
        if text[position:position + 1] == '}':
            raise _GeneralDocument()
        name, position = _DECODER.raw_decode(text, position)
        if name != expected:
            raise _GeneralDocument()
        position = _SPACE(text, position).end()
        if text[position:position + 1] != ':':
            raise ValueError('projection_colon_required')
        position = _SPACE(text, position + 1).end()

        if name != 'projection_digest' and legacy is not None:
            if included:
                legacy.update(b',')
            legacy.update(_json(name) + b':')
            included += 1

        if name in _ARRAYS:
            position, counts[name], source = _array(
                text, position, _ARRAYS[name], legacy, check,
                source_times=name == 'message_enrichments',
                observe=(
                    observe_message if name == 'message_enrichments'
                    else observe_metric
                ),
            )
            if source is not None:
                first_source = source
        else:
            value, position = _DECODER.raw_decode(text, position)
            value = _ADAPTERS[name].validate_python(value)
            header[name] = value
            if name != 'projection_digest' and legacy is not None:
                legacy.update(_json(_ADAPTERS[name].dump_python(value, mode='json')))

        position = _SPACE(text, position).end()
        if ordinal + 1 < len(_FIELDS):
            if text[position:position + 1] == '}':
                raise _GeneralDocument()
            if text[position:position + 1] != ',':
                raise ValueError('projection_separator_invalid')
            position = _SPACE(text, position + 1).end()

    if text[position:position + 1] != '}' or _SPACE(text, position + 1).end() != len(text):
        raise ValueError('projection_document_end_invalid')
    check()
    if legacy is not None:
        legacy.update(b'}')

    actual_components = _component_tuples(component_order, components)
    selected_components = actual_components
    if enrichment_components is not None:
        supplied = tuple(enrichment_components)
        if counts['message_enrichments'] and supplied != actual_components:
            raise ValueError('projection_enrichment_components_mismatch')
        selected_components = supplied

    verified_header = ProjectionHeader.model_validate(header)
    digest = None
    if with_digest:
        if ENRICHMENT_UNIT_PIPELINE_REVISION in verified_header.pipeline_revision:
            unit = hashlib.sha256(
                b'analytics-projection.enrichment-units.v1\0'
            )
            unit.update(_json(verified_header.model_dump(
                mode='json', exclude={'projection_digest'}
            )))
            unit.update(b'\0conversation_metrics:')
            unit.update(str(metrics_count).encode('ascii'))
            unit.update(b':')
            unit.update(metrics_digest.hexdigest().encode('ascii'))
            for conversation_ref, count, content_digest in selected_components:
                check()
                unit.update(b'\0message_enrichment:')
                unit.update(conversation_ref.encode('ascii'))
                unit.update(b':')
                unit.update(str(int(count)).encode('ascii'))
                unit.update(b':')
                unit.update(content_digest.encode('ascii'))
            digest = 'sha256:' + unit.hexdigest()
        else:
            digest = 'sha256:' + legacy.hexdigest()

    return VerifiedProjection(
        verified_header, digest, counts['message_enrichments'],
        counts['conversation_metrics'], first_source, True,
        metrics_digest.hexdigest(),
    )

def _metrics_digest_from_projection(projection):
    digest = hashlib.sha256()
    for ordinal, item in enumerate(projection.conversation_metrics):
        if ordinal:
            digest.update(b'\n')
        digest.update(_json(item.model_dump(mode='json')))
    return digest.hexdigest()


def _read_document(document: str, *, with_digest: bool,
                   check: Callable[[], None], enrichment_components=None) -> VerifiedProjection:
    """Verify every record; return metadata rather than arrays that callers do not use."""

    check()
    if not isinstance(document, str):
        raise ValueError('projection_document_text_required')
    try:
        return _stream(
            document, check, with_digest=with_digest,
            enrichment_components=enrichment_components,
        )
    except _GeneralDocument:
        # Public inputs may omit defaults or use a different top-level order.
        # Their established model validation remains the compatibility path.
        check()
        projection = AnalyticsProjection.model_validate_json(document)
        from app.analytics.projection_encoding import (
            enrichment_digest_components, projection_digest_from_components,
            ENRICHMENT_UNIT_PIPELINE_REVISION,
        )
        actual_components = enrichment_digest_components(
            projection.message_enrichments, check=check
        )
        selected = actual_components
        if enrichment_components is not None:
            supplied = tuple(enrichment_components)
            if projection.message_enrichments and supplied != actual_components:
                raise ValueError('projection_enrichment_components_mismatch')
            selected = supplied
        if with_digest:
            digest = (
                projection_digest_from_components(projection, selected, check=check)
                if ENRICHMENT_UNIT_PIPELINE_REVISION in projection.pipeline_revision
                else projection_digest(projection, check=check)
            )
        else:
            digest = None
        header = projection.model_dump(mode='python', exclude=set(_ARRAYS))
        result = VerifiedProjection(
            ProjectionHeader.model_validate(header), digest,
            len(projection.message_enrichments), len(projection.conversation_metrics),
            min((item.sent_at for item in projection.message_enrichments), default=None),
            False, _metrics_digest_from_projection(projection),
        )
        check()
        return result


def verify_projection_document(document: str, *,
                               check: Callable[[], None] = lambda: None,
                               enrichment_components=None) -> VerifiedProjection:
    """Validate every stored record and calculate its versioned projection digest."""

    result = _read_document(
        document, with_digest=True, check=check,
        enrichment_components=enrichment_components,
    )
    assert result.digest is not None
    return result

def read_projection_source_time(document: str, *, check: Callable[[], None] = lambda: None) -> tuple[str, datetime | None]:
    """Validate a retention input without calculating a digest the caller does not use."""

    result = _read_document(document, with_digest=False, check=check)
    first = result.first_source_at
    if first is None:
        from app.analytics.projection_encoding import ENRICHMENT_UNIT_PIPELINE_REVISION
        if ENRICHMENT_UNIT_PIPELINE_REVISION in result.header.pipeline_revision:
            first = result.header.creator_metrics.active_from
    return result.header.pipeline_revision, first
