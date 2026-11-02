[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $BuildPython,
    [Parameter(Mandatory)] [string] $Inputs,
    [Parameter(Mandatory)] [ValidatePattern('^[0-9a-f]{64}$')] [string] $InputsSha256,
    [Parameter(Mandatory)] [string] $OutputRoot,
    [switch] $ValidateOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$producer = Join-Path $projectRoot 'tools/optional_engine_packaging.py'
if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) { throw 'Build Python is unavailable' }
$arguments = @($producer, '--inputs', $Inputs, '--inputs-sha256', $InputsSha256, '--output', $OutputRoot)
if ($ValidateOnly) { $arguments += '--validate-only' }
& $BuildPython @arguments
if ($LASTEXITCODE -ne 0) { throw 'Optional Engine producer refused the requested operation' }
