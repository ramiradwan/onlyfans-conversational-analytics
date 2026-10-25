[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BuildPython,
    [Parameter(Mandatory)] [string] $StagingRoot,
    [Parameter(Mandatory)] [string] $PackageProfile,
    [Parameter(Mandatory)] [string] $OutputRoot,
    [string] $WindowsSdkVersion = '10.0.26100.0',
    [string] $MakeAppx = '',
    [string] $PackageUri = '',
    [string] $EngineBundle = '',
    [string] $EngineAcquisitionQuoteInputs = ''
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
$profileDocument = Get-Content -Raw -LiteralPath $PackageProfile | ConvertFrom-Json
$v2 = $profileDocument.schema -ceq 'ofca-msix-package-profile/v2'
if (-not $v2 -and $EngineBundle) { throw 'V1 release cannot carry an optional Engine artifact' }
$working = Join-Path $OutputRoot 'working'
$prepareArguments = @('prepare', '--stage', $StagingRoot, '--working', $working, '--profile', $PackageProfile)
if ($EngineAcquisitionQuoteInputs) {
    if (-not $EngineBundle) { throw 'Engine quote requires its independently admitted final signed bundle' }
    $prepareArguments += @('--engine-acquisition-quote-inputs', $EngineAcquisitionQuoteInputs, '--engine-bundle', $EngineBundle)
}
$prepared = Invoke-MsixPython -Arguments $prepareArguments
& (Join-Path $projectRoot 'packaging/write-digests.ps1') -Directory (Join-Path $prepared.payload 'app')
$sealed = Invoke-MsixPython -Arguments @('seal', '--working', $working, '--profile', $PackageProfile, '--makeappx', $MakeAppx)
$stem = "$($profile.identity.name)_$($profile.package_version)_x64"
$packagePath = Join-Path $OutputRoot "$stem.msix"
if ($v2) { $packagePath = Join-Path $OutputRoot ("ConversationAnalytics_" + $profile.package_version + '_x64.msix') }
# Semantic validation is mandatory; never pass /nv. The protected signing job
# receives these final packed contents and never rebuilds them.
Invoke-MakeAppx -Arguments @('pack', '/d', $prepared.payload, '/p', $packagePath)
Invoke-MakeAppx -Arguments @('unpack', '/p', $packagePath, '/d', (Join-Path $working 'unpacked'))
$inventoryPath = Join-Path $OutputRoot "$stem.inventory.json"
$profilePath = Join-Path $OutputRoot "$stem.profile.json"
Copy-Item -LiteralPath $sealed.inventory -Destination $inventoryPath
Copy-Item -LiteralPath $PackageProfile -Destination $profilePath
$verifyArguments = @('verify', '--package', $packagePath, '--inventory', $inventoryPath, '--profile', $profilePath)
if ($EngineAcquisitionQuoteInputs) { $verifyArguments += @('--engine-bundle', $EngineBundle) }
$verified = Invoke-MsixPython -Arguments $verifyArguments
$null = Invoke-MsixPython -Arguments @('verify-unpacked', '--directory', (Join-Path $working 'unpacked'), '--inventory', $inventoryPath, '--profile', $profilePath)
$artifactSet = $null
$artifactSetPath = Join-Path $OutputRoot "$stem.artifact-set.json"
if ($v2) {
    $artifactArguments = @('artifact-set','--profile',$profilePath,'--output',$artifactSetPath)
    if ($profileDocument.servicing.installed_artifact.kind -ceq 'related_bundle') {
        if (-not $EngineBundle) { throw 'Related release requires an explicit external Engine bundle' }
        $engineVersion = $profileDocument.servicing.installed_artifact.engine.package_version
        $engineName = 'TextAnalysisEngine_' + $engineVersion + '_x64.msixbundle'
        $enginePath = Join-Path $OutputRoot $engineName
        Copy-Item -LiteralPath $EngineBundle -Destination $enginePath
        if ($EngineAcquisitionQuoteInputs) {
            $null = Invoke-MsixPython -Arguments @('verify', '--package', $packagePath, '--inventory', $inventoryPath,
                '--profile', $profilePath, '--engine-bundle', $enginePath)
        }
        $mapping = Join-Path $working 'related.map'
        $null = Invoke-MsixPython -Arguments @('related-mapping','--profile',$profilePath,'--engine-bundle',$enginePath,'--output',$mapping)
        $bundleInputs = Join-Path $working 'bundle-inputs'
        $null = New-Item -ItemType Directory -Path $bundleInputs
        Copy-Item -LiteralPath $packagePath -Destination (Join-Path $bundleInputs ([IO.Path]::GetFileName($packagePath)))
        Copy-Item -LiteralPath $enginePath -Destination (Join-Path $bundleInputs $engineName)
        $bundlePath = Join-Path $OutputRoot ('ConversationAnalytics_' + $profile.package_version + '_x64.msixbundle')
        Push-Location $bundleInputs
        try { Invoke-MakeAppx -Arguments @('bundle','/f',$mapping,'/p',$bundlePath,'/bv',$profile.package_version) }
        finally { Pop-Location }
        $packagePath = $bundlePath
        $artifactArguments += @('--engine-bundle',$enginePath)
    } elseif ($EngineBundle) { throw 'MainPackage profile cannot acquire an Engine artifact' }
    $null = Invoke-MsixPython -Arguments ($artifactArguments + @('--package',$packagePath))
    $artifactSet = Get-Content -Raw -LiteralPath $artifactSetPath | ConvertFrom-Json
    # Finalize the external unsigned byte inventory after packing. The sealed
    # payload file closure stays unchanged; signatures have a separate record.
    $packedInventory = Get-Content -Raw -LiteralPath $inventoryPath | ConvertFrom-Json
    $packedInventory | Add-Member -NotePropertyName artifact_set -NotePropertyValue $artifactSet
    $packedInventory | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath $inventoryPath -Encoding utf8
    $verified.package_sha256 = Get-MsixFileDigest $packagePath
    $verified.inventory_sha256 = Get-MsixFileDigest $inventoryPath
}
$receiptPath = Join-Path $OutputRoot "$stem.build.json"
$producerCommit = (& git -C $projectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the producer source revision' }
$producerDirty = [bool]((& git -C $projectRoot status --porcelain) -join "`n")
$buildReceipt = [ordered]@{
    schema = 'ofca-msix-build/v1'
    verification = $verified
    windows_sdk = $WindowsSdkVersion
    makeappx_file_version = $tool.VersionInfo.FileVersion
    uap17_schema_sha256 = Get-MsixFileDigest -FilePath $schemaPath
    producer_source = [ordered]@{ base_commit = $producerCommit; dirty = $producerDirty }
}
if ($v2) { $buildReceipt.schema = 'ofca-msix-build/v2'; $buildReceipt.artifact_set = $artifactSet }
$buildReceipt | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $receiptPath -Encoding utf8
$appInstallerPath = $null
if ($v2 -and $profileDocument.distribution -ceq 'direct-appinstaller') {
    $templateSet = Get-Content -Raw -LiteralPath $artifactSetPath | ConvertFrom-Json
    $templateSet.core.sha256 = '0' * 64
    if ($templateSet.kind -ceq 'related_bundle') { $templateSet.engine.artifact.sha256 = '0' * 64 }
    $templateSetPath = Join-Path $working 'template-artifact-set.json'
    $templateSet | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $templateSetPath -Encoding utf8
    $appInstallerPath = Join-Path $OutputRoot "$stem.appinstaller"
    $null = Invoke-MsixPython -Arguments @('release-appinstaller','--profile',$profilePath,'--artifact-set',$templateSetPath,'--output',$appInstallerPath)
} elseif ($PackageUri) {
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
