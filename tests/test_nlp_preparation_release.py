"""Closed preparation metadata and immutable admission checks."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from app.core.desktop_release import PackageIdentity, parse_desktop_release
from app.nlp.contracts import NlpBoundaryError
from app.nlp.preparation import parse_preparation_release
from app.nlp.profiles import BASELINE_RESOURCE_POLICY, EngineIdentity
from tests.test_nlp_profile_authentication import keys, profile

pytestmark = pytest.mark.ci_tier('fast')


def fixture():
    raw = (Path(__file__).parents[1] / 'shared/desktop-release/qualification-direct-v2-related.json').read_bytes()
    value = json.loads(raw)
    identity = PackageIdentity(value['identity']['name'], value['identity']['publisher'],
        value['package_version'], 'x64', value['identity']['name'] + '_0123456789abc')
    release = parse_desktop_release(raw, identity)
    related = release.servicing.installed_artifact
    engine = EngineIdentity(package_name=related.engine.identity.name,
        package_family=related.engine.identity.family, publisher=identity.publisher,
        core_package_name=identity.name, core_package_version=identity.version,
        package_version=related.engine.package_version, related_set_id=related.related_set_id,
        engine_version='example', platform='windows-x64', engine_build_sha256='a'*64)
    value = dict(schema='nlp-preparation-release.v1', desktop_release_sha256=hashlib.sha256(raw).hexdigest(),
        engine=engine.model_dump(mode='json'), profiles=['b'*64],
        distribution_base_url='https://example.test/qualification/nlp/', minimum_free_bytes=512*1024**2, trusted_keys=keys(),
        resource_policy=BASELINE_RESOURCE_POLICY.model_dump(mode='json'))
    return value, raw, release


def test_exact_admission_and_defensive_nested_values():
    value, raw, release = fixture()
    p = profile()
    engine = EngineIdentity.model_validate_json(json.dumps(value['engine']))
    p = p.model_copy(update={'components': tuple(c.model_copy(update={'engine': engine}) for c in p.components)})
    value['profiles'] = [p.identity_digest()]
    admitted = parse_preparation_release(json.dumps(value).encode(), raw, release)
    admitted.require(p, BASELINE_RESOURCE_POLICY)
    copied = admitted.trusted_keys
    copied[0]['jwk']['x'] = 'changed'
    assert admitted.trusted_keys[0]['jwk']['x'] != 'changed'
    assert admitted.approves(p)
    assert not admitted.approves(p.model_copy(update={'revision': 'other'}))
    with pytest.raises(NlpBoundaryError):
        admitted.require(p, BASELINE_RESOURCE_POLICY.model_copy(update={'inference_threads': 3}))


@pytest.mark.parametrize('change', [
    lambda v: v.update(extra=True),
    lambda v: v['engine'].pop('worker_protocol'),
    lambda v: v.update(minimum_free_bytes=0),
    lambda v: v.update(minimum_free_bytes=True),
    lambda v: v.update(desktop_release_sha256='0'*64),
    lambda v: v.update(profiles=['b'*64, 'b'*64]),
    lambda v: v.update(profiles=['c'*64, 'b'*64]),
    lambda v: v.update(distribution_base_url='https://user:password@example.test/'),
    lambda v: v['engine'].update(package_family='foreign'),
    lambda v: v['trusted_keys'][0]['jwk'].update(d='secret'),
    lambda v: v['resource_policy'].update(inference_threads=True),
])
def test_closed_release_rejects_changed_bindings(change):
    value, raw, release = fixture()
    change(value)
    with pytest.raises((NlpBoundaryError, ValueError)):
        parse_preparation_release(json.dumps(value).encode(), raw, release)


def test_duplicate_nonfinite_oversize_and_nonqualification_rejected():
    value, raw, release = fixture()
    for body in (json.dumps(value).replace('{', '{"schema":"duplicate",', 1).encode(),
                 b'{"schema":NaN}', b' '*65537):
        with pytest.raises(NlpBoundaryError):
            parse_preparation_release(body, raw, release)
    with pytest.raises(NlpBoundaryError):
        parse_preparation_release(json.dumps(value).encode(), raw, replace(release, purpose='production'))


def test_source_loader_has_no_release_enablement(monkeypatch):
    from app.services import nlp_setup
    monkeypatch.setattr(nlp_setup.sys, 'frozen', False, raising=False)
    monkeypatch.setattr(nlp_setup, 'current_desktop_release', lambda: pytest.fail('identity read'))
    assert nlp_setup.preparation_configuration(lambda: pytest.fail('database opened'), Path('unused')) is None


@pytest.mark.windows_compat
@pytest.mark.parametrize('case', ['valid', 'absent', 'descriptor_changed', 'production', 'symlink'])
def test_fixed_sibling_loader_held_read_path(tmp_path, monkeypatch, case):
    import os
    from app.services import nlp_setup
    if os.name != 'nt':
        pytest.skip('Windows file handles')
    value, raw, release = fixture()
    image = tmp_path / 'Brain.exe'
    image.write_bytes(b'synthetic image')
    descriptor = tmp_path / 'desktop-release.json'
    descriptor.write_bytes(raw)
    path = tmp_path / 'nlp-preparation-release.json'
    if case != 'absent':
        path.write_bytes(json.dumps(value).encode())
    if case == 'descriptor_changed':
        descriptor.write_bytes(raw + b' ')
    if case == 'production':
        release = replace(release, purpose='production')
    if case == 'symlink':
        target = tmp_path / 'metadata.json'
        path.rename(target)
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip('symlink creation unavailable')
    monkeypatch.setattr(nlp_setup.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(nlp_setup.sys, 'executable', str(image))
    monkeypatch.setattr(nlp_setup, 'current_desktop_release', lambda: release)
    if case == 'absent':
        assert nlp_setup.load_preparation_release() is None
    elif case == 'valid':
        admitted = nlp_setup.load_preparation_release()
        assert admitted.profiles == tuple(value['profiles'])
        assert admitted.minimum_free_bytes == value['minimum_free_bytes']
    else:
        with pytest.raises((NlpBoundaryError, RuntimeError)):
            nlp_setup.load_preparation_release()
