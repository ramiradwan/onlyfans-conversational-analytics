[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BuildPython,
    [Parameter(Mandatory)] [string] $Wheelhouse,
    [string] $WorkRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script = Join-Path $PSScriptRoot "build-fixed-wheel.py"
if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) { throw "Build Python does not exist: $BuildPython" }
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Fixed SQLCipher wheel builder is missing: $script" }
$arguments = @($script, "--python", [IO.Path]::GetFullPath($BuildPython), "--wheelhouse", [IO.Path]::GetFullPath($Wheelhouse))
if ($WorkRoot) { $arguments += @("--work-root", [IO.Path]::GetFullPath($WorkRoot)) }
& $BuildPython @arguments
if ($LASTEXITCODE -ne 0) { throw "Fixed SQLCipher wheel build failed ($LASTEXITCODE)" }
