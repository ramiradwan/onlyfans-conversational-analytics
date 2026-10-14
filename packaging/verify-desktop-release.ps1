# Protected owner: verifies signed bytes and writes only outside-package metadata.
[CmdletBinding()]
param([Parameter(Mandatory)] [ValidateSet('inno','msix-direct','msix-store', IgnoreCase=$false)] [string] $Target,
      [Parameter(Mandatory)] [string] $Directory, [Parameter(Mandatory)] [string] $ProductRevision)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Target -ceq 'inno') {
    & (Join-Path $PSScriptRoot 'verify-signatures.ps1') -Path $Directory
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    return
}
Import-Module (Join-Path $PSScriptRoot 'msix/release-artifacts.psm1') -Force
$record = Get-MsixReleaseCandidate $Directory $Target $ProductRevision -CheckEnvironment
$mode = if ($Target -ceq 'msix-direct') { 'DirectSigned' } else { 'StoreSubmission' }
$receiptPath = Join-Path $Directory 'desktop.verification.json'
& (Join-Path $PSScriptRoot 'verify-msix.ps1') -Package (Join-Path $Directory $record.package_file) `
    -Inventory (Join-Path $Directory 'desktop.inventory.json') -PackageProfile (Join-Path $Directory 'desktop.profile.json') `
    -ExpectedFamilyName $record.expected_family -VerificationMode $mode -ReceiptPath $receiptPath
if ($LASTEXITCODE -ne 0) { throw 'Final MSIX verification refused' }
$verified = Read-ReleaseJson $receiptPath 32768
if ($verified.package_sha256 -cne (Get-ReleaseHash (Join-Path $Directory $record.package_file))) { throw 'Verified package changed' }
if ($Target -ceq 'msix-direct') {
    $delivery = Read-ReleaseJson (Join-Path $PSScriptRoot 'msix/direct-delivery.json') 16384
    $packageUri = $delivery.hosting.public_origin + $delivery.hosting.base_path + 'packages/' + $verified.package_sha256 + '/' + $record.package_file
    $profile = Read-ReleaseJson (Join-Path $Directory 'desktop.profile.json') 16384
    $document = Read-DirectAppInstallerTemplate (Join-Path $Directory 'desktop.appinstaller-template.xml') $profile `
        $record.package_version $record.package_file $record.appinstaller_template_sha256
    $namespaces = [Xml.XmlNamespaceManager]::new($document.NameTable)
    $namespaces.AddNamespace('a','http://schemas.microsoft.com/appx/appinstaller/2018')
    $package = $document.SelectSingleNode('/a:AppInstaller/a:MainPackage',$namespaces)
    $package.SetAttribute('Uri',$packageUri)
    $writerSettings = [Xml.XmlWriterSettings]::new()
    $writerSettings.Encoding = [Text.UTF8Encoding]::new($false)
    $writer = [Xml.XmlWriter]::Create((Join-Path $Directory 'ConversationAnalytics.appinstaller'),$writerSettings)
    try { $document.Save($writer) } finally { $writer.Dispose() }
}
$final = [ordered]@{
    schema='ofca-desktop-artifact-release/v1'; source_commit=$ProductRevision; source_tree=$record.source_tree; target=$Target
    application_version=$record.application_version; package_version=$record.package_version
    package_file=$record.package_file; package_sha256=$verified.package_sha256
    profile_sha256=$record.profile_sha256; inventory_sha256=$record.inventory_sha256
    build_sha256=$record.build_sha256; expected_family=$record.expected_family
    verification_sha256=(Get-ReleaseHash $receiptPath); signature_scope=$verified.signature
    agent_file=$record.agent_file; agent_sha256=$record.agent_sha256
    resolved_direct_inputs_sha256=$(if ($Target -ceq 'msix-direct') { Get-ReleaseHash (Join-Path $Directory 'desktop.resolved-direct-inputs.json') } else { $null })
    descriptor_sha256=$(if ($Target -ceq 'msix-direct') { Get-ReleaseHash (Join-Path $Directory 'ConversationAnalytics.appinstaller') } else { $null })
}
[IO.File]::WriteAllText((Join-Path $Directory 'desktop.artifact-release.json'),($final | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
Write-Host 'Final package verified; final signed hash and channel metadata recorded'
