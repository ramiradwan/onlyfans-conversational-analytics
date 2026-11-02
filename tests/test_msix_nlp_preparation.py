"""Bind preparation-only configuration to the exact qualification payload."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.nlp.profiles import BASELINE_RESOURCE_POLICY
from app.security.signed_objects import encode
from test_msix_packaging import stage, _msix_stage, _write_inert_digests
from tools import msix_packaging as msix
from tools.packaging_policy import verify_runtime_files

pytestmark = [pytest.mark.ci_tier('fast'), pytest.mark.windows_compat]


def inputs(tmp_path):
    selected = msix.read_json(msix.CONFIG_ROOT / 'fixtures/engine-acquisition-quote-profile.v2.json')
    path = tmp_path / 'profile.json'
    path.write_bytes(msix.canonical_json(selected))
    desktop = msix.canonical_json(msix.desktop_descriptor(selected, msix.sha256(path)))
    related = selected['servicing']['installed_artifact']
    engine = related['engine']
    key = ec.derive_private_key(1, ec.SECP256R1()).public_key().public_numbers()
    value = dict(schema='nlp-preparation-release.v1', desktop_release_sha256=hashlib.sha256(desktop).hexdigest(),
        engine=dict(package_name=engine['identity']['name'], package_family=engine['identity']['family'],
            core_package_name=selected['identity']['name'], core_package_version=msix.package_version(selected['release_sequence']),
            publisher=engine['identity']['publisher'], package_version=engine['package_version'],
            related_set_id=related['related_set_id'], engine_version='1', platform='windows-x64',
            worker_protocol='nlp-worker.v2', engine_build_sha256='a' * 64),
        profiles=['b' * 64], distribution_base_url='https://qualification.example.test/nlp/',
        trusted_keys=[dict(kid='fixture', jwk=dict(kty='EC', crv='P-256',
            x=encode(key.x.to_bytes(32, 'big')), y=encode(key.y.to_bytes(32, 'big'))))],
        resource_policy=BASELINE_RESOURCE_POLICY.model_dump(mode='json'), minimum_free_bytes=512 * 1024 * 1024)
    config = tmp_path / 'preparation.json'
    config.write_bytes(msix.canonical_json(value))
    return selected, path, desktop, value, config


@pytest.mark.skipif(os.name != 'nt', reason='PowerShell MSIX build wrapper')
@pytest.mark.parametrize(('configured', 'quoted_engine'), [(False, False), (True, False), (True, True)])
def test_build_wrapper_forwards_optional_preparation_without_starting_packaging(stage, tmp_path, monkeypatch,
                                                                             configured, quoted_engine):
    _, profile, _, _, config = inputs(tmp_path)
    config = config.rename(tmp_path / 'model preparation [fixture].json')
    captured = tmp_path / 'prepare-arguments.json'
    monkeypatch.setenv('OFCA_TEST_MSIX_ARGUMENTS', str(captured))
    # Stop at the real wrapper's producer boundary, before sealing or MakeAppx.
    producer = tmp_path / 'capture-producer.ps1'
    producer.write_text(r'''if ($args[1] -eq 'validate-profile') {
    $global:LASTEXITCODE = 0
    Write-Output '{}'
    return
}
if ($args[1] -ne 'prepare') { throw 'Unexpected producer operation' }
ConvertTo-Json -InputObject @($args) -Compress | Set-Content -LiteralPath $env:OFCA_TEST_MSIX_ARGUMENTS
throw 'captured-prepare-boundary'
''', encoding='utf-8')
    output = tmp_path / 'package-output'
    command = ['pwsh', '-NoProfile', '-NonInteractive', '-File',
        str(msix.ROOT / 'packaging/build-msix.ps1'), '-BuildPython', str(producer),
        '-StagingRoot', str(stage), '-PackageProfile', str(profile), '-OutputRoot', str(output)]
    if configured:
        command += ['-NlpPreparationRelease', str(config)]
    if quoted_engine:
        command += ['-EngineBundle', str(tmp_path / 'engine.msixbundle'),
            '-EngineAcquisitionQuoteInputs', str(tmp_path / 'quote-inputs.json')]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'captured-prepare-boundary' in result.stderr, result.stdout + result.stderr
    arguments = json.loads(captured.read_text(encoding='utf-8-sig'))
    expected = [str(msix.ROOT / 'tools/msix_packaging.py'), 'prepare', '--stage', str(stage),
        '--working', str(output / 'working'), '--profile', str(profile)]
    if quoted_engine:
        expected += ['--engine-acquisition-quote-inputs', str(tmp_path / 'quote-inputs.json'),
            '--engine-bundle', str(tmp_path / 'engine.msixbundle')]
    if configured:
        expected += ['--nlp-preparation-release', str(config)]
    assert arguments == expected
    assert not output.exists()


def archive_payload(working, path, *, preparation_path=msix.NLP_PREPARATION_PATH):
    with zipfile.ZipFile(path, 'w') as archive:
        for leaf in (working / 'payload').rglob('*'):
            if leaf.is_file():
                name = leaf.relative_to(working / 'payload').as_posix()
                archive.writestr(preparation_path if name == msix.NLP_PREPARATION_PATH else name, leaf.read_bytes())
        archive.writestr('AppxBlockMap.xml', b'inert footprint')
        archive.writestr('[Content_Types].xml', b'inert footprint')


@pytest.fixture(scope='module')
def sealed_preparation(tmp_path_factory):
    root = tmp_path_factory.mktemp('sealed-preparation')
    _, profile, _, _, config = inputs(root)
    working = root / 'configured'
    msix.prepare_payload(_msix_stage(root), working, profile, nlp_preparation_release=config)
    _write_inert_digests(working / 'payload/app')
    msix.seal_payload(working, profile, Path(sys.executable))
    return working


@pytest.fixture
def copied_preparation(sealed_preparation, tmp_path):
    _, profile, _, _, config = inputs(tmp_path)
    working = tmp_path / 'configured'
    shutil.copytree(sealed_preparation, working)
    receipt_path = working / 'preparation.json'
    receipt = msix.read_json(receipt_path, maximum=16 * 1024 * 1024)
    assert msix.sha256(config) == receipt['nlp_preparation']['sha256']
    receipt['nlp_preparation']['inputs'] = str(config.resolve())
    receipt_path.write_bytes(msix.canonical_json(receipt))
    return working, profile, config


def test_preparation_input_is_optional_and_sealed(stage, copied_preparation, tmp_path):
    working, profile, config = copied_preparation
    ordinary = tmp_path / 'ordinary'
    msix.prepare_payload(stage, ordinary, profile)
    assert not (ordinary / 'payload' / msix.NLP_PREPARATION_PATH).exists()
    target = working / 'payload' / msix.NLP_PREPARATION_PATH
    assert target.read_bytes() == config.read_bytes()
    inventory = msix.read_json(working / 'inventory.json', maximum=16 * 1024 * 1024)
    record = next(row for row in inventory['files'] if row['path'] == msix.NLP_PREPARATION_PATH)
    assert record['sha256'] == msix.sha256(config)
    assert 'nlp-preparation-release.json' in (working / 'payload/app/sha256sums.txt').read_text()
    package = tmp_path / 'core.msix'
    archive_payload(working, package)
    assert msix.verify_archive(package, working / 'inventory.json', profile)['file_count'] == len(inventory['files'])


@pytest.mark.parametrize('change', ['input', 'payload', 'unrequested', 'missing'])
def test_seal_refuses_changed_or_unrequested_preparation(tmp_path, request, change):
    if change == 'unrequested':
        _, profile, _, _, config = inputs(tmp_path)
        working = tmp_path / 'configured'
        msix.prepare_payload(request.getfixturevalue('stage'), working, profile)
    else:
        working, profile, config = request.getfixturevalue('copied_preparation')
    target = working / 'payload' / msix.NLP_PREPARATION_PATH
    if change == 'input':
        config.write_bytes(config.read_bytes() + b' ')
    elif change == 'payload':
        target.write_bytes(target.read_bytes() + b' ')
    elif change == 'missing':
        target.unlink()
    else:
        target.write_bytes(config.read_bytes())
    _write_inert_digests(working / 'payload/app')
    with pytest.raises((ValueError, FileNotFoundError)):
        msix.seal_payload(working, profile, Path(sys.executable))


@pytest.mark.parametrize('target', ['staging', 'inno'])
def test_preparation_cannot_enter_through_runtime_stage(stage, tmp_path, target):
    _, profile, _, _, config = inputs(tmp_path)
    (stage / 'nlp-preparation-release.json').write_bytes(config.read_bytes())
    assert any(f.code == 'nlp_preparation_forbidden' for f in verify_runtime_files(stage, target=target))
    with pytest.raises(ValueError, match='nlp_preparation_forbidden'):
        msix.prepare_payload(stage, tmp_path / 'output', profile, nlp_preparation_release=config)


@pytest.mark.parametrize('change', ['descriptor', 'engine', 'unknown', 'private_key', 'production', 'store'])
def test_archive_checks_preparation_semantics_after_inventory_matches(tmp_path, request, change):
    selected, profile, desktop, value, config = inputs(tmp_path)
    if change == 'production':
        selected['purpose'] = 'production'
    elif change == 'store':
        selected['distribution'] = 'microsoft-store'
    elif change == 'descriptor':
        value['desktop_release_sha256'] = '0' * 64
    elif change == 'engine':
        value['engine']['core_package_version'] = '8.0.0.0'
    elif change == 'unknown':
        value['execution_enabled'] = True
    else:
        value['trusted_keys'][0]['jwk']['d'] = 'private'
    if change in ('production', 'store'):
        with pytest.raises(ValueError, match='qualification related package'):
            msix.validate_nlp_preparation(config.read_bytes(), desktop, selected)
        return
    working = tmp_path / 'configured'
    shutil.copytree(request.getfixturevalue('sealed_preparation'), working)
    target = working / 'payload' / msix.NLP_PREPARATION_PATH
    target.write_bytes(msix.canonical_json(value))
    inventory_path = working / 'inventory.json'
    inventory = msix.read_json(inventory_path, maximum=16 * 1024 * 1024)
    inventory['files'] = [msix.file_record(working / 'payload' / r['path'], r['path']) for r in inventory['files']]
    inventory_path.write_bytes(msix.canonical_json(inventory))
    package = tmp_path / 'changed.msix'
    archive_payload(working, package)
    with pytest.raises(ValueError, match='nlp_preparation_release_invalid'):
        msix.verify_archive(package, inventory_path, profile)


@pytest.mark.parametrize('raw', [b'{}', b'x' * 65537, b'{"schema":"x","schema":"x"}'],
                         ids=['empty_object', 'oversized', 'duplicate_member'])
def test_prepare_rejects_bad_input_before_output_creation(stage, tmp_path, raw):
    _, profile, _, _, config = inputs(tmp_path)
    config.write_bytes(raw)
    output = tmp_path / 'refused'
    with pytest.raises(ValueError):
        msix.prepare_payload(stage, output, profile, nlp_preparation_release=config)
    assert not output.exists()


def test_archive_alias_cannot_bypass_preparation_validation(sealed_preparation, tmp_path):
    selected, profile, _, value, _ = inputs(tmp_path)
    working = tmp_path / 'configured'
    shutil.copytree(sealed_preparation, working)
    inventory = msix.read_json(working / 'inventory.json', maximum=16 * 1024 * 1024)
    value['desktop_release_sha256'] = '0' * 64
    target = working / 'payload' / msix.NLP_PREPARATION_PATH
    target.write_bytes(msix.canonical_json(value))
    record = next(row for row in inventory['files'] if row['path'] == msix.NLP_PREPARATION_PATH)
    record.update(msix.file_record(target, msix.NLP_PREPARATION_PATH))
    related = selected['servicing']['installed_artifact']
    # Inert bundle records exercise payload refusal before signature validation.
    inventory['artifact_set'] = dict(kind='related_bundle', related_set_id=related['related_set_id'],
        core=dict(file='ConversationAnalytics_1.0.11.0_x64.msixbundle', sha256='c' * 64, size=1),
        engine=dict(**related['engine'], artifact=dict(file='TextAnalysisEngine_1.0.42.0_x64.msixbundle',
            sha256='d' * 64, size=1)))
    inventory_path = working / 'inventory.json'
    inventory_path.write_bytes(msix.canonical_json(inventory))
    script = tmp_path / 'verify-payload.ps1'
    script.write_text(r'''param($Source, $Package, $Inventory, $PackageProfile)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$msixConfiguration = Join-Path (Split-Path $Source) 'msix'
Import-Module (Join-Path $msixConfiguration 'release-artifacts.psm1') -Force
$AllowQualification = $true
$VerificationMode = 'DirectSigned'
$ExpectedFamilyName = $SignTool = ''
$tokens = $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Invalid verifier syntax' }
foreach ($definition in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
try { Test-MsixPayload $Package; exit 0 } catch { Write-Output $_.Exception.Message; exit 2 }
''', encoding='utf-8')
    for name in (msix.NLP_PREPARATION_PATH, 'app/nlp%2Dpreparation-release.json',
                 'app/NLP-preparation-release.json'):
        package = tmp_path / 'changed.msix'
        archive_payload(working, package, preparation_path=name)
        alias = name != msix.NLP_PREPARATION_PATH
        reason = 'fixed location' if alias else 'nlp_preparation_release_invalid'
        with pytest.raises(ValueError, match=reason):
            msix.verify_archive(package, inventory_path, profile)
        if os.name == 'nt' and alias:
            result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(script),
                str(msix.ROOT / 'packaging/verify-msix.ps1'), str(package), str(inventory_path), str(profile)],
                capture_output=True, text=True, timeout=120)
            assert result.returncode == 2 and 'fixed location' in result.stdout, result.stdout + result.stderr


@pytest.mark.skipif(os.name != 'nt', reason='Native PowerShell package verification')
def test_powershell_preparation_verifier_is_closed_and_release_bound(tmp_path):
    selected, profile, desktop, value, _ = inputs(tmp_path)
    descriptor = tmp_path / 'desktop-descriptor.json'
    descriptor.write_bytes(desktop)
    cases = [('valid', value, True)]
    mutations = [
        ('unknown', lambda v: v.update(execution_enabled=True)),
        ('desktop', lambda v: v.update(desktop_release_sha256='0' * 64)),
        ('engine', lambda v: v['engine'].update(package_version='9.9.9.9')),
        ('protocol', lambda v: v['engine'].pop('worker_protocol')),
        ('duplicate_profile', lambda v: v['profiles'].append(v['profiles'][0])),
        ('unsorted_profiles', lambda v: v.update(profiles=['c' * 64, 'b' * 64])),
        ('private_key', lambda v: v['trusted_keys'][0]['jwk'].update(d='private')),
        ('key_point', lambda v: v['trusted_keys'][0]['jwk'].update(x=encode(bytes(32)), y=encode(bytes(32)))),
        ('key_encoding', lambda v: v['trusted_keys'][0]['jwk'].update(x='A' * 42 + 'B')),
        ('credentials', lambda v: v.update(distribution_base_url='https://user:password@example.test/')),
        ('http', lambda v: v.update(distribution_base_url='http://example.test/')),
        ('query', lambda v: v.update(distribution_base_url='https://example.test/?secret=x/')),
        ('no_reserve', lambda v: v.update(minimum_free_bytes=0)),
        ('bool_limit', lambda v: v['resource_policy'].update(inference_threads=True)),
        ('excess_limit', lambda v: v['resource_policy'].update(download_bytes=2**53)),
    ]
    for name, mutate in mutations:
        bad = copy.deepcopy(value)
        mutate(bad)
        cases.append((name, bad, False))
    for path in [('schema',), ('desktop_release_sha256',), ('engine', 'publisher'),
                 ('engine', 'platform'), ('engine', 'worker_protocol'),
                 ('trusted_keys', 0, 'jwk', 'kty'), ('trusted_keys', 0, 'jwk', 'crv')]:
        bad = copy.deepcopy(value)
        parent = bad
        for name in path[:-1]:
            parent = parent[name]
        parent[path[-1]] = [parent[path[-1]]]
        cases.append(('array_' + str(path[-1]), bad, False))
    manifest = []
    for name, body, accepted in cases:
        path = tmp_path / (name + '.json')
        raw = msix.canonical_json(body)
        path.write_bytes(raw)
        manifest.append(dict(name=name, path=str(path), accepted=accepted, profile=str(profile)))
        if accepted:
            msix.validate_nlp_preparation(raw, desktop, selected)
        else:
            with pytest.raises(ValueError):
                msix.validate_nlp_preparation(raw, desktop, selected)
    for name, body in [('duplicate_json', b'{"schema":0,"schema":1}'), ('oversized', b'x' * 65537)]:
        path = tmp_path / (name + '.json')
        path.write_bytes(body)
        manifest.append(dict(name=name, path=str(path), accepted=False, profile=str(profile)))
    for name, update in [('production', dict(purpose='production')), ('store', dict(distribution='microsoft-store'))]:
        changed = dict(selected, **update)
        path = tmp_path / (name + '-profile.json')
        path.write_bytes(msix.canonical_json(changed))
        manifest.append(dict(name=name, path=str(tmp_path / 'valid.json'), accepted=False, profile=str(path)))
    case_path = tmp_path / 'cases.json'
    case_path.write_bytes(msix.canonical_json(manifest))
    script = tmp_path / 'verify.ps1'
    script.write_text(r'''param($Source, $Descriptor, $Cases)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Invalid verifier syntax' }
foreach ($name in @('Get-ByteDigest', 'Convert-CheckedJson', 'Assert-ClosedObject', 'Assert-NlpPreparation')) {
    $definitions = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name }, $true))
    if ($definitions.Count -ne 1) { throw 'Expected one verifier function' }
    . ([scriptblock]::Create($definitions[0].Extent.Text))
}
$desktop = [IO.File]::ReadAllBytes($Descriptor)
$cases = Get-Content -LiteralPath $Cases -Raw | ConvertFrom-Json -AsHashtable
foreach ($case in $cases) {
    $profile = Get-Content -LiteralPath $case.profile -Raw | ConvertFrom-Json -AsHashtable
    $accepted = $false
    try { Assert-NlpPreparation ([IO.File]::ReadAllBytes($case.path)) $desktop $profile; $accepted = $true } catch { $reason = $_.Exception.Message }
    if ($accepted -ne $case.accepted) { throw ('Unexpected outcome for ' + $case.name + ': ' + $reason) }
}
Write-Output ('Verified ' + $cases.Count + ' cases')
''', encoding='utf-8')
    result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(script),
        str(msix.ROOT / 'packaging/verify-msix.ps1'), str(descriptor), str(case_path)],
        capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Verified 27 cases' in result.stdout
