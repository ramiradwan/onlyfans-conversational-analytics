[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BuildPython,
    [Parameter(Mandatory)] [string] $StagingRoot,
    [Parameter(Mandatory)] [string] $PackageProfile,
    [Parameter(Mandatory)] [string] $OutputRoot,
    [string] $WindowsSdkVersion = '10.0.26100.0',
    [string] $MakeAppx = '',
    [string] $PackageUri = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$producer = Join-Path $projectRoot 'tools/msix_packaging.py'
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
if (Test-Path -LiteralPath $OutputRoot) { throw 'MSIX output requires a fresh directory' }
if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) { throw 'Build Python is unavailable' }
if (-not (Test-Path -LiteralPath $PackageProfile -PathType Leaf)) { throw 'An explicit MSIX profile is required' }
if ($WindowsSdkVersion -notmatch '^10\.0\.[0-9]+\.0$') { throw 'Invalid pinned Windows SDK version' }
$sdkRoot = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits/10'
$schemaPath = Join-Path $sdkRoot "Include/$WindowsSdkVersion/winrt/UapManifestSchema_v17.xsd"
if (-not (Test-Path -LiteralPath $schemaPath -PathType Leaf)) {
    throw 'Install a Windows SDK supporting the selected UAP v17 manifest; validation cannot be disabled'
}
if (-not $MakeAppx) { $MakeAppx = Join-Path $sdkRoot "bin/$WindowsSdkVersion/x64/makeappx.exe" }
if (-not (Test-Path -LiteralPath $MakeAppx -PathType Leaf)) { throw 'Pinned MakeAppx is unavailable' }
$tool = Get-Item -LiteralPath $MakeAppx
if ($tool.VersionInfo.FileBuildPart -lt 26100) { throw 'MakeAppx predates the selected manifest features' }

function Invoke-MsixPython {
    param([string[]] $Arguments)
    $result = & $BuildPython $producer @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'MSIX producer refused the requested operation' }
    return ($result -join "`n") | ConvertFrom-Json
}

function Invoke-MakeAppx {
    param([string[]] $Arguments)
    $result = & $MakeAppx @Arguments 2>&1
    $result | Set-Content -LiteralPath (Join-Path $working ($Arguments[0] + '.log')) -Encoding utf8
    $result | Where-Object { "$_" -match 'error:|warning:|succeeded|failed|Packing|manifest for' } |
        Select-Object -First 20 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw 'MakeAppx validation or deployment-container operation failed' }
}

function Get-MsixFileDigest {
    param([string] $FilePath)
    $hasher = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($FilePath)
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hasher.Dispose() }
}

$profile = Invoke-MsixPython -Arguments @('validate-profile', '--profile', $PackageProfile)
$working = Join-Path $OutputRoot 'working'
$prepared = Invoke-MsixPython -Arguments @('prepare', '--stage', $StagingRoot, '--working', $working, '--profile', $PackageProfile)
& (Join-Path $projectRoot 'packaging/write-digests.ps1') -Directory (Join-Path $prepared.payload 'app')
$sealed = Invoke-MsixPython -Arguments @('seal', '--working', $working, '--profile', $PackageProfile, '--makeappx', $MakeAppx)
$stem = "$($profile.identity.name)_$($profile.package_version)_x64"
$packagePath = Join-Path $OutputRoot "$stem.msix"
# Semantic validation is mandatory; never pass /nv. The protected signing job
# receives these final packed contents and never rebuilds them.
Invoke-MakeAppx -Arguments @('pack', '/d', $prepared.payload, '/p', $packagePath)
Invoke-MakeAppx -Arguments @('unpack', '/p', $packagePath, '/d', (Join-Path $working 'unpacked'))
$inventoryPath = Join-Path $OutputRoot "$stem.inventory.json"
$profilePath = Join-Path $OutputRoot "$stem.profile.json"
Copy-Item -LiteralPath $sealed.inventory -Destination $inventoryPath
Copy-Item -LiteralPath $PackageProfile -Destination $profilePath
$verified = Invoke-MsixPython -Arguments @('verify', '--package', $packagePath, '--inventory', $inventoryPath, '--profile', $profilePath)
$null = Invoke-MsixPython -Arguments @('verify-unpacked', '--directory', (Join-Path $working 'unpacked'), '--inventory', $inventoryPath, '--profile', $profilePath)
$receiptPath = Join-Path $OutputRoot "$stem.build.json"
$producerCommit = (& git -C $projectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the producer source revision' }
$producerDirty = [bool]((& git -C $projectRoot status --porcelain) -join "`n")
[ordered]@{
    schema = 'ofca-msix-build/v1'
    verification = $verified
    windows_sdk = $WindowsSdkVersion
    makeappx_file_version = $tool.VersionInfo.FileVersion
    uap17_schema_sha256 = Get-MsixFileDigest -FilePath $schemaPath
    producer_source = [ordered]@{ base_commit = $producerCommit; dirty = $producerDirty }
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding utf8
$appInstallerPath = $null
if ($PackageUri) {
    $appInstallerPath = Join-Path $OutputRoot "$stem.appinstaller"
    $null = Invoke-MsixPython -Arguments @('appinstaller', '--profile', $profilePath, '--package-uri', $PackageUri, '--output', $appInstallerPath)
}
[pscustomobject]@{
    PackagePath = $packagePath
    InventoryPath = $inventoryPath
    ProfilePath = $profilePath
    BuildReceiptPath = $receiptPath
    AppInstallerPath = $appInstallerPath
}
