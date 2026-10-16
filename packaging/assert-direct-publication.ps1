# Reverify final MSIX bytes before the credential-bearing uploader runs.
[CmdletBinding()]
param([Parameter(Mandatory)] [string] $Directory,
      [Parameter(Mandatory)] [string] $ProductRevision,
      [Parameter(Mandatory)] [string] $FreshVerificationPath,
      [Parameter(Mandatory)] [string] $AdmissionPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'msix/release-artifacts.psm1') -Force
$candidate = Get-MsixReleaseCandidate $Directory 'msix-direct' $ProductRevision -CheckEnvironment
$finalPath = Join-Path $Directory 'desktop.artifact-release.json'
$final = Read-ReleaseJson $finalPath 32768
$keys = @('schema','source_commit','source_tree','target','application_version','package_version',
    'package_file','package_sha256','profile_sha256','inventory_sha256','build_sha256','expected_family',
    'verification_sha256','signature_scope','agent_file','agent_sha256','resolved_direct_inputs_sha256','descriptor_sha256')
if ($final.Count -ne $keys.Count -or @($final.Keys | Where-Object { $_ -cnotin $keys }).Count -or
    $final.schema -cne 'ofca-desktop-artifact-release/v1' -or $final.target -cne 'msix-direct' -or
    $final.signature_scope -cne 'trusted-timestamped') { throw 'Require the closed final direct release record' }
foreach ($name in @('source_commit','source_tree','target','application_version','package_version','package_file',
    'profile_sha256','inventory_sha256','build_sha256','expected_family','agent_file','agent_sha256')) {
    if ($final[$name] -cne $candidate[$name]) { throw 'Final direct release differs from its immutable candidate' }
}
foreach ($pair in @(
    @($candidate.package_file,'package_sha256'), @('desktop.verification.json','verification_sha256'),
    @('desktop.resolved-direct-inputs.json','resolved_direct_inputs_sha256'), @('ConversationAnalytics.appinstaller','descriptor_sha256'))) {
    if ($final[$pair[1]] -cnotmatch '^[0-9a-f]{64}$' -or
        (Get-ReleaseHash (Join-Path $Directory $pair[0])) -cne $final[$pair[1]]) { throw 'Final direct release bytes changed' }
}
# Use the existing closed XML validator after substituting only the final hash
# back to its template sentinel. This validates structure, policy and exact URI.
$descriptorPath = Join-Path $Directory 'ConversationAnalytics.appinstaller'
$bytes = [IO.File]::ReadAllBytes($descriptorPath)
if ($bytes.Length -gt 16384) { throw 'Direct descriptor exceeds its bound' }
$text = [Text.UTF8Encoding]::new($false,$true).GetString($bytes)
$delivery = Read-ReleaseJson (Join-Path $PSScriptRoot 'msix/direct-delivery.json') 16384
$prefix = $delivery.hosting.public_origin + $delivery.hosting.base_path + 'packages/'
$finalUri = $prefix + $final.package_sha256 + '/' + $final.package_file
$sentinelUri = $prefix + ('0' * 64) + '/' + $final.package_file
if ([regex]::Matches($text,[regex]::Escape($finalUri)).Count -ne 1) { throw 'Direct descriptor must carry one final package URI' }
$normalized = [Text.UTF8Encoding]::new($false).GetBytes($text.Replace($finalUri,$sentinelUri))
$temporary = [IO.Path]::GetFullPath($FreshVerificationPath + '.descriptor.xml')
if ([IO.File]::Exists($temporary) -or [IO.File]::Exists($FreshVerificationPath) -or [IO.File]::Exists($AdmissionPath)) {
    throw 'Publication admission requires fresh output paths'
}
$created = $false
try {
    $stream = [IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    $created = $true
    try { $stream.Write($normalized) } finally { $stream.Dispose() }
    $profile = Read-ReleaseJson (Join-Path $Directory 'desktop.profile.json') 16384
    $normalizedHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($normalized)).ToLowerInvariant()
    $null = Read-DirectAppInstallerTemplate $temporary $profile $final.package_version $final.package_file $normalizedHash
    & (Join-Path $PSScriptRoot 'verify-msix.ps1') -Package (Join-Path $Directory $final.package_file) `
        -Inventory (Join-Path $Directory 'desktop.inventory.json') -PackageProfile (Join-Path $Directory 'desktop.profile.json') `
        -ExpectedFamilyName $final.expected_family -VerificationMode DirectSigned -ReceiptPath $FreshVerificationPath
    if ($LASTEXITCODE -ne 0) { throw 'Independent final MSIX verification refused publication' }
    $fresh = Read-ReleaseJson $FreshVerificationPath 32768
    if ($fresh.package_sha256 -cne $final.package_sha256 -or $fresh.signature -cne 'trusted-timestamped' -or
        $fresh.purpose -cne 'production' -or $fresh.distribution -cne 'direct-appinstaller') { throw 'Publication signature scope differs' }
    $admission = [ordered]@{
        schema='ofca-direct-publication-admission/v1'; source_commit=$ProductRevision; source_tree=$final.source_tree
        artifact_record_sha256=(Get-ReleaseHash $finalPath); package_sha256=$fresh.package_sha256
        descriptor_sha256=$final.descriptor_sha256; verification_sha256=(Get-ReleaseHash $FreshVerificationPath)
        signature='trusted-timestamped'; identity=$fresh.identity; direct_delivery_sha256=$candidate.direct_delivery_sha256
    }
    [IO.File]::WriteAllText([IO.Path]::GetFullPath($AdmissionPath),($admission | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
} finally {
    if ($created -and [IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
}
Write-Host 'Final direct package and closed descriptor admitted for protected publication'
