"""Bounded release metadata; distribution signatures do not qualify engines."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.nlp.adapter import ProfileCompatibility
from app.nlp.contracts import Contract, Digest, Identifier
from app.nlp.profile_authentication import _keys
from app.security import signed_objects

CATALOG_TYPE = 'nlp-release-catalog.v1+jws'
CATALOG_BYTES = 1024 * 1024
CATALOG_ENVELOPE_BYTES = (CATALOG_BYTES * 4 + 2) // 3 + 1024
MAX_PROFILES = 32
MAX_WITHDRAWALS = 4096


class DependencyRejected(ValueError):
    def __init__(self, code='nlp_dependency_rejected'):
        super().__init__(code)


def require(value, code='nlp_dependency_rejected'):
    if not value:
        raise DependencyRejected(code)


def canonical(value):
    if isinstance(value, Contract):
        value = value.model_dump(mode='json')
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


class ReleaseEntry(Contract):
    profile_id: Identifier
    revision: Identifier
    profile_sha256: Digest
    compatibility: ProfileCompatibility


class ReleaseCatalog(Contract):
    schema_version: Literal['nlp-release-catalog.v1'] = 'nlp-release-catalog.v1'
    sequence: Annotated[int, Field(ge=1, le=2**53 - 1)]
    issued_at: Annotated[int, Field(ge=0, le=2**53 - 1)]
    expires_at: Annotated[int, Field(ge=1, le=2**53 - 1)]
    profiles: Annotated[tuple[ReleaseEntry, ...], Field(max_length=MAX_PROFILES)]
    withdrawals: Annotated[tuple[Digest, ...], Field(max_length=MAX_WITHDRAWALS)]

    @model_validator(mode='after')
    def closed(self):
        require(self.expires_at > self.issued_at)
        require(len({(p.profile_id, p.revision) for p in self.profiles}) == len(self.profiles))
        require(len({p.profile_sha256 for p in self.profiles}) == len(self.profiles))
        require(len(set(self.withdrawals)) == len(self.withdrawals))
        for entry in self.profiles:
            require(entry.compatibility.profile_sha256 == entry.profile_sha256)
        return self


def verify_catalog(raw, keys):
    try:
        require(type(raw) is bytes and len(raw) <= CATALOG_ENVELOPE_BYTES)

        def payload(body):
            signed_objects.parse(body, maximum=CATALOG_BYTES, canonicalizer=canonical)
            return ReleaseCatalog.model_validate_json(body)

        result = signed_objects.verify_compact(raw.decode('ascii'), typ=CATALOG_TYPE,
            keys=_keys(keys), maximum=CATALOG_BYTES, envelope_limit=CATALOG_ENVELOPE_BYTES,
            header_canonicalizer=canonical, payload_validator=payload)
        return result.payload, hashlib.sha256(canonical(result.payload)).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise DependencyRejected() from None
