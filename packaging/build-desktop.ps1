[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ProjectRoot,
    [string] $TargetDirectory = '',
    [string] $CustomerReleaseConfig = '',
    [string] $BuildPython = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$nativeRoot = Join-Path $ProjectRoot 'desktop/src-tauri'
if ($CustomerReleaseConfig -and (-not $TargetDirectory -or -not $BuildPython)) {
    throw 'The native release input requires an explicit build target directory and Python'
}
$releaseInput = if ($CustomerReleaseConfig) { (Resolve-Path -LiteralPath $CustomerReleaseConfig).Path } else { '' }
$target = if ($TargetDirectory) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TargetDirectory)
} else { Join-Path $nativeRoot 'target' }
if ($releaseInput -and ($target -eq $ProjectRoot -or
        $target.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase))) {
    throw 'Native release build output must be outside the source tree'
}
$configuration = Get-Content -Raw -LiteralPath (Join-Path $nativeRoot 'tauri.conf.json') | ConvertFrom-Json
$brain = Get-Content -Raw -LiteralPath (Join-Path $ProjectRoot 'app/core/config.py')
$version = [regex]::Match($brain, '(?m)^\s*version:\s*str\s*=\s*"(?<version>[^"]+)"').Groups['version'].Value
if (-not $version -or $configuration.version -ne $version) {
    throw 'Native desktop and Brain versions must agree'
}
$cargo = Get-Command cargo.exe -ErrorAction SilentlyContinue
$cargoPath = if ($cargo) { $cargo.Source } else { Join-Path $env:USERPROFILE '.cargo/bin/cargo.exe' }
if (-not (Test-Path -LiteralPath $cargoPath -PathType Leaf)) { throw 'Install the pinned Rust toolchain before packaging' }

# Cargo normally discovers MSVC itself. Some Build Tools installations expose
# their environment without registering an instance with vswhere.
if (-not (Get-Command link.exe -ErrorAction SilentlyContinue)) {
    $programFiles = [Environment]::GetFolderPath('ProgramFilesX86')
    $candidates = @(
        (Join-Path $programFiles 'Microsoft Visual Studio/2022/BuildTools/VC/Auxiliary/Build/vcvars64.bat'),
        (Join-Path $programFiles 'Microsoft Visual Studio/2019/BuildTools/VC/Auxiliary/Build/vcvars64.bat')
    )
    $environmentScript = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ($environmentScript) {
        $environmentBatch = Join-Path ([IO.Path]::GetTempPath()) ('bridge-msvc-' + [Guid]::NewGuid().ToString('N') + '.cmd')
        try {
            @('@echo off', ('call "{0}" >nul' -f $environmentScript), 'if errorlevel 1 exit /b 1', 'set') |
                Set-Content -LiteralPath $environmentBatch -Encoding ascii
            & (Join-Path ([Environment]::SystemDirectory) 'cmd.exe') /d /c $environmentBatch | ForEach-Object {
                if ($_ -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') }
            }
            if ($LASTEXITCODE -ne 0) { throw 'MSVC build environment is unavailable' }
        } finally {
            Remove-Item -LiteralPath $environmentBatch -Force
        }
    }
}

$previousRelease = $env:BRIDGE_BUILD_CUSTOMER_RELEASE_CONFIG
$previousProjection = $env:BRIDGE_BUILD_SUPPORT_PROJECTION
Push-Location $nativeRoot
try {
    $env:BRIDGE_BUILD_CUSTOMER_RELEASE_CONFIG = $null
    $env:BRIDGE_BUILD_SUPPORT_PROJECTION = $null
    if ($releaseInput) {
        $projection = Join-Path $target 'support-channels.json'
        $projectSupport = @'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core.customer_release import validate_customer_release_document

release = Path(sys.argv[2]).read_bytes()
config = validate_customer_release_document(json.loads(release.decode("utf-8")))
output = Path(sys.argv[3])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({
    "source_sha256": hashlib.sha256(release).hexdigest(),
    "support_channels": dict(config.support_channels),
}), encoding="utf-8")
'@
        $projectSupport | & $BuildPython - $ProjectRoot $releaseInput $projection
        if ($LASTEXITCODE -ne 0) { throw 'Native support projection failed' }
        $env:BRIDGE_BUILD_CUSTOMER_RELEASE_CONFIG = $releaseInput
        $env:BRIDGE_BUILD_SUPPORT_PROJECTION = $projection
    }
    & $cargoPath build --locked --release --bin Bridge --no-default-features --features custom-protocol --target x86_64-pc-windows-msvc --target-dir $target
    if ($LASTEXITCODE -ne 0) { throw 'Native desktop build failed' }
} finally {
    $env:BRIDGE_BUILD_CUSTOMER_RELEASE_CONFIG = $previousRelease
    $env:BRIDGE_BUILD_SUPPORT_PROJECTION = $previousProjection
    Pop-Location
}
if (-not (Test-Path -LiteralPath (Join-Path $target 'x86_64-pc-windows-msvc/release/Bridge.exe') -PathType Leaf)) {
    throw 'Native desktop build produced no Bridge.exe'
}
