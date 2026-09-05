<#
    Create a development Chrome extension ZIP from a staged Agent tree.

    This is not the release archive engine. The Store candidate is the archive
    extension/build.mjs --package produces, and this script refuses the Store
    candidate filename so a development bundle can never take its place.

    ZIP metadata is normalized so rebuilding the same staged Agent tree with
    the same runtime produces the same bytes. Archive names are treated as a
    security boundary: ambiguous, rooted, parent-traversing, duplicate, and
    reparse-point inputs are rejected before the output file is created.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string] $SourceDirectory,

    [Parameter(Mandatory)]
    [string] $BundlePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $SourceDirectory -PathType Container)) {
    throw "Agent bundle source directory does not exist: $SourceDirectory"
}

$sourceItem = Get-Item -LiteralPath $SourceDirectory -Force
if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Agent bundle source directory must not be a reparse point: $SourceDirectory"
}

$sourceRoot = [IO.Path]::GetFullPath($SourceDirectory).TrimEnd('\', '/')
$bundleFullPath = [IO.Path]::GetFullPath($BundlePath)
if ((Split-Path -Leaf $bundleFullPath).EndsWith("-chrome.zip", [StringComparison]::OrdinalIgnoreCase)) {
    throw "A development Agent bundle must not be named as the Store candidate: $bundleFullPath"
}
$bundleParent = Split-Path -Parent $bundleFullPath
if (-not (Test-Path -LiteralPath $bundleParent -PathType Container)) {
    throw "Agent bundle destination directory does not exist: $bundleParent"
}
if (Test-Path -LiteralPath $bundleFullPath) {
    throw "Agent bundle destination already exists: $bundleFullPath"
}
if (
    $bundleFullPath.Equals($sourceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $bundleFullPath.StartsWith($sourceRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
) {
    throw "Agent bundle destination must be outside its source directory"
}

$sourceByArchiveName = [Collections.Generic.Dictionary[string, string]]::new(
    [StringComparer]::Ordinal
)
$caseInsensitiveNames = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)

foreach ($item in @(Get-ChildItem -LiteralPath $sourceRoot -Recurse -Force)) {
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Agent bundle source contains a reparse point: $($item.FullName)"
    }
}

foreach ($file in @(Get-ChildItem -LiteralPath $sourceRoot -Recurse -Force -File)) {
    $fullName = [IO.Path]::GetFullPath($file.FullName)
    $sourcePrefix = $sourceRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $fullName.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Agent bundle source escapes its declared root: $fullName"
    }

    $archiveName = $fullName.Substring($sourcePrefix.Length) -replace '\\', '/'
    $segments = @($archiveName.Split('/'))
    if (
        [string]::IsNullOrWhiteSpace($archiveName) -or
        $archiveName.StartsWith('/') -or
        $archiveName.EndsWith('/') -or
        [IO.Path]::IsPathRooted($archiveName) -or
        $archiveName.Contains(':') -or
        $segments.Count -eq 0 -or
        @($segments | Where-Object { $_ -in @('', '.', '..') }).Count -ne 0
    ) {
        throw "Unsafe Agent bundle archive name: $archiveName"
    }
    if (-not $caseInsensitiveNames.Add($archiveName)) {
        throw "Duplicate or case-colliding Agent bundle archive name: $archiveName"
    }
    $sourceByArchiveName.Add($archiveName, $fullName)
}

if ($sourceByArchiveName.Count -eq 0) {
    throw "Agent bundle source contains no files: $sourceRoot"
}

$archiveNames = [string[]]@($sourceByArchiveName.Keys)
[Array]::Sort($archiveNames, [StringComparer]::Ordinal)
$fixedTimestamp = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)

Add-Type -AssemblyName System.IO.Compression | Out-Null
try {
    $bundleStream = [IO.File]::Open(
        $bundleFullPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $archive = [IO.Compression.ZipArchive]::new(
            $bundleStream,
            [IO.Compression.ZipArchiveMode]::Create,
            $false
        )
        try {
            foreach ($archiveName in $archiveNames) {
                $entry = $archive.CreateEntry(
                    $archiveName,
                    [IO.Compression.CompressionLevel]::Optimal
                )
                $entry.LastWriteTime = $fixedTimestamp
                $entry.ExternalAttributes = 0
                $entryStream = $entry.Open()
                try {
                    $bytes = [IO.File]::ReadAllBytes($sourceByArchiveName[$archiveName])
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
} catch {
    if (Test-Path -LiteralPath $bundleFullPath -PathType Leaf) {
        Remove-Item -LiteralPath $bundleFullPath -Force
    }
    throw
}
