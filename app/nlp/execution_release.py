"""Execution metadata for an explicitly selected qualification package."""
from dataclasses import dataclass, field
import hashlib
import re

from app.nlp.contracts import NlpBoundaryError
from app.nlp.profile_authentication import _keys
from app.security.signed_objects import encode, public_key, verify_compact
from app.security.text_analysis_authority import TextAnalysisKey, TextAnalysisTrust, fixture_trust
from app.security.text_analysis_contract import (
    SUPPORTED, TextAnalysisSchemas, canonical, parse_json, release_identity,
)

RELEASE_TYPE = 'text-analysis-desktop-release.v1+jws'
MAX_ENVELOPE = 24576


def _need(ok):
    if not ok:
        raise NlpBoundaryError('nlp_execution_release_invalid')


@dataclass(frozen=True, slots=True)
class ExecutionRelease:
    profile_sha256: str
    desktop_sha256: str
    package_binding: bytes
    release_pins: bytes
    manifest_keys: bytes = field(repr=False)
    trust: TextAnalysisTrust = field(repr=False)

    def _signed_manifest(self, envelope: bytes, schemas: TextAnalysisSchemas) -> dict:
        _need(type(envelope) is bytes and 0 < len(envelope) <= MAX_ENVELOPE)

        def validate(body):
            v = parse_json(body, maximum=16384, require_canonical=True)
            _need(type(v) is dict and set(v) == {'package','profile_sha256','supported_profiles','release_identity'})
            package = v['package']
            current_package = parse_json(self.package_binding, maximum=8192)
            _need(type(package) is dict and set(package) == set(current_package)
                  and all(package[k] == current_package[k] for k in ('name', 'publisher', 'family', 'architecture'))
                  and isinstance(package['desktop_sha256'], str) and re.fullmatch('[0-9a-f]{64}', package['desktop_sha256'])
                  and isinstance(v['profile_sha256'], str) and re.fullmatch('[0-9a-f]{64}', v['profile_sha256'])
                  and v['supported_profiles'] == list(SUPPORTED))
            identity = v['release_identity']
            _need(type(identity) is dict and identity.get('release_version') == package['version']
                  and identity.get('platform') == 'windows' and identity.get('architecture') == 'x86_64')
            release_identity(canonical(identity), schemas=schemas, expected=identity)
            return v

        return verify_compact(envelope.decode('ascii'), typ=RELEASE_TYPE,
            keys=parse_json(self.manifest_keys, maximum=8192), maximum=16384,
            envelope_limit=MAX_ENVELOPE, header_canonicalizer=canonical, payload_validator=validate).payload

    def verify_identity(self, envelope: bytes, schemas: TextAnalysisSchemas) -> bytes:
        manifest = self._signed_manifest(envelope, schemas)
        identity = manifest['release_identity']
        pins = {k: value for k, value in identity.items() if k != 'artifact_sha256'}
        _need(canonical(manifest['package']) == self.package_binding
              and manifest['profile_sha256'] == self.profile_sha256 and canonical(pins) == self.release_pins)
        return canonical(identity)

    def verify_retained_identity(self, envelope: bytes, schemas: TextAnalysisSchemas, *, desktop_sha256: str) -> bytes:
        """Authenticate the original release of a retained operation on the same package family."""
        if desktop_sha256 == self.desktop_sha256:
            return self.verify_identity(envelope, schemas)
        manifest = self._signed_manifest(envelope, schemas)
        _need(manifest['package']['desktop_sha256'] == desktop_sha256)
        return canonical(manifest['release_identity'])


def parse_execution_release(value, profiles, package_keys, desktop_sha256, release):
    _need(type(value) is dict and set(value) == {'profile_sha256','contract_manifest_sha256',
        'vector_manifest_sha256','trust_set_sha256','authority_projection','authority_projection_sha256','release_manifest_key'})
    _need(list(profiles) == [value['profile_sha256']])
    for key in ('contract_manifest_sha256','vector_manifest_sha256','trust_set_sha256','authority_projection_sha256'):
        _need(type(value[key]) is str and re.fullmatch('[a-f0-9]{64}',value[key]))
    _need(type(value['authority_projection']) is str)
    raw = value['authority_projection'].encode('utf-8')
    _need(hashlib.sha256(raw).hexdigest() == value['authority_projection_sha256'] == value['trust_set_sha256'])
    projection = parse_json(raw, maximum=8192)
    _need(type(projection) is dict and set(projection)=={'keys','environment','production_usable'}
          and projection['environment']=='private-qa' and projection['production_usable'] is False
          and type(projection['keys']) is list and len(projection['keys']) == 2)
    keys = {}
    excluded = {public_key(k['jwk']).public_numbers() for k in package_keys}
    excluded.update(k.public_key.public_numbers() for k in fixture_trust(environment='test').keys.values())
    for item in projection['keys']:
        _need(type(item) is dict and set(item)=={'purpose','jwk','thumbprint'} and type(item['jwk']) is dict)
        jwk = dict(item['jwk'])
        kid = jwk.pop('kid', None)
        point = public_key(jwk)
        _need(encode(hashlib.sha256(canonical(jwk)).digest()) == item['thumbprint']
              and point.public_numbers() not in excluded)
        keys[kid] = TextAnalysisKey(item['purpose'],point)
        excluded.add(point.public_numbers())
    _need({k.purpose for k in keys.values()} == {'text-analysis-license','text-analysis-preview'})
    trust = TextAnalysisTrust(keys,fixture_only=False)
    manifest_keys = _keys([value['release_manifest_key']])
    _need(public_key(manifest_keys[0]['jwk']).public_numbers() not in excluded)
    identity = release.identity
    package = dict(name=identity.name,family=identity.family,version=identity.version,publisher=identity.publisher,
                   architecture=identity.architecture,desktop_sha256=desktop_sha256)
    pins = dict(profile='urn:bridge-clean:text-analysis-release-identity:v1',product_id='bridge-clean',
                platform='windows',architecture='x86_64',release_version=identity.version,
                **{k:value[k] for k in ('contract_manifest_sha256','vector_manifest_sha256','trust_set_sha256')})
    return ExecutionRelease(profiles[0],desktop_sha256,canonical(package),canonical(pins),canonical(manifest_keys),trust)
