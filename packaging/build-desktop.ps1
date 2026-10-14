[CmdletBinding()]
param([Parameter(Mandatory)] [string] $ProjectRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$nativeRoot = Join-Path $ProjectRoot 'desktop/src-tauri'
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

Push-Location $nativeRoot
try {
    & $cargoPath build --locked --release --bin Bridge --no-default-features --features custom-protocol --target x86_64-pc-windows-msvc --target-dir target
    if ($LASTEXITCODE -ne 0) { throw 'Native desktop build failed' }
} finally { Pop-Location }
if (-not (Test-Path -LiteralPath (Join-Path $nativeRoot 'target/x86_64-pc-windows-msvc/release/Bridge.exe') -PathType Leaf)) {
    throw 'Native desktop build produced no Bridge.exe'
}
