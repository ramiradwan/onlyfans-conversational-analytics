"""Original-byte identities for text-analysis authorities."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Literal, Mapping
from types import MappingProxyType

from app.security.signed_objects import decode
from app.security.text_analysis_authority import VerifiedTextAnalysisAuthority
from app.security.text_analysis_contract import canonical, parse_json

PROFILE_SHA256 = MappingProxyType({
    'license': '534e6c99b04ee22faaf665f504a9c354a3cb2de76e1be71c5e9d2e5ffa41a7b6',
    'preview': '3b65a56289fdad76847435765449760a3a6b04849151250f7a76c8c8f25452bc',
})


@dataclass(frozen=True, slots=True)
class _Reference:
    authority: VerifiedTextAnalysisAuthority = field(repr=False)

    def __post_init__(self):
        if type(self.authority) is not VerifiedTextAnalysisAuthority:
            raise ValueError('invalid_text_analysis_reference')
        expected = 'license' if type(self) is TextAnalysisLicenseReference else 'preview'
        if self.authority.kind != expected or type(self.authority.fixture_only) is not bool:
            raise ValueError('invalid_text_analysis_reference')
        object.__setattr__(self, 'authority', VerifiedTextAnalysisAuthority(
            self.authority.kind, self.authority.compact_jws, self.authority.payload_sha256,
            _freeze(self.authority.payload), self.authority.fixture_only))
        header, payload, _ = self.compact_jws.split('.')
        document = parse_json(decode(payload, 16384), maximum=16384, require_canonical=True)
        if canonical(document) != canonical(_plain(self.claims)):
            raise ValueError('reference_payload_mismatch')
        if hashlib.sha256(decode(payload, 16384)).hexdigest() != self.payload_sha256:
            raise ValueError('reference_payload_digest')
        object.__setattr__(self, 'authority', VerifiedTextAnalysisAuthority(
            self.authority.kind, self.authority.compact_jws, self.authority.payload_sha256,
            _freeze(document), self.authority.fixture_only))
        parse_json(decode(header, 1024), maximum=1024, require_canonical=True)

    @property
    def kind(self) -> Literal['license', 'preview']:
        return self.authority.kind

    @property
    def claims(self) -> Mapping:
        return self.authority.payload

    @property
    def compact_jws(self) -> str:
        return self.authority.compact_jws

    @property
    def compact_jws_sha256(self) -> str:
        return hashlib.sha256(self.compact_jws.encode('ascii')).hexdigest()

    @property
    def payload_sha256(self) -> str:
        return self.authority.payload_sha256

    @property
    def reference_id(self) -> str:
        return f'text-analysis-{self.kind}.v1.{self.compact_jws_sha256}'

    @property
    def object_id(self) -> str:
        return self.claims['license_id' if self.kind == 'license' else 'permit_id']

    @property
    def issuance_id(self) -> str:
        return self.claims['issuance_id']

    @property
    def signer_kid(self) -> str:
        return parse_json(decode(self.compact_jws.split('.')[0], 1024), maximum=1024)['kid']

    @property
    def fixture_only(self) -> bool:
        return self.authority.fixture_only


@dataclass(frozen=True, slots=True)
class TextAnalysisLicenseReference(_Reference):
    pass


@dataclass(frozen=True, slots=True)
class TextAnalysisPreviewReference(_Reference):
    pass


TextAnalysisReference = TextAnalysisLicenseReference | TextAnalysisPreviewReference


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def reference_from_verified(authority: VerifiedTextAnalysisAuthority) -> TextAnalysisReference:
    if authority.kind == 'license':
        return TextAnalysisLicenseReference(authority)
    if authority.kind == 'preview':
        return TextAnalysisPreviewReference(authority)
    raise ValueError('invalid_text_analysis_kind')
