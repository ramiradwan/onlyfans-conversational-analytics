param($Wrapper, $Python, $Project, $Output, $Selected, $Bindings, $Case)
$ErrorActionPreference = 'Stop'
$env:BRAIN_PROJECT_ROOT = $Project
$receipt = Join-Path $Project 'forwarding.txt'
function Record-Input($Consumer, $Snapshot) {
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha256.ComputeHash([IO.File]::ReadAllBytes($Snapshot))).Replace('-', '').ToLowerInvariant() }
    finally { $sha256.Dispose() }
    [IO.File]::AppendAllText($receipt, "$Consumer|$Snapshot|$hash`n")
}
function powershell.exe {
    function Get-Command {
        [CmdletBinding()]
        param([string] $Name)
        if ($Name -eq 'cargo.exe') { return [pscustomobject]@{ Source = $fixtureCargo } }
        if ($Name -eq 'link.exe') { return $true }
        Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
    }
    $file = [array]::IndexOf($args, '-File')
    $native = $args[$file + 1]
    $arguments = $args[($file + 2)..($args.Length - 1)]
    $parameters = @{}
    for ($i = 0; $i -lt $arguments.Length; $i += 2) {
        $parameters[$arguments[$i].Substring(1)] = $arguments[$i + 1]
    }
    & $native @parameters
    $global:LASTEXITCODE = 0
}
function npm.cmd { $global:LASTEXITCODE = 0 }
function git { $global:LASTEXITCODE = 0; return ('a' * 40) }
function node.exe {
    $argument = $args | Where-Object { $_ -like '--customer-release-config=*' }
    $snapshot = $argument.Split('=', 2)[1]
    Record-Input $args[1] $snapshot
    if ($args[1] -eq '--package') {
        $dist = Join-Path $Project 'extension/dist'
        New-Item -ItemType Directory -Path $dist | Out-Null
        [IO.File]::WriteAllText((Join-Path $dist 'build-meta.json'),
            '{"schema":"fixture","extension_version":"1.2.3","signing_parameters":{},"legal_bindings":{},"privacy_policy_configured":true}')
        [IO.File]::WriteAllText((Join-Path $dist 'conversation-analytics-1.2.3.zip'), 'fixture')
    }
    $global:LASTEXITCODE = 0
}
$freeze = Join-Path $Project 'fixture-freeze.ps1'
@'
Record-Input 'brain' $env:BRAIN_BUILD_CUSTOMER_RELEASE_CONFIG
$dist = $args[([array]::IndexOf($args, '--distpath') + 1)]
$stage = Join-Path $dist 'Brain/_internal/app/core'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$staged = Join-Path $stage 'customer-release.json'
[IO.File]::Copy($env:BRAIN_BUILD_CUSTOMER_RELEASE_CONFIG, $staged)
if ($Case -eq 'changed-brain') { [IO.File]::AppendAllText($staged, ' ') }
[IO.File]::WriteAllText((Join-Path $dist 'Brain/Brain.exe'), 'fixture')
$global:LASTEXITCODE = 0
'@ | Set-Content -LiteralPath $freeze -Encoding ascii
$fixtureCargo = Join-Path $Project 'fixture-cargo.ps1'
@'
Record-Input 'native' $env:BRIDGE_BUILD_CUSTOMER_RELEASE_CONFIG
$target = $args[([array]::IndexOf($args, '--target-dir') + 1)]
$stage = Join-Path $target 'x86_64-pc-windows-msvc/release'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $stage 'Bridge.exe'), 'fixture')
$global:LASTEXITCODE = 0
'@ | Set-Content -LiteralPath $fixtureCargo -Encoding ascii
$compiler = Join-Path $Project 'fixture-inno.ps1'
@'
$directory = ($args | Where-Object { $_ -like '/DOutputRoot=*' }).Split('=', 2)[1]
[IO.File]::WriteAllText((Join-Path $directory 'OnlyFans-Conversational-Analytics-Setup-1.2.3-x64.exe'), 'fixture')
$global:LASTEXITCODE = 0
'@ | Set-Content -LiteralPath $compiler -Encoding ascii
$selection = @{}
if ($Case -notin @('default', 'prebuilt-empty')) { $selection.CustomerReleaseConfig = $Selected }
if ($Case -in @('prebuilt', 'prebuilt-empty')) { $selection.SkipAssetBuild = $true }
if ($Case -eq 'prebuilt-empty') {
    $stage = Join-Path $Project 'desktop/src-tauri/target/x86_64-pc-windows-msvc/release'
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $stage 'Bridge.exe'), 'fixture')
}
& $Wrapper -BuildPython $Python -OutputRoot $Output -PyInstallerExecutable $freeze -InnoSetupCompiler $compiler `
    -LegalReleaseBindings $Bindings -PrivacyPolicyUrl 'https://legal.example.com/privacy' @selection
