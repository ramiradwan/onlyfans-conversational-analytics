# Build owner: run only after the common complete runtime's qualification.
[CmdletBinding()]
param([Parameter(Mandatory)] [ValidateSet('msix-direct','msix-store', IgnoreCase=$false)] [string] $Target,
      [Parameter(Mandatory)] [string] $BuildRoot,
      [Parameter(Mandatory)] [string] $BuildPython,
      [Parameter(Mandatory)] [string] $ProductRevision)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'msix/release-artifacts.psm1') -Force
$configuration = Join-Path $PSScriptRoot 'msix'
$profileName = if ($Target -ceq 'msix-direct') { 'production-direct.json' } else { 'production-store.json' }
$profile = Read-ReleaseJson (Join-Path $configuration ('profiles/' + $profileName)) 16384
$runtime = Join-Path $BuildRoot 'dist/Brain'
$release = Read-ReleaseJson (Join-Path $runtime 'release-manifest.json') 16384
if ($ProductRevision -cnotmatch '^[0-9a-f]{40}$' -or $release.source_commit -cne $ProductRevision) { throw 'Runtime source differs from dispatch revision' }
& (Join-Path $PSScriptRoot 'verify-pe-manifests.ps1') -Path @((Join-Path $runtime 'Brain.exe'),(Join-Path $runtime 'Bridge.exe'))
if ($LASTEXITCODE -ne 0) { throw 'Runtime PE manifest verification failed' }
$output = Join-Path $BuildRoot 'msix'
$descriptorArguments = @{}
if ($Target -ceq 'msix-direct') {
    $delivery = Read-ReleaseJson (Join-Path $configuration 'direct-delivery.json') 16384
    $version = '1.{0}.{1}.0' -f [Math]::Floor($profile.release_sequence / 65536), ($profile.release_sequence % 65536)
    $filename = 'ConversationAnalytics_' + $version + '_x64.msix'
    $descriptorArguments.PackageUri = $delivery.hosting.public_origin + $delivery.hosting.base_path + 'packages/' + ('0' * 64) + '/' + $filename
}
& (Join-Path $PSScriptRoot 'build-msix.ps1') -BuildPython $BuildPython -StagingRoot $runtime `
    -PackageProfile (Join-Path $configuration ('profiles/' + $profileName)) -OutputRoot $output @descriptorArguments
if ($LASTEXITCODE -ne 0) { throw 'MSIX release construction failed' }
$packages = @(Get-ChildItem -LiteralPath $output -Filter '*.msix' -File)
$agents = @(Get-ChildItem -LiteralPath (Join-Path $BuildRoot 'installer') -Filter '*-chrome.zip' -File)
if ($packages.Count -ne 1 -or $agents.Count -ne 1) { throw 'Require one complete package and one qualified Agent artifact' }
$stem = [IO.Path]::GetFileNameWithoutExtension($packages[0].Name)
$inventory = Read-ReleaseJson (Join-Path $output ($stem + '.inventory.json'))
$directory = Join-Path $BuildRoot 'msix-release'
if (Test-Path -LiteralPath $directory) { throw 'Require a fresh MSIX release directory' }
$null = New-Item -ItemType Directory -Path $directory
$filename = 'ConversationAnalytics_' + $inventory.package_version + '_x64.msix'
Copy-Item -LiteralPath $packages[0].FullName -Destination (Join-Path $directory $filename)
Copy-Item -LiteralPath $agents[0].FullName -Destination (Join-Path $directory $agents[0].Name)
foreach ($kind in @('profile','inventory','build')) {
    Copy-Item -LiteralPath (Join-Path $output ($stem + '.' + $kind + '.json')) -Destination (Join-Path $directory ('desktop.' + $kind + '.json'))
}
$directHash = $null
$templateHash = $null
$family = (Read-ReleaseJson (Join-Path $configuration 'store-reservation.json') 16384).package_family_name
if ($Target -ceq 'msix-direct') {
    Copy-Item -LiteralPath (Join-Path $output ($stem + '.appinstaller')) -Destination (Join-Path $directory 'desktop.appinstaller-template.xml')
    $templateHash = Get-ReleaseHash (Join-Path $directory 'desktop.appinstaller-template.xml')
    $directHash = Get-ReleaseHash (Join-Path $configuration 'direct-delivery.json')
    $family = $delivery.expected_package_family
}
$tree = (& git -C (Split-Path -Parent $PSScriptRoot) rev-parse 'HEAD^{tree}').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot freeze source tree coordinate' }
$record = [ordered]@{
    schema='ofca-msix-release-inputs/v1'; target=$Target; source_commit=$ProductRevision; source_tree=$tree
    package_file=$filename; package_version=$inventory.package_version; application_version=$inventory.application_version
    profile_sha256=(Get-ReleaseHash (Join-Path $directory 'desktop.profile.json'))
    inventory_sha256=(Get-ReleaseHash (Join-Path $directory 'desktop.inventory.json'))
    build_sha256=(Get-ReleaseHash (Join-Path $directory 'desktop.build.json'))
    unsigned_package_sha256=(Get-ReleaseHash (Join-Path $directory $filename))
    agent_file=$agents[0].Name; agent_sha256=(Get-ReleaseHash (Join-Path $directory $agents[0].Name))
    expected_family=$family; direct_delivery_sha256=$directHash; appinstaller_template_sha256=$templateHash
}
[IO.File]::WriteAllText((Join-Path $directory 'desktop.release-inputs.json'),($record | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
$null = Get-MsixReleaseCandidate $directory $Target $ProductRevision -BeforeSigning
Write-Host 'MSIX release handoff sealed; signing remains a separate owner'
