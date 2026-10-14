"""Native payload and prerequisite failure behavior at the packaging boundary."""
import json
import os
import re
from pathlib import Path
import subprocess

import pytest

from test_packaging_policy import _stage_runtime_tree
from tools.packaging_policy import verify_runtime_files

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.windows_compat]


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


@pytest.mark.parametrize("relative", ["brain-fixture.exe", "WebView/msedgewebview2.exe"])
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


@pytest.mark.skipif(os.name != "nt", reason="Windows prerequisite script")
@pytest.mark.parametrize("scenario", ["present", "missing", "untrusted", "wrong_publisher", "download_failure", "install_failure", "missing_after_install", "timeout"])
def test_shared_webview_prerequisite_skips_present_and_refuses_failed_install(tmp_path, scenario):
    script = (ROOT / "packaging/windows/ensure-webview2.ps1").read_text(encoding="utf-8")
    # Run the real installer orchestration with OS/download boundaries replaced.
    # No network or machine-wide runtime changes belong in this unit test.
    definitions = script[:script.index("$resultCode = 0")]
    harness = tmp_path / "prerequisite.ps1"
    harness.write_text(definitions + f"\n$scenario = '{scenario}'\n" + r'''
$script:installed = $scenario -eq 'present'
$script:downloads = 0
$script:starts = 0
$script:kills = 0
$script:disposed = 0
function Test-WebView2Runtime { return $script:installed }
function Invoke-WebRequest {
    param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
    $script:downloads++
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
    $script:installed = $scenario -notin @('install_failure','missing_after_install','timeout')
    $result = [pscustomobject]@{ExitCode=$(if ($scenario -eq 'install_failure') {1} else {0});HasExited=$scenario -ne 'timeout'}
    $result | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {param($timeout) return $scenario -ne 'timeout' -or $script:kills -gt 0}
    $result | Add-Member -MemberType ScriptMethod -Name Kill -Value {$script:kills++}
    $result | Add-Member -MemberType ScriptMethod -Name Dispose -Value {$script:disposed++}
    return $result
}
$failed = $false
try { Install-WebView2Runtime } catch { $failed = $true }
if ($scenario -eq 'present') {
    if ($failed -or $script:downloads -ne 0 -or $script:starts -ne 0) { exit 11 }
} elseif ($scenario -eq 'missing') {
    if ($failed -or $script:downloads -ne 1 -or $script:starts -ne 1 -or -not $script:installed) { exit 12 }
} else {
    if (-not $failed) { exit 13 }
    if ($scenario -notin @('install_failure','missing_after_install','timeout') -and $script:starts -ne 0) { exit 14 }
}
if ($script:disposed -ne $script:starts) { exit 15 }
if ($script:kills -ne $(if ($scenario -eq 'timeout') {1} else {0})) { exit 16 }
''', encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(harness)], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
