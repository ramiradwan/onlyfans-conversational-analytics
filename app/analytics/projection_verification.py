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


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _array(text: str, position: int, model, digest, check, *, source_times=False):
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
            if digest is not None:
                if count:
                    digest.update(b',')
                digest.update(_json(item.model_dump(mode='json')))
            count += 1
            if source_times and (first_source is None or item.sent_at < first_source):
                first_source = item.sent_at
            del item, value
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


def _stream(text: str, check, *, with_digest: bool) -> VerifiedProjection:
    position = _SPACE(text, 0).end()
    if text[position:position + 1] != '{':
        raise ValueError('projection_object_required')
    position = _SPACE(text, position + 1).end()
    digest = hashlib.sha256(b'{') if with_digest else None
    included = 0
    header, counts, first_source = {}, {}, None
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
        if name != 'projection_digest' and digest is not None:
            if included:
                digest.update(b',')
            digest.update(_json(name) + b':')
            included += 1
        if name in _ARRAYS:
            position, counts[name], source = _array(text, position, _ARRAYS[name], digest, check,
                                                   source_times=name == 'message_enrichments')
            if source is not None:
                first_source = source
        else:
            value, position = _DECODER.raw_decode(text, position)
            value = _ADAPTERS[name].validate_python(value)
            header[name] = value
            if name != 'projection_digest' and digest is not None:
                digest.update(_json(_ADAPTERS[name].dump_python(value, mode='json')))
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
    if digest is not None:
        digest.update(b'}')
    return VerifiedProjection(ProjectionHeader.model_validate(header),
        'sha256:' + digest.hexdigest() if digest is not None else None, counts['message_enrichments'],
        counts['conversation_metrics'], first_source, True)


def _read_document(document: str, *, with_digest: bool,
                   check: Callable[[], None]) -> VerifiedProjection:
    """Verify every record; return metadata rather than arrays that callers do not use."""

    check()
    if not isinstance(document, str):
        raise ValueError('projection_document_text_required')
    try:
        return _stream(document, check, with_digest=with_digest)
    except _GeneralDocument:
        # Public inputs may omit defaults or use a different top-level order.
        # Their established model validation remains the compatibility path.
        check()
        projection = AnalyticsProjection.model_validate_json(document)
        digest = projection_digest(projection, check=check) if with_digest else None
        header = projection.model_dump(mode='python', exclude=set(_ARRAYS))
        result = VerifiedProjection(ProjectionHeader.model_validate(header), digest,
            len(projection.message_enrichments), len(projection.conversation_metrics),
            min((item.sent_at for item in projection.message_enrichments), default=None), False)
        check()
        return result


def verify_projection_document(document: str, *,
                               check: Callable[[], None] = lambda: None) -> VerifiedProjection:
    """Validate every stored record and calculate its canonical projection digest."""

    result = _read_document(document, with_digest=True, check=check)
    assert result.digest is not None
    return result


def read_projection_source_time(document: str, *, check: Callable[[], None] = lambda: None) -> tuple[str, datetime | None]:
    """Validate a retention input without calculating a digest the caller does not use."""

    result = _read_document(document, with_digest=False, check=check)
    return result.header.pipeline_revision, result.first_source_at
