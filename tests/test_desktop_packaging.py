"""Native payload and prerequisite failure behavior at the packaging boundary."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

import inno_setup_compiler
from test_packaging_policy import _stage_runtime_tree
from tools.packaging_policy import verify_runtime_files

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


@pytest.mark.skipif(os.name != "nt", reason="actual PowerShell dispatch guard")
def test_prerequisite_guard_refuses_mutable_wrong_source_and_publication(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / '.github/workflows/windows-package.yml').read_text(encoding='utf-8'))
    guard = workflow['jobs']['prerequisite-check']['steps'][0]['run']
    path = tmp_path / "guard.ps1"
    path.write_text("$ErrorActionPreference='Stop'\n" + guard, encoding="utf-8")
    valid = {"REF_TYPE": "tag", "REF_NAME": "vmsix-diagnostic-local", "REF_COMMIT": "a" * 40,
             "PRODUCT_REVISION": "a" * 40, "PUBLICATION_REQUESTED": "false"}
    for overrides, accepted in (({}, True), ({"REF_TYPE": "branch"}, False),
        ({"REF_NAME": "v1.0.0"}, False), ({"PRODUCT_REVISION": "b" * 40}, False),
        ({"PUBLICATION_REQUESTED": "true"}, False)):
        result = subprocess.run(["pwsh.exe", "-NoProfile", "-NonInteractive", "-File", str(path)],
            env=os.environ | valid | overrides, capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert (result.returncode == 0) is accepted, result.stdout + result.stderr


@pytest.mark.parametrize("capability", ["desktop", "window-frame"])
def test_client_routes_retain_the_owned_native_document_capability(capability):
    routes = (ROOT / "frontend/src/routing/useAppRoutes.tsx").read_text(encoding="utf-8")
    application_paths = {"/"} | {
        "/" + path for path in re.findall(r"\bpath:\s*'([^']+)'", routes)
        if path != "*"
    }
    profile = json.loads((ROOT / f"desktop/src-tauri/capabilities/{capability}.json").read_text(encoding="utf-8"))
    origin = "http://bridge.localhost:17871"
    admitted = set(profile["remote"]["urls"])
    assert {origin + path for path in application_paths} <= admitted
    assert profile["windows"] == ["main"]
    assert origin + "/*" not in admitted
    assert origin + "/browser-tasks/*" not in admitted
    assert origin + "/setup-code/*" not in admitted
    assert origin + "/api/*" not in admitted


@pytest.mark.parametrize("relative", ["brain-fixture.exe", "prerequisite-fixture.exe", "WebView/msedgewebview2.exe"])
def test_release_rejects_qualification_fixture_and_private_browser_engine(tmp_path, relative):
    stage = _stage_runtime_tree(tmp_path)
    target = stage / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not-a-release-payload")
    assert any(item.code == "forbidden_path_present" and item.path == relative for item in verify_runtime_files(stage))


def test_release_requires_native_host_as_well_as_frozen_backend(tmp_path):
    stage = _stage_runtime_tree(tmp_path)
    (stage / "Bridge.exe").unlink()
    assert any(item.code == "required_file_missing" and item.path == "Bridge.exe" for item in verify_runtime_files(stage))


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell module inheritance")
@pytest.mark.parametrize("architecture", ["System32", "SysWOW64"])
def test_webview_helper_uses_builtin_signature_verifier_after_an_intermediate_process(tmp_path, architecture):
    # Python preserves the inherited PS7 search path, unlike a direct PS7 to
    # Windows PowerShell launch. Exercise the actual helper initialization and
    # real signature command. Windows may retrieve certificate/revocation
    # metadata under its trust policy; no runtime is installed or trust mocked.
    pwsh = shutil.which("pwsh.exe")
    assert pwsh, "PowerShell 7 is required to reproduce inherited module paths"
    modules = Path(pwsh).resolve().parent / "Modules"
    assert (modules / "Microsoft.PowerShell.Security").is_dir()
    source = (ROOT / "packaging/windows/ensure-webview2.ps1").read_text(encoding="utf-8")
    initialization = tmp_path / "helper-initialization.ps1"
    initialization.write_text(source[:source.index("$script:webView2Acquisition =")], encoding="utf-8")
    unsigned = tmp_path / "unsigned.exe"
    unsigned.write_bytes(b"synthetic unsigned prerequisite; never executable")
    harness = tmp_path / "inherited-signature.ps1"
    harness.write_text(r'''
param([string]$Initialization, [string]$Unsigned)
$ErrorActionPreference = 'Stop'
[Console]::WriteLine('signature_probe:ambient:begin'); [Console]::Out.Flush()
$ambientFailed = $false
try { $null = Get-AuthenticodeSignature -LiteralPath $Unsigned }
catch [Management.Automation.CommandNotFoundException] { $ambientFailed = $true }
if (-not $ambientFailed) { throw 'incompatible_module_context_not_reproduced' }
[Console]::WriteLine('signature_probe:ambient:complete'); [Console]::Out.Flush()
[Console]::WriteLine('signature_probe:initialization:begin'); [Console]::Out.Flush()
. $Initialization -MinimumVersion '134.0.3124.0'
[Console]::WriteLine('signature_probe:initialization:complete'); [Console]::Out.Flush()
[Console]::WriteLine('signature_probe:signed:begin'); [Console]::Out.Flush()
$signed = Get-AuthenticodeSignature -LiteralPath ([IO.Path]::Combine($PSHOME, 'powershell.exe'))
if ($signed.Status -ne 'Valid' -or $null -eq $signed.SignerCertificate) { throw 'real_signed_file_not_verified' }
[Console]::WriteLine('signature_probe:signed:complete'); [Console]::Out.Flush()
[Console]::WriteLine('signature_probe:unsigned:begin'); [Console]::Out.Flush()
$unsignedSignature = Get-AuthenticodeSignature -LiteralPath $Unsigned
if ($unsignedSignature.Status -eq 'Valid' -or $null -ne $unsignedSignature.SignerCertificate) { throw 'unsigned_file_admitted' }
[Console]::WriteLine('signature_probe:unsigned:complete'); [Console]::Out.Flush()
[Console]::WriteLine('signature_probe:module:begin'); [Console]::Out.Flush()
if ($env:PSModulePath -cne [IO.Path]::Combine($PSHOME, 'Modules')) { throw 'module_discovery_not_isolated' }
$command = Get-Command Get-AuthenticodeSignature
if ($command.Module.Version.ToString() -ne '3.0.0.0') { throw 'wrong_security_module' }
[Console]::WriteLine('signature_probe:module:complete'); [Console]::Out.Flush()
# Also exercise the other built-in modules needed after signature validation.
[Console]::WriteLine('signature_probe:management:begin'); [Console]::Out.Flush()
Start-Sleep -Milliseconds 1
if (-not (Test-Path -LiteralPath $Unsigned)) { throw 'management_module_unavailable' }
[Console]::WriteLine('signature_probe:management:complete'); [Console]::Out.Flush()
[Console]::WriteLine('signature_probe:complete'); [Console]::Out.Flush()
[Console]::Write('builtin_signature_verification_passed')
''', encoding="utf-8")
    executable = Path(os.environ["SystemRoot"]) / architecture / "WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([
            str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(harness), "-Initialization", str(initialization), "-Unsigned", str(unsigned),
        ], env=os.environ | {"PSModulePath": str(modules)}, capture_output=True, text=True,
            timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = exc.stdout or "", exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        pytest.fail(
            f"Signature probe timed out after 120s ({architecture}); "
            f"partial stdout (last 4096 characters): {stdout[-4096:]!r}; "
            f"partial stderr (last 4096 characters): {stderr[-4096:]!r}",
            pytrace=False,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    expected_markers = [
        "signature_probe:ambient:begin", "signature_probe:ambient:complete",
        "signature_probe:initialization:begin", "signature_probe:initialization:complete",
        "signature_probe:signed:begin", "signature_probe:signed:complete",
        "signature_probe:unsigned:begin", "signature_probe:unsigned:complete",
        "signature_probe:module:begin", "signature_probe:module:complete",
        "signature_probe:management:begin", "signature_probe:management:complete",
        "signature_probe:complete", "builtin_signature_verification_passed",
    ]
    assert result.stdout.splitlines() == expected_markers and not result.stderr


@pytest.mark.skipif(os.name != "nt", reason="actual workflow inherited Windows PowerShell context")
def test_prerequisite_workflow_preserves_inherited_context_for_real_signature_probe(tmp_path):
    pwsh = shutil.which("pwsh.exe")
    assert pwsh, "PowerShell 7 is required for the actual workflow launch"
    modules = Path(pwsh).resolve().parent / "Modules"
    assert (modules / "Microsoft.PowerShell.Security").is_dir()
    workflow = yaml.safe_load((ROOT / ".github/workflows/windows-package.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["prerequisite-check"]["steps"]
        if step.get("name") == "Exercise the actual prerequisite helper without building an app")
    source = (ROOT / "packaging/windows/ensure-webview2.ps1").read_text(encoding="utf-8")
    initialization = source[:source.index("$script:webView2Acquisition =")]
    # Observe the child before the real initialization repairs discovery. A
    # direct PS7 native invocation would filter its module path and fail this
    # inherited-context requirement even if the signature probe then worked.
    initialization = initialization.replace("Set-StrictMode -Version Latest", r'''
$script:InheritedPowerShell7Modules = ($env:PSModulePath -split [IO.Path]::PathSeparator) -contains $env:OFCA_TEST_PS7_MODULES
[Console]::WriteLine('private fixture stdout must not escape')
[Console]::Error.WriteLine('private fixture stderr must not escape')
Set-StrictMode -Version Latest''', 1)
    fixture = tmp_path / "workflow-cwd"
    policy = fixture / "shared/desktop-release/prerequisite-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_bytes((ROOT / "shared/desktop-release/prerequisite-policy.json").read_bytes())
    helper = fixture / "packaging/windows/ensure-webview2.ps1"
    helper.parent.mkdir(parents=True)
    (helper.parent / "unsigned.exe").write_bytes(b"synthetic unsigned probe; never executable")
    # Only the production initialization runs: no acquisition function, network
    # request or runtime installer exists in this small helper fixture.
    helper.write_text(initialization + r'''
$modulePathIsolated = $env:PSModulePath -ceq [IO.Path]::Combine($PSHOME, 'Modules')
$signedAccepted = $false
$unsignedRefused = $false
$builtinVerifier = $false
try {
    $signed = Get-AuthenticodeSignature -LiteralPath ([IO.Path]::Combine($PSHOME, 'powershell.exe'))
    $unsigned = Get-AuthenticodeSignature -LiteralPath ([IO.Path]::Combine($PSScriptRoot, 'unsigned.exe'))
    $command = Get-Command Get-AuthenticodeSignature
    $signedAccepted = $signed.Status -eq 'Valid' -and $null -ne $signed.SignerCertificate
    $unsignedRefused = $unsigned.Status -ne 'Valid' -and $null -eq $unsigned.SignerCertificate
    $builtinVerifier = $command.Module.Name -eq 'Microsoft.PowerShell.Security' -and $command.Module.Version.ToString() -eq '3.0.0.0'
} catch { }
$passed = $script:InheritedPowerShell7Modules -and $modulePathIsolated -and $signedAccepted -and $unsignedRefused -and $builtinVerifier
$context = '{"inherited_ps7_modules":' + $script:InheritedPowerShell7Modules.ToString().ToLowerInvariant() +
    ',"module_path_isolated":' + $modulePathIsolated.ToString().ToLowerInvariant() +
    ',"signed_accepted":' + $signedAccepted.ToString().ToLowerInvariant() +
    ',"unsigned_refused":' + $unsignedRefused.ToString().ToLowerInvariant() +
    ',"builtin_verifier":' + $builtinVerifier.ToString().ToLowerInvariant() + '}'
[IO.File]::WriteAllText($ResultPath + '.context.json', $context, [Text.Encoding]::ASCII)
$line = if ($passed) { 'phase=probe;outcome=pass;reason=compatible_present;bootstrapper_exit_code=none' }
    else { 'phase=probe;outcome=fail;reason=probe_failed;bootstrapper_exit_code=none' }
[IO.File]::WriteAllText($ResultPath + '.diagnostics', $line, [Text.Encoding]::ASCII)
$code = if ($passed) { 0 } else { 1 }
[IO.File]::WriteAllText($ResultPath, [string]$code, [Text.Encoding]::ASCII)
exit $code
''', encoding="utf-8")
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    harness = fixture / "workflow-run.ps1"
    harness.write_text("$ErrorActionPreference='Stop'\n" + step["run"], encoding="utf-8")
    result = subprocess.run([
        pwsh, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness),
    ], cwd=fixture, env=os.environ | {
        "PSModulePath": str(modules), "OFCA_TEST_PS7_MODULES": str(modules), "RUNNER_TEMP": str(runner_temp),
    }, capture_output=True, text=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "phase=probe;outcome=pass;reason=compatible_present;bootstrapper_exit_code=none",
        "Shared runtime prerequisite qualified. No Product package was built or signed.",
    ]
    context = json.loads((runner_temp / "webview2-qualification-result.txt.context.json").read_text(encoding="ascii"))
    assert context == {
        "inherited_ps7_modules": True, "module_path_isolated": True, "signed_accepted": True,
        "unsigned_refused": True, "builtin_verifier": True,
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows prerequisite script")
@pytest.mark.parametrize("scenario", [
    "present", "present_user", "newer", "maximum_major", "old", "partial", "extra",
    "leading_zero", "preview", "zero_major", "out_of_range", "missing",
    "untrusted", "wrong_publisher", "download_failure", "install_failure",
    "missing_after_install", "old_after_install", "delayed_registration", "registration_at_deadline",
    "nonzero_exit_with_runtime", "timeout",
])
def test_shared_webview_prerequisite_skips_present_and_refuses_failed_install(tmp_path, scenario):
    script = (ROOT / "packaging/windows/ensure-webview2.ps1").read_text(encoding="utf-8")
    minimum = json.loads((ROOT / "shared/desktop-release/prerequisite-policy.json").read_text(encoding="utf-8"))["webview2_minimum_version"]
    # Run the real installer orchestration with OS/download boundaries replaced.
    # No network or machine-wide runtime changes belong in this unit test.
    definitions = script[:script.index("$resultCode = 0")]
    harness = tmp_path / "prerequisite.ps1"
    harness.write_text(definitions + f"\n$scenario = '{scenario}'\n" + r'''
$required = [Version]$MinimumVersion
$script:runtimeVersion = switch ($scenario) {
    'present' { $MinimumVersion }
    'present_user' { $MinimumVersion }
    'newer' { "$($required.Major + 1).0.0.0" }
    'maximum_major' { '65535.65535.65535.65535' }
    'old' { "$($required.Major - 1).65535.65535.65535" }
    'partial' { "$($required.Major).0" }
    'extra' { "$MinimumVersion.1" }
    'leading_zero' { "0$MinimumVersion" }
    'preview' { "$MinimumVersion dev" }
    'zero_major' { '0.65535.65535.65535' }
    'out_of_range' { '65536.0.0.0' }
    default { $null }
}
$script:downloads = 0
$script:starts = 0
$script:kills = 0
$script:disposed = 0
$script:downloadPath = $null
$script:elapsed = 0
$script:clocks = 0
$script:sleeps = 0
function Get-WebView2InstallClock {
    $script:clocks++
    $clock = [pscustomobject]@{}
    $clock | Add-Member -MemberType ScriptProperty -Name ElapsedMilliseconds -Value { return $script:elapsed }
    return $clock
}
function Start-Sleep {
    param([int]$Milliseconds)
    if ($Milliseconds -le 0 -or $Milliseconds -gt 250 -or $script:elapsed + $Milliseconds -gt 180000) { throw 'poll_budget_exceeded' }
    $script:sleeps++
    $script:elapsed += $Milliseconds
    if ($scenario -eq 'delayed_registration' -and $script:elapsed -ge 500) { $script:runtimeVersion = $MinimumVersion }
    if ($scenario -eq 'registration_at_deadline' -and $script:elapsed -eq 180000) { $script:runtimeVersion = $MinimumVersion }
}
function Get-ItemProperty {
    param($LiteralPath, $Name, $ErrorAction)
    if ($Name -ne 'pv' -or $LiteralPath -notin @(
        'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'Registry::HKEY_CURRENT_USER\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    )) { throw 'unexpected_registry_query' }
    if ($scenario -eq 'present_user' -and $LiteralPath.StartsWith('Registry::HKEY_LOCAL_MACHINE')) { return $null }
    if ($script:runtimeVersion) { return [pscustomobject]@{pv=$script:runtimeVersion} }
}
function Invoke-WebRequest {
    param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
    $script:downloads++
    $script:downloadPath = $OutFile
    if ($Uri -ne 'https://go.microsoft.com/fwlink/p/?LinkId=2124703') { throw 'unexpected_download' }
    if ($scenario -eq 'download_failure') { throw 'offline' }
    Set-Content -LiteralPath $OutFile -Value 'fixture'
}
function Get-AuthenticodeSignature {
    param($LiteralPath)
    [pscustomobject]@{Status=$(if ($scenario -eq 'untrusted') {'NotTrusted'} else {'Valid'});
        SignerCertificate=[pscustomobject]@{Subject=$(if ($scenario -eq 'wrong_publisher') {'CN=Other Publisher'} else {'CN=Microsoft Corporation, O=Microsoft Corporation'})}}
}
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru)
    $script:starts++
    if ($WindowStyle -ne 'Hidden' -or ($ArgumentList -join ' ') -ne '/silent /install') { throw 'unexpected_invocation' }
    $script:runtimeVersion = if ($scenario -eq 'old_after_install') { "$($required.Major - 1).65535.65535.65535" }
        elseif ($scenario -notin @('install_failure','missing_after_install','delayed_registration','registration_at_deadline','timeout')) { $MinimumVersion }
        else { $null }
    $result = [pscustomobject]@{ExitCode=$(if ($scenario -in @('install_failure','nonzero_exit_with_runtime')) {1} else {0});HasExited=$scenario -ne 'timeout'}
    $result | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
        param($timeout)
        if ($script:kills -gt 0) { if ($timeout -ne 5000) { throw 'wrong_cleanup_budget' }; return $true }
        if ($timeout -ne 180000) { throw 'wrong_install_budget' }
        if ($scenario -eq 'registration_at_deadline') { $script:elapsed = 179900 }
        if ($scenario -eq 'timeout') { $script:elapsed = 180000; return $false }
        return $true
    }
    $result | Add-Member -MemberType ScriptMethod -Name Kill -Value {$script:kills++}
    $result | Add-Member -MemberType ScriptMethod -Name Dispose -Value {$script:disposed++}
    return $result
}
$failed = $false
try { Install-WebView2Runtime } catch { $failed = $true }
if ($scenario -in @('present','present_user','newer','maximum_major')) {
    if ($failed -or $script:downloads -ne 0 -or $script:starts -ne 0 -or $script:clocks -ne 0) { exit 11 }
} elseif ($scenario -in @('old','partial','extra','leading_zero','preview','zero_major','out_of_range','missing','delayed_registration')) {
    if ($failed -or $script:downloads -ne 1 -or $script:starts -ne 1 -or -not (Test-WebView2Runtime)) { exit 12 }
} else {
    if (-not $failed) { exit 13 }
    if ($scenario -notin @('install_failure','missing_after_install','old_after_install','registration_at_deadline','nonzero_exit_with_runtime','timeout') -and $script:starts -ne 0) { exit 14 }
}
if ($script:disposed -ne $script:starts) { exit 15 }
if ($script:kills -ne $(if ($scenario -eq 'timeout') {1} else {0})) { exit 16 }
if ($script:downloadPath -and (Test-Path -LiteralPath (Split-Path -Parent $script:downloadPath))) { exit 17 }
if ($scenario -eq 'delayed_registration' -and ($script:sleeps -ne 2 -or $script:elapsed -ne 500)) { exit 18 }
if ($scenario -in @('missing_after_install','old_after_install','registration_at_deadline') -and ($script:elapsed -ne 180000 -or $script:webView2Acquisition.reason -ne 'runtime_not_ready')) { exit 19 }
if ($scenario -eq 'nonzero_exit_with_runtime' -and ($script:sleeps -ne 0 -or $script:webView2Acquisition.reason -ne 'bootstrapper_failed')) { exit 20 }
$diagnostic = Get-WebView2AcquisitionLine
if ($diagnostic.Length -gt 256 -or $diagnostic -notmatch '^phase=[a-z_]+;outcome=(pass|fail);reason=[a-z_]+;bootstrapper_exit_code=(none|-?[0-9]+)$') { exit 21 }
if (($script:webView2Acquisition.outcome -eq 'fail') -ne $failed) { exit 22 }
''', encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(harness), "-MinimumVersion", minimum], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows prerequisite result protocol")
@pytest.mark.parametrize("scenario", ["present", "invalid_minimum", "registry_failure", "diagnostic_write_failure"])
def test_webview_acquisition_diagnostics_are_bounded_and_preserve_result_protocol(tmp_path, scenario):
    script = (ROOT / "packaging/windows/ensure-webview2.ps1").read_text(encoding="utf-8")
    minimum = json.loads((ROOT / "shared/desktop-release/prerequisite-policy.json").read_text(encoding="utf-8"))["webview2_minimum_version"]
    boundary = script.index("$resultCode = 0")
    # Exercise the real top-level result writer, replacing registry reads only.
    # Every case finishes before network acquisition; private exception text is
    # deliberately supplied to prove it cannot become observable diagnostics.
    harness = tmp_path / "prerequisite-result.ps1"
    harness.write_text(script[:boundary] + f"\n$scenario = '{scenario}'\n" + r'''
function Get-ItemProperty {
    param($LiteralPath, $Name, $ErrorAction)
    if ($scenario -eq 'registry_failure') { throw 'private exception path or URL must not escape' }
    return [pscustomobject]@{pv=$MinimumVersion}
}
function Invoke-WebRequest { throw 'network must not be reached' }
''' + script[boundary:], encoding="utf-8")
    result_path = tmp_path / "result.txt"
    diagnostic_path = Path(str(result_path) + ".diagnostics")
    if scenario == "diagnostic_write_failure":
        diagnostic_path.mkdir()
    else:
        diagnostic_path.write_text("stale private observation", encoding="ascii")
    supplied_minimum = "not-a-version" if scenario == "invalid_minimum" else minimum
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(harness), "-MinimumVersion", supplied_minimum, "-ResultPath", str(result_path),
    ], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    failed = scenario in {"invalid_minimum", "registry_failure"}
    assert result.returncode == int(failed), result.stdout + result.stderr
    assert result_path.read_bytes() == (b"1" if failed else b"0")
    reason = "minimum_invalid" if scenario == "invalid_minimum" else "probe_failed" if failed else "compatible_present"
    expected = f"phase=probe;outcome={'fail' if failed else 'pass'};reason={reason};bootstrapper_exit_code=none"
    assert result.stdout.strip() == expected
    assert result.stderr == ""
    assert len(expected.encode("ascii")) <= 256
    if scenario == "diagnostic_write_failure":
        assert diagnostic_path.is_dir(), "diagnostic failure must not alter result admission"
    else:
        assert diagnostic_path.read_bytes() == expected.encode("ascii")
    assert not list(tmp_path.glob("*.pending"))


@pytest.mark.skipif(os.name != "nt", reason="Windows build policy consumer")
def test_build_passes_validated_shared_webview_floor_to_the_inno_compiler(tmp_path):
    # Evaluate the actual policy function, assignment and compiler call over a
    # minimal source root. No runtime build or full source copy is necessary.
    harness = tmp_path / "build-policy.ps1"
    harness.write_text(r'''
param([string]$BuildScript, [string]$PolicySource, [string]$ProjectRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($BuildScript, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'build_script_parse_failed' }
$function = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-WebView2MinimumVersion'
}, $true)
$assignment = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left.Extent.Text -eq '$webView2MinimumVersion'
}, $true)
$compile = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq 'Invoke-RequiredCommand' -and
        $node.Extent.Text.Contains('/DWebView2MinimumVersion=')
}, $true)
if (-not $function -or -not $assignment -or -not $compile) { throw 'missing_build_policy_consumer' }
Invoke-Expression $function.Extent.Text
$policy = Get-Content -Raw -LiteralPath $PolicySource | ConvertFrom-Json
$original = [Version]$policy.webview2_minimum_version
$policyPath = Join-Path $ProjectRoot 'shared/desktop-release/prerequisite-policy.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $policyPath) -Force | Out-Null
$compiler = 'fixture-iscc.exe'; $stagingRoot = 'fixture-stage'; $installerOutput = 'fixture-output'
$version = '1.2.3'; $InnoScriptPath = 'fixture.iss'
function Invoke-RequiredCommand { param($FilePath, $Arguments) $script:compileArguments = $Arguments }
foreach ($minimum in @($policy.webview2_minimum_version, "$($original.Major + 1).0.0.0")) {
    $policy.webview2_minimum_version = $minimum
    $policy | ConvertTo-Json | Set-Content -LiteralPath $policyPath
    Invoke-Expression $assignment.Extent.Text
    Invoke-Expression $compile.Extent.Text
    $bound = @($script:compileArguments | Where-Object { $_.StartsWith('/DWebView2MinimumVersion=') })
    if ($bound.Count -ne 1 -or $bound[0] -cne "/DWebView2MinimumVersion=$minimum") { throw 'floor_not_bound_to_compiler' }
}
foreach ($invalid in @('0.1.2.3', '01.2.3.4', '1.2.3', '1.2.3.4.5', '65536.0.0.0', '1.2.3.4 dev')) {
    $policy.webview2_minimum_version = $invalid
    $policy | ConvertTo-Json | Set-Content -LiteralPath $policyPath
    $refused = $false
    try { Invoke-Expression $assignment.Extent.Text } catch { $refused = $true }
    if (-not $refused) { throw 'invalid_floor_accepted' }
}
$policy.webview2_minimum_version = $original.ToString(4)
$policy.schema = 'unexpected-policy'
$policy | ConvertTo-Json | Set-Content -LiteralPath $policyPath
$refused = $false
try { Invoke-Expression $assignment.Extent.Text } catch { $refused = $true }
if (-not $refused) { throw 'invalid_policy_accepted' }
''', encoding="utf-8")
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(harness), "-BuildScript", str(ROOT / "packaging/build-windows.ps1"),
        "-PolicySource", str(ROOT / "shared/desktop-release/prerequisite-policy.json"),
        "-ProjectRoot", str(tmp_path / "source"),
    ], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows Inno version policy consumer")
@pytest.mark.parametrize("minimum_kind", ["shared", "large_major"])
def test_inno_webview_floor_uses_canonical_four_part_numeric_versions(tmp_path, minimum_kind):
    source = (ROOT / "packaging/inno/brain.iss").read_text(encoding="utf-8")
    start = source.index("function CompatibleWebView2Version(")
    end = source.index("function SharedWebView2Available()", start)
    source_minimum = json.loads((ROOT / "shared/desktop-release/prerequisite-policy.json").read_text(encoding="utf-8"))["webview2_minimum_version"]
    minimum = source_minimum if minimum_kind == "shared" else "65534.0.0.0"
    major = int(minimum.split(".")[0])
    cases = {
        minimum: True,
        f"{major + 1}.0.0.0": True,
        "65535.65535.65535.65535": True,
        f"{major - 1}.65535.65535.65535": False,
        f"{major}.0": False,
        minimum + ".1": False,
        "0" + minimum: False,
        minimum + " dev": False,
        "0.65535.65535.65535": False,
        "65536.0.0.0": False,
    }
    if minimum_kind == "large_major":
        cases[source_minimum] = False
    receipt = tmp_path / "version-result.txt"
    checks = "\n".join(
        f"  if CompatibleWebView2Version('{value}') <> {str(expected)} then RaiseException('case_{index}');"
        for index, (value, expected) in enumerate(cases.items())
    )
    # InitializeSetup refuses before any installation, registration or global
    # state change. Only the actual Pascal version predicate is exercised.
    script = tmp_path / "version-probe.iss"
    script.write_text(
        "[Setup]\nAppName=WebView2 Policy Probe\nAppVersion=1.0\n"
        "DefaultDirName={tmp}\\unused-webview-policy-probe\nCreateAppDir=no\n"
        "Uninstallable=no\nPrivilegesRequired=lowest\nOutputBaseFilename=version-probe\n"
        "[Code]\n" + source[start:end] + "\nfunction InitializeSetup(): Boolean;\nbegin\n"
        + checks + f"\n  if not SaveStringToFile('{receipt}', 'versions_verified', False) then RaiseException('receipt_failed');\n"
        "  Result := False;\nend;\n", encoding="utf-8",
    )
    compiler = inno_setup_compiler.require_inno_setup_compiler("Inno Setup is required for the native version policy probe")
    built = subprocess.run([
        str(compiler), f"/O{tmp_path}", f"/DWebView2MinimumVersion={minimum}", str(script),
    ], capture_output=True, text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    assert built.returncode == 0, built.stdout + built.stderr
    log = tmp_path / "version-probe.log"
    ran = subprocess.run([
        str(tmp_path / "version-probe.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
        f"/LOG={log}",
    ], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert receipt.is_file(), f"exit={ran.returncode}\n" + ran.stdout + ran.stderr + log.read_text(encoding="utf-8-sig")
    assert receipt.read_text(encoding="utf-8-sig") == "versions_verified"
