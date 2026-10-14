[CmdletBinding()]
param([Parameter(Mandatory)] [ValidateSet('inno','msix-direct','msix-store', IgnoreCase=$false)] [string] $Target,
      [Parameter(Mandatory)] [string] $Directory, [Parameter(Mandatory)] [string] $ProductRevision)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Target -ceq 'inno') { return }
$sdk = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits/10/bin/10.0.26100.0/x64'
if (-not [IO.File]::Exists((Join-Path $sdk 'signtool.exe'))) { throw 'Pinned verification SDK is unavailable' }
Import-Module (Join-Path $PSScriptRoot 'msix/release-artifacts.psm1') -Force
$record = Get-MsixReleaseCandidate $Directory $Target $ProductRevision -BeforeSigning -CheckEnvironment
if ($Target -ceq 'msix-direct') {
    $values = [ordered]@{}
    foreach ($name in @('MSIX_DIRECT_PUBLISHER','MSIX_DIRECT_EXPECTED_FAMILY','MSIX_DIRECT_APPINSTALLER_URI',
        'DESKTOP_R2_BUCKET','DESKTOP_R2_ENDPOINT','DESKTOP_UPDATES_BASE_URL')) {
        $values[$name] = [Environment]::GetEnvironmentVariable($name)
    }
    $resolved = [ordered]@{schema='ofca-direct-release-environment/v1';source_commit=$ProductRevision;
        profile_sha256=$record.profile_sha256;values=$values}
    [IO.File]::WriteAllText((Join-Path $Directory 'desktop.resolved-direct-inputs.json'),
        ($resolved | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
}
Write-Host 'Immutable package, source and approved release inputs agree'
