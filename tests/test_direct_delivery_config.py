"""Direct delivery inputs agree with the existing producer and isolated serving owner."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from tools import msix_packaging as msix

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / 'packaging/msix/direct-delivery.json'
PROFILE = ROOT / 'packaging/msix/profiles/production-direct.json'


def test_production_direct_inputs_are_complete_and_match_the_producer():
    data = msix.read_json(INPUTS)
    profile = msix.load_profile(PROFILE, production=True)
    assert data['profile'] == PROFILE.relative_to(ROOT).as_posix()
    assert profile['purpose'] == 'production'
    assert profile['distribution'] == 'direct-appinstaller'
    assert profile['identity']['publisher'] == data['signing']['subject'] == 'CN=Dipsy, O=Dipsy, L=London, C=GB'
    assert profile['app_installer_uri'] == data['hosting']['app_installer_uri']
    assert profile['minimum_os_version'] == '10.0.26200.0'
    # Input provisioning does not authorize a product release.
    assert data['stable_feed_published'] is False


def test_store_and_qualification_authority_remain_separate():
    direct = msix.load_profile(PROFILE, production=True)
    store = msix.load_profile(ROOT / 'packaging/msix/profiles/production-store.json', production=True)
    assert store['app_installer_uri'] is None
    assert direct['identity']['publisher'] != store['identity']['publisher']
    assert direct['identity']['name'] != store['identity']['name']
    with pytest.raises(ValueError):
        msix.load_profile(ROOT / 'packaging/msix/profiles/production-direct.template.json', production=True)
    for name in ('qualification-direct.json', 'qualification-store.json'):
        with pytest.raises(ValueError):
            msix.load_profile(ROOT / 'packaging/msix/profiles' / name, production=True)


def test_appinstaller_output_has_exact_source_and_the_same_package_origin():
    profile = msix.load_profile(PROFILE, production=True)
    version = msix.package_version(profile['release_sequence'])
    uri = 'https://updates.dipsy.fi/desktop/v1/packages/' + 'a' * 64 + f'/ConversationAnalytics_{version}_x64.msix'
    tree = ET.fromstring(msix.render_appinstaller(profile, uri))
    ns = {'a': 'http://schemas.microsoft.com/appx/appinstaller/2018'}
    assert tree.attrib['Uri'] == profile['app_installer_uri']
    package = tree.find('a:MainPackage', ns)
    assert package.attrib['Publisher'] == profile['identity']['publisher']
    assert package.attrib['Uri'] == uri
    assert tree.find('a:UpdateSettings/a:ForceUpdateFromAnyVersion', ns) is None


def test_serving_owner_has_only_dedicated_desktop_buckets():
    config = json.loads((ROOT / 'tools/desktop-delivery/wrangler.jsonc').read_text(encoding='utf-8'))
    inputs = msix.read_json(INPUTS)
    assert config['name'] == inputs['hosting']['worker']
    assert config['workers_dev'] is False and config['preview_urls'] is False
    assert config['routes'][0]['pattern'] == 'updates.dipsy.fi/desktop/v1/*'
    assert {b['bucket_name'] for b in config['r2_buckets']} == {inputs['hosting']['bucket'], inputs['hosting']['qualification_bucket']}
    assert all(b['jurisdiction'] == 'eu' for b in config['r2_buckets'])
    assert 'dipsy-signing-parameters' not in {b['bucket_name'] for b in config['r2_buckets']}


@pytest.mark.skipif(os.name != 'nt', reason='Windows Appx identity derivation')
def test_verified_direct_family_is_derived_by_windows(tmp_path):
    profile = msix.load_profile(PROFILE, production=True)
    manifest = tmp_path / 'AppxManifest.xml'
    manifest.write_bytes(msix.render_manifest(profile))
    script = tmp_path / 'read-identity.ps1'
    script.write_text('param([string]$Binding,[string]$Manifest)\n'
                      '$ErrorActionPreference="Stop"; Add-Type -Path $Binding; '
                      '[Ofca.Msix.IdentityReader]::Read([IO.File]::ReadAllBytes($Manifest)) | ConvertTo-Json -Compress', encoding='utf-8')
    result = subprocess.run(['pwsh', '-NoProfile', '-File', str(script),
                             str(ROOT / 'packaging/msix/ReadPackageIdentity.cs'), str(manifest)],
                            check=True, capture_output=True, text=True, timeout=30)
    identity = json.loads(result.stdout)
    assert identity['Publisher'] == profile['identity']['publisher']
    assert identity['FamilyName'] == msix.read_json(INPUTS)['expected_package_family']
