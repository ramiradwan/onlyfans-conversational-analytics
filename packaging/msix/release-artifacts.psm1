# Build/sign handoff validation. No application build, network or credentials.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-ReleaseJson {
    param([string] $Path, [long] $Maximum = 8388608)
    $bytes = [IO.File]::ReadAllBytes($Path)
    if (-not $bytes.Length -or $bytes.Length -gt $Maximum) { throw 'Release JSON exceeds its bound' }
    $document = [Text.UTF8Encoding]::new($false, $true).GetString($bytes).TrimStart([char]0xfeff)
    $parsed = [Text.Json.JsonDocument]::Parse($document)
    try {
        $pending = [Collections.Generic.Stack[Text.Json.JsonElement]]::new()
        $pending.Push($parsed.RootElement)
        while ($pending.Count) {
            $value = $pending.Pop()
            if ($value.ValueKind -eq [Text.Json.JsonValueKind]::Object) {
                $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                foreach ($property in $value.EnumerateObject()) {
                    if (-not $names.Add($property.Name)) { throw 'Duplicate release JSON member' }
                    $pending.Push($property.Value)
                }
            } elseif ($value.ValueKind -eq [Text.Json.JsonValueKind]::Array) {
                foreach ($item in $value.EnumerateArray()) { $pending.Push($item) }
            }
        }
    } finally { $parsed.Dispose() }
    return ConvertFrom-Json -AsHashtable -InputObject $document
}

function Get-ReleaseHash {
    param([string] $Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-ReleaseJsonEqual {
    param($Left, $Right)
    if ($null -eq $Left -or $null -eq $Right) { return $null -eq $Left -and $null -eq $Right }
    if ($Left -is [Collections.IDictionary]) {
        if ($Right -isnot [Collections.IDictionary] -or $Left.Count -ne $Right.Count) { return $false }
        foreach ($key in $Left.Keys) {
            if ($key -cnotin @($Right.Keys) -or -not (Test-ReleaseJsonEqual $Left[$key] $Right[$key])) { return $false }
        }
        return $true
    }
    if ($Left -is [array]) {
        if ($Right -isnot [array] -or $Left.Count -ne $Right.Count) { return $false }
        for ($index = 0; $index -lt $Left.Count; $index++) {
            if (-not (Test-ReleaseJsonEqual $Left[$index] $Right[$index])) { return $false }
        }
        return $true
    }
    return $Left.GetType() -eq $Right.GetType() -and $Left -ceq $Right
}

function Read-DirectAppInstallerTemplate {
    param([string] $Path, $Profile, [string] $Version, [string] $PackageFile, [string] $ExpectedHash, $ArtifactSet = $null)
    $bytes = [IO.File]::ReadAllBytes($Path)
    if (-not $bytes.Length -or $bytes.Length -gt 16384 -or
        [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant() -cne $ExpectedHash) {
        throw 'Frozen App Installer template changed or exceeds its bound'
    }
    $settings = [Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $stream = [IO.MemoryStream]::new($bytes,$false)
    $reader = [Xml.XmlReader]::Create($stream,$settings)
    $document = [Xml.XmlDocument]::new()
    $document.XmlResolver = $null
    try { $document.Load($reader) } finally { $reader.Dispose(); $stream.Dispose() }
    $delivery = Read-ReleaseJson (Join-Path $PSScriptRoot 'direct-delivery.json') 16384
    $uri = $delivery.hosting.public_origin + $delivery.hosting.base_path + 'packages/' + ('0' * 64) + '/' + $PackageFile
    $namespace = 'http://schemas.microsoft.com/appx/appinstaller/2018'
    $nodes = @($document.SelectNodes('//*'))
    $related = $null -ne $ArtifactSet -and $ArtifactSet.kind -ceq 'related_bundle'
    $nodeCount = if ($related) { 7 } else { 5 }
    if ($nodes.Count -ne $nodeCount -or @($nodes | Where-Object { $_.NamespaceURI -cne $namespace }).Count) { throw 'Unexpected App Installer element' }
    $expected = @(
        @('AppInstaller',@{xmlns=$namespace;Version=$Version;Uri=$Profile.app_installer_uri}),
        @('MainPackage',@{Name=$Profile.identity.name;Publisher=$Profile.identity.publisher;Version=$Version;ProcessorArchitecture='x64';Uri=$uri}),
        @('UpdateSettings',@{}),
        @('OnLaunch',@{HoursBetweenUpdateChecks='6';ShowPrompt='false';UpdateBlocksActivation='false'}),
        @('AutomaticBackgroundTask',@{})
    )
    if ($related) {
        $engine = $ArtifactSet.engine
        $engineUri = $delivery.hosting.public_origin + $delivery.hosting.base_path + 'packages/' + ('0' * 64) + '/' + $engine.artifact.file
        $expected = @(
            @('AppInstaller',@{xmlns=$namespace;Version=$Version;Uri=$Profile.app_installer_uri}),
            @('MainBundle',@{Name=$Profile.identity.name;Publisher=$Profile.identity.publisher;Version=$Version;Uri=$uri}),
            @('RelatedPackages',@{}),
            @('Bundle',@{Name=$engine.identity.name;Publisher=$engine.identity.publisher;Version=$engine.package_version;Uri=$engineUri}),
            @('UpdateSettings',@{}),
            @('OnLaunch',@{HoursBetweenUpdateChecks='6';ShowPrompt='false';UpdateBlocksActivation='false'}),
            @('AutomaticBackgroundTask',@{})
        )
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        $node = $nodes[$index]
        $attributes = $expected[$index][1]
        if ($node.LocalName -cne $expected[$index][0] -or $node.Attributes.Count -ne $attributes.Count) { throw 'Unexpected App Installer structure' }
        foreach ($name in $attributes.Keys) {
            if ($node.GetAttribute($name) -cne $attributes[$name]) { throw 'App Installer policy or identity disagrees' }
        }
        foreach ($child in $node.ChildNodes) {
            if ($child.NodeType -eq [Xml.XmlNodeType]::Text -and -not [string]::IsNullOrWhiteSpace($child.Value)) { throw 'Unexpected App Installer text' }
        }
    }
    $parents = if ($related) { @{1=0;2=0;3=2;4=0;5=4;6=4} } else { @{1=0;2=0;3=2;4=2} }
    foreach ($index in $parents.Keys) {
        if ($nodes[$index].ParentNode -ne $nodes[$parents[$index]]) { throw 'Unexpected App Installer nesting' }
    }
    return $document
}

function Assert-ReleaseMembers {
    param($Value, [string[]] $Names)
    if ($Value -isnot [Collections.IDictionary] -or $Value.Count -ne $Names.Count -or
        @($Value.Keys | Where-Object { $_ -cnotin $Names }).Count) { throw 'Invalid closed artifact set' }
}

function Get-ReleaseArtifactRecords {
    param($Set, [string] $Version)
    if ($Set.kind -ceq 'main_package') {
        Assert-ReleaseMembers $Set @('kind','core')
        $filename = 'ConversationAnalytics_' + $Version + '_x64.msix'
    } elseif ($Set.kind -ceq 'related_bundle') {
        Assert-ReleaseMembers $Set @('kind','core','related_set_id','engine')
        Assert-ReleaseMembers $Set.engine @('identity','package_version','artifact')
        Assert-ReleaseMembers $Set.engine.identity @('name','publisher','family','architecture')
        if ($Set.related_set_id -cnotmatch '^[A-Za-z0-9_.:-]{1,160}$' -or
            $Set.engine.package_version -cnotmatch '^[1-9][0-9]*(?:\.(?:0|[1-9][0-9]*)){3}$' -or
            @($Set.engine.package_version.Split('.') | Where-Object { [long]$_ -gt 65535 }).Count -or
            $Set.engine.identity.name -cnotmatch '^[A-Za-z0-9.-]{3,50}$' -or
            $Set.engine.identity.architecture -cne 'x64' -or
            $Set.engine.identity.family -cnotmatch ('^' + [regex]::Escape($Set.engine.identity.name) + '_[a-z0-9]{13}$') -or
            $Set.engine.identity.publisher -cnotmatch '^CN=.+$') { throw 'Invalid Engine release identity' }
        $filename = 'ConversationAnalytics_' + $Version + '_x64.msixbundle'
    } else { throw 'Unknown artifact set variant' }
    $records = @($Set.core)
    if ($Set.kind -ceq 'related_bundle') {
        $records += $Set.engine.artifact
        if ($Set.engine.artifact.file -cne ('TextAnalysisEngine_' + $Set.engine.package_version + '_x64.msixbundle')) {
            throw 'Noncanonical Engine artifact filename'
        }
    }
    foreach ($item in $records) {
        Assert-ReleaseMembers $item @('file','sha256','size')
        if ($item.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            ($item.size -isnot [long] -and $item.size -isnot [int]) -or $item.size -le 0 -or
            $item.size -gt 4294967295) { throw 'Invalid artifact bytes' }
    }
    if ($Set.core.file -cne $filename) { throw 'Noncanonical core artifact filename' }
    return $records
}

function Assert-DesktopServicing {
    param($Value, $Identity, [string] $Family = '')
    if ($Family -and $Family -cnotmatch ('^' + [regex]::Escape($Identity.name) + '_[a-z0-9]{13}$')) { throw 'Invalid trusted Main family' }
    Assert-ReleaseMembers $Value @('installed_artifact','allowed_targets')
    $layout, $capability = $Value.installed_artifact, $Value.allowed_targets
    $engine = $null
    if ($capability.kind -ceq 'main_package_only') { Assert-ReleaseMembers $capability @('kind') }
    elseif ($capability.kind -ceq 'main_package_and_related_bundle') {
        Assert-ReleaseMembers $capability @('kind','engine_identity')
        $engine = $capability.engine_identity
        Assert-ReleaseMembers $engine @('name','publisher','family','architecture')
        if ($engine.name -cnotmatch '^[A-Za-z0-9.-]{3,50}$' -or
            $engine.name.Equals($Identity.name,[StringComparison]::OrdinalIgnoreCase) -or
            $engine.publisher -cne $Identity.publisher -or $engine.architecture -cne 'x64' -or
            $engine.family -cnotmatch ('^' + [regex]::Escape($engine.name) + '_[a-z0-9]{13}$')) { throw 'Invalid Engine capability identity' }
        if ($Family) {
            $prefix = $Identity.name + '_'
            if (-not $Family.StartsWith($prefix,[StringComparison]::Ordinal) -or
                $Family.Substring($prefix.Length) -cnotmatch '^[a-z0-9]{13}$' -or
                $engine.family -cne ($engine.name + '_' + $Family.Substring($prefix.Length))) { throw 'Engine publisher identity differs from Windows' }
        }
    } else { throw 'Unknown target capability' }
    if ($layout.kind -ceq 'main_package') { Assert-ReleaseMembers $layout @('kind') }
    elseif ($layout.kind -ceq 'related_bundle') {
        Assert-ReleaseMembers $layout @('kind','related_set_id','engine')
        Assert-ReleaseMembers $layout.engine @('identity','package_version')
        if ($null -eq $engine -or -not (Test-ReleaseJsonEqual $engine $layout.engine.identity) -or
            $layout.related_set_id -cnotmatch '^[A-Za-z0-9_.:-]{1,160}$' -or
            $layout.engine.package_version -cnotmatch '^[1-9][0-9]*(?:\.(?:0|[1-9][0-9]*)){3}$' -or
            @($layout.engine.package_version.Split('.') | Where-Object { [long]$_ -gt 65535 }).Count) { throw 'Invalid installed related pairing' }
    } else { throw 'Unknown installed artifact layout' }
}

function Get-MsixReleaseCandidate {
    param([string] $Directory, [ValidateSet('msix-direct', 'msix-store', IgnoreCase=$false)] [string] $Target,
          [string] $ProductRevision, [switch] $BeforeSigning, [switch] $CheckEnvironment)
    if ($ProductRevision -cnotmatch '^[0-9a-f]{40}$') { throw 'Require the exact Product revision' }
    $record = Read-ReleaseJson (Join-Path $Directory 'desktop.release-inputs.json') 32768
    $keys = @('schema','target','source_commit','source_tree','package_file','package_version','application_version',
        'profile_sha256','inventory_sha256','build_sha256','unsigned_package_sha256','agent_file','agent_sha256',
        'expected_family','direct_delivery_sha256','appinstaller_template_sha256')
    $v2 = $record.schema -ceq 'ofca-msix-release-inputs/v2'
    if ($v2) { $keys = @($keys | Where-Object { $_ -cnotin @('package_file','unsigned_package_sha256') }) + 'artifact_set' }
    if ($record.Count -ne $keys.Count -or @($record.Keys | Where-Object { $_ -cnotin $keys }).Count -or
        $record.schema -cnotin @('ofca-msix-release-inputs/v1','ofca-msix-release-inputs/v2') -or $record.target -cne $Target -or
        $record.source_commit -cne $ProductRevision -or $record.source_tree -cnotmatch '^[0-9a-f]{40}$') {
        throw 'Invalid MSIX release handoff'
    }
    $artifacts = @()
    if ($v2) {
        if ($Target -cne 'msix-direct') { throw 'V2 release artifacts require direct delivery' }
        $artifacts = @(Get-ReleaseArtifactRecords $record.artifact_set $record.package_version)
        # Internal aliases only; serialized v2 records keep the tagged shape.
        $record.package_file = $record.artifact_set.core.file
        $record.unsigned_package_sha256 = $record.artifact_set.core.sha256
    }
    $project = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $head = (& git -C $project rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -cne $ProductRevision) { throw 'Verifier checkout differs from the Product revision' }
    $tree = (& git -C $project rev-parse 'HEAD^{tree}').Trim()
    if ($LASTEXITCODE -ne 0 -or $tree -cne $record.source_tree) { throw 'Release source tree disagrees' }
    if ($record.package_version -cnotmatch '^1\.[0-9]+\.[0-9]+\.0$' -or
        (-not $v2 -and $record.package_file -cne ('ConversationAnalytics_' + $record.package_version + '_x64.msix')) -or
        $record.agent_file -cnotmatch '^[A-Za-z0-9._-]+-chrome\.zip$') { throw 'Unexpected release artifact name' }
    $allowed = @('desktop.release-inputs.json','desktop.profile.json','desktop.inventory.json','desktop.build.json',
        $record.package_file,$record.agent_file)
    if ($v2 -and $record.artifact_set.kind -ceq 'related_bundle') { $allowed += $record.artifact_set.engine.artifact.file }
    if ($Target -ceq 'msix-direct') { $allowed += 'desktop.appinstaller-template.xml' }
    if (-not $BeforeSigning) {
        $allowed += @('desktop.verification.json','desktop.artifact-release.json','sha256sums.txt')
        if ($Target -ceq 'msix-direct') { $allowed += @('ConversationAnalytics.appinstaller','desktop.resolved-direct-inputs.json') }
    }
    foreach ($entry in Get-ChildItem -LiteralPath $Directory -Force) {
        if ($entry.PSIsContainer -or $entry.LinkType -or $entry.Name -cnotin $allowed) { throw 'Unexpected release-directory member' }
    }
    foreach ($pair in @(
        @('desktop.profile.json','profile_sha256'), @('desktop.inventory.json','inventory_sha256'),
        @('desktop.build.json','build_sha256'), @($record.agent_file,'agent_sha256'))) {
        if ((Get-ReleaseHash (Join-Path $Directory $pair[0])) -cne $record[$pair[1]]) { throw 'Frozen release input changed' }
    }
    $profile = Read-ReleaseJson (Join-Path $Directory 'desktop.profile.json') 16384
    $approvedName = if ($Target -ceq 'msix-direct') { 'production-direct.json' } else { 'production-store.json' }
    $approved = Read-ReleaseJson (Join-Path $PSScriptRoot ('profiles/' + $approvedName)) 16384
    if (-not (Test-ReleaseJsonEqual $profile $approved) -or $profile.purpose -cne 'production') {
        throw 'Release profile differs from the approved source profile'
    }
    $version = '1.{0}.{1}.0' -f [Math]::Floor($profile.release_sequence / 65536), ($profile.release_sequence % 65536)
    if ($record.package_version -cne $version -or $record.application_version -cne $profile.application_version) {
        throw 'Release versions disagree with the approved profile'
    }
    $inventory = Read-ReleaseJson (Join-Path $Directory 'desktop.inventory.json')
    $build = Read-ReleaseJson (Join-Path $Directory 'desktop.build.json') 16384
    $inventorySchema = if ($v2) { 'ofca-msix-inventory/v2' } else { 'ofca-msix-inventory/v1' }
    if ($inventory.schema -cne $inventorySchema -or $inventory.profile_sha256 -cne $record.profile_sha256 -or
        $inventory.runtime_release.source_commit -cne $ProductRevision -or
        $inventory.package_version -cne $record.package_version -or
        $inventory.application_version -cne $record.application_version -or
        $build.producer_source.base_commit -cne $ProductRevision -or $build.producer_source.dirty -cne $false -or
        $build.verification.package_sha256 -cne $record.unsigned_package_sha256 -or
        -not (Test-ReleaseJsonEqual $inventory.identity $profile.identity)) { throw 'MSIX source or inventory handoff disagrees' }
    if ($v2) {
        Assert-ReleaseMembers $inventory @('schema','profile_sha256','identity','purpose','distribution','application_version',
            'package_version','runtime_release','linked_agent','runtime_policy_sha256','manifest_template_sha256',
            'asset_source_sha256','makeappx_sha256','files','installed_artifact','desktop_descriptor_sha256','artifact_set')
        Assert-ReleaseMembers $build @('schema','verification','windows_sdk','makeappx_file_version','uap17_schema_sha256','producer_source','artifact_set')
        if ($profile.schema -cne 'ofca-msix-package-profile/v2' -or
            -not (Test-ReleaseJsonEqual $inventory.installed_artifact $profile.servicing.installed_artifact) -or
            $inventory.desktop_descriptor_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            $record.artifact_set.kind -cne $inventory.installed_artifact.kind -or
            -not (Test-ReleaseJsonEqual $inventory.artifact_set $record.artifact_set) -or
            $build.schema -cne 'ofca-msix-build/v2' -or
            -not (Test-ReleaseJsonEqual $build.artifact_set $record.artifact_set)) { throw 'V2 source artifact set disagrees' }
        if ($record.artifact_set.kind -ceq 'related_bundle') {
            $layout = $profile.servicing.installed_artifact
            if ($record.artifact_set.related_set_id -cne $layout.related_set_id -or
                $record.artifact_set.engine.package_version -cne $layout.engine.package_version -or
                -not (Test-ReleaseJsonEqual $record.artifact_set.engine.identity $layout.engine.identity)) { throw 'Related artifact pairing differs' }
        }
    }
    $family = if ($Target -ceq 'msix-direct') {
        $delivery = Read-ReleaseJson (Join-Path $PSScriptRoot 'direct-delivery.json') 16384
        if ((Get-ReleaseHash (Join-Path $PSScriptRoot 'direct-delivery.json')) -cne $record.direct_delivery_sha256 -or
            $delivery.signing.subject -cne $profile.identity.publisher -or
            $delivery.hosting.app_installer_uri -cne $profile.app_installer_uri) { throw 'Direct delivery inputs disagree' }
        if ($CheckEnvironment) {
            $expected = @{
                MSIX_DIRECT_PUBLISHER = $profile.identity.publisher
                MSIX_DIRECT_EXPECTED_FAMILY = $delivery.expected_package_family
                MSIX_DIRECT_APPINSTALLER_URI = $profile.app_installer_uri
                DESKTOP_R2_BUCKET = $delivery.hosting.bucket
                DESKTOP_R2_ENDPOINT = $delivery.hosting.s3_endpoint
                DESKTOP_UPDATES_BASE_URL = ($delivery.hosting.public_origin + $delivery.hosting.base_path)
            }
            foreach ($name in $expected.Keys) {
                if ([Environment]::GetEnvironmentVariable($name) -cne $expected[$name]) {
                    throw ('Release environment input disagrees: ' + $name)
                }
            }
            if (-not $BeforeSigning) {
                $resolved = Read-ReleaseJson (Join-Path $Directory 'desktop.resolved-direct-inputs.json') 32768
                if ($resolved.Count -ne 4 -or $resolved.schema -cne 'ofca-direct-release-environment/v1' -or
                    $resolved.source_commit -cne $ProductRevision -or $resolved.profile_sha256 -cne $record.profile_sha256 -or
                    -not (Test-ReleaseJsonEqual $resolved['values'] $expected)) { throw 'Resolved release environment changed' }
            }
        }
        if ((Get-ReleaseHash (Join-Path $Directory 'desktop.appinstaller-template.xml')) -cne $record.appinstaller_template_sha256) {
            throw 'Frozen App Installer template changed'
        }
        $null = Read-DirectAppInstallerTemplate (Join-Path $Directory 'desktop.appinstaller-template.xml') $profile `
            $record.package_version $record.package_file $record.appinstaller_template_sha256 $(if ($v2) { $record.artifact_set } else { $null })
        $delivery.expected_package_family
    } else {
        if ($null -ne $record.direct_delivery_sha256 -or $null -ne $record.appinstaller_template_sha256 -or
            $null -ne $profile.app_installer_uri) { throw 'Store candidate carries direct delivery authority' }
        (Read-ReleaseJson (Join-Path $PSScriptRoot 'store-reservation.json') 16384).package_family_name
    }
    if ($record.expected_family -cne $family) { throw 'Expected family differs from its approved identity' }
    if ($v2) { Assert-DesktopServicing $profile.servicing $profile.identity $family }
    if ($v2 -and $record.artifact_set.kind -ceq 'related_bundle') {
        $engine = $record.artifact_set.engine.identity
        $prefix = $profile.identity.name + '_'
        if (-not $family.StartsWith($prefix,[StringComparison]::Ordinal) -or
            $engine.family -cne ($engine.name + '_' + $family.Substring($prefix.Length)) -or
            $engine.publisher -cne $profile.identity.publisher -or
            $engine.name.Equals($profile.identity.name,[StringComparison]::OrdinalIgnoreCase)) { throw 'Engine family differs from the approved Main publisher identity' }
    }
    if ($BeforeSigning -and (Get-ReleaseHash (Join-Path $Directory $record.package_file)) -cne $record.unsigned_package_sha256) {
        throw 'Unsigned package changed before signing'
    }
    if ($BeforeSigning -and $v2) {
        foreach ($artifact in $artifacts) {
            $path = Join-Path $Directory $artifact.file
            if ((Get-Item -LiteralPath $path).Length -ne $artifact.size -or (Get-ReleaseHash $path) -cne $artifact.sha256) {
                throw 'Unsigned artifact set changed before signing'
            }
        }
    }
    return $record
}

Export-ModuleMember -Function Read-ReleaseJson,Get-ReleaseHash,Get-MsixReleaseCandidate,Read-DirectAppInstallerTemplate,Get-ReleaseArtifactRecords,Assert-DesktopServicing,Test-ReleaseJsonEqual
