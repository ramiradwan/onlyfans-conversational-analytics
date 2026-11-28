"""Package-owned admission for local preparation and optional execution selection."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import json
import os
import re
import stat
import sys
from urllib.parse import urlsplit

from app.core.desktop_release import DesktopRelease, RelatedBundle, current_desktop_release, parse_desktop_release
from app.nlp.contracts import NlpBoundaryError
from app.nlp.profile_authentication import _keys
from app.nlp.profiles import EngineIdentity, ResourcePolicy, validate_resource_policy

if TYPE_CHECKING:
    from app.nlp.execution_release import ExecutionRelease


def _require(value):
    if not value:
        raise NlpBoundaryError('nlp_preparation_release_invalid')


def load_preparation_release():
    if os.name != 'nt' or not getattr(sys, 'frozen', False) or Path(sys.executable).name.lower() != 'brain.exe':
        return None
    from app.persistence.private_files import reject_path_aliases
    root = reject_path_aliases(Path(sys.executable).parent)
    path = root / 'nlp-preparation-release.json'
    if not path.exists():
        return None
    from app.nlp.retained_artifacts import _windows_handle
    # Package identity and retained file handles exclude mutable runtime inputs.
    with ExitStack() as stack:
        for ancestor in reversed((root, *root.parents)):
            stack.enter_context(_windows_handle(ancestor, directory=True))
        for leaf in (path, root / 'desktop-release.json'):
            reject_path_aliases(leaf)
            stack.enter_context(_windows_handle(leaf, directory=False))
            before = leaf.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536:
                raise NlpBoundaryError('nlp_preparation_release_invalid')
        release = current_desktop_release()
        def read(leaf):
            before = leaf.lstat()
            with leaf.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                raw = stream.read(65537)
                after = os.fstat(stream.fileno())
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
            if (identity(before) != identity(opened) or identity(opened) != identity(after)
                    or identity(after) != identity(leaf.lstat()) or len(raw) != before.st_size):
                raise NlpBoundaryError('nlp_preparation_release_invalid')
            return raw
        return parse_preparation_release(read(path), read(root / 'desktop-release.json'), release)


@dataclass(frozen=True, slots=True)
class PreparationAdmission:
    engine: EngineIdentity
    profiles: tuple[str, ...]
    distribution_base_url: str
    minimum_free_bytes: int
    _keys_json: bytes
    _policy_json: bytes
    execution: ExecutionRelease | None = None

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
        if self.execution is not None:
            _require(profile.schema_version == 'nlp-profile.v3'
                     and [binding.task for binding in profile.bindings] == ['concept_mentions'])
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
        fields = {'schema', 'desktop_release_sha256', 'engine', 'profiles', 'distribution_base_url',
                  'trusted_keys', 'resource_policy', 'minimum_free_bytes'}
        _require(type(value) is dict and value.get('schema') in ('nlp-preparation-release.v1','nlp-preparation-release.v2'))
        with_execution = value['schema'] == 'nlp-preparation-release.v2'
        _require(set(value) == fields | ({'execution'} if with_execution else set()))
        _require(value['desktop_release_sha256'] == hashlib.sha256(desktop_raw).hexdigest())
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
        execution = None
        if with_execution:
            from app.nlp.execution_release import parse_execution_release
            execution = parse_execution_release(value['execution'], profiles, keys, value['desktop_release_sha256'], release)
        return PreparationAdmission(engine, tuple(profiles), base, reserve,
            json.dumps(keys, sort_keys=True).encode(), policy.model_dump_json().encode(), execution)
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise NlpBoundaryError('nlp_preparation_release_invalid') from None
