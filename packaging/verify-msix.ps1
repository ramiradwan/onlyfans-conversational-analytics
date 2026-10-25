#Requires -Version 7.4
<#
Read-only verification of one MSIX and its sealed build inventory. No package
registration, application build, dependency installation or content mutation.
Direct release requires SignTool's trusted, timestamped signature verification.
StoreSubmission verifies the unsigned submission, not Store-delivered bytes.
Qualification must be requested explicitly and cannot become production proof.
#>
[CmdletBinding()]
param(
    [string] $Package = '',
    [string] $Inventory = '',
    [string] $PackageProfile = '',
    [ValidateSet('DirectSigned', 'StoreSubmission')] [string] $VerificationMode = 'DirectSigned',
    [switch] $AllowQualification,
    [string] $ExpectedFamilyName = '',
    [string] $SignTool = '',
    [string] $ReceiptPath = '',
    [string] $EngineBundle = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$msixConfiguration = Join-Path $PSScriptRoot 'msix'
Import-Module (Join-Path $msixConfiguration 'release-artifacts.psm1') -Force

function Get-ByteDigest {
    param([byte[]] $Bytes)
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Bytes)).ToLowerInvariant()
}

function Get-StreamDigest {
    param([IO.Stream] $Stream)
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Stream)).ToLowerInvariant()
}

function Read-BoundedJson {
    param([string] $FilePath, [long] $Maximum)
    if ([IO.FileInfo]::new($FilePath).Length -gt $Maximum) { throw 'JSON input exceeds its bound' }
    $bytes = [IO.File]::ReadAllBytes($FilePath)
    return Convert-CheckedJson $bytes $Maximum
}

function Convert-CheckedJson {
    param([byte[]] $Bytes, [long] $Maximum)
    $bytes = $Bytes
    if ($bytes.Length -eq 0 -or $bytes.Length -gt $Maximum) { throw 'JSON input exceeds its bound' }
    $text = [Text.UTF8Encoding]::new($false, $true).GetString($bytes).TrimStart([char]0xfeff)
    $document = [System.Text.Json.JsonDocument]::Parse($text)
    try {
        $pending = [Collections.Generic.Stack[System.Text.Json.JsonElement]]::new()
        $pending.Push($document.RootElement)
        while ($pending.Count) {
            $element = $pending.Pop()
            if ($element.ValueKind -eq [System.Text.Json.JsonValueKind]::Object) {
                $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                foreach ($member in $element.EnumerateObject()) {
                    if (-not $names.Add($member.Name)) { throw 'Duplicate JSON member' }
                    $pending.Push($member.Value)
                }
            } elseif ($element.ValueKind -eq [System.Text.Json.JsonValueKind]::Array) {
                foreach ($member in $element.EnumerateArray()) { $pending.Push($member) }
            }
        }
    } finally { $document.Dispose() }
    return $text | ConvertFrom-Json -AsHashtable -Depth 64
}

function Assert-ClosedObject {
    param($Object, [string[]] $Fields)
    if ($Object -isnot [Collections.IDictionary] -or $Object.Count -ne $Fields.Count) { throw 'Invalid object fields' }
    foreach ($key in $Object.Keys) { if ($key -cnotin $Fields) { throw 'Unexpected object field' } }
}

function Convert-Version {
    param($Value)
    if ($Value -isnot [string] -or $Value -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
        throw 'Invalid canonical Windows version'
    }
    $version = [Version]$Value
    if (@($version.Major, $version.Minor, $version.Build, $version.Revision | Where-Object { $_ -gt 65535 }).Count) {
        throw 'Windows version component exceeds 65535'
    }
    return $version
}

function Read-ZipBytes {
    param([IO.Compression.ZipArchiveEntry] $Entry, [long] $Maximum)
    if ($null -eq $Entry -or $Entry.Length -gt $Maximum) { throw 'Missing or oversized package metadata' }
    $stream = $Entry.Open()
    $buffer = [IO.MemoryStream]::new()
    try { $stream.CopyTo($buffer); return ,$buffer.ToArray() }
    finally { $stream.Dispose(); $buffer.Dispose() }
}

function Read-SafeXml {
    param([byte[]] $Bytes)
    $settings = [Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $settings.MaxCharactersInDocument = 1048576
    $stream = [IO.MemoryStream]::new($Bytes, $false)
    $reader = [Xml.XmlReader]::Create($stream, $settings)
    $document = [Xml.XmlDocument]::new()
    $document.XmlResolver = $null
    try { $document.Load($reader); return ,$document }
    finally { $reader.Dispose(); $stream.Dispose() }
}

function Get-XmlShape {
    param([Xml.XmlNode] $Node)
    $attributes = [Collections.Generic.List[object]]::new()
    foreach ($attribute in $Node.Attributes | Sort-Object NamespaceURI, LocalName) {
        if ($attribute.NamespaceURI -ne 'http://www.w3.org/2000/xmlns/') {
            $attributes.Add(@($attribute.NamespaceURI, $attribute.LocalName, $attribute.Value))
        }
    }
    $children = [Collections.Generic.List[object]]::new()
    foreach ($child in $Node.ChildNodes) {
        if ($child.NodeType -eq [Xml.XmlNodeType]::Element) {
            $children.Add((Get-XmlShape $child))
        } elseif ($child.NodeType -eq [Xml.XmlNodeType]::Text) {
            $children.Add(@('text', $child.Value))
        } elseif ($child.NodeType -notin @([Xml.XmlNodeType]::Whitespace, [Xml.XmlNodeType]::SignificantWhitespace)) {
            throw 'Unexpected manifest XML node'
        }
    }
    return ,@($Node.NamespaceURI, $Node.LocalName, $attributes.ToArray(), $children.ToArray())
}

function Assert-EngineAcquisitionQuote {
    param([byte[]] $Bytes, [byte[]] $DescriptorBytes, $Profile, $ArtifactSet, [byte[]] $EnginePayload)
    if ($Bytes.Length -eq 0 -or $Bytes.Length -gt 16384 -or
        ($Bytes.Length -ge 3 -and $Bytes[0] -eq 239 -and $Bytes[1] -eq 187 -and $Bytes[2] -eq 191)) { throw 'Invalid Engine quote bytes' }
    $quote = Convert-CheckedJson $Bytes 16384
    Assert-ClosedObject $quote @('schema','desktop_descriptor_sha256','related_set_id','engine','bundle','disk')
    Assert-ClosedObject $quote.engine @('identity','package_version')
    Assert-ClosedObject $quote.engine.identity @('name','publisher','family','architecture')
    Assert-ClosedObject $quote.bundle @('file','uri','sha256','size_bytes')
    Assert-ClosedObject $quote.disk @('installed_space_budget_bytes','temporary_space_budget_bytes','rollback_space_budget_bytes')
    if ($quote.schema -isnot [string] -or $quote.desktop_descriptor_sha256 -isnot [string] -or
        $quote.related_set_id -isnot [string] -or $quote.bundle.file -isnot [string] -or
        $Profile.schema -cne 'ofca-msix-package-profile/v2' -or $Profile.distribution -cne 'direct-appinstaller' -or
        $ArtifactSet.kind -cne 'related_bundle' -or $quote.schema -cne 'optional-engine-acquisition-quote/v1' -or
        $quote.desktop_descriptor_sha256 -cne (Get-ByteDigest $DescriptorBytes) -or
        $quote.related_set_id -cne $Profile.servicing.installed_artifact.related_set_id -or
        -not (Test-ReleaseJsonEqual $quote.engine $Profile.servicing.installed_artifact.engine) -or
        $quote.bundle.sha256 -isnot [string] -or $quote.bundle.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Engine quote release binding differs' }
    $artifact = $ArtifactSet.engine.artifact
    if ($quote.bundle.file -cne $artifact.file -or $quote.bundle.sha256 -cne $artifact.sha256 -or
        $quote.bundle.size_bytes -cne $artifact.size) { throw 'Engine quote differs from the final signed artifact set' }
    $suffix = if ($Profile.purpose -ceq 'production') { 'windows/x64/stable/ConversationAnalytics.appinstaller' } else { 'probe.appinstaller' }
    if (-not $Profile.app_installer_uri.EndsWith($suffix,[StringComparison]::Ordinal)) { throw 'Engine quote has no approved release base' }
    $base = $Profile.app_installer_uri.Substring(0,$Profile.app_installer_uri.Length-$suffix.Length)
    $expectedUri = $base + 'packages/' + $artifact.sha256 + '/' + $artifact.file
    if ($quote.bundle.uri -isnot [string] -or $quote.bundle.uri.Length -gt 2048 -or $quote.bundle.uri -cne $expectedUri) { throw 'Engine quote immutable URI differs' }
    $maximum = [long]9007199254740991
    $values = @($quote.bundle.size_bytes,$quote.disk.installed_space_budget_bytes,
        $quote.disk.temporary_space_budget_bytes,$quote.disk.rollback_space_budget_bytes)
    foreach ($value in $values) {
        if (($value -isnot [long] -and $value -isnot [int]) -or $value -lt 0 -or $value -gt $maximum) { throw 'Engine quote requires safe integers' }
    }
    if ($values[0] -eq 0 -or $values[1] -eq 0 -or
        ([long]$values[1]+$values[2]+$values[3]) -gt $maximum) { throw 'Engine quote disk budget is invalid' }
    $stream = [IO.MemoryStream]::new($EnginePayload,$false)
    $archive = [IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read)
    try {
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $installed = [long]0
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName -match '%(?![0-9A-Fa-f]{2})|%(2f|5c)') { throw 'Engine quote payload has an unsafe part escape' }
            $name = [Ofca.Msix.IdentityReader]::DecodePartName($entry.FullName)
            if (-not $name -or $name -match '(^/|[\\:\x00-\x1f]|//|(^|/)\.{1,2}(/|$)|/$)' -or
                -not $names.Add($name) -or (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'Engine quote payload has unsafe paths or aliases' }
            if ($name -cin @('AppxBlockMap.xml','[Content_Types].xml','AppxSignature.p7x','AppxMetadata/CodeIntegrity.cat')) { continue }
            if ($entry.Length -gt $maximum-$installed) { throw 'Engine quote payload size overflow' }
            $installed += $entry.Length
        }
        if ($installed -gt $quote.disk.installed_space_budget_bytes) { throw 'Engine quote installed budget is below the final payload' }
    } finally { $archive.Dispose(); $stream.Dispose() }
}

function Test-MsixPayload {
    param([string] $Package, [string] $SignatureContainer = '')
    if (-not $IsWindows) { throw 'MSIX release verification requires Windows' }
    foreach ($file in @($Package, $Inventory, $PackageProfile)) {
        if (-not $file -or -not [IO.File]::Exists($file)) { throw 'Name an MSIX, its inventory and its explicit profile' }
    }
    if ([IO.Path]::GetExtension($Package).ToLowerInvariant() -cne '.msix') { throw 'Verification requires an MSIX file' }
    $profile = Read-BoundedJson $PackageProfile 65536
    $v2 = $profile.schema -ceq 'ofca-msix-package-profile/v2'
    $profileKeys = @('schema', 'purpose', 'distribution', 'identity', 'application_version', 'release_sequence', 'minimum_os_version', 'maximum_os_version_tested', 'webview2_minimum_version', 'app_installer_uri')
    if ($v2) { $profileKeys += 'servicing' }
    Assert-ClosedObject $profile $profileKeys
    Assert-ClosedObject $profile.identity @('name', 'publisher', 'publisher_display_name', 'application_id', 'architecture')
    if ($profile.schema -cnotin @('ofca-msix-package-profile/v1','ofca-msix-package-profile/v2') -or $profile.purpose -cnotin @('production', 'qualification') -or
        $profile.distribution -cnotin @('direct-appinstaller', 'microsoft-store')) { throw 'Invalid desktop profile' }
    if ($v2) {
        if ($profile.distribution -cne 'direct-appinstaller') { throw 'V2 requires direct delivery' }
        Assert-DesktopServicing $profile.servicing $profile.identity
    }
    $qualification = $profile.purpose -ceq 'qualification'
    if ($qualification -and -not $AllowQualification) { throw 'Production verification refuses qualification profiles' }
    $identity = $profile.identity
    foreach ($value in $identity.Values) {
        if ($value -isnot [string] -or $value -match '[\x00-\x1f]' -or $value.Trim() -cne $value) { throw 'Invalid identity string' }
    }
    if ($identity.name -cnotmatch '^[A-Za-z0-9.-]{3,50}$' -or $identity.publisher -cnotmatch '^CN=.+$' -or
        $identity.publisher.Length -gt 8192 -or $identity.publisher_display_name.Length -notin 1..256 -or
        $identity.application_id -cnotmatch '^[A-Za-z][A-Za-z0-9]{0,63}$' -or $identity.architecture -cne 'x64') { throw 'Invalid package identity' }
    $syntheticName = $identity.name.StartsWith('OFCA.Qualification.', [StringComparison]::OrdinalIgnoreCase)
    $syntheticPublisher = $identity.publisher.StartsWith('CN=OFCA MSIX Qualification', [StringComparison]::OrdinalIgnoreCase)
    if (($qualification -and -not ($syntheticName -and $syntheticPublisher)) -or
        (-not $qualification -and ($syntheticName -or $syntheticPublisher))) { throw 'Qualification and production identities must remain separate' }
    $minimum = Convert-Version $profile.minimum_os_version
    if ($minimum -lt [Version]'10.0.26100.0' -or (-not $qualification -and $minimum -lt [Version]'10.0.26200.0') -or
        (Convert-Version $profile.maximum_os_version_tested) -lt $minimum -or
        (Convert-Version $profile.webview2_minimum_version).Major -eq 0) { throw 'Unsupported profile platform' }
    if ($profile.application_version -isnot [string] -or $profile.application_version.Length -gt 64 -or
        $profile.application_version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?$' -or
        $profile.release_sequence -isnot [long] -or $profile.release_sequence -lt 1 -or $profile.release_sequence -gt 4294967295) { throw 'Invalid release version' }
    $packageVersion = '1.{0}.{1}.0' -f ([long][Math]::Floor($profile.release_sequence / 65536)), ($profile.release_sequence % 65536)
    if ($profile.distribution -ceq 'direct-appinstaller') {
        $uri = $null
        if ($profile.app_installer_uri -isnot [string] -or $profile.app_installer_uri.Length -gt 2048 -or
            -not [Uri]::TryCreate($profile.app_installer_uri, [UriKind]::Absolute, [ref]$uri) -or
            $uri.Scheme -cne 'https' -or $uri.Port -ne 443 -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
            -not $uri.AbsolutePath.EndsWith('.appinstaller', [StringComparison]::Ordinal) -or
            $uri.HostNameType -ne [UriHostNameType]::Dns -or $uri.IsLoopback -or
            $profile.app_installer_uri -match '[\\\x00-\x20]') { throw 'Invalid App Installer URI' }
        if (-not $qualification -and ($uri.DnsSafeHost -match '\.(example|test|invalid|example\.com)$' -or
            $uri.DnsSafeHost -in @('example.com', 'example.org', 'example.net'))) { throw 'Production requires verified hosting coordinates' }
    } elseif ($null -ne $profile.app_installer_uri) { throw 'Store profiles cannot register a direct feed' }
    if (($VerificationMode -ceq 'DirectSigned' -and $profile.distribution -cne 'direct-appinstaller') -or
        ($VerificationMode -ceq 'StoreSubmission' -and $profile.distribution -cne 'microsoft-store')) { throw 'Verification mode disagrees with distribution' }

    $inventoryDocument = Read-BoundedJson $Inventory 16777216
    $inventoryKeys = @('schema', 'profile_sha256', 'identity', 'purpose', 'distribution', 'application_version', 'package_version', 'runtime_release', 'linked_agent', 'runtime_policy_sha256', 'manifest_template_sha256', 'asset_source_sha256', 'makeappx_sha256', 'files')
    if ($v2) { $inventoryKeys += @('installed_artifact','desktop_descriptor_sha256','artifact_set') }
    Assert-ClosedObject $inventoryDocument $inventoryKeys
    $inventorySchema = if ($v2) { 'ofca-msix-inventory/v2' } else { 'ofca-msix-inventory/v1' }
    $profileDigest = Get-ByteDigest ([IO.File]::ReadAllBytes($PackageProfile))
    if ($inventoryDocument.schema -cne $inventorySchema -or $inventoryDocument.profile_sha256 -cne $profileDigest -or
        $inventoryDocument.package_version -cne $packageVersion -or $inventoryDocument.purpose -cne $profile.purpose -or
        $inventoryDocument.distribution -cne $profile.distribution -or $inventoryDocument.application_version -cne $profile.application_version) { throw 'Inventory disagrees with the profile' }
    Assert-ClosedObject $inventoryDocument.identity @('name', 'publisher', 'publisher_display_name', 'application_id', 'architecture')
    foreach ($key in $identity.Keys) { if ($identity[$key] -cne $inventoryDocument.identity[$key]) { throw 'Inventory identity mismatch' } }
    $templatePath = Join-Path $msixConfiguration 'AppxManifest.xml.in'
    if ($inventoryDocument.manifest_template_sha256 -cne (Get-ByteDigest ([IO.File]::ReadAllBytes($templatePath)))) { throw 'Inventory uses an unexpected manifest template' }
    $expected = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $aliases = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    if ($inventoryDocument.files -isnot [array] -or $inventoryDocument.files.Count -lt 1 -or $inventoryDocument.files.Count -gt 50000) { throw 'Invalid inventory count' }
    foreach ($record in $inventoryDocument.files) {
        Assert-ClosedObject $record @('path', 'sha256', 'size')
        if ($record.path -isnot [string] -or $record.path -match '(^/|[\\:\x00-\x1f]|//|(^|/)\.{1,2}(/|$)|/$)' -or
            -not $record.path -or $record.sha256 -isnot [string] -or $record.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            $record.size -isnot [long] -or $record.size -lt 0 -or $record.size -gt 4294967295 -or -not $aliases.Add($record.path)) { throw 'Invalid inventory record or path alias' }
        $expected.Add($record.path, $record)
    }
    if (-not ('Ofca.Msix.IdentityReader' -as [type])) { Add-Type -Path (Join-Path $msixConfiguration 'ReadPackageIdentity.cs') }
    $signatureArchive = $null
    $archive = [IO.Compression.ZipFile]::OpenRead([IO.Path]::GetFullPath($Package))
    try {
        $footprints = @('AppxBlockMap.xml', '[Content_Types].xml', 'AppxSignature.p7x', 'AppxMetadata/CodeIntegrity.cat')
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $count = 0
        if ($archive.Entries.Count -gt 50004) { throw 'Excessive package entries' }
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName -match '%(?![0-9A-Fa-f]{2})|%(2f|5c)') { throw 'Unsafe MSIX part escape' }
            $installedPath = [Ofca.Msix.IdentityReader]::DecodePartName($entry.FullName)
            if ($installedPath -match '(^/|[\\:\x00-\x1f]|//|(^|/)\.{1,2}(/|$)|/$)' -or
                -not $installedPath -or -not $names.Add($installedPath)) { throw 'Package path alias or unsafe installed path' }
            if ($installedPath -cin $footprints) {
                if ($entry.FullName -cne $installedPath) { throw 'Package footprint alias' }
                continue
            }
            if (-not $expected.ContainsKey($installedPath)) { throw 'Unexpected installed file' }
            $record = $expected[$installedPath]
            if ($entry.Length -ne $record.size -or (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'Package file size or type mismatch' }
            $stream = $entry.Open()
            try { if ((Get-StreamDigest $stream) -cne $record.sha256) { throw 'Package payload digest mismatch' } }
            finally { $stream.Dispose() }
            $count++
        }
        if ($count -ne $expected.Count -or $null -eq $archive.GetEntry('AppxBlockMap.xml') -or
            $null -eq $archive.GetEntry('[Content_Types].xml')) { throw 'Missing package payload or deployment footprint' }
        $manifestBytes = Read-ZipBytes ($archive.GetEntry('AppxManifest.xml')) 1048576
        $manifest = Read-SafeXml $manifestBytes
        $template = Read-SafeXml ([IO.File]::ReadAllBytes($templatePath))
        $substitutions = @{
            '@NAME@' = $identity.name; '@PUBLISHER@' = $identity.publisher; '@PUBLISHER_DISPLAY_NAME@' = $identity.publisher_display_name
            '@APPLICATION_ID@' = $identity.application_id; '@PACKAGE_VERSION@' = $packageVersion
            '@MINIMUM_OS@' = $profile.minimum_os_version; '@TESTED_OS@' = $profile.maximum_os_version_tested
            '@WEBVIEW2_MINIMUM@' = $profile.webview2_minimum_version
            '@WEBVIEW2_OPTIONAL@' = $(if ($profile.distribution -ceq 'direct-appinstaller') { 'true' } else { 'false' })
        }
        foreach ($node in $template.SelectNodes('//@* | //text()')) {
            if ($substitutions.ContainsKey($node.Value)) { $node.Value = $substitutions[$node.Value] }
        }
        $actualShape = ConvertTo-Json -InputObject (Get-XmlShape $manifest.DocumentElement) -Depth 64 -Compress
        $expectedShape = ConvertTo-Json -InputObject (Get-XmlShape $template.DocumentElement) -Depth 64 -Compress
        if ($actualShape -cne $expectedShape) { throw 'Manifest identity or policy differs from the approved template' }
        $desktop = Convert-CheckedJson (Read-ZipBytes ($archive.GetEntry('app/desktop-release.json')) 65536) 65536
        if ($null -ne $archive.GetEntry('app/engine-acquisition-quote.json') -and
            (-not $v2 -or -not $SignatureContainer -or $profile.servicing.installed_artifact.kind -cne 'related_bundle')) {
            throw 'Engine quote requires its final related bundle verification'
        }
        $desktopKeys = @('schema', 'purpose', 'identity', 'application_version', 'package_version', 'profile_sha256', 'update_owner', 'app_installer_uri', 'storage_policy', 'executable_family', 'webview2_minimum_version')
        if ($v2) { $desktopKeys += 'servicing' }
        Assert-ClosedObject $desktop $desktopKeys
        $desktopSchema = if ($v2) { 'ofca-desktop-release/v2' } else { 'ofca-desktop-release/v1' }
        Assert-ClosedObject $desktop.identity @('name', 'publisher', 'publisher_display_name', 'application_id', 'architecture')
        foreach ($key in $identity.Keys) { if ($identity[$key] -cne $desktop.identity[$key]) { throw 'Desktop descriptor identity mismatch' } }
        if ($desktop.schema -cne $desktopSchema -or $desktop.purpose -cne $profile.purpose -or
            $desktop.application_version -cne $profile.application_version -or $desktop.package_version -cne $packageVersion -or
            $desktop.profile_sha256 -cne $profileDigest -or $desktop.update_owner -cne $profile.distribution -or
            $desktop.app_installer_uri -cne $profile.app_installer_uri -or $desktop.storage_policy -cne 'retained-product-root/v1' -or
            $desktop.executable_family -cne 'brain-bridge/v1' -or $desktop.webview2_minimum_version -cne $profile.webview2_minimum_version) {
            throw 'Desktop descriptor disagrees with the approved profile'
        }
        $osIdentity = [Ofca.Msix.IdentityReader]::Read($manifestBytes)
        if ($osIdentity.Name -cne $identity.name -or $osIdentity.Publisher -cne $identity.publisher -or
            $osIdentity.Version -cne $packageVersion -or $osIdentity.Architecture -cne 'x64') { throw 'Windows-derived package identity mismatch' }
        if ($ExpectedFamilyName -and $ExpectedFamilyName -cne $osIdentity.FamilyName) { throw 'Expected package family mismatch' }
        if ($v2) {
            Assert-DesktopServicing $desktop.servicing $profile.identity $osIdentity.FamilyName
            $null = Get-ReleaseArtifactRecords $inventoryDocument.artifact_set $packageVersion
            if (-not (Test-ReleaseJsonEqual $desktop.servicing $profile.servicing) -or
                -not (Test-ReleaseJsonEqual $inventoryDocument.installed_artifact $profile.servicing.installed_artifact) -or
                $inventoryDocument.artifact_set.kind -cne $profile.servicing.installed_artifact.kind -or
                $inventoryDocument.desktop_descriptor_sha256 -cne (Get-ByteDigest (Read-ZipBytes ($archive.GetEntry('app/desktop-release.json')) 65536))) { throw 'V2 descriptor and inventory pairing differs' }
            if ($inventoryDocument.artifact_set.kind -ceq 'related_bundle') {
                $set = $inventoryDocument.artifact_set
                $layout = $profile.servicing.installed_artifact
                if ($set.related_set_id -cne $layout.related_set_id -or
                    $set.engine.package_version -cne $layout.engine.package_version -or
                    -not (Test-ReleaseJsonEqual $set.engine.identity $layout.engine.identity)) { throw 'V2 unsigned related inventory differs' }
            }
        }
        $signatureArchive = $archive
        if ($SignatureContainer) { $signatureArchive = [IO.Compression.ZipFile]::OpenRead($SignatureContainer) }
        $signed = $null -ne $signatureArchive.GetEntry('AppxSignature.p7x')
        if ($VerificationMode -ceq 'StoreSubmission') {
            if ($signed) { throw 'Store submission expects the unsigned candidate; qualify test-signed copies separately' }
        } else {
            if (-not $signed) { throw 'Direct release requires an MSIX signature' }
            if (-not $SignTool) {
                $SignTool = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits/10/bin/10.0.26100.0/x64/signtool.exe'
            }
            if (-not [IO.File]::Exists($SignTool)) { throw 'Pinned SignTool is unavailable' }
            # Exit 2 (warnings, including /tw missing timestamp) is also refusal.
            $signatureTarget = if ($SignatureContainer) { $SignatureContainer } else { $Package }
            $signatureOutput = & $SignTool verify /pa /all /v /tw $signatureTarget 2>&1
            if ($LASTEXITCODE -ne 0) { throw 'SignTool rejected the package signature, timestamp or trust chain' }
            $signatureBytes = Read-ZipBytes ($signatureArchive.GetEntry('AppxSignature.p7x')) 16777216
            if ($signatureBytes.Length -le 4 -or [Text.Encoding]::ASCII.GetString($signatureBytes, 0, 4) -cne 'PKCX') { throw 'Invalid package signature header' }
            $cms = [Security.Cryptography.Pkcs.SignedCms]::new()
            $cms.Decode($signatureBytes[4..($signatureBytes.Length - 1)])
            $cms.CheckSignature($true)
            if ($cms.SignerInfos.Count -ne 1 -or $cms.SignerInfos[0].Certificate.Subject -cne $identity.publisher) { throw 'Signing certificate subject differs from the profile Publisher' }
            $hasTimestamp = $false
            foreach ($attribute in $cms.SignerInfos[0].UnsignedAttributes) {
                if ($attribute.Oid.Value -in @('1.2.840.113549.1.9.6', '1.3.6.1.4.1.311.3.3.1')) { $hasTimestamp = $true }
            }
            if (-not $hasTimestamp) { throw 'Direct release requires a countersignature timestamp' }
        }
    } finally {
        if ($SignatureContainer -and $null -ne $signatureArchive) { $signatureArchive.Dispose() }
        $archive.Dispose()
    }
    $packageStream = [IO.File]::OpenRead($(if ($SignatureContainer) { $SignatureContainer } else { $Package }))
    try { $packageDigest = Get-StreamDigest $packageStream } finally { $packageStream.Dispose() }
    $receipt = [ordered]@{
        schema = 'ofca-msix-release-verification/v1'; purpose = $profile.purpose; distribution = $profile.distribution
        mode = $VerificationMode; identity = $osIdentity; file_count = $count
        package_sha256 = $packageDigest; profile_sha256 = $profileDigest
        inventory_sha256 = Get-ByteDigest ([IO.File]::ReadAllBytes($Inventory))
        signature = $(if ($VerificationMode -ceq 'DirectSigned') { 'trusted-timestamped' } else { 'unsigned-store-submission' })
    }
    return $receipt
}

function Read-BundleApplication {
    param([string] $Path, $Profile, [switch] $Engine)
    $layout = $Profile.servicing.installed_artifact
    $version = if ($Engine) { $layout.engine.package_version } else {
        '1.{0}.{1}.0' -f ([long][Math]::Floor($Profile.release_sequence / 65536)), ($Profile.release_sequence % 65536)
    }
    $identity = if ($Engine) { $layout.engine.identity } else { $Profile.identity }
    $filename = if ($Engine) { 'TextAnalysisEngine_' + $version + '_x64.msix' } else { 'ConversationAnalytics_' + $version + '_x64.msix' }
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $archive.Entries) {
            if (-not $names.Add($entry.FullName) -or $entry.FullName -match '(^/|[\\:\x00-\x1f%]|//|(^|/)\.{1,2}(/|$))' -or
                (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'Bundle path alias or unsafe member' }
        }
        $metadata = Read-SafeXml (Read-ZipBytes ($archive.GetEntry('AppxMetadata/AppxBundleManifest.xml')) 65536)
        $namespace = $metadata.DocumentElement.NamespaceURI
        if (($namespace -cne 'http://schemas.microsoft.com/appx/2016/bundle' -and
                (-not $Engine -or $namespace -cne 'http://schemas.microsoft.com/appx/2013/bundle')) -or
            $metadata.DocumentElement.LocalName -cne 'Bundle' -or $metadata.DocumentElement.GetAttribute('SchemaVersion') -cne '5.0') { throw 'Unsupported bundle metadata' }
        $ns = [Xml.XmlNamespaceManager]::new($metadata.NameTable)
        $ns.AddNamespace('b',$namespace)
        $ids = @($metadata.SelectNodes('/b:Bundle/b:Identity',$ns))
        $packages = @($metadata.SelectNodes('/b:Bundle/b:Packages/b:Package',$ns))
        $optionals = @($metadata.SelectNodes('/b:Bundle/b:OptionalBundle',$ns))
        if ($ids.Count -ne 1 -or $packages.Count -ne 1 -or $optionals.Count -ne $(if ($Engine) { 0 } else { 1 })) { throw 'Bundle requires its exact application/relation closure' }
        if ($Engine) {
            $containers = @($metadata.SelectNodes('/b:Bundle/b:Packages',$ns))
            if ($metadata.DocumentElement.SelectNodes('*').Count -ne 2 -or $containers.Count -ne 1 -or
                $containers[0].SelectNodes('*').Count -ne 1) { throw 'Engine bundle requires only identity and one application' }
        }
        foreach ($pair in @(@('Name',$identity.name),@('Publisher',$identity.publisher),@('Version',$version))) {
            if ($ids[0].GetAttribute($pair[0]) -cne $pair[1]) { throw 'Bundle identity mismatch' }
        }
        foreach ($pair in @(@('FileName',$filename),@('Version',$version),@('Architecture','x64'),@('Type','application'))) {
            if ($packages[0].GetAttribute($pair[0]) -cne $pair[1]) { throw 'Bundle application mismatch' }
        }
        if (-not $Engine) {
            $engineName = 'TextAnalysisEngine_' + $layout.engine.package_version + '_x64.msixbundle'
            foreach ($pair in @(@('Name',$layout.engine.identity.name),@('Publisher',$layout.engine.identity.publisher),
                    @('Version',$layout.engine.package_version),@('FileName',$engineName))) {
                if ($optionals[0].GetAttribute($pair[0]) -cne $pair[1]) { throw 'External Engine bundle mismatch' }
            }
            $external = @($optionals[0].SelectNodes('b:Package',$ns))
            $engineApplication = 'TextAnalysisEngine_' + $layout.engine.package_version + '_x64.msix'
            if ($external.Count -ne 1 -or $archive.GetEntry($engineName) -or $archive.GetEntry($engineApplication)) { throw 'Engine must remain external' }
            foreach ($pair in @(@('FileName',$engineApplication),@('Version',$layout.engine.package_version),@('Architecture','x64'),@('Type','application'))) {
                if ($external[0].GetAttribute($pair[0]) -cne $pair[1]) { throw 'External Engine application mismatch' }
            }
        }
        $allowed = @($filename,'AppxMetadata/AppxBundleManifest.xml','AppxBlockMap.xml','[Content_Types].xml','AppxSignature.p7x')
        if (@($archive.Entries | Where-Object { $_.FullName -cnotin $allowed }).Count -or
            -not $archive.GetEntry('AppxBlockMap.xml') -or -not $archive.GetEntry('[Content_Types].xml')) { throw 'Unexpected bundle file closure' }
        return ,(Read-ZipBytes ($archive.GetEntry($filename)) 536870912)
    } finally { $archive.Dispose() }
}

function Test-EngineSignature {
    param([string] $Path, [string] $Publisher)
    $tool = $SignTool
    if (-not $tool) { $tool = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits/10/bin/10.0.26100.0/x64/signtool.exe' }
    if (-not [IO.File]::Exists($tool)) { throw 'Pinned SignTool is unavailable' }
    $signatureOutput = & $tool verify /pa /all /v /tw $Path 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'SignTool rejected the Engine bundle signature, timestamp or trust chain' }
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try { $bytes = Read-ZipBytes ($archive.GetEntry('AppxSignature.p7x')) 16777216 }
    finally { $archive.Dispose() }
    if ($bytes.Length -le 4 -or [Text.Encoding]::ASCII.GetString($bytes,0,4) -cne 'PKCX') { throw 'Invalid Engine signature header' }
    $cms = [Security.Cryptography.Pkcs.SignedCms]::new()
    $cms.Decode($bytes[4..($bytes.Length-1)]); $cms.CheckSignature($true)
    if ($cms.SignerInfos.Count -ne 1 -or $cms.SignerInfos[0].Certificate.Subject -cne $Publisher) { throw 'Engine signing Publisher differs' }
    $timestamp = $false
    foreach ($attribute in $cms.SignerInfos[0].UnsignedAttributes) {
        if ($attribute.Oid.Value -in @('1.2.840.113549.1.9.6','1.3.6.1.4.1.311.3.3.1')) { $timestamp = $true }
    }
    if (-not $timestamp) { throw 'Engine requires a timestamp' }
}

function Test-RelatedArtifacts {
    param($Profile)
    if ($VerificationMode -cne 'DirectSigned' -or -not $EngineBundle -or
        [IO.Path]::GetExtension($Package) -cne '.msixbundle' -or
        [IO.Path]::GetExtension($EngineBundle) -cne '.msixbundle') { throw 'Related verification requires both signed bundles' }
    Assert-DesktopServicing $Profile.servicing $Profile.identity $ExpectedFamilyName
    $mainBytes = Read-BundleApplication $Package $Profile
    $engineBytes = Read-BundleApplication $EngineBundle $Profile -Engine
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('msix-related-verify-' + [Guid]::NewGuid().ToString('N'))
    if ([IO.Directory]::Exists($temporary)) { throw 'Require a fresh verification directory' }
    $null = [IO.Directory]::CreateDirectory($temporary)
    try {
        $corePath = Join-Path $temporary 'core.msix'
        [IO.File]::WriteAllBytes($corePath,$mainBytes)
        # Only this private call can substitute a signed enclosing container.
        # Its exact embedded bytes were read from that same approved bundle.
        $receipt = Test-MsixPayload $corePath $Package
        $stream = [IO.MemoryStream]::new($engineBytes,$false)
        $archive = [IO.Compression.ZipArchive]::new($stream,[IO.Compression.ZipArchiveMode]::Read)
        try {
            $manifestBytes = Read-ZipBytes ($archive.GetEntry('AppxManifest.xml')) 65536
            $manifest = Read-SafeXml $manifestBytes
            $ns = [Xml.XmlNamespaceManager]::new($manifest.NameTable)
            $ns.AddNamespace('f','http://schemas.microsoft.com/appx/manifest/foundation/windows10')
            $ns.AddNamespace('u','http://schemas.microsoft.com/appx/manifest/uap/windows10/3')
            $dependencies = @($manifest.SelectNodes('/f:Package/f:Dependencies/u:MainPackageDependency',$ns))
            if ($dependencies.Count -ne 1 -or $dependencies[0].Attributes.Count -ne 1 -or
                $dependencies[0].GetAttribute('Name') -cne $Profile.identity.name -or
                $manifest.SelectSingleNode('/f:Package/f:Applications',$ns) -or
                @($archive.Entries | Where-Object FullName -CEQ 'worker/TextAnalysisWorker.exe').Count -ne 1) { throw 'Engine dependency/worker closure differs' }
            $engineIdentity = [Ofca.Msix.IdentityReader]::Read($manifestBytes)
            $engine = $Profile.servicing.installed_artifact.engine
            foreach ($pair in @(@('Name',$engine.identity.name),@('Publisher',$engine.identity.publisher),
                    @('FamilyName',$engine.identity.family),@('Version',$engine.package_version),@('Architecture','x64'))) {
                if ($engineIdentity.($pair[0]) -cne $pair[1]) { throw 'Windows Engine identity differs' }
            }
        } finally { $archive.Dispose(); $stream.Dispose() }
        Test-EngineSignature $EngineBundle $Profile.identity.publisher
        $version = '1.{0}.{1}.0' -f ([long][Math]::Floor($Profile.release_sequence/65536)),($Profile.release_sequence%65536)
        $set = [ordered]@{kind='related_bundle';related_set_id=$Profile.servicing.installed_artifact.related_set_id
            core=@{file=('ConversationAnalytics_'+$version+'_x64.msixbundle');sha256=$receipt.package_sha256;size=([IO.FileInfo]::new($Package).Length)}
            engine=@{identity=$engine.identity;package_version=$engine.package_version;artifact=@{
                file=('TextAnalysisEngine_'+$engine.package_version+'_x64.msixbundle')
                sha256=(Get-ReleaseHash $EngineBundle);size=([IO.FileInfo]::new($EngineBundle).Length)}}}
        $null = Get-ReleaseArtifactRecords $set $version
        $coreArchive = [IO.Compression.ZipFile]::OpenRead($corePath)
        try {
            $quoteEntry = $coreArchive.GetEntry('app/engine-acquisition-quote.json')
            if ($null -ne $quoteEntry) {
                Assert-EngineAcquisitionQuote (Read-ZipBytes $quoteEntry 16384) `
                    (Read-ZipBytes ($coreArchive.GetEntry('app/desktop-release.json')) 65536) $Profile $set $engineBytes
            }
        } finally { $coreArchive.Dispose() }
        $receipt.schema = 'ofca-msix-release-verification/v2'
        $receipt.artifact_set = $set
        $receipt.engine_identity = $engineIdentity
        return $receipt
    } finally {
        if (-not ([IO.Path]::GetFullPath($temporary)).StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase)) { throw 'Verification cleanup path refused' }
        [IO.Directory]::Delete($temporary,$true)
    }
}

try {
    if (-not $IsWindows) { throw 'MSIX release verification requires Windows' }
    foreach ($file in @($Package, $Inventory, $PackageProfile)) {
        if (-not $file -or -not [IO.File]::Exists($file)) { throw 'Name an MSIX, its inventory and its explicit profile' }
    }
    if ([IO.Path]::GetExtension($Package).ToLowerInvariant() -cnotin @('.msix', '.msixbundle')) { throw 'Verification requires an MSIX file or MSIX bundle' }
    $profile = Read-BoundedJson $PackageProfile 65536
    if ($profile.schema -ceq 'ofca-msix-package-profile/v2' -and $profile.servicing.installed_artifact.kind -ceq 'related_bundle') {
        $receipt = Test-RelatedArtifacts $profile
    } else {
        if ($EngineBundle) { throw 'MainPackage verification cannot carry an Engine bundle' }
        $receipt = Test-MsixPayload $Package
        if ($profile.schema -ceq 'ofca-msix-package-profile/v2') {
            $receipt.schema = 'ofca-msix-release-verification/v2'
            $receipt.artifact_set = @{kind='main_package';core=@{
                file=('ConversationAnalytics_'+$receipt.identity.Version+'_x64.msix');sha256=$receipt.package_sha256
                size=([IO.FileInfo]::new($Package).Length)}}
        }
    }
    $json = $receipt | ConvertTo-Json -Depth 16
    if ($ReceiptPath) {
        if ([IO.File]::Exists($ReceiptPath)) { throw 'Verification receipt requires a fresh path' }
        [IO.File]::WriteAllText([IO.Path]::GetFullPath($ReceiptPath), $json + "`n", [Text.UTF8Encoding]::new($false))
    }
    Write-Output $json
    exit 0
} catch {
    Write-Host ('verify-msix: rejected: ' + $_.Exception.Message)
    exit 2
}
