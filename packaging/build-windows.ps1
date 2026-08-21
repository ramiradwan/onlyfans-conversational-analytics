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

    # Release builds may proceed only when CI has supplied the signing
    # configuration marker. This unit deliberately does not handle signing
    # material or invoke a signing tool.
    [switch] $ReleaseMode,
    [string] $SigningConfiguration = $env:WINDOWS_SIGNING_CONFIGURATION
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
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)

# Staging directories the installer leaves out; keep in step with the Excludes
# directive in packaging/inno/brain.iss. A digest file that ships inside the
# installation may not list anything under them.
$InstallerExcludedStagingDirectories = @("Agent")

function Assert-ReleaseSigningConfiguration {
    param(
        [switch] $ReleaseMode,
        [string] $SigningConfiguration
    )

    if (-not $ReleaseMode) {
        return
    }
    if ([string]::IsNullOrWhiteSpace($SigningConfiguration)) {
        throw "Release mode requires signing configuration: WINDOWS_SIGNING_CONFIGURATION is not set."
    }
}

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
if ($OutputRoot -eq $ProjectRoot -or $OutputRoot.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Build output must be outside the repository: $OutputRoot"
}
if (Test-Path -LiteralPath $OutputRoot) {
    throw "Build output directory already exists; choose a fresh path: $OutputRoot"
}

Assert-ReleaseSigningConfiguration -ReleaseMode:$ReleaseMode -SigningConfiguration $SigningConfiguration

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
        Record `<sha256> *<relative/path>` for files that sit beside the digest
        file. A path that is absent is a defect in the caller's artifact set, so
        it fails the build instead of being recorded.
    #>
    param(
        [Parameter(Mandatory)] [string] $Directory,
        [Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $RelativePaths
    )

    $lines = foreach ($relative in @($RelativePaths | Sort-Object)) {
        $path = Join-Path $Directory $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "A digest file may only record files beside it; this one is absent: $path"
        }
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = [BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($path))).Replace("-", "").ToLowerInvariant()
        } finally {
            $hasher.Dispose()
        }
        "{0} *{1}" -f $digest, ($relative -replace '\\', '/')
    }
    Set-Content -LiteralPath (Join-Path $Directory "sha256sums.txt") -Value $lines -Encoding ascii
}

function Get-AgentVersion {
    param([Parameter(Mandatory)] [string] $AgentRoot)

    $metadata = Get-Content -Raw -LiteralPath (Join-Path $AgentRoot "build-meta.json") | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($metadata.extension_version)) {
        throw "The staged Agent artifact declares no extension_version"
    }
    return $metadata.extension_version
}

function New-AgentBundle {
    <#
        Pack the staged Agent directory into the archive a user loads into the
        browser. It is packed from the staging tree the packaging policy has
        already accepted, and entry names use forward slashes.
    #>
    param(
        [Parameter(Mandatory)] [string] $AgentRoot,
        [Parameter(Mandatory)] [string] $BundlePath
    )

    Add-Type -AssemblyName System.IO.Compression | Out-Null
    $bundleStream = [IO.File]::Open($BundlePath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $archive = [IO.Compression.ZipArchive]::new($bundleStream, [IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($file in @(Get-ChildItem -LiteralPath $AgentRoot -Recurse -File | Sort-Object FullName)) {
                $relative = Get-RelativeFilePath -Root $AgentRoot -FullName $file.FullName
                $entryStream = $archive.CreateEntry($relative, [IO.Compression.CompressionLevel]::Optimal).Open()
                try {
                    $bytes = [IO.File]::ReadAllBytes($file.FullName)
                    $entryStream.Write($bytes, 0, $bytes.Length)
                } finally {
                    $entryStream.Dispose()
                }
            }
        } finally {
            $archive.Dispose()
        }
    } finally {
        $bundleStream.Dispose()
    }
}

if (-not $SkipAssetBuild) {
    Invoke-RequiredCommand -FilePath "npm.cmd" -Arguments @("ci", "--prefix", (Join-Path $ProjectRoot "frontend"))
    Invoke-RequiredCommand -FilePath "npm.cmd" -Arguments @("run", "build", "--prefix", (Join-Path $ProjectRoot "frontend"))
    Invoke-RequiredCommand -FilePath "npm.cmd" -Arguments @("ci", "--prefix", (Join-Path $ProjectRoot "extension"))
    Invoke-RequiredCommand -FilePath "npm.cmd" -Arguments @("run", "build", "--prefix", (Join-Path $ProjectRoot "extension"))
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

# Reserved signing hook: signing is intentionally not implemented in this unit.
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

# The published artifact set: the installer, the Agent bundle a user loads into
# the browser, and a digest file computed over both after they exist.
$stagedAgentRoot = Join-Path $stagingRoot "Agent"
$agentBundleName = "OnlyFans-Conversational-Analytics-Agent-$(Get-AgentVersion -AgentRoot $stagedAgentRoot)-chrome.zip"
New-AgentBundle -AgentRoot $stagedAgentRoot -BundlePath (Join-Path $installerOutput $agentBundleName)
Write-Sha256Sums -Directory $installerOutput -RelativePaths @($installerName, $agentBundleName)
Write-Host "Windows installer ready: $installerPath"
Write-Host "Agent bundle ready: $(Join-Path $installerOutput $agentBundleName)"
Write-Host "Published digests ready: $(Join-Path $installerOutput 'sha256sums.txt')"
