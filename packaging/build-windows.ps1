[CmdletBinding()]
param(
    # This must be an isolated build environment that contains requirements.txt
    # and packaging/requirements-build.txt; never point it at product/.venv.
    [Parameter(Mandatory)]
    [string] $BuildPython,

    # Optional command override for a hermetic build runner. It is not needed
    # for normal builds, which always use ``BuildPython -m PyInstaller``.
    [string] $PyInstallerExecutable = "",

    # A fresh per-process directory outside the repository avoids untracked
    # build output and makes the script non-destructive by default.
    [string] $OutputRoot = (Join-Path $env:TEMP ("brain-pyinstaller-" + $PID)),

    # Intended for the build-gate test, where known checked-in immutable assets
    # are staged by a PyInstaller stand-in. Normal builds always rebuild both.
    [switch] $SkipAssetBuild,

    # A narrow falsifier seam. It can only inject named prohibited material
    # after freezing and before policy verification; it cannot alter a release.
    [ValidateSet("", "DevelopmentConfiguration", "InstallationClaim", "EmbeddedExtensionIdentityMismatch")]
    [string] $TestInjection = "",

    # Optional explicit Inno Setup compiler. When omitted, discovery runs
    # only after the staged artifact has passed every release gate.
    [string] $InnoSetupCompiler = "",

    # The packaged signing rule and the Legal release bindings the Store
    # candidate is built against. A release requires both; nothing else makes
    # them optional and there is no discovery, default or environment fallback.
    [string] $PackagedSigningRule = "",
    [string] $LegalReleaseBindings = "",

    # Privacy policy URL for the packaged Agent configuration. When it is
    # omitted the checked-in extension configuration must already carry one.
    [string] $PrivacyPolicyUrl = "",

    # Development packaging. It publishes a development bundle in place of the
    # Store candidate and mints no Store candidate at all.
    [switch] $DevelopmentAgentBundle
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = if ($env:BRAIN_PROJECT_ROOT) {
    [IO.Path]::GetFullPath($env:BRAIN_PROJECT_ROOT)
} else {
    Split-Path -Parent $PSScriptRoot
}
$SpecPath = Join-Path $ProjectRoot "packaging\pyinstaller\brain.spec"
$PolicyPath = Join-Path $ProjectRoot "tools\packaging_policy.py"
$RuntimePolicyPath = Join-Path $ProjectRoot "packaging\runtime-files.json"
$InnoScriptPath = Join-Path $ProjectRoot "packaging\inno\brain.iss"
$DigestScriptPath = Join-Path $ProjectRoot "packaging\write-digests.ps1"
$AgentBundleScriptPath = Join-Path $ProjectRoot "packaging\new-agent-bundle.ps1"
$ExtensionRoot = Join-Path $ProjectRoot "extension"
$ExtensionBuildScript = Join-Path $ExtensionRoot "build.mjs"
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)

# The one filename a Chrome Web Store submission is made from. Only the archive
# extension/build.mjs --package produces is ever copied to it.
$StoreCandidateSuffix = "-chrome.zip"
$ReleaseMode = -not $DevelopmentAgentBundle

# Staging directories the installer leaves out; keep in step with the Excludes
# directive in packaging/inno/brain.iss. A digest file that ships inside the
# installation may not list anything under them.
$InstallerExcludedStagingDirectories = @("Agent")

if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) {
    throw "Build Python does not exist: $BuildPython"
}
if (-not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) {
    throw "PyInstaller spec does not exist: $SpecPath"
}
if (-not (Test-Path -LiteralPath $RuntimePolicyPath -PathType Leaf)) {
    throw "Runtime packaging policy does not exist: $RuntimePolicyPath"
}
if (-not (Test-Path -LiteralPath $InnoScriptPath -PathType Leaf)) {
    throw "Inno Setup script does not exist: $InnoScriptPath"
}
if (-not (Test-Path -LiteralPath $DigestScriptPath -PathType Leaf)) {
    throw "Digest writer does not exist: $DigestScriptPath"
}
if (-not (Test-Path -LiteralPath $AgentBundleScriptPath -PathType Leaf)) {
    throw "Deterministic Agent bundle writer does not exist: $AgentBundleScriptPath"
}
if ($OutputRoot -eq $ProjectRoot -or $OutputRoot.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Build output must be outside the repository: $OutputRoot"
}
if (Test-Path -LiteralPath $OutputRoot) {
    throw "Build output directory already exists; choose a fresh path: $OutputRoot"
}

function Resolve-ReleaseInput {
    <#
        Resolve a release input that a Store candidate may not be built without.
        It runs before anything is written, so a release missing one of them
        stops before the build output directory exists.
    #>
    param(
        [Parameter(Mandatory)] [AllowEmptyString()] [string] $Value,
        [Parameter(Mandatory)] [string] $Name
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "A release build requires -$Name; a Store candidate is never built without it"
    }
    $resolved = [IO.Path]::GetFullPath($Value)
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "The declared $Name does not exist: $resolved"
    }
    return $resolved
}

if (-not (Test-Path -LiteralPath $ExtensionBuildScript -PathType Leaf)) {
    throw "Extension build script does not exist: $ExtensionBuildScript"
}
if ($ReleaseMode) {
    $PackagedSigningRule = Resolve-ReleaseInput -Value $PackagedSigningRule -Name "PackagedSigningRule"
    $LegalReleaseBindings = Resolve-ReleaseInput -Value $LegalReleaseBindings -Name "LegalReleaseBindings"
}

& $BuildPython -c "import struct, sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Windows Brain builds are x64 only; BuildPython is not 64-bit"
}

New-Item -ItemType Directory -Path $OutputRoot | Out-Null
$distPath = Join-Path $OutputRoot "dist"
$workPath = Join-Path $OutputRoot "work"

function Invoke-RequiredCommand {
    param([Parameter(Mandatory)] [string] $FilePath, [Parameter(Mandatory)] [string[]] $Arguments)

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Invoke-ExtensionBuild {
    <#
        Run extension/build.mjs, the validator for every Agent release input.
        Legal validation lives there and is never restated here; this reports
        what it said and refuses the build when it exits non-zero.
    #>
    param(
        [Parameter(Mandatory)] [string[]] $Arguments,
        [Parameter(Mandatory)] [string] $FailureMessage
    )

    # The validator reports refusals on standard error. That is diagnostic text
    # rather than a terminating error here, so the exit code decides.
    $ErrorActionPreference = "Continue"
    Push-Location $ExtensionRoot
    try {
        $output = & node.exe @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $output | Write-Host
    if ($exitCode -ne 0) {
        throw "$FailureMessage (exit $exitCode)"
    }
}

function Get-ExtensionReleaseArguments {
    param([Parameter(Mandatory)] [AllowEmptyString()] [string] $Verb)

    $arguments = @($ExtensionBuildScript)
    if ($Verb) {
        $arguments += $Verb
    }
    if ($PackagedSigningRule) {
        $arguments += "--packaged-signing-rule=$PackagedSigningRule"
    }
    if ($LegalReleaseBindings) {
        $arguments += "--legal-release-bindings=$LegalReleaseBindings"
    }
    if ($PrivacyPolicyUrl) {
        $arguments += "--privacy-policy-url=$PrivacyPolicyUrl"
    }
    return $arguments
}

function Get-Sha256Digest {
    <#
        Hash a file the way packaging/write-digests.ps1 does, so a digest this
        script compares reads the same as the digest it publishes. The
        computation is .NET rather than a cmdlet because the release path must
        not depend on module autoloading.
    #>
    param([Parameter(Mandatory)] [string] $Path)

    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString(
            $hasher.ComputeHash([IO.File]::ReadAllBytes($Path))
        ).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}

function New-AgentStorePackage {
    <#
        Mint the Store candidate with extension/build.mjs --package, the only
        archive engine a release uses. That step refuses the package when the
        packaged signing rule, the Legal release bindings or the privacy policy
        configuration is absent or invalid, it writes the archive only after
        every one of those checks, and it removes an archive that fails its own
        audit, so a refusal leaves no archive behind.

        The archive is moved out of the source tree, which keeps the staged
        Agent directory the unpacked extension the archive was packed from.
    #>
    param([Parameter(Mandatory)] [string] $OutputRoot)

    $distRoot = Join-Path $ExtensionRoot "dist"
    Invoke-ExtensionBuild `
        -Arguments (Get-ExtensionReleaseArguments -Verb "--package") `
        -FailureMessage "The Agent Store package was refused"

    $metadata = Get-Content -Raw -LiteralPath (Join-Path $distRoot "build-meta.json") | ConvertFrom-Json
    foreach ($declaration in @("signing_rule", "legal_bindings")) {
        if ($null -eq $metadata.$declaration) {
            throw "The packaged Agent artifact records no $declaration"
        }
    }
    if ($metadata.privacy_policy_configured -ne $true) {
        throw "The packaged Agent artifact records no privacy policy configuration"
    }

    $built = Join-Path $distRoot "conversation-analytics-$($metadata.extension_version).zip"
    if (-not (Test-Path -LiteralPath $built -PathType Leaf)) {
        throw "The Agent package step produced no archive: $built"
    }
    $packageRoot = Join-Path $OutputRoot "agent-package"
    New-Item -ItemType Directory -Path $packageRoot | Out-Null
    $archive = Join-Path $packageRoot (Split-Path -Leaf $built)
    $digest = Get-Sha256Digest -Path $built
    Move-Item -LiteralPath $built -Destination $archive
    $moved = Get-Sha256Digest -Path $archive
    if ($moved -ne $digest) {
        throw "The Agent archive changed while it left the source tree: $digest then $moved"
    }
    return [pscustomobject]@{
        Archive = $archive
        Sha256 = $digest
        ExtensionVersion = $metadata.extension_version
    }
}

function Copy-DeclaredTopLevelFiles {
    param([Parameter(Mandatory)] [string] $StagingRoot)

    $policy = Get-Content -Raw -LiteralPath $RuntimePolicyPath | ConvertFrom-Json
    foreach ($relative in $policy.required_files) {
        if ($relative -in @("Brain.exe", "release-manifest.json") -or $relative.StartsWith("_internal/")) {
            continue
        }
        $source = Join-Path $ProjectRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Declared top-level runtime file is absent: $source"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $StagingRoot $relative)
    }
}

function Copy-AgentArtifact {
    param([Parameter(Mandatory)] [string] $StagingRoot)

    $source = Join-Path $ProjectRoot "extension\dist"
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        throw "The declared Agent artifact is absent: $source"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $StagingRoot "Agent") -Recurse
}

function New-PackagingSourceRoot {
    param(
        [Parameter(Mandatory)] [string] $BuildPython,
        [Parameter(Mandatory)] [string] $OutputRoot,
        [Parameter(Mandatory)] [string] $ProjectRoot
    )

    $sourceRoot = Join-Path $OutputRoot "source"
    $sourceApp = Join-Path $sourceRoot "app"
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "app") -Destination $sourceApp -Recurse
    $manifestPath = Join-Path $ProjectRoot "extension\manifest.json"
    $embeddedIdentityPath = Join-Path $sourceApp "core\packaged_extension_identity.py"
    Push-Location $ProjectRoot
    try {
        Invoke-RequiredCommand -FilePath $BuildPython -Arguments @(
            "-m", "app.core.extension_identity", "--manifest", $manifestPath,
            "--output", $embeddedIdentityPath
        )
    } finally {
        Pop-Location
    }
    return [pscustomobject]@{
        SourceRoot = $sourceRoot
        ManifestPath = $manifestPath
        EmbeddedIdentityPath = $embeddedIdentityPath
    }
}

function Assert-EmbeddedExtensionIdentity {
    param(
        [Parameter(Mandatory)] [string] $BuildPython,
        [Parameter(Mandatory)] [string] $ProjectRoot,
        [Parameter(Mandatory)] [string] $ManifestPath,
        [Parameter(Mandatory)] [string] $EmbeddedIdentityPath
    )

    Push-Location $ProjectRoot
    try {
        & $BuildPython -m app.core.extension_identity --manifest $ManifestPath --verify-embedded $EmbeddedIdentityPath
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        throw "Embedded extension identity does not match the current manifest key"
    }
}

function Get-BrainVersion {
    $config = Get-Content -Raw -LiteralPath (Join-Path $ProjectRoot "app\core\config.py")
    $match = [regex]::Match($config, '(?m)^\s*version:\s*str\s*=\s*"(?<version>[^"]+)"')
    if (-not $match.Success) {
        throw "The authoritative Brain version is absent from app/core/config.py"
    }
    return $match.Groups["version"].Value
}

function Resolve-InnoSetupCompiler {
    param([string] $ExplicitCompiler)

    $searched = [System.Collections.Generic.List[string]]::new()
    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($ExplicitCompiler) {
        $searched.Add("explicit -InnoSetupCompiler: $ExplicitCompiler")
        $candidates.Add($ExplicitCompiler)
    } else {
        $searched.Add("explicit -InnoSetupCompiler (not supplied)")
    }
    foreach ($environmentVariable in @("INNO_SETUP_COMPILER", "ISCC")) {
        $value = [Environment]::GetEnvironmentVariable($environmentVariable)
        $searched.Add("environment variable $environmentVariable" + $(if ($value) { ": $value" } else { " (unset)" }))
        if ($value) {
            $candidates.Add($value)
        }
    }
    $searched.Add("PATH: ISCC.exe")
    $onPath = Get-Command -Name "ISCC.exe" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $onPath) {
        $candidates.Add($onPath.Source)
    }
    $knownBases = @(
        $env:LOCALAPPDATA,
        ${env:ProgramFiles(x86)},
        $env:ProgramFiles
    ) | Where-Object { $_ }
    foreach ($knownBase in $knownBases) {
        $knownLocation = Join-Path $knownBase "Programs\Inno Setup 6\ISCC.exe"
        if ($knownBase -ne $env:LOCALAPPDATA) {
            $knownLocation = Join-Path $knownBase "Inno Setup 6\ISCC.exe"
        }
        $searched.Add("known location: $knownLocation")
        $candidates.Add($knownLocation)
    }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw "Inno Setup compiler was not found. Searched: $($searched -join '; ')"
}

function Write-ReleaseManifest {
    param([Parameter(Mandatory)] [string] $StagingRoot)

    $extensionMetadata = Get-Content -Raw -LiteralPath (Join-Path $ProjectRoot "extension\dist\build-meta.json") | ConvertFrom-Json
    $commit = (& git -C $ProjectRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to resolve the source commit for release-manifest.json"
    }
    [ordered]@{
        schema = "ofca-release-manifest/v1"
        version = (Get-BrainVersion)
        source_commit = $commit
        architecture = "x64"
        extension_version = $extensionMetadata.extension_version
        extension_build_schema = $extensionMetadata.schema
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $StagingRoot "release-manifest.json") -Encoding utf8
}

function Invoke-PackagingPolicy {
    param(
        [Parameter(Mandatory)] [string] $BuildPython,
        [Parameter(Mandatory)] [string] $ProjectRoot,
        [Parameter(Mandatory)] [string] $StagingRoot
    )

    $verifier = @'
import json
import sys
from dataclasses import asdict
from pathlib import Path

from tools.packaging_policy import verify_runtime_files

findings = verify_runtime_files(Path(sys.argv[1]))
print(json.dumps([asdict(finding) for finding in findings], sort_keys=True))
raise SystemExit(1 if findings else 0)
'@
    Push-Location $ProjectRoot
    try {
        $output = & $BuildPython -c $verifier $StagingRoot 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $output | Write-Host
    if ($exitCode -ne 0) {
        throw "Packaging policy rejected staged output (exit $exitCode)"
    }
}

function Get-RelativeFilePath {
    param(
        [Parameter(Mandatory)] [string] $Root,
        [Parameter(Mandatory)] [string] $FullName
    )

    return ($FullName.Substring($Root.Length).TrimStart('\', '/')) -replace '\\', '/'
}

function Get-InstalledRelativePath {
    <#
        The relative paths the installer places, which is the staged tree minus
        the directories the Inno script excludes and minus the digest file.
    #>
    param([Parameter(Mandatory)] [string] $StagingRoot)

    $excludedPrefixes = @(
        $InstallerExcludedStagingDirectories | ForEach-Object { "$_/" }
    )
    return @(
        Get-ChildItem -LiteralPath $StagingRoot -Recurse -File |
            Sort-Object FullName |
            ForEach-Object { Get-RelativeFilePath -Root $StagingRoot -FullName $_.FullName } |
            Where-Object { $_ -ne "sha256sums.txt" } |
            Where-Object {
                $relative = $_
                $excluded = @(
                    $excludedPrefixes |
                        Where-Object { $relative.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }
                )
                $excluded.Count -eq 0
            }
    )
}

function Write-Sha256Sums {
    <#
        Delegate to packaging/write-digests.ps1, the one digest writer the
        release path uses. The signing job runs the same script over the signed
        artifacts, so a digest file means the same thing in both places.
    #>
    param(
        [Parameter(Mandatory)] [string] $Directory,
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $RelativePaths
    )

    & $DigestScriptPath -Directory $Directory -RelativePath $RelativePaths
}

function Get-AgentVersion {
    param([Parameter(Mandatory)] [string] $AgentRoot)

    $metadata = Get-Content -Raw -LiteralPath (Join-Path $AgentRoot "build-meta.json") | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($metadata.extension_version)) {
        throw "The staged Agent artifact declares no extension_version"
    }
    return $metadata.extension_version
}

function New-DevelopmentAgentBundle {
    <#
        Pack the staged Agent directory into a bundle a developer loads into
        the browser. It is a development artifact: the bundler refuses the
        Store candidate filename, and no release path calls this.
    #>
    param(
        [Parameter(Mandatory)] [string] $AgentRoot,
        [Parameter(Mandatory)] [string] $BundlePath
    )

    & $AgentBundleScriptPath -SourceDirectory $AgentRoot -BundlePath $BundlePath
}

if (-not $SkipAssetBuild) {
    Invoke-RequiredCommand -FilePath "npm.cmd" -Arguments @("ci", "--prefix", (Join-Path $ProjectRoot "frontend"))
    Invoke-RequiredCommand -FilePath "npm.cmd" -Arguments @("run", "build", "--prefix", (Join-Path $ProjectRoot "frontend"))
    Invoke-RequiredCommand -FilePath "npm.cmd" -Arguments @("ci", "--prefix", $ExtensionRoot)
    if (-not $ReleaseMode) {
        Invoke-ExtensionBuild `
            -Arguments (Get-ExtensionReleaseArguments -Verb "") `
            -FailureMessage "The development Agent build failed"
    }
}
if ($ReleaseMode) {
    $storePackage = New-AgentStorePackage -OutputRoot $OutputRoot
}

$packagingSource = New-PackagingSourceRoot -BuildPython $BuildPython -OutputRoot $OutputRoot -ProjectRoot $ProjectRoot
if ($TestInjection -eq "EmbeddedExtensionIdentityMismatch") {
    Set-Content -LiteralPath $packagingSource.EmbeddedIdentityPath -Value 'EXTENSION_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' -Encoding ascii
}
Assert-EmbeddedExtensionIdentity -BuildPython $BuildPython -ProjectRoot $ProjectRoot -ManifestPath $packagingSource.ManifestPath -EmbeddedIdentityPath $packagingSource.EmbeddedIdentityPath

$previousProjectRoot = $env:BRAIN_PROJECT_ROOT
$previousSourceRoot = $env:BRAIN_SOURCE_ROOT
$env:BRAIN_PROJECT_ROOT = $ProjectRoot
$env:BRAIN_SOURCE_ROOT = $packagingSource.SourceRoot
try {
    $pyInstallerArguments = @(
        "--noconfirm", "--clean", "--distpath", $distPath,
        "--workpath", $workPath, $SpecPath
    )
    if ($PyInstallerExecutable) {
        if (-not (Test-Path -LiteralPath $PyInstallerExecutable -PathType Leaf)) {
            throw "PyInstaller executable does not exist: $PyInstallerExecutable"
        }
        Invoke-RequiredCommand -FilePath $PyInstallerExecutable -Arguments $pyInstallerArguments
    } else {
        $pythonArguments = @("-m", "PyInstaller") + $pyInstallerArguments
        Invoke-RequiredCommand -FilePath $BuildPython -Arguments $pythonArguments
    }
} finally {
    $env:BRAIN_PROJECT_ROOT = $previousProjectRoot
    $env:BRAIN_SOURCE_ROOT = $previousSourceRoot
}

$stagingRoot = Join-Path $distPath "Brain"
if (-not (Test-Path -LiteralPath (Join-Path $stagingRoot "Brain.exe") -PathType Leaf)) {
    throw "PyInstaller did not produce the required Brain.exe staging artifact"
}
Copy-DeclaredTopLevelFiles -StagingRoot $stagingRoot
Copy-AgentArtifact -StagingRoot $stagingRoot
Write-ReleaseManifest -StagingRoot $stagingRoot

if ($TestInjection -eq "DevelopmentConfiguration") {
    Set-Content -LiteralPath (Join-Path $stagingRoot "_internal\app\runtime.env") -Value "ENVIRONMENT=development" -Encoding ascii
} elseif ($TestInjection -eq "InstallationClaim") {
    Set-Content -LiteralPath (Join-Path $stagingRoot "_internal\claim.txt") -Value "installation_claim=claim-package-with-secret" -Encoding ascii
}

Invoke-PackagingPolicy -BuildPython $BuildPython -ProjectRoot $ProjectRoot -StagingRoot $stagingRoot
Write-Sha256Sums -Directory $stagingRoot -RelativePaths (Get-InstalledRelativePath -StagingRoot $stagingRoot)

$installerOutput = Join-Path $OutputRoot "installer"
$compiler = Resolve-InnoSetupCompiler -ExplicitCompiler $InnoSetupCompiler
$version = Get-BrainVersion
Invoke-RequiredCommand -FilePath $compiler -Arguments @(
    "/DStagingRoot=$stagingRoot",
    "/DOutputRoot=$installerOutput",
    "/DAppVersion=$version",
    $InnoScriptPath
)
$installerName = "OnlyFans-Conversational-Analytics-Setup-$version-x64.exe"
$installerPath = Join-Path $installerOutput $installerName
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "Inno Setup did not produce the required installer: $installerPath"
}

# The published artifact set: the installer, the Agent archive a user loads into
# the browser, and a digest file computed over both after they exist.
$stagedAgentRoot = Join-Path $stagingRoot "Agent"
$agentVersion = Get-AgentVersion -AgentRoot $stagedAgentRoot
if ($ReleaseMode) {
    if ($agentVersion -ne $storePackage.ExtensionVersion) {
        throw "The staged Agent artifact is not the packaged one: $agentVersion against $($storePackage.ExtensionVersion)"
    }
    $storeCandidateName = "OnlyFans-Conversational-Analytics-Agent-$agentVersion$StoreCandidateSuffix"
    $storeCandidate = Join-Path $installerOutput $storeCandidateName
    Copy-Item -LiteralPath $storePackage.Archive -Destination $storeCandidate
    $storeCandidateDigest = Get-Sha256Digest -Path $storeCandidate
    if ($storeCandidateDigest -ne $storePackage.Sha256) {
        Remove-Item -LiteralPath $storeCandidate -Force
        throw "The Store candidate is not the packaged archive: $($storePackage.Sha256) then $storeCandidateDigest"
    }
    Invoke-ExtensionBuild `
        -Arguments ((Get-ExtensionReleaseArguments -Verb "--audit-package") + "--artifact=$storeCandidate") `
        -FailureMessage "The published Store candidate failed its package audit"
    Write-Sha256Sums -Directory $installerOutput -RelativePaths @($installerName, $storeCandidateName)
    Write-Host "Windows installer ready: $installerPath"
    Write-Host "Store candidate ready: $storeCandidate (sha256:$storeCandidateDigest)"
} else {
    $developmentOutput = Join-Path $OutputRoot "development"
    New-Item -ItemType Directory -Path $developmentOutput | Out-Null
    $developmentBundle = Join-Path $developmentOutput "agent-development-unpacked-$agentVersion.zip"
    New-DevelopmentAgentBundle -AgentRoot $stagedAgentRoot -BundlePath $developmentBundle
    Write-Sha256Sums -Directory $installerOutput -RelativePaths @($installerName)
    Write-Host "Windows installer ready: $installerPath"
    Write-Host "Development Agent bundle ready: $developmentBundle"
}
Write-Host "Published digests ready: $(Join-Path $installerOutput 'sha256sums.txt')"
