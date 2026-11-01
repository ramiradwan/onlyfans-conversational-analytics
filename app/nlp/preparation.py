"""Closed release admission for installed preparation without analysis authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit

from app.core.desktop_release import DesktopRelease, RelatedBundle, parse_desktop_release
from app.nlp.contracts import NlpBoundaryError
from app.nlp.profile_authentication import _keys
from app.nlp.profiles import EngineIdentity, ResourcePolicy, validate_resource_policy


def _require(value):
    if not value:
        raise NlpBoundaryError('nlp_preparation_release_invalid')


@dataclass(frozen=True, slots=True)
class PreparationAdmission:
    engine: EngineIdentity
    profiles: tuple[str, ...]
    distribution_base_url: str
    minimum_free_bytes: int
    _keys_json: bytes
    _policy_json: bytes

    @property
    def trusted_keys(self):
        return tuple(json.loads(self._keys_json))

    @property
    def resource_policy(self):
        return ResourcePolicy.model_validate_json(self._policy_json)

    def require(self, profile, policy):
        _require(type(policy) is ResourcePolicy and policy == self.resource_policy)
        _require(profile.identity_digest() in self.profiles
                 and all(c.engine == self.engine for c in profile.components))
        validate_resource_policy(profile, policy)

    def approves(self, profile):
        try:
            self.require(profile, self.resource_policy)
            return True
        except (ValueError, TypeError, AttributeError):
            return False


def parse_preparation_release(raw: bytes, desktop_raw: bytes, release: DesktopRelease) -> PreparationAdmission:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= 65536)
        _require(type(desktop_raw) is bytes and 0 < len(desktop_raw) <= 65536)
        _require(type(release) is DesktopRelease and release.purpose == 'qualification'
                 and release.update_owner == 'direct-appinstaller'
                 and isinstance(release.servicing.installed_artifact, RelatedBundle))
        _require(parse_desktop_release(desktop_raw, release.identity) == release)
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: _require(False))
        _require(type(value) is dict and set(value) == {'schema', 'desktop_release_sha256',
            'engine', 'profiles', 'distribution_base_url', 'trusted_keys', 'resource_policy', 'minimum_free_bytes'})
        _require(value['schema'] == 'nlp-preparation-release.v1'
                 and value['desktop_release_sha256'] == hashlib.sha256(desktop_raw).hexdigest())
        _require(type(value['engine']) is dict and set(value['engine']) == set(EngineIdentity.model_fields))
        engine = EngineIdentity.model_validate_json(json.dumps(value['engine']))
        related = release.servicing.installed_artifact
        _require(engine.package_name == related.engine.identity.name
                 and engine.package_family == related.engine.identity.family
                 and engine.publisher == related.engine.identity.publisher
                 and engine.package_version == related.engine.package_version
                 and engine.core_package_name == release.identity.name
                 and engine.core_package_version == release.identity.version
                 and engine.related_set_id == related.related_set_id
                 and release.identity.architecture == related.engine.identity.architecture == 'x64')
        profiles = value['profiles']
        _require(type(profiles) is list and 1 <= len(profiles) <= 8
                 and all(type(p) is str and re.fullmatch('[a-f0-9]{64}', p) for p in profiles)
                 and profiles == sorted(set(profiles)))
        base = value['distribution_base_url']
        _require(type(base) is str and len(base) <= 2048)
        parsed = urlsplit(base)
        _require(parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                 and parsed.password is None and parsed.port in (None, 443)
                 and not parsed.query and not parsed.fragment and base.endswith('/')
                 and chr(92) not in base and not any(ord(c) <= 32 for c in base))
        keys = _keys(value['trusted_keys'])
        policy = ResourcePolicy.model_validate_json(json.dumps(value['resource_policy']))
        reserve = value['minimum_free_bytes']
        _require(type(reserve) is int and 1 <= reserve <= 2**53 - 1)
        return PreparationAdmission(engine, tuple(profiles), base, reserve,
            json.dumps(keys, sort_keys=True).encode(), policy.model_dump_json().encode())
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise NlpBoundaryError('nlp_preparation_release_invalid') from None
